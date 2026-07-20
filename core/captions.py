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
    # Work in centiseconds so rounding carries correctly (59.996s → 1:00.00, never a bogus :60.00).
    cs = max(0, round(seconds * 100))
    s, cs = divmod(cs, 100)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\n", " ").replace("{", "(").replace("}", ")")


def hex_to_ass(hex_color: str) -> str:
    """'#RRGGBB' (or '0xRRGGBB') → ASS '&H00BBGGRR' (ASS colours are BGR)."""
    h = hex_color.strip()
    if h[:2].lower() == "0x":
        h = h[2:]
    h = h.lstrip("#")
    if len(h) != 6:
        return "&H00FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


# Caption themes (Cinema Polish). Each is a full ASS style recipe; "pop" adds a quick per-word
# scale bounce via \t transforms — the dominant viral-Shorts caption look. All render in the same
# single encode pass (zero extra cost).
CAPTION_THEMES: dict[str, dict] = {
    "classic": {"primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H80000000",
                 "border_style": 1, "outline_w": 4, "pop": False, "blur": 0},
    "highlight": {"primary": None,  # accent colour (campaign tint or warm yellow)
                  "outline": "&H00000000", "back": "&H80000000",
                  "border_style": 1, "outline_w": 4, "pop": True, "blur": 0},
    "boxed": {"primary": "&H00FFFFFF", "outline": "&H00181010", "back": "&HA0181010",
              "border_style": 3, "outline_w": 7, "pop": False, "blur": 0},
    "neon": {"primary": "&H0050FFB0", "outline": "&H00206040", "back": "&H80000000",
             "border_style": 1, "outline_w": 5, "pop": True, "blur": 2},
}
DEFAULT_ACCENT_ASS = "&H006BCFFF"  # warm yellow (#FFCF6B) in ASS BGR
POP_TAG = r"{\fscx82\fscy82\t(0,120,\fscx106\fscy106)\t(120,240,\fscx100\fscy100)}"


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
    theme: str = "highlight",
    accent_hex: str | None = None,  # '#RRGGBB' (e.g. campaign brand tint) for accent themes
    font_px: int = DEFAULT_FONT_PX,
    font_name: str = "DejaVu Sans",
) -> str:
    """Write an ASS file with one Dialogue per word (or per phrase in "line" style), styled by a
    caption theme (classic / highlight / boxed / neon)."""
    if style == "line":
        timings = group_words_into_lines(timings)
    spec = CAPTION_THEMES.get(theme, CAPTION_THEMES["classic"])
    primary = spec["primary"] or (hex_to_ass(accent_hex) if accent_hex else DEFAULT_ACCENT_ASS)
    prefix = ""
    if spec["blur"]:
        prefix += r"{\blur%d}" % spec["blur"]
    if spec["pop"]:
        prefix += POP_TAG

    usable = VIDEO_W - 2 * MARGIN_PX
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {VIDEO_W}
PlayResY: {VIDEO_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{font_name},{font_px},{primary},{spec['outline']},{spec['back']},1,0,{spec['border_style']},{spec['outline_w']},2,2,{MARGIN_PX},{MARGIN_PX},280,1

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
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Word,,0,0,0,,{prefix}{text}\n")

    content = "".join(lines)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path
