"""Vibe Engine (ADR-056): per-episode creative randomisation for the "quote" content style.

Aesthetic quote channels (à la the reference) publish one short poem per video over a single mood
illustration, and every video feels DIFFERENT while the channel's art style stays constant. This
module rolls that per-episode variety — mood, whether a one-off character appears (never a fixed
cast), the setting, the music mood, and a small voice-pace jitter — deterministically from a seed, so
a re-render of the same episode reproduces the same vibe (and its cached frames).

Pure and dependency-free: it only picks from curated pools; the script model then WRITES the poem +
per-line illustration briefs in the rolled vibe, and the render animates them. The art style itself
is NEVER rolled here — that's the campaign's `visual_style`, constant across every video (the channel
identity).
"""
from __future__ import annotations

import random

# The melancholic family the operator asked for — every video's overall tone is one of these.
MOODS = [
    "blue", "pensive", "somber", "moody", "melancholic", "wistful",
    "lonely", "nostalgic", "tender", "quietly hopeful",
]

# Atmospheric settings — a lone figure or empty scene lives in one of these (English, for the image
# model). Deliberately varied so no two videos share a backdrop by default.
SETTINGS = [
    "a rain-streaked window at night", "an empty dusk street with warm lamplight",
    "a quiet rooftop under a wash of city lights", "a winter field at golden hour",
    "an empty train station at dawn", "a seaside at twilight", "a narrow alley after rain",
    "a small room lit by a single warm lamp", "a hillside under a vast pale-blue sky",
    "a foggy riverbank at first light", "a bus window on a night drive", "a park bench under bare trees",
    "a balcony overlooking a sleeping city", "a snow-dusted courtyard at blue hour",
]

# One-off characters — a fresh anonymous figure each time, NOT a recurring cast member. Small and
# distant in-frame (the genre's silhouettes read well even on text-only providers).
CHARACTERS = [
    "a lone girl seen from behind", "a young man gazing at the horizon",
    "a small figure with a shoulder bag", "a woman with short dark hair in side profile",
    "a child looking up at the sky", "a person standing alone at a railing",
    "a figure walking away down the road", "someone sitting quietly by a window",
    "a traveller with a backpack, seen small in the scene",
]

# Tiny voice-pace jitter (percent, added to the campaign rate) so narration cadence varies too.
_RATE_JITTER = [-4, -2, 0, 0, 2]


def roll(seed: int, *, character_ratio: float = 0.5) -> dict:
    """Draw one episode's vibe, deterministically from `seed` (so a re-render is identical).

    `character_ratio` is the chance the video features a one-off character (else pure atmosphere).
    Returns {mood, has_character, subject, setting, music_mood, rate_delta} — `subject` is a character
    description when has_character, else None (scenery only)."""
    r = random.Random(seed)
    mood = r.choice(MOODS)
    has_character = r.random() < max(0.0, min(1.0, character_ratio))
    subject = r.choice(CHARACTERS) if has_character else None
    setting = r.choice(SETTINGS)
    # Music search follows the mood (used when the campaign's music mode is Auto).
    music_mood = r.choice([f"{mood} ambient", f"{mood} piano", f"{mood} lofi", f"{mood} cinematic"])
    return {
        "mood": mood,
        "has_character": has_character,
        "subject": subject,
        "setting": setting,
        "music_mood": music_mood,
        "rate_delta": r.choice(_RATE_JITTER),
    }
