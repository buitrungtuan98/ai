"""AI engine (mocked Gemini) parsing/retry and the safety filter / variation gate."""
from __future__ import annotations

import json

import pytest


VALID = {
    "language": "en", "topic": "Space",
    "scenes": [
        {"index": 0, "narration": "The sun is a star.", "caption_hook": "SUN", "pexels_keywords": ["sun"]},
        {"index": 1, "narration": "It is hot.", "caption_hook": None, "pexels_keywords": ["fire"]},
        {"index": 2, "narration": "Earth orbits it.", "caption_hook": None, "pexels_keywords": ["earth"]},
    ],
    "metadata_variations": [
        {"variant": "A", "title": "Sun", "description": "d", "tags": ["a", "b", "c"]},
        {"variant": "B", "title": "Star", "description": "d", "tags": ["a", "b", "c"]},
        {"variant": "C", "title": "Hot", "description": "d", "tags": ["a", "b", "c"]},
    ],
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import core.ai_engine as ai

    monkeypatch.setattr(ai, "_BACKOFF_BASE_SECONDS", 0)


def test_parse_valid(monkeypatch):
    import core.ai_engine as ai
    from core.ai_engine import VideoScript, generate_structured

    monkeypatch.setattr(ai, "_call_gemini", lambda **k: json.dumps(VALID))
    res = generate_structured(prompt="x", schema=VideoScript, api_key="k")
    assert len(res.scenes) == 3 and len(res.metadata_variations) == 3


def test_code_fence_stripped(monkeypatch):
    import core.ai_engine as ai
    from core.ai_engine import VideoScript, generate_structured

    monkeypatch.setattr(ai, "_call_gemini", lambda **k: "```json\n" + json.dumps(VALID) + "\n```")
    assert generate_structured(prompt="x", schema=VideoScript, api_key="k").topic == "Space"


def test_retry_then_success(monkeypatch):
    import core.ai_engine as ai
    from core.ai_engine import VideoScript, generate_structured

    seq = ["not json", json.dumps(VALID)]
    monkeypatch.setattr(ai, "_call_gemini", lambda **k: seq.pop(0))
    assert generate_structured(prompt="x", schema=VideoScript, api_key="k").topic == "Space"
    assert not seq


def test_blocked_not_retried(monkeypatch):
    import core.ai_engine as ai
    from core.ai_engine import GeminiBlockedError, VideoScript, generate_structured

    def blocked(**k):
        raise GeminiBlockedError("safety")

    monkeypatch.setattr(ai, "_call_gemini", blocked)
    with pytest.raises(GeminiBlockedError):
        generate_structured(prompt="x", schema=VideoScript, api_key="k")


def test_exhausted_retries(monkeypatch):
    import core.ai_engine as ai
    from core.ai_engine import GeminiError, VideoScript, generate_structured

    monkeypatch.setattr(ai, "_call_gemini", lambda **k: "garbage")
    with pytest.raises(GeminiError):
        generate_structured(prompt="x", schema=VideoScript, api_key="k", max_retries=2)


def test_safety_filter_remove_and_mask():
    from core import safety_filter as sf

    r = sf.filter_text("This is damn stupid content", "en", mode="remove")
    assert not sf.contains_blacklisted(r.clean_text, "en")
    assert {w.lower() for w in r.replaced} == {"damn", "stupid"}
    assert sf.filter_text("damn", "en", mode="mask").clean_text == "d***"


def test_variation_gate():
    from core import safety_filter as sf

    assert sf.check_variation_request(num_videos=1, identical_source=True).allowed is True
    blocked = sf.check_variation_request(num_videos=5, identical_source=True)
    assert blocked.allowed is False and any("Refused" in w for w in blocked.warnings)
    assert sf.check_variation_request(num_videos=5, identical_source=True, allow_bulk_variation=True).allowed is True


def test_footage_license_guard():
    from core import safety_filter as sf

    sf.assert_licensed_footage("pexels")
    with pytest.raises(ValueError):
        sf.assert_licensed_footage("random")


def test_compose_system_prompt_persona_layer():
    from core.ai_engine import NATURAL_STYLE_RULES, compose_system_prompt

    system = compose_system_prompt(
        "vi",
        custom_system_prompt="Horror ngắn, twist cuối.",
        persona="Chú Ba miền Tây, giọng thân mật, hay dùng 'nha'.",
        style_examples="Khuya nay kể chuyện nhà bà Sáu nha...",
        catchphrase_open="Khuya rồi đó… tắt đèn chưa?",
        catchphrase_close="Ngủ ngon nha… nếu ngủ được.",
    )
    # Everything the operator configures actually reaches the model, plus the anti-AI-tell rules.
    assert NATURAL_STYLE_RULES in system
    assert "Chú Ba miền Tây" in system
    assert "nhà bà Sáu" in system
    assert "tắt đèn chưa?" in system and "nếu ngủ được." in system
    assert "Horror ngắn" in system

    # Minimal call still works with no persona configured.
    bare = compose_system_prompt("en")
    assert NATURAL_STYLE_RULES in bare and "CHARACTER" not in bare


def test_build_script_prompt_episode_memory():
    from core.ai_engine import build_script_prompt

    prev = ["A ghost in the old market", "The taxi that never arrives"]
    no_repeat = build_script_prompt("horror", "vi", 30, 3, continuity="no_repeat", previous_synopses=prev)
    assert "EPISODE MEMORY" in no_repeat
    assert "ghost in the old market" in no_repeat and "clearly different premise" in no_repeat

    serial = build_script_prompt("horror", "vi", 30, 3, continuity="serial", previous_synopses=prev)
    assert "SERIAL STORY" in serial and "Continue DIRECTLY" in serial
    assert "The taxi that never arrives" in serial  # continues from the LAST episode

    plain = build_script_prompt("horror", "vi", 30, 1)
    assert "EPISODE MEMORY" not in plain and "SERIAL STORY" not in plain
    assert "synopsis" in plain  # the schema field is always requested
