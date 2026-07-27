"""Studio Mode units (ADR-052): character selection, prompt building, the image primitive, and the
still→clip arg builder. All pure/mockable — no Gemini key and no ffmpeg needed (the real ffmpeg
still→clip→scene path is covered by the ffmpeg-gated integration test)."""
from __future__ import annotations

import types

import pytest

CAST = [
    {"id": "a", "name": "Pencil", "description": "a cheerful stickman", "style": "pencil sketch", "sheet_path": None},
    {"id": "b", "name": "Owl", "description": "a wise cartoon owl", "style": "flat vector", "sheet_path": None},
    {"id": "c", "name": "Rex", "description": "a friendly dinosaur", "style": "", "sheet_path": None},
]


# ── character selection ──────────────────────────────────────────────────────
def test_pick_character_deterministic_and_filters():
    from core import studio

    # Deterministic in the seed (so a re-render reuses the same character + cached sheet).
    picks = {studio.pick_character(CAST, seed=n)["id"] for n in range(20)}
    assert picks <= {"a", "b", "c"} and len(picks) > 1          # varies across episodes
    assert studio.pick_character(CAST, seed=7)["id"] == studio.pick_character(CAST, seed=7)["id"]

    # Malformed entries (non-dict, blank name) are dropped; an empty cast yields None.
    messy = [*CAST, {"name": "  "}, "not a dict", {"id": "x"}]
    assert studio.pick_character(messy, seed=1)["id"] in {"a", "b", "c"}
    assert studio.pick_character([], seed=0) is None
    assert studio.pick_character(None, seed=0) is None


def test_style_resolution_priority():
    from core import studio

    char = CAST[0]  # own style "pencil sketch"
    assert studio._style_for(char, "noir ink") == "noir ink"          # campaign override wins
    assert studio._style_for(char, "") == "pencil sketch"             # else the character's own style
    assert studio._style_for(CAST[2], None) == studio._BASE_STYLE     # else the house base style


# ── prompt building ──────────────────────────────────────────────────────────
def test_prompts_carry_identity_style_and_beat_direction():
    from core import studio

    sheet = studio.character_sheet_prompt(CAST[0], "noir ink")
    assert "Pencil" in sheet and "cheerful stickman" in sheet and "noir ink" in sheet
    assert "reference sheet" in sheet.lower() and "do not draw any text" in sheet.lower()

    scene = studio.scene_prompt(CAST[0], "a rocket launch, night sky", mood="Did you know…")
    assert "Pencil" in scene and "reference image" in scene.lower()
    assert "rocket launch" in scene
    # The operator's wishlist is baked into every beat: motion cues + a no-text instruction.
    assert "motion blur" in scene.lower() and "action lines" in scene.lower()
    assert "pencil sketch" in scene.lower()  # falls back to the character's own style


# ── sheet + scene generation (injected gen_image, no key) ────────────────────
def test_character_sheet_caches_and_calls_generator(tmp_path):
    from core import studio

    calls = []

    def fake_gen(*, prompt, api_key, out_path, model=None, reference_paths=None, reference_url=None):
        calls.append(prompt)
        open(out_path, "w").write("PNG")
        return out_path

    out = str(tmp_path / "sheet.png")
    p1 = studio.character_sheet(CAST[0], api_key="k", out_path=out, gen_image=fake_gen)
    assert p1 == out and len(calls) == 1
    # A second call reuses the cached file on disk — the sheet defines the character, must not drift.
    p2 = studio.character_sheet(CAST[0], api_key="k", out_path=out, gen_image=fake_gen)
    assert p2 == out and len(calls) == 1


def test_scene_visual_forwards_references(tmp_path):
    from core import studio

    seen = {}

    def fake_gen(*, prompt, api_key, out_path, model=None, reference_paths=None, reference_url=None):
        seen["prompt"] = prompt
        seen["refs"] = reference_paths
        seen["model"] = model
        return out_path

    studio.scene_visual(
        character=CAST[1], subject="an owl reading", api_key="k",
        out_path=str(tmp_path / "s.png"), reference_paths=["sheet.png", "prev.png"],
        model="img-x", gen_image=fake_gen,
    )
    assert seen["refs"] == ["sheet.png", "prev.png"] and seen["model"] == "img-x"
    assert "owl reading" in seen["prompt"] and "Owl" in seen["prompt"]


# ── the image primitive (mock the raw SDK call) ──────────────────────────────
def test_extract_image_bytes():
    from core.ai_engine import _extract_image_bytes

    inline = types.SimpleNamespace(data=b"\x89PNG-bytes")
    part = types.SimpleNamespace(inline_data=inline)
    text_part = types.SimpleNamespace(inline_data=None)
    cand = types.SimpleNamespace(content=types.SimpleNamespace(parts=[text_part, part]))
    resp = types.SimpleNamespace(candidates=[cand])
    assert _extract_image_bytes(resp) == b"\x89PNG-bytes"

    empty = types.SimpleNamespace(candidates=[])
    assert _extract_image_bytes(empty) is None


def test_generate_image_writes_and_falls_back_on_quota(tmp_path, monkeypatch):
    from core import ai_engine

    monkeypatch.setattr(ai_engine, "_BACKOFF_BASE_SECONDS", 0)
    seen_models = []

    def fake_call(*, api_key, model, prompt, reference_paths, out_path):
        seen_models.append(model)
        if model == "img-a":
            raise RuntimeError("429 ... quota_id ...PerDay... exhausted")
        open(out_path, "wb").write(b"IMG")
        return out_path

    monkeypatch.setattr(ai_engine, "_call_gemini_image", fake_call)
    out = str(tmp_path / "o.png")
    res = ai_engine.generate_image(prompt="draw", api_key="k", out_path=out, model="img-a,img-b")
    assert res == out and open(out, "rb").read() == b"IMG"
    assert seen_models == ["img-a", "img-b"]   # spent daily quota → fell over to the next model


def test_generate_image_block_is_not_retried_or_fallen_back(tmp_path, monkeypatch):
    from core import ai_engine
    from core.ai_engine import GeminiBlockedError

    monkeypatch.setattr(ai_engine, "_BACKOFF_BASE_SECONDS", 0)
    calls = []

    def fake_call(**k):
        calls.append(k["model"])
        raise GeminiBlockedError("blocked")

    monkeypatch.setattr(ai_engine, "_call_gemini_image", fake_call)
    with pytest.raises(GeminiBlockedError):
        ai_engine.generate_image(prompt="p", api_key="k", out_path=str(tmp_path / "o.png"),
                                 model="img-a,img-b")
    assert calls == ["img-a"]   # a content block is terminal — no retry, no fallback


# ── still → clip arg builder (no ffmpeg) ─────────────────────────────────────
def test_still_to_clip_args(monkeypatch):
    from core import video_factory
    from core.video_factory import LONG_PROFILE

    captured = {}
    monkeypatch.setattr(video_factory, "run_ffmpeg", lambda args, **k: captured.update(args=list(args)))

    out = video_factory.still_to_clip("frame.png", "clip.mp4", 5.0)
    a = captured["args"]
    assert out == "clip.mp4" and a[-1] == "clip.mp4"
    assert a[a.index("-loop") + 1] == "1"
    assert a[a.index("-i") + 1] == "frame.png"
    assert "-an" in a                                   # silent — audio comes from TTS downstream
    assert abs(float(a[a.index("-t") + 1]) - 5.3) < 0.01  # padded so the downstream -t trim never overshoots
    vf = a[a.index("-vf") + 1]
    assert "1080" in vf and "1920" in vf                # vertical (short) geometry by default

    # Long profile → 16:9 geometry.
    video_factory.still_to_clip("f.png", "c.mp4", 2.0, profile=LONG_PROFILE)
    vf2 = captured["args"][captured["args"].index("-vf") + 1]
    assert "1920" in vf2 and "1080" in vf2


# ── produce() Studio orchestration (fully mocked — no ffmpeg, no key) ─────────
def _studio_script(n=3):
    from core.ai_engine import VideoScript

    return VideoScript(
        language="en", topic="Robots", synopsis="Robots learn to dream",
        scenes=[{"index": i, "narration": f"scene {i}", "pexels_keywords": [f"k{i}"]} for i in range(n)],
        metadata_variations=[{"variant": v, "title": f"T{v}", "description": "d", "tags": ["a", "b", "c"]}
                             for v in "ABC"],
    )


def test_produce_studio_chains_references_and_skips_pexels(tmp_path, monkeypatch):
    """The Studio branch of produce(): draws the sheet ONCE, then one keyframe per scene, chaining the
    previous frame in as a reference for temporal continuity — and never touches Pexels."""
    from core import video_factory
    from core.tts import WordTiming

    # Stub every ffmpeg/TTS/thumbnail touchpoint so only the orchestration logic runs.
    def fake_tts(text, out, **k):
        open(out, "w").write("audio")
        return [WordTiming("w", 0.0, 1.0)]

    monkeypatch.setattr(video_factory.tts, "synthesize_paced", fake_tts)
    monkeypatch.setattr(video_factory, "voice_check", lambda *a, **k: None)
    monkeypatch.setattr(video_factory.media, "probe_duration", lambda p: 5.0)
    monkeypatch.setattr(video_factory, "still_to_clip", lambda img, out, d, profile=None: out)
    monkeypatch.setattr(video_factory, "build_ass", lambda *a, **k: open(a[1], "w").write("ass"))
    monkeypatch.setattr(video_factory, "run_ffmpeg", lambda *a, **k: None)
    monkeypatch.setattr(video_factory, "pick_metadata",
                        lambda *a, **k: {"title": "T", "variant": "A", "description": "d"})
    monkeypatch.setattr(video_factory, "generate_thumbnail", lambda *a, **k: None)
    # Pexels must never be called in Studio Mode.
    monkeypatch.setattr(video_factory.pexels, "download",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Pexels touched in Studio")))
    monkeypatch.setattr(video_factory, "search_footage",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Pexels searched in Studio")))

    gen_calls = []

    def fake_gen(*, prompt, api_key, out_path, model=None, reference_paths=None, reference_url=None):
        gen_calls.append({"out": out_path, "refs": reference_paths})
        open(out_path, "w").write("PNG")
        return out_path

    cast = [{"id": "hero", "name": "Hero", "description": "a hero", "style": "ink", "sheet_path": None}]
    result = video_factory.produce(
        script=_studio_script(3), episode_number=1, pexels_api_key="", job_id="studiojob",
        output_dir=str(tmp_path / "out"), visual_source="studio", characters=cast,
        image_api_key="gkey", image_model="img-x", studio_sheet_dir=str(tmp_path / "sheets"),
        gen_image=fake_gen, motion=True,
    )

    assert result.scene_count == 3 and result.used_clip_ids == []      # drawn, not stock
    # 1 sheet draw + 3 scene draws; the sheet is drawn exactly once (cached for later scenes).
    assert len(gen_calls) == 4
    sheet_calls = [c for c in gen_calls if c["refs"] is None]
    scene_calls = [c for c in gen_calls if c["refs"] is not None]
    assert len(sheet_calls) == 1 and len(scene_calls) == 3
    sheet_path = sheet_calls[0]["out"]
    assert sheet_path.endswith("hero.png")                              # stable per-character cache
    # Reference chaining: scene 0 → [sheet]; scene 1 → [sheet, still0]; scene 2 → [sheet, still1].
    assert scene_calls[0]["refs"] == [sheet_path]
    assert scene_calls[1]["refs"] == [sheet_path, scene_calls[0]["out"]]
    assert scene_calls[2]["refs"] == [sheet_path, scene_calls[1]["out"]]


# ── Pollinations image provider + provider chain (ADR-053) ───────────────────
def test_pollinations_seed_is_deterministic_per_prompt():
    from core.ai_engine import _pollinations_seed

    assert _pollinations_seed("same prompt") == _pollinations_seed("same prompt")
    assert _pollinations_seed("scene a") != _pollinations_seed("scene b")   # scenes vary
    assert 0 <= _pollinations_seed("x") < 1_000_000


def test_call_pollinations_builds_request_and_writes(tmp_path, monkeypatch):
    import requests

    from core import ai_engine

    captured = {}

    class FakeResp:
        status_code = 200
        headers = {"content-type": "image/jpeg"}
        content = b"IMGDATA"

        def raise_for_status(self):
            pass

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResp()

    monkeypatch.setattr(requests, "get", fake_get)
    out = str(tmp_path / "o.png")
    res = ai_engine._call_pollinations(model="flux", prompt="a hero jumping", out_path=out,
                                       token="tok", width=1080, height=1920, seed=42)
    assert res == out and open(out, "rb").read() == b"IMGDATA"
    assert "image.pollinations.ai/prompt/" in captured["url"] and "a%20hero%20jumping" in captured["url"]
    p = captured["params"]
    assert p["model"] == "flux" and p["width"] == 1080 and p["height"] == 1920 and p["seed"] == 42
    assert p["token"] == "tok" and p["safe"] == "true" and p["nologo"] == "true"


def test_call_pollinations_rejects_non_image(tmp_path, monkeypatch):
    import requests

    from core import ai_engine
    from core.ai_engine import ImageGenError

    class FakeResp:
        status_code = 200
        headers = {"content-type": "text/html"}   # a blocked/failed prompt returns an error page
        content = b"<html>no</html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp())
    with pytest.raises(ImageGenError):
        ai_engine._call_pollinations(model="flux", prompt="x", out_path=str(tmp_path / "o.png"),
                                     token=None, width=10, height=10, seed=1)


def test_generate_image_dispatches_to_pollinations(tmp_path, monkeypatch):
    from core import ai_engine

    seen = {}

    def fake_poll(*, model, prompt, out_path, token, width, height, seed, reference_url=None):
        seen.update(model=model, token=token, width=width, height=height)
        open(out_path, "wb").write(b"P")
        return out_path

    monkeypatch.setattr(ai_engine, "_call_pollinations", fake_poll)
    monkeypatch.setattr(ai_engine, "_call_gemini_image",
                        lambda **k: (_ for _ in ()).throw(AssertionError("Gemini called for a pollinations entry")))
    out = str(tmp_path / "o.png")
    res = ai_engine.generate_image(prompt="p", api_key="k", out_path=out, model="pollinations:flux",
                                   pollinations_token="tok", width=720, height=1280)
    assert res == out and seen == {"model": "flux", "token": "tok", "width": 720, "height": 1280}


def test_generate_image_falls_back_gemini_to_pollinations(tmp_path, monkeypatch):
    from core import ai_engine

    monkeypatch.setattr(ai_engine, "_BACKOFF_BASE_SECONDS", 0)

    def dead_gemini(**k):
        raise RuntimeError("500 transient server error")   # → GeminiError after retries

    def fake_poll(*, model, prompt, out_path, token, width, height, seed, reference_url=None):
        open(out_path, "wb").write(b"P")
        return out_path

    monkeypatch.setattr(ai_engine, "_call_gemini_image", dead_gemini)
    monkeypatch.setattr(ai_engine, "_call_pollinations", fake_poll)
    out = str(tmp_path / "o.png")
    res = ai_engine.generate_image(prompt="p", api_key="k", out_path=out,
                                   model="gemini-2.5-flash-image,pollinations:flux")
    assert res == out and open(out, "rb").read() == b"P"    # Google down → drew for free on Pollinations


def test_generate_image_pollinations_primary_skips_gemini(tmp_path, monkeypatch):
    from core import ai_engine

    def fake_poll(*, model, prompt, out_path, token, width, height, seed, reference_url=None):
        open(out_path, "wb").write(b"P")
        return out_path

    monkeypatch.setattr(ai_engine, "_call_pollinations", fake_poll)
    monkeypatch.setattr(ai_engine, "_call_gemini_image",
                        lambda **k: (_ for _ in ()).throw(AssertionError("Gemini called though Pollinations is primary")))
    out = str(tmp_path / "o.png")
    ai_engine.generate_image(prompt="p", api_key="k", out_path=out,
                             model="pollinations:flux,gemini-2.5-flash-image")
    assert open(out, "rb").read() == b"P"


def test_generate_image_block_does_not_reroute_to_pollinations(tmp_path, monkeypatch):
    from core import ai_engine
    from core.ai_engine import GeminiBlockedError

    monkeypatch.setattr(ai_engine, "_BACKOFF_BASE_SECONDS", 0)
    poll_calls = []
    monkeypatch.setattr(ai_engine, "_call_gemini_image",
                        lambda **k: (_ for _ in ()).throw(GeminiBlockedError("blocked")))
    monkeypatch.setattr(ai_engine, "_call_pollinations", lambda **k: poll_calls.append(1))
    with pytest.raises(GeminiBlockedError):
        ai_engine.generate_image(prompt="p", api_key="k", out_path=str(tmp_path / "o.png"),
                                 model="gemini-2.5-flash-image,pollinations:flux")
    assert not poll_calls   # unsafe content is terminal — never rerouted to another provider


# ── Pollinations reference image via kontext (ADR-055) ───────────────────────
def test_call_pollinations_kontext_uses_image_but_flux_ignores(tmp_path, monkeypatch):
    import requests

    from core import ai_engine

    captured: dict = {}

    class FakeResp:
        status_code = 200
        headers = {"content-type": "image/png"}
        content = b"IMG"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda url, params=None, timeout=None: (
        captured.update(params=dict(params)), FakeResp())[1])

    # An image-editing model gets the reference passed as `image=`.
    ai_engine._call_pollinations(model="kontext", prompt="p", out_path=str(tmp_path / "a.png"),
                                 token=None, width=1080, height=1920, seed=1,
                                 reference_url="https://f.example/studio/ref/abc")
    assert captured["params"].get("image") == "https://f.example/studio/ref/abc"

    # A text-only model ignores it (flux can't do image-to-image).
    ai_engine._call_pollinations(model="flux", prompt="p", out_path=str(tmp_path / "b.png"),
                                 token=None, width=1080, height=1920, seed=1,
                                 reference_url="https://f.example/studio/ref/abc")
    assert "image" not in captured["params"]


def test_pollinations_token_never_leaks_into_error(tmp_path, monkeypatch):
    """A failing Pollinations request must NOT put the token (a secret) into the raised error/logs."""
    import requests

    from core import ai_engine
    from core.ai_engine import ImageGenError

    def boom(url, params=None, timeout=None):
        raise requests.RequestException(
            f"500 Server Error for url: https://image.pollinations.ai/prompt/x?token={params['token']}")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(ImageGenError) as ei:
        ai_engine._call_pollinations(model="kontext", prompt="p", out_path=str(tmp_path / "o.png"),
                                     token="sk_supersecret123", width=1080, height=1920, seed=1,
                                     reference_url="https://f.example/ref/a")
    assert "sk_supersecret123" not in str(ei.value) and "REDACTED" in str(ei.value)


def test_generate_image_safety_net_flux_when_chain_dies(tmp_path, monkeypatch):
    """When every chain entry fails and Pollinations was in the chain, a last-resort text-only flux
    draw keeps the episode rendering instead of hard-failing."""
    from core import ai_engine
    from core.ai_engine import ImageGenError

    monkeypatch.setattr(ai_engine, "_BACKOFF_BASE_SECONDS", 0)
    seen = []

    def flaky_call(*, model, prompt, out_path, token, width, height, seed, reference_url=None):
        seen.append(model)
        if model == "kontext":
            raise ImageGenError("500 kontext")   # kontext keeps 500-ing (e.g. blocked image fetch)
        open(out_path, "wb").write(b"P")           # flux net succeeds
        return out_path

    monkeypatch.setattr(ai_engine, "_call_pollinations", flaky_call)
    out = str(tmp_path / "o.png")
    res = ai_engine.generate_image(prompt="p", api_key="k", out_path=out,
                                   model="pollinations:kontext", reference_url="https://f.example/ref/a")
    assert res == out and open(out, "rb").read() == b"P"
    assert "kontext" in seen and seen[-1] == "flux"   # kontext failed → flux safety net drew the scene


def test_generate_image_no_safety_net_for_gemini_only(tmp_path, monkeypatch):
    """A Gemini-only chain that fails is NOT silently rerouted to Pollinations (the operator didn't
    opt into it)."""
    from core import ai_engine
    from core.ai_engine import GeminiError

    monkeypatch.setattr(ai_engine, "_BACKOFF_BASE_SECONDS", 0)
    monkeypatch.setattr(ai_engine, "_call_gemini_image",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("500 down")))
    poll = []
    monkeypatch.setattr(ai_engine, "_call_pollinations", lambda **k: poll.append(1))
    with pytest.raises(GeminiError):
        ai_engine.generate_image(prompt="p", api_key="k", out_path=str(tmp_path / "o.png"),
                                 model="gemini-2.5-flash-image")
    assert not poll   # no Pollinations entry → no flux net


def test_kontext_without_reference_degrades_to_flux(tmp_path, monkeypatch):
    """An image-editing model with no reference URL (no upload / PUBLIC_BASE_URL unset) must degrade
    to text-only flux — NOT 500 the render by calling kontext with no input image."""
    from core import ai_engine

    seen: dict = {}

    def fake_call(*, model, prompt, out_path, token, width, height, seed, reference_url=None):
        seen["model"] = model
        open(out_path, "wb").write(b"P")
        return out_path

    monkeypatch.setattr(ai_engine, "_call_pollinations", fake_call)
    ai_engine.generate_image(prompt="p", api_key="k", out_path=str(tmp_path / "o.png"),
                             model="pollinations:kontext")   # no reference_url
    assert seen["model"] == "flux"   # degraded — the render succeeds instead of failing


def test_generate_image_kontext_forwards_reference_url(tmp_path, monkeypatch):
    from core import ai_engine

    seen: dict = {}

    def fake_poll(*, model, prompt, out_path, token, width, height, seed, reference_url=None):
        seen.update(model=model, ref_url=reference_url)
        open(out_path, "wb").write(b"P")
        return out_path

    monkeypatch.setattr(ai_engine, "_call_pollinations", fake_poll)
    ai_engine.generate_image(prompt="p", api_key="k", out_path=str(tmp_path / "o.png"),
                             model="pollinations:kontext", reference_url="https://f.example/studio/ref/tok")
    assert seen["model"] == "kontext" and seen["ref_url"] == "https://f.example/studio/ref/tok"


def test_cast_with_ref_urls(monkeypatch):
    from core.config import settings
    from workers import video_worker

    cast = [{"id": "a", "name": "A", "ref_token": "deadbeef01"}, {"id": "b", "name": "B", "ref_token": None}]
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://factory.example.com")
    out = video_worker._cast_with_ref_urls(cast)
    assert out[0]["ref_url"] == "https://factory.example.com/studio/ref/deadbeef01"
    assert "ref_url" not in out[1]                              # no token → no public url
    # A non-public base (dev localhost) is skipped — Pollinations couldn't reach it anyway.
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "http://127.0.0.1:8000")
    assert "ref_url" not in video_worker._cast_with_ref_urls(cast)[0]
    assert video_worker._cast_with_ref_urls(None) is None


def _mock_render_touchpoints(video_factory, monkeypatch):
    """Stub every ffmpeg/TTS/thumbnail touchpoint so only produce()'s orchestration runs."""
    from core.tts import WordTiming

    def fake_tts(text, out, **k):
        open(out, "w").write("audio")
        return [WordTiming("w", 0.0, 1.0)]

    monkeypatch.setattr(video_factory.tts, "synthesize_paced", fake_tts)
    monkeypatch.setattr(video_factory, "voice_check", lambda *a, **k: None)
    monkeypatch.setattr(video_factory.media, "probe_duration", lambda p: 5.0)
    monkeypatch.setattr(video_factory, "still_to_clip", lambda img, out, d, profile=None: out)
    monkeypatch.setattr(video_factory, "run_ffmpeg", lambda *a, **k: None)


def test_produce_title_overlay_burns_headline_and_poster(tmp_path, monkeypatch):
    """title_overlay=on feeds the hook title into build_ass (headline) AND the thumbnail (poster),
    with the brand tint as the accent — so the in-video billboard and the thumbnail match."""
    from core import video_factory

    _mock_render_touchpoints(video_factory, monkeypatch)
    cap: dict = {}

    def fake_ass(timings, out, **k):
        cap["headline"] = k.get("headline")
        cap["accent"] = k.get("headline_accent_hex")
        open(out, "w").write("ass")
        return out

    tcap: dict = {}

    def fake_thumb(video, out, title, **k):
        tcap.update(title=title, poster=k.get("poster"), accent=k.get("accent_hex"))
        return out

    monkeypatch.setattr(video_factory, "build_ass", fake_ass)
    monkeypatch.setattr(video_factory, "generate_thumbnail", fake_thumb)

    cast = [{"id": "h", "name": "Hero", "description": "d", "style": "s", "ref_image": None, "sheet_path": None}]
    video_factory.produce(
        script=_studio_script(3), episode_number=1, pexels_api_key="", job_id="ovl",
        output_dir=str(tmp_path / "o"), visual_source="studio", characters=cast, image_api_key="k",
        gen_image=lambda **kw: (open(kw["out_path"], "w").write("P"), kw["out_path"])[1],
        title_overlay=True, branding=video_factory.Branding(tint_color="0xFF3B30"),
    )
    assert cap["headline"] == "TA" and cap["accent"] == "0xFF3B30"   # hook title (variant A) + brand accent
    assert tcap["title"] == "TA" and tcap["poster"] is True and tcap["accent"] == "0xFF3B30"


def test_produce_studio_uses_uploaded_ref_image(tmp_path, monkeypatch):
    """A character with an uploaded reference image uses it as the anchor directly — the AI sheet is
    NOT generated, and every scene draw references the uploaded image."""
    from core import video_factory

    _mock_render_touchpoints(video_factory, monkeypatch)
    monkeypatch.setattr(video_factory, "build_ass", lambda t, o, **k: open(o, "w").write("a"))
    monkeypatch.setattr(video_factory, "generate_thumbnail", lambda *a, **k: a[1])

    ref = tmp_path / "myhero.png"
    ref.write_bytes(b"a-real-uploaded-image")
    gen_refs, gen_urls = [], []

    def fake_gen(*, prompt, api_key, out_path, model=None, reference_paths=None, reference_url=None):
        gen_refs.append(reference_paths)
        gen_urls.append(reference_url)
        open(out_path, "w").write("P")
        return out_path

    cast = [{"id": "h", "name": "Hero", "description": "d", "style": "s",
             "ref_image": str(ref), "ref_url": "https://f.example/studio/ref/tok", "sheet_path": None}]
    video_factory.produce(
        script=_studio_script(3), episode_number=1, pexels_api_key="", job_id="refimg",
        output_dir=str(tmp_path / "o"), visual_source="studio", characters=cast, image_api_key="k",
        gen_image=fake_gen,
    )
    # 3 scene draws, no 4th sheet-generation call; the uploaded image anchors every scene (local path
    # for the Gemini leg AND the public url forwarded for the Pollinations kontext leg).
    assert len(gen_refs) == 3
    assert all(rp and rp[0] == str(ref) for rp in gen_refs)
    assert all(u == "https://f.example/studio/ref/tok" for u in gen_urls)


def test_produce_studio_requires_a_cast(tmp_path):
    """Studio Mode with an empty cast fails clearly instead of silently rendering stock."""
    import pytest

    from core import video_factory

    with pytest.raises(RuntimeError, match="no characters"):
        video_factory.produce(
            script=_studio_script(3), episode_number=1, pexels_api_key="", job_id="j",
            output_dir=str(tmp_path / "o"), visual_source="studio", characters=[],
            image_api_key="gkey",
        )
