"""The single entry point for every ffmpeg invocation (DRY).

One place applies the ARM/CPU constraints: `nice -n <FFMPEG_NICE>` (keep the HTTP server
responsive), `-threads <FFMPEG_THREADS>` (use all cores), and `-progress pipe:1` parsing so long
renders can report a percentage back to the caller.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable, Sequence

from core.config import settings

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float], None]


class FFmpegError(RuntimeError):
    pass


def _nice_prefix() -> list[str]:
    # Use the `nice` binary if present (portable); otherwise run without it.
    if shutil.which("nice"):
        return ["nice", f"-n{settings.FFMPEG_NICE}"]
    return []


def run_ffmpeg(
    args: Sequence[str],
    *,
    total_duration: float | None = None,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Run `ffmpeg <args>` at low priority with N threads.

    If `total_duration` and `on_progress` are given, parse `-progress` output and call
    `on_progress(pct)` (0..99) as the encode advances. `args` should NOT include the `ffmpeg`
    binary, `-threads`, or `-progress` — those are added here.
    """
    cmd = [
        *_nice_prefix(),
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-progress", "pipe:1",
        "-threads", str(settings.FFMPEG_THREADS),
        *args,
    ]
    logger.debug("ffmpeg: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )

    last_reported = 0.0
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not (total_duration and on_progress):
            continue
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            raw = line.split("=", 1)[1]
            try:
                # out_time_us is microseconds; out_time_ms is (confusingly) also microseconds in ffmpeg.
                micros = int(raw)
            except ValueError:
                continue
            pct = min(99.0, (micros / 1e6) / total_duration * 100.0)
            if pct - last_reported >= 1.0:
                last_reported = pct
                on_progress(pct)

    proc.wait()
    if proc.returncode != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise FFmpegError(f"ffmpeg failed (exit {proc.returncode}): {stderr[-2000:]}")


def extract_frame(video_path: str, out_path: str, at_seconds: float) -> None:
    """Grab a single frame (used for thumbnails)."""
    run_ffmpeg(["-ss", f"{at_seconds:.2f}", "-i", video_path, "-frames:v", "1", "-q:v", "2", out_path])
