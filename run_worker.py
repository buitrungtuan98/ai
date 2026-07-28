"""RQ worker entrypoint.

One `SimpleWorker` (no fork) consuming the single `renders` queue → renders run strictly one at a
time (ADR-004). SimpleWorker already handles SIGTERM as a warm shutdown: it finishes the current
job, then exits — so a `docker compose` redeploy won't kill a render mid-encode (compose also grants
a 300s stop grace period).
"""
from __future__ import annotations

import logging

from rq import SimpleWorker

from core.config import settings
from database.db_session import init_db
from workers.scheduler import run_scheduler_thread
from workers.task_queue import LOCK_KEY, clear_all_progress, clear_restart_request, conn, render_queue
from workers.watchdog import run_watchdog_thread


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
    run_scheduler_thread()  # periodic buffer hydration + housekeeping (in-process, no extra container)
    run_watchdog_thread()   # wedged-render / operator-restart recovery (ADR-057)
    worker = SimpleWorker([render_queue], connection=conn)
    worker.work(with_scheduler=False, logging_level=settings.LOG_LEVEL)


if __name__ == "__main__":
    main()
