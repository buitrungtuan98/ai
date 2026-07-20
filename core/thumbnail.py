"""Thumbnail generation (PIL). Reuses the caption module's font loader + wrap (DRY)."""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

from core.captions import MARGIN_PX, VIDEO_H, VIDEO_W, _load_font, wrap_text
from core.ffmpeg_runner import extract_frame

TITLE_FONT_PX = 96


def generate_thumbnail(
    video_path: str,
    out_path: str,
    title: str,
    *,
    at_fraction: float = 0.15,
    logo_path: str | None = None,
    font_path: str | None = None,
) -> str:
    """Grab a representative frame, darken the lower area, and draw a wrapped bold title."""
    frame_png = out_path + ".frame.png"
    # A frame ~15% in avoids a black intro. Duration is not always known here, so use a small fixed
    # offset if fraction can't be applied by the caller.
    extract_frame(video_path, frame_png, at_seconds=max(0.5, at_fraction * 10))

    try:
        with Image.open(frame_png) as raw:
            img = raw.convert("RGB").resize((VIDEO_W, VIDEO_H))
        draw = ImageDraw.Draw(img, "RGBA")

        # Bottom scrim for text contrast.
        scrim_top = int(VIDEO_H * 0.6)
        draw.rectangle([0, scrim_top, VIDEO_W, VIDEO_H], fill=(0, 0, 0, 150))

        usable = VIDEO_W - 2 * MARGIN_PX
        lines = wrap_text(title, TITLE_FONT_PX, usable, font_path=font_path)
        font = _load_font(font_path, TITLE_FONT_PX)
        line_h = TITLE_FONT_PX + 18
        y = VIDEO_H - MARGIN_PX - line_h * len(lines)
        for line in lines:
            draw.text(
                (MARGIN_PX, y), line, font=font, fill=(255, 255, 255),
                stroke_width=6, stroke_fill=(0, 0, 0),
            )
            y += line_h

        if logo_path and os.path.exists(logo_path):
            with Image.open(logo_path) as logo_raw:
                logo = logo_raw.convert("RGBA")
                img.paste(logo, (VIDEO_W - logo.width - 40, 40), logo)

        img.convert("RGB").save(out_path, "JPEG", quality=85, optimize=True)
    finally:
        # Never leave the intermediate frame behind in the persistent output dir, even on error.
        if os.path.exists(frame_png):
            os.remove(frame_png)
    return out_path
