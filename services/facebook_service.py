"""Facebook Page video publishing.

A channel stores its Page id + permanent Page Access Token (JSON) in `channel.encrypted_credentials`
(decrypted on read). Page tokens are long-lived, so no refresh flow is needed — we upload directly to
the Graph API video endpoint.

This module also owns the Graph constants and the "what did Facebook actually say" error handling for
the WHOLE Facebook surface — verification, publishing and analytics all import them — so the version
and the error semantics exist exactly once (ADR-072).
"""
from __future__ import annotations

import json
import logging
import re

from database.models import Channel

logger = logging.getLogger(__name__)

# ONE Graph version for the whole app. It was hardcoded in four places: the same copy-the-list
# pattern that hid the `vintage` colour grade for a release (ADR-071). Meta keeps a version alive
# ~2 years, so when this one retires every call site moves together.
GRAPH_VERSION = "v23.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
GRAPH_VIDEO = f"https://graph-video.facebook.com/{GRAPH_VERSION}"

# Graph's OAuth failure codes — the token is dead, revoked, or was never valid. Distinct from every
# other error because no retry can fix it: the operator has to paste a new one (ADR-072).
_AUTH_ERROR_CODES = {102, 190, 463, 467}
_AUTH_ERROR_TYPES = {"OAuthException"}

_TOKEN_IN_URL = re.compile(r"access_token=[^&\s\"']+")


class FacebookError(RuntimeError):
    """A Graph call failed, carrying Facebook's own explanation instead of '400 Bad Request'."""

    def __init__(self, message: str, *, code: int | None = None, subcode: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode


class FacebookAuthError(FacebookError):
    """The Page Access Token is invalid, expired or revoked. Retrying cannot help — callers mark the
    channel `expired` so the UI stops claiming it works."""


def scrub(text: str, token: str | None = None) -> str:
    """Strip an access token out of any string before it is logged, stored or shown.

    Graph carries the token in the query string, so a raw `requests` exception message embeds the
    whole credential — and a render error is stored on the Task and rendered in the UI and the alert
    bell. Both the known token and any `access_token=…` are removed."""
    out = text or ""
    if token:
        out = out.replace(token, "***")
    return _TOKEN_IN_URL.sub("access_token=***", out)


def raise_for_graph(resp, *, token: str | None = None, what: str = "Facebook") -> None:
    """Turn a failed Graph response into a readable, token-free error; return quietly on success.

    `resp.raise_for_status()` produced "400 Client Error: Bad Request for url: …" — unreadable, AND
    it leaked the token through the URL, while discarding the one useful thing in the body:
    Facebook's own `error.message` ("Error validating access token: Session has expired…")."""
    if resp.status_code < 400:
        return
    message, code, subcode, etype = "", None, None, ""
    try:
        err = (resp.json() or {}).get("error") or {}
        message = err.get("message") or ""
        code = err.get("code")
        subcode = err.get("error_subcode")
        etype = err.get("type") or ""
    except ValueError:                      # an HTML error page, not JSON
        message = (resp.text or "")[:200]
    detail = scrub(message or f"HTTP {resp.status_code}", token)
    is_auth = code in _AUTH_ERROR_CODES or etype in _AUTH_ERROR_TYPES
    if is_auth:
        # The words matter: `core.failure` classifies from this text, and "OAuth error" is what tells
        # the episode page, the bell and the autopilot that no retry can fix it (ADR-072).
        raise FacebookAuthError(f"{what}: OAuth error {code or ''} — {detail}".replace("  ", " "),
                                code=code, subcode=subcode)
    raise FacebookError(f"{what}: {detail}", code=code, subcode=subcode)


def _load(channel: Channel) -> tuple[str, str]:
    data = json.loads(channel.encrypted_credentials or "{}")
    page_id = data.get("page_id")
    token = data.get("page_access_token")
    if not (page_id and token):
        raise RuntimeError(f"Channel {channel.id} is missing page_id/page_access_token.")
    return page_id, token


def upload_video(channel: Channel, video_path: str, metadata: dict) -> str:
    """Upload a video to the Page feed and return the Facebook video id."""
    import requests

    page_id, token = _load(channel)
    url = f"{GRAPH_VIDEO}/{page_id}/videos"
    description = metadata.get("description", "")
    title = metadata.get("title", "")
    try:
        with open(video_path, "rb") as fh:
            resp = requests.post(
                url,
                data={"title": title, "description": description, "access_token": token},
                files={"source": fh},
                timeout=600,
            )
    except requests.RequestException as exc:
        raise FacebookError(f"Facebook upload failed: {scrub(str(exc), token)}") from None
    raise_for_graph(resp, token=token, what="Facebook upload")
    video_id = resp.json().get("id", "")
    logger.info("Uploaded Facebook video %s to page %s", video_id, page_id)
    return video_id
