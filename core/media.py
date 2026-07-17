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
