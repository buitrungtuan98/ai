"""edge-tts narration synthesis.

Returns the mp3 path plus exact per-word timings taken from edge-tts `WordBoundary` events — so we
get caption timing for free, with no forced aligner (KISS). The SDK is imported lazily so tests and
non-render code don't require it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 3       # Microsoft's endpoint throws occasional transient 403s/disconnects
_RETRY_SLEEP_SECONDS = 3  # patched to 0 in tests

# edge-tts default voices per language (overridable per campaign).
DEFAULT_VOICES: dict[str, str] = {
    "en": "en-US-AriaNeural",
    "vi": "vi-VN-HoaiMyNeural",
    "es": "es-ES-ElviraNeural",
}

_TICKS_PER_SECOND = 1e7  # edge-tts offsets/durations are in 100-nanosecond ticks


@dataclass
class WordTiming:
    text: str
    start: float  # seconds
    end: float    # seconds


def resolve_voice(language: str, voice: str | None) -> str:
    return voice or DEFAULT_VOICES.get(language, DEFAULT_VOICES["en"])


def _rate_str(rate_pct: int) -> str:
    """edge-tts wants a signed percentage string, e.g. '+10%' / '-5%'."""
    return f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"


async def _synthesize_async(text: str, voice: str, rate_pct: int, out_path: str) -> list[WordTiming]:
    import edge_tts

    # edge-tts >= 7 defaults to SENTENCE boundaries; captions need per-WORD timings, so request
    # them explicitly (without this, timings come back empty and videos render with no subtitles).
    communicate = edge_tts.Communicate(text, voice, rate=_rate_str(rate_pct), boundary="WordBoundary")
    timings: list[WordTiming] = []
    with open(out_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / _TICKS_PER_SECOND
                dur = chunk["duration"] / _TICKS_PER_SECOND
                timings.append(WordTiming(text=chunk["text"], start=start, end=start + dur))
    return timings


def synthesize(
    text: str,
    out_path: str,
    *,
    language: str = "en",
    voice: str | None = None,
    rate_pct: int = 0,
) -> list[WordTiming]:
    """Synthesize `text` to `out_path` (mp3). Returns word timings (relative to clip start).

    Retries transient endpoint failures (the service occasionally drops a handshake); a
    persistent failure still raises so the episode fails visibly rather than silently."""
    resolved = resolve_voice(language, voice)
    last: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return asyncio.run(_synthesize_async(text, resolved, rate_pct, out_path))
        except Exception as exc:  # noqa: BLE001 — retry the flaky network path, then surface
            last = exc
            logger.warning("TTS attempt %d/%d failed: %s", attempt + 1, _RETRY_ATTEMPTS, exc)
            if attempt < _RETRY_ATTEMPTS - 1 and _RETRY_SLEEP_SECONDS:
                time.sleep(_RETRY_SLEEP_SECONDS * (attempt + 1))
    raise last if last is not None else RuntimeError("TTS failed with no error")
