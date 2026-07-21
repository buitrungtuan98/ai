"""Credential verification — one cheap live call per provider so a wrong key is caught at save
time, not at 2am when a render fails. Each returns (ok, detail) and never raises."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

TIMEOUT = 15


def verify_gemini(api_key: str) -> tuple[bool, str]:
    try:
        import requests

        resp = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": api_key, "pageSize": 1},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            return True, "Gemini key is valid."
        return False, f"Gemini rejected the key (HTTP {resp.status_code})."
    except Exception as exc:  # noqa: BLE001 — the exception text embeds the URL (?key=…); never expose it
        logger.warning("Gemini verification network error: %s", type(exc).__name__)
        return False, "Could not reach Gemini (network error)."


def verify_pexels(api_key: str) -> tuple[bool, str]:
    try:
        import requests

        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": api_key},
            params={"query": "nature", "per_page": 1},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            return True, "Pexels key is valid."
        return False, f"Pexels rejected the key (HTTP {resp.status_code})."
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pexels verification network error: %s", type(exc).__name__)
        return False, "Could not reach Pexels (network error)."


def verify_freesound(api_key: str) -> tuple[bool, str]:
    """One tiny search — proves the key works AND that CC0 results come back (auto music path)."""
    try:
        import requests

        resp = requests.get(
            "https://freesound.org/apiv2/search/text/",
            params={"query": "ambient", "filter": 'license:"Creative Commons 0"',
                    "page_size": 1, "fields": "id", "token": api_key},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            return True, "Freesound key is valid — Auto background music will work."
        return False, f"Freesound rejected the key (HTTP {resp.status_code})."
    except Exception as exc:  # noqa: BLE001 — the exception text embeds the URL (?token=…); never expose it
        logger.warning("Freesound verification network error: %s", type(exc).__name__)
        return False, "Could not reach Freesound (network error)."


def verify_telegram(token: str, chat_id: str | None = None) -> tuple[bool, str]:
    try:
        import requests

        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=TIMEOUT)
        if resp.status_code != 200 or not resp.json().get("ok"):
            return False, "Telegram rejected the bot token."
        bot = resp.json()["result"].get("username", "bot")
        if chat_id:
            msg = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": "✅ AI Video Factory: Telegram alerts are working."},
                timeout=TIMEOUT,
            )
            if msg.status_code != 200:
                return False, f"Token OK (@{bot}) but sending to chat {chat_id} failed — check the chat ID."
            return True, f"Token OK (@{bot}) — test message sent."
        return True, f"Token OK (@{bot}). Add a chat ID to test delivery."
    except Exception as exc:  # noqa: BLE001 — the exception text embeds the URL (/bot<token>/); never expose it
        logger.warning("Telegram verification network error: %s", type(exc).__name__)
        return False, "Could not reach Telegram (network error)."
