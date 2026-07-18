"""Word-by-word burned subtitles (ASS) and shared text-wrapping.

ASS (one `ass=` filter) is used instead of stacking dozens of `drawtext` filters — cleaner styling,
built-in wrapping, one graph node (KISS). Timing comes straight from the edge-tts word boundaries.
The PIL font loader + `wrap_text` are shared with `thumbnail.py` (DRY) so text metrics live once.
"""
from __future__ import annotations

import functools
import os

from PIL import ImageFont

from core.tts import WordTiming

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
]

VIDEO_W = 1080
VIDEO_H = 1920
DEFAULT_FONT_PX = 72
MARGIN_PX = 80  # left+right safe margin → usable width = 1080 - 2*80


@functools.lru_cache(maxsize=16)
def _load_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = font_path or next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _text_width(font, text: str) -> float:
    if hasattr(font, "getlength"):
        return font.getlength(text)
    return font.getbbox(text)[2]  # fallback


def wrap_text(text: str, font_px: int, usable_px: int, font_path: str | None = None) -> list[str]:
    """Greedy word-wrap so no line exceeds `usable_px`. Long single words are hard-split."""
    font = _load_font(font_path, font_px)
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if _text_width(font, trial) <= usable_px:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            if _text_width(font, word) <= usable_px:
                cur = word
            else:
                cur = _hard_split(word, font, usable_px, lines)
    if cur:
        lines.append(cur)
    return lines


def _hard_split(word: str, font, usable_px: int, lines: list[str]) -> str:
    chunk = ""
    for ch in word:
        if _text_width(font, chunk + ch) <= usable_px:
            chunk += ch
        else:
            lines.append(chunk)
            chunk = ch
    return chunk


def _ass_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\n", " ").replace("{", "(").replace("}", ")")


# Line-style grouping: break a line when it grows past this many characters or the narration
# pauses longer than this gap (a natural phrase boundary).
LINE_MAX_CHARS = 28
LINE_BREAK_GAP_S = 0.6


def group_words_into_lines(timings: list[WordTiming]) -> list[WordTiming]:
    """Merge word timings into phrase-level timings for the "line" subtitle style."""
    lines: list[WordTiming] = []
    buf: list[WordTiming] = []
    for wt in timings:
        if buf:
            too_long = len(" ".join(w.text for w in buf) + " " + wt.text) > LINE_MAX_CHARS
            long_pause = wt.start - buf[-1].end > LINE_BREAK_GAP_S
            if too_long or long_pause:
                lines.append(WordTiming(" ".join(w.text for w in buf), buf[0].start, buf[-1].end))
                buf = []
        buf.append(wt)
    if buf:
        lines.append(WordTiming(" ".join(w.text for w in buf), buf[0].start, buf[-1].end))
    return lines


def build_ass(
    timings: list[WordTiming],
    out_path: str,
    *,
    clip_duration: float | None = None,
    style: str = "word",  # "word" = one caption per word; "line" = phrase-level captions
    font_px: int = DEFAULT_FONT_PX,
    primary_colour: str = "&H00FFFFFF",  # white (AABBGGRR)
    font_name: str = "DejaVu Sans",
) -> str:
    """Write an ASS file with one Dialogue per word (or per phrase in "line" style)."""
    if style == "line":
        timings = group_words_into_lines(timings)
    usable = VIDEO_W - 2 * MARGIN_PX
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {VIDEO_W}
PlayResY: {VIDEO_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{font_name},{font_px},{primary_colour},&H00000000,&H80000000,1,0,1,4,2,2,{MARGIN_PX},{MARGIN_PX},280,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    n = len(timings)
    for i, wt in enumerate(timings):
        start = wt.start
        # Butt words together (use next word's start) to avoid flicker gaps.
        end = timings[i + 1].start if i + 1 < n else wt.end
        if clip_duration is not None:
            end = min(end, clip_duration)
        if end <= start:
            end = start + 0.05
        text = "\\N".join(wrap_text(_ass_escape(wt.text), font_px, usable))
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Word,,0,0,0,,{text}\n")

    content = "".join(lines)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path
