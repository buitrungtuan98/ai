"""Queue wiring — the single source of truth for the Redis queue, the global render lock, and live
progress (DRY: queue name, lock key, and Redis URL are defined ONLY here).

Render concurrency is 1 (ADR-004). It is guaranteed by topology (one worker, one SimpleWorker) and
belt-and-suspenders by `with_render_lock` — a Redis mutex so even a stray second worker can't run a
second render.

High-frequency progress is written to Redis (not SQLite) so the single DB writer stays near-idle.
"""
from __future__ import annotations

import functools
import time
from collections.abc import Callable

import redis
from rq import Queue

from core.config import settings

QUEUE_NAME = "renders"
LOCK_KEY = "render:global-lock"
# Both job kinds share the one queue (uploads stay sequential with renders — ADR-004), so anything
# introspecting the queue must tell them apart by function name.
RENDER_FUNC = "workers.video_worker.render_task"
PUBLISH_FUNC = "workers.video_worker.publish_task"
_PROGRESS_KEY = "task:progress"  # a Redis hash: field=<task_id> value=<pct>
# Companion hash: field=<task_id> value=<unix ts of the last progress CHANGE>. Only a changed value
# refreshes the stamp, so "progress stopped moving" is measurable without polling the render itself.
_PROGRESS_TS_KEY = "task:progress-ts"
RESTART_KEY = "worker:restart-requested"  # operator-requested clean worker exit (Operations page)

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


def enqueue_compile(task_id: int) -> str:
    """Enqueue a best-of compilation build (ADR-082). Same queue as renders — a stream-copy concat
    is cheap, but one ffmpeg at a time is the law of this box either way."""
    job = render_queue.enqueue(
        "workers.video_worker.compile_task",
        task_id,
        job_timeout=1800,   # concat + one thumbnail — generous, nowhere near a render's cap
        result_ttl=3600,
    )
    return job.id


def enqueue_publish(buffer_item_id: int) -> str:
    """Enqueue a publish (upload) job for an approved buffer item. Same queue/worker, so uploads
    stay sequential with renders (KISS on one box); a short upload never blocks for long.

    An hour, not 30 minutes: a 15-minute long-form master on a slow uplink can exceed half an hour,
    and being killed mid-upload is the one failure this box handles worst (the platform may already
    hold a partial video)."""
    job = render_queue.enqueue(
        "workers.video_worker.publish_task",
        buffer_item_id,
        job_timeout=3600,
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
def _text(raw) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)


def set_progress(task_id: int, pct: float) -> None:
    """Publish a render's live percentage. The change-stamp is refreshed ONLY when the value
    actually moves, which is what makes a wedged render detectable (`render_stall_seconds`)."""
    field, value = str(task_id), f"{pct:.1f}"
    prev = conn.hget(_PROGRESS_KEY, field)
    conn.hset(_PROGRESS_KEY, field, value)
    if prev is None or _text(prev) != value:
        conn.hset(_PROGRESS_TS_KEY, field, f"{time.time():.0f}")


def get_progress(task_id: int) -> float:
    raw = conn.hget(_PROGRESS_KEY, str(task_id))
    return float(raw) if raw is not None else 0.0


def clear_progress(task_id: int) -> None:
    conn.hdel(_PROGRESS_KEY, str(task_id))
    conn.hdel(_PROGRESS_TS_KEY, str(task_id))


def clear_all_progress() -> None:
    """Drop every live-progress entry. Called at worker boot: there is exactly one worker and it is
    not rendering yet, so any entry is a crash artifact — and a stale one would otherwise read as a
    permanently stalled render and put the watchdog into a restart loop."""
    conn.delete(_PROGRESS_KEY, _PROGRESS_TS_KEY)


def stall_limit_seconds() -> int:
    """How long a render may show no progress before it is considered wedged. Deliberately LONGER
    than RQ's own job timeout: below that line a slow-but-alive render is RQ's to kill cleanly, so
    only a render that outlived its own timeout — i.e. a worker that stopped executing — trips it."""
    return settings.JOB_TIMEOUT_SECONDS + settings.WORKER_STALL_GRACE_SECONDS


def stalled_render(now: float | None = None) -> tuple[int, float] | None:
    """The in-flight render that has not moved for longer than `stall_limit_seconds`, as
    (task_id, stalled_seconds) — or None when nothing is rendering or everything is progressing.

    Fail-open by construction: a progress entry written before this build carries no change-stamp
    and is skipped rather than treated as stalled (a mid-deploy render is never killed)."""
    try:
        live = conn.hgetall(_PROGRESS_KEY)
        if not live:
            return None
        stamps = conn.hgetall(_PROGRESS_TS_KEY)
        now = time.time() if now is None else now
        limit, worst = stall_limit_seconds(), None
        for field in live:
            stamp = stamps.get(field)
            if stamp is None:
                continue
            age = now - float(_text(stamp))
            if age >= limit and (worst is None or age > worst[1]):
                worst = (int(_text(field)), age)
        return worst
    except Exception:  # noqa: BLE001 — a health probe must never raise
        return None


def worker_healthy() -> bool:
    """Container healthcheck: a worker is registered AND no render is wedged. Reported as
    `(unhealthy)` by Docker; the actual recovery is the in-process watchdog, because a plain
    `restart:` policy reacts to a container EXITING, not to a failing healthcheck."""
    return worker_alive() and stalled_render() is None


def active_render_task_ids() -> set[str]:
    """Task ids with a live progress entry — i.e. a render in flight. The orphan sweeper uses this
    to never delete the workspace of the job that is rendering right now (even under disk pressure)."""
    try:
        return {_text(k) for k in conn.hkeys(_PROGRESS_KEY)}
    except Exception:  # noqa: BLE001 — never let housekeeping raise
        return set()


def worker_alive() -> bool:
    """True if at least one RQ worker is registered (used by the worker healthcheck)."""
    try:
        from rq import Worker

        return len(Worker.all(connection=conn)) > 0
    except Exception:  # noqa: BLE001 — healthcheck must never raise
        return False


# ── Queue introspection + operator controls (Operations page) ────────────────
# Read-only views of what the worker will actually do next, plus the two interventions an operator
# needs when a slot is close: jump the queue, or drop a job. Everything lives here because this
# module owns the queue (DRY) and everything fails soft — the Operations page must render even when
# Redis is unreachable.
def queued_jobs() -> list[dict]:
    """Queued jobs in the exact order the worker will run them:
    [{position, job_id, kind ('render'|'publish'), arg (task/buffer id), enqueued_at}].

    `position` is the true queue position across BOTH kinds, so a render sitting behind a publish
    job is not shown as if it were next."""
    from rq.job import Job

    try:
        ids = render_queue.get_job_ids()
        jobs = Job.fetch_many(ids, connection=conn)
    except Exception:  # noqa: BLE001 — a broken queue read must not break the page
        return []
    out: list[dict] = []
    for pos, job in enumerate(jobs, start=1):
        if job is None:  # expired/vanished job id still listed in the queue
            continue
        kind = {RENDER_FUNC: "render", PUBLISH_FUNC: "publish"}.get(job.func_name)
        if kind is None:
            continue
        out.append({"position": pos, "job_id": job.id, "kind": kind,
                    "arg": job.args[0] if job.args else None,
                    "enqueued_at": job.enqueued_at})
    return out


def move_job_to_front(job_id: str) -> bool:
    """Re-queue an already-queued job at the head. The Job itself is untouched (so `Task.rq_job_id`
    stays valid) — only its place in the queue's id list changes. False if it is no longer queued."""
    try:
        if job_id not in render_queue.get_job_ids():
            return False
        render_queue.remove(job_id)
        render_queue.push_job_id(job_id, at_front=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def cancel_job(job_id: str) -> bool:
    """Drop a queued job so the worker never runs it. Only affects the QUEUE — a job already in
    flight keeps running (RQ cannot interrupt it); the caller owns the Task-row bookkeeping."""
    from rq.job import Job

    try:
        if job_id not in render_queue.get_job_ids():
            return False
        Job.fetch(job_id, connection=conn).cancel()
        return True
    except Exception:  # noqa: BLE001
        return False


def worker_snapshot() -> dict | None:
    """Live state of the single RQ worker, or None when none is registered. Every field is
    best-effort: an older/partial registration must still render a card."""
    try:
        from rq import Worker

        workers = Worker.all(connection=conn)
    except Exception:  # noqa: BLE001
        return None
    if not workers:
        return None
    w = workers[0]  # render concurrency is 1 by topology — there is only ever one (ADR-004)
    snap = {"name": None, "state": None, "current_job_id": None, "last_heartbeat": None,
            "birth_date": None, "successful": None, "failed": None}
    for key, attr in (("name", "name"), ("last_heartbeat", "last_heartbeat"),
                      ("birth_date", "birth_date"), ("successful", "successful_job_count"),
                      ("failed", "failed_job_count")):
        snap[key] = getattr(w, attr, None)
    try:
        state = w.get_state()
        snap["state"] = state if state in ("busy", "idle", "suspended", "started") else None
        snap["current_job_id"] = w.get_current_job_id()
    except Exception:  # noqa: BLE001
        pass
    return snap


def render_lock_held() -> bool:
    try:
        return conn.get(LOCK_KEY) is not None
    except Exception:  # noqa: BLE001
        return False


def progress_age_seconds(task_id: int, now: float | None = None) -> float | None:
    """Seconds since this render's progress last MOVED (None when it has no change-stamp). Unlike
    `stalled_render` this reports the age whatever it is, so the UI can show a rising number long
    before the watchdog's limit is reached."""
    try:
        stamp = conn.hget(_PROGRESS_TS_KEY, str(task_id))
        if stamp is None:
            return None
        return (time.time() if now is None else now) - float(_text(stamp))
    except Exception:  # noqa: BLE001
        return None


# ── Operator-requested restart (no Docker socket) ────────────────────────────
# The web container must never reach the Docker daemon (it is the internet-facing service), so a
# "Restart worker" click only raises this flag. The worker's own watchdog thread sees it and exits;
# compose's `restart: unless-stopped` then recreates the container. The TTL means a flag set while
# the worker is down cannot silently kill a healthy worker much later.
def request_worker_restart(ttl_seconds: int = 300) -> None:
    conn.set(RESTART_KEY, "1", ex=ttl_seconds)


def restart_requested() -> bool:
    try:
        return conn.get(RESTART_KEY) is not None
    except Exception:  # noqa: BLE001 — never let the watchdog raise
        return False


def clear_restart_request() -> None:
    try:
        conn.delete(RESTART_KEY)
    except Exception:  # noqa: BLE001
        pass
