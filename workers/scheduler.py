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

from sqlalchemy import func, select

from core import failure
# One dismissal-cooldown definition for every proposer (ADR-086) — the council shares it too.
from core.autopilot import REPROPOSE_AFTER_DAYS
from core.cleanup import sweep_orphans
from core.config import settings
from database.db_session import SessionLocal
from database.models import AutopilotAction, BufferPoolItem, Campaign, Channel, Task, User
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


# Locale-independent weekday keys (datetime.weekday(): Monday == 0).
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def is_posting_day(days: list[str], now: datetime) -> bool:
    """True if `now` (already in the campaign's timezone) falls on an allowed publish day.
    An empty list means every day (backwards compatible)."""
    if not days:
        return True
    return WEEKDAY_KEYS[now.weekday()] in days


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
    """Mark `ready` buffer items older than the cutoff as expired and delete their files.

    The cutoff knows the schedule (ADR-087), because "old" depends on when the item's turn IS:
      * weekday-gated campaigns (`posting_days`) get ≥ 7.5 days — a healthy pre-render can wait
        most of a week for its publish day;
      * slot-scheduled campaigns get a RUNWAY-aware age: the N-th item in line with S slots/day
        legitimately waits ~N/S days, so a buffer deeper than the daily slot count no longer
        cycles render → expire → re-render (a bounded but pure-waste loop the autopilot ran on
        its own retry budget);
      * an operator-scheduled item (`publish_at`, ADR-059) lives until its OWN time + a day —
        the flat cutoff used to destroy an episode deliberately parked five days out."""
    now = now or datetime.utcnow()
    max_age_hours = settings.BUFFER_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    campaigns = {c.id: c for c in db.scalars(select(Campaign)).all()}
    items = db.scalars(select(BufferPoolItem).where(BufferPoolItem.status == BufferStatus.ready)
                       .order_by(BufferPoolItem.campaign_id, BufferPoolItem.episode_number)).all()
    queue_pos: dict[int, int] = {}   # position of each campaign's next item in its publish queue
    expired = 0
    for item in items:
        cfg = {}
        campaign = campaigns.get(item.campaign_id)
        if campaign is not None:
            cfg = campaign.config_json or {}
        item_max_age = max(max_age_hours, 7 * 24 + 12) if cfg.get("posting_days") else max_age_hours
        slots = [s for s in (cfg.get("posting_slots") or []) if s]
        if item.publish_at is None:
            pos = queue_pos.get(item.campaign_id, 0)
            queue_pos[item.campaign_id] = pos + 1
            if slots:
                days_until_turn = pos // len(slots) + 1
                item_max_age = max(item_max_age, (days_until_turn + 1) * 24)  # +1 day of slack
        elif item.created_at is not None:
            own_wait_h = (item.publish_at - item.created_at).total_seconds() / 3600
            item_max_age = max(item_max_age, own_wait_h + 24)
        cutoff = now.timestamp() - item_max_age * 3600
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
                    f"{item_max_age}h). Use Retry to re-render.")
            # The machine threw finished work away — that belongs in the activity feed, not only
            # in a log file the operator never reads (ADR-087).
            channel = db.get(Channel, item.channel_id)
            if channel is not None:
                _log_action(db, channel, "expired",
                            f"Expired Ep {item.episode_number} of "
                            f"“{campaign.topic_name if campaign else '?'}” — it waited "
                            f"{int(item_max_age)}h for a posting slot and went stale; a retry "
                            "re-renders it nearer its turn.",
                            campaign_id=item.campaign_id,
                            evidence={"episode": item.episode_number,
                                      "max_age_hours": int(item_max_age)})
    if expired:
        db.commit()
        logger.info("Expired %d stale buffer item(s)", expired)
    return expired


def finish_stranded_campaign(db, campaign) -> bool:
    """Complete a campaign that can no longer finish on its own (ADR-087). Returns True if it did.

    A permanently-failed episode used to strand its campaign "active" at N-1/N forever:
    `current_episode` only advances on publish, hydration saw nothing left to create, and no code
    path ever closed the loop. Stranded = active, short of its total, every planned episode exists
    and is terminal, nothing is waiting in the buffer, and no failed episode is still within the
    autopilot's reach (transient/quota failures under the retry cap WILL be retried — those are
    not stranded, just slow). Completing is reversible the same way it always was: a manual Retry
    of the dead episode still works, and a later publish keeps the completed status consistent."""
    from core.compilation import COMPILATION_EPISODE_BASE

    if campaign.status != CampaignStatus.active:
        return False
    total = campaign.total_episodes or 0
    if not total or campaign.current_episode >= total:
        return False
    tasks = [t for t in db.scalars(select(Task).where(Task.campaign_id == campaign.id)).all()
             if t.episode_number < COMPILATION_EPISODE_BASE]
    by_ep = {t.episode_number: t for t in tasks}
    if any(ep not in by_ep for ep in range(1, total + 1)):
        return False   # episodes still to be created — hydration owns this campaign
    terminal = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
    if any(t.status not in terminal for t in tasks):
        return False   # something is still rendering/waiting — not stranded
    if db.scalar(select(func.count()).select_from(BufferPoolItem).where(
            BufferPoolItem.campaign_id == campaign.id,
            BufferPoolItem.status.in_([BufferStatus.ready, BufferStatus.awaiting_review]),
            BufferPoolItem.episode_number < COMPILATION_EPISODE_BASE)):
        return False   # a rendered episode is still on its way out
    dead = [t for t in tasks if t.status == TaskStatus.FAILED]
    for t in dead:
        msg = (t.error_message or "").lower()
        human_reject = "rejected in review" in msg and "auto-review" not in msg
        if (not human_reject and (t.auto_retry_count or 0) < AUTOPILOT_MAX_RETRIES
                and (failure.is_transient(msg) or failure.is_quota(msg))):
            return False   # the autopilot can still save this one — wait for it
    campaign.status = CampaignStatus.completed
    db.commit()
    user = db.get(User, campaign.user_id)
    blocked = ", ".join(f"Ep {t.episode_number}" for t in dead[:5]) or "—"
    channel = db.get(Channel, campaign.channel_id)
    if channel is not None:
        _log_action(db, channel, "report",
                    f"Completed “{campaign.topic_name}” at {campaign.current_episode}/{total} — "
                    f"{blocked} cannot be produced automatically (details on the episode page); "
                    "a manual Retry re-opens them.",
                    campaign_id=campaign.id,
                    evidence={"published": campaign.current_episode, "planned": total,
                              "blocked": [t.episode_number for t in dead[:5]]})
    if user is not None:
        video_worker._notify(
            user,
            f"🏁 Campaign '{campaign.topic_name}' completed at {campaign.current_episode}/{total} "
            f"episodes — {blocked} failed in a way no automatic retry can fix. Retry them from "
            "the episode page if you want the full run.")
    nxt = db.scalar(select(Campaign).where(
        Campaign.user_id == campaign.user_id,
        Campaign.status == CampaignStatus.pending).order_by(Campaign.id))
    if nxt is not None:   # the lifecycle promise: finishing one activates the next in line
        nxt.status = CampaignStatus.active
        db.commit()
    logger.warning("Campaign %s completed stranded at %s/%s", campaign.id,
                   campaign.current_episode, total)
    return True


def disk_usage_pct(path: str) -> float:
    try:
        usage = shutil.disk_usage(path)
        return usage.used / usage.total * 100.0
    except OSError:
        return 0.0


def _recently_published(db, campaign_id: int, window_minutes: int) -> bool:
    """True if this campaign already published an ORDINARY episode within the window — the
    one-per-slot guard, so an hourly tick landing twice inside one slot's tolerance can't
    double-post. Compilations are extra content outside the slot schedule (ADR-085): approving
    one must not eat the slot the next regular episode was waiting for."""
    from core.compilation import COMPILATION_EPISODE_BASE

    cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)
    latest = db.scalar(
        select(Task.finished_at)
        .where(Task.campaign_id == campaign_id, Task.status == TaskStatus.COMPLETED,
               Task.episode_number < COMPILATION_EPISODE_BASE)
        .order_by(Task.finished_at.desc())
        .limit(1)
    )
    return latest is not None and latest >= cutoff


def due_override_item(db, campaign: Campaign, now_utc: datetime | None = None):
    """A ready buffer item whose operator-set `publish_at` has arrived (ADR-059), earliest first.

    An override REPLACES the slot schedule for that one episode, so it deliberately skips the
    posting-day / slot-window / one-per-slot gates: the operator named an exact time and that time
    is now. It still respects `auto_publish` — a review-first campaign publishes on approval only."""
    if not (campaign.config_json or {}).get("auto_publish", True):
        return None
    now_utc = now_utc or datetime.utcnow()
    return db.scalar(
        select(BufferPoolItem)
        .where(BufferPoolItem.campaign_id == campaign.id,
               BufferPoolItem.status == BufferStatus.ready,
               BufferPoolItem.publish_at.isnot(None),
               BufferPoolItem.publish_at <= now_utc)
        .order_by(BufferPoolItem.publish_at)
        .limit(1)
    )


def publish_due_campaign(db, campaign: Campaign, now: datetime | None = None,
                         enqueue=None) -> int | None:
    """Publish exactly ONE buffer item if something is due: an operator-rescheduled episode whose
    time has come, else the next ready episode when the campaign's posting slot is current (in the
    campaign's own timezone). Returns the buffer id queued, or None."""
    cfg = campaign.config_json or {}
    enqueue = enqueue or task_queue.enqueue_publish
    # A per-episode override outranks the slot schedule and works even for a campaign with no slots
    # (the operator picked a time for this one episode; nothing else needs to be configured).
    override = due_override_item(db, campaign)
    if override is not None:
        enqueue(override.id)
        logger.info("Rescheduled publish: campaign %s episode %s queued",
                    campaign.id, override.episode_number)
        return override.id
    slots = cfg.get("posting_slots") or []
    if not slots or not cfg.get("auto_publish", True):
        return None  # continuous mode publishes at render time; review mode publishes on approval
    now = now or local_now(cfg.get("timezone"))
    if not is_posting_day(cfg.get("posting_days") or [], now):
        return None  # weekday-gated campaign: today is not a publish day
    if not is_within_slot(slots, now):
        return None
    if _recently_published(db, campaign.id, settings.SLOT_TOLERANCE_MINUTES):
        return None
    buf = db.scalar(
        select(BufferPoolItem)
        .where(BufferPoolItem.campaign_id == campaign.id,
               BufferPoolItem.status == BufferStatus.ready,
               # An episode moved to a future time must not be grabbed by the normal slot path —
               # that would undo the operator's reschedule.
               BufferPoolItem.publish_at.is_(None))
        .order_by(BufferPoolItem.episode_number)
        .limit(1)
    )
    if buf is None:
        return None
    enqueue(buf.id)
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
        task_queue.clear_progress(task.id)  # a crash skipped the finally — drop the ghost % (F1)
    if stuck:
        db.commit()
        logger.warning("Reaped %d stuck task(s)", len(stuck))
    return len(stuck)


_LOCK_SUSPECT_KEY = "render:lock-suspect"


def _render_appears_live(db) -> bool:
    """True while anything looks like a real render in flight — a task in a working status or a live
    Redis progress entry. THE guard for render-concurrency-1: a real render sets a working status
    within milliseconds of acquiring the lock, so this can never be false during one."""
    working = db.scalar(select(func.count()).select_from(Task)
                        .where(Task.status.in_(_STUCK_STATUSES))) or 0
    return bool(working or task_queue.active_render_task_ids())


def clear_orphaned_render_lock(db) -> bool:
    """Free a render lock left behind by a hard-crashed worker (its release `finally` was skipped),
    so the queue doesn't have to wait out the full lock TTL (~46 min) before anything renders again.

    SAFE for the render-concurrency-1 guarantee: the lock is cleared only after TWO consecutive ticks
    where the lock is held but NO task is in a working status and NO live progress exists. A real
    render sets a working status (AI_GENERATION) within milliseconds of acquiring the lock, so a live
    render can never be seen as orphaned across two ticks. Returns True if it cleared the lock."""
    try:
        if not task_queue.conn.get(task_queue.LOCK_KEY):
            task_queue.conn.delete(_LOCK_SUSPECT_KEY)
            return False
        if _render_appears_live(db):
            task_queue.conn.delete(_LOCK_SUSPECT_KEY)  # a render is genuinely live — not orphaned
            return False
        # Lock held but nothing is actually rendering. Require the condition to persist across two
        # ticks (nx marker) before clearing, so a just-acquired lock is never yanked mid-setup.
        if not task_queue.conn.set(_LOCK_SUSPECT_KEY, "1", nx=True, ex=600):
            task_queue.conn.delete(task_queue.LOCK_KEY)
            task_queue.conn.delete(_LOCK_SUSPECT_KEY)
            logger.warning("Cleared an orphaned render lock — held with no active render across two ticks")
            return True
    except Exception:  # noqa: BLE001 — housekeeping must never raise
        logger.debug("orphaned-lock check failed", exc_info=True)
    return False


def recover_now(db, now: datetime | None = None) -> dict:
    """Operator-triggered recovery, exposed as the Operations page's "Recover stuck renders" button:
    fail definitively-dead tasks and free a render lock left behind by a crashed worker.

    Same recovery the hourly tick performs — the point is not having to wait for the tick (or SSH in)
    when an episode is already stranded. It keeps the render-concurrency-1 guard (`_render_appears
    _live`) but skips the automatic sweep's two-tick delay, because an explicit operator click is
    itself the confirming second signal. Returns {'reaped': n, 'lock_cleared': bool}."""
    reaped = reap_stuck_tasks(db, now=now)
    cleared = False
    try:
        if task_queue.conn.get(task_queue.LOCK_KEY) and not _render_appears_live(db):
            task_queue.conn.delete(task_queue.LOCK_KEY)
            task_queue.conn.delete(_LOCK_SUSPECT_KEY)
            cleared = True
            logger.warning("Operator cleared an orphaned render lock from the Operations page")
    except Exception:  # noqa: BLE001 — recovery must never raise at the operator
        logger.debug("operator lock clear failed", exc_info=True)
    return {"reaped": reaped, "lock_cleared": cleared}


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
    # Only MEASURED episodes count — early views (near-real-time, no retention yet) carry stats_json
    # but no `avg_pct_viewed`, and must not trip the learning threshold or dilute the summary (T4).
    # Ordinary episodes only (ADR-085): a compilation's retention would teach the playbook lessons
    # about a format the scriptwriter never writes.
    from core.compilation import ordinary_episodes

    rows = [r for r in ordinary_episodes(rows)
            if (r.stats_json or {}).get("avg_pct_viewed") is not None]
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
    # Retention drop-off findings (scene where each episode lost viewers) — the same distiller call
    # now also learns WHERE episodes lose people, at zero extra API cost.
    drop_notes = [f"Ep {t.episode_number}: {t.stats_json['drop_summary']}"
                  for t in rows if t.stats_json.get("drop_summary")]
    try:
        update = distill_playbook(
            api_key=api_key,
            model=(user.gemini_model if user else None) or settings.GEMINI_MODEL,
            performance_summary="\n".join(summary_lines),
            current_playbook=learning.get("playbook"),
            reject_reasons=learning.get("reject_reasons"),
            drop_notes=drop_notes,
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
        from core.compilation import COMPILATION_EPISODE_BASE

        published = db.scalar(
            select(func.count()).select_from(Task).where(
                Task.campaign_id == campaign.id,
                Task.status == TaskStatus.COMPLETED,
                # A compilation must not paper over a day the campaign's REGULAR cadence failed.
                Task.episode_number < COMPILATION_EPISODE_BASE,
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
    disk = disk_usage_pct(settings.MEDIA_ROOT)
    sent = 0
    for uid in user_ids:
        user = db.get(User, uid)
        if user is None:
            continue
        # Budget is the user's Settings value when set, else the app-wide fallback (matches the
        # dashboard quota meter in main.py `_system_health`).
        budget = (user.settings_json or {}).get("ai_daily_budget") or settings.GEMINI_DAILY_BUDGET
        quota_bit = f"{calls}/{budget}" if budget else str(calls)
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


def check_monetization_milestones(db) -> int:
    """Announce each channel's newly-crossed monetization milestones (80% and 100% of each
    threshold), once per level per metric (ADR-080). The announced levels live in
    `autopilot_json["milestones"]`, so restarts and daily re-runs never re-fire them."""
    from core import monetize

    announced = 0
    for channel in db.scalars(select(Channel)).all():
        try:
            progress = monetize.channel_progress(db, channel)
        except Exception:  # noqa: BLE001 — a progress hiccup must not stop the daily pass
            logger.warning("Monetization progress failed for channel %s", channel.id, exc_info=True)
            continue
        already = dict((channel.autopilot_json or {}).get("milestones") or {})
        crossed = monetize.crossed_milestones(progress, already)
        if not crossed:
            continue
        user = db.get(User, channel.user_id)
        for key, level in crossed:
            row = next(r for r in progress["rows"] if r["key"] == key)
            done = level >= 100
            summary = (f"{'Reached' if done else f'At {level}% of'} the “{row['label']}” threshold "
                       f"for {progress['program']}: {row['have']:,} of {row['need']:,}."
                       + (" Apply for the program in the platform's studio!"
                          if done and progress.get("eligible") else ""))
            _log_action(db, channel, "milestone", summary)
            if user is not None and done:  # a crossed threshold is phone-worthy; 80% is inbox-only
                video_worker._notify(user, f"💰 {channel.channel_name}: {summary}")
            already[key] = max(int(already.get(key, 0)), level)
            announced += 1
        cfg = dict(channel.autopilot_json or {})
        cfg["milestones"] = already
        channel.autopilot_json = cfg
        db.commit()
    return announced


def daily_learning_pass(db, now: datetime | None = None) -> dict:
    """Once-a-day: re-distill playbooks, check daily minimums, and send the operator heartbeat
    digest. Stats collection moved to its own hourly pass (`hourly_stats_pass`) so first-retention
    latency isn't the daily tick + the ~2-day Analytics lag stacked."""
    result = {"distilled": 0, "min_alerts": 0, "heartbeats": 0}
    for campaign in db.scalars(select(Campaign)).all():
        if maybe_distill_campaign(db, campaign, now=now):
            result["distilled"] += 1
    result["min_alerts"] = check_daily_minimums(db, now=now)
    result["heartbeats"] = send_daily_heartbeat(db, now=now)
    result["milestones"] = check_monetization_milestones(db)
    return result


def hourly_stats_pass(db, now: datetime | None = None) -> dict:
    """Near-real-time early views for young videos (< the Analytics lag) + a retention refresh for
    mature ones. Each internally throttles per-episode work (early ~55 min, retention 24 h), so
    running this hourly is cheap; it just makes fresh data appear within the hour instead of the
    next daily tick. Best-effort — a fetch failure never breaks the tick."""
    from services.analytics_service import (
        collect_channel_snapshots,
        collect_early_stats,
        collect_stats,
    )

    return {"early_stats": collect_early_stats(db, now=now),
            "stats_updated": collect_stats(db, now=now),
            # Channel growth series (ADR-063). Self-throttling to one row per channel per local day,
            # so riding the hourly pass just means the sample lands early in the operator's day.
            "channel_snapshots": collect_channel_snapshots(db, now=now)}


# ── Autopilot: the "hands" — AI review / auto-reject / retry / catch-up publish (ADR-044) ──
AUTOPILOT_MAX_RETRIES = 2  # auto-retry a genuine render failure at most this many times
AUTOPILOT_MAX_REJECTS = 2  # …and auto-reject-and-re-render one episode at most this many times


def _auto_rejects_spent(db, item) -> int:
    """How many re-renders the autopilot has already spent on this episode's quality (ADR-076).
    Lives on the Task, not the buffer row: a re-render DELETES the buffer row and inserts a fresh
    one, so a counter kept there would reset itself every cycle — which is exactly how the loop
    stayed invisible."""
    task = db.scalar(select(Task).where(Task.campaign_id == item.campaign_id,
                                        Task.episode_number == item.episode_number))
    return (task.auto_reject_count or 0) if task is not None else 0

# How long a FAILED render's workspace survives as a resume checkpoint (ADR-069). Long enough for
# the slowest autopilot cadence (24h) and for an operator who retries the next morning; bounded so
# an episode nobody ever retries cannot hold its stills forever.
RESUME_KEEP_HOURS = 24


def fail_orphaned_renders(db, now: datetime | None = None) -> int:
    """At worker BOOT, fail every task still sitting in a working state (ADR-070). Returns the count.

    This is the one moment the answer is certain: there is exactly one worker, machine-wide, and it
    has not started rendering yet — so a task in AI_GENERATION/RENDERING/AUDIO_SYNCED/PUBLISHING is
    the abandoned remains of the previous process (an operator restart, a redeploy whose 300s grace
    expired mid-encode, an OOM kill). Nothing was reporting it: the watchdog only does this
    bookkeeping on the STALL path, not on the restart-request path, so a deliberate "Restart worker"
    click left the episode reading "Rendering 47%" until the stuck-task reaper noticed ~2 hours later.

    Marked FAILED rather than re-queued on purpose: the retry cap still applies, and the message
    classifies as transient (`core.failure`) so the autopilot picks it up and — since R7 keeps the
    workspace — resumes from the scenes already drawn. Re-enqueueing here would outrank the cap and
    could crash-loop on the very episode that killed the worker."""
    now = now or datetime.utcnow()
    orphans = list(db.scalars(select(Task).where(Task.status.in_(_STUCK_STATUSES))).all())
    for task in orphans:
        task.status = TaskStatus.FAILED
        task.finished_at = now
        task.error_message = (
            "The worker restarted while this episode was in flight (operator restart, redeploy or a "
            "crash), so the render was abandoned. Retry picks it up and resumes from the scenes "
            "already rendered — the autopilot does this on its own.")
        task_queue.clear_progress(task.id)
    if orphans:
        db.commit()
        logger.warning("Boot recovery: failed %d orphaned render(s) left by the previous worker: %s",
                       len(orphans), [t.id for t in orphans])
    return len(orphans)


def resume_checkpoint_ids(db) -> set[str]:
    """Workspace names (task ids) the orphan sweep must leave alone: tasks that failed recently
    enough that a retry — autopilot or human — would still resume from them."""
    cutoff = datetime.utcnow() - timedelta(hours=RESUME_KEEP_HOURS)
    return {str(tid) for (tid,) in db.execute(
        select(Task.id).where(Task.status == TaskStatus.FAILED,
                              Task.updated_at >= cutoff)).all()}


AUTOPILOT_LOG_KINDS = ("approved", "rejected", "escalated", "recommended", "retried", "caught_up",
                       "requalified", "expired")
AUTOPILOT_LOG_RETENTION_DAYS = 90  # prune the operational decision log beyond this so it never bloats


def _log_action(db, channel, kind: str, summary: str, *,
                campaign_id: int | None = None, evidence: dict | None = None) -> None:
    """Record ONE autonomous operational decision (approve/reject/escalate/retry/catch-up) as a
    done AutopilotAction so the operator can see what autopilot did and WHY. Status 'done' keeps
    these out of the proposal-idempotency logic. Fail-open: a logging error never breaks the pass."""
    try:
        db.add(AutopilotAction(
            user_id=channel.user_id, channel_id=channel.id, campaign_id=campaign_id,
            kind=kind, summary=summary[:300], evidence=evidence or {}, params={},
            status="done", resolved_at=datetime.utcnow()))
        db.commit()
    except Exception:  # noqa: BLE001 — the audit log is a nicety, never a gate
        db.rollback()
        logger.debug("autopilot action log failed", exc_info=True)


def prune_autopilot_log(db, now: datetime | None = None) -> int:
    """Delete operational log rows (status 'done') older than the retention window. Proposals and
    applied structural changes are kept (they're the audit of real config edits). Returns deleted."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=AUTOPILOT_LOG_RETENTION_DAYS)
    stale = db.scalars(select(AutopilotAction).where(
        AutopilotAction.status == "done", AutopilotAction.created_at < cutoff)).all()
    for row in stale:
        db.delete(row)
    if stale:
        db.commit()
    return len(stale)


def autopilot_review_channel(db, channel, mode: str, approve_min: int, reject_max: int) -> dict:
    """Review every awaiting-review render for a channel's campaigns from its STORED QC verdict
    (0 AI calls). Reject fires in copilot AND autopilot (a rejection never publishes and teaches the
    scriptwriter — safe); approve only in autopilot; copilot instead tags approve-eligible items with
    a hint the Review page shows for one-click confirm. Borderline / verdict-less → escalate."""
    from core import autopilot

    counts = {"approved": 0, "rejected": 0, "recommended": 0, "escalated": 0}
    items = db.scalars(
        select(BufferPoolItem).join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        .where(Campaign.channel_id == channel.id,
               BufferPoolItem.status == BufferStatus.awaiting_review)
        .order_by(BufferPoolItem.id)).all()
    for item in items:
        qc = (item.metadata_json or {}).get("qc")
        score = (qc or {}).get("score")
        ep = item.episode_number
        action, reason = autopilot.review_decision(qc, approve_min, reject_max)
        if action == "reject" and _auto_rejects_spent(db, item) >= AUTOPILOT_MAX_REJECTS:
            # Rejecting re-renders, and a re-render can score badly again — so an episode the judge
            # keeps disliking is an unbounded loop on the one render slot this box has (ADR-076).
            # Nothing else stops it: `apply_reject` never routes through `_fail_task`, so the
            # consecutive-failure circuit breaker is never even consulted. Hand it to the operator
            # instead: escalating parks it, costs nothing, and is the honest answer — after this
            # many tries the machine has no better idea.
            action = "escalate"
            reason = (f"auto-QC rejected {AUTOPILOT_MAX_REJECTS} re-renders of this episode "
                      f"({reason}) — it needs your eye, not another render")
        if action == "reject":
            video_worker.apply_reject(db, item, "auto-review: " + reason, rerender=True,
                                      automatic=True)
            counts["rejected"] += 1
            _log_action(db, channel, "rejected", f"Rejected Ep {ep}: {reason}; re-rendering.",
                        campaign_id=item.campaign_id, evidence={"episode": ep, "qc_score": score})
        elif action == "approve" and mode == "autopilot":
            video_worker.apply_approve(db, item)
            counts["approved"] += 1
            _log_action(db, channel, "approved", f"Approved & published Ep {ep}: {reason}.",
                        campaign_id=item.campaign_id, evidence={"episode": ep, "qc_score": score})
        else:  # copilot approve-eligible, or a borderline/verdict-less item → leave + hint
            had_hint = bool((item.metadata_json or {}).get("ap_hint"))
            md = dict(item.metadata_json or {})
            md["ap_hint"] = {"action": action, "reason": reason}
            item.metadata_json = md
            db.commit()
            counts["recommended" if action == "approve" else "escalated"] += 1
            if not had_hint:  # log the transition ONCE, not every cadence tick
                kind = "recommended" if action == "approve" else "escalated"
                verb = "Recommended for your ✓" if action == "approve" else "Escalated"
                _log_action(db, channel, kind, f"{verb} Ep {ep}: {reason}.",
                            campaign_id=item.campaign_id, evidence={"episode": ep, "qc_score": score})
    return counts


REQUALIFY_MIN_AGE_HOURS = 2   # give a judge outage time to clear before asking again
REQUALIFY_MAX_AUTO = 1        # ONE automatic re-judge per item; after that the button is the human's


def autopilot_requalify_channel(db, channel) -> int:
    """Re-judge parked renders whose QC judge was ABSENT when they rendered (ADR-084/086).

    Batch Q made an unavailable judge park honestly — but then the item waited for a human forever,
    even though `requalify_task` exists and judge outages are quota-shaped (they clear at the
    Pacific reset or within minutes for a rate limit). This queues ONE automatic re-judge per item,
    only after the outage has had ≥ REQUALIFY_MIN_AGE_HOURS to clear, and only below the shared
    budget reserve. The re-judge updates the verdict in place and the item STAYS parked — the next
    review pass routes on the fresh verdict exactly as it would on any other. The marker lives at
    the metadata top level (`auto_requalify`) because `requalify_task` rewrites the `qc` dict."""
    from core.usage import reserve_reached

    user = db.get(User, channel.user_id)
    if user is None or not (user.gemini_api_key or settings.GEMINI_API_KEY):
        return 0
    if reserve_reached(user):
        return 0   # the judge is likely absent for exactly this reason — asking again burns quota
    cutoff = datetime.utcnow() - timedelta(hours=REQUALIFY_MIN_AGE_HOURS)
    queued = 0
    items = db.scalars(
        select(BufferPoolItem).join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        .where(Campaign.channel_id == channel.id,
               BufferPoolItem.status == BufferStatus.awaiting_review)
        .order_by(BufferPoolItem.id)).all()
    for item in items:
        md = item.metadata_json or {}
        qc = md.get("qc") or {}
        if not qc.get("unavailable") or md.get("auto_requalify"):
            continue
        if item.created_at and item.created_at > cutoff:
            continue   # too fresh — the outage that parked it may still be in force
        item.metadata_json = {**md, "auto_requalify": datetime.utcnow().isoformat()}
        db.commit()
        task_queue.enqueue_requalify(item.id)
        queued += 1
        _log_action(db, channel, "requalified",
                    f"Re-running Auto-QC on Ep {item.episode_number} — the judge was unavailable "
                    "when it rendered; the verdict updates in place and the video stays parked.",
                    campaign_id=item.campaign_id,
                    evidence={"episode": item.episode_number,
                              "was": qc.get("unavailable_reason")})
    return queued


def autopilot_retry_channel(db, channel) -> int:
    """Re-queue genuinely-failed renders (both modes — re-rendering never publishes), which is how an
    interrupted render CONTINUES on its own (ADR-069): the retry resumes from the kept checkpoint —
    same persisted script, scenes already drawn are reused — so a mid-episode vendor timeout costs
    only the missing scenes, not the whole render. Skips operator rejects (their decision stands),
    failures a retry cannot fix (missing key, spent quota, safety block — one classification with the
    episode page and the bell: `core.failure`), and tasks already retried to the cap."""
    retried = 0
    # CANCELLED is deliberately absent: an operator who dropped an episode from the queue must not
    # find it back a few minutes later (ADR-064). Only genuine failures are auto-retried.
    # ACTIVE campaigns only. Without this the autopilot fought two other mechanisms: it re-queued the
    # very episodes the consecutive-failure circuit breaker had just stopped a campaign for, and it
    # re-rendered (and, on auto-publish, actually UPLOADED) leftover failures of campaigns the
    # operator had already completed.
    for t in db.scalars(
            select(Task).join(Campaign, Task.campaign_id == Campaign.id)
            .where(Campaign.channel_id == channel.id, Task.status == TaskStatus.FAILED,
                   Campaign.status == CampaignStatus.active)).all():
        msg = (t.error_message or "").lower()
        if "rejected in review" in msg and "auto-review" not in msg:
            continue  # a human rejected this — don't silently re-render it
        # The autopilot's OWN budget (ADR-076). This used to read `retry_count`, which every path
        # increments — so an operator who pressed Retry twice by hand, or two earlier auto-QC
        # rejections, silently spent the self-healing budget R7 exists to provide.
        if (t.auto_retry_count or 0) >= AUTOPILOT_MAX_RETRIES:
            continue
        # A retry can't mint a key or unblock deterministic content — but a spent QUOTA is the one
        # non-transient failure that heals by pure waiting (it resets at midnight US-Pacific), and
        # nothing else ever re-queued those: every quota-failed episode was a manual Retry the
        # operator owed the machine (ADR-085).
        if not failure.is_transient(msg) and not failure.quota_reset_since(msg, t.finished_at):
            continue
        t.status = TaskStatus.PENDING_QUEUE
        t.error_message = None
        t.progress_pct = 0
        t.retry_count += 1
        t.auto_retry_count = (t.auto_retry_count or 0) + 1
        db.commit()
        task_queue.clear_progress(t.id)  # drop any ghost % from the interrupted attempt (F1)
        # Retry the step that actually failed (ADR-085). When the rendered file still exists (a
        # transient UPLOAD failure parked it back to review), only the publish is re-queued — the
        # manual Retry button has always been this smart, while the autopilot re-rendered 30-60
        # minutes of CPU it already owned and then deleted the good file it was replacing.
        buf = db.scalar(select(BufferPoolItem).where(
            BufferPoolItem.campaign_id == t.campaign_id,
            BufferPoolItem.episode_number == t.episode_number,
            BufferPoolItem.status.in_([BufferStatus.ready, BufferStatus.awaiting_review])))
        if buf is not None and buf.video_path and os.path.exists(buf.video_path):
            task_queue.enqueue_publish(buf.id)
            retried += 1
            _log_action(db, channel, "retried",
                        f"Retried the UPLOAD of Ep {t.episode_number} — the video is already "
                        f"rendered, only the publish failed (attempt "
                        f"{t.auto_retry_count}/{AUTOPILOT_MAX_RETRIES}).",
                        campaign_id=t.campaign_id,
                        evidence={"episode": t.episode_number, "attempt": t.auto_retry_count,
                                  "mode": "publish"})
            continue
        t.rq_job_id = video_worker.enqueue_task(t)   # kind-aware: compilations re-concat
        db.commit()
        retried += 1
        _log_action(db, channel, "retried",
                    f"Retried failed render Ep {t.episode_number} (attempt "
                    f"{t.auto_retry_count}/{AUTOPILOT_MAX_RETRIES}).",
                    campaign_id=t.campaign_id,
                    evidence={"episode": t.episode_number, "attempt": t.auto_retry_count})
    return retried


def _published_today(db, campaign_id: int, now_local: datetime, tz_name: str | None) -> int:
    """Count episodes published on `now_local`'s calendar day, in the campaign's timezone."""
    from datetime import timezone as _tz

    try:
        tz = ZoneInfo(tz_name or settings.TIMEZONE)
    except Exception:  # noqa: BLE001
        tz = _tz.utc
    from core.compilation import COMPILATION_EPISODE_BASE

    day = now_local.date()
    n = 0
    # Ordinary episodes only (ADR-085): a published compilation must not count as "today's slot
    # was filled" — the catch-up pass would then skip the regular episode the slot was for.
    for ft in db.scalars(select(Task.finished_at).where(
            Task.campaign_id == campaign_id, Task.status == TaskStatus.COMPLETED,
            Task.episode_number < COMPILATION_EPISODE_BASE,
            Task.finished_at.is_not(None))).all():
        if ft.replace(tzinfo=_tz.utc).astimezone(tz).date() == day:
            n += 1
    return n


def catch_up_due(db, campaign: Campaign, now: datetime | None = None):
    """A ready buffer item to publish NOW because a posting slot earlier today was missed (the buffer
    was empty then and an episode is ready now) — so a finished video isn't wasted waiting a full day
    for the next slot. Conservative: only auto-publish (slotted) campaigns, only on a posting day,
    never while a slot is currently live (the normal publish handles that), never within the
    recently-published guard, and only when fewer posts went out today than slots have already
    passed. Returns the item or None. Bounds bursting to ≤1 per pass per campaign."""
    cfg = campaign.config_json or {}
    if not cfg.get("auto_publish", True):
        return None
    slots = sorted(cfg.get("posting_slots") or [])
    if not slots:
        return None  # continuous mode publishes at render time — nothing to catch up
    now_l = now or local_now(cfg.get("timezone"))
    if not is_posting_day(cfg.get("posting_days") or [], now_l):
        return None
    if is_within_slot(slots, now_l) or _recently_published(db, campaign.id, settings.SLOT_TOLERANCE_MINUTES):
        return None
    now_min = now_l.hour * 60 + now_l.minute
    past_slots = 0
    for s in slots:
        try:
            hh, mm = (int(x) for x in s.split(":"))
        except ValueError:
            continue
        if hh * 60 + mm < now_min:
            past_slots += 1
    if past_slots == 0 or _published_today(db, campaign.id, now_l, cfg.get("timezone")) >= past_slots:
        return None  # nothing missed yet today
    return db.scalar(
        select(BufferPoolItem).where(
            BufferPoolItem.campaign_id == campaign.id, BufferPoolItem.status == BufferStatus.ready,
            # Never catch up an episode the operator moved to a specific time (ADR-059) — its own
            # override is what publishes it.
            BufferPoolItem.publish_at.is_(None))
        .order_by(BufferPoolItem.episode_number).limit(1))


def autopilot_catchup_channel(db, channel, now: datetime | None = None) -> int:
    """Publish one missed-slot recovery per eligible campaign on the channel (both modes — this only
    completes the auto-publish the operator already configured, recovering a slot lost to an empty
    buffer)."""
    published = 0
    for c in db.scalars(select(Campaign).where(
            Campaign.channel_id == channel.id, Campaign.status == CampaignStatus.active)).all():
        buf = catch_up_due(db, c, now)
        if buf is not None:
            task_queue.enqueue_publish(buf.id)
            logger.info("Autopilot catch-up: campaign %s episode %s queued", c.id, buf.episode_number)
            published += 1
            _log_action(db, channel, "caught_up",
                        f"Published Ep {buf.episode_number} of “{c.topic_name}” to catch up a "
                        "missed slot.", campaign_id=c.id, evidence={"episode": buf.episode_number})
    return published




def autopilot_propose_channel(db, channel, now: datetime | None = None) -> int:
    """File strategy proposals (extend / successor / wind-down) for a channel's campaigns into the
    AutopilotAction inbox. Idempotent: never files a second live proposal of the same kind for the
    same campaign, nor re-files one dismissed within REPROPOSE_AFTER_DAYS. Returns the number filed."""
    from core import autopilot

    now = now or datetime.utcnow()
    campaigns = db.scalars(select(Campaign).where(
        Campaign.channel_id == channel.id, Campaign.status == CampaignStatus.active)).all()
    if not campaigns:
        return 0
    cls = autopilot.classify_campaigns(db, campaigns)
    filed = 0
    for c in campaigns:
        tasks = db.scalars(select(Task).where(Task.campaign_id == c.id)).all()
        for p in autopilot.propose_actions(c, tasks, cls[c.id]):
            # Skip if the same (campaign, kind) is already live or was recently dismissed.
            existing = db.scalars(select(AutopilotAction).where(
                AutopilotAction.campaign_id == c.id, AutopilotAction.kind == p["kind"])
                .order_by(AutopilotAction.id.desc())).first()
            if existing is not None:
                if existing.status in ("proposed", "applied"):
                    continue
                if (existing.status == "dismissed" and existing.resolved_at is not None
                        and (now - existing.resolved_at) < timedelta(days=REPROPOSE_AFTER_DAYS)):
                    continue
            db.add(AutopilotAction(
                user_id=channel.user_id, channel_id=channel.id, campaign_id=c.id,
                kind=p["kind"], summary=p["summary"], evidence=p["evidence"], params=p["params"]))
            filed += 1

    # Channel-level audience-geography check (K3): are we actually reaching the target country?
    all_tasks = db.scalars(select(Task).join(Campaign, Task.campaign_id == Campaign.id)
                           .where(Campaign.channel_id == channel.id)).all()
    aud = autopilot.audience_summary(all_tasks, channel.profile_json)
    if aud and aud["match"] is False and aud["measured"] >= autopilot.AUDIENCE_MIN_MEASURED:
        prior = db.scalars(select(AutopilotAction).where(
            AutopilotAction.channel_id == channel.id, AutopilotAction.kind == "audience_drift")
            .order_by(AutopilotAction.id.desc())).first()
        recent_dismissed = (prior is not None and prior.status == "dismissed"
                            and prior.resolved_at is not None
                            and (now - prior.resolved_at) < timedelta(days=REPROPOSE_AFTER_DAYS))
        if not (prior is not None and prior.status in ("proposed", "applied")) and not recent_dismissed:
            db.add(AutopilotAction(
                user_id=channel.user_id, channel_id=channel.id, campaign_id=None,
                kind="audience_drift",
                summary=(f"Audience mismatch on “{channel.channel_name}”: most views come from "
                         f"{aud['country']}, off-target for this channel's language. Check the voice, "
                         "topics and posting time.")[:300],
                evidence={"top_country": aud["country"], "share_pct": aud["pct"],
                          "measured": aud["measured"]}, params={}))
            filed += 1
    if filed:
        db.commit()
    return filed


_SUCCESSOR_CREATIVE_KEYS = ("persona", "style_examples", "catchphrase_open", "catchphrase_close",
                            "continuity", "script_depth", "caption_theme", "color_grade",
                            "music_mood", "cta")


def _design_successor(db, parent):
    """S5: one budget-guarded AI call designing a FRESH successor angle that carries the parent's
    proven formula (its playbook + the channel profile). Returns a CampaignProposal or None (no
    key / over the daily budget / AI error) — the caller then falls back to a plain clone."""
    from core import ai_engine
    from core.usage import reserve_reached

    user = db.get(User, parent.user_id)
    key = (user.gemini_api_key if user else None) or settings.GEMINI_API_KEY
    if not key:
        return None
    if reserve_reached(user):
        return None  # budget reserve — a successor design never eats the quota rendering needs
    channel = db.get(Channel, parent.channel_id)
    pcfg = parent.config_json or {}
    playbook = (parent.learning_json or {}).get("playbook") or []
    context = (f'This is a successor to the proven series "{parent.topic_name}" — keep what works but '
               "give it a genuinely fresh angle, not a rerun.")
    if playbook:
        context += " Proven lessons to carry forward: " + "; ".join(playbook[:5]) + "."
    try:
        return ai_engine.propose_campaign(
            topic=parent.topic_name, language=pcfg.get("language"),
            video_format=pcfg.get("video_format", "short"),
            profile=(channel.profile_json if channel else None),
            api_key=key, model=(user.gemini_model if user else None) or settings.GEMINI_MODEL,
            nonce=parent.id, extra_context=context)
    except Exception:  # noqa: BLE001 — design is an enhancement; a successor is always created
        logger.warning("Successor design failed for campaign %s — cloning instead", parent.id,
                       exc_info=True)
        return None


def _create_successor(db, parent, *, auto_start: bool = False, review_first: bool = False) -> int:
    """A successor carries a proven campaign's FORMULA into a fresh campaign. The base is the parent's
    config (voice/format/schedule/QC/branding — what works); an optional AI design pass (S5) freshens
    the creative layer (topic, persona, catchphrases, caption/grade) while keeping that formula, and
    falls back to a plain "<parent> II" clone when AI is unavailable. Copilot-approved → PENDING for
    the operator to start. Full-auto → auto_start + review_first: it renders but its first videos wait
    for review ("training wheels", ADR-044). Reversible. Returns the new campaign id."""
    config = dict(parent.config_json or {})
    topic = (parent.topic_name + " II")[:255]
    proposal = _design_successor(db, parent)
    if proposal is not None:
        topic = (proposal.topic_name or topic)[:255]
        for k in _SUCCESSOR_CREATIVE_KEYS:  # overlay only the creative layer; keep the proven formula
            v = getattr(proposal, k, None)
            if v:
                config[k] = v
    if review_first:
        config["auto_publish"] = False  # training wheels: gate the new campaign's output on review
    new = Campaign(user_id=parent.user_id, channel_id=parent.channel_id,
                   topic_name=topic,
                   total_episodes=parent.total_episodes,
                   status=CampaignStatus.active if auto_start else CampaignStatus.pending,
                   config_json=config)
    db.add(new)
    db.commit()
    db.refresh(new)
    if auto_start:
        try:
            video_worker.hydrate_campaign(db, new)
        except Exception:  # noqa: BLE001
            logger.warning("successor hydration failed for campaign %s", new.id, exc_info=True)
    return new.id


def autopilot_council_channel(db, user, channel) -> dict:
    """The daily judged pass (ADR-081) — Gemini reads the evidence pack and files proposals through
    the rails. Guarded like the strategist: a Gemini key, the 80% budget reserve (rendering is never
    starved for strategy), and once per UTC day per channel — the pack-hash cache inside makes even
    that call free when nothing changed. Returns the council summary dict (zeros when skipped)."""
    from core import council
    from core.usage import reserve_reached

    zeros = {"filed": 0, "refused": 0, "held": 0, "skipped_unchanged": False}
    gemini_key = None
    if user is not None:
        gemini_key = user.gemini_api_key or settings.GEMINI_API_KEY
    if not gemini_key:
        return zeros
    state = (channel.autopilot_json or {}).get("council") or {}
    if str(state.get("at", ""))[:10] == datetime.utcnow().strftime("%Y-%m-%d"):
        return zeros    # already judged today
    if reserve_reached(user):
        return zeros    # strategy never outbids rendering for the daily AI budget
    model = (user.gemini_model if user else None) or settings.GEMINI_MODEL
    result = council.run_council(db, channel, api_key=gemini_key, model=model)
    log_council_report(db, channel, user, result)
    return result


def log_council_report(db, channel, user, result: dict) -> None:
    """The manager report (D4, ADR-081): the council's own verdict text, delivered like a human
    manager's daily note — what I saw, what I filed, what I'm watching. No extra AI call: the
    verdict IS the report. Logged always; Telegram only when something was filed. Shared by the
    daily pass and the operator's "Run council now" button (ADR-086)."""
    if result["skipped_unchanged"]:
        return
    state = (channel.autopilot_json or {}).get("council") or {}
    watching = "; ".join(state.get("watching") or [])
    report = (state.get("summary", "") +
              (f" — filed {result['filed']} proposal(s) for your review." if result["filed"]
               else " — no changes proposed today.") +
              (f" Watching: {watching}." if watching else ""))
    _log_action(db, channel, "report", report[:300],
                evidence={"filed": result["filed"], "refused": result["refused"]})
    if result["filed"] and user is not None:
        video_worker._notify(user, f"🧠 {channel.channel_name} — {report[:400]}")


def apply_autopilot_action(db, action, *, auto_start_successor: bool = False,
                           review_first_successor: bool = False) -> bool:
    """Apply a proposed action — reversible config changes only, never a delete. Marks the row
    applied (or failed) and returns success. Shared by the Copilot approve route (defaults: a
    successor is created PENDING) and full-auto (auto_start + review_first)."""
    campaign = db.get(Campaign, action.campaign_id) if action.campaign_id else None
    try:
        if action.kind in ("extend", "wind_down"):
            if campaign is None:
                raise ValueError("campaign gone")
            campaign.total_episodes = max(1, int(action.params.get("total_episodes")))
            db.commit()
            if action.kind == "extend":
                try:
                    video_worker.hydrate_campaign(db, campaign)  # render the newly-allowed episodes
                except Exception:  # noqa: BLE001
                    logger.warning("extend hydration failed for campaign %s", campaign.id, exc_info=True)
        elif action.kind == "tune":
            if campaign is None:
                raise ValueError("campaign gone")
            cfg = dict(campaign.config_json or {})
            for k in ("caption_theme", "music_mood", "rate_pct"):
                if k in (action.params or {}):
                    cfg[k] = action.params[k]
            campaign.config_json = cfg
            db.commit()
        elif action.kind == "successor":
            if campaign is None:
                raise ValueError("campaign gone")
            new_id = _create_successor(db, campaign, auto_start=auto_start_successor,
                                       review_first=review_first_successor)
            action.params = {**(action.params or {}), "created_campaign_id": new_id}
        elif action.kind == "compile":
            # Build a best-of from the library (ADR-082). Applying only CREATES + QUEUES the build;
            # the result always parks for review — in every mode — so approving this proposal never
            # publishes anything by itself.
            from core import compilation

            if campaign is None:
                raise ValueError("campaign gone")
            ep = compilation.next_compilation_number(db, campaign.id)
            t = Task(campaign_id=campaign.id, user_id=campaign.user_id, episode_number=ep,
                     video_kind="compilation",
                     render_json={"top_n": int((action.params or {}).get(
                         "top_n", compilation.DEFAULT_TOP_N))})
            db.add(t)
            db.commit()
            db.refresh(t)
            t.rq_job_id = task_queue.enqueue_compile(t.id)
            db.commit()
            action.params = {**(action.params or {}), "created_task_id": t.id}
        elif action.kind == "slot_change":
            # Golden-hour move (ADR-081): swap ONE posting slot, reversibly — the config keeps its
            # other slots and everything else. The council's rails already enforced HH:MM shape,
            # membership and the weekly cooldown; re-check membership here because the operator may
            # have edited slots between proposal and approval.
            if campaign is None:
                raise ValueError("campaign gone")
            cfg = dict(campaign.config_json or {})
            slots = list(cfg.get("posting_slots") or [])
            frm, to = (action.params or {}).get("from"), (action.params or {}).get("to")
            if frm not in slots:
                raise ValueError(f"slot {frm!r} no longer exists on the campaign")
            slots[slots.index(frm)] = to
            cfg["posting_slots"] = slots
            campaign.config_json = cfg
            db.commit()
        else:
            raise ValueError(f"unknown action kind {action.kind!r}")
        action.status = "applied"
        action.resolved_at = datetime.utcnow()
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — a bad action must not crash the pass or the route
        db.rollback()
        action.status = "failed"
        action.summary = (action.summary + f" — failed: {type(exc).__name__}")[:300]
        action.resolved_at = datetime.utcnow()
        db.commit()
        logger.warning("Autopilot action %s failed", action.id, exc_info=True)
        return False


def autopilot_autoapply_channel(db, channel) -> dict:
    """Full-auto only: apply the proposals just filed, with guardrails. Structural, reversible
    actions (extend / wind-down / successor) auto-apply; a successor respects the `max_active` cap
    (default 2) and at most one is created per pass; creative 'tune' proposals are left for the
    operator (creative direction stays human-confirmed). Never deletes anything."""
    cfg = channel.autopilot_json or {}
    max_active = int(cfg.get("max_active", 2) or 2)
    applied = {"extend": 0, "wind_down": 0, "successor": 0}
    successors = 0
    for a in db.scalars(select(AutopilotAction).where(
            AutopilotAction.channel_id == channel.id, AutopilotAction.status == "proposed")
            .order_by(AutopilotAction.id)).all():
        if a.kind == "successor":
            active_n = db.scalar(select(func.count()).select_from(Campaign).where(
                Campaign.channel_id == channel.id, Campaign.status == CampaignStatus.active)) or 0
            if active_n >= max_active or successors >= 1:
                continue  # cap reached — leave it as a proposal for the operator
            if apply_autopilot_action(db, a, auto_start_successor=True, review_first_successor=True):
                successors += 1
                applied["successor"] += 1
        elif a.kind in ("extend", "wind_down", "slot_change", "compile"):
            # slot_change is bounded upstream (one applied change per campaign per week, enforced
            # by the council rails); compile only queues a build that ALWAYS parks for review, so
            # full-auto applying it publishes nothing by itself.
            if apply_autopilot_action(db, a):
                applied[a.kind] = applied.get(a.kind, 0) + 1
    return applied


def autopilot_strategist_channel(db, user, channel, respect_cadence: bool = True) -> int:
    """Weekly: ONE Gemini call suggesting a small creative tweak (caption theme / music mood / TTS
    rate), filed as a suggest-only 'tune' proposal (creative direction always stays operator-
    confirmed, even in full-auto). Guarded three ways: weekly cadence (Redis NX), a Gemini key, and
    the daily-budget reserve (skips above 80% so rendering is never starved). Returns 0 or 1."""
    from core import autopilot
    from core.usage import reserve_reached

    if respect_cadence:
        try:
            if not task_queue.conn.set(f"autopilot:strat:{channel.id}", "1", nx=True, ex=7 * 86400):
                return 0
        except Exception:  # noqa: BLE001
            pass
    key = user.gemini_api_key or settings.GEMINI_API_KEY
    if not key:
        return 0
    if reserve_reached(user):
        return 0  # budget reserve — strategy never eats the quota rendering needs
    campaigns = db.scalars(select(Campaign).where(
        Campaign.channel_id == channel.id, Campaign.status == CampaignStatus.active)).all()
    if not campaigns:
        return 0
    cls = autopilot.classify_campaigns(db, campaigns)
    # S6: tune the WEAKEST measured campaign — the one that actually needs help — not an arbitrary
    # campaigns[0]. Fall back to the first campaign when nothing has measured retention yet.
    measured = [c for c in campaigns if cls[c.id].get("retention") is not None]
    target = min(measured, key=lambda c: cls[c.id]["retention"]) if measured else campaigns[0]
    if db.scalar(select(AutopilotAction).where(
            AutopilotAction.campaign_id == target.id, AutopilotAction.kind == "tune",
            AutopilotAction.status == "proposed").limit(1)):
        return 0  # don't stack tune proposals
    # S4: the target's retention drop-off findings (where viewers leave) — so the strategist reasons
    # about WHICH scene types to fix, not just averages. Zero extra API calls (reuses stored data).
    drop_offs = [t.stats_json["drop_summary"]
                 for t in db.scalars(select(Task).where(Task.campaign_id == target.id)).all()
                 if (t.stats_json or {}).get("drop_summary")]
    scorecard = {
        "channel": channel.channel_name,
        "profile": channel.profile_json or {},  # audience/vision/style/language — the channel persona
        "playbook": (target.learning_json or {}).get("playbook"),
        "tuning": target.topic_name,  # the weakest campaign, the one this tune targets
        "campaigns": [{"topic": c.topic_name, "verdict": cls[c.id]["label"],
                       "retention": cls[c.id]["retention"]} for c in campaigns],
        "retention_drop_offs": drop_offs[:8],
        "current": {k: (target.config_json or {}).get(k)
                    for k in ("caption_theme", "music_mood", "rate_pct")},
    }
    try:
        from core import ai_engine

        tune = ai_engine.suggest_channel_tune(
            scorecard=scorecard, api_key=key, model=user.gemini_model or settings.GEMINI_MODEL)
    except Exception:  # noqa: BLE001 — a strategist hiccup must not disturb the operations loop
        logger.warning("Autopilot strategist failed for channel %s", channel.id, exc_info=True)
        return 0
    params = {}
    if tune.caption_theme:
        params["caption_theme"] = tune.caption_theme
    if tune.music_mood:
        params["music_mood"] = tune.music_mood
    if tune.rate_pct is not None:
        params["rate_pct"] = tune.rate_pct
    if not params:
        return 0  # the AI chose to change nothing
    db.add(AutopilotAction(
        user_id=user.id, channel_id=channel.id, campaign_id=target.id, kind="tune",
        summary=(f"Try a creative tweak on “{target.topic_name}”: {tune.rationale}")[:300],
        evidence={"rationale": tune.rationale}, params=params))
    db.commit()
    return 1


FAILED_PASS_RETRY_SECONDS = 600  # a partly-failed pass may try again in 10 min, not in `interval`


def _channel_can_act(db, channel) -> bool:
    """Can this channel actually publish right now? (ADR-076)

    The pass used to iterate every channel regardless of status, so a Page whose token had died kept
    auto-approving: each approval queued a publish that failed, rolled back, and left the episode
    ready to fail again — while the buffer kept being topped up and then expired. On a box that
    renders one video at a time, that is the render slot being spent on videos that cannot be posted.
    An expired channel needs a token, not another episode; the Channels page already says so."""
    from database.types import ChannelStatus

    if channel.status == ChannelStatus.active:
        return True
    logger.info("Autopilot skipped channel %s — status %s (needs re-authorisation)",
                channel.id, channel.status.value)
    return False


def _ap_step(db, channel, name: str, fn, default, failures: list):
    """Run ONE autopilot step in its own blast radius (ADR-076).

    These steps were a single try block, so the last one could cancel the first: a Gemini 503 inside
    the strategist — an optional, once-a-week creative suggestion — skipped review, retry, catch-up
    and auto-apply, and (because the cadence key was already set) kept skipping them for the whole
    interval, up to 24 hours. Those four need no AI and are the safety net; nothing an optional step
    does should be able to silence them. Records the step name so the caller can shorten the cadence
    instead of banking a failed pass as if it had succeeded."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 — one step's fault must not cost the others their turn
        logger.warning("Autopilot step %r failed for channel %s", name, channel.id, exc_info=True)
        db.rollback()
        failures.append(name)
        return default


def autopilot_pass(db=None, now: datetime | None = None, respect_cadence: bool = True) -> dict:
    """One autopilot cycle across every channel that has it enabled. Per-channel cadence is enforced
    with a Redis NX guard (default 3h, operator-set) so a frequent scheduler tick doesn't over-run a
    channel. Read/enqueue only — never renders inline, so the single-render guarantee holds.

    Steps run cheapest-and-most-important first (review → catch-up → retry, none of which call AI),
    then the optional strategy work, each isolated from the others (ADR-076)."""
    from core import autopilot

    own = db is None
    db = db or SessionLocal()
    summary: dict = {"channels": 0, "approved": 0, "rejected": 0, "recommended": 0,
                     "escalated": 0, "retried": 0, "caught_up": 0, "proposed": 0, "auto_applied": 0,
                     "partial": 0}
    try:
        channels = db.scalars(select(Channel)).all()
        for ch in channels:
            mode = autopilot.ap_mode(ch)
            if mode == "off":
                continue
            if not _channel_can_act(db, ch):
                continue
            if respect_cadence:
                try:
                    # Claimed BEFORE the work, deliberately: this key is also the mutual-exclusion
                    # guard against two overlapping ticks acting on one channel. A failed pass
                    # shortens it below (it must not bank the full interval it never earned).
                    if not task_queue.conn.set(f"autopilot:ch:{ch.id}", "1", nx=True,
                                               ex=autopilot.ap_interval_seconds(ch)):
                        continue  # not due yet for this channel
                except Exception:  # noqa: BLE001 — no Redis → run every tick rather than never
                    pass
            approve_min, reject_max = autopilot.review_thresholds(ch)
            failures: list[str] = []
            zero_review = {"approved": 0, "rejected": 0, "recommended": 0, "escalated": 0}
            # Free and load-bearing first — these are the safety net.
            r = _ap_step(db, ch, "review", lambda: autopilot_review_channel(
                db, ch, mode, approve_min, reject_max), zero_review, failures)
            # Self-healing QC (ADR-086): one vision call per no-verdict park, budget-guarded.
            _ap_step(db, ch, "requalify",
                     lambda: autopilot_requalify_channel(db, ch), 0, failures)
            caught = _ap_step(db, ch, "catchup",
                              lambda: autopilot_catchup_channel(db, ch, now=now), 0, failures)
            retried = _ap_step(db, ch, "retry", lambda: autopilot_retry_channel(db, ch), 0, failures)
            # Optional strategy work — may call AI, may be rate-limited, may simply be down.
            proposed = _ap_step(db, ch, "propose",
                                lambda: autopilot_propose_channel(db, ch, now=now), 0, failures)
            proposed += _ap_step(db, ch, "strategist", lambda: autopilot_strategist_channel(
                db, db.get(User, ch.user_id), ch), 0, failures)
            council_r = _ap_step(db, ch, "council", lambda: autopilot_council_channel(
                db, db.get(User, ch.user_id), ch), {"filed": 0}, failures)
            proposed += council_r.get("filed", 0)
            autoapplied = _ap_step(db, ch, "autoapply", lambda: autopilot_autoapply_channel(db, ch),
                                   {}, failures) if mode == "autopilot" else {}
            if failures:
                summary["partial"] += 1
                if respect_cadence:
                    try:  # come back in minutes; a half-run pass has not earned the full interval
                        task_queue.conn.expire(f"autopilot:ch:{ch.id}",
                                               min(FAILED_PASS_RETRY_SECONDS,
                                                   autopilot.ap_interval_seconds(ch)))
                    except Exception:  # noqa: BLE001 — no Redis; the next tick runs anyway
                        pass
            summary["channels"] += 1
            for k in ("approved", "rejected", "recommended", "escalated"):
                summary[k] += r[k]
            summary["retried"] += retried
            summary["caught_up"] += caught
            summary["proposed"] += proposed
            n_applied = sum(autoapplied.values())
            summary["auto_applied"] += n_applied
            acted = r["approved"] + r["rejected"] + retried + caught + n_applied
            if acted or proposed:
                _autopilot_notify(db, ch, r, retried, caught, proposed, n_applied)
            _record_heartbeat(db, ch, r, retried, caught, proposed, n_applied)
        return summary
    finally:
        if own:
            db.close()


def _record_heartbeat(db, channel, review: dict, retried: int, caught: int,
                      proposed: int, auto_applied: int) -> None:
    """Stamp this channel's autopilot run (time + one-line summary) into its config JSON, so the UI
    can show 'last ran Xh ago' and — crucially — distinguish 'ran, nothing to do' from 'never ran'
    (the tell that the worker container is down). Preserves the operator's config keys."""
    bits = []
    if review["approved"]:
        bits.append(f"approved {review['approved']}")
    if review["rejected"]:
        bits.append(f"rejected {review['rejected']}")
    if review["recommended"]:
        bits.append(f"{review['recommended']} to confirm")
    if review["escalated"]:
        bits.append(f"{review['escalated']} need review")
    if retried:
        bits.append(f"retried {retried}")
    if caught:
        bits.append(f"caught up {caught}")
    if proposed:
        bits.append(f"filed {proposed} proposal(s)")
    if auto_applied:
        bits.append(f"applied {auto_applied} change(s)")
    # Re-read the row first (ADR-076). This is a read-modify-write on a JSON blob the operator edits
    # from the Channels form, and it runs on every pass in a different process from the web app —
    # so a stale in-memory copy here would silently undo a mode or cadence change saved seconds ago.
    # Guarded: the same concurrency that makes the re-read necessary also means the row may be GONE
    # (the operator removed the channel mid-pass), and an unguarded refresh would raise here — past
    # every step's own isolation — and cost the remaining channels their turn.
    try:
        db.refresh(channel)
    except Exception:  # noqa: BLE001 — deleted or detached; there is nothing left to stamp
        logger.info("Channel %s vanished mid-pass — no heartbeat recorded", channel.id)
        db.rollback()
        return
    cfg = dict(channel.autopilot_json or {})
    cfg["last_run"] = {"at": datetime.utcnow().isoformat(),
                       "summary": ", ".join(bits) if bits else "no action needed"}
    channel.autopilot_json = cfg
    db.commit()


def _autopilot_notify(db, channel, review: dict, retried: int, caught: int, proposed: int = 0,
                      auto_applied: int = 0) -> None:
    """Tell the operator what their autopilot did this cycle (Telegram), if anything material did."""
    user = db.get(User, channel.user_id)
    if user is None:
        return
    bits = []
    if review["approved"]:
        bits.append(f"approved+published {review['approved']}")
    if review["rejected"]:
        bits.append(f"rejected {review['rejected']} (re-rendering)")
    if review["recommended"]:
        bits.append(f"{review['recommended']} awaiting your ✓")
    if caught:
        bits.append(f"caught up {caught} missed slot(s)")
    if retried:
        bits.append(f"retried {retried} failed render(s)")
    if auto_applied:
        bits.append(f"applied {auto_applied} strategy change(s)")
    if proposed:
        bits.append(f"filed {proposed} proposal(s) — review under Autopilot")
    if bits:
        video_worker._notify(user, f"🤖 Autopilot · {channel.channel_name}: " + ", ".join(bits) + ".")


def periodic_tick(db=None, now: datetime | None = None) -> dict:
    """One automation cycle. `now` (local time) drives the posting-slot check; buffer expiry uses
    UTC internally to match DB timestamps. Returns a small summary dict."""
    own_session = db is None
    db = db or SessionLocal()
    summary = {"swept": 0, "expired": 0, "hydrated": [], "published": [], "learning": None, "reaped": 0}
    try:
        summary["reaped"] = reap_stuck_tasks(db)
        summary["lock_cleared"] = clear_orphaned_render_lock(db)  # unwedge a crashed-worker lock (F3)
        # Disk hygiene. Never sweep the workspace of a render in flight (its dir mtime goes stale
        # during a long single-scene encode). Recently-FAILED tasks keep theirs too (ADR-069):
        # that workspace is the resume checkpoint, and the autopilot's retry cadence (hours) is
        # slower than the orphan age (minutes) — sweeping it would quietly turn every resume back
        # into a from-scratch re-render. Under real disk pressure the checkpoints are sacrificed:
        # a machine that cannot write at all is strictly worse than a slower retry.
        active = task_queue.active_render_task_ids()
        summary["swept"] = sweep_orphans(skip=active | resume_checkpoint_ids(db))
        if disk_usage_pct(settings.MEDIA_ROOT) >= settings.DISK_PRESSURE_PCT:
            logger.warning("Disk pressure high on %s — sweeping aggressively", settings.MEDIA_ROOT)
            summary["swept"] += sweep_orphans(max_age_minutes=5, skip=active)
        summary["expired"] = expire_stale_buffers(db)

        campaigns = db.scalars(select(Campaign).where(Campaign.status == CampaignStatus.active)).all()
        summary["stalled_channels"] = 0
        for campaign in campaigns:
            # Isolate each campaign — one campaign's fault must not starve the others' hydration or
            # cost them their posting slot this tick.
            try:
                # A campaign on a channel whose token has died must not keep rendering (ADR-076).
                # Every episode it makes would queue a publish that fails, roll back, and then age
                # out of the buffer — on a box that renders one video at a time, that is the whole
                # factory working for nothing while a real campaign waits its turn. The Channels
                # page and the alert bell already say what to do; stopping is the honest response.
                channel = db.get(Channel, campaign.channel_id)
                if channel is not None and not _channel_can_act(db, channel):
                    summary["stalled_channels"] += 1
                    continue
                # Render eagerly — a full buffer is what makes on-the-dot slot publishing possible.
                summary["hydrated"] += video_worker.hydrate_campaign(db, campaign)
                # Publish exactly one pre-rendered episode if this campaign's slot is now.
                published = publish_due_campaign(db, campaign, now=now)
                if published is not None:
                    summary["published"].append(published)
                # A campaign with no path to its own finish line gets closed honestly (ADR-087)
                # instead of sitting "active" at N-1/N forever.
                finish_stranded_campaign(db, campaign)
            except Exception:  # noqa: BLE001 — keep processing the remaining campaigns
                logger.warning("Tick failed for campaign %s", campaign.id, exc_info=True)
                db.rollback()

        # Autopilot: enabled channels manage themselves (review/reject/retry/catch-up). Per-channel
        # cadence is guarded inside the pass; a failure here must not stop the tick.
        try:
            summary["autopilot"] = autopilot_pass(db, now=now)
        except Exception:  # noqa: BLE001
            logger.warning("Autopilot pass failed", exc_info=True)

        # Hourly analytics (NX guard, ~1/hour across ticks/restarts): early views for young videos +
        # a retention refresh for mature ones — so fresh data appears within the hour, not the daily
        # tick. Separate from the daily pass; each fetch internally throttles per-episode work.
        try:
            if task_queue.conn.set("stats:hourly", "1", nx=True, ex=3600):
                # UTC explicitly, NOT this tick's `now` (ADR-076). `now` here is LOCAL time — it
                # drives the posting-slot check — while the stats pass compares it against
                # `Task.finished_at`, which is UTC. In production `now` is None so both are UTC and
                # the bug never fires; any caller passing a local `now` would have shifted the
                # "old enough to measure" cutoff by the whole timezone offset.
                summary["stats"] = hourly_stats_pass(db, now=datetime.utcnow())
        except Exception:  # noqa: BLE001
            logger.warning("Hourly stats pass failed", exc_info=True)

        # Self-improvement pass at most once per day (Redis NX guard across ticks/restarts).
        try:
            if task_queue.conn.set("learning:daily-pass", "1", nx=True, ex=86400):
                summary["learning"] = daily_learning_pass(db)
                summary["pruned_log"] = prune_autopilot_log(db, now=now)
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
