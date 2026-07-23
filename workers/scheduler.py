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

    Campaigns with weekday-gated publishing (`posting_days`) get a stretched window (≥ 7.5 days):
    a healthy pre-render can legitimately wait most of a week for its publish day — expiring it at
    the default 72h would destroy it before its slot ever arrived."""
    now = now or datetime.utcnow()
    max_age_hours = settings.BUFFER_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    campaigns = {c.id: c for c in db.scalars(select(Campaign)).all()}
    items = db.scalars(select(BufferPoolItem).where(BufferPoolItem.status == BufferStatus.ready)).all()
    expired = 0
    for item in items:
        cfg = {}
        campaign = campaigns.get(item.campaign_id)
        if campaign is not None:
            cfg = campaign.config_json or {}
        item_max_age = max(max_age_hours, 7 * 24 + 12) if cfg.get("posting_days") else max_age_hours
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
    if not is_posting_day(cfg.get("posting_days") or [], now):
        return None  # weekday-gated campaign: today is not a publish day
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
            model=(user.gemini_model if user else None) or settings.GEMINI_MODEL,
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


# ── Autopilot: the "hands" — AI review / auto-reject / retry / catch-up publish (ADR-044) ──
AUTOPILOT_MAX_RETRIES = 2  # auto-retry a genuine render failure at most this many times


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
        action, reason = autopilot.review_decision(qc, approve_min, reject_max)
        if action == "reject":
            video_worker.apply_reject(db, item, "auto-review: " + reason, rerender=True)
            counts["rejected"] += 1
        elif action == "approve" and mode == "autopilot":
            video_worker.apply_approve(db, item)
            counts["approved"] += 1
        else:  # copilot approve-eligible, or a borderline/verdict-less item → leave + hint
            md = dict(item.metadata_json or {})
            md["ap_hint"] = {"action": action, "reason": reason}
            item.metadata_json = md
            db.commit()
            counts["recommended" if action == "approve" else "escalated"] += 1
    return counts


def autopilot_retry_channel(db, channel) -> int:
    """Re-queue genuinely-failed renders (both modes — re-rendering never publishes). Skips operator
    rejects (their decision stands), quota exhaustion (wait for the reset — don't burn it), and tasks
    already retried to the cap."""
    retried = 0
    for t in db.scalars(
            select(Task).join(Campaign, Task.campaign_id == Campaign.id)
            .where(Campaign.channel_id == channel.id, Task.status == TaskStatus.FAILED)).all():
        msg = (t.error_message or "").lower()
        if "rejected in review" in msg and "auto-review" not in msg:
            continue  # a human rejected this — don't silently re-render it
        if t.retry_count >= AUTOPILOT_MAX_RETRIES:
            continue
        if any(k in msg for k in ("429", "quota", "exhaust", "rate limit")):
            continue  # quota is spent; the reset is what fixes it, not another attempt now
        t.status = TaskStatus.PENDING_QUEUE
        t.error_message = None
        t.progress_pct = 0
        t.retry_count += 1
        db.commit()
        t.rq_job_id = task_queue.enqueue_render(t.id)
        db.commit()
        retried += 1
    return retried


def _published_today(db, campaign_id: int, now_local: datetime, tz_name: str | None) -> int:
    """Count episodes published on `now_local`'s calendar day, in the campaign's timezone."""
    from datetime import timezone as _tz

    try:
        tz = ZoneInfo(tz_name or settings.TIMEZONE)
    except Exception:  # noqa: BLE001
        tz = _tz.utc
    day = now_local.date()
    n = 0
    for ft in db.scalars(select(Task.finished_at).where(
            Task.campaign_id == campaign_id, Task.status == TaskStatus.COMPLETED,
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
            BufferPoolItem.campaign_id == campaign.id, BufferPoolItem.status == BufferStatus.ready)
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
    return published


REPROPOSE_AFTER_DAYS = 30  # don't re-file a proposal the operator dismissed until this long passes


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
    if filed:
        db.commit()
    return filed


def _create_successor(db, parent, *, auto_start: bool = False, review_first: bool = False) -> int:
    """A successor = a clone of a proven campaign's config (same persona/voice/format/schedule),
    titled "<parent> II". Copilot-approved → PENDING for the operator to start. Full-auto →
    auto_start + review_first: it begins rendering but its first videos wait for review ("training
    wheels", ADR-044) even in full-auto, so the operator sees the new campaign's quality before it
    ever self-publishes. Deterministic, 0-AI, reversible. Returns the new campaign id."""
    config = dict(parent.config_json or {})
    if review_first:
        config["auto_publish"] = False  # training wheels: gate the new campaign's output on review
    new = Campaign(user_id=parent.user_id, channel_id=parent.channel_id,
                   topic_name=(parent.topic_name + " II")[:255],
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
        elif a.kind in ("extend", "wind_down"):
            if apply_autopilot_action(db, a):
                applied[a.kind] += 1
    return applied


def autopilot_strategist_channel(db, user, channel, respect_cadence: bool = True) -> int:
    """Weekly: ONE Gemini call suggesting a small creative tweak (caption theme / music mood / TTS
    rate), filed as a suggest-only 'tune' proposal (creative direction always stays operator-
    confirmed, even in full-auto). Guarded three ways: weekly cadence (Redis NX), a Gemini key, and
    the daily-budget reserve (skips above 80% so rendering is never starved). Returns 0 or 1."""
    from core import autopilot
    from core.usage import ai_calls_today

    if respect_cadence:
        try:
            if not task_queue.conn.set(f"autopilot:strat:{channel.id}", "1", nx=True, ex=7 * 86400):
                return 0
        except Exception:  # noqa: BLE001
            pass
    key = user.gemini_api_key or settings.GEMINI_API_KEY
    if not key:
        return 0
    budget = (user.settings_json or {}).get("ai_daily_budget") or settings.GEMINI_DAILY_BUDGET
    if budget and ai_calls_today() >= budget * 0.8:
        return 0  # budget reserve — strategy never eats the quota rendering needs
    campaigns = db.scalars(select(Campaign).where(
        Campaign.channel_id == channel.id, Campaign.status == CampaignStatus.active)).all()
    if not campaigns:
        return 0
    target = campaigns[0]
    if db.scalar(select(AutopilotAction).where(
            AutopilotAction.campaign_id == target.id, AutopilotAction.kind == "tune",
            AutopilotAction.status == "proposed").limit(1)):
        return 0  # don't stack tune proposals
    cls = autopilot.classify_campaigns(db, campaigns)
    scorecard = {
        "channel": channel.channel_name,
        "playbook": (target.learning_json or {}).get("playbook"),
        "campaigns": [{"topic": c.topic_name, "verdict": cls[c.id]["label"],
                       "retention": cls[c.id]["retention"]} for c in campaigns],
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


def autopilot_pass(db=None, now: datetime | None = None, respect_cadence: bool = True) -> dict:
    """One autopilot cycle across every channel that has it enabled. Per-channel cadence is enforced
    with a Redis NX guard (default 3h, operator-set) so a frequent scheduler tick doesn't over-run a
    channel. Read/enqueue only — never renders inline, so the single-render guarantee holds."""
    from core import autopilot

    own = db is None
    db = db or SessionLocal()
    summary: dict = {"channels": 0, "approved": 0, "rejected": 0, "recommended": 0,
                     "escalated": 0, "retried": 0, "caught_up": 0, "proposed": 0, "auto_applied": 0}
    try:
        channels = db.scalars(select(Channel)).all()
        for ch in channels:
            mode = autopilot.ap_mode(ch)
            if mode == "off":
                continue
            if respect_cadence:
                try:
                    if not task_queue.conn.set(f"autopilot:ch:{ch.id}", "1", nx=True,
                                               ex=autopilot.ap_interval_seconds(ch)):
                        continue  # not due yet for this channel
                except Exception:  # noqa: BLE001 — no Redis → run every tick rather than never
                    pass
            approve_min, reject_max = autopilot.review_thresholds(ch)
            try:
                r = autopilot_review_channel(db, ch, mode, approve_min, reject_max)
                caught = autopilot_catchup_channel(db, ch, now=now)
                retried = autopilot_retry_channel(db, ch)
                proposed = autopilot_propose_channel(db, ch, now=now)
                proposed += autopilot_strategist_channel(db, db.get(User, ch.user_id), ch)
                autoapplied = autopilot_autoapply_channel(db, ch) if mode == "autopilot" else {}
            except Exception:  # noqa: BLE001 — one channel's fault must not stop the others
                logger.warning("Autopilot failed for channel %s", ch.id, exc_info=True)
                db.rollback()
                continue
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
        return summary
    finally:
        if own:
            db.close()


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

        # Autopilot: enabled channels manage themselves (review/reject/retry/catch-up). Per-channel
        # cadence is guarded inside the pass; a failure here must not stop the tick.
        try:
            summary["autopilot"] = autopilot_pass(db, now=now)
        except Exception:  # noqa: BLE001
            logger.warning("Autopilot pass failed", exc_info=True)

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
