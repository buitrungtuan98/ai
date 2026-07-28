"""Studio Mode: AI-drawn story visuals with consistent characters (ADR-052).

The second video-build source alongside Pexels stock footage. For each script scene we draw one
keyframe with the Gemini image model, starring a character picked from the channel's cast; that
character's reference sheet (drawn once) plus the previous scene's frame are passed back as reference
images so the face and art style stay consistent across the whole episode. The render pipeline then
animates each still with its existing Ken-Burns motion + crossfade stage — no local diffusion model,
no paid API, so the CPU-only / $0 constraints hold.

Layering (SOLID): this module owns the Studio DOMAIN logic (character selection + prompt building +
reference chaining); `ai_engine.generate_image` owns the raw Gemini call; `video_factory` owns the
ffmpeg render. The `gen_image` seam on every generator makes the whole module testable without a key.
"""
from __future__ import annotations

import logging
import os
import random
from collections.abc import Callable

from core import ai_engine

logger = logging.getLogger(__name__)

# The house look every Studio drawing shares unless a character/campaign overrides it — keeps a
# channel visually coherent and steers the model toward clean, animation-friendly frames.
_BASE_STYLE = "clean 2D illustration, bold readable shapes, high contrast, simple uncluttered background"

# Expectations baked into every beat prompt (the operator's Studio-Mode wishlist): a dynamic pose
# that reads the action, motion cues (action lines / subtle motion blur) so a still implies movement,
# and a vertical-friendly composition with room for the burned-in captions.
_BEAT_DIRECTION = (
    "Dynamic, expressive pose that reads the action of the moment. "
    "Add subtle motion cues — light action lines and a touch of motion blur on anything moving. "
    "Single clear focal subject, strong silhouette, generous empty space in the lower third for "
    "on-screen captions. Do not draw any text, words, letters, logos or watermark in the image."
)

GenImage = Callable[..., str]  # ai_engine.generate_image signature (kwargs), injectable for tests


def pick_character(characters: list[dict] | None, *, seed: int = 0) -> dict | None:
    """Choose one character from the cast for an episode. Deterministic in `seed` (the episode
    number) so a re-render reuses the same character — and therefore its already-cached sheet."""
    cast = [c for c in (characters or []) if isinstance(c, dict) and (c.get("name") or "").strip()]
    if not cast:
        return None
    return random.Random(seed).choice(cast)


def _style_for(character: dict, style_override: str | None) -> str:
    """Resolve the art style for a drawing: explicit campaign override > the character's own style >
    the house base style."""
    return (style_override or "").strip() or (character.get("style") or "").strip() or _BASE_STYLE


def character_sheet_prompt(character: dict, style_override: str | None = None) -> str:
    """Prompt for the one-time reference sheet that pins a character's identity."""
    style = _style_for(character, style_override)
    name = (character.get("name") or "the character").strip()
    desc = (character.get("description") or "").strip()
    return (
        f"Character reference sheet for a fictional cartoon character named {name}. "
        f"{desc + '. ' if desc else ''}"
        f"Art style: {style}. "
        "Draw the character full-body, front-facing, neutral standing pose, on a plain light "
        "background, with consistent proportions and colors. This is a model sheet used to keep the "
        "character identical in later frames. Do not draw any text, labels or watermark."
    )


def scene_prompt(
    character: dict | None, subject: str, *, mood: str | None = None, style_override: str | None = None,
) -> str:
    """Prompt for one scene keyframe. With a `character`, the SAME character (backed by the reference
    image) acts out `subject` in the resolved art style. With NO character (`None`, the quote content
    style — ADR-056), it's a pure atmosphere/scene drawing in the campaign's art style. `subject`
    should be a short visual description (the scene's English visual keywords work well); `mood` an
    optional context snippet from the narration."""
    style = _style_for(character or {}, style_override)
    parts: list[str] = []
    if character:
        name = (character.get("name") or "the character").strip()
        desc = (character.get("description") or "").strip()
        parts.append(f"Draw {name}" + (f" ({desc})" if desc else "")
                     + " — the SAME character shown in the reference image — in this scene.")
        parts.append(f"Keep the character's face, colors and proportions identical to the reference. "
                     f"Art style: {style}.")
    else:
        parts.append(f"Draw a single evocative illustration in this consistent art style: {style}.")
    if subject.strip():
        parts.append(f"Scene: {subject.strip()}.")
    if mood and mood.strip():
        parts.append(f"Context: {mood.strip()[:200]}.")
    parts.append(_BEAT_DIRECTION)
    return " ".join(parts)


def character_sheet(
    character: dict, *, api_key: str, out_path: str,
    model: str | None = None, style_override: str | None = None, gen_image: GenImage | None = None,
) -> str:
    """Draw the character's reference sheet ONCE, caching it at `out_path` (reused as-is if the file
    already exists — the sheet defines the character, so it must not drift between episodes). Returns
    the path to the sheet image."""
    gen = gen_image or ai_engine.generate_image
    if os.path.exists(out_path):
        return out_path
    logger.info("Studio: drawing character sheet for %r", character.get("name"))
    return gen(
        prompt=character_sheet_prompt(character, style_override),
        api_key=api_key, out_path=out_path, model=model,
    )


def scene_visual(
    *, character: dict | None, subject: str, api_key: str, out_path: str,
    mood: str | None = None, style_override: str | None = None,
    reference_paths: list[str] | None = None, reference_url: str | None = None,
    model: str | None = None, gen_image: GenImage | None = None,
) -> str:
    """Draw one scene keyframe starring `character`, conditioned on `reference_paths` (the character
    sheet first, then the previous scene's frame for temporal continuity — used by the Gemini leg) and
    `reference_url` (a public image URL used by Pollinations image-editing models like `kontext`, so
    the free provider can honour an uploaded reference too). Returns the image path."""
    gen = gen_image or ai_engine.generate_image
    return gen(
        prompt=scene_prompt(character, subject, mood=mood, style_override=style_override),
        api_key=api_key, out_path=out_path,
        reference_paths=reference_paths or [], reference_url=reference_url, model=model,
    )
