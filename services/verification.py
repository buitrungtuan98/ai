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


def verify_pollinations(token: str | None = None) -> tuple[bool, str]:
    """Draw one tiny image — proves Pollinations is reachable (and, if a token is set, that it's
    accepted). The keyless anonymous tier is valid too, so no token is not a failure."""
    try:
        import requests

        params: dict = {"model": "flux", "width": 256, "height": 256, "safe": "true"}
        # Backend auth is the Authorization: Bearer header (a `token` query param is not documented
        # and gets ignored — the request would silently test the anonymous tier instead of the key).
        # gen.pollinations.ai/image is the current API endpoint (same one the render path calls).
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = requests.get("https://gen.pollinations.ai/image/a%20tiny%20test%20icon",
                            params=params, headers=headers, timeout=90)
        ctype = resp.headers.get("content-type", "")
        if resp.status_code == 200 and ctype.startswith("image/") and resp.content:
            tier = "token accepted" if token else "keyless anonymous tier"
            return True, f"Pollinations is reachable ({tier}) — Studio Mode can draw for free."
        return False, f"Pollinations did not return an image (HTTP {resp.status_code}, {ctype!r})."
    except Exception as exc:  # noqa: BLE001 — the exception text can embed the token; never expose it
        logger.warning("Pollinations verification network error: %s", type(exc).__name__)
        return False, "Could not reach Pollinations (network error)."


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


def check_facebook_page(page_id: str, token: str) -> tuple[bool | None, str]:
    """Does this Page id + token actually work? THREE-state on purpose (ADR-068):
    `True` verified · `False` definitely rejected · `None` could not tell.

    The other checks here answer a Test button, where "couldn't reach it" may as well be a failure.
    This one gates a save, so the distinction matters: a made-up token must be refused, but a network
    hiccup must never stop a real operator from connecting their Page. Never raises.
    """
    try:
        import requests

        resp = requests.get(
            f"https://graph.facebook.com/v20.0/{page_id}",
            params={"fields": "id,name", "access_token": token}, timeout=TIMEOUT)
        if resp.status_code == 200 and (resp.json() or {}).get("id"):
            return True, f"Verified: {(resp.json() or {}).get('name') or page_id}."
        # Graph answers a bad token or a wrong Page id with 400/403/404 and an explanation.
        if resp.status_code in (400, 401, 403, 404):
            detail = ""
            try:
                detail = ((resp.json() or {}).get("error") or {}).get("message") or ""
            except ValueError:
                pass
            return False, detail or f"Facebook rejected these details (HTTP {resp.status_code})."
        return None, f"Facebook answered HTTP {resp.status_code} — could not verify right now."
    except Exception as exc:  # noqa: BLE001 — the URL carries the token; never surface the raw text
        logger.warning("Facebook page verification network error: %s", type(exc).__name__)
        return None, "Could not reach Facebook to verify — saved without checking."
