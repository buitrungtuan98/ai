"""edge-tts narration synthesis.

Returns the mp3 path plus exact per-word timings taken from edge-tts `WordBoundary` events — so we
get caption timing for free, with no forced aligner (KISS). The SDK is imported lazily so tests and
non-render code don't require it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

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

    communicate = edge_tts.Communicate(text, voice, rate=_rate_str(rate_pct))
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
    """Synthesize `text` to `out_path` (mp3). Returns word timings (relative to clip start)."""
    resolved = resolve_voice(language, voice)
    return asyncio.run(_synthesize_async(text, resolved, rate_pct, out_path))
