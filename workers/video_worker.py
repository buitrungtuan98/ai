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

from sqlalchemy import func, select

from core import video_factory
from core.ai_engine import generate_script
from core.config import settings
from core.video_factory import Branding
from database.db_session import SessionLocal
from database.models import BufferPoolItem, Campaign, ChannelClipUsage, Channel, Task, User
from database.types import BufferStatus, CampaignStatus, Platform, TaskStatus
from workers.task_queue import (
    clear_progress,
    enqueue_render,
    set_progress,
    with_render_lock,
)

logger = logging.getLogger(__name__)

_RECENT_CLIP_WINDOW = 400  # remember this many recent clip ids per channel for footage dedupe


def _recent_clip_ids(db, channel_id: int) -> set[int]:
    """Pexels clip ids this channel used recently — handed to produce() so it prefers fresh footage.
    Fail-open: any error yields an empty set (dedupe is advisory and must never block a render)."""
    try:
        rows = db.execute(
            select(ChannelClipUsage.clip_id)
            .where(ChannelClipUsage.channel_id == channel_id)
            .order_by(ChannelClipUsage.id.desc()).limit(_RECENT_CLIP_WINDOW)
        ).all()
        return {cid for (cid,) in rows}
    except Exception:  # noqa: BLE001
        logger.debug("recent clip-id lookup failed", exc_info=True)
        return set()


def _record_clip_usage(db, channel_id: int, clip_ids: list[int]) -> None:
    """Persist the clip ids an episode used so later episodes on this channel avoid them. Fail-open
    and idempotent — existing rows are filtered out first so the unique constraint never trips."""
    ids = set(clip_ids)
    if not ids:
        return
    try:
        existing = {cid for (cid,) in db.execute(
            select(ChannelClipUsage.clip_id).where(
                ChannelClipUsage.channel_id == channel_id,
                ChannelClipUsage.clip_id.in_(ids))).all()}
        for cid in ids - existing:
            db.add(ChannelClipUsage(channel_id=channel_id, clip_id=cid))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.debug("recording clip usage failed", exc_info=True)


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


def _resolve_music(cfg: dict) -> tuple[str | None, dict | None]:
    """Resolve the campaign's music mode to a local file path (+ credit for transparency).

    Modes: "auto" = random CC0 track by mood via Freesound; "file" = operator-supplied path;
    "none"/absent = narration only. Legacy configs with only music_path behave as "file".

    Config truth: a DETERMINISTIC misconfiguration fails loudly — "file" with a missing file
    (raised downstream by produce()) and "auto" without a FREESOUND_API_KEY both mean the operator
    asked for music the box can never deliver; silently publishing music-less videos hid this for
    weeks. A TRANSIENT auto failure (Freesound down, no results) still degrades to no music.
    """
    mode = cfg.get("music_mode") or ("file" if cfg.get("music_path") else "none")
    if mode == "file":
        return cfg.get("music_path") or None, None
    if mode == "auto":
        if not settings.FREESOUND_API_KEY:
            raise RuntimeError(
                "This campaign is set to Auto background music, but FREESOUND_API_KEY is not set "
                "in .env — add the (free) key from freesound.org and Retry, or switch the "
                "campaign's Background music to None."
            )
        from services import music_service

        picked = music_service.pick_music(
            cfg.get("music_mood") or "ambient background",
            settings.FREESOUND_API_KEY,
            os.path.join(settings.MEDIA_ROOT, "music_cache"),
        )
        if picked:
            return picked
        logger.warning("Auto-music unavailable — rendering without music")
    return None, None


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
    # current_episode counts episodes published (starts at 0). The campaign is done once that count
    # REACHES total_episodes — `>` never fires (only N episodes ever publish, so it stops at N == N)
    # and the campaign would sit "active" at N/N forever, never completing or activating the next.
    if campaign.current_episode >= campaign.total_episodes:
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
def _campaign_day_start_utc(campaign: Campaign, now: datetime | None = None) -> datetime:
    """Midnight of 'today' in the campaign's timezone, as a naive-UTC datetime (DB timestamps are
    naive UTC). Falls back to UTC on a bad/absent timezone."""
    from zoneinfo import ZoneInfo

    tz_name = (campaign.config_json or {}).get("timezone") or settings.TIMEZONE
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — a bad tz must not break hydration
        tz = ZoneInfo("UTC")
    now_local = (now or datetime.utcnow()).replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def renders_started_today(db, campaign: Campaign, now: datetime | None = None) -> int:
    """How many episode renders were enqueued for this campaign since its local midnight — the
    basis for the per-campaign daily render cap (Gemini-quota rationing across campaigns)."""
    return db.scalar(
        select(func.count()).select_from(Task).where(
            Task.campaign_id == campaign.id,
            Task.created_at >= _campaign_day_start_utc(campaign, now),
        )
    ) or 0


def hydrate_campaign(db, campaign: Campaign, *, buffer_size: int | None = None, enqueue=enqueue_render) -> list[int]:
    """Ensure ONE campaign has up to `buffer_size` upcoming (not-yet-finished) episodes queued.
    Precedence: explicit arg > campaign config `buffer_size` > global default. Idempotent —
    unique(campaign,episode) prevents duplicates. Returns Task ids created.

    Config `max_per_day` caps how many NEW renders this campaign may start per local day, so one
    campaign can't monopolize the shared Gemini quota when several campaigns/accounts run at once
    (publishing cadence is still governed by posting slots)."""
    cfg = campaign.config_json or {}
    cfg_size = cfg.get("buffer_size")
    size = buffer_size or (int(cfg_size) if cfg_size else None) or settings.DEFAULT_BUFFER_SIZE
    day_budget: int | None = None
    max_per_day = cfg.get("max_per_day")
    if max_per_day:
        day_budget = max(0, int(max_per_day) - renders_started_today(db, campaign))
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
        if day_budget is not None and len(created) >= day_budget:
            logger.info("Campaign %s reached its daily render cap (%s) — resuming tomorrow",
                        campaign.id, max_per_day)
            break
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
    # Close the A/B loop: record WHICH metadata variant went live, so the Performance page can
    # compare real retention per variant instead of rotating variants blindly forever.
    task.ab_variant = (buf.metadata_json or {}).get("variant")
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
    _maybe_trip_circuit_breaker(db, campaign, user)


# ── Failure circuit breaker ──────────────────────────────────────────────────
CONSECUTIVE_FAILURES_TO_PAUSE = 3


def consecutive_failures(db, campaign: Campaign) -> int:
    """Length of the campaign's CURRENT failure streak: finished tasks newest-first, counting
    FAILED until the first non-failed outcome (a publish, a parked review, a scheduled render —
    any of them proves the pipeline works and resets the streak)."""
    statuses = db.scalars(
        select(Task.status)
        .where(Task.campaign_id == campaign.id, Task.finished_at.isnot(None))
        .order_by(Task.finished_at.desc(), Task.id.desc())
        .limit(CONSECUTIVE_FAILURES_TO_PAUSE)
    ).all()
    streak = 0
    for status in statuses:
        if status != TaskStatus.FAILED:
            break
        streak += 1
    return streak


def _maybe_trip_circuit_breaker(db, campaign: Campaign, user: User) -> bool:
    """After N consecutive failures, stop the campaign instead of burning API quota and Telegram
    noise on a systemic fault (dead key, retired model, revoked OAuth). The campaign is set to
    `failed` — hydration and slot publishing skip it, the ▶ Start button resumes it, and if an
    already-queued episode later succeeds anyway, `advance_campaign` re-activates it (self-heal).
    Guarded on `active` so a tripped campaign alerts exactly once."""
    if campaign.status != CampaignStatus.active:
        return False
    if consecutive_failures(db, campaign) < CONSECUTIVE_FAILURES_TO_PAUSE:
        return False
    campaign.status = CampaignStatus.failed
    db.commit()
    logger.warning("Circuit breaker tripped: campaign %s paused after %d consecutive failures",
                   campaign.id, CONSECUTIVE_FAILURES_TO_PAUSE)
    _notify(user, f"⛔ Campaign '{campaign.topic_name}' paused after "
                  f"{CONSECUTIVE_FAILURES_TO_PAUSE} consecutive failures — no new renders will "
                  "start. Check the Task Logs for the cause (API key? quota? channel token?), "
                  "fix it, then press ▶ Start on the campaign to resume.")
    return True


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

    # Loaded inside the try so a transient DB error here can't escape the finally (which closes the
    # session and clears progress) — otherwise the task would strand and the session would leak.
    campaign = channel = user = None
    try:
        campaign = db.get(Campaign, task.campaign_id)
        channel = db.get(Channel, campaign.channel_id)
        user = db.get(User, task.user_id)
        cfg = campaign.config_json or {}
        auto_publish = bool(cfg.get("auto_publish", True))
        # With posting slots configured, auto mode renders ahead into the buffer and the scheduler
        # publishes exactly one episode per slot (ADR-011). Without slots: publish right after render.
        slot_scheduled = auto_publish and bool(cfg.get("posting_slots"))
        # Auto-QC gate (ADR-013): vision-vet footage during render + judge the finished master.
        # Default ON; every check fails open, so a vision-API outage never blocks an episode.
        auto_qc = cfg.get("auto_qc", "on") != "off"

        gemini_key, pexels_key = _resolve_keys(user)
        # Model chain: the user's Credentials choice wins; .env GEMINI_MODEL is the server default.
        gemini_model = user.gemini_model or settings.GEMINI_MODEL
        task.started_at = datetime.utcnow()

        _set_status(db, task, TaskStatus.AI_GENERATION, 5)
        # Episode memory: prior synopses steer the model away from repeats (or continue the serial).
        previous = [
            s for (s,) in db.execute(
                select(Task.synopsis)
                .where(Task.campaign_id == campaign.id, Task.synopsis.isnot(None),
                       Task.episode_number < task.episode_number)
                .order_by(Task.episode_number)
            ).all()
        ][-15:]
        learning = campaign.learning_json or {}
        script = generate_script(
            topic=campaign.topic_name,
            language=cfg.get("language", "en"),
            total_episodes=campaign.total_episodes,
            episode=task.episode_number,
            api_key=gemini_key,
            custom_system_prompt=cfg.get("system_prompt"),
            persona=cfg.get("persona"),
            style_examples=cfg.get("style_examples"),
            catchphrase_open=cfg.get("catchphrase_open"),
            catchphrase_close=cfg.get("catchphrase_close"),
            continuity=cfg.get("continuity", "none"),
            previous_synopses=previous,
            playbook=learning.get("playbook"),
            best_examples=learning.get("best_examples"),
            avoid=learning.get("reject_reasons"),
            self_critique=cfg.get("self_critique", "on") != "off",
            duration_min_s=cfg.get("duration_min_s"),
            duration_max_s=cfg.get("duration_max_s"),
            rate_pct=int(cfg.get("rate_pct", 0)),
            script_depth=cfg.get("script_depth", "standard"),
            model=gemini_model,
        )
        # Episode memory must NEVER be empty after a successful generation — an episode without a
        # synopsis is invisible to every later episode's no-repeat/serial prompt (continuity
        # silently degrades). The schema requires a synopsis; the variant-A title is the fallback.
        task.synopsis = (script.synopsis or script.metadata_variations[0].title)[:300]
        db.commit()

        _set_status(db, task, TaskStatus.RENDERING, 10)
        output_dir = os.path.join(settings.MEDIA_ROOT, "buffer", str(campaign.id))
        music_path, music_credit = _resolve_music(cfg)
        recent_clips = _recent_clip_ids(db, channel.id)  # prefer footage this channel hasn't used

        vet_batch = None
        if auto_qc:
            from core import qc  # lazy, like the publishing services

            vet_batch = qc.make_batch_vetter(gemini_key, model=gemini_model)

        # Render, then let the machine review its own output. A failing verdict triggers exactly
        # one re-render; if it still fails, the episode is parked for human review (the backup).
        qc_report: dict | None = None
        for attempt in (1, 2):
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
                caption_theme=cfg.get("caption_theme", "highlight"),
                motion=cfg.get("motion", "on") != "off",
                color_grade=cfg.get("color_grade"),
                music_path=music_path,
                music_volume=float(cfg.get("music_volume", 0.15)),
                ab_testing=bool(cfg.get("ab_testing", True)),
                title_prefix=cfg.get("title_prefix"),
                affiliate_url=cfg.get("affiliate_url"),
                affiliate_label=cfg.get("affiliate_label"),
                recent_clip_ids=recent_clips,
                motion_seed=task.episode_number,
                vet_batch=vet_batch,
                on_progress=lambda p: set_progress(task_id, 10 + p * 0.8),
            )
            if not auto_qc:
                break
            verdict = qc.run_final_qc(
                result.master_path, api_key=gemini_key, model=gemini_model,
                context=f"The narration language is '{cfg.get('language', 'en')}'.",
            )
            qc_report = {**verdict.as_dict(), "attempts": attempt}
            if verdict.passed:
                break
            if attempt == 1:
                logger.info("Auto-QC rejected episode %s (score %s, issues %s) — re-rendering once",
                            task.episode_number, verdict.score, verdict.issues)
                _safe_remove(result.master_path, result.thumbnail_path)
        qc_failed = qc_report is not None and not qc_report["passed"]
        _record_clip_usage(db, channel.id, result.used_clip_ids)  # so future episodes vary footage
        _set_status(db, task, TaskStatus.AUDIO_SYNCED, 88)

        # Carry distribution settings into the stored metadata so the publish step (now or after
        # review) has everything it needs.
        result.metadata.setdefault("cta", cfg.get("cta"))
        result.metadata.setdefault("privacy", cfg.get("privacy", "public"))
        if cfg.get("affiliate_url"):
            # The pinned comment carries the affiliate link too (with disclosure).
            line = f"{(cfg.get('affiliate_label') or '🔗').strip()} {cfg['affiliate_url']} (affiliate)"
            result.metadata["cta"] = ((result.metadata.get("cta") or "") + "\n" + line).strip()
        if music_credit:
            result.metadata["music_credit"] = music_credit  # per-episode transparency (CC0)
        if qc_report:
            result.metadata["qc"] = qc_report  # machine verdict, visible in the Asset Pool

        # A re-render (e.g. Retry after a reject, or an expired slot item) supersedes any prior
        # buffer row for this episode. Remove it first — (campaign, episode) is unique, so a blind
        # insert would raise IntegrityError and dead-end the Retry in a re-render→fail loop.
        # CRITICAL: renders write to a deterministic per-episode path, so the old row usually
        # points at the SAME path the new render just produced — deleting it blindly would destroy
        # the fresh master/thumbnail (Ready card with no playable file). Skip the new artifacts.
        fresh = {result.master_path, result.thumbnail_path}
        for old in db.scalars(select(BufferPoolItem).where(
            BufferPoolItem.campaign_id == campaign.id,
            BufferPoolItem.episode_number == task.episode_number,
        )).all():
            _safe_remove(*[p for p in (old.video_path, old.thumbnail_path)
                           if p and p not in fresh])
            db.delete(old)
        db.flush()

        # A double Auto-QC failure never publishes: it degrades to review mode for this episode.
        parked_for_review = not auto_publish or qc_failed
        buf = BufferPoolItem(
            campaign_id=campaign.id,
            channel_id=channel.id,
            episode_number=task.episode_number,
            video_path=result.master_path,
            thumbnail_path=result.thumbnail_path,
            metadata_json=result.metadata,
            status=BufferStatus.awaiting_review if parked_for_review else BufferStatus.ready,
        )
        db.add(buf)
        db.commit()
        db.refresh(buf)

        if qc_failed:
            task.finished_at = datetime.utcnow()
            _set_status(db, task, TaskStatus.AWAITING_REVIEW, 90)
            issues = "; ".join((qc_report or {}).get("issues") or []) or "low quality score"
            _notify(user, f"🔍 Episode {task.episode_number} of '{campaign.topic_name}' failed "
                          f"Auto-QC twice ({issues}). It is parked in the Asset Pool for your review.")
        elif not auto_publish:
            task.finished_at = datetime.utcnow()
            _set_status(db, task, TaskStatus.AWAITING_REVIEW, 90)
            _notify(user, f"🎬 Episode {task.episode_number} of '{campaign.topic_name}' is rendered "
                          "and waiting for your review in the Asset Pool.")
        elif slot_scheduled:
            # Pre-rendered and parked; the scheduler publishes it at the next posting slot.
            task.finished_at = datetime.utcnow()
            _set_status(db, task, TaskStatus.SCHEDULED, 90)
        else:
            _publish_buffer(db, task, buf, campaign, channel, user)

        # Keep the render pipeline fed — but isolated: a hydration hiccup (a race inserting the next
        # episode, a transient enqueue error) must NOT flip this just-completed episode to FAILED.
        try:
            hydrate_buffers(db)
        except Exception:  # noqa: BLE001
            logger.warning("post-publish hydration failed for campaign %s", campaign.id, exc_info=True)

    except Exception as exc:  # noqa: BLE001 — record, alert, and continue the queue
        if campaign is not None and user is not None:
            _fail_task(db, task, user, campaign, exc, "render_task")
        else:  # failed before the campaign/user loaded — mark FAILED without the Telegram path
            db.rollback()
            task.status = TaskStatus.FAILED
            task.finished_at = datetime.utcnow()
            task.error_message = f"render_task setup failed: {exc}"
            db.commit()
            logger.exception("render_task setup failed for task %s", task_id)
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
    # Idempotency: a slot tick re-enqueue or a double-clicked Approve can queue this buffer twice.
    # Only ready/awaiting_review items are publishable — anything else was already handled; bail so
    # we never upload the same episode twice or resurrect a consumed row against a deleted file.
    if buf.status not in (BufferStatus.ready, BufferStatus.awaiting_review):
        logger.info("publish_task: buffer %s already handled (status=%s) — skipping",
                    buffer_item_id, buf.status)
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
