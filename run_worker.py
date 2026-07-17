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
from workers.task_queue import conn, render_queue


def main() -> None:
    logging.basicConfig(level=settings.LOG_LEVEL)
    init_db()  # ensure schema exists before processing jobs
    worker = SimpleWorker([render_queue], connection=conn)
    worker.work(with_scheduler=False, logging_level=settings.LOG_LEVEL)


if __name__ == "__main__":
    main()
