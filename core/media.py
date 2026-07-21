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
