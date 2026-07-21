"""Periodic automation tick — eager rendering, slot-timed publishing, and housekeeping.

Runs as a daemon thread inside the worker process (KISS: no extra container). The tick only enqueues
jobs and sweeps files — it never renders — so the single-render guarantee is untouched.

Cadence model (ADR-011): rendering runs EAGERLY (keep every active campaign's buffer full), while
publishing is what posting slots control — exactly ONE pre-rendered episode is published per slot,
in the campaign's timezone. Campaigns without slots publish immediately after render (continuous
mode); review-mode campaigns publish only on operator approval.

Responsibilities each tick:
  * sweep orphaned temp media (crash survivors) and relieve disk pressure,
  * expire stale pre-rendered buffer items (and delete their files),
  * top up every active campaign's render buffer,
  * publish one `ready` buffer item per campaign whose posting slot is current.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from core.cleanup import sweep_orphans
from core.config import settings
from database.db_session import SessionLocal
from database.models import BufferPoolItem, Campaign, Task
from database.types import BufferStatus, CampaignStatus, TaskStatus
from workers import task_queue, video_worker

logger = logging.getLogger(__name__)


def local_now(timezone: str | None = None) -> datetime:
    """Now in the given (or globally configured) timezone — posting slots are interpreted in it,
    so a user in Asia/Ho_Chi_Minh who types 09:00 gets a 09:00 local post, not 09:00 UTC."""
    tz = timezone or settings.TIMEZONE
    try:
        return datetime.now(ZoneInfo(tz))
    except Exception:  # noqa: BLE001 — a bad timezone value must not kill the scheduler
        logger.warning("Invalid timezone %r — falling back to UTC", tz)
        return datetime.utcnow()


def is_within_slot(slots: list[str], now: datetime, tolerance_min: int | None = None) -> bool:
    """True if `now` is within `tolerance_min` of any "HH:MM" slot. Empty slots = always allowed."""
    if not slots:
        return True
    tolerance_min = settings.SLOT_TOLERANCE_MINUTES if tolerance_min is None else tolerance_min
    now_min = now.hour * 60 + now.minute
    for slot in slots:
        try:
            hh, mm = slot.split(":")
            slot_min = int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            continue
        # Compare on a 24h circle so slots near midnight still match.
        diff = abs(now_min - slot_min)
        diff = min(diff, 1440 - diff)
        if diff <= tolerance_min:
            return True
    return False


def expire_stale_buffers(db, *, max_age_hours: int | None = None, now: datetime | None = None) -> int:
    """Mark `ready` buffer items older than the cutoff as expired and delete their files."""
    now = now or datetime.utcnow()
    max_age_hours = settings.BUFFER_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    cutoff = now.timestamp() - max_age_hours * 3600
    items = db.scalars(select(BufferPoolItem).where(BufferPoolItem.status == BufferStatus.ready)).all()
    expired = 0
    for item in items:
        created = item.created_at.timestamp() if item.created_at else now.timestamp()
        if created < cutoff:
            for p in (item.video_path, item.thumbnail_path):
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            item.status = BufferStatus.expired
            expired += 1
            # A slot-scheduled task points at this now-deleted buffer; without this it would sit in
            # SCHEDULED forever (no reaper/retry reaches it). Fail it so Retry can re-render.
            task = db.scalar(select(Task).where(
                Task.campaign_id == item.campaign_id,
                Task.episode_number == item.episode_number,
                Task.status == TaskStatus.SCHEDULED,
            ))
            if task is not None:
                task.status = TaskStatus.FAILED
                task.finished_at = now
                task.error_message = (
                    f"Pre-rendered episode expired before its posting slot (buffer older than "
                    f"{max_age_hours}h). Use Retry to re-render.")
    if expired:
        db.commit()
        logger.info("Expired %d stale buffer item(s)", expired)
    return expired


def disk_usage_pct(path: str) -> float:
    try:
        usage = shutil.disk_usage(path)
        return usage.used / usage.total * 100.0
    except OSError:
        return 0.0


def _recently_published(db, campaign_id: int, window_minutes: int) -> bool:
    """True if this campaign already published within the window — the one-per-slot guard, so an
    hourly tick landing twice inside one slot's tolerance can't double-post."""
    cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)
    latest = db.scalar(
        select(Task.finished_at)
        .where(Task.campaign_id == campaign_id, Task.status == TaskStatus.COMPLETED)
        .order_by(Task.finished_at.desc())
        .limit(1)
    )
    return latest is not None and latest >= cutoff


def publish_due_campaign(db, campaign: Campaign, now: datetime | None = None,
                         enqueue=None) -> int | None:
    """Publish exactly ONE ready buffer item if the campaign's posting slot is current (in the
    campaign's own timezone). Returns the buffer id queued, or None."""
    cfg = campaign.config_json or {}
    slots = cfg.get("posting_slots") or []
    if not slots or not cfg.get("auto_publish", True):
        return None  # continuous mode publishes at render time; review mode publishes on approval
    now = now or local_now(cfg.get("timezone"))
    if not is_within_slot(slots, now):
        return None
    if _recently_published(db, campaign.id, settings.SLOT_TOLERANCE_MINUTES):
        return None
    buf = db.scalar(
        select(BufferPoolItem)
        .where(BufferPoolItem.campaign_id == campaign.id,
               BufferPoolItem.status == BufferStatus.ready)
        .order_by(BufferPoolItem.episode_number)
        .limit(1)
    )
    if buf is None:
        return None
    (enqueue or task_queue.enqueue_publish)(buf.id)
    logger.info("Slot publish: campaign %s episode %s queued", campaign.id, buf.episode_number)
    return buf.id


DISTILL_MIN_EPISODES = 5     # need this many measured episodes before learning anything
DISTILL_EVERY_DAYS = 7       # refresh the playbook at most weekly

# A hard-killed worker (OOM, power loss) can leave a task frozen in a working state with no job
# behind it. Anything untouched for 2× the job timeout is definitively dead — fail it so the
# operator sees it and the Retry button works.
_STUCK_STATUSES = [TaskStatus.AI_GENERATION, TaskStatus.AUDIO_SYNCED,
                   TaskStatus.RENDERING, TaskStatus.PUBLISHING]


def reap_stuck_tasks(db, now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=settings.JOB_TIMEOUT_SECONDS * 2)
    stuck = list(db.scalars(
        select(Task).where(Task.status.in_(_STUCK_STATUSES), Task.updated_at <= cutoff)
    ).all())
    # PENDING_QUEUE tasks can strand if their job was dead-lettered (e.g. a stale lock at restart)
    # or an enqueue raised after the row was committed — no reaper/retry reaches them and hydration
    # counts them as active, freezing the campaign. Anything queued far longer than any real
    # backlog (3× the job timeout) is definitively stuck. A larger cutoff avoids failing a task
    # that is legitimately waiting behind a deep buffer.
    pending_cutoff = now - timedelta(seconds=settings.JOB_TIMEOUT_SECONDS * 3)
    stuck += list(db.scalars(
        select(Task).where(Task.status == TaskStatus.PENDING_QUEUE, Task.updated_at <= pending_cutoff)
    ).all())
    for task in stuck:
        task.status = TaskStatus.FAILED
        task.finished_at = now
        task.error_message = ("Worker crashed, timed out, or the job was lost (no progress for a "
                              "long time). Use Retry.")
    if stuck:
        db.commit()
        logger.warning("Reaped %d stuck task(s)", len(stuck))
    return len(stuck)


def maybe_distill_campaign(db, campaign: Campaign, now: datetime | None = None) -> bool:
    """Update the campaign's playbook from real performance data — bounded, guarded, best-effort."""
    from core.ai_engine import distill_playbook
    from database.models import Task, User

    now = now or datetime.utcnow()
    learning = dict(campaign.learning_json or {})
    last = learning.get("distilled_at")
    if last and datetime.fromisoformat(last) > now - timedelta(days=DISTILL_EVERY_DAYS):
        return False
    rows = db.scalars(
        select(Task).where(Task.campaign_id == campaign.id, Task.stats_json.isnot(None))
        .order_by(Task.episode_number)
    ).all()
    if len(rows) < DISTILL_MIN_EPISODES:
        return False
    user = db.get(User, campaign.user_id)
    api_key = (user.gemini_api_key if user else None) or settings.GEMINI_API_KEY
    if not api_key:
        return False
    summary_lines = [
        f"Ep {t.episode_number}: '{t.synopsis or '?'}' — "
        f"retention {t.stats_json.get('avg_pct_viewed', '?')}%, views {t.stats_json.get('views', '?')}, "
        f"likes {t.stats_json.get('likes', '?')}"
        for t in rows
    ]
    try:
        update = distill_playbook(
            api_key=api_key,
            performance_summary="\n".join(summary_lines),
            current_playbook=learning.get("playbook"),
            reject_reasons=learning.get("reject_reasons"),
        )
    except Exception:  # noqa: BLE001 — learning must never break the factory
        logger.warning("Playbook distillation failed for campaign %s", campaign.id, exc_info=True)
        return False
    learning["playbook"] = update.playbook[:15]
    learning["best_examples"] = update.best_examples[:3]
    learning["distilled_at"] = now.isoformat()
    campaign.learning_json = learning
    db.commit()
    logger.info("Campaign %s playbook updated (%d lessons)", campaign.id, len(update.playbook))
    return True


def check_daily_minimums(db, now: datetime | None = None) -> int:
    """Min-per-day watchdog: a config `min_per_day` can't FORCE publishes (failures happen), but
    the operator must never find out by accident. Alert via Telegram when an active campaign
    published fewer episodes in the last 24h than its configured minimum. Returns alerts sent."""
    from sqlalchemy import func

    from database.models import User

    now = now or datetime.utcnow()
    cutoff = now - timedelta(hours=24)
    alerts = 0
    for campaign in db.scalars(
        select(Campaign).where(Campaign.status == CampaignStatus.active)
    ).all():
        min_per_day = (campaign.config_json or {}).get("min_per_day")
        if not min_per_day:
            continue
        published = db.scalar(
            select(func.count()).select_from(Task).where(
                Task.campaign_id == campaign.id,
                Task.status == TaskStatus.COMPLETED,
                Task.finished_at >= cutoff,
            )
        ) or 0
        if published < int(min_per_day):
            user = db.get(User, campaign.user_id)
            if user is not None:
                video_worker._notify(
                    user,
                    f"⚠️ Campaign '{campaign.topic_name}' published {published}/{min_per_day} "
                    "episodes in the last 24h (below its daily minimum). Check Task Logs for "
                    "failures or quota limits.",
                )
            alerts += 1
    return alerts


def send_daily_heartbeat(db, now: datetime | None = None) -> int:
    """One Telegram line per operator per day: what the factory did in the last 24h plus the
    quota/disk vitals — so "hands-off" means reading one message, not checking a dashboard.
    Sent only to users with at least one active campaign. Returns digests sent."""
    from sqlalchemy import func

    from core.usage import ai_calls_today
    from database.models import User

    now = now or datetime.utcnow()
    cutoff = now - timedelta(hours=24)
    user_ids = set(db.scalars(
        select(Campaign.user_id).where(Campaign.status == CampaignStatus.active)
    ).all())
    if not user_ids:
        return 0
    calls = ai_calls_today()
    budget = settings.GEMINI_DAILY_BUDGET
    quota_bit = f"{calls}/{budget}" if budget else str(calls)
    disk = disk_usage_pct(settings.MEDIA_ROOT)
    sent = 0
    for uid in user_ids:
        user = db.get(User, uid)
        if user is None:
            continue
        counts = dict(db.execute(
            select(Task.status, func.count()).where(
                Task.user_id == uid, Task.finished_at >= cutoff
            ).group_by(Task.status)
        ).all())
        awaiting = db.scalar(
            select(func.count()).select_from(Task).where(
                Task.user_id == uid, Task.status == TaskStatus.AWAITING_REVIEW)
        ) or 0
        video_worker._notify(
            user,
            f"📊 Factory heartbeat (24h): published {counts.get(TaskStatus.COMPLETED, 0)}, "
            f"failed {counts.get(TaskStatus.FAILED, 0)}, awaiting review {awaiting}. "
            f"AI calls today: {quota_bit}. Disk {disk:.0f}%.",
        )
        sent += 1
    return sent


def daily_learning_pass(db, now: datetime | None = None) -> dict:
    """Once-a-day: collect platform stats, re-distill playbooks, check daily minimums, and send
    the operator heartbeat digest."""
    from services.analytics_service import collect_stats

    result = {"stats_updated": 0, "distilled": 0, "min_alerts": 0, "heartbeats": 0}
    result["stats_updated"] = collect_stats(db, now=now)
    for campaign in db.scalars(select(Campaign)).all():
        if maybe_distill_campaign(db, campaign, now=now):
            result["distilled"] += 1
    result["min_alerts"] = check_daily_minimums(db, now=now)
    result["heartbeats"] = send_daily_heartbeat(db, now=now)
    return result


def periodic_tick(db=None, now: datetime | None = None) -> dict:
    """One automation cycle. `now` (local time) drives the posting-slot check; buffer expiry uses
    UTC internally to match DB timestamps. Returns a small summary dict."""
    own_session = db is None
    db = db or SessionLocal()
    summary = {"swept": 0, "expired": 0, "hydrated": [], "published": [], "learning": None, "reaped": 0}
    try:
        summary["reaped"] = reap_stuck_tasks(db)
        # Disk hygiene. Never sweep the workspace of a render in flight (its dir mtime goes stale
        # during a long single-scene encode), even under disk pressure.
        active = task_queue.active_render_task_ids()
        summary["swept"] = sweep_orphans(skip=active)
        if disk_usage_pct(settings.MEDIA_ROOT) >= settings.DISK_PRESSURE_PCT:
            logger.warning("Disk pressure high on %s — sweeping aggressively", settings.MEDIA_ROOT)
            summary["swept"] += sweep_orphans(max_age_minutes=5, skip=active)
        summary["expired"] = expire_stale_buffers(db)

        campaigns = db.scalars(select(Campaign).where(Campaign.status == CampaignStatus.active)).all()
        for campaign in campaigns:
            # Isolate each campaign — one campaign's fault must not starve the others' hydration or
            # cost them their posting slot this tick.
            try:
                # Render eagerly — a full buffer is what makes on-the-dot slot publishing possible.
                summary["hydrated"] += video_worker.hydrate_campaign(db, campaign)
                # Publish exactly one pre-rendered episode if this campaign's slot is now.
                published = publish_due_campaign(db, campaign, now=now)
                if published is not None:
                    summary["published"].append(published)
            except Exception:  # noqa: BLE001 — keep processing the remaining campaigns
                logger.warning("Tick failed for campaign %s", campaign.id, exc_info=True)
                db.rollback()

        # Self-improvement pass at most once per day (Redis NX guard across ticks/restarts).
        try:
            if task_queue.conn.set("learning:daily-pass", "1", nx=True, ex=86400):
                summary["learning"] = daily_learning_pass(db)
        except Exception:  # noqa: BLE001
            logger.warning("Daily learning pass failed", exc_info=True)
        return summary
    finally:
        if own_session:
            db.close()


def run_scheduler_thread(interval: int | None = None) -> threading.Thread:
    """Start the periodic tick in a daemon thread. Returns the thread (already started)."""
    interval = settings.SCHEDULER_INTERVAL_SECONDS if interval is None else interval

    def _loop() -> None:
        logger.info("Scheduler thread started (interval=%ss)", interval)
        while True:
            try:
                periodic_tick()
            except Exception:  # noqa: BLE001 — a tick failure must not kill the loop
                logger.exception("periodic_tick failed")
            time.sleep(interval)

    thread = threading.Thread(target=_loop, name="scheduler", daemon=True)
    thread.start()
    return thread
