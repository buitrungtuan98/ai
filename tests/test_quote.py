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


# ── Render pieces (captions / thumbnail / grade) ─────────────────────────────
def test_build_ass_quote_style_and_signature(tmp_path):
    """Quote style = ONE centered, faded italic caption spanning the clip (not karaoke); signature =
    a small lower-centre mark on every frame."""
    from core.captions import build_ass
    from core.tts import WordTiming

    out = str(tmp_path / "q.ass")
    build_ass([WordTiming("so", 0.0, 0.5), WordTiming("when", 0.5, 1.0), WordTiming("life", 1.0, 2.0)],
              out, clip_duration=4.0, style="quote", signature="@MyChannel")
    txt = open(out, encoding="utf-8").read()
    assert "Style: Quote" in txt and ",5," in txt                 # middle-centre alignment
    assert "Style: Signature" in txt and "@MyChannel" in txt
    assert r"\fad(" in txt                                        # gentle fade on the quote line
    assert txt.count("Dialogue:") == 2                            # one quote line + one signature (no karaoke)
    assert "so when life" in txt                                  # whole line shown at once


def test_thumbnail_scribble_cover_renders(tmp_path, monkeypatch):
    from PIL import Image

    from core import thumbnail

    monkeypatch.setattr(thumbnail, "_select_frame",
                        lambda v, f, frac, d: Image.new("RGB", (400, 700), (40, 55, 60)).save(f))
    out = str(tmp_path / "cover.jpg")
    thumbnail.generate_thumbnail("v.mp4", out, "unused title", scribble_word="calm",
                                 width=360, height=640)
    with Image.open(out) as im:
        assert im.size == (360, 640)


def test_vintage_grade_registered():
    from core.video_factory import COLOR_GRADES

    assert "vintage" in COLOR_GRADES and "vignette" in COLOR_GRADES["vintage"]


def _quote_script(n=5):
    from core.ai_engine import VideoScript

    return VideoScript(
        language="en", topic="late nights", synopsis="a quiet poem about letting go", cover_word="CALM",
        scenes=[{"index": i, "narration": f"line {i}", "pexels_keywords": ["dusk street, lone figure"]}
                for i in range(n)],
        metadata_variations=[{"variant": v, "title": f"T{v}", "description": "d #poetry",
                              "tags": ["a", "b", "c"]} for v in "ABC"],
    )


def test_produce_quote_mode_is_castless_centered_and_scribble(tmp_path, monkeypatch):
    """Quote mode renders via Studio with NO cast: character-less draws, quote captions + signature,
    and a scribble-word cover — all without hard-failing on a missing character."""
    from core import video_factory
    from core.tts import WordTiming

    def fake_tts(text, out, **k):
        open(out, "w").write("a")
        return [WordTiming("w", 0.0, 1.0)]

    monkeypatch.setattr(video_factory.tts, "synthesize_paced", fake_tts)
    monkeypatch.setattr(video_factory, "voice_check", lambda *a, **k: None)
    monkeypatch.setattr(video_factory.media, "probe_duration", lambda p: 5.0)
    monkeypatch.setattr(video_factory, "still_to_clip", lambda img, out, d, profile=None: out)
    monkeypatch.setattr(video_factory, "run_ffmpeg", lambda *a, **k: None)

    ass_cap, thumb_cap = {}, {}
    monkeypatch.setattr(video_factory, "build_ass",
                        lambda t, o, **k: (ass_cap.update(style=k.get("style"), signature=k.get("signature")),
                                           open(o, "w").write("a"))[1])
    monkeypatch.setattr(video_factory, "generate_thumbnail",
                        lambda v, o, title, **k: (thumb_cap.update(scribble=k.get("scribble_word")), o)[1])

    gen_chars = []

    def fake_gen(*, prompt, api_key, out_path, model=None, reference_paths=None, reference_url=None):
        gen_chars.append("SAME character" in prompt)   # False for a character-less quote draw
        open(out_path, "w").write("P")
        return out_path

    result = video_factory.produce(
        script=_quote_script(5), episode_number=1, pexels_api_key="", job_id="q",
        output_dir=str(tmp_path / "o"), visual_source="studio", characters=None,  # NO cast
        image_api_key="k", gen_image=fake_gen, content_style="quote", signature="@Me",
    )
    assert result.scene_count == 5                              # rendered, no cast-required failure
    assert ass_cap["style"] == "quote" and ass_cap["signature"] == "@Me"
    assert thumb_cap["scribble"] == "CALM"                      # scribble cover = the cover word
    assert not any(gen_chars)                                   # every draw was character-less
