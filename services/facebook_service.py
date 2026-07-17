"""Facebook Page video publishing.

A channel stores its Page id + permanent Page Access Token (JSON) in `channel.encrypted_credentials`
(decrypted on read). Page tokens are long-lived, so no refresh flow is needed — we upload directly to
the Graph API video endpoint.
"""
from __future__ import annotations

import json
import logging

from database.models import Channel

logger = logging.getLogger(__name__)

_GRAPH_VERSION = "v20.0"


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
    url = f"https://graph-video.facebook.com/{_GRAPH_VERSION}/{page_id}/videos"
    description = metadata.get("description", "")
    title = metadata.get("title", "")
    with open(video_path, "rb") as fh:
        resp = requests.post(
            url,
            data={"title": title, "description": description, "access_token": token},
            files={"source": fh},
            timeout=600,
        )
    resp.raise_for_status()
    video_id = resp.json().get("id", "")
    logger.info("Uploaded Facebook video %s to page %s", video_id, page_id)
    return video_id
