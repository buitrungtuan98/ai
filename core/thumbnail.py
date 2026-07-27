"""Thumbnail generation (PIL). Reuses the caption module's font loader + wrap (DRY)."""
from __future__ import annotations

import logging
import os

from PIL import Image, ImageDraw, ImageFilter, ImageStat

from core.captions import MARGIN_PX, VIDEO_H, VIDEO_W, _load_font, split_two_tone, wrap_text
from core.ffmpeg_runner import extract_frame

logger = logging.getLogger(__name__)

TITLE_FONT_PX = 96
_DEFAULT_ACCENT_RGB = (255, 207, 107)  # warm yellow (#FFCF6B) — matches the caption default accent


def _accent_rgb(accent_hex: str | None) -> tuple[int, int, int]:
    """Resolve a '#RRGGBB'/'0xRRGGBB' brand colour to RGB, falling back to the default warm yellow."""
    if not accent_hex:
        return _DEFAULT_ACCENT_RGB
    h = accent_hex.strip().lower().removeprefix("0x").lstrip("#")
    if len(h) != 6:
        return _DEFAULT_ACCENT_RGB
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return _DEFAULT_ACCENT_RGB


def _fit_font(lines: list[str], usable_px: int, max_px: int, min_px: int, font_path: str | None):
    """Largest font (≤ max_px, ≥ min_px) at which every line fits `usable_px` wide — so a poster title
    scales down to fit instead of overflowing. Returns (font, px)."""
    px = max_px
    while px > min_px:
        font = _load_font(font_path, px)
        if all(font.getbbox(ln)[2] <= usable_px for ln in lines if ln):
            return font, px
        px -= 4
    return _load_font(font_path, min_px), min_px
# Candidate frame positions (fraction of duration) when we know the length — spread across the body,
# skipping the very start/end where intros/outros and fades live.
_FRAME_SAMPLES = (0.12, 0.3, 0.5, 0.68, 0.85)


def _frame_score(img: Image.Image) -> float:
    """Higher = a better thumbnail frame: rich in edge detail (not blurry/near-black) and colourful.
    Edge stddev dominates; colour (mean RGB channel spread) is a lighter tie-breaker."""
    edges = img.convert("L").filter(ImageFilter.FIND_EDGES)
    sharp = ImageStat.Stat(edges).stddev[0]
    color = sum(ImageStat.Stat(img).stddev) / 3.0
    return sharp + 0.3 * color


def _select_frame(video_path: str, frame_png: str, at_fraction: float, duration: float | None) -> None:
    """Write the chosen candidate frame to `frame_png`. With a known duration, sample several frames
    and keep the sharpest/most-colourful (avoids a blurry or near-black mid-video grab); otherwise
    fall back to one fixed-offset frame. Fully fail-open — any error yields the single-frame path."""
    if not duration or duration <= 0:
        extract_frame(video_path, frame_png, at_seconds=max(0.5, at_fraction * 10))
        return
    best_score, best_tmp = None, None
    tmps: list[str] = []
    try:
        for i, frac in enumerate(_FRAME_SAMPLES):
            tmp = f"{frame_png}.cand{i}.png"
            try:
                extract_frame(video_path, tmp, at_seconds=max(0.3, frac * duration))
                with Image.open(tmp) as raw:
                    score = _frame_score(raw.convert("RGB"))
            except Exception:  # noqa: BLE001 — a bad seek/decode on one candidate must not fail all
                continue
            tmps.append(tmp)
            if best_score is None or score > best_score:
                best_score, best_tmp = score, tmp
        if best_tmp is None:  # every candidate failed — fall back to a single fixed grab
            extract_frame(video_path, frame_png, at_seconds=max(0.5, at_fraction * duration))
            return
        os.replace(best_tmp, frame_png)
        tmps.remove(best_tmp)
    finally:
        for t in tmps:
            if os.path.exists(t):
                os.remove(t)


def _draw_poster_title(draw, title, *, width, height, accent, font_path):
    """Draw the top billboard title on a thumbnail: UPPERCASE, two-tone (white then accent), heavy
    black outline, over a dark top scrim. Mirrors the in-video headline so the two match."""
    usable = width - 2 * MARGIN_PX
    line1, line2 = split_two_tone(title.upper())
    # Wrap each tone-line at a generous size, then pick a font that fits the widest row.
    max_px = round(height * 0.058)
    rows1 = wrap_text(line1, max_px, usable, font_path=font_path)
    rows2 = wrap_text(line2, max_px, usable, font_path=font_path) if line2 else []
    all_rows = [(r, (255, 255, 255)) for r in rows1] + [(r, accent) for r in rows2]
    if not all_rows:
        return
    font, px = _fit_font([r for r, _ in all_rows], usable, max_px, round(height * 0.03), font_path)
    line_h = px + round(px * 0.18)
    total_h = line_h * len(all_rows)
    top = round(height * 0.045)
    # Dark scrim behind the whole title block so it reads over any frame.
    draw.rectangle([0, 0, width, top + total_h + round(height * 0.02)], fill=(0, 0, 0, 150))
    y = top
    stroke = max(6, round(px * 0.09))
    for text, color in all_rows:
        w = font.getbbox(text)[2]
        draw.text(((width - w) // 2, y), text, font=font, fill=color,
                  stroke_width=stroke, stroke_fill=(0, 0, 0))
        y += line_h


def generate_thumbnail(
    video_path: str,
    out_path: str,
    title: str,
    *,
    at_fraction: float = 0.15,
    duration: float | None = None,
    logo_path: str | None = None,
    font_path: str | None = None,
    width: int = VIDEO_W,
    height: int = VIDEO_H,
    poster: bool = False,
    accent_hex: str | None = None,
) -> str:
    """Grab a representative frame, darken the lower area, and draw a wrapped bold title. `width`/
    `height` set the thumbnail geometry (default vertical 1080×1920; 1920×1080 for long-form). When
    `duration` is given, the frame is the sharpest/most-colourful of several candidates.

    `poster=True` draws the reference-channel billboard instead: a big UPPERCASE two-tone title (line 1
    white, line 2 `accent_hex`, heavy black outline) at the TOP over a dark scrim, character/frame
    visible below — the same title treatment burned into the video, so the thumbnail matches."""
    frame_png = out_path + ".frame.png"
    _select_frame(video_path, frame_png, at_fraction, duration)

    try:
        with Image.open(frame_png) as raw:
            img = raw.convert("RGB").resize((width, height))
        draw = ImageDraw.Draw(img, "RGBA")

        if poster:
            _draw_poster_title(draw, title, width=width, height=height,
                               accent=_accent_rgb(accent_hex), font_path=font_path)
        else:
            # Bottom scrim for text contrast.
            scrim_top = int(height * 0.6)
            draw.rectangle([0, scrim_top, width, height], fill=(0, 0, 0, 150))

            usable = width - 2 * MARGIN_PX
            lines = wrap_text(title, TITLE_FONT_PX, usable, font_path=font_path)
            font = _load_font(font_path, TITLE_FONT_PX)
            line_h = TITLE_FONT_PX + 18
            y = height - MARGIN_PX - line_h * len(lines)
            for line in lines:
                draw.text(
                    (MARGIN_PX, y), line, font=font, fill=(255, 255, 255),
                    stroke_width=6, stroke_fill=(0, 0, 0),
                )
                y += line_h

        if logo_path and os.path.exists(logo_path):
            with Image.open(logo_path) as logo_raw:
                logo = logo_raw.convert("RGBA")
                img.paste(logo, (width - logo.width - 40, 40), logo)

        img.convert("RGB").save(out_path, "JPEG", quality=85, optimize=True)
    finally:
        # Never leave the intermediate frame behind in the persistent output dir, even on error.
        if os.path.exists(frame_png):
            os.remove(frame_png)
    return out_path
