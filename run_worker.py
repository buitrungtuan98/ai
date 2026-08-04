"""RQ worker entrypoint.

One `SimpleWorker` (no fork) consuming the single `renders` queue → renders run strictly one at a
time (ADR-004). SimpleWorker already handles SIGTERM as a warm shutdown: it finishes the current
job, then exits — so a `docker compose` redeploy won't kill a render mid-encode (compose also grants
a 300s stop grace period).
"""
from __future__ import annotations

import logging

from rq import SimpleWorker
from rq.timeouts import UnixSignalDeathPenalty

from core.config import settings
from database.db_session import init_db
from database.db_session import SessionLocal
from workers.scheduler import fail_orphaned_renders, run_scheduler_thread
from workers.task_queue import LOCK_KEY, clear_all_progress, clear_restart_request, conn, render_queue
from workers.watchdog import run_watchdog_thread


class JobHardTimeout(BaseException):
    """RQ's job-timeout signal, made unswallowable (R22).

    Stock rq raises JobTimeoutException, an ordinary Exception — and the pipeline is full of broad
    `except Exception` retry/fail-open handlers (structured-output retries, image/TTS retry loops,
    QC fail-open). When the one-shot SIGALRM fired inside one of those, the timeout was logged as a
    transient provider error, retried, and the job ran on with NO timeout armed at all — leaving the
    watchdog's mid-transaction os._exit as the only terminator. Deriving from BaseException means no
    handler in the pipeline can eat it; rq's perform_job still records the job failed."""


class _HardTimeoutPenalty(UnixSignalDeathPenalty):
    def handle_death_penalty(self, signum, frame):  # noqa: ARG002 — signal-handler signature
        raise JobHardTimeout(f"Task exceeded maximum timeout value ({self._timeout} seconds)")


class FactoryWorker(SimpleWorker):
    """SimpleWorker whose death penalty cannot be swallowed by pipeline exception handlers."""

    death_penalty_class = _HardTimeoutPenalty


def main() -> None:
    logging.basicConfig(level=settings.LOG_LEVEL)
    init_db()  # ensure schema exists before processing jobs
    # A render lock present at startup is a crash artifact: this is the one worker, and it is not
    # rendering yet, so no live render owns it. Clearing it prevents a hard crash mid-render from
    # dead-lettering every queued job (each would fail to acquire the still-held lock).
    conn.delete(LOCK_KEY)
    # Same reasoning for live progress: nothing is rendering yet, so every entry is a crash artifact.
    # Leaving one behind would read as a permanently stalled render and put the watchdog into a
    # restart loop. A restart flag consumed by the previous process must not kill this one either.
    clear_all_progress()
    clear_restart_request()
    # The DB rows that belonged to those artifacts (ADR-070). Redis is now clean, but a task killed
    # mid-render still reads RENDERING — a "Rendering 47%" that nothing is working on. Failing it here
    # makes it immediately retryable (and R7 makes that retry a resume) instead of waiting ~2h for the
    # stuck-task reaper. This is why the "Restart worker" button can promise the render is retried.
    with SessionLocal() as db:
        fail_orphaned_renders(db)
    run_scheduler_thread()  # periodic buffer hydration + housekeeping (in-process, no extra container)
    run_watchdog_thread()   # wedged-render / operator-restart recovery (ADR-057)
    worker = FactoryWorker([render_queue], connection=conn)
    worker.work(with_scheduler=False, logging_level=settings.LOG_LEVEL)


if __name__ == "__main__":
    main()
