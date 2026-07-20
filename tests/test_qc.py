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


def test_final_qc_pass_fail_and_fail_open(monkeypatch):
    from core import qc

    monkeypatch.setattr(qc.media, "probe_duration", lambda path: 30.0)
    sampled: list[float] = []
    monkeypatch.setattr(qc, "extract_frame", lambda video, out, at: sampled.append(at))

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
