"""Quote content style + Vibe Engine units (ADR-056)."""
from __future__ import annotations


# ── Vibe Engine ──────────────────────────────────────────────────────────────
def test_vibe_roll_deterministic_and_shaped():
    from core import vibe

    v = vibe.roll(42)
    assert v["mood"] in vibe.MOODS
    assert v["setting"] in vibe.SETTINGS
    assert isinstance(v["has_character"], bool)
    assert (v["subject"] in vibe.CHARACTERS) if v["has_character"] else (v["subject"] is None)
    assert v["mood"] in v["music_mood"]
    # Deterministic in the seed (a re-render reproduces the same vibe), varied across seeds.
    assert vibe.roll(42) == v
    moods = {vibe.roll(n)["mood"] for n in range(30)}
    assert len(moods) > 3


def test_vibe_character_ratio_bounds():
    from core import vibe

    # ratio 0 → never a character; ratio 1 → always.
    assert all(vibe.roll(n, character_ratio=0.0)["subject"] is None for n in range(20))
    assert all(vibe.roll(n, character_ratio=1.0)["subject"] is not None for n in range(20))


# ── Quote script prompt ──────────────────────────────────────────────────────
def test_build_quote_prompt_carries_vibe_and_asks_for_cover_word():
    from core.ai_engine import build_quote_prompt

    vibe = {"mood": "somber", "subject": "a lone girl seen from behind",
            "setting": "a rain-streaked window at night", "music_mood": "somber piano"}
    p = build_quote_prompt("late-night thoughts", "vi", 3, vibe=vibe,
                           previous_synopses=["a poem about letting go"])
    assert "somber" in p and "rain-streaked window" in p and "lone girl" in p
    assert "cover_word" in p and "10 words" in p            # short poetic lines + the scribble word
    assert "letting go" in p                                # episode memory (no repeats)

    # No character → the prompt says pure atmosphere.
    p2 = build_quote_prompt("t", "en", 1, vibe={"mood": "blue", "subject": None,
                                                "setting": "an empty dusk street"})
    assert "NO people" in p2 or "no people" in p2.lower()


def test_generate_script_quote_uses_quote_prompt(monkeypatch):
    """content_style='quote' routes through the quote prompt (poem), not the story prompt."""
    from core import ai_engine
    from core.ai_engine import VideoScript

    seen = {}

    def fake_generate_structured(*, prompt, schema, **k):
        seen["prompt"] = prompt
        return VideoScript(
            language="en", topic="t", synopsis="a quiet poem", cover_word="CALM",
            scenes=[{"index": i, "narration": f"line {i}", "pexels_keywords": ["dusk street, lone figure"]}
                    for i in range(5)],
            metadata_variations=[{"variant": v, "title": f"T{v}", "description": "d #poetry",
                                  "tags": ["a", "b", "c"]} for v in "ABC"],
        )

    monkeypatch.setattr(ai_engine, "generate_structured", fake_generate_structured)
    script = ai_engine.generate_script(
        topic="late nights", language="en", total_episodes=10, episode=2, api_key="k",
        content_style="quote", vibe={"mood": "melancholic", "subject": None,
                                     "setting": "a foggy riverbank"},
        self_critique=False,
    )
    assert "poem" in seen["prompt"].lower() and "melancholic" in seen["prompt"]
    assert script.cover_word == "CALM"
