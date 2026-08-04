"""Facebook Page publishing — Reels for vertical shorts, Page video for long-form.

A channel stores its Page id + permanent Page Access Token (JSON) in `channel.encrypted_credentials`
(decrypted on read). Page tokens are long-lived, so no refresh flow is needed.

This module also owns the Graph constants and the "what did Facebook actually say" error handling for
the WHOLE Facebook surface — verification, publishing and analytics all import them — so the version
and the error semantics exist exactly once (ADR-072).

The publish path deliberately mirrors `youtube_service` feature for feature (ADR-073): the campaign's
privacy choice is honoured, the CTA is posted as a comment, and the upload is chunked and resumable.
Where the two platforms genuinely differ the difference is named in a comment, not left implicit.
"""
from __future__ import annotations

import json
import logging
import os
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
# Codes that really mean "this token is dead": 102 session key invalid, 190 access token invalid/
# expired (the canonical one), 463/467 expired/invalidated. ONLY these retire a channel.
_AUTH_ERROR_CODES = {102, 190, 463, 467}
# Codes that mean "not now", not "not this token": 1/2 temporary API errors, 4 app rate limit,
# 17 user rate limit, 32 PAGE rate limit (a small Page's insights quota is tiny — the hourly stats
# pass can trip this on a perfectly healthy token), 341/613 call-rate limits, 368 temporary block.
# Graph stamps `"type": "OAuthException"` on MOST of these too, which is exactly why the type field
# must never be used to declare a token dead (ADR-083).
_TRANSIENT_ERROR_CODES = {1, 2, 4, 17, 32, 341, 368, 613}

_TOKEN_IN_URL = re.compile(r"access_token=[^&\s\"']+")

_TRANSFER_TIMEOUT = 600      # the bytes themselves
_CALL_TIMEOUT = 60           # the small start/finish/comment calls


class FacebookError(RuntimeError):
    """A Graph call failed, carrying Facebook's own explanation instead of '400 Bad Request'.
    `transient` marks rate limits and temporary API errors — failures that heal by waiting."""

    def __init__(self, message: str, *, code: int | None = None, subcode: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.transient = code in _TRANSIENT_ERROR_CODES


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
    message, code, subcode = "", None, None
    try:
        err = (resp.json() or {}).get("error") or {}
        message = err.get("message") or ""
        code = err.get("code")
        subcode = err.get("error_subcode")
    except ValueError:                      # an HTML error page, not JSON
        message = (resp.text or "")[:200]
    detail = scrub(message or f"HTTP {resp.status_code}", token)
    # Auth is decided by CODE alone, never by `type` (ADR-083). Graph stamps "OAuthException" on
    # rate limits and temporary errors too, and for months that blanket rule executed healthy
    # channels: a small Page tripping its insights quota (code 32, type OAuthException) read as a
    # dead token, the channel was retired hourly, and the operator's re-pasted — identical — token
    # "fixed" it every time.
    if code in _AUTH_ERROR_CODES:
        # The words matter: `core.failure` classifies from this text, and "OAuth error" is what tells
        # the episode page, the bell and the autopilot that no retry can fix it (ADR-072).
        raise FacebookAuthError(f"{what}: OAuth error {code or ''} — {detail}".replace("  ", " "),
                                code=code, subcode=subcode)
    if code in _TRANSIENT_ERROR_CODES:
        raise FacebookError(f"{what}: temporarily unavailable (rate limit / transient, "
                            f"code {code}) — {detail}", code=code, subcode=subcode)
    raise FacebookError(f"{what}: {detail}", code=code, subcode=subcode)


def token_definitely_dead(channel: Channel) -> bool:
    """True only when a fresh /me verification DEFINITELY rejects the stored token (ADR-083).

    The one question both retirement sites must ask before condemning a channel — it encodes what
    the operator does by hand when they re-paste the same token "and it works". A check that cannot
    run or cannot tell returns False: under-retiring costs one more failed publish; falsely retiring
    costs the operator a daily token-pasting ritual. Never raises."""
    try:
        from services import verification  # function-level: verification imports this module

        page_id, token = _load(channel)
        return verification.check_facebook_page(page_id, token).ok is False
    except Exception:  # noqa: BLE001 — no verdict = not definitely dead
        logger.warning("Token re-verification failed for channel %s", channel.id, exc_info=True)
        return False


def _load(channel: Channel) -> tuple[str, str]:
    data = json.loads(channel.encrypted_credentials or "{}")
    page_id = data.get("page_id")
    token = data.get("page_access_token")
    if not (page_id and token):
        raise RuntimeError(f"Channel {channel.id} is missing page_id/page_access_token.")
    return page_id, token


# ── What the operator's choices mean on Facebook ─────────────────────────────
def is_reel(metadata: dict) -> bool:
    """A vertical short is published as a REEL (ADR-073).

    Posting a 9:16 clip to `/{page_id}/videos` produces an ordinary Page video: it never enters Reels
    distribution, which is the entire reason this product renders vertical video. Long-form 16:9 stays
    a Page video, where it belongs."""
    return (metadata.get("video_format") or "short") != "long"


def wants_public(metadata: dict) -> bool:
    """Facebook has no 'unlisted'. The honest mapping of the campaign's privacy choice is therefore
    public → live, anything else → not publicly visible (a Reel DRAFT / an unpublished video).

    This was simply ignored before: a campaign set to `private` published PUBLICLY to the Page while
    the same campaign on YouTube stayed private (ADR-073)."""
    return (metadata.get("privacy") or "public") == "public"


def permalink(video_id: str, *, reel: bool = True) -> str:
    """The canonical, resolvable URL of a published video.

    `https://www.facebook.com/{video_id}` — what this used to build — is not a video permalink, so
    every "View ↗" link the app showed for a Facebook publish led nowhere (ADR-073)."""
    return (f"https://www.facebook.com/reel/{video_id}" if reel
            else f"https://www.facebook.com/watch/?v={video_id}")


# ── Idempotency: never post the same episode twice ───────────────────────────
def find_existing_upload(channel: Channel, *, video_id: str | None = None,
                         title: str = "") -> str | None:
    """Did a previous attempt already put this episode on the Page? (ADR-073)

    An upload that succeeds server-side but times out client-side looks exactly like a failure here,
    so the retry used to post the video a second time. The Reels flow hands us the video id BEFORE the
    bytes go up, which makes this exact; for a Page video there is no pre-id, so we fall back to
    looking for our own title among the Page's most recent uploads. Best-effort: any doubt returns
    None and the upload proceeds (a duplicate is bad, a missing episode is worse)."""
    import requests

    page_id, token = _load(channel)
    try:
        if video_id:
            resp = requests.get(f"{GRAPH}/{video_id}",
                                params={"fields": "id,status{video_status,uploading_phase}",
                                        "access_token": token},
                                timeout=_CALL_TIMEOUT)
            body = (resp.json() or {}) if resp.status_code == 200 else {}
            if not body.get("id"):
                return None
            # The id EXISTS the moment `start` reserves it — BEFORE any byte goes up (ADR-085).
            # Adopting on existence alone marked an episode "published" pointing at an empty
            # draft whenever the transfer phase died: the exact failure this guard exists for.
            # Only adopt when Facebook says the bytes actually landed.
            st = body.get("status") or {}
            uploaded = str((st.get("uploading_phase") or {}).get("status") or "").lower()
            video_status = str(st.get("video_status") or "").lower()
            if uploaded == "complete" or video_status == "ready":
                return str(body["id"])
            logger.warning("Facebook id %s is reserved but its bytes never landed "
                           "(video_status=%r, uploading=%r) — uploading again", video_id,
                           video_status, uploaded)
            return None
        if not title:
            return None
        resp = requests.get(f"{GRAPH}/{page_id}/videos",
                            params={"fields": "id,title,description", "limit": 10,
                                    "access_token": token}, timeout=_CALL_TIMEOUT)
        if resp.status_code != 200:
            return None
        for row in (resp.json() or {}).get("data", []):
            if (row.get("title") or "").strip() == title.strip():
                return str(row.get("id"))
    except Exception as exc:  # noqa: BLE001 — a guard that raises would block a legitimate retry
        logger.warning("Could not check for an existing Facebook upload: %s", scrub(str(exc)))
    return None


# ── Publishing ───────────────────────────────────────────────────────────────
def _post_comment(page_id: str, token: str, video_id: str, text: str) -> None:
    """Post the campaign's CTA (and its affiliate line) under the video.

    YouTube has always done this; Facebook silently dropped it, so every CTA and every affiliate link
    an operator configured was simply missing from half their channels (ADR-073). Best-effort — a
    failed comment must never fail a published video."""
    import requests

    try:
        resp = requests.post(f"{GRAPH}/{video_id}/comments",
                             data={"message": text, "access_token": token}, timeout=_CALL_TIMEOUT)
        raise_for_graph(resp, token=token, what="Facebook comment")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to post the CTA comment on %s: %s", video_id, scrub(str(exc), token))


def _upload_reel(page_id: str, token: str, video_path: str, metadata: dict,
                 on_pending=None) -> str:
    """Publish a Reel with Meta's three-phase upload (start → transfer → finish).

    The phases are not ceremony: `start` reserves the video id (which is what makes a retry safe), and
    `transfer` is a plain byte stream with an `offset` header, so a large file is resumable instead of
    one 600-second all-or-nothing POST like the old code used (ADR-073)."""
    import requests

    start = requests.post(f"{GRAPH}/{page_id}/video_reels",
                          data={"upload_phase": "start", "access_token": token},
                          timeout=_CALL_TIMEOUT)
    raise_for_graph(start, token=token, what="Facebook Reel start")
    body = start.json() or {}
    video_id, upload_url = str(body.get("video_id") or ""), body.get("upload_url") or ""
    if not (video_id and upload_url):
        raise FacebookError("Facebook Reel start: no video_id/upload_url in the response")
    if on_pending:
        # Persisted BEFORE a single byte goes up, so a retry can ask "did this one already land?"
        on_pending(video_id)

    size = os.path.getsize(video_path)
    with open(video_path, "rb") as fh:
        transfer = requests.post(
            upload_url,
            headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
            data=fh, timeout=_TRANSFER_TIMEOUT)
    raise_for_graph(transfer, token=token, what="Facebook Reel upload")

    finish = requests.post(
        f"{GRAPH}/{page_id}/video_reels",
        data={"upload_phase": "finish", "video_id": video_id,
              # DRAFT is Facebook's nearest thing to "not public": there is no unlisted Reel.
              "video_state": "PUBLISHED" if wants_public(metadata) else "DRAFT",
              "description": metadata.get("description", ""),
              "access_token": token},
        timeout=_CALL_TIMEOUT)
    raise_for_graph(finish, token=token, what="Facebook Reel finish")
    return video_id


def _upload_page_video(page_id: str, token: str, video_path: str, metadata: dict) -> str:
    """Publish long-form 16:9 as an ordinary Page video."""
    import requests

    public = wants_public(metadata)
    data = {"title": metadata.get("title", ""), "description": metadata.get("description", ""),
            "access_token": token, "published": "true" if public else "false"}
    if not public:
        data["unpublished_content_type"] = "DRAFT"
    with open(video_path, "rb") as fh:
        resp = requests.post(f"{GRAPH_VIDEO}/{page_id}/videos", data=data,
                             files={"source": fh}, timeout=_TRANSFER_TIMEOUT)
    raise_for_graph(resp, token=token, what="Facebook upload")
    return str((resp.json() or {}).get("id") or "")


def upload_video(channel: Channel, video_path: str, metadata: dict,
                 *, pending_video_id: str | None = None, on_pending=None) -> str:
    """Publish a video to the Page and return its Facebook id.

    Vertical shorts go up as Reels, long-form as a Page video; the campaign's privacy choice is
    honoured either way and the CTA is posted as a comment (ADR-073). `pending_video_id`/`on_pending`
    are the retry guard: a previous attempt's id is checked before anything is uploaded again."""
    import requests

    page_id, token = _load(channel)
    reel = is_reel(metadata)

    already = find_existing_upload(channel, video_id=pending_video_id,
                                   title=metadata.get("title", "") if not reel else "")
    if already:
        logger.warning("Facebook: episode already uploaded as %s — adopting it instead of "
                       "posting a duplicate", already)
        return already

    try:
        video_id = (_upload_reel(page_id, token, video_path, metadata, on_pending) if reel
                    else _upload_page_video(page_id, token, video_path, metadata))
    except requests.RequestException as exc:
        raise FacebookError(f"Facebook upload failed: {scrub(str(exc), token)}") from None
    if not video_id:
        raise FacebookError("Facebook accepted the upload but returned no video id")

    logger.info("Published Facebook %s %s to page %s", "reel" if reel else "video", video_id, page_id)
    cta = metadata.get("cta")
    if cta:
        _post_comment(page_id, token, video_id, cta)
    return video_id
