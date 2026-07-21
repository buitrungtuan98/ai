"""Daily AI-call usage counter — powers the dashboard quota meter and the heartbeat digest.

Counts every Gemini API call attempt in Redis, keyed by the **US-Pacific calendar day** so the
meter aligns with Google's free-tier quota window (which resets ~midnight Pacific). It is an
estimate (a rejected call may not consume Google-side quota), but it makes the factory's #1 silent
failure mode — quota exhaustion — visible *before* renders start failing.

Fail-silent by design: a Redis hiccup must never break a generation call.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # Google free-tier quota resets on this clock
_KEY_PREFIX = "ai:calls:"
_KEY_TTL_SECONDS = 3 * 86400  # keep a couple of days for the heartbeat, then let Redis expire


def _today_key() -> str:
    return _KEY_PREFIX + datetime.now(_QUOTA_TZ).strftime("%Y-%m-%d")


def record_ai_call(n: int = 1) -> None:
    """Count `n` Gemini API call attempts against today's (Pacific) bucket."""
    try:
        from workers.task_queue import conn  # single Redis connection source (DRY)

        pipe = conn.pipeline()
        key = _today_key()
        pipe.incrby(key, n)
        pipe.expire(key, _KEY_TTL_SECONDS)
        pipe.execute()
    except Exception:  # noqa: BLE001 — metering must never break generation
        logger.debug("record_ai_call failed (Redis unavailable?)", exc_info=True)


def ai_calls_today() -> int:
    """AI call attempts so far in the current Pacific quota day (0 if Redis is unavailable)."""
    try:
        from workers.task_queue import conn

        raw = conn.get(_today_key())
        return int(raw) if raw else 0
    except Exception:  # noqa: BLE001
        return 0
