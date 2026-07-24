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
    min_short_side: int = 1080,
) -> list[PexelsClip]:
    """Search Pexels videos, choosing the rendition that matches the output orientation at (but not
    wastefully above) the target resolution. `min_short_side` is the output's shorter edge (1080 for
    both 1080×1920 shorts and 1920×1080 long-form). Clips whose best rendition can't reach that floor
    sort last — they'd upscale to visibly soft footage."""
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
        # A missing/zero duration is unusable for coverage math (it would make plan_shots cycle
        # up to its safety valve and build a pathological command). Skip such clips.
        duration = float(video.get("duration", 0) or 0)
        if duration <= 0:
            continue
        best = _best_file(files, orientation, min_short_side)
        clips.append(
            PexelsClip(
                id=video["id"],
                duration=duration,
                width=best.get("width") or 0,
                height=best.get("height") or 0,
                download_url=best["link"],
            )
        )
    # Resolution floor: below-target clips drop to the back (stable, so keyword relevance is kept).
    clips.sort(key=lambda c: min(c.width, c.height) < min_short_side)
    return clips


def _best_file(files: list[dict], orientation: str = "portrait", min_short_side: int = 1080) -> dict:
    """Pick the rendition to download: matching the requested orientation, and the SMALLEST one that
    still clears the resolution floor (sharp without a wasteful 4K download that burns ARM decode
    CPU). If none clears the floor, fall back to the largest available."""
    def short_side(f: dict) -> int:
        return min(f.get("width") or 0, f.get("height") or 0)

    def area(f: dict) -> int:
        return (f.get("width") or 0) * (f.get("height") or 0)

    def matches(f: dict) -> bool:
        w, h = f.get("width") or 0, f.get("height") or 0
        return h >= w if orientation == "portrait" else w >= h

    pool = [f for f in files if matches(f)] or files
    meeting = [f for f in pool if short_side(f) >= min_short_side]
    return min(meeting, key=area) if meeting else max(pool, key=area)


def download(url: str, out_path: str) -> str:
    import requests

    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return out_path
