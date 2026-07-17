"""Pexels stock-footage client (search + download). Thin wrapper; `requests` imported lazily.

Pexels footage is free to use under the Pexels license. `safety_filter.assert_licensed_footage`
guards that we only source from here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.pexels.com/videos/search"


@dataclass
class PexelsClip:
    id: int
    duration: float
    width: int
    height: int
    download_url: str


def search_videos(
    query: str,
    api_key: str,
    *,
    per_page: int = 10,
    orientation: str = "portrait",
) -> list[PexelsClip]:
    """Search Pexels videos, preferring vertical renditions closest to 1080x1920."""
    import requests

    resp = requests.get(
        _SEARCH_URL,
        headers={"Authorization": api_key},
        params={"query": query, "per_page": per_page, "orientation": orientation},
        timeout=30,
    )
    resp.raise_for_status()
    clips: list[PexelsClip] = []
    for video in resp.json().get("videos", []):
        files = video.get("video_files", [])
        if not files:
            continue
        # Prefer a portrait rendition >= 1080 wide; else the largest available.
        best = _best_file(files)
        clips.append(
            PexelsClip(
                id=video["id"],
                duration=float(video.get("duration", 0)),
                width=best.get("width") or 0,
                height=best.get("height") or 0,
                download_url=best["link"],
            )
        )
    return clips


def _best_file(files: list[dict]) -> dict:
    portrait = [f for f in files if (f.get("height") or 0) >= (f.get("width") or 0)]
    pool = portrait or files
    return max(pool, key=lambda f: (f.get("width") or 0) * (f.get("height") or 0))


def download(url: str, out_path: str) -> str:
    import requests

    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return out_path
