"""Automatic background-music selection — zero manual work, zero licensing risk.

Searches Freesound.org (free API) filtered to **CC0 / public-domain** tracks only, so every track
is safe for commercial/monetized videos with no attribution required. A random track from the most
popular matches is chosen per episode (variety within the campaign's mood), downloaded once into a
local cache, and mixed under the narration by the existing music path in the renderer.

Best-effort by design: any failure returns None and the episode renders without music — a missing
music bed must never fail a video.
"""
from __future__ import annotations

import logging
import os
import random

logger = logging.getLogger(__name__)

FREESOUND_SEARCH_URL = "https://freesound.org/apiv2/search/text/"
CC0_FILTER = 'license:"Creative Commons 0" duration:[60 TO 600]'
TOP_POOL = 20  # random pick among the N most-downloaded matches


def pick_music(mood: str, api_key: str, cache_dir: str) -> tuple[str, dict] | None:
    """Return (local mp3 path, credit dict) for a random CC0 track matching `mood`, or None."""
    import requests

    try:
        resp = requests.get(
            FREESOUND_SEARCH_URL,
            params={
                "query": mood or "ambient background",
                "filter": CC0_FILTER,
                "fields": "id,name,username,previews,duration,num_downloads",
                "sort": "downloads_desc",
                "page_size": 30,
                "token": api_key,
            },
            timeout=30,
        )
        resp.raise_for_status()
        results = [
            r for r in resp.json().get("results", [])
            if (r.get("previews") or {}).get("preview-hq-mp3")
        ]
        if not results:
            logger.warning("Auto-music: no CC0 results for mood %r", mood)
            return None

        track = random.choice(results[:TOP_POOL])
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"freesound_{track['id']}.mp3")
        if not os.path.exists(path):
            with requests.get(track["previews"]["preview-hq-mp3"], stream=True, timeout=120) as dl:
                dl.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in dl.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
        credit = {
            "source": "freesound",
            "id": track["id"],
            "title": track.get("name"),
            "author": track.get("username"),
            "license": "CC0",
        }
        logger.info("Auto-music: %r by %s (freesound #%s)", credit["title"], credit["author"], track["id"])
        return path, credit
    except Exception:  # noqa: BLE001 — music is an enhancement, never a blocker
        logger.warning("Auto-music selection failed — rendering without music", exc_info=True)
        return None
