"""Worker self-recovery: detect a wedged render (or an operator restart request) and exit the
process so compose recreates the container.

Why a thread inside the worker instead of a container healthcheck (ADR-057): a plain
`restart: unless-stopped` policy reacts to a container **exiting**, not to a failing healthcheck —
only Swarm restarts on `unhealthy`. So the healthcheck can only *report* a wedged worker; something
inside the process has to end it. A daemon thread can: a render blocked in ffmpeg, a socket read or
a subprocess wait holds the main thread with the GIL **released**, so this thread keeps running.

The bookkeeping matters as much as the exit — before dying, the stalled Task is marked FAILED with
an actionable message, its progress entry is dropped and the global render lock is released, so the
replacement worker starts clean and the episode is immediately retryable instead of sitting in
RENDERING until the (much later) stuck-task reaper notices.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

from core.config import settings
from database.db_session import SessionLocal
from database.models import Task
from database.types import TaskStatus
from workers import task_queue

logger = logging.getLogger(__name__)


def _die(code: int = 1) -> None:
    """Leave immediately. `os._exit` skips interpreter cleanup on purpose: the normal paths (a
    SIGTERM warm shutdown, or `sys.exit` from a non-main thread) both wait on the very job that is
    wedged, so they would never return."""
    os._exit(code)


def fail_stalled_task(db, task_id: int, stalled_seconds: float) -> bool:
    """Mark a wedged render FAILED so it is retryable the moment the worker comes back. Returns
    False when the row is gone or already finished (nothing to do)."""
    task = db.get(Task, task_id)
    if task is None or task.status not in (
        TaskStatus.AI_GENERATION, TaskStatus.RENDERING, TaskStatus.AUDIO_SYNCED, TaskStatus.PUBLISHING
    ):
        return False
    minutes = int(stalled_seconds // 60)
    task.status = TaskStatus.FAILED
    task.finished_at = datetime.utcnow()
    task.error_message = (
        f"Render stalled — no progress for {minutes} minutes, past this job's own timeout. The "
        "worker was restarted automatically to free the queue. Use Retry to render this episode "
        "again; if it stalls repeatedly, check the Operations page and the image/TTS provider."
    )
    db.commit()
    return True


def check_once(db=None, *, exit_fn=_die) -> str | None:
    """One watchdog pass. Returns the reason it asked the process to exit ('restart' | 'stalled'),
    or None when nothing is wrong. Never raises — a watchdog that crashes is worse than none."""
    own = db is None
    db = db or SessionLocal()
    try:
        if task_queue.restart_requested():
            # Consume the flag first: if this exit races with a crash, the replacement worker must
            # not read the same flag and immediately exit again.
            task_queue.clear_restart_request()
            logger.warning("Operator requested a worker restart — exiting so compose recreates it")
            exit_fn(0)
            return "restart"

        stalled = task_queue.stalled_render()
        if stalled is None:
            return None
        task_id, seconds = stalled
        logger.error("Render for task %s has not progressed in %.0fs (limit %ss) — the worker is "
                     "wedged; failing the task and restarting", task_id, seconds,
                     task_queue.stall_limit_seconds())
        try:
            if fail_stalled_task(db, task_id, seconds):
                _notify_stall(db, task_id, seconds)
        except Exception:  # noqa: BLE001 — bookkeeping must never block the restart
            db.rollback()
            logger.exception("Could not fail the stalled task %s before restarting", task_id)
        # Release the render lock and the ghost progress entry so the replacement worker can render
        # immediately instead of waiting out the lock TTL.
        task_queue.clear_progress(task_id)
        try:
            task_queue.conn.delete(task_queue.LOCK_KEY)
        except Exception:  # noqa: BLE001
            logger.debug("could not release the render lock", exc_info=True)
        exit_fn(1)
        return "stalled"
    except Exception:  # noqa: BLE001 — the loop below must survive any fault
        logger.exception("watchdog pass failed")
        return None
    finally:
        if own:
            db.close()


def _notify_stall(db, task_id: int, seconds: float) -> None:
    """Tell the operator the box healed itself — a silent auto-restart is indistinguishable from a
    lost episode. Best-effort; a failed alert never blocks the restart.

    The failure is infrastructural, so it deliberately does NOT feed the campaign's consecutive-
    failure circuit breaker: a wedged worker is not evidence that the campaign's config is broken.
    """
    from database.models import Campaign, User
    from workers import video_worker

    task = db.get(Task, task_id)
    if task is None:
        return
    campaign = db.get(Campaign, task.campaign_id)
    user = db.get(User, task.user_id)
    if user is None:
        return
    topic = campaign.topic_name if campaign else "a campaign"
    video_worker._notify(
        user,
        f"🔁 Episode {task.episode_number} of '{topic}' stalled for {int(seconds // 60)} minutes "
        "and the worker was restarted automatically. The episode is marked failed — Retry it from "
        "the Episodes page (autopilot will retry it for you if enabled).",
    )


def run_watchdog_thread(interval: int | None = None) -> threading.Thread:
    """Start the stall/restart check in a daemon thread. Deliberately separate from the scheduler
    thread: the scheduler ticks hourly, which would leave a wedged render sitting for up to an hour,
    while this check is two Redis reads and can run every minute."""
    interval = settings.WATCHDOG_INTERVAL_SECONDS if interval is None else interval

    def _loop() -> None:
        logger.info("Watchdog thread started (interval=%ss, stall limit=%ss)",
                    interval, task_queue.stall_limit_seconds())
        while True:
            time.sleep(interval)
            check_once()

    thread = threading.Thread(target=_loop, name="watchdog", daemon=True)
    thread.start()
    return thread
