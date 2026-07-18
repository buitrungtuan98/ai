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


def periodic_tick(db=None, now: datetime | None = None) -> dict:
    """One automation cycle. `now` (local time) drives the posting-slot check; buffer expiry uses
    UTC internally to match DB timestamps. Returns a small summary dict."""
    own_session = db is None
    db = db or SessionLocal()
    summary = {"swept": 0, "expired": 0, "hydrated": [], "published": []}
    try:
        # Disk hygiene.
        summary["swept"] = sweep_orphans()
        if disk_usage_pct(settings.MEDIA_ROOT) >= settings.DISK_PRESSURE_PCT:
            logger.warning("Disk pressure high on %s — sweeping aggressively", settings.MEDIA_ROOT)
            summary["swept"] += sweep_orphans(max_age_minutes=5)
        summary["expired"] = expire_stale_buffers(db)

        campaigns = db.scalars(select(Campaign).where(Campaign.status == CampaignStatus.active)).all()
        for campaign in campaigns:
            # Render eagerly — a full buffer is what makes on-the-dot slot publishing possible.
            summary["hydrated"] += video_worker.hydrate_campaign(db, campaign)
            # Publish exactly one pre-rendered episode if this campaign's slot is now.
            published = publish_due_campaign(db, campaign, now=now)
            if published is not None:
                summary["published"].append(published)
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
