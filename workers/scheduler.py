"""Periodic automation tick — buffer hydration gated by posting slots, plus housekeeping.

Runs as a daemon thread inside the worker process (KISS: no extra container). The tick only enqueues
jobs and sweeps files — it never renders — so the single-render guarantee is untouched (the one
worker still consumes the queue one job at a time).

Responsibilities each tick:
  * sweep orphaned temp media (crash survivors) and relieve disk pressure,
  * expire stale pre-rendered buffer items (and delete their files),
  * for each active campaign whose posting slot is current, top up the render buffer.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from datetime import datetime

from sqlalchemy import select

from core.cleanup import sweep_orphans
from core.config import settings
from database.db_session import SessionLocal
from database.models import BufferPoolItem, Campaign
from database.types import BufferStatus, CampaignStatus
from workers import video_worker

logger = logging.getLogger(__name__)


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


def periodic_tick(db=None, now: datetime | None = None) -> dict:
    """One automation cycle. Returns a small summary dict (handy for tests/logging)."""
    now = now or datetime.utcnow()
    own_session = db is None
    db = db or SessionLocal()
    summary = {"swept": 0, "expired": 0, "hydrated": []}
    try:
        # Disk hygiene.
        summary["swept"] = sweep_orphans()
        if disk_usage_pct(settings.MEDIA_ROOT) >= settings.DISK_PRESSURE_PCT:
            logger.warning("Disk pressure high on %s — sweeping aggressively", settings.MEDIA_ROOT)
            summary["swept"] += sweep_orphans(max_age_minutes=5)
        summary["expired"] = expire_stale_buffers(db, now=now)

        # Slot-gated buffer hydration.
        campaigns = db.scalars(select(Campaign).where(Campaign.status == CampaignStatus.active)).all()
        for campaign in campaigns:
            slots = (campaign.config_json or {}).get("posting_slots") or []
            if is_within_slot(slots, now):
                summary["hydrated"] += video_worker.hydrate_campaign(db, campaign)
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
