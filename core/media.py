"""ffprobe helpers. The audio's duration is the ground truth for video length."""
from __future__ import annotations

import json
import subprocess


def probe_duration(path: str) -> float:
    """Return media duration in seconds via ffprobe. Raises on failure."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def probe_audio_stats(path: str) -> dict:
    """Return {'mean_volume_db', 'max_volume_db'} (floats, dBFS; None if undetectable) via
    ffmpeg's volumedetect filter — the deterministic basis for the voice sanity check."""
    out = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", path,
            "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-",
        ],
        capture_output=True, text=True, check=True,
    )
    stats: dict = {"mean_volume_db": None, "max_volume_db": None}
    for line in out.stderr.splitlines():  # volumedetect reports on stderr
        for key, marker in (("mean_volume_db", "mean_volume:"), ("max_volume_db", "max_volume:")):
            if marker in line:
                try:
                    stats[key] = float(line.split(marker, 1)[1].replace("dB", "").strip())
                except ValueError:
                    pass
    return stats


def max_black_span(path: str) -> float:
    """Longest continuous fully-black stretch in seconds (0.0 if none), via ffmpeg blackdetect —
    the deterministic basis for catching a broken/black render. Raises on an unreadable file."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
         "-vf", "blackdetect=d=0.5:pic_th=0.98", "-an", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    spans: list[float] = []
    for line in out.stderr.splitlines():  # blackdetect reports on stderr
        marker = "black_duration:"
        if marker in line:
            try:
                spans.append(float(line.split(marker, 1)[1].strip().split()[0]))
            except (ValueError, IndexError):
                pass
    return max(spans, default=0.0)


def max_silence_span(path: str) -> float:
    """Longest continuous silence in seconds (0.0 if none), via ffmpeg silencedetect — the
    deterministic basis for catching a muted/broken audio track. Raises on an unreadable file."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
         "-af", "silencedetect=noise=-40dB:d=1.0", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    spans: list[float] = []
    for line in out.stderr.splitlines():  # silencedetect reports on stderr
        marker = "silence_duration:"
        if marker in line:
            try:
                spans.append(float(line.split(marker, 1)[1].strip().split()[0]))
            except (ValueError, IndexError):
                pass
    return max(spans, default=0.0)


def probe_video_meta(path: str) -> dict:
    """Return {width, height, duration, codec, fps} for the first video stream."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name,avg_frame_rate",
            "-show_entries", "format=duration", "-of", "json", path,
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    stream = (data.get("streams") or [{}])[0]
    fps_raw = stream.get("avg_frame_rate", "0/1")
    num, _, den = fps_raw.partition("/")
    fps = (float(num) / float(den)) if den and float(den) != 0 else 0.0
    return {
        "width": stream.get("width"),
        "height": stream.get("height"),
        "codec": stream.get("codec_name"),
        "fps": fps,
        "duration": float(data.get("format", {}).get("duration", 0.0) or 0.0),
    }
