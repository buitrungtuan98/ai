"""The render/publish job and the campaign automation logic.

`render_task` is the enqueued unit (one episode), wrapped by `with_render_lock` so only one render
ever runs. Everything is wrapped in try/except: on failure the Task is marked FAILED with the stack
trace, the user is alerted via Telegram, and the worker moves on — one failure never takes down the
queue.

Publishing services and Telegram are imported lazily so this module (and its state-machine tests)
don't require them.
"""
from __future__ import annotations

import logging
import os
import traceback
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from core import video_factory
from core.ai_engine import generate_script
from core.config import settings
from core.video_factory import Branding
from database.db_session import SessionLocal
from database.models import BufferPoolItem, Campaign, Channel, Task, User
from database.types import BufferStatus, CampaignStatus, Platform, TaskStatus
from workers.task_queue import (
    clear_progress,
    enqueue_render,
    set_progress,
    with_render_lock,
)

logger = logging.getLogger(__name__)


# ── Status helper (durable state + coarse Redis mirror) ──────────────────────
def _set_status(db, task: Task, status: TaskStatus, pct: float) -> None:
    task.status = status
    task.progress_pct = int(pct)
    db.commit()
    set_progress(task.id, pct)


def _resolve_keys(user: User) -> tuple[str, str]:
    gemini = user.gemini_api_key or settings.GEMINI_API_KEY
    pexels = user.pexels_api_key or settings.PEXELS_API_KEY
    if not gemini or not pexels:
        raise RuntimeError("Missing Gemini/Pexels API key (set per-user in the dashboard or in .env).")
    return gemini, pexels


def _branding_from_config(cfg: dict) -> Branding:
    b = cfg.get("branding") or {}
    return Branding(
        watermark_path=b.get("watermark_path"),
        tint_color=b.get("tint_color"),
        tint_opacity=float(b.get("tint_opacity", 0.0)),
        mirror=bool(b.get("mirror", False)),
    )


# ── Campaign state machine (pure — returns events, no side-effects beyond DB) ─
@dataclass
class AdvanceEvents:
    completed: bool = False
    activated_campaign_id: int | None = None


def advance_campaign(db, campaign: Campaign) -> AdvanceEvents:
    """Increment the episode counter and apply lifecycle transitions.

    While current_episode <= total_episodes → Active. When it exceeds total_episodes → Completed,
    and the next Pending campaign for the same user is auto-activated.
    """
    events = AdvanceEvents()
    campaign.current_episode += 1
    if campaign.current_episode > campaign.total_episodes:
        campaign.status = CampaignStatus.completed
        db.commit()
        events.completed = True
        nxt = db.scalar(
            select(Campaign)
            .where(Campaign.user_id == campaign.user_id, Campaign.status == CampaignStatus.pending)
            .order_by(Campaign.id)
        )
        if nxt is not None:
            nxt.status = CampaignStatus.active
            db.commit()
            events.activated_campaign_id = nxt.id
    else:
        if campaign.status != CampaignStatus.active:
            campaign.status = CampaignStatus.active
        db.commit()  # always persist the episode increment (not only on a status change)
    return events


# ── Buffer hydration ─────────────────────────────────────────────────────────
def hydrate_campaign(db, campaign: Campaign, *, buffer_size: int | None = None, enqueue=enqueue_render) -> list[int]:
    """Ensure ONE campaign has up to `buffer_size` upcoming (not-yet-finished) episodes queued.
    Precedence: explicit arg > campaign config `buffer_size` > global default. Idempotent —
    unique(campaign,episode) prevents duplicates. Returns Task ids created."""
    cfg_size = (campaign.config_json or {}).get("buffer_size")
    size = buffer_size or (int(cfg_size) if cfg_size else None) or settings.DEFAULT_BUFFER_SIZE
    created: list[int] = []
    # Query episode numbers directly (never via the cached `campaign.tasks` relationship, which can
    # be stale after we insert Tasks by campaign_id within the same session).
    all_eps = set(db.scalars(select(Task.episode_number).where(Task.campaign_id == campaign.id)).all())
    active_eps = set(
        db.scalars(
            select(Task.episode_number).where(
                Task.campaign_id == campaign.id,
                Task.status.notin_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            )
        ).all()
    )
    next_ep = campaign.current_episode + 1
    while len(active_eps) < size and next_ep <= campaign.total_episodes:
        if next_ep not in all_eps:
            task = Task(campaign_id=campaign.id, user_id=campaign.user_id, episode_number=next_ep)
            db.add(task)
            db.commit()
            db.refresh(task)
            task.rq_job_id = enqueue(task.id)
            db.commit()
            created.append(task.id)
            active_eps.add(next_ep)
            all_eps.add(next_ep)
        next_ep += 1
    return created


def hydrate_buffers(db, *, buffer_size: int | None = None, enqueue=enqueue_render) -> list[int]:
    """Ensure every active campaign is topped up to `buffer_size` upcoming episodes."""
    created: list[int] = []
    campaigns = db.scalars(select(Campaign).where(Campaign.status == CampaignStatus.active)).all()
    for campaign in campaigns:
        created += hydrate_campaign(db, campaign, buffer_size=buffer_size, enqueue=enqueue)
    return created


# ── Publishing / notification dispatch (lazy imports) ────────────────────────
def _publish(channel: Channel, video_path: str, metadata: dict, user: User) -> str:
    if channel.platform == Platform.youtube:
        from services import youtube_service

        return youtube_service.upload_video(channel, video_path, metadata, user)
    if channel.platform == Platform.facebook:
        from services import facebook_service

        return facebook_service.upload_video(channel, video_path, metadata)
    raise RuntimeError(f"Unknown platform: {channel.platform}")


def published_url_for(platform: Platform, video_id: str) -> str:
    """Human-clickable URL of a published video (shown in Task Logs)."""
    if platform == Platform.youtube:
        return f"https://www.youtube.com/shorts/{video_id}"
    return f"https://www.facebook.com/{video_id}"


def _notify(user: User, message: str) -> None:
    token = user.telegram_token or settings.TELEGRAM_BOT_TOKEN
    chat = user.telegram_chat_id or settings.TELEGRAM_CHAT_ID
    if not (token and chat):
        return
    try:
        from services import telegram_bot

        telegram_bot.send(token, chat, message)
    except Exception:  # noqa: BLE001 — a failed alert must not fail the job
        logger.exception("Telegram notification failed")


def _safe_remove(*paths: str) -> None:
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            logger.warning("Could not remove %s", p)


# ── Publish step (shared by auto mode and review-approval) ───────────────────
def _publish_buffer(db, task: Task, buf: BufferPoolItem, campaign: Campaign,
                    channel: Channel, user: User) -> str:
    """Upload a buffered episode, record the outcome on the task, clean up, and advance the
    campaign. Raises on failure (caller handles FAILED bookkeeping)."""
    _set_status(db, task, TaskStatus.PUBLISHING, 92)
    video_id = _publish(channel, buf.video_path, buf.metadata_json or {}, user)

    task.published_video_id = video_id
    task.published_url = published_url_for(channel.platform, video_id)
    buf.status = BufferStatus.consumed
    buf.consumed_at = datetime.utcnow()
    db.commit()
    _safe_remove(buf.video_path, buf.thumbnail_path)  # strict cleanup after publish

    task.finished_at = datetime.utcnow()
    _set_status(db, task, TaskStatus.COMPLETED, 100)
    events = advance_campaign(db, campaign)
    _notify(user, f"✅ Episode {task.episode_number} of '{campaign.topic_name}' published: {task.published_url}")
    if events.completed:
        _notify(user, f"🎉 Campaign '{campaign.topic_name}' Finished!")
    if events.activated_campaign_id:
        _notify(user, f"▶️ Next campaign #{events.activated_campaign_id} activated.")
    return video_id


def _fail_task(db, task: Task, user: User, campaign: Campaign, exc: Exception, job: str) -> None:
    db.rollback()
    task.status = TaskStatus.FAILED
    task.finished_at = datetime.utcnow()
    task.error_message = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
    db.commit()
    logger.exception("%s for task %s failed", job, task.id)
    _notify(user, f"❌ Episode {task.episode_number} of '{campaign.topic_name}' failed: {exc}")


# ── The jobs ─────────────────────────────────────────────────────────────────
@with_render_lock
def render_task(task_id: int) -> None:
    """Render one episode into the buffer pool; auto-publish or park for review per campaign."""
    db = SessionLocal()
    task = db.get(Task, task_id)
    if task is None:
        logger.error("render_task: no Task %s", task_id)
        db.close()
        return

    campaign = db.get(Campaign, task.campaign_id)
    channel = db.get(Channel, campaign.channel_id)
    user = db.get(User, task.user_id)
    cfg = campaign.config_json or {}
    auto_publish = bool(cfg.get("auto_publish", True))

    try:
        gemini_key, pexels_key = _resolve_keys(user)
        task.started_at = datetime.utcnow()

        _set_status(db, task, TaskStatus.AI_GENERATION, 5)
        script = generate_script(
            topic=campaign.topic_name,
            language=cfg.get("language", "en"),
            total_episodes=campaign.total_episodes,
            episode=task.episode_number,
            api_key=gemini_key,
            custom_system_prompt=cfg.get("system_prompt"),
        )

        _set_status(db, task, TaskStatus.RENDERING, 10)
        output_dir = os.path.join(settings.MEDIA_ROOT, "buffer", str(campaign.id))
        result = video_factory.produce(
            script=script,
            episode_number=task.episode_number,
            pexels_api_key=pexels_key,
            job_id=str(task.id),
            output_dir=output_dir,
            voice=cfg.get("voice"),
            rate_pct=int(cfg.get("rate_pct", 0)),
            branding=_branding_from_config(cfg),
            subtitle_style=cfg.get("subtitle_style", "word"),
            music_path=cfg.get("music_path") or None,
            music_volume=float(cfg.get("music_volume", 0.15)),
            ab_testing=bool(cfg.get("ab_testing", True)),
            on_progress=lambda p: set_progress(task_id, 10 + p * 0.8),
        )
        _set_status(db, task, TaskStatus.AUDIO_SYNCED, 88)

        # Carry distribution settings into the stored metadata so the publish step (now or after
        # review) has everything it needs.
        result.metadata.setdefault("cta", cfg.get("cta"))
        result.metadata.setdefault("privacy", cfg.get("privacy", "public"))

        buf = BufferPoolItem(
            campaign_id=campaign.id,
            channel_id=channel.id,
            episode_number=task.episode_number,
            video_path=result.master_path,
            thumbnail_path=result.thumbnail_path,
            metadata_json=result.metadata,
            status=BufferStatus.ready if auto_publish else BufferStatus.awaiting_review,
        )
        db.add(buf)
        db.commit()
        db.refresh(buf)

        if auto_publish:
            _publish_buffer(db, task, buf, campaign, channel, user)
        else:
            task.finished_at = datetime.utcnow()
            _set_status(db, task, TaskStatus.AWAITING_REVIEW, 90)
            _notify(user, f"🎬 Episode {task.episode_number} of '{campaign.topic_name}' is rendered "
                          "and waiting for your review in the Asset Pool.")

        # Keep the render pipeline fed.
        hydrate_buffers(db)

    except Exception as exc:  # noqa: BLE001 — record, alert, and continue the queue
        _fail_task(db, task, user, campaign, exc, "render_task")
    finally:
        clear_progress(task_id)
        db.close()


def publish_task(buffer_item_id: int) -> None:
    """Publish an approved (or retried) buffered episode. Network-bound — no render lock needed."""
    db = SessionLocal()
    buf = db.get(BufferPoolItem, buffer_item_id)
    if buf is None:
        logger.error("publish_task: no BufferPoolItem %s", buffer_item_id)
        db.close()
        return
    campaign = db.get(Campaign, buf.campaign_id)
    channel = db.get(Channel, buf.channel_id)
    user = db.get(User, campaign.user_id)
    task = db.scalar(
        select(Task).where(Task.campaign_id == buf.campaign_id,
                           Task.episode_number == buf.episode_number)
    )
    if task is None:
        logger.error("publish_task: no Task for buffer %s", buffer_item_id)
        db.close()
        return
    try:
        _publish_buffer(db, task, buf, campaign, channel, user)
    except Exception as exc:  # noqa: BLE001
        _fail_task(db, task, user, campaign, exc, "publish_task")  # rolls back first
        # Keep the file + buffer row so the operator can retry the upload without re-rendering.
        buf.status = BufferStatus.awaiting_review
        db.commit()
    finally:
        clear_progress(task.id)
        db.close()
