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


# Cinema Polish: subtle motion baked into the same encode pass. Effects rotate deterministically
# per scene (zoom in → pan → zoom out), so cuts feel edited without any randomness to break tests.
MOTION_EFFECTS = ["zoom_in", "pan", "zoom_out"]
_MOTION_MAX_ZOOM = 1.08

# Per-campaign colour grades, baked into the one scene encode (no extra pass). Applied to the
# footage before mirror/tint/watermark/captions so text is never graded.
COLOR_GRADES: dict[str, str] = {
    "cinematic": "eq=contrast=1.06:saturation=0.92,colorbalance=bs=0.06:ms=0.03:hs=-0.03",
    "warm": "eq=contrast=1.03:saturation=1.08,colorbalance=rs=0.05:rm=0.03:bs=-0.04",
    "cool": "eq=contrast=1.05:saturation=0.95,colorbalance=bs=0.07:bm=0.04:rs=-0.03",
    "vivid": "eq=contrast=1.08:saturation=1.25",
    "noir": "hue=s=0,eq=contrast=1.15:brightness=-0.02",
}

# Loudness normalization to the -14 LUFS short-form platform target (EBU R128 single pass).
LOUDNORM_FILTER = "loudnorm=I=-14:TP=-1.5:LRA=11"


def motion_filter(effect: str, duration: float) -> str:
    frames = max(int(duration * FPS), 1)
    rate = (_MOTION_MAX_ZOOM - 1.0) / frames
    center = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    if effect == "zoom_in":
        return (f"zoompan=z='min(zoom+{rate:.6f},{_MOTION_MAX_ZOOM})':{center}"
                f":d=1:s={TARGET_W}x{TARGET_H}:fps={FPS}")
    if effect == "zoom_out":
        return (f"zoompan=z='if(eq(on,1),{_MOTION_MAX_ZOOM},max(zoom-{rate:.6f},1.0))':{center}"
                f":d=1:s={TARGET_W}x{TARGET_H}:fps={FPS}")
    # pan: overscan then glide horizontally across the extra width for the scene duration
    ow, oh = int(TARGET_W * 1.12), int(TARGET_H * 1.12)
    return (f"scale={ow}:{oh},crop={TARGET_W}:{TARGET_H}"
            f":x='(in_w-out_w)*min(t/{max(duration, 0.1):.3f},1)':y='(in_h-out_h)/2'")


def build_scene_args(
    clip_paths: list[str],
    audio_path: str,
    ass_path: str,
    out_path: str,
    duration: float,
    branding: Branding | None = None,
    motion_effect: str | None = None,
    color_grade: str | None = None,
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

    if motion_effect:
        filters.append(f"{cur}{motion_filter(motion_effect, duration)}[vmn]")
        cur = "[vmn]"

    if color_grade and color_grade in COLOR_GRADES:
        filters.append(f"{cur}{COLOR_GRADES[color_grade]}[vg]")
        cur = "[vg]"

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


def build_concat_args(
    list_file: str,
    out_path: str,
    music_path: str | None = None,
    music_volume: float = 0.15,
    loudnorm: bool = True,
) -> list[str]:
    """Concat demuxer with video stream copy — the video is NEVER re-encoded here. The audio is
    re-encoded once when needed: to mix looped, ducked background music under the narration and/or
    to normalize the final mix to -14 LUFS (`loudnorm`), so every episode publishes at the same
    perceived volume."""
    if not music_path and not loudnorm:
        return ["-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out_path]
    if not music_path:
        return [
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-af", LOUDNORM_FILTER,
            "-map", "0:v", "-map", "0:a",
            "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-b:a", "128k",
            out_path,
        ]
    out_label = "[mix]" if loudnorm else "[aout]"
    mix = (f"[1:a]volume={music_volume:.2f}[m];"
           f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0{out_label}")
    if loudnorm:
        mix += f";[mix]{LOUDNORM_FILTER}[aout]"
    return [
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-stream_loop", "-1", "-i", music_path,
        "-filter_complex", mix,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-b:a", "128k", "-shortest",
        out_path,
    ]


FALLBACK_FOOTAGE_QUERY = "abstract dark background"


def search_footage(keywords: list[str], api_key: str) -> list:
    """Resilient footage search: joined keywords → each keyword alone → generic fallback.
    One weak keyword (or a non-English slip) must not fail the whole episode."""
    queries = [" ".join(keywords), *keywords, FALLBACK_FOOTAGE_QUERY]
    for query in queries:
        found = pexels.search_videos(query, api_key, per_page=10)
        if found:
            if query != queries[0]:
                logger.info("Footage fallback used: %r", query)
            return found
    return []


FOOTAGE_VET_CANDIDATES = 3  # bound the extra downloads/vision calls per scene


def vet_candidates(found: list, narration: str, vet, download, path_for) -> tuple[list, dict[int, str]]:
    """Run the vision judge over the leading footage candidates (Auto-QC).

    Each candidate is downloaded (`download(url, path)` to `path_for(idx)`) and judged with
    `vet(path, narration)`; the first accepted one becomes the pool leader and the rejected
    leaders are dropped. Returns (candidates, predownloaded {index: path}) so the render step
    never downloads the same clip twice. If every vetted candidate is rejected, the original
    order is kept — a rendered episode beats a failed one (fail-open)."""
    downloaded: dict[int, str] = {}
    for idx in range(min(FOOTAGE_VET_CANDIDATES, len(found))):
        path = path_for(idx)
        download(found[idx].download_url, path)
        downloaded[idx] = path
        if vet(path, narration):
            return found[idx:], {i - idx: p for i, p in downloaded.items() if i >= idx}
    logger.info("Footage vetting rejected all %d candidates — keeping original order", len(downloaded))
    return found, downloaded


def pick_metadata(script: VideoScript, episode_number: int, ab_testing: bool = True) -> dict:
    """A/B rotation: cycle the 3 metadata variations across episodes. With A/B testing disabled,
    variant A is always used."""
    variations = script.metadata_variations
    if not ab_testing:
        return variations[0].model_dump()
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
    subtitle_style: str = "word",
    caption_theme: str = "highlight",
    motion: bool = True,
    color_grade: str | None = None,
    music_path: str | None = None,
    music_volume: float = 0.15,
    loudnorm: bool = True,
    ab_testing: bool = True,
    extra_blacklist: set[str] | None = None,
    vet_clip=None,
    on_progress=None,
) -> RenderResult:
    """Render one episode from a validated script. Output (master.mp4 + thumb.jpg) is written to
    `output_dir` (outside the workspace) so it survives cleanup; all temp media is auto-removed."""
    import os

    branding = branding or Branding()
    if music_path and not os.path.exists(music_path):
        # Explicit failure beats a silently music-less video (config-truth rule).
        raise FileNotFoundError(f"Background music file not found: {music_path}")
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

            # 3. Fetch footage to cover d_i (with keyword fallback chain).
            safety_filter.assert_licensed_footage("pexels")
            found = search_footage(scene.pexels_keywords, pexels_api_key)
            if not found:
                raise RuntimeError(
                    f"No Pexels footage for scene {si} (keywords={scene.pexels_keywords!r})")

            # 3b. Optional AI footage vetting (Auto-QC): the first candidate the vision judge
            # accepts leads the pool; rejected leaders are dropped. Downloads are reused below.
            predownloaded: dict[int, str] = {}
            if vet_clip is not None and len(found) > 1:
                found, predownloaded = vet_candidates(
                    found, scene.narration, vet_clip, pexels.download,
                    lambda k, si=si: ws.path(f"scene_{si}_vet_{k}.mp4"),
                )

            picks = select_clips([c.duration for c in found], d_i)
            # Download each unique clip once (reusing vetted downloads), then expand `picks`
            # (which may repeat) to file paths.
            path_by_idx: dict[int, str] = dict(predownloaded)
            for k, idx in enumerate(dict.fromkeys(picks)):
                if idx in path_by_idx:
                    continue
                p = ws.path(f"scene_{si}_clip_{k}.mp4")
                pexels.download(found[idx].download_url, p)
                path_by_idx[idx] = p
            clip_paths = [path_by_idx[idx] for idx in picks]

            # 4. Captions + scene render (motion + theme baked into the same pass).
            ass_path = ws.path(f"scene_{si}.ass")
            build_ass(timings, ass_path, clip_duration=d_i, style=subtitle_style,
                      theme=caption_theme, accent_hex=branding.tint_color)
            scene_out = ws.path(f"scene_{si}.mp4")
            effect = MOTION_EFFECTS[si % len(MOTION_EFFECTS)] if motion else None
            args = build_scene_args(clip_paths, audio_path, ass_path, scene_out, d_i, branding,
                                    motion_effect=effect, color_grade=color_grade)
            run_ffmpeg(
                args, total_duration=d_i,
                on_progress=lambda p, s=si: report("render", (s + p / 100) / n_scenes * 100),
            )
            scene_files.append(scene_out)
            report("prep", (si + 1) / n_scenes * 100)

        # 5. Stitch (video stream copy; audio-only re-encode when music is mixed in).
        list_file = ws.path("concat.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for sf in scene_files:
                f.write(f"file '{sf}'\n")
        master = os.path.join(output_dir, f"episode_{episode_number}.mp4")
        run_ffmpeg(build_concat_args(list_file, master, music_path, music_volume, loudnorm=loudnorm))
        report("concat", 100)

        # 6. Thumbnail + metadata.
        metadata = pick_metadata(script, episode_number, ab_testing=ab_testing)
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
