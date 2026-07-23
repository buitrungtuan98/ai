"""Auto-QC gate units: footage vetter thresholds, final QC verdicts, and fail-open behavior.

The vision API is never called — judges are monkeypatched. What matters here is the contract:
QC can reject, but it can never break a render (every error path degrades to a pass).
"""
from __future__ import annotations

from core.ai_engine import FootageVerdict, VideoQCVerdict


def test_footage_vetter_threshold(monkeypatch):
    from core import qc

    monkeypatch.setattr(qc, "extract_frame", lambda video, out, at: None)
    monkeypatch.setattr(qc, "judge_footage",
                        lambda frame, narration, *, api_key, **kw: FootageVerdict(match_score=3, reason="off-topic"))
    vet = qc.make_footage_vetter("key")
    assert vet("clip.mp4", "a story about the sea") is False

    monkeypatch.setattr(qc, "judge_footage",
                        lambda frame, narration, *, api_key, **kw: FootageVerdict(match_score=9))
    assert qc.make_footage_vetter("key")("clip.mp4", "a story about the sea") is True


def test_footage_vetter_fails_open(monkeypatch):
    from core import qc

    monkeypatch.setattr(qc, "extract_frame", lambda video, out, at: None)

    def boom(*a, **kw):
        raise RuntimeError("vision API down")

    monkeypatch.setattr(qc, "judge_footage", boom)
    # A QC outage must never reject a clip (fail-open) — the render proceeds as before.
    assert qc.make_footage_vetter("key")("clip.mp4", "narration") is True


def test_batch_vetter_thresholds_and_fail_open(monkeypatch):
    """The batch vetter judges all scenes in one call; per-item thresholding; any error accepts
    everything (fail-open — QC must never block a render)."""
    from core import qc

    monkeypatch.setattr(qc, "extract_frame", lambda video, out, at: None)
    monkeypatch.setattr(
        qc, "judge_footage_batch",
        lambda items, *, api_key, **kw: [FootageVerdict(match_score=s) for s in (9, 3, 7)])
    vet = qc.make_batch_vetter("key")
    assert vet([("a.mp4", "n1"), ("b.mp4", "n2"), ("c.mp4", "n3")]) == [True, False, True]

    def boom(*a, **kw):
        raise RuntimeError("vision down")

    monkeypatch.setattr(qc, "judge_footage_batch", boom)
    assert qc.make_batch_vetter("key")([("a.mp4", "n1"), ("b.mp4", "n2")]) == [True, True]


def test_judge_footage_batch_count_mismatch(monkeypatch):
    """A response with the wrong number of verdicts raises (the vetter then fails open)."""
    import json as _json

    import core.ai_engine as ai

    monkeypatch.setattr(ai, "_call_gemini_vision",
                        lambda **k: _json.dumps({"verdicts": [{"match_score": 9, "reason": ""}]}))
    import pytest as _pytest
    with _pytest.raises(ValueError, match="expected 2"):
        ai.judge_footage_batch([("f1.jpg", "n1"), ("f2.jpg", "n2")], api_key="k")


def test_final_qc_pass_fail_and_fail_open(monkeypatch):
    from core import qc

    monkeypatch.setattr(qc.media, "probe_duration", lambda path: 30.0)
    sampled: list[float] = []
    monkeypatch.setattr(qc, "extract_frame", lambda video, out, at: sampled.append(at))
    monkeypatch.setattr(qc, "extract_audio", lambda video, out: None)

    monkeypatch.setattr(qc, "judge_video_frames",
                        lambda frames, *, api_key, context="", **kw: VideoQCVerdict(quality_score=9))
    res = qc.run_final_qc("m.mp4", api_key="key")
    assert res.passed and res.score == 9 and res.issues == []
    assert len(sampled) == qc.FINAL_QC_FRAMES
    assert 0 < min(sampled) and max(sampled) < 30.0  # evenly spaced, never the very edges

    monkeypatch.setattr(qc, "judge_video_frames",
                        lambda frames, *, api_key, context="", **kw: VideoQCVerdict(
                            quality_score=4, issues=["captions clipped"]))
    res = qc.run_final_qc("m.mp4", api_key="key")
    assert not res.passed and res.score == 4 and res.issues == ["captions clipped"]
    assert res.as_dict() == {"passed": False, "score": 4, "issues": ["captions clipped"]}

    def boom(*a, **kw):
        raise RuntimeError("vision API down")

    monkeypatch.setattr(qc, "judge_video_frames", boom)
    res = qc.run_final_qc("m.mp4", api_key="key")
    assert res.passed and res.score is None  # fail-open: outage never blocks an episode


def test_final_qc_attaches_audio_for_voice_check(monkeypatch):
    """The master's audio track rides along in the SAME vision call (voice QC at zero extra API
    cost); a failed audio extraction degrades to frames-only judging, never a blocked render."""
    from core import qc

    monkeypatch.setattr(qc.media, "probe_duration", lambda path: 30.0)
    monkeypatch.setattr(qc, "extract_frame", lambda video, out, at: None)
    monkeypatch.setattr(qc, "extract_audio", lambda video, out: None)
    seen: dict = {}

    def fake_judge(frames, *, api_key, context="", audio_path=None, **kw):
        seen["audio_path"] = audio_path
        return VideoQCVerdict(quality_score=9)

    monkeypatch.setattr(qc, "judge_video_frames", fake_judge)
    assert qc.run_final_qc("m.mp4", api_key="key").passed
    assert seen["audio_path"] and seen["audio_path"].endswith(".aac")  # audio reached the judge

    def audio_boom(video, out):
        raise RuntimeError("no audio stream")

    monkeypatch.setattr(qc, "extract_audio", audio_boom)
    assert qc.run_final_qc("m.mp4", api_key="key").passed
    assert seen["audio_path"] is None  # frames-only fallback


def test_judge_video_frames_audio_prompt(monkeypatch):
    """With audio attached, the judging prompt must actually ask about the voice."""
    import json as _json

    import core.ai_engine as ai

    captured = {}

    def fake_vision(**kw):
        captured.update(kw)
        return _json.dumps({"quality_score": 8, "issues": []})

    monkeypatch.setattr(ai, "_call_gemini_vision", fake_vision)
    ai.judge_video_frames(["f.jpg"], api_key="k", audio_path="a.aac")
    assert captured["audio_path"] == "a.aac"
    assert "VOICE" in captured["prompt"]

    captured.clear()
    ai.judge_video_frames(["f.jpg"], api_key="k")
    assert captured["audio_path"] is None
    assert "VOICE" not in captured["prompt"]


def test_deterministic_qc_flags_black_and_silence(monkeypatch):
    """Free ffmpeg checks fail CLOSED on a mostly-black or long-silent master."""
    from core import qc

    monkeypatch.setattr(qc.media, "max_black_span", lambda p: 0.2)
    monkeypatch.setattr(qc.media, "max_silence_span", lambda p: 0.5)
    clean = qc.run_deterministic_qc("m.mp4")
    assert clean.passed and clean.issues == []

    monkeypatch.setattr(qc.media, "max_black_span", lambda p: 5.0)
    black = qc.run_deterministic_qc("m.mp4")
    assert not black.passed and any("black" in i for i in black.issues)

    monkeypatch.setattr(qc.media, "max_black_span", lambda p: 0.0)
    monkeypatch.setattr(qc.media, "max_silence_span", lambda p: 9.0)
    silent = qc.run_deterministic_qc("m.mp4")
    assert not silent.passed and any("silence" in i for i in silent.issues)


def test_deterministic_qc_fails_open_on_detector_error(monkeypatch):
    """A detector outage must never block a render — each check fails open individually."""
    from core import qc

    def boom(p):
        raise RuntimeError("ffmpeg down")

    monkeypatch.setattr(qc.media, "max_black_span", boom)
    monkeypatch.setattr(qc.media, "max_silence_span", boom)
    assert qc.run_deterministic_qc("m.mp4").passed
