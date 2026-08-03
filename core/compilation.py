"""Best-of compilations (ADR-082) — turn already-rendered Shorts into the format that actually pays.

Shorts RPM is cents; long-form RPM is 10-30× that, and a video past eight minutes carries mid-roll
ads. This module builds that video out of work the box has ALREADY done: published masters are
retained in a per-campaign library instead of being deleted at publish, and a compilation is a
stream-copy concat of the top-retention episodes — near-zero CPU, zero AI calls, chapters included
(YouTube turns "0:00 Title" description lines into a chapter list).

Honest bounds:
  * Only episodes published AFTER this shipped are in the library — old masters are gone.
  * The library is capped per campaign (oldest beyond the cap deleted) so disk stays bounded.
  * A compilation ALWAYS parks for review, even in full-auto — a 10-minute video that will anchor
    the channel's long-form shelf deserves one human look before it goes out.
"""
from __future__ import annotations

import logging
import os
import shutil

from sqlalchemy import select

from core.config import settings
from database.models import Task
from database.types import TaskStatus

logger = logging.getLogger(__name__)

LIBRARY_CAP_PER_CAMPAIGN = 24   # newest masters kept; a short is ~10-50MB → ≤ ~1.2GB per campaign
MIN_EPISODES_TO_COMPILE = 10    # the council may propose a compile only past this
DEFAULT_TOP_N = 12              # 12 × ~55s ≈ 11 minutes — comfortably past the 8-min mid-roll bar
COMPILATION_EPISODE_BASE = 9000  # sentinel numbering: keeps unique(campaign, episode) untouched


def library_dir(campaign_id: int) -> str:
    return os.path.join(settings.MEDIA_ROOT, "library", str(campaign_id))


def episode_master_path(campaign_id: int, episode_number: int) -> str:
    return os.path.join(library_dir(campaign_id), f"ep_{episode_number}.mp4")


def retain_master(campaign_id: int, episode_number: int, video_path: str | None) -> str | None:
    """Move a just-published master into the campaign library instead of deleting it (ADR-082),
    then trim the library to its cap, oldest first. Fail-open: any error means the file is simply
    gone, exactly as before this feature — retention is a bonus, never a publish blocker."""
    try:
        if not video_path or not os.path.exists(video_path):
            return None
        d = library_dir(campaign_id)
        os.makedirs(d, exist_ok=True)
        dest = episode_master_path(campaign_id, episode_number)
        shutil.move(video_path, dest)
        entries = sorted((os.path.getmtime(os.path.join(d, f)), f)
                         for f in os.listdir(d) if f.endswith(".mp4"))
        for _mtime, name in entries[:-LIBRARY_CAP_PER_CAMPAIGN]:
            os.remove(os.path.join(d, name))
        return dest
    except OSError:
        logger.warning("Could not retain master for campaign %s ep %s", campaign_id,
                       episode_number, exc_info=True)
        return None


def compilable_episodes(db, campaign) -> list[Task]:
    """The campaign's episodes that can be compiled RIGHT NOW: completed, measured, and with their
    master still in the library — best retention first (views as the tiebreak/fallback)."""
    tasks = db.scalars(select(Task).where(
        Task.campaign_id == campaign.id, Task.status == TaskStatus.COMPLETED,
        Task.episode_number < COMPILATION_EPISODE_BASE)).all()
    out = [t for t in tasks
           if os.path.exists(episode_master_path(campaign.id, t.episode_number))
           and (t.stats_json or {}).get("views") is not None]
    return sorted(out, key=lambda t: ((t.stats_json or {}).get("avg_pct_viewed") or 0.0,
                                      (t.stats_json or {}).get("views") or 0), reverse=True)


def next_compilation_number(db, campaign_id: int) -> int:
    latest = db.scalar(select(Task.episode_number).where(
        Task.campaign_id == campaign_id, Task.episode_number >= COMPILATION_EPISODE_BASE)
        .order_by(Task.episode_number.desc()).limit(1))
    return (latest or COMPILATION_EPISODE_BASE) + 1


def _chapter_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def compilation_metadata(campaign, picked: list[Task], durations: list[float]) -> dict:
    """Title/description/chapters for a best-of — deterministic, zero AI. The chapters double as
    YouTube's chapter list; each line names the episode by its stored memory (synopsis)."""
    lang = (campaign.config_json or {}).get("language", "en")
    seq = 1  # cosmetic only; uniqueness comes from the episode sentinel
    title_word = {"vi": "Tuyển tập hay nhất", "es": "Lo mejor de"}.get(lang, "Best of")
    title = f"{title_word}: {campaign.topic_name}"[:100]
    lines, at = [], 0.0
    for t, d in zip(picked, durations):
        label = (t.synopsis or f"Episode {t.episode_number}")[:80]
        lines.append(f"{_chapter_time(at)} {label}")
        at += d
    outro = {"vi": "Tổng hợp những tập được xem nhiều nhất của kênh.",
             "es": "Recopilación de los episodios más vistos del canal."}.get(
        lang, "A compilation of this channel's most-watched episodes.")
    return {"title": title, "description": "\n".join(lines) + f"\n\n{outro}",
            "tags": (campaign.config_json or {}).get("tags") or [],
            "video_format": "long",      # publishes as a normal video, never a Reel/Short
            "language": lang,
            "compiled_from": [t.episode_number for t in picked],
            "variant": None,
            "seq": seq}


def build_concat_list(paths: list[str], list_file: str) -> str:
    with open(list_file, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '%s'\n" % p.replace("'", "'\\''"))
    return list_file
