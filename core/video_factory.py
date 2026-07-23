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

# Stage weights for a monotonic global progress bar (sum = 100). Per-scene work (TTS + footage +
# encode) is one rising band so progress never jumps backward between scenes.
_STAGE_BUDGET = {"scenes": 85, "concat": 8, "thumb": 7}


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
    "cinematic": "eq=contrast=1.06:saturation=0.92,colorbalance=bs=0.06:bm=0.03:bh=-0.03",
    "warm": "eq=contrast=1.03:saturation=1.08,colorbalance=rs=0.05:rm=0.03:bs=-0.04",
    "cool": "eq=contrast=1.05:saturation=0.95,colorbalance=bs=0.07:bm=0.04:rs=-0.03",
    "vivid": "eq=contrast=1.08:saturation=1.25",
    "noir": "hue=s=0,eq=contrast=1.15:brightness=-0.02",
}

# Loudness normalization to the -14 LUFS short-form platform target (EBU R128 single pass).
LOUDNORM_FILTER = "loudnorm=I=-14:TP=-1.5:LRA=11"

# Deterministic voice sanity gate (zero API cost): a silent or truncated TTS output must fail
# BEFORE minutes of CPU rendering, not ship as a broken published video. edge-tts speech sits
# around -20..-30 dB mean; anything below this across the whole clip is effectively silence.
VOICE_SILENCE_MEAN_DB = -50.0


def voice_check(audio_path: str, text: str) -> str | None:
    """Return a problem description if the narration audio is unusable, else None.

    An unreadable file IS a problem (the render would die at probe time anyway) — this check
    fails CLOSED, unlike the vision QC, because it is deterministic and free."""
    try:
        duration = media.probe_duration(audio_path)
        stats = media.probe_audio_stats(audio_path)
    except Exception as exc:  # noqa: BLE001 — a corrupt/empty file is exactly what we're catching
        return f"narration audio unreadable ({exc})"
    words = len(text.split())
    if words >= 3 and duration < 0.5:
        return f"audio lasts {duration:.2f}s for {words} words (truncated TTS output)"
    mean = stats.get("mean_volume_db")
    if mean is not None and mean < VOICE_SILENCE_MEAN_DB:
        return f"audio is effectively silent (mean volume {mean:.1f} dB)"
    return None


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
        # Encoder thread cap (CLAUDE.md constraint 4). As an OUTPUT option it binds to libx264;
        # the runner's global -threads only sets input-decode threads.
        "-threads", str(settings.FFMPEG_THREADS),
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
    # Duck the music UNDER the narration (the human-editor sound): split the narration, key a
    # compressor on the music with one copy as the sidechain, then mix the ducked music back with
    # the other. Music dips when the voice speaks and swells in the gaps. music_volume sets the
    # base bed level before ducking. normalize=0: amix must NOT auto-scale inputs by 1/n (that would
    # halve the narration); loudnorm (default on) tames the final peaks.
    mix = (
        f"[0:a]asplit=2[nar_mix][nar_sc];"
        f"[1:a]volume={music_volume:.2f}[mus];"
        f"[mus][nar_sc]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=250[duck];"
        f"[nar_mix][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0{out_label}"
    )
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
    One weak keyword (or a non-English slip) must not fail the whole episode — and neither must
    a transient Pexels error on one query: each call is isolated so the chain can continue."""
    queries = [" ".join(keywords), *keywords, FALLBACK_FOOTAGE_QUERY]
    for query in queries:
        try:
            found = pexels.search_videos(query, api_key, per_page=10)
        except Exception:  # noqa: BLE001 — a 429/5xx on one query must not kill the episode
            logger.warning("Pexels search failed for %r — trying next fallback", query, exc_info=True)
            continue
        if found:
            if query != queries[0]:
                logger.info("Footage fallback used: %r", query)
            return found
    return []


def _batch_vet_plans(plans: list[dict], vet_batch, path_for, download=None) -> None:
    """Batched Auto-QC footage vetting over prepared scene plans (mutates them in place).

    One vision call judges every scene's lead candidate at once; rejected scenes swap to their
    next candidate, which is verified in ONE follow-up call — ≤2 vision calls per episode
    (previously ~1 per scene). Everything fails open: a vet or download error keeps the current
    candidate, because a rendered episode beats a failed one."""
    download = download or pexels.download
    idxs = [i for i, p in enumerate(plans) if 0 in p["pre"]]
    if not idxs:
        return
    accepts = vet_batch([(plans[i]["pre"][0], plans[i]["clean"]) for i in idxs])
    retry: list[int] = []
    for i, ok in zip(idxs, accepts):
        plan = plans[i]
        if ok or len(plan["found"]) < 2:
            continue
        plan["found"] = plan["found"][1:]  # drop the rejected leader
        try:
            p = path_for(i, 1)
            download(plan["found"][0].download_url, p)
            plan["pre"] = {0: p}
            retry.append(i)
        except Exception:  # noqa: BLE001
            plan["pre"] = {}  # replacement download failed — render loop fetches by pick
            logger.warning("Replacement candidate download failed for scene %d", i, exc_info=True)
    if retry:
        accepts2 = vet_batch([(plans[i]["pre"][0], plans[i]["clean"]) for i in retry])
        for i, ok in zip(retry, accepts2):
            if not ok:
                logger.info("Scene %d replacement candidate still weak — keeping it (fail-open)", i)


def pick_metadata(script: VideoScript, episode_number: int, ab_testing: bool = True,
                  title_prefix: str | None = None,
                  affiliate_url: str | None = None, affiliate_label: str | None = None) -> dict:
    """A/B rotation: cycle the 3 metadata variations across episodes. With A/B testing disabled,
    variant A is always used. An optional operator-set `title_prefix` (channel brand mark, e.g.
    '🔥 SỬ VIỆT |') is prepended to the AI's standalone hook title, and an optional affiliate link
    is appended to the description WITH a disclosure marker (platform/FTC rules require it)."""
    variations = script.metadata_variations
    chosen = variations[0] if not ab_testing else variations[(max(episode_number, 1) - 1) % len(variations)]
    meta = chosen.model_dump()
    if title_prefix and title_prefix.strip():
        meta["title"] = f"{title_prefix.strip()} {meta['title']}"[:100]  # YouTube's hard cap
    if affiliate_url:
        footer = f"\n\n{(affiliate_label or '🔗').strip()} {affiliate_url}\n(affiliate link)"
        meta["description"] = meta["description"][:4500] + footer  # stay well under YT's cap
    return meta


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
    title_prefix: str | None = None,
    affiliate_url: str | None = None,
    affiliate_label: str | None = None,
    extra_blacklist: set[str] | None = None,
    vet_batch=None,
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

        # Phase A — prep every scene first (filter → TTS → footage search + lead-candidate
        # download). Collecting all scenes before rendering is what lets Auto-QC vet the WHOLE
        # episode's footage in one batched vision call instead of one call per scene.
        plans: list[dict] = []
        for si, scene in enumerate(script.scenes):
            # Safety filter narration before TTS (policy lives in safety_filter). If the filter
            # emptied a non-empty narration (whole scene was blacklisted), do NOT fall back to the
            # raw text — that would defeat the brand-safety gate. The empty result surfaces as a
            # clear render failure instead of burning unsafe words into the video.
            filtered = safety_filter.filter_text(
                scene.narration, lang, extra_terms=extra_blacklist, mode="remove"
            )
            clean = filtered.clean_text
            if not clean and not filtered.changed:
                clean = scene.narration  # nothing filtered → empty means the source was empty

            # TTS → mp3 + word timings; audio duration = ground truth. Paced synthesis stitches the
            # scene's sentences with breath gaps (returns merged timings so captions still align).
            audio_path = ws.path(f"scene_{si}.mp3")
            timings = tts.synthesize_paced(clean, audio_path, language=lang, voice=voice,
                                           rate_pct=rate_pct)
            # Voice sanity (deterministic, free): silent/truncated TTS output → one re-synthesis,
            # then a loud failure — never hours later as a broken published video.
            problem = voice_check(audio_path, clean)
            if problem:
                logger.warning("Scene %d voice check failed (%s) — re-synthesizing once", si, problem)
                timings = tts.synthesize_paced(clean, audio_path, language=lang, voice=voice,
                                               rate_pct=rate_pct)
                problem = voice_check(audio_path, clean)
                if problem:
                    raise RuntimeError(f"Scene {si} narration failed the voice check: {problem}")
            d_i = media.probe_duration(audio_path)

            # Footage to cover d_i (with keyword fallback chain).
            safety_filter.assert_licensed_footage("pexels")
            found = search_footage(scene.pexels_keywords, pexels_api_key)
            if not found:
                raise RuntimeError(
                    f"No Pexels footage for scene {si} (keywords={scene.pexels_keywords!r})")
            pre: dict[int, str] = {}
            if vet_batch is not None and len(found) > 1:
                p = ws.path(f"scene_{si}_vet_0.mp4")
                try:
                    pexels.download(found[0].download_url, p)
                    pre[0] = p
                except Exception:  # noqa: BLE001 — vetting is optional; render must proceed
                    logger.warning("Lead-candidate download failed for scene %d", si, exc_info=True)
            plans.append({"clean": clean, "audio": audio_path, "timings": timings,
                          "d": d_i, "found": found, "pre": pre})
            report("scenes", (si + 1) / n_scenes * 30)

        # Phase B — batched Auto-QC footage vetting: ≤2 vision calls per EPISODE, fail-open.
        if vet_batch is not None:
            _batch_vet_plans(
                plans, vet_batch,
                path_for=lambda i, k: ws.path(f"scene_{i}_vet_{k}.mp4"),
            )
        durations = [p["d"] for p in plans]

        # Phase C — render each scene (captions + motion + grade baked into one encode pass).
        for si, plan in enumerate(plans):
            d_i = plan["d"]
            picks = select_clips([c.duration for c in plan["found"]], d_i)
            # Download each unique clip once (reusing vetted downloads), then expand `picks`
            # (which may repeat) to file paths.
            path_by_idx: dict[int, str] = dict(plan["pre"])
            for k, idx in enumerate(dict.fromkeys(picks)):
                if idx in path_by_idx:
                    continue
                p = ws.path(f"scene_{si}_clip_{k}.mp4")
                pexels.download(plan["found"][idx].download_url, p)
                path_by_idx[idx] = p
            clip_paths = [path_by_idx[idx] for idx in picks]

            ass_path = ws.path(f"scene_{si}.ass")
            build_ass(plan["timings"], ass_path, clip_duration=d_i, style=subtitle_style,
                      theme=caption_theme, accent_hex=branding.tint_color)
            scene_out = ws.path(f"scene_{si}.mp4")
            effect = MOTION_EFFECTS[si % len(MOTION_EFFECTS)] if motion else None
            args = build_scene_args(clip_paths, plan["audio"], ass_path, scene_out, d_i, branding,
                                    motion_effect=effect, color_grade=color_grade)
            run_ffmpeg(
                args, total_duration=d_i,
                on_progress=lambda p, s=si: report("scenes", 30 + (s + p / 100) / n_scenes * 70),
            )
            scene_files.append(scene_out)
            report("scenes", 30 + (si + 1) / n_scenes * 70)

        # 5. Stitch (video stream copy; audio-only re-encode when music is mixed in).
        list_file = ws.path("concat.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for sf in scene_files:
                # concat demuxer: a single quote inside a quoted path is written as '\'' .
                f.write("file '%s'\n" % sf.replace("'", "'\\''"))
        master = os.path.join(output_dir, f"episode_{episode_number}.mp4")
        run_ffmpeg(build_concat_args(list_file, master, music_path, music_volume, loudnorm=loudnorm))
        report("concat", 100)

        # 6. Thumbnail + metadata.
        metadata = pick_metadata(script, episode_number, ab_testing=ab_testing,
                                 title_prefix=title_prefix,
                                 affiliate_url=affiliate_url, affiliate_label=affiliate_label)
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
