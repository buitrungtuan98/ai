"""Telegram alerts — a single DRY helper used for queued/finished/failed notifications."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def send(token: str, chat_id: str, message: str) -> bool:
    """Send a message to a Telegram chat. Returns True on success; never raises (alerts are
    best-effort and must not fail the calling job)."""
    import requests

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Telegram send failed")
        return False
