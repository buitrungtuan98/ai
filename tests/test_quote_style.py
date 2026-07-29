"""ADR-071 — the aesthetic quote look and its intimate read.

The reference style is retro 80s/90s anime stills, sepia/burnt-orange with film grain, one poem line
at a time, lofi piano underneath, read as if someone were confiding in you. Quote mode had the
skeleton of that (drawn scenes, centred lines, a per-episode Vibe roll) but three of the six settings
that make it recognisable were unreachable, off by default, or simply not built.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


# ── Q1: the vintage grade exists AND can be chosen ───────────────────────────
def test_the_vintage_grade_is_selectable_at_last():
    """It was written for ADR-056 and rendered correctly, but the create-time whitelist and the form's
    dropdown were hand-maintained copies of the grade list — neither learned about it, so no campaign
    could ever select it."""
    from core.video_factory import COLOR_GRADE_CHOICES, COLOR_GRADES

    assert "vintage" in COLOR_GRADES and "vintage" in COLOR_GRADE_CHOICES
    # Every selectable grade must have a filter — the assertion that makes the drift impossible.
    assert set(COLOR_GRADE_CHOICES) <= set(COLOR_GRADES)


def test_the_vintage_filter_is_the_look_the_operator_asked_for():
    from core.video_factory import COLOR_GRADES

    f = COLOR_GRADES["vintage"]
    assert "noise=" in f and "allf=t" in f      # film grain, and MOVING (static grain = dirty lens)
    assert "vignette" in f
    assert "saturation=0.8" in f                # muted, not punchy
    assert "rs=0.07" in f and "bs=-0.08" in f   # warm sepia / burnt orange, blues pulled out


def test_a_campaign_can_actually_save_the_vintage_grade(client, session, user, channel):
    from database.models import Campaign

    r = client.post("/campaigns", data={
        "topic_name": "Quote Ch", "channel_id": channel.id, "total_episodes": 5,
        "language": "vi", "content_style": "quote", "color_grade": "vintage",
    }, follow_redirects=False)
    assert r.status_code == 303
    camp = session.scalars(_select_campaigns()).first()
    assert isinstance(camp, Campaign)
    assert camp.config_json["color_grade"] == "vintage"


def test_an_invented_grade_is_still_rejected(client, session, user, channel):
    """The whitelist got wider, not absent."""
    client.post("/campaigns", data={
        "topic_name": "X", "channel_id": channel.id, "total_episodes": 5,
        "color_grade": "sparkles",
    }, follow_redirects=False)
    assert session.scalars(_select_campaigns()).first().config_json["color_grade"] is None


def test_the_form_offers_every_grade_from_the_one_catalog(client, channel):
    from core.video_factory import COLOR_GRADE_CHOICES

    body = client.get("/campaigns/new").text
    for grade in COLOR_GRADE_CHOICES:
        assert f'value="{grade}"' in body


def _select_campaigns():
    from sqlalchemy import select

    from database.models import Campaign

    return select(Campaign)


# ── Q2: the art-style preset is the retro-anime look ─────────────────────────
def test_the_first_preset_describes_retro_80s_90s_anime(client, channel):
    """It is the preset Quote auto-fills, so it IS the channel look for most quote campaigns."""
    body = client.get("/campaigns/new").text
    preset = body.split("data-style=\"", 1)[1].split("\"", 1)[0].lower()
    for phrase in ("1980s", "anime", "sepia", "grain"):
        assert phrase in preset, f"the quote preset should mention {phrase}"
    assert "burnt-orange" in preset or "burnt orange" in preset


# ── Q3: the soft, confiding read ─────────────────────────────────────────────
def test_soft_delivery_slows_and_lowers_the_voice():
    from core import tts

    rate, pitch = tts._delivery_params("soft", "vi-VN-HoaiMyNeural", 0)
    assert rate == -12                    # confiding, not announcing
    assert pitch == "-18Hz"

    # A campaign's own rate is respected, not replaced.
    rate2, _ = tts._delivery_params("soft", "vi-VN-HoaiMyNeural", 5)
    assert rate2 == -7


def test_a_deep_voice_is_barely_pitched_down():
    """Dropping an already-deep voice as far as a bright one reads as muddy and unwell, not intimate."""
    from core import tts

    _, deep = tts._delivery_params("soft", "en-US-ChristopherNeural", 0)
    _, bright = tts._delivery_params("soft", "en-US-JennyNeural", 0)
    assert int(deep.rstrip("Hz")) > int(bright.rstrip("Hz"))


def test_a_normal_read_is_untouched():
    from core import tts

    assert tts._delivery_params("normal", "vi-VN-HoaiMyNeural", 3) == (3, "+0Hz")


def test_the_soft_filter_chain_earns_every_stage():
    from core import tts

    f = tts.SOFT_VOICE_FILTER
    assert "highpass" in f       # a pitch shift exaggerates low rumble
    assert "lowpass" in f        # takes the edge off sibilance → consonants read as breath
    assert "acompressor" in f    # what makes a quiet, close read audible without shouting
    assert "aecho" in f          # a small room, not an echo
    # Nothing that changes duration: the edge-tts word timings drive the captions.
    for stretcher in ("atempo", "asetrate", "rubberband"):
        assert stretcher not in f


def test_the_soft_pass_is_a_plain_ffmpeg_reencode():
    from core import tts

    args = tts.build_soft_voice_args("/in.mp3", "/out.mp3")
    assert args[:2] == ["-i", "/in.mp3"]
    assert "-af" in args and tts.SOFT_VOICE_FILTER in args
    assert args[-1] == "/out.mp3"


def test_the_soft_pass_runs_once_on_the_assembled_narration(tmp_path, monkeypatch):
    """Not per sentence: a compressor applied six times over is a different effect, and it would cost
    six extra ffmpeg passes per scene."""
    from core import tts

    calls = {"synth": 0, "soft": 0}

    def fake_raw(text, out, voice, rate, pitch):
        calls["synth"] += 1
        open(out, "w").close()
        return [tts.WordTiming(text.split()[0], 0.0, 1.0)]

    monkeypatch.setattr(tts, "_synthesize_raw", fake_raw)
    monkeypatch.setattr(tts, "apply_soft_voice", lambda p: calls.__setitem__("soft", calls["soft"] + 1))
    monkeypatch.setattr("core.media.probe_duration", lambda p: 1.0)
    monkeypatch.setattr("core.ffmpeg_runner.run_ffmpeg", lambda *a, **k: None)

    tts.synthesize_paced("Alpha here. Beta there. Gamma too.", str(tmp_path / "o.mp3"),
                         language="vi", delivery="soft")
    assert calls["synth"] == 3 and calls["soft"] == 1


def test_a_normal_campaign_never_gets_the_soft_pass(tmp_path, monkeypatch):
    from core import tts

    softs = []
    monkeypatch.setattr(tts, "_synthesize_raw",
                        lambda text, out, voice, rate, pitch: (open(out, "w").close(), [])[1])
    monkeypatch.setattr(tts, "apply_soft_voice", lambda p: softs.append(p))
    monkeypatch.setattr("core.media.probe_duration", lambda p: 1.0)
    monkeypatch.setattr("core.ffmpeg_runner.run_ffmpeg", lambda *a, **k: None)

    tts.synthesize_paced("One. Two.", str(tmp_path / "o.mp3"), language="vi")
    assert softs == []


def test_soft_writes_via_a_temp_file(tmp_path, monkeypatch):
    """A failed pass must never leave a half-written narration where the render expects a finished one."""
    from core import tts

    src = tmp_path / "n.mp3"
    src.write_bytes(b"original")
    seen = {}

    def fake_run(args, **k):
        seen["out"] = args[-1]
        open(args[-1], "wb").write(b"softened")

    monkeypatch.setattr("core.ffmpeg_runner.run_ffmpeg", fake_run)
    tts.apply_soft_voice(str(src))
    assert seen["out"].endswith(".soft.mp3") and seen["out"] != str(src)
    assert src.read_bytes() == b"softened"
    assert not (tmp_path / "n.mp3.soft.mp3").exists()


def test_the_delivery_reaches_the_render_from_the_campaign(session, user, channel, monkeypatch):
    from core.video_factory import RenderResult
    from database.models import Campaign, Task
    from database.types import CampaignStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Q", current_episode=0,
                   total_episodes=1, status=CampaignStatus.active,
                   config_json={"language": "vi", "voice_delivery": "soft", "auto_qc": "off"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)

    captured: dict = {}
    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script())
    monkeypatch.setattr(video_worker.video_factory, "produce",
                        lambda **k: captured.update(k) or RenderResult(
                            master_path="/no/m.mp4", thumbnail_path="/no/t.jpg",
                            metadata={"title": "T", "variant": "A"}, duration=5.0, scene_count=1))
    monkeypatch.setattr(video_worker, "_publish", lambda *a, **k: "vid-1")
    video_worker.render_task(t.id)
    assert captured["voice_delivery"] == "soft"


def _script():
    from core.ai_engine import VideoScript

    return VideoScript(
        language="vi", topic="Q", synopsis="s",
        scenes=[{"index": i, "narration": "n", "pexels_keywords": ["k"]} for i in range(3)],
        metadata_variations=[{"variant": v, "title": f"T{v}", "description": "d",
                              "tags": ["a", "b", "c"]} for v in "ABC"],
    )


def test_the_form_is_honest_about_what_soft_is_not(client, channel):
    """edge-tts has no whispering style; Azure's paid one has no Vietnamese or Spanish voice at all.
    Promising a whisper we cannot synthesize would be the dishonest kind of feature copy."""
    body = client.get("/campaigns/new").text
    assert 'name="voice_delivery"' in body
    assert "not a real whisper" in body


# ── Q4 + the rest of the auto-tune: Quote agrees with itself ─────────────────
def test_picking_quote_tunes_every_setting_the_genre_needs(client, channel):
    """Six settings have to agree, and a video missing any one of them just looks like a normal
    narrated video with centred text."""
    body = client.get("/campaigns/new").text
    tune = body.split("if (isQuote()) {", 1)[1].split("applyContentVisual();", 1)[0]
    assert 'grade.value = "vintage"' in tune              # film grain + sepia
    assert 'delivery.value = "soft"' in tune              # the confiding read
    assert 'music.value = "auto"' in tune                 # lofi/piano from the Vibe roll
    assert 'mvol.value = "0.2"' in tune                   # the voice sits inside the bed
    assert "SOFT_VOICES" in tune                          # a 🌙 voice for the language
    assert "preset.dataset.style" in tune                 # the retro-anime look


def test_the_auto_tune_never_overrides_a_deliberate_choice(client, channel):
    """Every branch is guarded on the field being unset — an operator who chose Noir keeps Noir."""
    body = client.get("/campaigns/new").text
    tune = body.split("if (isQuote()) {", 1)[1].split("applyContentVisual();", 1)[0]
    assert "if (grade && !grade.value)" in tune
    assert 'if (music && music.value === "none")' in tune
    assert "if (voiceSel && !voiceSel.value)" in tune


# ── Q5: soft voices per language, including the foreign campaigns ────────────
def test_every_language_has_curated_soft_voices():
    """A foreign-language quote campaign needs this as much as a Vietnamese one — the same soft
    processing over an announcer read sounds wrong, not intimate."""
    from core.tts import QUOTE_VOICES, VOICE_CHOICES

    for lang in ("vi", "en", "es"):
        assert QUOTE_VOICES.get(lang), f"no soft voices curated for {lang}"
        catalog = {vid for vid, _ in VOICE_CHOICES[lang]}
        assert set(QUOTE_VOICES[lang]) <= catalog, f"{lang}: soft voice outside the catalog"


def test_the_energetic_voices_are_deliberately_excluded():
    from core.tts import QUOTE_VOICES

    flat = {v for vs in QUOTE_VOICES.values() for v in vs}
    for loud in ("en-US-AriaNeural", "en-US-GuyNeural", "es-ES-ElviraNeural"):
        assert loud not in flat


def test_every_soft_voice_has_a_pitch_tuned_for_it():
    """The default exists as a safety net, but a curated voice should not need it."""
    from core import tts

    for vs in tts.QUOTE_VOICES.values():
        for vid in vs:
            assert vid in tts._SOFT_PITCH_HZ, f"{vid} has no tuned soft pitch"


def test_the_picker_marks_the_soft_voices(client, channel):
    body = client.get("/campaigns/new").text
    assert "SOFT_VOICES" in body and "🌙" in body


def test_the_designer_only_proposes_soft_voices_for_a_quote_campaign(monkeypatch):
    from core import ai_engine
    from core.tts import QUOTE_VOICES

    seen = {}

    class FakeProposal:
        voice = "en-US-GuyNeural"        # energetic — must be refused for a quote campaign
        video_format = "short"
        duration_min_s = 20
        duration_max_s = 40
        posting_slots = "21:00"
        posting_days: list[str] = []

    def fake_structured(*, prompt, schema, api_key, model, temperature):
        seen["prompt"] = prompt
        return FakeProposal()

    monkeypatch.setattr(ai_engine, "generate_structured", fake_structured)
    p = ai_engine.propose_campaign(topic="quiet nights", language="en", api_key="k",
                                   content_style="quote")
    assert p.voice == QUOTE_VOICES["en"][0]          # fell back to a curated soft voice
    # The prompt only offered the soft pool, and asked for the right read + music family.
    assert "en-US-GuyNeural" not in seen["prompt"]
    assert "en-US-JennyNeural" in seen["prompt"]
    assert "whispered" in seen["prompt"] and "lofi" in seen["prompt"]


def test_a_story_campaign_still_gets_the_whole_catalog(monkeypatch):
    from core import ai_engine

    seen = {}

    class FakeProposal:
        voice = "en-US-GuyNeural"
        video_format = "short"
        duration_min_s = 20
        duration_max_s = 40
        posting_slots = "21:00"
        posting_days: list[str] = []

    monkeypatch.setattr(ai_engine, "generate_structured",
                        lambda **k: (seen.update(prompt=k["prompt"]), FakeProposal())[1])
    p = ai_engine.propose_campaign(topic="space facts", language="en", api_key="k")
    assert p.voice == "en-US-GuyNeural"      # an energetic voice is fine for a narrated video
    assert "whispered" not in seen["prompt"]


def test_the_propose_endpoint_forwards_the_content_style(client, session, user, channel, monkeypatch):
    from core import ai_engine

    seen = {}

    class FakeProposal:
        topic_name = "T"
        total_episodes = 5
        language = "vi"
        voice = "vi-VN-HoaiMyNeural"
        rate_pct = 0
        video_format = "short"
        duration_min_s = 20
        duration_max_s = 40
        persona = style_examples = catchphrase_open = catchphrase_close = ""
        continuity = "none"
        script_depth = "standard"
        subtitle_style = "word"
        caption_theme = "highlight"
        motion = "on"
        color_grade = "vintage"
        music_mode = "auto"
        music_mood = "melancholic piano"
        privacy = "public"
        cta = title_prefix = ""
        posting_slots = "21:00"
        posting_days: list[str] = []
        ab_testing = False
        rationale = "r"

        def model_dump(self):      # the route returns the proposal as JSON
            return {"topic_name": self.topic_name, "voice": self.voice,
                    "color_grade": self.color_grade, "music_mode": self.music_mode}

    def fake_propose(**kw):
        seen.update(kw)
        return FakeProposal()

    monkeypatch.setattr(ai_engine, "propose_campaign", fake_propose)
    r = client.post("/campaigns/propose", data={"topic": "night", "language": "vi",
                                                "content_style": "quote"})
    assert r.status_code == 200
    assert seen["content_style"] == "quote"
