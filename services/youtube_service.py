"""YouTube publishing with proactive OAuth2 token refresh and multi-account routing.

Each channel stores its OAuth token bundle (JSON) in `channel.encrypted_credentials` (decrypted on
read by the EncryptedString type). Before uploading we check the access token; if expired we refresh
it with the stored `refresh_token` + the app's client id/secret and write the fresh token back onto
the channel (the worker's next commit persists it). Google client libraries are imported lazily so
this module (and its tests) don't require them unless an upload actually runs.
"""
from __future__ import annotations

import json
import logging

from core.config import settings
from database.models import Channel, User

logger = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _load_creds_dict(channel: Channel) -> dict:
    return json.loads(channel.encrypted_credentials or "{}")


def _parse_expiry(value: str | None):
    """Stored token_expiry (naive UTC isoformat) → datetime, or None."""
    if not value:
        return None
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=None)  # google-auth compares against a naive UTC datetime


def build_credentials(channel: Channel):
    """Build google Credentials, refreshing (and persisting) if the access token is expired."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    data = _load_creds_dict(channel)
    creds = Credentials(
        token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", _TOKEN_URI),
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        # scopes=None on refresh preserves whatever scopes the channel actually authorized. Passing
        # a fixed subset would DOWNSCOPE the refreshed token (dropping yt-analytics.readonly and
        # silently breaking the stats/self-improvement loop) — and would break channels connected
        # before a scope was added. The stored expiry lets `creds.valid` reflect reality, so the
        # proactive refresh-and-persist branch below actually runs.
        scopes=None,
    )
    creds.expiry = _parse_expiry(data.get("token_expiry"))
    if not creds.valid:
        if not creds.refresh_token:
            raise RuntimeError(f"Channel {channel.id} has no refresh_token; reconnect the account.")
        creds.refresh(Request())
        # Persist the refreshed token back onto the channel (worker commits later).
        data["access_token"] = creds.token
        if creds.expiry:
            data["token_expiry"] = creds.expiry.isoformat()
        channel.encrypted_credentials = json.dumps(data)
        logger.info("Refreshed YouTube access token for channel %s", channel.id)
    return creds


def upload_video(channel: Channel, video_path: str, metadata: dict, user: User | None = None) -> str:
    """Upload a video (resumable) to the channel and return the new video id. Posts the CTA as a
    top-level comment if provided (YouTube's API cannot pin comments programmatically)."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = build_credentials(channel)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title": metadata.get("title", "")[:100],
            "description": metadata.get("description", ""),
            "tags": metadata.get("tags", []),
            "categoryId": str(metadata.get("category_id", "22")),
        },
        "status": {
            "privacyStatus": metadata.get("privacy", "public"),
            "selfDeclaredMadeForKids": False,
        },
    }
    # Declare the spoken + metadata language (BCP-47) — the clearest signal to YouTube's classifier
    # of which audience this video targets, so it seeds the right country (ADR-045).
    lang = metadata.get("language")
    if lang in ("en", "vi", "es"):
        body["snippet"]["defaultLanguage"] = lang
        body["snippet"]["defaultAudioLanguage"] = lang
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _status, response = request.next_chunk()
    video_id = response["id"]
    logger.info("Uploaded video %s to channel %s", video_id, channel.id)

    cta = metadata.get("cta")
    if cta:
        _post_comment(youtube, video_id, cta)
    return video_id


def _post_comment(youtube, video_id: str, text: str) -> None:
    try:
        youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {"snippet": {"textOriginal": text}},
                }
            },
        ).execute()
    except Exception:  # noqa: BLE001 — a failed CTA comment must not fail the upload
        logger.warning("Failed to post CTA comment on %s", video_id)
