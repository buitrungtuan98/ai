"""Rendering orchestration: script → TTS → footage → captions → render → stitch → thumbnail.

The audio (edge-tts) duration is the ground truth for each scene's length. Each scene is re-encoded
exactly once (scale/crop/branding/captions burned in the same pass); the finished scenes — emitted
with identical codec params — are stitched with the concat demuxer using `-c copy` (no re-encode),
which is the biggest CPU saver on ARM.

Content variation is optional channel branding (watermark/tint/mirror) — see ADR-006. It is applied
before captions so text/branding is never mirrored, and is NOT tuned for detection evasion.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from itertools import cycle

from core import media, pexels, safety_filter, tts
from core.ai_engine import VideoScript
from core.captions import build_ass
from core.cleanup import RenderWorkspace
from core.config import settings
from core.ffmpeg_runner import run_ffmpeg
from core.thumbnail import generate_thumbnail

logger = logging.getLogger(__name__)

TARGET_W = 1080
TARGET_H = 1920
FPS = 30

# Stage weights for a monotonic global progress bar (sum = 100).
_STAGE_BUDGET = {"prep": 40, "render": 45, "concat": 8, "thumb": 7}


@dataclass
class Branding:
    watermark_path: str | None = None
    tint_color: str | None = None     # e.g. "0x1E90FF"
    tint_opacity: float = 0.0         # 0..1
    mirror: bool = False

    @property
    def active(self) -> bool:
        return bool(self.watermark_path or (self.tint_color and self.tint_opacity > 0) or self.mirror)


@dataclass
class RenderResult:
    master_path: str
    thumbnail_path: str
    metadata: dict
    duration: float
    scene_count: int
    branding_applied: bool = False
    policy_warnings: list[str] = field(default_factory=list)


# ── Pure, testable helpers ───────────────────────────────────────────────────
def select_clips(clip_durations: list[float], target: float) -> list[int]:
    """Greedily pick clip indices (cycling if needed) until their total covers `target`."""
    if not clip_durations:
        raise ValueError("no clips available to cover the scene")
    chosen: list[int] = []
    total = 0.0
    for idx in cycle(range(len(clip_durations))):
        chosen.append(idx)
        total += clip_durations[idx]
        if total >= target:
            break
        if len(chosen) > 200:  # safety valve against zero/near-zero durations
            break
    return chosen


def build_scene_args(
    clip_paths: list[str],
    audio_path: str,
    ass_path: str,
    out_path: str,
    duration: float,
    branding: Branding | None = None,
) -> list[str]:
    """Build the ffmpeg args (after the `ffmpeg` binary) for one re-encoded scene."""
    branding = branding or Branding()
    args: list[str] = []
    for path in clip_paths:
        args += ["-i", path]
    args += ["-i", audio_path]
    audio_idx = len(clip_paths)

    wm_idx = None
    if branding.watermark_path:
        args += ["-i", branding.watermark_path]
        wm_idx = audio_idx + 1

    filters: list[str] = []
    for i in range(len(clip_paths)):
        filters.append(
            f"[{i}:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H},setsar=1,fps={FPS},setpts=PTS-STARTPTS[v{i}]"
        )
    if len(clip_paths) == 1:
        cur = "[v0]"
    else:
        concat_in = "".join(f"[v{i}]" for i in range(len(clip_paths)))
        filters.append(f"{concat_in}concat=n={len(clip_paths)}:v=1:a=0[vc]")
        cur = "[vc]"

    if branding.mirror:
        filters.append(f"{cur}hflip[vm]")
        cur = "[vm]"
    if branding.tint_color and branding.tint_opacity > 0:
        filters.append(
            f"{cur}drawbox=x=0:y=0:w=iw:h=ih:"
            f"color={branding.tint_color}@{branding.tint_opacity:.2f}:t=fill[vt]"
        )
        cur = "[vt]"
    if wm_idx is not None:
        filters.append(f"{cur}[{wm_idx}:v]overlay=W-w-40:40[vo]")
        cur = "[vo]"
    filters.append(f"{cur}ass={ass_path}[vout]")

    args += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", f"{audio_idx}:a",
        "-c:v", "libx264", "-preset", settings.FFMPEG_PRESET, "-crf", "23",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-vsync", "cfr",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
        "-t", f"{duration:.3f}", "-video_track_timescale", "30000",
        out_path,
    ]
    return args


def build_concat_args(list_file: str, out_path: str) -> list[str]:
    """Concat demuxer with stream copy — zero re-encode."""
    return ["-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out_path]


def pick_metadata(script: VideoScript, episode_number: int) -> dict:
    """A/B rotation: cycle the 3 metadata variations across episodes."""
    variations = script.metadata_variations
    chosen = variations[(max(episode_number, 1) - 1) % len(variations)]
    return chosen.model_dump()


# ── Orchestration ────────────────────────────────────────────────────────────
def produce(
    *,
    script: VideoScript,
    episode_number: int,
    pexels_api_key: str,
    job_id: str,
    output_dir: str,
    voice: str | None = None,
    rate_pct: int = 0,
    branding: Branding | None = None,
    extra_blacklist: set[str] | None = None,
    on_progress=None,
) -> RenderResult:
    """Render one episode from a validated script. Output (master.mp4 + thumb.jpg) is written to
    `output_dir` (outside the workspace) so it survives cleanup; all temp media is auto-removed."""
    import os

    branding = branding or Branding()
    os.makedirs(output_dir, exist_ok=True)
    lang = script.language
    n_scenes = len(script.scenes)

    def report(stage: str, sub: float) -> None:
        if not on_progress:
            return
        base = sum(v for k, v in _STAGE_BUDGET.items() if _stage_order(k) < _stage_order(stage))
        on_progress(min(99.0, base + sub / 100.0 * _STAGE_BUDGET[stage]))

    with RenderWorkspace(job_id) as ws:
        scene_files: list[str] = []
        durations: list[float] = []

        for si, scene in enumerate(script.scenes):
            # 1. Safety filter narration before TTS (policy lives in safety_filter).
            clean = safety_filter.filter_text(
                scene.narration, lang, extra_terms=extra_blacklist, mode="remove"
            ).clean_text or scene.narration

            # 2. TTS → mp3 + word timings; audio duration = ground truth.
            audio_path = ws.path(f"scene_{si}.mp3")
            timings = tts.synthesize(clean, audio_path, language=lang, voice=voice, rate_pct=rate_pct)
            d_i = media.probe_duration(audio_path)
            durations.append(d_i)

            # 3. Fetch footage to cover d_i.
            safety_filter.assert_licensed_footage("pexels")
            query = " ".join(scene.pexels_keywords)
            found = pexels.search_videos(query, pexels_api_key, per_page=10)
            if not found:
                raise RuntimeError(f"No Pexels footage for scene {si} (query={query!r})")
            picks = select_clips([c.duration for c in found], d_i)
            # Download each unique clip once, then expand `picks` (which may repeat) to file paths.
            path_by_idx: dict[int, str] = {}
            for k, idx in enumerate(dict.fromkeys(picks)):
                p = ws.path(f"scene_{si}_clip_{k}.mp4")
                pexels.download(found[idx].download_url, p)
                path_by_idx[idx] = p
            clip_paths = [path_by_idx[idx] for idx in picks]

            # 4. Captions + scene render.
            ass_path = ws.path(f"scene_{si}.ass")
            build_ass(timings, ass_path, clip_duration=d_i)
            scene_out = ws.path(f"scene_{si}.mp4")
            args = build_scene_args(clip_paths, audio_path, ass_path, scene_out, d_i, branding)
            run_ffmpeg(
                args, total_duration=d_i,
                on_progress=lambda p, s=si: report("render", (s + p / 100) / n_scenes * 100),
            )
            scene_files.append(scene_out)
            report("prep", (si + 1) / n_scenes * 100)

        # 5. Stitch (no re-encode).
        list_file = ws.path("concat.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for sf in scene_files:
                f.write(f"file '{sf}'\n")
        master = os.path.join(output_dir, f"episode_{episode_number}.mp4")
        run_ffmpeg(build_concat_args(list_file, master))
        report("concat", 100)

        # 6. Thumbnail + metadata.
        metadata = pick_metadata(script, episode_number)
        thumb = os.path.join(output_dir, f"episode_{episode_number}.jpg")
        generate_thumbnail(master, thumb, metadata["title"],
                           logo_path=branding.watermark_path)
        report("thumb", 100)

    return RenderResult(
        master_path=master,
        thumbnail_path=thumb,
        metadata=metadata,
        duration=sum(durations),
        scene_count=n_scenes,
        branding_applied=branding.active,
    )


def _stage_order(stage: str) -> int:
    return list(_STAGE_BUDGET).index(stage)
