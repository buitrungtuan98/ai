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
from functools import partial

from sqlalchemy import func, select

from core import failure, slop_gate, video_factory
from core import vibe as vibe_mod
from core.ai_engine import VideoScript, generate_image, generate_script
from core.config import settings
from core.video_factory import Branding
from database.db_session import SessionLocal
from database.models import BufferPoolItem, Campaign, ChannelClipUsage, Channel, Task, User
from database.types import BufferStatus, CampaignStatus, ChannelStatus, Platform, TaskStatus
from workers.task_queue import (
    clear_progress,
    enqueue_publish,
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


def _append_journey(task: Task, step: str, note: str) -> None:
    """Add one entry to the episode's decision journey (ADR-084). Reassigns the JSON column — an
    in-place mutation would not persist. Caller commits."""
    rj = dict(task.render_json or {})
    rj["journey"] = ((rj.get("journey") or []) + [{"step": step, "note": note[:200]}])[-12:]
    task.render_json = rj


def _cast_with_ref_urls(characters) -> list[dict] | None:
    """Attach a PUBLIC `ref_url` to each character that has an uploaded reference token (ADR-055), so
    a Pollinations image-editing model can fetch it over the internet. Skipped when the box has no
    real public base (dev localhost) — then only the Gemini leg (local file) can use the reference."""
    if not characters:
        return None
    base = (settings.PUBLIC_BASE_URL or settings.OAUTH_REDIRECT_BASE or "").rstrip("/")
    public = base.startswith(("http://", "https://")) and "127.0.0.1" not in base and "localhost" not in base
    out = []
    for c in characters:
        c = dict(c)
        if public and c.get("ref_token"):
            c["ref_url"] = f"{base}/studio/ref/{c['ref_token']}"
        out.append(c)
    return out


def _resolve_keys(user: User, *, visual_source: str = "stock") -> tuple[str, str]:
    """Gemini (script/QC/Studio-image) key is always required. Stock mode also needs a Pexels key;
    Studio mode draws its visuals with the Gemini image model, so Pexels is optional there."""
    gemini = user.gemini_api_key or settings.GEMINI_API_KEY
    pexels = user.pexels_api_key or settings.PEXELS_API_KEY
    if not gemini:
        raise RuntimeError("Missing Gemini API key (set per-user in the dashboard or in .env).")
    if visual_source != "studio" and not pexels:
        raise RuntimeError("Missing Pexels API key (set per-user in the dashboard or in .env).")
    return gemini, pexels or ""


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
    # Clamp at the total (R22): a manual Retry of a dead episode on an already-completed campaign
    # publishes late, and without the clamp each one drifted current_episode past total_episodes
    # and re-entered the completion branch — re-notifying "Finished!" and activating one MORE
    # pending campaign per retry.
    advanced = campaign.current_episode + 1
    campaign.current_episode = min(advanced, campaign.total_episodes) \
        if campaign.total_episodes else advanced
    # current_episode counts episodes published (starts at 0). The campaign is done once that count
    # REACHES total_episodes — `>` never fires (only N episodes ever publish, so it stops at N == N)
    # and the campaign would sit "active" at N/N forever, never completing or activating the next.
    if campaign.current_episode >= campaign.total_episodes:
        already_completed = campaign.status == CampaignStatus.completed
        campaign.status = CampaignStatus.completed
        db.commit()
        events.completed = not already_completed
        # Activate the next pending campaign only on the TRANSITION into completed — a late
        # publish on a finished campaign must not start yet another queued-up future campaign.
        nxt = None if already_completed else db.scalar(
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


def enqueue_task(task) -> str:
    """Route a task to the job that can actually build it (ADR-082): a compilation re-runs the
    concat, an episode re-runs the render. Every Retry path goes through here — enqueueing
    render_task for a compilation would try to write a script for it."""
    from workers.task_queue import enqueue_compile

    if (getattr(task, "video_kind", None) or "episode") == "compilation":
        return enqueue_compile(task.id)
    return enqueue_render(task.id)


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
    # "Active" = still on its way to publication. COMPLETED/FAILED/CANCELLED are all finished
    # outcomes: leaving CANCELLED in here would make hydration believe that episode is still coming
    # and starve the buffer forever (ADR-064).
    active_eps = set(
        db.scalars(
            select(Task.episode_number).where(
                Task.campaign_id == campaign.id,
                Task.status.notin_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]),
            )
        ).all()
    )
    # Compilations use sentinel numbers (9001+) and are EXTRA content — one waiting for review must
    # not count against the buffer of ordinary upcoming episodes (ADR-082).
    from core.compilation import COMPILATION_EPISODE_BASE

    active_eps = {e for e in active_eps if e < COMPILATION_EPISODE_BASE}
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
def _publish(channel: Channel, video_path: str, metadata: dict, user: User,
             *, pending_video_id: str | None = None, on_pending=None,
             retrying: bool = False) -> str:
    """Upload to whichever platform this channel is. `pending_video_id`/`on_pending` are Facebook's
    duplicate-post guard (ADR-073); `retrying` arms YouTube's title-match guard (ADR-087) — its
    resumable upload only protects within one call, not across a fresh retry job."""
    if channel.platform == Platform.youtube:
        from services import youtube_service

        return youtube_service.upload_video(channel, video_path, metadata, user,
                                            check_existing=retrying)
    if channel.platform == Platform.facebook:
        from services import facebook_service

        return facebook_service.upload_video(channel, video_path, metadata,
                                             pending_video_id=pending_video_id,
                                             on_pending=on_pending)
    raise RuntimeError(f"Unknown platform: {channel.platform}")


def published_url_for(platform: Platform, video_id: str, video_format: str = "short") -> str:
    """Human-clickable URL of a published video (shown on the episode page and the activity feed).

    The format decides the shape on BOTH platforms (ADR-073): a vertical short is a YouTube Short /
    a Facebook Reel, long-form is a normal watch page. The Facebook branch used to build
    `facebook.com/{video_id}`, which is not a video permalink at all — every "View ↗" for a Facebook
    publish led nowhere."""
    short = (video_format or "short") != "long"
    if platform == Platform.youtube:
        return (f"https://www.youtube.com/shorts/{video_id}" if short
                else f"https://www.youtube.com/watch?v={video_id}")
    from services import facebook_service

    return facebook_service.permalink(video_id, reel=short)


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


# ── Shared review actions (used by the manual Review page AND the autopilot reviewer) ──
class ReviewConflict(RuntimeError):
    """Approving now would act on stale or missing evidence — the caller must surface it, not
    approve anyway (a silent approve of a vanished file or of an episode mid-re-render published
    the wrong thing or nothing, R22)."""


_WORKING_TASK_STATUSES = (TaskStatus.PENDING_QUEUE, TaskStatus.AI_GENERATION,
                          TaskStatus.AUDIO_SYNCED, TaskStatus.RENDERING, TaskStatus.PUBLISHING)


def apply_approve(db, item) -> None:
    """Approve a review render: it leaves the review queue immediately and its publish job is queued.
    DRY: the /assets approve route and `core.autopilot`'s reviewer both go through here.

    Both state changes matter (ADR-064). The buffer row moves `awaiting_review` → `ready`, so the
    review queue, its counters and the Review page all drop it the moment it is approved — leaving it
    `awaiting_review` until the upload finished made one episode read as "approved" and "still waiting
    for review" at the same time, and invited a second approve click. The Task moves to SCHEDULED, not
    PENDING_QUEUE: it is rendered and waiting to go out, and calling it "queued" both mislabelled the
    stage and inflated the render-queue count with something that was never a render job.

    Raises ReviewConflict instead of approving blind (R22): a vanished file cannot publish, and an
    episode whose task is mid-render/publish is being changed under this row's feet — the finishing
    job would delete the row this approve just queued."""
    if not (item.video_path and os.path.exists(item.video_path)):
        raise ReviewConflict("The video file is no longer on disk — re-render it instead.")
    task = db.scalar(select(Task).where(
        Task.campaign_id == item.campaign_id, Task.episode_number == item.episode_number))
    if task is not None and task.status in _WORKING_TASK_STATUSES:
        raise ReviewConflict("A render or publish for this episode is already in flight — "
                             "let it finish first.")
    now = datetime.utcnow()
    item.status = BufferStatus.ready
    item.ready_at = now  # the expiry sweep ages a reviewed item from its approval, not its render
    # Durable publish intent (R22): the enqueue below can be lost (crash/redeploy between commit and
    # enqueue, Redis flush) — this marker lets the hourly reconciler re-issue the job instead of the
    # approved episode silently aging out in SCHEDULED.
    item.metadata_json = {**(item.metadata_json or {}), "publish_requested_at": now.isoformat()}
    if task is not None:
        if task.status == TaskStatus.FAILED:
            # This approve IS the retry of the failed step — count it, which also arms the
            # platform-side duplicate check on the re-upload.
            task.retry_count += 1
        task.status = TaskStatus.SCHEDULED  # the publish job drives it to PUBLISHING → COMPLETED
        task.error_message = None
        _append_journey(task, "Review", "approved — publish queued")
    db.commit()
    enqueue_publish(item.id)


def _judge_script_safe(user, channel, cfg, fp: dict, api_key: str, model: str):
    """Run the AI script judge (ADR-079, C2) when it is allowed to run: campaign toggle on
    (default), and the daily AI budget below its 80% reserve — a strategy call never outbids
    rendering. Returns a ScriptVerdict or None ('no verdict'); NEVER raises — the deterministic
    gate has already run, and a judge outage must not stop the factory."""
    if cfg.get("script_judge", "on") == "off":
        return None
    try:
        from core.usage import reserve_reached

        if reserve_reached(user):
            logger.info("Script judge skipped — AI budget reserve reached")
            return None
        from core.ai_engine import judge_script

        return judge_script(fp["narration"], fp["title"], api_key=api_key,
                            language=cfg.get("language", "en"), model=model)
    except Exception:  # noqa: BLE001 — no verdict beats no factory
        logger.warning("Script judge unavailable — proceeding without a verdict", exc_info=True)
        return None


def _recent_fingerprints(db, campaign, before_episode: int) -> list[dict]:
    """The narration/title fingerprints of this campaign's most recent episodes (ADR-079) — what a
    new script is judged against. Reads the persisted fingerprint of finished episodes AND the saved
    script of in-flight ones; episodes from before the fingerprint existed simply contribute
    nothing, which narrows the check rather than failing it."""
    rows = db.execute(
        select(Task.episode_number, Task.render_json)
        .where(Task.campaign_id == campaign.id, Task.render_json.isnot(None),
               Task.episode_number < before_episode)
        .order_by(Task.episode_number.desc()).limit(slop_gate.RECENT_EPISODES)).all()
    out: list[dict] = []
    for ep, rj in rows:
        rj = rj or {}
        if rj.get("narration"):
            out.append({"episode": ep, "narration": rj["narration"], "title": rj.get("title", "")})
        elif isinstance(rj.get("script"), dict):   # in-flight checkpoint — still a real episode
            sc = rj["script"]
            out.append({"episode": ep,
                        "narration": " ".join(s.get("narration", "") for s in sc.get("scenes", [])),
                        "title": ((sc.get("metadata_variations") or [{}])[0]).get("title", "")})
    return out


def drop_script_checkpoint(task) -> None:
    """Forget a persisted resume script (ADR-069). Called wherever the operator's intent is a
    REROLL — reject, discard & re-render — because those exist precisely to get different content;
    the checkpoint exists to rebuild the same content after an infrastructure failure. The
    checkpointed stills need no explicit invalidation: they are named by prompt hash, so a fresh
    script simply never matches them."""
    rj = dict(task.render_json or {})
    if rj.pop("script", None) is not None:
        task.render_json = rj or None


def apply_reject(db, item, reason: str = "", *, rerender: bool = False,
                 automatic: bool = False) -> None:
    """Reject a render: delete its files, mark the buffer row rejected + the task FAILED with the
    reason, and feed the reason into the campaign's avoid-list (learning loop). When `rerender`,
    also queue a fresh render of the same episode — autopilot rejects re-render so the episode
    regenerates with the new avoid-note; the manual Review reject leaves it FAILED for an explicit
    Retry (unchanged behavior).

    `automatic` marks a rejection the autopilot decided, so it is charged to the autopilot's own
    re-render budget (`auto_reject_count`) and not to the operator's. The caller enforces the cap —
    this only keeps the count (ADR-076)."""
    _safe_remove(*[p for p in (item.video_path, item.thumbnail_path) if p])
    item.status = BufferStatus.rejected
    reason = (reason or "").strip()[:200]
    task = db.scalar(select(Task).where(
        Task.campaign_id == item.campaign_id, Task.episode_number == item.episode_number))
    if task is not None:
        task.status = TaskStatus.FAILED
        # The "(auto-review)" tag is structural, not free text (R22): the autopilot's
        # their-decision-stands checks used to substring-match the reason, so a HUMAN writing
        # "auto-review missed this" read as the bot's own reject and was silently re-rendered.
        who = " (auto-review)" if automatic else ""
        task.error_message = (f"Rejected in review{who}: {reason}") if reason else \
            f"Rejected in review{who}. Use Retry to re-render."
        drop_script_checkpoint(task)  # judged bad — the re-render must write a FRESH script
    # The operator's/AI's reason becomes a permanent avoid-note (Loop 1 learning signal) — but only
    # for ORDINARY episodes. A compilation's rejection ("wrong episode order", "too long") is about
    # editing, not writing; feeding it to the scriptwriter would steer every future SCRIPT away
    # from a complaint that was never about scripts (ADR-082).
    if reason and (task is None or (task.video_kind or "episode") != "compilation"):
        campaign = db.get(Campaign, item.campaign_id)
        if campaign is not None:
            learning = dict(campaign.learning_json or {})
            reasons = (learning.get("reject_reasons") or [])[-9:]
            learning["reject_reasons"] = reasons + [reason]
            campaign.learning_json = learning
    db.commit()
    if rerender and task is not None:
        task.status = TaskStatus.PENDING_QUEUE
        task.error_message = None
        task.progress_pct = 0
        task.retry_count += 1
        if automatic:
            task.auto_reject_count = (task.auto_reject_count or 0) + 1
        clear_progress(task.id)  # no ghost % carries into the re-queued render (F1)
        db.commit()
        # Kind-aware on purpose (ADR-085): "every Retry path goes through enqueue_task" — this one
        # didn't, so a rejected compilation would have been handed to render_task to SCRIPT it.
        task.rq_job_id = enqueue_task(task)
        db.commit()


class ChannelExpired(RuntimeError):
    """The channel's credential is dead, so a rendered episode simply waits (ADR-073) — it is not a
    failure of the episode, and must not consume a retry or read as a broken render."""


def _notify_channel_expired(channel: Channel, user: User, exc: Exception) -> None:
    """Tell the operator their episodes are waiting on a fresh token — ONCE per channel per day
    (R22). Silence here left approved episodes parked with no visible reason; per-episode alerts
    would spam every slot tick. Best-effort: a Redis hiccup drops the dedupe, never the publish."""
    try:
        from workers.task_queue import conn as _conn

        if not _conn.set(f"notify:channel-expired:{channel.id}", "1", nx=True, ex=86400):
            return
    except Exception:  # noqa: BLE001 — dedupe is a nicety
        pass
    _notify(user, f"🔑 “{channel.channel_name}” cannot publish: {exc}")


# ── Publish step (shared by auto mode and review-approval) ───────────────────
def _publish_buffer(db, task: Task, buf: BufferPoolItem, campaign: Campaign,
                    channel: Channel, user: User) -> str:
    """Upload a buffered episode, record the outcome on the task, clean up, and advance the
    campaign. Raises on failure (caller handles FAILED bookkeeping)."""
    # An expired channel cannot publish anything (ADR-073). Skipping is deliberate: the episode is
    # already rendered, so the honest outcome is "waiting for a working token", not FAILED. It stays
    # in the buffer and goes out on the next slot once the operator pastes one.
    if channel.status == ChannelStatus.expired:
        raise ChannelExpired(
            f"“{channel.channel_name}” has an expired access token, so this episode cannot publish "
            "yet. It stays in the buffer — paste a fresh token on the Channels page and it goes out "
            "at the next slot.")

    _set_status(db, task, TaskStatus.PUBLISHING, 92)
    # Publish jobs carry NO live progress entry (R22): the stall watchdog's limit (render
    # job_timeout + grace) sits BELOW the publish job's own RQ timeout, so a legal >55-minute
    # upload read as a wedged render and was os._exit-killed mid-transfer — the exact failure the
    # generous publish timeout exists to avoid. The upload's kill authority is its RQ timeout.
    clear_progress(task.id)
    fmt = (campaign.config_json or {}).get("video_format", "short")
    meta = {**(buf.metadata_json or {}), "video_format": fmt}

    # Persist evidence of THIS attempt before any bytes go up (R22): the slot tick, catch-up and
    # Publish-now never increment retry_count, so a re-publish after a mid-upload kill used to run
    # with the platform duplicate check disarmed and could post the episode twice.
    prior_attempts = int((buf.metadata_json or {}).get("publish_attempts") or 0)
    buf.metadata_json = {**(buf.metadata_json or {}), "publish_attempts": prior_attempts + 1}
    db.commit()

    def remember_pending(vid: str) -> None:
        """Persist the reserved video id BEFORE the bytes go up, so a retry after a timeout can ask
        Facebook whether that upload already landed instead of posting it twice (ADR-073)."""
        buf.metadata_json = {**(buf.metadata_json or {}), "pending_video_id": vid}
        db.commit()

    video_id = _publish(channel, buf.video_path, meta, user,
                        pending_video_id=(buf.metadata_json or {}).get("pending_video_id"),
                        on_pending=remember_pending,
                        retrying=bool(task.retry_count) or prior_attempts > 0)

    # Publish success is ONE commit (R22): published ids, the consumed buffer and the COMPLETED
    # status land together. They used to span three commits with file I/O in between, so a kill in
    # the window left "published on the platform, FAILED in the DB" — which the retry paths then
    # re-rendered and re-published as a duplicate.
    now = datetime.utcnow()
    task.published_video_id = video_id
    task.published_url = published_url_for(channel.platform, video_id, fmt)
    # Close the A/B loop: record WHICH metadata variant went live, so the Performance page can
    # compare real retention per variant instead of rotating variants blindly forever.
    task.ab_variant = (buf.metadata_json or {}).get("variant")
    buf.status = BufferStatus.consumed
    buf.consumed_at = now
    task.finished_at = now
    task.status = TaskStatus.COMPLETED
    task.progress_pct = 100
    _append_journey(task, "Publish attempt", f"published — {video_id}")
    db.commit()
    set_progress(task.id, 100)
    if channel.platform == Platform.youtube:
        # Series playlist (ADR-080): binge navigation + session-time signal + watch-hours toward
        # the money threshold. Fail-open inside — the publish above already succeeded (and is now
        # durably recorded, so a crash here can no longer erase it).
        from services.youtube_service import add_to_series_playlist

        add_to_series_playlist(channel, campaign, db, video_id)
    if (task.video_kind or "episode") == "compilation":
        # A compilation is BUILT FROM the library; retaining it would recurse best-ofs into best-ofs.
        _safe_remove(buf.video_path, buf.thumbnail_path)
    else:
        # Retain the published master in the campaign library (ADR-082) — the raw material for
        # best-of compilations, the long-form format that actually pays. Capped inside; fail-open
        # to plain deletion. The thumbnail is not needed again either way.
        from core import compilation

        compilation.retain_master(campaign.id, task.episode_number, buf.video_path)
        _safe_remove(buf.video_path, buf.thumbnail_path)

    if (task.video_kind or "episode") == "compilation":
        # A best-of is EXTRA content: it must not advance the campaign's episode count — the next
        # ordinary episode would silently be skipped.
        _notify(user, f"🎬 Best-of compilation for '{campaign.topic_name}' published: "
                      f"{task.published_url}")
        return video_id
    events = advance_campaign(db, campaign)
    _notify(user, f"✅ Episode {task.episode_number} of '{campaign.topic_name}' published: {task.published_url}")
    if events.completed:
        _notify(user, f"🎉 Campaign '{campaign.topic_name}' Finished!")
    if events.activated_campaign_id:
        _notify(user, f"▶️ Next campaign #{events.activated_campaign_id} activated.")
    return video_id


def _mark_channel_expired(db, campaign: Campaign, exc: Exception) -> bool:
    """A dead Page/OAuth token means the CHANNEL is broken, not this episode (ADR-072).

    Nothing used to set `ChannelStatus.expired`, so the Channels page kept showing "● Active" for a
    channel that could no longer publish anything, and its "Expired token" pill and filter chip were
    decoration. Marking it here is what makes those honest — and what stops the operator debugging
    episode after episode when the real fix is one new token.

    Platform-aware (R22): the escape hatch was Facebook-only, so a revoked YouTube refresh token
    (e.g. an OAuth app left in Testing revokes weekly) failed every publish on every campaign with
    a misdiagnosed banner while the Channels page said Active — the operator looped on Approve
    against a dead credential and the autopilot burned its retries the same way."""
    if campaign is None:
        return False
    from services.facebook_service import FacebookAuthError

    auth_shaped = isinstance(exc, FacebookAuthError)
    if not auth_shaped:
        low = str(exc).lower()
        auth_shaped = "invalid_grant" in low or "reconnect the account" in low
    if not auth_shaped:
        return False
    channel = db.get(Channel, campaign.channel_id)
    if channel is None or channel.status == ChannelStatus.expired:
        return False
    # Verify before condemning (ADR-083): one cheap probe, exactly what the operator does by hand
    # when they re-paste the same token "and it works". Only a DEFINITE rejection retires the
    # channel — a rate-limited or unreachable provider is "not now", never "not this token". An
    # operator reported living this loop: healthy token, hourly false expiries, re-pasted daily.
    if channel.platform == Platform.youtube:
        from services.youtube_service import token_definitely_dead
    else:
        from services.facebook_service import token_definitely_dead

    if not token_definitely_dead(channel):
        logger.warning("Channel %s hit an auth-class error but its token re-verified — NOT marking "
                       "expired (transient misclassification?): %s", channel.id, exc)
        return False
    channel.status = ChannelStatus.expired
    db.commit()
    logger.warning("Channel %s marked expired: %s", channel.id, exc)
    return True


def _fail_task(db, task: Task, user: User, campaign: Campaign, exc: Exception, job: str) -> None:
    from services.facebook_service import scrub

    db.rollback()
    task.status = TaskStatus.FAILED
    task.finished_at = datetime.utcnow()
    # Scrubbed before storing: this string is rendered on the episode page and in the alert bell, and
    # a Graph traceback carries `access_token=…` in the request URL (ADR-072). The actual exception
    # leads and the traceback follows (R22) — the first line the operator reads is the error, and
    # `core.failure` classifies the exception summary rather than the frames' file paths.
    summary = scrub(f"{type(exc).__name__}: {exc}")[:1200]
    if job == "publish_task":
        summary = ("Publish failed — the rendered video is safe in the buffer; fix the cause "
                   "below, then Retry re-uploads it without a re-render.\n" + summary)
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    task.error_message = (summary + "\n\n" + scrub(trace)[-(4000 - len(summary) - 2):])
    _append_journey(task, "Publish attempt" if job == "publish_task" else "Render",
                    f"failed — {scrub(str(exc))[:150]}")
    db.commit()
    logger.exception("%s for task %s failed", job, task.id)
    expired = _mark_channel_expired(db, campaign, exc)
    note = " The channel is marked expired — paste a fresh Page token." if expired else ""
    _notify(user, f"❌ Episode {task.episode_number} of '{campaign.topic_name}' failed: "
                  f"{scrub(str(exc))}{note}")
    _maybe_trip_circuit_breaker(db, campaign, user)


# ── Failure circuit breaker ──────────────────────────────────────────────────
CONSECUTIVE_FAILURES_TO_PAUSE = 3


def consecutive_failures(db, campaign: Campaign) -> int:
    """Length of the campaign's CURRENT failure streak: finished tasks newest-first, counting
    FAILED until the first non-failed outcome (a publish, a parked review, a scheduled render —
    any of them proves the pipeline works and resets the streak).

    Infrastructure failures (worker stall/restart, killed upload, expired buffer) and review
    rejections are TRANSPARENT — skipped, neither counted nor streak-breaking (R22). A wedged box
    is not evidence the campaign's config is broken (the contract the watchdog always claimed),
    and skipping rather than resetting means a genuine systemic fault interleaved with a worker
    restart still trips the breaker."""
    rows = db.execute(
        select(Task.status, Task.error_message)
        .where(Task.campaign_id == campaign.id, Task.finished_at.isnot(None))
        .order_by(Task.finished_at.desc(), Task.id.desc())
        .limit(CONSECUTIVE_FAILURES_TO_PAUSE * 4)  # skipped rows need lookback beyond the cap
    ).all()
    streak = 0
    for status, message in rows:
        if status != TaskStatus.FAILED:
            break
        if failure.is_infrastructure(message) or failure.is_reject(message):
            continue
        streak += 1
        if streak >= CONSECUTIVE_FAILURES_TO_PAUSE:
            break
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
def requalify_task(buffer_item_id: int) -> None:
    """Re-run Auto-QC on a parked render whose judge was unavailable (ADR-084) — the “Run QC now”
    button. Updates the stored verdict in place; the item STAYS parked either way (routing decisions
    belong to review/autopilot, not to this job). Cheap: frames + one vision call."""
    from core import qc

    db = SessionLocal()
    try:
        buf = db.get(BufferPoolItem, buffer_item_id)
        if buf is None or not (buf.video_path and os.path.exists(buf.video_path)):
            logger.warning("requalify_task: buffer %s missing or file gone", buffer_item_id)
            return
        campaign = db.get(Campaign, buf.campaign_id)
        user = db.get(User, campaign.user_id) if campaign else None
        if user is None:
            return
        gemini_key = user.gemini_api_key or settings.GEMINI_API_KEY
        if not gemini_key:
            return
        cfg = campaign.config_json or {}
        det = qc.run_deterministic_qc(buf.video_path)
        verdict = qc.run_final_qc(buf.video_path, api_key=gemini_key,
                                  model=(user.gemini_model or settings.GEMINI_MODEL),
                                  context=f"The narration language is '{cfg.get('language', 'en')}'.")
        prior = (buf.metadata_json or {}).get("qc") or {}
        report = {"passed": det.passed and verdict.passed, "score": verdict.score,
                  "issues": det.issues + verdict.issues,
                  "attempts": int(prior.get("attempts") or 1)}
        if verdict.unavailable:
            report.update(unavailable=True, unavailable_reason=verdict.unavailable_reason,
                          prior_fail=bool(prior.get("prior_fail")))
        buf.metadata_json = {**(buf.metadata_json or {}), "qc": report}
        db.commit()
        logger.info("Requalified buffer %s: passed=%s score=%s unavailable=%s",
                    buffer_item_id, report["passed"], report["score"],
                    report.get("unavailable", False))
    except Exception:  # noqa: BLE001 — a requalify hiccup must not disturb the queue
        db.rollback()
        logger.warning("requalify_task failed for buffer %s", buffer_item_id, exc_info=True)
    finally:
        db.close()


@with_render_lock
def compile_task(task_id: int) -> None:
    """Build a best-of compilation from the campaign's library (ADR-082): a stream-copy concat of
    the top-retention masters — near-zero CPU, zero AI — with chapters and a poster thumbnail. It
    holds the render lock like any job (cheap or not, one ffmpeg at a time is the law of this box)
    and ALWAYS parks for review: a ten-minute video that will anchor the channel's long-form shelf
    gets one human look, in every mode."""
    from core import compilation, media, qc
    from core.captions import teaser
    from core.ffmpeg_runner import run_ffmpeg
    from core.thumbnail import generate_thumbnail
    from core.video_factory import build_concat_args

    db = SessionLocal()
    task = db.get(Task, task_id)
    if task is None:
        logger.error("compile_task: no Task %s", task_id)
        db.close()
        return
    # Same idempotency rule as render_task (R22): a duplicate/orphaned job never re-builds a
    # compilation that already finished or was cancelled.
    if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED) or _job_is_stale(task):
        logger.info("compile_task: task %s already handled (status=%s) or job superseded — skipping",
                    task_id, task.status)
        db.close()
        return
    campaign = user = None
    try:
        campaign = db.get(Campaign, task.campaign_id)
        user = db.get(User, task.user_id)
        _set_status(db, task, TaskStatus.RENDERING, 10)
        top_n = int((task.render_json or {}).get("top_n") or compilation.DEFAULT_TOP_N)
        picked = compilation.compilable_episodes(db, campaign)[:top_n]
        if len(picked) < 2:
            raise RuntimeError(
                f"only {len(picked)} compilable episode(s) in the library — a compilation needs "
                "at least 2 (masters are retained from publishes made after this feature shipped)")
        paths = [compilation.episode_master_path(campaign.id, t.episode_number)
                 for t in picked]
        durations = [media.probe_duration(p) for p in paths]

        output_dir = os.path.join(settings.MEDIA_ROOT, "buffer", str(campaign.id))
        os.makedirs(output_dir, exist_ok=True)
        master = os.path.join(output_dir, f"compilation_{task.episode_number}.mp4")
        list_file = master + ".txt"
        compilation.build_concat_list(paths, list_file)
        # Stream copy — the segments share the pipeline's codec parameters by construction.
        run_ffmpeg(build_concat_args(list_file, master, music_path=None, loudnorm=False))
        os.remove(list_file)
        _set_status(db, task, TaskStatus.AUDIO_SYNCED, 70)

        metadata = compilation.compilation_metadata(campaign, picked, durations)
        thumb = os.path.join(output_dir, f"compilation_{task.episode_number}.jpg")
        generate_thumbnail(master, thumb, teaser(metadata["title"]),
                           duration=sum(durations), poster=True)
        cfg = campaign.config_json or {}
        metadata.setdefault("cta", cfg.get("cta"))
        metadata.setdefault("privacy", cfg.get("privacy", "public"))
        # Free sanity check on the concat (ADR-087): a truncated library master produces a broken
        # long-form video, and until now NOTHING looked at a compilation before it parked. Score
        # stays None on purpose — the review autopilot escalates score-less verdicts, so a
        # compilation is never auto-rejected (its complaints must not steer the scriptwriter) and
        # never auto-approved; the human look stays mandatory, now with a verdict line on the card.
        det = qc.run_deterministic_qc(master)
        metadata["qc"] = {"passed": det.passed, "score": None, "issues": det.issues,
                          "deterministic_only": True}

        buf = BufferPoolItem(
            campaign_id=campaign.id, channel_id=campaign.channel_id,
            episode_number=task.episode_number, video_path=master, thumbnail_path=thumb,
            metadata_json=metadata, status=BufferStatus.awaiting_review)
        db.add(buf)
        task.render_json = {**(task.render_json or {}),
                            "duration": sum(durations),
                            "compiled_from": [t.episode_number for t in picked]}
        task.synopsis = metadata["title"][:300]
        task.finished_at = datetime.utcnow()
        _set_status(db, task, TaskStatus.AWAITING_REVIEW, 90)
        db.commit()
        _notify(user, f"🎬 Best-of compilation for '{campaign.topic_name}' is built "
                      f"({len(picked)} episodes, {round(sum(durations) / 60)} min) and waiting "
                      "for your review in the Asset Pool.")
    except Exception as exc:  # noqa: BLE001 — record and continue the queue
        if campaign is not None and user is not None:
            _fail_task(db, task, user, campaign, exc, "compile_task")
        else:
            db.rollback()
            task.status = TaskStatus.FAILED
            task.finished_at = datetime.utcnow()
            task.error_message = f"compile_task failed: {exc}"
            db.commit()
            logger.exception("compile_task failed for task %s", task_id)
    finally:
        clear_progress(task_id)
        db.close()


def _job_is_stale(task: Task) -> bool:
    """Is the currently-executing RQ job an orphan for this task? The task row's `rq_job_id` always
    points at the most recently enqueued (authoritative) job; a reaped-then-retried episode can
    leave an older job in the queue, and letting it run re-rendered (and in auto mode re-published)
    an episode that had already finished (R22). Fail-open: outside a job context, run."""
    try:
        from rq import get_current_job

        job = get_current_job()
    except Exception:  # noqa: BLE001 — direct calls (tests) have no job context
        return False
    return job is not None and bool(task.rq_job_id) and job.id != task.rq_job_id


@with_render_lock
def render_task(task_id: int) -> None:
    """Render one episode into the buffer pool; auto-publish or park for review per campaign."""
    db = SessionLocal()
    task = db.get(Task, task_id)
    if task is None:
        logger.error("render_task: no Task %s", task_id)
        db.close()
        return
    # Idempotency (R22), mirroring publish_task's guard: a duplicate/orphaned job must not re-render
    # an episode that already finished — that deleted the live buffer row, published a second copy
    # in auto mode, and advanced the campaign twice.
    if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED) or _job_is_stale(task):
        logger.info("render_task: task %s already handled (status=%s) or job superseded — skipping",
                    task_id, task.status)
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

        # Content style (ADR-056): "quote" = aesthetic poem-per-video with a per-episode Vibe roll and
        # drawn visuals (so it forces Studio). "story" = the normal narrated video.
        content_style = "quote" if cfg.get("content_style") == "quote" else "story"
        # Vibe Engine: roll this episode's mood/subject/setting/music/pace, seeded per (campaign,
        # episode) so a re-render is identical but every video differs.
        vibe = (vibe_mod.roll(campaign.id * 1000 + task.episode_number)
                if content_style == "quote" else None)

        # Visual source (ADR-052): stock footage (Pexels) or Studio Mode (AI-drawn). Studio draws with
        # the Gemini image model, so it needs no Pexels key. Quote videos are always drawn.
        visual_source = "studio" if (cfg.get("visual_source") == "studio"
                                     or content_style == "quote") else "stock"
        gemini_key, pexels_key = _resolve_keys(user, visual_source=visual_source)
        # Model chain: the user's Credentials choice wins; .env GEMINI_MODEL is the server default.
        gemini_model = user.gemini_model or settings.GEMINI_MODEL
        # Image model is managed separately from the text model (own field, own provider chain).
        image_model = user.gemini_image_model or settings.GEMINI_IMAGE_MODEL
        # Studio image generator: bind the Pollinations token + output geometry once (ADR-053), so a
        # `pollinations:flux` chain entry can draw for free when Gemini is unavailable — or as primary.
        _prof = video_factory.resolve_profile(cfg.get("video_format", "short"))
        image_gen = partial(generate_image,
                            pollinations_token=(user.pollinations_token or settings.POLLINATIONS_TOKEN),
                            width=_prof.width, height=_prof.height)
        # Per-attempt image-vendor wait (ADR-069): the operator's Settings choice wins over the
        # server default. A retry doubles it; the per-episode total is capped inside produce().
        try:
            image_timeout_s = int((user.settings_json or {}).get("image_timeout_s")
                                  or settings.IMAGE_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            image_timeout_s = settings.IMAGE_TIMEOUT_SECONDS
        # Studio cast: attach each character's PUBLIC reference URL (ADR-055) so a Pollinations
        # image-editing model (kontext) can fetch an uploaded reference over the internet. The Gemini
        # leg uses the local file instead; a non-public base (dev localhost) yields no url → skipped.
        studio_characters = _cast_with_ref_urls(channel.characters_json)
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
        # Voice pace: quote mode adds the vibe's small per-episode jitter to the campaign rate.
        rate_pct = int(cfg.get("rate_pct", 0)) + (vibe["rate_delta"] if vibe else 0)
        # Resume (ADR-069): an interrupted attempt persisted its script below — reuse it, so a Retry
        # makes the SAME episode (its checkpointed stills still match their prompt hashes) and costs
        # no second script call. A reject clears this (apply_reject/rerender): judged-bad content
        # must regenerate with the new avoid-note, not be faithfully rebuilt.
        script = None
        saved_script = (task.render_json or {}).get("script")
        if saved_script:
            try:
                script = VideoScript.model_validate(saved_script)
                logger.info("Task %s: resuming with the script from the interrupted attempt", task.id)
            except Exception:  # noqa: BLE001 — an unreadable checkpoint just regenerates
                logger.warning("Task %s: persisted script did not validate — regenerating", task.id)
        slop_warnings: list[str] = []
        # The decision journey (ADR-084): every judgment made about this episode, in order, in
        # plain words — persisted on the Task so the episode page can answer "why is it in this
        # state" without the operator reading worker logs.
        journey: list[dict] = []
        if script is not None:
            journey.append({"step": "Script", "note": "resumed from the interrupted attempt's checkpoint"})
        if script is None:
            # The pre-render quality gate (ADR-079). A blocked script regenerates ONCE with the
            # gate's issues as explicit avoid-notes; a second block fails the task honestly — a
            # script the gate rejects twice needs the operator (or a different topic), not a render.
            recent = _recent_fingerprints(db, campaign, task.episode_number)
            cliches = slop_gate.merged_cliches((user.settings_json or {}).get("slop_blacklist"))
            avoid_notes = list(learning.get("reject_reasons") or []) + \
                list((learning.get("flop_notes") or []))
            gate = None
            for _gen_attempt in (1, 2):
                script = generate_script(
                    topic=campaign.topic_name,
                    language=cfg.get("language", "en"),
                    total_episodes=campaign.total_episodes,
                    episode=task.episode_number,
                    api_key=gemini_key,
                    content_style=content_style,
                    vibe=vibe,
                    custom_system_prompt=cfg.get("system_prompt"),
                    persona=cfg.get("persona"),
                    style_examples=cfg.get("style_examples"),
                    # Per-campaign on/off: the text stays saved, but only applied when its flag is on
                    # (default on for pre-flag campaigns — unchanged behaviour).
                    catchphrase_open=(cfg.get("catchphrase_open") if cfg.get("catchphrase_open_on", True) else None),
                    catchphrase_close=(cfg.get("catchphrase_close") if cfg.get("catchphrase_close_on", True) else None),
                    continuity=cfg.get("continuity", "none"),
                    previous_synopses=previous,
                    playbook=learning.get("playbook"),
                    best_examples=learning.get("best_examples"),
                    avoid=avoid_notes or None,
                    self_critique=cfg.get("self_critique", "on") != "off",
                    duration_min_s=cfg.get("duration_min_s"),
                    duration_max_s=cfg.get("duration_max_s"),
                    rate_pct=rate_pct,
                    script_depth=cfg.get("script_depth", "standard"),
                    video_format=cfg.get("video_format", "short"),
                    model=gemini_model,
                )
                # Heartbeat between AI steps (R22): the whole script phase used to sit at a frozen
                # 5%, so a merely-slow provider read as a wedged worker to the stall watchdog. The
                # value must actually move — set_progress only re-stamps on change.
                set_progress(task_id, 5 + _gen_attempt)
                fp = slop_gate.script_fingerprint(script)
                gate = slop_gate.check_script(fp["narration"], fp["title"], recent=recent,
                                              cliches=cliches, content_style=content_style)
                if not gate.blocked:
                    journey.append({"step": "Script gate",
                                    "note": ("warnings: " + "; ".join(gate.issues))
                                    if gate.issues else "clean"})
                    # C2 (ADR-079): the AI judge shares the ONE regenerate budget with the
                    # deterministic gate and the channel's reject threshold with the vision QC —
                    # one scale, one discipline. Fail-open: a judge outage is "no verdict", never
                    # a stalled factory (the deterministic gate has already run).
                    verdict = _judge_script_safe(user, channel, cfg, fp, gemini_key, gemini_model)
                    set_progress(task_id, 5 + _gen_attempt + 0.5)  # judge done — still alive
                    if verdict is None:
                        journey.append({"step": "Script judge", "note": "no verdict (off or unavailable)"})
                        break
                    from core import autopilot as _ap

                    _approve_min, reject_max = _ap.review_thresholds(channel)
                    if verdict.score > reject_max:
                        journey.append({"step": "Script judge", "note": f"{verdict.score}/10"
                                        + ("; " + "; ".join(verdict.issues) if verdict.issues else "")})
                        break
                    gate = slop_gate.GateReport(
                        "block", [f"script judge scored it {verdict.score}/10"] + verdict.issues)
                journey.append({"step": "Script gate", "note": "BLOCKED — regenerating once: "
                                + "; ".join(gate.issues)})
                logger.info("Task %s: script blocked by the quality gate (%s) — regenerating once",
                            task.id, "; ".join(gate.issues))
                avoid_notes = avoid_notes + [f"your previous draft was rejected: {i}"
                                             for i in gate.issues]
            if gate is not None and gate.blocked:
                # Deliberately NOT persisted as a checkpoint: a Retry must write a fresh script,
                # not faithfully resume the one the gate just refused twice.
                raise RuntimeError("Script failed the quality gate twice: "
                                   + "; ".join(gate.issues))
            slop_warnings = gate.issues if gate is not None else []
            # Persist the script the moment it exists (ADR-069): if the render dies mid-way, the
            # retry rebuilds THIS episode instead of paying for a new script — and only a matching
            # script lets the checkpointed stills be reused. The success path overwrites render_json
            # wholesale, which is what consumes the checkpoint.
            task.render_json = {**(task.render_json or {}),
                                "script": script.model_dump(mode="json")}
        # Episode memory must NEVER be empty after a successful generation — an episode without a
        # synopsis is invisible to every later episode's no-repeat/serial prompt (continuity
        # silently degrades). The schema requires a synopsis; the variant-A title is the fallback.
        task.synopsis = (script.synopsis or script.metadata_variations[0].title)[:300]
        db.commit()

        _set_status(db, task, TaskStatus.RENDERING, 10)
        output_dir = os.path.join(settings.MEDIA_ROOT, "buffer", str(campaign.id))
        # Quote mode with Auto music: use the vibe's rolled mood for the CC0 search.
        music_cfg = ({**cfg, "music_mood": vibe["music_mood"]}
                     if (vibe and cfg.get("music_mode") == "auto") else cfg)
        music_path, music_credit = _resolve_music(music_cfg)
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
                rate_pct=rate_pct,
                voice_delivery=cfg.get("voice_delivery", "normal"),
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
                video_format=cfg.get("video_format", "short"),
                visual_source=visual_source,
                characters=studio_characters,
                visual_style=cfg.get("visual_style"),
                image_api_key=gemini_key,
                image_model=image_model,
                studio_sheet_dir=os.path.join(settings.MEDIA_ROOT, "studio", "sheets", str(channel.id)),
                gen_image=image_gen,
                image_timeout_s=image_timeout_s,
                # Attempt 2 asks the free image provider for DIFFERENT draws (ADR-070): its seed is
                # derived from the prompt, so re-rendering reproduced the QC-rejected video
                # pixel-for-pixel and re-judged it — a whole episode of image calls for the same
                # verdict. Attempt 1 stays salt-free so a resume reuses its checkpointed stills.
                image_seed_salt=attempt - 1,
                # Raw config value — produce() normalizes every historical shape (bool / "on" /
                # off|thumb|flash) in ONE place, so stored legacy campaigns need no migration.
                title_overlay=cfg.get("title_overlay", "off"),
                content_style=content_style,
                signature=cfg.get("signature"),
                vet_batch=vet_batch,
                on_progress=lambda p: set_progress(task_id, 10 + p * 0.8),
            )
            if not auto_qc:
                break
            # Free deterministic checks (black/silence) run alongside the vision judge — they catch
            # catastrophic breakage even when the vision API fails open.
            det = qc.run_deterministic_qc(result.master_path)
            verdict = qc.run_final_qc(
                result.master_path, api_key=gemini_key, model=gemini_model,
                context=f"The narration language is '{cfg.get('language', 'en')}'.",
            )
            passed = det.passed and verdict.passed
            issues = det.issues + verdict.issues
            qc_report = {"passed": passed, "score": verdict.score, "issues": issues,
                         "attempts": attempt}
            journey.append({"step": f"Auto-QC (attempt {attempt})",
                            "note": ("could not run — " + (verdict.unavailable_reason or "API error"))
                            if verdict.unavailable else
                            (f"{'passed' if passed else 'FAILED'}"
                             + (f" {verdict.score}/10" if verdict.score is not None else "")
                             + ("; " + "; ".join(issues) if issues and not passed else ""))})
            if verdict.unavailable:
                # No verdict is not a verdict (ADR-084). Never burn the one re-render on an ABSENT
                # judge — the video was not judged bad — and record why it could not run. Routing
                # happens below: after a real fail this always parks; on a first attempt it parks
                # unless the campaign explicitly chose the old fail-open (`qc_failopen: publish`).
                qc_report.update(unavailable=True,
                                 unavailable_reason=verdict.unavailable_reason,
                                 prior_fail=attempt > 1)
                break
            if passed:
                break
            if attempt == 1:
                logger.info("Auto-QC rejected episode %s (score %s, issues %s) — re-rendering once",
                            task.episode_number, verdict.score, issues)
                _safe_remove(result.master_path, result.thumbnail_path)
        qc_failed = qc_report is not None and not qc_report["passed"]
        # An unavailable judge parks for review: always after a prior fail (the judge already
        # disliked this episode once — publishing because the judge went ABSENT is the one
        # indefensible path), and by default even on a first attempt; `qc_failopen: publish`
        # restores the old behaviour for operators who prefer availability over the check.
        qc_no_verdict_park = bool(
            qc_report and qc_report.get("unavailable")
            and (qc_report.get("prior_fail") or cfg.get("qc_failopen", "review") != "publish"))
        _record_clip_usage(db, channel.id, result.used_clip_ids)  # so future episodes vary footage
        # Persist the scene map + duration on the Task (it outlives the buffer item) so the retention
        # curve fetched days later can be attributed to the scene that lost viewers. `render_seconds`
        # is the true render wall-time — the Task Logs TIME column reads it instead of
        # finished_at−started_at, which for slot-scheduled episodes wrongly counts the wait-for-slot.
        render_seconds = (int((datetime.utcnow() - task.started_at).total_seconds())
                          if task.started_at else None)
        # This write deliberately CONSUMES the resume checkpoint (the "script" key). The gate
        # fingerprint survives it: future episodes compare their narration/title against these
        # (ADR-079) — without it, every completed episode is invisible to the slop gate.
        task.render_json = {"scenes": result.scene_map, "duration": result.duration,
                            "render_seconds": render_seconds, "journey": journey[:12],
                            **slop_gate.script_fingerprint(script)}
        _set_status(db, task, TaskStatus.AUDIO_SYNCED, 88)

        # Carry distribution settings into the stored metadata so the publish step (now or after
        # review) has everything it needs.
        if slop_warnings:
            # Warn-level gate findings ride into review (ADR-079): the render went ahead, but the
            # reviewer — human or autopilot hint — sees what the gate noticed, on the card.
            result.metadata["slop_warnings"] = slop_warnings[:6]
        result.metadata.setdefault("cta", cfg.get("cta"))
        result.metadata.setdefault("privacy", cfg.get("privacy", "public"))
        # Carry the language so the upload can declare it (defaultAudioLanguage / defaultLanguage) —
        # the strongest signal telling the platform which country/audience this video is for (ADR-045).
        result.metadata.setdefault("language", cfg.get("language", "en"))
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
        # So does an ABSENT judge (ADR-084): "the check could not run" used to route exactly like
        # "the check approved", which meant a video the judge failed once could publish unseen
        # because the judge was unavailable for the re-check.
        parked_for_review = not auto_publish or qc_failed or qc_no_verdict_park
        now = datetime.utcnow()
        buf = BufferPoolItem(
            campaign_id=campaign.id,
            channel_id=channel.id,
            episode_number=task.episode_number,
            video_path=result.master_path,
            thumbnail_path=result.thumbnail_path,
            metadata_json=result.metadata,
            status=BufferStatus.awaiting_review if parked_for_review else BufferStatus.ready,
            ready_at=None if parked_for_review else now,
        )
        db.add(buf)
        # The task's terminal state rides the SAME commit as the buffer row (R22). Committing the
        # buffer first left a window — QC verdict written, video on disk, status still working —
        # where a crash/watchdog kill produced the incident screen: a FAILED banner over a
        # finished, QC-passed video with an Approve button.
        if parked_for_review:
            task.finished_at = now
            task.status = TaskStatus.AWAITING_REVIEW
            task.progress_pct = 90
        elif slot_scheduled:
            # Pre-rendered and parked; the scheduler publishes it at the next posting slot.
            task.finished_at = now
            task.status = TaskStatus.SCHEDULED
            task.progress_pct = 90
        db.commit()
        db.refresh(buf)
        if parked_for_review or slot_scheduled:
            set_progress(task.id, 90)

        if qc_failed:
            issues = "; ".join((qc_report or {}).get("issues") or []) or "low quality score"
            _notify(user, f"🔍 Episode {task.episode_number} of '{campaign.topic_name}' failed "
                          f"Auto-QC twice ({issues}). It is parked in the Asset Pool for your review.")
        elif qc_no_verdict_park:
            _notify(user, f"⚪ Episode {task.episode_number} of '{campaign.topic_name}' rendered, "
                          f"but Auto-QC could not run ({qc_report.get('unavailable_reason')}). "
                          "It is parked in the Asset Pool — review it, or hit “Run QC now”.")
        elif not auto_publish:
            _notify(user, f"🎬 Episode {task.episode_number} of '{campaign.topic_name}' is rendered "
                          "and waiting for your review in the Asset Pool.")
        elif not slot_scheduled:
            try:
                _publish_buffer(db, task, buf, campaign, channel, user)
            except ChannelExpired as exc:
                # The render is DONE and committed as `ready` above — a dead channel credential
                # parks the episode, it does not fail it (ADR-073). Before R22 this fell through
                # to _fail_task, read as a worker stall, and burned autopilot retries re-uploading
                # against the same dead token.
                logger.warning("render_task: %s", exc)
                db.rollback()
                task.finished_at = datetime.utcnow()
                _set_status(db, task, TaskStatus.SCHEDULED, 90)
                _notify_channel_expired(channel, user, exc)

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
    # Only `ready` items are publishable (R22): approval — apply_approve flipping the row to ready —
    # is the ONE gate onto the platform. Accepting awaiting_review here let recovery/retry paths
    # upload videos that failed QC or that a review-first campaign had parked for a human; anything
    # else was already handled, so bail rather than upload twice or resurrect a consumed row.
    if buf.status != BufferStatus.ready:
        logger.info("publish_task: buffer %s not publishable (status=%s) — skipping",
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
    except ChannelExpired as exc:
        # Not a failure of this episode: the channel's credential is what is broken. Leaving the task
        # untouched keeps the rendered video in the buffer, so it publishes at the next slot once the
        # token is replaced — instead of burning a retry and reading as a broken render (ADR-073).
        # Said out loud (R22): a silent return here left approved episodes reading "Scheduled" with
        # no visible reason until the expiry sweep destroyed them.
        logger.warning("publish_task: %s", exc)
        db.rollback()
        _notify_channel_expired(channel, user, exc)
    except Exception as exc:  # noqa: BLE001
        _fail_task(db, task, user, campaign, exc, "publish_task")  # rolls back first
        # The buffer stays `ready` (R22): it was approved, and a failed upload does not un-approve
        # it. Parking it back to awaiting_review erased the approval, re-listed the episode as
        # "waiting for review", and invited the approve→fail→approve loop of the incident; `ready`
        # keeps every honest retry path (Retry button, autopilot, next slot) able to re-upload.
    finally:
        clear_progress(task.id)
        db.close()
