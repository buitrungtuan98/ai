"""The render/publish job and the campaign automation logic.

`render_task` is the enqueued unit (one episode), wrapped by `with_render_lock` so only one render
ever runs. Everything is wrapped in try/except: on failure the Task is marked FAILED with the stack
trace, the user is alerted via Telegram, and the worker moves on — one failure never takes down the
queue.

Publishing services and Telegram are imported lazily so this module (and its state-machine tests)
don't require them.
"""
from __future__ import annotations

import json
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
            db.commit()
    return events


# ── Buffer hydration ─────────────────────────────────────────────────────────
def hydrate_buffers(db, *, buffer_size: int | None = None, enqueue=enqueue_render) -> list[int]:
    """Ensure each active campaign has up to `buffer_size` upcoming (not-yet-finished) episodes
    queued. Returns the list of Task ids created. Idempotent — unique(campaign,episode) prevents
    duplicates."""
    size = buffer_size or settings.DEFAULT_BUFFER_SIZE
    created: list[int] = []
    campaigns = db.scalars(select(Campaign).where(Campaign.status == CampaignStatus.active)).all()
    for campaign in campaigns:
        # Query episode numbers directly (never via the cached `campaign.tasks` relationship, which
        # can be stale after we insert Tasks by campaign_id within the same session).
        all_eps = set(
            db.scalars(select(Task.episode_number).where(Task.campaign_id == campaign.id)).all()
        )
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


# ── Publishing / notification dispatch (lazy imports) ────────────────────────
def _publish(channel: Channel, result: video_factory.RenderResult, user: User) -> str:
    if channel.platform == Platform.youtube:
        from services import youtube_service

        return youtube_service.upload_video(channel, result.master_path, result.metadata, user)
    if channel.platform == Platform.facebook:
        from services import facebook_service

        return facebook_service.upload_video(channel, result.master_path, result.metadata)
    raise RuntimeError(f"Unknown platform: {channel.platform}")


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


# ── The job ──────────────────────────────────────────────────────────────────
@with_render_lock
def render_task(task_id: int) -> None:
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

    try:
        gemini_key, pexels_key = _resolve_keys(user)

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
            on_progress=lambda p: set_progress(task_id, 10 + p * 0.8),
        )
        _set_status(db, task, TaskStatus.AUDIO_SYNCED, 88)

        # Park the pre-rendered episode in the buffer pool (durable record).
        buf = BufferPoolItem(
            campaign_id=campaign.id,
            channel_id=channel.id,
            episode_number=task.episode_number,
            video_path=result.master_path,
            thumbnail_path=result.thumbnail_path,
            metadata_json=result.metadata,
            status=BufferStatus.ready,
        )
        db.add(buf)
        db.commit()
        db.refresh(buf)

        _set_status(db, task, TaskStatus.PUBLISHING, 92)
        # Carry campaign-level distribution settings into the publish metadata.
        result.metadata.setdefault("cta", cfg.get("cta"))
        result.metadata.setdefault("privacy", cfg.get("privacy", "public"))
        video_id = _publish(channel, result, user)

        buf.status = BufferStatus.consumed
        buf.consumed_at = datetime.utcnow()
        db.commit()
        _safe_remove(result.master_path, result.thumbnail_path)  # strict cleanup after publish

        _set_status(db, task, TaskStatus.COMPLETED, 100)
        events = advance_campaign(db, campaign)
        _notify(user, f"✅ Episode {task.episode_number} of '{campaign.topic_name}' published ({video_id}).")
        if events.completed:
            _notify(user, f"🎉 Campaign '{campaign.topic_name}' Finished!")
        if events.activated_campaign_id:
            _notify(user, f"▶️ Next campaign #{events.activated_campaign_id} activated.")

        # Keep the render pipeline fed.
        hydrate_buffers(db)

    except Exception as exc:  # noqa: BLE001 — record, alert, and continue the queue
        db.rollback()
        task.status = TaskStatus.FAILED
        task.error_message = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
        db.commit()
        logger.exception("render_task %s failed", task_id)
        _notify(user, f"❌ Episode {task.episode_number} of '{campaign.topic_name}' failed: {exc}")
    finally:
        clear_progress(task_id)
        db.close()
