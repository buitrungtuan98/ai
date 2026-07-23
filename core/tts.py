"""edge-tts narration synthesis.

Returns the mp3 path plus exact per-word timings taken from edge-tts `WordBoundary` events — so we
get caption timing for free, with no forced aligner (KISS). The SDK is imported lazily so tests and
non-render code don't require it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
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

# Curated edge-tts voice catalog per language — THE single source of truth for voice pickers:
# the campaign form's dropdown filters to the target language from this dict, and the AI campaign
# designer may only propose voices from it (a typo'd/invented voice name would fail every render).
# (voice id, human label) — labels describe gender/region/feel so an operator can choose blind.
VOICE_CHOICES: dict[str, list[tuple[str, str]]] = {
    "vi": [
        ("vi-VN-HoaiMyNeural", "Hoài My — nữ, ấm áp, kể chuyện"),
        ("vi-VN-NamMinhNeural", "Nam Minh — nam, trầm, điềm đạm"),
    ],
    "en": [
        ("en-US-AriaNeural", "Aria — female US, warm & expressive"),
        ("en-US-JennyNeural", "Jenny — female US, friendly"),
        ("en-US-MichelleNeural", "Michelle — female US, confident"),
        ("en-US-GuyNeural", "Guy — male US, energetic"),
        ("en-US-ChristopherNeural", "Christopher — male US, deep & calm"),
        ("en-US-EricNeural", "Eric — male US, mature"),
        ("en-GB-SoniaNeural", "Sonia — female UK"),
        ("en-GB-RyanNeural", "Ryan — male UK"),
        ("en-AU-NatashaNeural", "Natasha — female AU"),
        ("en-AU-WilliamNeural", "William — male AU"),
    ],
    "es": [
        ("es-ES-ElviraNeural", "Elvira — mujer, España"),
        ("es-ES-AlvaroNeural", "Álvaro — hombre, España"),
        ("es-MX-DaliaNeural", "Dalia — mujer, México"),
        ("es-MX-JorgeNeural", "Jorge — hombre, México"),
        ("es-US-PalomaNeural", "Paloma — mujer, EE. UU."),
        ("es-US-AlonsoNeural", "Alonso — hombre, EE. UU."),
    ],
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


# ── Paced narration: real editors leave breathing room between sentences ─────
# A single TTS call for a whole scene runs sentences together with no beat; real narration pauses,
# and pauses LONGER after a question or a cliffhanger. synthesize_paced() renders each sentence
# separately, stitches them with deterministic silence gaps, and returns ONE merged word-timing list
# with correct absolute offsets, so captions still line up exactly with the assembled audio.
_SENTENCE_RE = re.compile(r"[^.!?…]*[.!?…]+|[^.!?…]+$", re.UNICODE)
_GAP_DEFAULT = 0.35


def split_sentences(text: str) -> list[str]:
    """Split narration into sentences, keeping terminators. Deterministic (no NLP dependency)."""
    return [m.group().strip() for m in _SENTENCE_RE.finditer(text or "") if m.group().strip()]


def pause_after(sentence: str) -> float:
    """Deterministic breath gap (seconds) following a sentence — longer after a beat that lands."""
    s = sentence.rstrip()
    if s.endswith("…") or s.endswith("..."):
        return 0.7   # cliffhanger / trailing off — let it hang
    if s.endswith("?"):
        return 0.6   # a question invites a beat before the answer
    if s.endswith("!"):
        return 0.5
    return _GAP_DEFAULT


# One common audio format for every segment so the concat filter never rejects a mismatch.
_SEG_FMT = "aformat=sample_fmts=fltp:sample_rates=24000:channel_layouts=mono"


def build_paced_concat_args(parts: list[str], gaps: list[float], out_path: str) -> list[str]:
    """ffmpeg args (after the binary) that concatenate sentence audio `parts` with `gaps[i]` seconds
    of generated silence after part i (gaps has one fewer entry than parts). One re-encode; free."""
    args: list[str] = []
    for p in parts:
        args += ["-i", p]
    filters = [f"[{i}:a]{_SEG_FMT}[a{i}]" for i in range(len(parts))]
    seq: list[str] = []
    for i in range(len(parts)):
        seq.append(f"[a{i}]")
        if i < len(gaps) and gaps[i] > 0:
            filters.append(f"aevalsrc=0:d={gaps[i]:.3f}:s=24000,{_SEG_FMT}[g{i}]")
            seq.append(f"[g{i}]")
    filters.append("".join(seq) + f"concat=n={len(seq)}:v=0:a=1[out]")
    return args + ["-filter_complex", ";".join(filters), "-map", "[out]",
                   "-c:a", "libmp3lame", "-q:a", "4", out_path]


def synthesize_paced(
    text: str,
    out_path: str,
    *,
    language: str = "en",
    voice: str | None = None,
    rate_pct: int = 0,
) -> list[WordTiming]:
    """Like synthesize(), but paces multi-sentence narration with breath gaps. A single-sentence
    (or terminator-free) input falls straight through to synthesize() — identical to the old path."""
    sentences = split_sentences(text)
    if len(sentences) <= 1:
        return synthesize(text, out_path, language=language, voice=voice, rate_pct=rate_pct)

    from core import media
    from core.ffmpeg_runner import run_ffmpeg

    parts: list[str] = []
    per_timings: list[list[WordTiming]] = []
    durations: list[float] = []
    for i, sentence in enumerate(sentences):
        part = f"{out_path}.p{i}.mp3"
        per_timings.append(synthesize(sentence, part, language=language, voice=voice, rate_pct=rate_pct))
        parts.append(part)
        durations.append(media.probe_duration(part))

    gaps = [pause_after(s) for s in sentences[:-1]]
    run_ffmpeg(build_paced_concat_args(parts, gaps, out_path))

    merged: list[WordTiming] = []
    offset = 0.0
    for i, timings in enumerate(per_timings):
        merged += [WordTiming(w.text, w.start + offset, w.end + offset) for w in timings]
        offset += durations[i] + (gaps[i] if i < len(gaps) else 0.0)

    for part in parts:  # be tidy (the render workspace would sweep them anyway)
        try:
            os.remove(part)
        except OSError:
            pass
    return merged
