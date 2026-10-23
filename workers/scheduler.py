"""Periodic automation tick — eager rendering, slot-timed publishing, and housekeeping.

Runs as a daemon thread inside the worker process (KISS: no extra container). The tick only enqueues
jobs and sweeps files — it never renders — so the single-render guarantee is untouched.

Cadence model (ADR-011, ADR-090): rendering runs EAGERLY (keep every active campaign's buffer
full), while posting slots control publishing — exactly ONE pre-rendered episode is published per
slot, in the campaign's timezone, on its posting days. Campaigns without slots publish immediately
after render (continuous mode).

Review mode (`auto_publish=False`) is a GATE, not a trigger: it decides whether a human must approve
before an episode may publish, never when it publishes. An approved episode joins the same slot
queue as an auto-published one — see `slot_scheduled`.

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

from core import autopilot, creative_brief, failure
# One dismissal-cooldown definition for every proposer (ADR-086) — the council shares it too.
from core.autopilot import REPROPOSE_AFTER_DAYS
from core.cleanup import sweep_orphans
from core.config import settings
from database.db_session import SessionLocal
from database.models import AutopilotAction, BufferPoolItem, Campaign, Channel, Task, User
from database.types import BufferStatus, CampaignStatus, ChannelStatus, TaskStatus
from workers import task_queue, video_worker
from workers.reconcile import live_buffer, reconcile_task_outcome

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


def slots_for_day(cfg: dict, day: datetime) -> list[str]:
    """The "HH:MM" slots for one local day: that weekday's override from `slots_by_day` (written by
    the best-time engine, R37 — e.g. Mondays post at 06:30 instead of 19:00) or the daily slots.
    ONE definition, shared by the publish check, catch-up and the upcoming-slot list."""
    override = (cfg.get("slots_by_day") or {}).get(WEEKDAY_KEYS[day.weekday()])
    return list(override or cfg.get("posting_slots") or [])


def slot_scheduled(cfg: dict) -> bool:
    """True when a posting schedule owns this campaign's publish TIMING (ADR-090).

    THE definition, because `auto_publish` answers a different question — whether a human approves
    first — and the two were stored in one boolean. Overloading it meant "Review first" silently
    disabled the schedule: the operator's own posting slots were dropped, the campaign vanished from
    the calendar, and the approve click became the only thing left that could publish. Approval is a
    gate; slots are the clock. A campaign with no slots has no clock and publishes at render time."""
    return bool(cfg.get("posting_slots"))


def upcoming_slots(campaign, count: int = 1, now: datetime | None = None) -> list[datetime]:
    """The next `count` posting-slot datetimes for a campaign, in its own timezone, weekday gate
    applied. Empty for continuous / slot-less campaigns.

    ONE definition of "when will this campaign post next" (ADR-067), shared by the dashboard's
    next-slot chip, the calendar grid, the publish list and the approve path — so the time an
    operator is promised on approval is the same time the scheduler will actually use."""
    cfg = campaign.config_json or {}
    if not slot_scheduled(cfg):
        return []
    allowed = cfg.get("posting_days") or []
    now_local = now or local_now(cfg.get("timezone"))
    out: list[datetime] = []
    for dd in range(0, 60):  # a horizon long enough for any weekday-gated pattern
        day = now_local + timedelta(days=dd)
        if allowed and WEEKDAY_KEYS[day.weekday()] not in allowed:
            continue
        for s in sorted(slots_for_day(cfg, day)):
            try:
                hh, mm = (int(x) for x in s.split(":"))
            except ValueError:
                continue
            cand = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand > now_local:
                out.append(cand)
                if len(out) >= count:
                    return out
    return out


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
    channels = {c.id: c for c in db.scalars(select(Channel)).all()}
    items = db.scalars(select(BufferPoolItem).where(BufferPoolItem.status == BufferStatus.ready)
                       .order_by(BufferPoolItem.campaign_id, BufferPoolItem.episode_number)).all()
    publish_in_motion = task_queue.queued_publish_buffer_ids()
    queue_pos: dict[int, int] = {}   # position of each campaign's next item in its publish queue
    expired = 0
    held_changed = False
    for item in items:
        # An upload already on its way is never stale inventory (R22): approving an old-enough
        # episode used to hand it straight to this sweep, which deleted the file minutes before
        # the queued publish job ran it — "Approve & publish" answered by destroying the video.
        if item.id in publish_in_motion:
            continue
        # A channel waiting on a fresh token holds its finished episodes (ADR-073/R22): the wait is
        # the machine's fault, and they publish the moment the operator pastes a new token.
        channel = channels.get(item.channel_id)
        if channel is not None and channel.status == ChannelStatus.expired:
            continue
        cfg = {}
        campaign = campaigns.get(item.campaign_id)
        # A campaign the circuit breaker stopped holds its finished episodes too (R27): the breaker
        # halts slot publishing, so its ready items could only wait — and this sweep then deleted
        # them as "aged out". A run of failed renders (a stale edge-tts, a dead key) cost the
        # episodes that HAD rendered fine. ▶ Start resumes publishing them where they stood.
        # The clock STOPS while held (R30): skipping alone only postponed the delete to the first
        # sweep after the resume, which ran before the publish loop and found the item days old.
        md = dict(item.metadata_json or {})
        if campaign is not None and campaign.status == CampaignStatus.failed:
            if "held_since" not in md:
                item.metadata_json = {**md, "held_since": now.isoformat()}
                held_changed = True
                continue
            # Held, but not forever (R34): a campaign nobody restarts for HOLD_MAX_DAYS would keep
            # its videos on disk for good. Past that, it ages normally — the marker stays, so the
            # hold is not re-armed, and the held time is not credited.
            if now - datetime.fromisoformat(md["held_since"]) < timedelta(days=HOLD_MAX_DAYS):
                continue
            hold_over = True
        else:
            hold_over = False
        if "held_since" in md and not hold_over:   # released — bank the held time, age normally
            since = datetime.fromisoformat(md.pop("held_since"))
            md["held_hours"] = round(float(md.get("held_hours") or 0)
                                     + max(0.0, (now - since).total_seconds() / 3600), 2)
            item.metadata_json = md
            held_changed = True
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
        item_max_age += float(md.get("held_hours") or 0)   # time held paused is not age
        cutoff = now.timestamp() - item_max_age * 3600
        # Age from when the item became PUBLISHABLE (approval), not from the render (R22): days
        # parked awaiting review used to count against the budget, so reviewing late meant the
        # approval itself pushed the episode over the cliff. Pre-R22 rows keep the old base.
        basis = item.ready_at or item.created_at
        created = basis.timestamp() if basis else now.timestamp()
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
                # "aged out before it could publish" is this writer's classification phrase
                # (core/failure.py) — honest for every wait (slot cadence, review, worker backlog),
                # where the old copy blamed a "posting slot" campaigns may not even have.
                task.error_message = (
                    f"Rendered episode aged out before it could publish (waited more than "
                    f"{int(item_max_age)}h). Use Retry to re-render it.")
            # The machine threw finished work away — that belongs in the activity feed, not only
            # in a log file the operator never reads (ADR-087).
            if channel is not None:
                _log_action(db, channel, "expired",
                            f"Expired Ep {item.episode_number} of "
                            f"“{campaign.topic_name if campaign else '?'}” — it waited "
                            f"{int(item_max_age)}h without publishing and went stale; a retry "
                            "re-renders it nearer its turn.",
                            campaign_id=item.campaign_id,
                            evidence={"episode": item.episode_number,
                                      "max_age_hours": int(item_max_age)})
    if expired or held_changed:
        db.commit()
    if expired:
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
    if not total:
        return False
    tasks = [t for t in db.scalars(select(Task).where(Task.campaign_id == campaign.id)).all()
             if t.episode_number < COMPILATION_EPISODE_BASE]
    buffered = db.scalar(select(func.count()).select_from(BufferPoolItem).where(
        BufferPoolItem.campaign_id == campaign.id,
        BufferPoolItem.status.in_([BufferStatus.ready, BufferStatus.awaiting_review]),
        BufferPoolItem.episode_number < COMPILATION_EPISODE_BASE))
    if campaign.current_episode >= total:
        # At its total and still "active" (R30): a wind-down sets total = published, and
        # completion only ever ran on a publish. Rendered extras get to publish first — the one
        # that does completes it the normal way — and once none is left, it is closed here, so
        # auto-create sees the gap instead of a campaign "running" at N/N forever.
        working = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
                   TaskStatus.SCHEDULED, TaskStatus.AWAITING_REVIEW)
        if buffered or any(t.status not in working for t in tasks):
            return False
        video_worker.complete_campaign(db, campaign)
        logger.info("Campaign %s closed at %s/%s", campaign.id, campaign.current_episode, total)
        return True
    by_ep = {t.episode_number: t for t in tasks}
    if any(ep not in by_ep for ep in range(1, total + 1)):
        return False   # episodes still to be created — hydration owns this campaign
    terminal = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
    if any(t.status not in terminal for t in tasks):
        return False   # something is still rendering/waiting — not stranded
    if buffered:
        return False   # a rendered episode is still on its way out
    dead = [t for t in tasks if t.status == TaskStatus.FAILED]
    for t in dead:
        msg = t.error_message or ""
        if (not failure.is_human_reject(msg) and (t.auto_retry_count or 0) < AUTOPILOT_MAX_RETRIES
                and (failure.is_transient(msg) or failure.is_quota(msg))):
            return False   # the autopilot can still save this one — wait for it
    video_worker.complete_campaign(db, campaign)   # also activates the next in line
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
    logger.warning("Campaign %s completed stranded at %s/%s", campaign.id,
                   campaign.current_episode, total)
    return True


# Disk retention (R34). Published videos were "removed after publish" in the operator's mind, but
# three stores kept growing: the per-campaign compilation library (24 masters, never freed), the
# files of deleted campaigns, and the music cache (one MP3 per track ever used).
LIBRARY_KEEP_AFTER_DONE_DAYS = 14   # a finished campaign's library: a best-of window, then freed
MUSIC_CACHE_MAX_AGE_DAYS = 30       # unused that long → re-downloaded on demand if ever picked again
HOLD_MAX_DAYS = 14                  # a breaker-stopped campaign holds its finished videos this long


def _newest_mtime(path: str) -> float:
    try:
        return max((e.stat().st_mtime for e in os.scandir(path)), default=os.path.getmtime(path))
    except OSError:
        return 0.0


def prune_media(db, now: datetime | None = None) -> dict:
    """Free what no feature will read again (R34). Returns counts per store.

    * library/<campaign>: gone when the campaign is deleted, or finished and its newest master is
      older than LIBRARY_KEEP_AFTER_DONE_DAYS (best-of compilations are only proposed for running
      campaigns, so a finished campaign's library is dead weight);
    * buffer/<campaign>: gone when the campaign is deleted (deleting a campaign removed its rows,
      never its files);
    * music_cache: tracks unused for MUSIC_CACHE_MAX_AGE_DAYS (a cache hit refreshes the age)."""
    now = now or datetime.utcnow()
    out = {"library": 0, "buffer": 0, "music": 0}
    campaigns = {c.id: c for c in db.scalars(select(Campaign)).all()}
    root = settings.MEDIA_ROOT

    def subdirs(name: str):
        d = os.path.join(root, name)
        try:
            return [e for e in os.scandir(d) if e.is_dir() and e.name.isdigit()]
        except OSError:
            return []

    lib_cutoff = (now - timedelta(days=LIBRARY_KEEP_AFTER_DONE_DAYS)).timestamp()
    # A compilation still queued or rendering reads its campaign's library (R39).
    compiling = set(db.scalars(select(Task.campaign_id).where(
        Task.video_kind == "compilation",
        Task.status.notin_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]))).all())
    for e in subdirs("library"):
        c = campaigns.get(int(e.name))
        done = c is not None and c.status == CampaignStatus.completed \
            and c.id not in compiling and _newest_mtime(e.path) < lib_cutoff
        if c is None or done:
            shutil.rmtree(e.path, ignore_errors=True)
            out["library"] += 1
    for e in subdirs("buffer"):
        if int(e.name) not in campaigns:
            shutil.rmtree(e.path, ignore_errors=True)
            out["buffer"] += 1
    music_cutoff = (now - timedelta(days=MUSIC_CACHE_MAX_AGE_DAYS)).timestamp()
    try:
        for e in os.scandir(os.path.join(root, "music_cache")):
            if e.is_file() and e.stat().st_mtime < music_cutoff:
                os.remove(e.path)
                out["music"] += 1
    except OSError:
        pass
    if any(out.values()):
        logger.info("Disk retention freed %s", out)
    return out


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
    is now. Review mode needs no gate here (ADR-090): only `ready` items are eligible, and in review
    mode an episode reaches `ready` only by being approved. The old `auto_publish` check meant an
    operator could set an exact publish time on an approved episode and have it silently ignored."""
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
    campaign's own timezone). Returns the buffer id queued, or None.

    Review-first campaigns are served here too (ADR-090). Only `ready` items are eligible and only
    approval makes a reviewed episode `ready`, so the human gate is already enforced by the time
    this runs — and five episodes approved in one sitting go out one per slot, on the campaign's own
    posting days, instead of all five landing the moment they were approved."""
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
    if not slot_scheduled(cfg):
        return None  # no slots: continuous mode already published at render time
    now = now or local_now(cfg.get("timezone"))
    if not is_posting_day(cfg.get("posting_days") or [], now):
        return None  # weekday-gated campaign: today is not a publish day
    if not is_within_slot(slots_for_day(cfg, now), now):
        return None
    if _recently_published(db, campaign.id, settings.SLOT_TOLERANCE_MINUTES):
        return None
    if (cfg.get("slot_changed_on") == now.date().isoformat()
            and _published_today(db, campaign.id, now, cfg.get("timezone"))
            >= len(slots_for_day(cfg, now))):
        return None   # a slot moved today and today's quota already went out at the old time
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
    try:
        still_queued = set(task_queue.render_queue.get_job_ids())
    except Exception:  # noqa: BLE001 — a broken queue read must not stop the reap
        still_queued = set()
    reaped = 0
    for task in stuck:
        # A job genuinely waiting its turn is a backlog, not a corpse (R22): three campaigns'
        # boot hydration alone exceeds the pending cutoff, and reaping a queued task spawned a
        # DUPLICATE render via the autopilot while the original job was still coming.
        if (task.status == TaskStatus.PENDING_QUEUE
                and task.rq_job_id and task.rq_job_id in still_queued):
            continue
        # Work that actually finished repairs its status instead of failing (R22) — see
        # workers/reconcile.py for the crash windows that make status alone a liar here.
        if reconcile_task_outcome(db, task, now=now) is None:
            task.status = TaskStatus.FAILED
            task.finished_at = now
            task.error_message = ("Worker crashed, timed out, or the job was lost (no progress "
                                  "for a long time). Use Retry.")
            if task.rq_job_id and task.rq_job_id in still_queued:
                # Never leave an orphaned job behind a FAILED row — it would run later, re-render,
                # and double-publish the episode the operator has meanwhile retried.
                task_queue.cancel_job(task.rq_job_id)
        task_queue.clear_progress(task.id)  # a crash skipped the finally — drop the ghost % (F1)
        reaped += 1
    if reaped:
        db.commit()
        logger.warning("Reaped %d stuck task(s)", reaped)
    return reaped


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
    failed: list[int] = []
    for task in orphans:
        # The dead worker may have finished the actual work before dying (buffer committed, upload
        # landed) and only lost the status commit — repair instead of failing (R22): boot used to
        # write "abandoned … Retry resumes from the scenes" over finished, QC-passed videos.
        if reconcile_task_outcome(db, task, now=now) is None:
            was_upload = task.status == TaskStatus.PUBLISHING
            task.status = TaskStatus.FAILED
            task.finished_at = now
            # Status-aware copy (R22): telling the operator a killed UPLOAD "resumes from the
            # scenes already rendered" pointed them at a re-render for a video that never needed one.
            task.error_message = (
                "The upload was interrupted by a worker restart (operator restart, redeploy or a "
                "crash). Retry re-attempts the upload — the platform is checked for a duplicate "
                "first." if was_upload else
                "The worker restarted while this episode was in flight (operator restart, redeploy "
                "or a crash), so the render was abandoned. Retry picks it up and resumes from the "
                "scenes already rendered — the autopilot does this on its own.")
            failed.append(task.id)
        task_queue.clear_progress(task.id)
    if orphans:
        db.commit()
        logger.warning("Boot recovery: %d orphaned task(s) left by the previous worker "
                       "(%d failed: %s; the rest reconciled to their finished state)",
                       len(orphans), len(failed), failed)
    return len(orphans)


def reconcile_stranded_episodes(db, now: datetime | None = None) -> dict:
    """Hourly self-heal (R22) for the two stranded shapes crash windows leave behind. Returns
    {'repaired': n, 'requeued': n}.

    (1) FAILED tasks whose work actually survived — a finished render parked for review, a
    publish-ready file, or an upload that landed. The failure writers now reconcile at their own
    decision points, but rows written before this build (or by a path that died mid-way) still sit
    in the DB telling the incident's double story; this pass repairs them within the hour.

    (2) SCHEDULED tasks whose ready buffer has NO publish job anywhere — the enqueue was lost
    (crash between apply_approve's commit and its enqueue, a Redis flush, or a ChannelExpired wait
    whose token has since been fixed). Approval recorded durable intent (`publish_requested_at`);
    re-issue the job instead of letting the approved episode age out into the expiry sweep."""
    now = now or datetime.utcnow()
    healed = {"repaired": 0, "requeued": 0}
    for t in db.scalars(select(Task).where(Task.status == TaskStatus.FAILED)).all():
        if failure.is_human_reject(t.error_message or ""):
            continue  # never resurrect an episode a human rejected
        if reconcile_task_outcome(db, t, now=now) is not None:
            healed["repaired"] += 1
    if healed["repaired"]:
        db.commit()
        logger.warning("Reconcile: repaired %d task(s) whose work had actually finished",
                       healed["repaired"])
    queued = task_queue.queued_publish_buffer_ids()
    grace = now - timedelta(minutes=15)  # never race a just-approved episode's own enqueue
    for t in db.scalars(select(Task).where(Task.status == TaskStatus.SCHEDULED,
                                           Task.updated_at <= grace)).all():
        buf = live_buffer(db, t)
        if buf is None or buf.status != BufferStatus.ready or buf.id in queued:
            continue
        channel = db.get(Channel, buf.channel_id)
        if channel is None or channel.status != ChannelStatus.active:
            continue  # waiting on a fresh token — this pass fires the tick after it is pasted
        campaign = db.get(Campaign, t.campaign_id)
        cfg = (campaign.config_json or {}) if campaign else {}
        # `publish_requested_at` means "a publish job was issued for this item and may have been
        # lost". `apply_approve` writes it ONLY when it enqueues, so a slot-scheduled approval never
        # carries one and this pass leaves it to its slot (ADR-090) — without that pairing, every
        # approved episode would be re-issued here within the hour and publish early anyway.
        if slot_scheduled(cfg) and not (buf.metadata_json or {}).get("publish_requested_at"):
            continue  # the slot scheduler owns this one — nothing was lost
        if buf.publish_at is not None and buf.publish_at > now:
            continue  # the operator parked it for a future time on purpose
        t.rq_job_id = task_queue.enqueue_publish(buf.id)
        db.commit()
        healed["requeued"] += 1
        logger.warning("Reconcile: re-issued the lost publish job for task %s (buffer %s)",
                       t.id, buf.id)
    return healed


def resume_checkpoint_ids(db) -> set[str]:
    """Workspace names (task ids) the orphan sweep must leave alone: tasks that failed recently
    enough that a retry — autopilot or human — would still resume from them."""
    cutoff = datetime.utcnow() - timedelta(hours=RESUME_KEEP_HOURS)
    return {str(tid) for (tid,) in db.execute(
        select(Task.id).where(Task.status == TaskStatus.FAILED,
                              Task.updated_at >= cutoff)).all()}


AUTOPILOT_LOG_KINDS = ("approved", "rejected", "escalated", "recommended", "retried", "caught_up",
                       "requalified", "expired", "reconciled", "resumed")
AUTOPILOT_LOG_RETENTION_DAYS = 90  # prune the operational decision log beyond this so it never bloats


def _log_action(db, channel, kind: str, summary: str, *,
                campaign_id: int | None = None, evidence: dict | None = None) -> None:
    """Record ONE autonomous operational decision (approve/reject/escalate/retry/catch-up) so the
    operator can see what autopilot did and WHY. The writer itself lives in `core.autopilot` — the
    web layer logs campaign edits to the same trail (ADR-089) and there must be one definition."""
    autopilot.log_action(db, channel, kind, summary,
                         campaign_id=campaign_id, evidence=evidence)


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
    # ACTIVE campaigns only (R22): a campaign the circuit breaker paused must actually stop —
    # its parked items used to keep cycling through auto-approve regardless.
    items = db.scalars(
        select(BufferPoolItem).join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        .where(Campaign.channel_id == channel.id,
               Campaign.status == CampaignStatus.active,
               BufferPoolItem.status == BufferStatus.awaiting_review)
        .order_by(BufferPoolItem.id)).all()
    for item in items:
        qc = (item.metadata_json or {}).get("qc")
        score = (qc or {}).get("score")
        ep = item.episode_number
        task = db.scalar(select(Task).where(Task.campaign_id == item.campaign_id,
                                            Task.episode_number == item.episode_number))
        if task is not None and task.status in video_worker._WORKING_TASK_STATUSES:
            continue  # a re-render/publish is in flight — this row is about to be superseded (R22)
        action, reason = autopilot.review_decision(qc, approve_min, reject_max)
        if action == "approve" and (item.metadata_json or {}).get("publish_attempts"):
            # This item already went out once and came back (upload failure) — approving again on
            # the same stored verdict is how the approve→fail→re-park loop ran unbounded (R22).
            # The retry pass owns upload retries (it is budgeted); a second approve adds nothing.
            action, reason = "escalate", ("its upload already failed once — see the episode page "
                                          "for the error; Retry re-attempts the upload")
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
            # `automatic=True` tags the stored message "(auto-review)" structurally — no prefix
            # smuggled through the free-text reason (R22).
            video_worker.apply_reject(db, item, reason, rerender=True, automatic=True)
            counts["rejected"] += 1
            _log_action(db, channel, "rejected", f"Rejected Ep {ep}: {reason}; re-rendering.",
                        campaign_id=item.campaign_id, evidence={"episode": ep, "qc_score": score})
        elif action == "approve" and mode == "autopilot":
            try:
                video_worker.apply_approve(db, item)
            except video_worker.ReviewConflict as exc:
                # A vanished file or an in-flight re-render — approving blind published nothing or
                # the wrong thing (R22). Escalate: parking costs nothing and a human sees why.
                action, reason = "escalate", str(exc)
                db.rollback()
                md = dict(item.metadata_json or {})
                md["ap_hint"] = {"action": action, "reason": reason}
                item.metadata_json = md
                db.commit()
                counts["escalated"] += 1
                _log_action(db, channel, "escalated", f"Escalated Ep {ep}: {reason}",
                            campaign_id=item.campaign_id, evidence={"episode": ep})
                continue
            counts["approved"] += 1
            # Say which of the two things actually happened (ADR-090) — on a slotted campaign this
            # approval scheduled the episode; the log claiming it published would be a false record
            # of an irreversible act.
            item_camp = db.get(Campaign, item.campaign_id)
            landed = ("queued for its posting slot"
                      if item_camp is not None and slot_scheduled(item_camp.config_json or {})
                      else "published")
            _log_action(db, channel, "approved", f"Approved & {landed} Ep {ep}: {reason}.",
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
REQUALIFY_MAX_AUTO = 3        # automatic re-judges per item (R27); after that the button is the human's


def autopilot_requalify_channel(db, channel) -> int:
    """Re-judge parked renders whose QC judge was ABSENT when they rendered (ADR-084/086).

    Batch Q made an unavailable judge park honestly — but then the item waited for a human forever,
    even though `requalify_task` exists and judge outages are quota-shaped (they clear at the
    Pacific reset or within minutes for a rate limit). This queues up to REQUALIFY_MAX_AUTO
    automatic re-judges per item, each ≥ REQUALIFY_MIN_AGE_HOURS after the last, and only below the
    shared budget reserve. A judge absent for a spent DAILY quota is not asked again until a Pacific
    reset has passed (R27): the single re-judge used to fire 2h later into the same spent quota,
    fail identically, and leave the episode for a human — every episode, on a quota-tight day.
    The re-judge updates the verdict in place and the item STAYS parked — the next review pass
    routes on the fresh verdict exactly as it would on any other. The markers live at the metadata
    top level (`auto_requalify` = last attempt, `auto_requalify_n` = count) because
    `requalify_task` rewrites the `qc` dict."""
    from core.usage import reserve_reached

    user = db.get(User, channel.user_id)
    if user is None or not (user.gemini_api_key or settings.GEMINI_API_KEY):
        return 0
    if reserve_reached(user):
        return 0   # the judge is likely absent for exactly this reason — asking again burns quota
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=REQUALIFY_MIN_AGE_HOURS)
    queued = 0
    items = db.scalars(
        select(BufferPoolItem).join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        # Active campaigns only (R30): the review pass skips the rest, so re-judging their parks
        # spent vision calls on a verdict nothing would read.
        .where(Campaign.channel_id == channel.id, Campaign.status == CampaignStatus.active,
               BufferPoolItem.status == BufferStatus.awaiting_review)
        .order_by(BufferPoolItem.id)).all()
    for item in items:
        md = item.metadata_json or {}
        qc = md.get("qc") or {}
        if not qc.get("unavailable"):
            continue
        last = md.get("auto_requalify")
        tries = int(md.get("auto_requalify_n") or (1 if last else 0))  # pre-R27 marker = one try
        if tries >= REQUALIFY_MAX_AUTO:
            continue
        since = datetime.fromisoformat(last) if last else item.created_at
        if since and since > cutoff:
            continue   # too fresh — the outage that parked it may still be in force
        reason = qc.get("unavailable_reason") or ""
        if since and "daily quota" in reason and not failure.quota_reset_since(reason, since, now):
            continue   # the spent quota only comes back at the Pacific reset
        item.metadata_json = {**md, "auto_requalify": now.isoformat(), "auto_requalify_n": tries + 1}
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
        msg = t.error_message or ""
        if failure.is_human_reject(msg):
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
        # What actually survived decides the retry mode (R22) — decided BEFORE the task row is
        # touched. The old order flipped the task to PENDING_QUEUE, wiped the error, and then
        # looked at the buffer; worse, it treated ANY parked buffer as a failed upload and
        # published it — including videos that failed QC or were parked for a human's review,
        # whose task a crash had marked FAILED (the review gate bypassed by a worker OOM).
        outcome = reconcile_task_outcome(db, t)
        if outcome in ("published", "review"):
            db.commit()
            _log_action(db, channel, "reconciled",
                        (f"Ep {t.episode_number} had already published — finalized it instead of "
                         f"retrying." if outcome == "published" else
                         f"Ep {t.episode_number}'s render had already finished — restored it to "
                         f"the review queue (nothing re-rendered, nothing published)."),
                        campaign_id=t.campaign_id,
                        evidence={"episode": t.episode_number, "outcome": outcome})
            continue
        if outcome == "scheduled":
            # Rendered and approved/pre-rendered: retry the step that actually failed (ADR-085) —
            # only the publish is re-queued, never a 30-60 min re-render of a finished video.
            buf = live_buffer(db, t)
            t.retry_count += 1
            t.auto_retry_count = (t.auto_retry_count or 0) + 1
            db.commit()
            task_queue.clear_progress(t.id)  # drop any ghost % from the interrupted attempt (F1)
            t.rq_job_id = task_queue.enqueue_publish(buf.id)
            db.commit()
            retried += 1
            _log_action(db, channel, "retried",
                        f"Retried the UPLOAD of Ep {t.episode_number} — the video is already "
                        f"rendered, only the publish failed (attempt "
                        f"{t.auto_retry_count}/{AUTOPILOT_MAX_RETRIES}).",
                        campaign_id=t.campaign_id,
                        evidence={"episode": t.episode_number, "attempt": t.auto_retry_count,
                                  "mode": "publish"})
            continue
        t.status = TaskStatus.PENDING_QUEUE
        t.error_message = None
        t.progress_pct = 0
        t.retry_count += 1
        t.auto_retry_count = (t.auto_retry_count or 0) + 1
        db.commit()
        task_queue.clear_progress(t.id)  # drop any ghost % from the interrupted attempt (F1)
        t.rq_job_id = video_worker.enqueue_task(t)   # kind-aware: compilations re-concat
        db.commit()
        retried += 1
        _log_action(db, channel, "retried",
                    f"Retried failed render Ep {t.episode_number} (attempt "
                    f"{t.auto_retry_count}/{AUTOPILOT_MAX_RETRIES}).",
                    campaign_id=t.campaign_id,
                    evidence={"episode": t.episode_number, "attempt": t.auto_retry_count})
    return retried


# ── Config-aware recovery: an operator's edit un-strands a gate-failed episode (ADR-089) ──
def retry_after_brief_edits(db, *, enqueue=None) -> int:
    """Re-queue gate-failed episodes whose campaign's creative brief has been EDITED since.

    The pre-render gate fails an episode on honest grounds — two drafts refused means a human has to
    change something (ADR-079) — so `core.failure` marks it non-transient and every automatic retry
    path leaves it alone. Then the human DOES change something: a new topic angle, a rewritten
    persona, a catchphrase retired. Nothing connected the two. The episode stayed FAILED under a
    brief that no longer existed, and the only way back was for the operator to remember which
    episodes had died and press Retry on each.

    Deliberately OUTSIDE the autopilot pass and its budget: the trigger is not a machine judgment
    that might be wrong, it is the operator's own edit — and re-rendering is what an active campaign
    does anyway (hydration renders new episodes with autopilot off too). It never publishes. ONE
    attempt per brief VERSION, with the fingerprint that bought it stamped on the record, so a tick
    every minute cannot re-render the same episode while the operator sits still.
    """
    enqueue = enqueue or video_worker.enqueue_task
    requeued = 0
    for t in db.scalars(
            select(Task).join(Campaign, Task.campaign_id == Campaign.id)
            .where(Task.status == TaskStatus.FAILED,
                   Campaign.status == CampaignStatus.active)).all():
        record = (t.render_json or {}).get("gate_failure") or {}
        if not record.get("brief"):
            continue
        # Structurally impossible today (a rendered episode's success path rewrites render_json and
        # drops this record, so a human can only ever reject an episode that has none) — asserted
        # anyway, because "a human's rejection is the one decision no automatic retry may override"
        # must not depend on a coincidence of write ordering.
        if failure.is_human_reject(t.error_message or ""):
            continue
        try:
            campaign = db.get(Campaign, t.campaign_id)
            channel = db.get(Channel, campaign.channel_id)
            # A channel that cannot publish must not spend the single render slot (ADR-076).
            if channel is None or not _channel_can_act(db, channel):
                continue
            digests = creative_brief.key_digests(campaign, channel, db.get(User, t.user_id))
            fingerprint = creative_brief.fingerprint(digests)
            if fingerprint in (record.get("fingerprint"), record.get("requeued_for")):
                continue  # the same brief, or this exact edit already bought its retry
            edited = creative_brief.changed(record["brief"], digests)
            if not edited:
                continue
            rj = dict(t.render_json or {})
            rj["gate_failure"] = {**record, "requeued_for": fingerprint}
            t.render_json = rj
            t.status = TaskStatus.PENDING_QUEUE
            t.error_message = None
            t.progress_pct = 0
            t.retry_count += 1   # the operator's visible count: this IS another attempt
            db.commit()
            task_queue.clear_progress(t.id)  # no ghost % from the refused attempt (F1)
            t.rq_job_id = enqueue(t)
            db.commit()
            requeued += 1
            _log_action(db, channel, "requeued",
                        f"Re-queued Ep {t.episode_number} — the campaign brief changed after its "
                        f"script was refused ({creative_brief.describe(edited)}), so the next draft "
                        "is written from the current brief.",
                        campaign_id=t.campaign_id,
                        evidence={"episode": t.episode_number, "changed": edited})
        except Exception:  # noqa: BLE001 — one bad row must not stop the sweep
            logger.warning("brief-edit retry failed for task %s", t.id, exc_info=True)
            db.rollback()
    return requeued


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
    # The ONE place `auto_publish` still gates timing, and deliberately (ADR-090). Catch-up publishes
    # OFF-schedule to rescue a slot the machine missed because nothing was rendered yet. In review
    # mode the slot was missed because the human had not approved yet — so firing here would publish
    # the moment they approve, which is exactly the behaviour ADR-090 removes. A reviewed episode
    # waits for a real slot; an operator who wants it out sooner has "Publish now".
    if not cfg.get("auto_publish", True):
        return None
    if not slot_scheduled(cfg):
        return None  # continuous mode publishes at render time — nothing to catch up
    now_l = now or local_now(cfg.get("timezone"))
    slots = sorted(slots_for_day(cfg, now_l))
    if not is_posting_day(cfg.get("posting_days") or [], now_l):
        return None
    if is_within_slot(slots, now_l) or _recently_published(db, campaign.id, settings.SLOT_TOLERANCE_MINUTES):
        return None
    if cfg.get("slot_changed_on") == now_l.date().isoformat():
        return None   # slots moved today: a "missed" slot may just be one that did not exist (R39)
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
    # The weekly analyst's topic ideas (R38) — grounded in this channel's own numbers.
    from core.analyst import topic_ideas

    ideas = topic_ideas(channel) if channel else []
    if ideas:
        context += (" Fresh directions this channel's analysis suggested (use one if it fits): "
                    + "; ".join(f"{i['topic']} ({i['why']})" for i in ideas[:3]) + ".")
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


def _create_successor(db, parent, *, auto_start: bool = False, review_first: bool = False,
                      design=None) -> int:
    """A successor carries a proven campaign's FORMULA into a fresh campaign. The base is the parent's
    config (voice/format/schedule/QC/branding — what works); an optional AI design pass (S5) freshens
    the creative layer (topic, persona, catchphrases, caption/grade) while keeping that formula, and
    falls back to a plain "<parent> II" clone when AI is unavailable. Copilot-approved → PENDING for
    the operator to start. Full-auto → auto_start + review_first: it renders but its first videos wait
    for review ("training wheels", ADR-044). Reversible. Returns the new campaign id. `design` is a
    CampaignProposal the caller already obtained (auto-create's last-resort path, which must not
    fall back to a clone)."""
    config = dict(parent.config_json or {})
    topic = (parent.topic_name + " II")[:255]
    proposal = design if design is not None else _design_successor(db, parent)
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
                   # A last-resort parent was cut short by its wind-down (R30): its total is
                   # where it STOPPED, not a plan — take the fresh design's length instead.
                   total_episodes=(getattr(design, "total_episodes", None)
                                   or parent.total_episodes),
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


def _best_time_samples(db, channel, *, campaign_id: int | None = None,
                       since: datetime | None = None, now: datetime | None = None) -> list:
    """Measured ordinary episodes of a channel (or one campaign) as best-time samples: local
    weekday + hour of publish (each on its campaign's clock) and views at 24h."""
    from core import best_time
    from core.compilation import ordinary_episodes
    from services.analytics_service import publish_local, published_time

    now = now or datetime.utcnow()
    since = since or now - timedelta(days=best_time.WINDOW_DAYS)
    q = (select(Task, Campaign).join(Campaign, Task.campaign_id == Campaign.id)
         .where(Campaign.channel_id == channel.id, Task.status == TaskStatus.COMPLETED,
                Task.published_video_id.isnot(None)))
    if campaign_id is not None:
        q = q.where(Task.campaign_id == campaign_id)
    rows = db.execute(q).all()
    keep = {t.id for t in ordinary_episodes([t for t, _ in rows])}
    out = []
    for t, c in rows:
        v = (t.stats_json or {}).get("views_24h")
        when = published_time(t)
        if t.id not in keep or v is None or when is None or when < since:
            continue
        local = publish_local(t, c)
        out.append(best_time.Sample(best_time.WEEKDAYS[local.weekday()], local.hour, int(v),
                                    (t.render_json or {}).get("style")))
    return out


def autopilot_best_time_channel(db, channel, now: datetime | None = None) -> int:
    """The best-time engine (R37, ADR-101): judge earlier moves, then file at most ONE slot move per
    campaign as a `slot_change` action — the same kind the council files, so its weekly cooldown
    and "already proposed" guard cover both. Copilot: a proposal. Full-auto: auto-applied by the
    autoapply step. Returns actions filed. Pure numbers — no AI call."""
    from core import best_time
    from core.council import _slot_change_recently_applied

    now = now or datetime.utcnow()
    filed = 0
    grid = best_time.build_grid(_best_time_samples(db, channel, now=now))

    def _file(campaign_id: int, summary: str, params: dict, evidence: dict) -> None:
        db.add(AutopilotAction(user_id=channel.user_id, channel_id=channel.id,
                               campaign_id=campaign_id, kind="slot_change", summary=summary[:300],
                               evidence={"source": "best_time", **evidence}, params=params))

    # 1. Did the moves we made pay? Undo the ones that did not (an undo is never judged itself).
    reverted: set[int] = set()
    for a in db.scalars(select(AutopilotAction).where(
            AutopilotAction.channel_id == channel.id, AutopilotAction.kind == "slot_change",
            AutopilotAction.status == "applied")).all():
        ev = a.evidence or {}
        if ev.get("source") != "best_time" or ev.get("revert") or ev.get("judged"):
            continue
        if a.resolved_at is None or a.campaign_id is None:
            continue
        verdict = best_time.judge_move(
            ev, a.params or {},
            _best_time_samples(db, channel, campaign_id=a.campaign_id, since=a.resolved_at, now=now),
            (now - a.resolved_at).total_seconds() / 86400)
        if verdict == "wait":
            continue
        a.evidence = {**ev, "judged": verdict}
        if verdict == "revert":
            p = a.params or {}
            _file(a.campaign_id,
                  f"Undo: posting at {p.get('to')} did worse than {p.get('from')} — moving back",
                  {"from": p.get("to"), "to": p.get("from"), "scope": p.get("scope") or "all"},
                  {"revert": True, "revert_of": a.id})
            reverted.add(a.campaign_id)
            filed += 1
    db.commit()

    # 2. One move per campaign, at most weekly (shared cooldown with the council's moves).
    iso_week = now.isocalendar()[1]
    for c in db.scalars(select(Campaign).where(Campaign.channel_id == channel.id,
                                               Campaign.status == CampaignStatus.active)).all():
        cfg = c.config_json or {}
        if c.id in reverted or not slot_scheduled(cfg):
            continue
        pending = db.scalar(select(func.count()).select_from(AutopilotAction).where(
            AutopilotAction.campaign_id == c.id, AutopilotAction.kind == "slot_change",
            AutopilotAction.status == "proposed")) or 0
        last = db.scalars(select(AutopilotAction).where(
            AutopilotAction.campaign_id == c.id, AutopilotAction.kind == "slot_change")
            .order_by(AutopilotAction.id.desc()).limit(1)).first()
        dismissed_recently = bool(
            last and last.status == "dismissed" and last.resolved_at
            and last.resolved_at >= now - timedelta(days=REPROPOSE_AFTER_DAYS))
        if pending or dismissed_recently or _slot_change_recently_applied(db, c.id):
            continue   # a "no" from the operator holds for REPROPOSE_AFTER_DAYS, as elsewhere
        move = best_time.plan_move(grid, cfg, iso_week=iso_week, channel_id=channel.id)
        if move is None:
            continue
        where = "every day" if move.scope == "all" else f"on {move.scope.title()}"
        _file(c.id, f"Post at {move.to} instead of {move.frm} {where}: {move.reason}",
              {"from": move.frm, "to": move.to, "scope": move.scope},
              {"from_score": move.from_score, "to_score": move.to_score, "explore": move.explore,
               "median_24h": grid["median_24h"], "samples": grid["n"]})
        filed += 1
    db.commit()
    return filed


def _recent_ai_failure(channel, step: str) -> bool:
    at = ((channel.autopilot_json or {}).get("ai_failed_at") or {}).get(step)
    try:
        return bool(at) and datetime.fromisoformat(at) > datetime.utcnow() - timedelta(
            hours=AI_FAILURE_BACKOFF_H)
    except ValueError:
        return False


def _note_ai_failure(db, channel, step: str) -> None:
    """Remember when an optional AI step failed (R38) so the 10-minute partial-pass retry does not
    re-ask a Gemini that is down or out of quota every time. Best-effort."""
    try:
        db.rollback()
        cfg = dict(channel.autopilot_json or {})
        cfg["ai_failed_at"] = {**(cfg.get("ai_failed_at") or {}), step: datetime.utcnow().isoformat()}
        channel.autopilot_json = cfg
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


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
    if _recent_ai_failure(channel, "council"):
        return zeros    # failed within the hour — the partial-pass retry must not re-ask every 10 min
    model = (user.gemini_model if user else None) or settings.GEMINI_MODEL
    try:
        result = council.run_council(db, channel, api_key=gemini_key, model=model)
    except Exception:
        _note_ai_failure(db, channel, "council")
        raise
    log_council_report(db, channel, user, result)
    return result


def autopilot_analyst_channel(db, user, channel, now: datetime | None = None) -> int:
    """The weekly AI analyst (R38, ADR-102): one Gemini call per channel per week, behind the same
    guards as the council (a key, the budget reserve). It decides nothing — the report is shown on
    the Channels page, logged here, and its topic ideas feed the next successor design. Returns 1
    when a report was written."""
    from core import analyst
    from core.usage import reserve_reached

    key = ((user.gemini_api_key if user else None) or settings.GEMINI_API_KEY)
    if not key or not analyst.due(channel, now) or reserve_reached(user):
        return 0
    if _recent_ai_failure(channel, "analyst"):
        return 0
    try:
        state = analyst.run_analyst(
            db, channel, api_key=key, now=now,
            model=(user.gemini_model if user else None) or settings.GEMINI_MODEL)
    except Exception:
        _note_ai_failure(db, channel, "analyst")
        raise
    if not state:
        return 0
    log_analyst_report(db, channel, user, state)
    return 1


def autopilot_review_rules_channel(db, user, channel, mode: str,
                                   deadline: float | None = None) -> int:
    """AI review of rule-made decisions (R38, ADR-102), before autoapply. Full-auto: a veto
    dismisses the action with the AI's reason; anything that prevents a review (no key, budget
    reserve, outage) is recorded as 'unavailable' and the rule decision stands — fail open.
    Copilot: the opinion rides on the proposal, and an unreviewable one simply waits for the next
    pass. At most MAX_REVIEWS_PER_PASS calls. Returns reviews written."""
    from core import decision_review
    from core.usage import reserve_reached

    pending = [a for a in db.scalars(select(AutopilotAction).where(
        AutopilotAction.channel_id == channel.id, AutopilotAction.status == "proposed")
        .order_by(AutopilotAction.id)).all() if decision_review.needs_review(a)]
    if not pending:
        return 0
    key = ((user.gemini_api_key if user else None) or settings.GEMINI_API_KEY)
    blocked = ("no Gemini key" if not key
               else ("today's AI budget is reserved for rendering" if reserve_reached(user)
                     else ("the AI review failed within the hour" if _recent_ai_failure(
                         channel, "ai_review") else None)))
    model = (user.gemini_model if user else None) or settings.GEMINI_MODEL
    written = 0
    for a in pending[:decision_review.MAX_REVIEWS_PER_PASS]:
        if deadline is not None and time.monotonic() >= deadline and not blocked:
            break   # this pass's AI time is spent — the rest wait (autoapply's REVIEW_WAIT_H caps it)
        if blocked:
            result = decision_review.unavailable(blocked)
        else:
            try:
                result = decision_review.review(db, channel, a, api_key=key, model=model)
            except Exception:  # noqa: BLE001 — a review outage must never stall the rules
                logger.warning("AI review failed for action %s", a.id, exc_info=True)
                _note_ai_failure(db, channel, "ai_review")   # Copilot would re-ask every pass
                result = decision_review.unavailable("the AI could not be reached")
                blocked = "the AI review failed within the hour"
        if result["verdict"] == "unavailable" and mode != "autopilot":
            continue   # Copilot: nothing is applied without a human anyway — try again later
        a.evidence = {**(a.evidence or {}), "ai_review": result}
        if result["verdict"] == "veto" and mode == "autopilot":
            a.status, a.resolved_at = "dismissed", datetime.utcnow()
            _log_action(db, channel, "ai_review",
                        f"AI review stopped “{a.summary[:120]}” — {result['reason']}"[:300],
                        campaign_id=a.campaign_id, evidence={"action": a.id, "kind": a.kind})
        db.commit()
        written += 1
    return written


def log_analyst_report(db, channel, user, state: dict) -> None:
    """Shared by the weekly pass and the "Analyse now" button."""
    _log_action(db, channel, "analyst", ("📊 " + state["headline"])[:300],
                evidence={"insights": len(state["insights"]), "ideas": len(state["topic_ideas"]),
                          "dropped": state["dropped"]})
    if user is not None:
        video_worker._notify(user, f"📊 {channel.channel_name} — {state['headline'][:300]}")


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
                           review_first_successor: bool = False, successor_design=None) -> bool:
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
            keys = [k for k in ("caption_theme", "music_mood", "rate_pct") if k in (action.params or {})]
            # What it replaced rides on the action (R28): an auto-applied tune must be undoable by
            # reading the decision log, not by remembering what the campaign used to say.
            action.evidence = {**(action.evidence or {}), "previous": {k: cfg.get(k) for k in keys}}
            for k in keys:
                cfg[k] = action.params[k]
            campaign.config_json = cfg
            db.commit()
        elif action.kind == "successor":
            if campaign is None:
                raise ValueError("campaign gone")
            new_id = _create_successor(db, campaign, auto_start=auto_start_successor,
                                       review_first=review_first_successor,
                                       design=successor_design)
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
            # A best-time move (R37) may be scoped to ONE weekday: that day gets its own slot list
            # in `slots_by_day`, a copy of what it posted at before with the one slot swapped.
            cfg = dict(campaign.config_json or {})
            frm, to = (action.params or {}).get("from"), (action.params or {}).get("to")
            day = (action.params or {}).get("scope") or "all"
            if day == "all":
                slots = list(cfg.get("posting_slots") or [])
            else:
                by_day = dict(cfg.get("slots_by_day") or {})
                slots = list(by_day.get(day) or cfg.get("posting_slots") or [])
            if frm not in slots:
                raise ValueError(f"slot {frm!r} no longer exists on the campaign")
            if to in slots:
                raise ValueError(f"slot {to!r} is already one of the campaign's slots")
            slots[slots.index(frm)] = to
            if day == "all":
                cfg["posting_slots"] = slots
                # Weekday overrides are copies of the daily list (R39): a daily move must move
                # them too, or a Monday keeps posting at the slot just judged weak.
                by_day = {}
                for d, ds in (cfg.get("slots_by_day") or {}).items():
                    ds = [to if x == frm and to not in ds else x for x in ds]
                    if sorted(ds) != sorted(slots):
                        by_day[d] = ds
                cfg["slots_by_day"] = by_day
            elif sorted(slots) == sorted(cfg.get("posting_slots") or []):
                by_day.pop(day, None)            # back to the daily slots — no override needed
                cfg["slots_by_day"] = by_day
            else:
                by_day[day] = slots
                cfg["slots_by_day"] = by_day
            # The day a slot moved, the old slot may already have posted (R39): the publish and
            # catch-up paths read this so that day neither posts twice nor posts off-schedule.
            cfg["slot_changed_on"] = local_now(cfg.get("timezone")).date().isoformat()
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


def autopilot_autoapply_channel(db, channel, allow_ai: bool = True) -> dict:
    """Full-auto only: apply the proposals just filed, with guardrails. Structural, reversible
    actions (extend / wind-down / successor) auto-apply; a successor respects the `max_active` cap
    (default 2) and at most one is created per pass; creative 'tune' proposals are left for the
    operator unless the channel's `auto_tune` switch is on (R28 — the strategist files at most one a
    week, and the replaced values are kept on the action). Never deletes anything."""
    from core import autopilot

    from core import decision_review

    fs = autopilot.factory_settings(channel)
    max_active = fs["max_active"]
    applied = {"extend": 0, "wind_down": 0, "successor": 0}
    successors = 0
    for a in db.scalars(select(AutopilotAction).where(
            AutopilotAction.channel_id == channel.id, AutopilotAction.status == "proposed")
            .order_by(AutopilotAction.id)).all():
        if decision_review.needs_review(a):
            if not decision_review.waited_too_long(a):
                continue   # its AI review (R38) has not run yet — the next pass applies it
            # Fail open on time too: a review that never arrives must not freeze the rules.
            a.evidence = {**(a.evidence or {}), "ai_review": decision_review.unavailable(
                f"no review within {decision_review.REVIEW_WAIT_H}h")}
            db.commit()
        if a.kind == "successor":
            if not allow_ai:
                continue   # its AI design would overrun this pass's AI budget — next pass (R39)
            active_n = db.scalar(select(func.count()).select_from(Campaign).where(
                Campaign.channel_id == channel.id, Campaign.status == CampaignStatus.active)) or 0
            if active_n >= max_active or successors >= 1:
                continue  # cap reached — leave it as a proposal for the operator
            if apply_autopilot_action(db, a, auto_start_successor=True, review_first_successor=True):
                successors += 1
                applied["successor"] += 1
        elif a.kind == "tune" and fs["auto_tune"]:
            if apply_autopilot_action(db, a):
                applied["tune"] = applied.get("tune", 0) + 1
        elif a.kind in ("extend", "wind_down", "slot_change", "compile"):
            # slot_change is bounded upstream (one applied change per campaign per week, enforced
            # by the council rails); compile only queues a build that ALWAYS parks for review, so
            # full-auto applying it publishes nothing by itself.
            if apply_autopilot_action(db, a):
                applied[a.kind] = applied.get(a.kind, 0) + 1
    return applied


REFILL_COOLDOWN_DAYS = 3        # at most one auto-created campaign per channel per this window
RESUME_AFTER_HOURS = 24         # a breaker-stopped campaign waits this long before a retry
MAX_AUTO_RESUMES = 3            # resumes without a success in between, then it's the operator's


def autopilot_refill_channel(db, channel, now: datetime | None = None) -> int:
    """Auto-create (R28): keep `min_active` campaigns running on a full-auto channel.

    The successor path only fires for a HEALTHY campaign near its cap, so a channel whose campaigns
    finished, were wound down, or were deleted simply went quiet until a human noticed. This carries
    the channel's best remaining formula (`pick_refill_parent`) into a new campaign through the
    same `_create_successor` the successor action uses: AI-designed angle, started, and
    review-first ("training wheels") so its first videos wait for the review gate. A stopped
    laggard is a parent only as a last resort, and then only under a FRESH AI design — never a
    plain clone (R29). One per channel per REFILL_COOLDOWN_DAYS, never above `max_active`; the
    first campaign of a channel is always the operator's. Returns 1 if it created one."""
    from core import autopilot

    fs = autopilot.factory_settings(channel)
    if not fs["auto_create"]:
        return 0
    now = now or datetime.utcnow()
    campaigns = db.scalars(select(Campaign).where(Campaign.channel_id == channel.id)).all()
    # A breaker-stopped campaign still holds its slot (R30): replacing it the same pass let a
    # systemic fault (dead key, refused voice) spawn a fresh failing campaign every few days, and
    # auto-resume then pushed the channel past `max_active`. It waits for resume or the operator.
    occupied = (CampaignStatus.active, CampaignStatus.failed)
    if sum(c.status in occupied for c in campaigns) >= fs["min_active"]:
        return 0
    if any(c.status == CampaignStatus.pending for c in campaigns):
        return 0   # the operator has one queued up — starting it is their call, not a refill
    recent = db.scalar(select(func.count()).select_from(AutopilotAction).where(
        AutopilotAction.channel_id == channel.id, AutopilotAction.kind == "successor",
        AutopilotAction.created_at >= now - timedelta(days=REFILL_COOLDOWN_DAYS)))
    if recent:
        return 0
    classes = autopilot.classify_campaigns(db, campaigns)
    parent = autopilot.pick_refill_parent(campaigns, classes)
    if parent is None:
        return 0
    design = None
    stopped = db.scalar(select(func.count()).select_from(AutopilotAction).where(
        AutopilotAction.campaign_id == parent.id, AutopilotAction.kind == "wind_down",
        AutopilotAction.status == "applied"))
    if stopped or not autopilot.is_proven_parent(classes[parent.id]):
        # A flop-breaker or council stop needs no "underperforming" label (R30) — a stopped
        # angle is unproven by definition, and its total records where it stopped.
        # A fresh AI-designed angle or nothing — never a plain clone of the angle that just
        # failed. No key / over budget → try again next pass.
        design = _design_successor(db, parent)
        if design is None:
            return 0
    action = AutopilotAction(
        user_id=channel.user_id, channel_id=channel.id, campaign_id=parent.id, kind="successor",
        summary=(f"Auto-created a new campaign from “{parent.topic_name}” — the channel had fewer "
                 f"than {fs['min_active']} running. Its first videos wait for review.")[:300],
        evidence={"reason": "refill", "min_active": fs["min_active"]}, params={})
    db.add(action)
    db.commit()
    return 1 if apply_autopilot_action(db, action, auto_start_successor=True,
                                       review_first_successor=True,
                                       successor_design=design) else 0


def autopilot_resume_channel(db, channel, now: datetime | None = None) -> int:
    """Auto-resume (R28): restart a campaign the circuit breaker stopped for a fault that heals.

    The breaker is right to stop a campaign whose renders keep failing — but a passing outage (a
    provider blip, a spent quota) left it stopped until a human pressed ▶ Start, and its review
    queue frozen with it. This resumes one after RESUME_AFTER_HOURS when its last failure is
    retryable (`failure.is_transient`, or a quota whose Pacific reset has passed); if the fault is
    still there, the very next failure trips the breaker again (the old streak still counts). A fault that needs a human — a dead
    key, a refused voice service, a safety block — is never resumed, and after MAX_AUTO_RESUMES
    tries without a single success it stays stopped for the operator. Returns campaigns resumed."""
    from core import autopilot

    fs = autopilot.factory_settings(channel)
    if not fs["auto_resume"]:
        return 0
    now = now or datetime.utcnow()
    resumed = 0
    active = db.scalar(select(func.count()).select_from(Campaign).where(
        Campaign.channel_id == channel.id, Campaign.status == CampaignStatus.active)) or 0
    for c in db.scalars(select(Campaign).where(Campaign.channel_id == channel.id,
                                               Campaign.status == CampaignStatus.failed)).all():
        if active >= fs["max_active"]:
            break
        # The CAUSE is the newest failure the breaker counted (R30) — the same rows
        # `consecutive_failures` counts. A later review reject or worker-restart row is not why
        # the campaign stopped, and reading it either blocked a healable resume or hid a dead key.
        last_fail = next((t for t in db.scalars(select(Task).where(
            Task.campaign_id == c.id, Task.status == TaskStatus.FAILED,
            Task.finished_at.isnot(None)).order_by(Task.finished_at.desc())).all()
            if not (failure.is_reject(t.error_message) or failure.is_infrastructure(t.error_message))),
            None)
        if last_fail is None or (now - last_fail.finished_at) < timedelta(hours=RESUME_AFTER_HOURS):
            continue
        msg = last_fail.error_message
        if not (failure.is_transient(msg) or failure.quota_reset_since(msg, last_fail.finished_at, now)):
            continue
        last_ok = db.scalar(select(func.max(Task.finished_at)).where(
            Task.campaign_id == c.id, Task.status == TaskStatus.COMPLETED))
        tries = db.scalar(select(func.count()).select_from(AutopilotAction).where(
            AutopilotAction.campaign_id == c.id, AutopilotAction.kind == "resumed",
            AutopilotAction.created_at >= (last_ok or datetime.min)))
        if tries >= MAX_AUTO_RESUMES:
            continue
        c.status = CampaignStatus.active
        db.commit()
        _log_action(db, channel, "resumed",
                    f"Resumed “{c.topic_name}” — stopped {int((now - last_fail.finished_at).total_seconds() // 3600)}h "
                    f"ago for a fault that heals on its own (try {tries + 1}/{MAX_AUTO_RESUMES}).",
                    campaign_id=c.id, evidence={"try": tries + 1})
        try:
            video_worker.hydrate_campaign(db, c)
        except Exception:  # noqa: BLE001 — the tick's own hydration picks it up next time
            logger.warning("resume hydration failed for campaign %s", c.id, exc_info=True)
        resumed += 1
        active += 1
    return resumed


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


AI_PASS_BUDGET_S = 300   # optional AI steps stop starting new calls after this long in one pass
AI_FAILURE_BACKOFF_H = 1  # a failed analyst/council call is not retried for this long
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
    ai_deadline = time.monotonic() + AI_PASS_BUDGET_S
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
            # Optional strategy work — may call AI, may be rate-limited, may simply be down. The AI
            # steps share one time budget per pass (R38): this pass runs inside the scheduler tick
            # that publishes on time, and a slow Gemini must not hold that tick for half an hour.
            def ai_ok() -> bool:
                return time.monotonic() < ai_deadline

            proposed = _ap_step(db, ch, "propose",
                                lambda: autopilot_propose_channel(db, ch, now=now), 0, failures)
            if ai_ok():
                proposed += _ap_step(db, ch, "strategist", lambda: autopilot_strategist_channel(
                    db, db.get(User, ch.user_id), ch), 0, failures)
            proposed += _ap_step(db, ch, "best_time",
                                 lambda: autopilot_best_time_channel(db, ch, now=now), 0, failures)
            if ai_ok():
                _ap_step(db, ch, "analyst", lambda: autopilot_analyst_channel(
                    db, db.get(User, ch.user_id), ch, now=now), 0, failures)
            if ai_ok():
                council_r = _ap_step(db, ch, "council", lambda: autopilot_council_channel(
                    db, db.get(User, ch.user_id), ch), {"filed": 0}, failures)
                proposed += council_r.get("filed", 0)
            _ap_step(db, ch, "ai_review", lambda: autopilot_review_rules_channel(
                db, db.get(User, ch.user_id), ch, mode, deadline=ai_deadline), 0, failures)
            autoapplied = _ap_step(db, ch, "autoapply", lambda: autopilot_autoapply_channel(
                                       db, ch, allow_ai=ai_ok()),
                                   {}, failures) if mode == "autopilot" else {}
            if mode == "autopilot":   # the factory switches (R28) — each a no-op until turned on
                autoapplied = dict(autoapplied)
                autoapplied["resumed"] = _ap_step(
                    db, ch, "resume", lambda: autopilot_resume_channel(db, ch), 0, failures)
                # Refill designs the new campaign with AI — inside the pass's AI budget, or next pass.
                autoapplied["created"] = _ap_step(
                    db, ch, "refill", lambda: autopilot_refill_channel(db, ch), 0,
                    failures) if ai_ok() else 0
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
        # Self-heal the crash-window strandings (R22): FAILED rows whose work survived, and
        # approved episodes whose publish job was lost — BEFORE hydration/expiry can act on lies.
        summary["reconciled"] = reconcile_stranded_episodes(db)
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
        summary["pruned"] = prune_media(db)

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

        # An operator edit to a campaign's brief revives the episodes its gate refused (ADR-089).
        # Outside the autopilot pass on purpose: the decision was the human's, so it applies to
        # channels that drive everything by hand too. Re-renders only — it never publishes.
        try:
            summary["brief_retries"] = retry_after_brief_edits(db)
        except Exception:  # noqa: BLE001
            logger.warning("Brief-edit retry sweep failed", exc_info=True)
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
            started = time.monotonic()
            try:
                periodic_tick()
            except Exception:  # noqa: BLE001 — a tick failure must not kill the loop
                logger.exception("periodic_tick failed")
            # Start to start, not end to start (R39): slot publishing only happens in this tick,
            # and its ±SLOT_TOLERANCE window is ~1h wide. Sleeping a full interval AFTER a long
            # tick (AI calls) stretched the gap past the window and could skip a slot entirely.
            time.sleep(max(0.0, interval - (time.monotonic() - started)))

    thread = threading.Thread(target=_loop, name="scheduler", daemon=True)
    thread.start()
    return thread
