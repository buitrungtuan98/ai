"""Queue wiring — the single source of truth for the Redis queue, the global render lock, and live
progress (DRY: queue name, lock key, and Redis URL are defined ONLY here).

Render concurrency is 1 (ADR-004). It is guaranteed by topology (one worker, one SimpleWorker) and
belt-and-suspenders by `with_render_lock` — a Redis mutex so even a stray second worker can't run a
second render.

High-frequency progress is written to Redis (not SQLite) so the single DB writer stays near-idle.
"""
from __future__ import annotations

import functools
from collections.abc import Callable

import redis
from rq import Queue

from core.config import settings

QUEUE_NAME = "renders"
LOCK_KEY = "render:global-lock"
_PROGRESS_KEY = "task:progress"  # a Redis hash: field=<task_id> value=<pct>

# redis-py connects lazily; importing this module does not require a live server (tests inject
# a fake connection via `set_connection`).
conn: redis.Redis = redis.from_url(settings.REDIS_URL)
render_queue = Queue(QUEUE_NAME, connection=conn)


def set_connection(new_conn: redis.Redis) -> None:
    """Swap the Redis connection (used by tests with fakeredis)."""
    global conn, render_queue
    conn = new_conn
    render_queue = Queue(QUEUE_NAME, connection=conn)


def enqueue_render(task_id: int) -> str:
    """Enqueue a render job for a Task row. Returns the RQ job id."""
    job = render_queue.enqueue(
        "workers.video_worker.render_task",
        task_id,
        job_timeout=settings.JOB_TIMEOUT_SECONDS,
        result_ttl=3600,
    )
    return job.id


def enqueue_publish(buffer_item_id: int) -> str:
    """Enqueue a publish (upload) job for an approved buffer item. Same queue/worker, so uploads
    stay sequential with renders (KISS on one box); a short upload never blocks for long."""
    job = render_queue.enqueue(
        "workers.video_worker.publish_task",
        buffer_item_id,
        job_timeout=1800,
        result_ttl=3600,
    )
    return job.id


def with_render_lock(fn: Callable) -> Callable:
    """Ensure at most one render runs cluster-wide. The lock has a TTL so a crashed worker can't
    wedge the queue forever."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        acquired = conn.set(LOCK_KEY, "1", nx=True, ex=settings.JOB_TIMEOUT_SECONDS + 60)
        if not acquired:
            raise RuntimeError("another render holds the global lock")
        try:
            return fn(*args, **kwargs)
        finally:
            conn.delete(LOCK_KEY)

    return wrapper


# ── Live progress (Redis-backed) ─────────────────────────────────────────────
def set_progress(task_id: int, pct: float) -> None:
    conn.hset(_PROGRESS_KEY, str(task_id), f"{pct:.1f}")


def get_progress(task_id: int) -> float:
    raw = conn.hget(_PROGRESS_KEY, str(task_id))
    return float(raw) if raw is not None else 0.0


def clear_progress(task_id: int) -> None:
    conn.hdel(_PROGRESS_KEY, str(task_id))


def worker_alive() -> bool:
    """True if at least one RQ worker is registered (used by the worker healthcheck)."""
    try:
        from rq import Worker

        return len(Worker.all(connection=conn)) > 0
    except Exception:  # noqa: BLE001 — healthcheck must never raise
        return False
