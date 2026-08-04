"""ADR-084 — three QC states, honestly routed: a judge that errored is not a judge that approved.

The reported symptoms: "QC unavailable" rendering as a green pass, and — worse — a video the judge
failed once publishing unseen because the judge was absent for the re-check. These tests pin the
classification, the routing, the recovery button, and the UI truth.
"""
from __future__ import annotations

import pytest


class _Det:
    passed = True
    score = None
    issues: list = []
    unavailable = False


def _mk_env(session, user, channel, **cfg):
    from database.models import Campaign, Task
    from database.types import CampaignStatus

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=5,
                   status=CampaignStatus.active, config_json={"language": "en", **cfg})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)
    return cam, t


def _render(session, monkeypatch, t, verdicts):
    """Run render_task with a scripted sequence of vision verdicts (one per attempt)."""
    from core import qc
    from core.video_factory import RenderResult
    from workers import video_worker

    seq = list(verdicts)
    monkeypatch.setattr(video_worker, "generate_script",
                        lambda **k: __import__("tests.test_worker", fromlist=["x"])._script())
    monkeypatch.setattr(video_worker.video_factory, "produce", lambda **k: RenderResult(
        master_path="/no/m.mp4", thumbnail_path="/no/t.jpg",
        metadata={"title": "T", "variant": "A"}, duration=9.0, scene_count=3))
    monkeypatch.setattr(qc, "make_batch_vetter", lambda *a, **k: None)
    monkeypatch.setattr(qc, "run_deterministic_qc", lambda p: _Det())
    monkeypatch.setattr(qc, "run_final_qc", lambda *a, **k: seq.pop(0))
    published = []
    monkeypatch.setattr(video_worker, "_publish", lambda *a, **k: published.append(1) or "vid-1")
    notes = []
    monkeypatch.setattr(video_worker, "_notify", lambda u, m: notes.append(m))
    video_worker.render_task(t.id)
    session.refresh(t)
    return published, notes


def _v(passed=True, score=8, issues=(), unavailable=False, reason=None):
    from core.qc import QCResult

    return QCResult(passed=passed, score=score, issues=list(issues),
                    unavailable=unavailable, unavailable_reason=reason)


def test_unavailable_reason_classification():
    from core.qc import _unavailable_reason

    assert "daily quota" in _unavailable_reason(RuntimeError(
        "Gemini daily quota exhausted — resets ~midnight US-Pacific"))
    assert "rate-limiting" in _unavailable_reason(RuntimeError("429 RESOURCE_EXHAUSTED"))
    assert "model" in _unavailable_reason(RuntimeError("Gemini model not found (gemini-x)"))
    assert "reached" in _unavailable_reason(RuntimeError("connection reset by peer"))


def test_absent_judge_parks_by_default_and_burns_no_rerender(session, user, channel, monkeypatch):
    from database.models import BufferPoolItem
    from database.types import BufferStatus, TaskStatus

    cam, t = _mk_env(session, user, channel)
    published, notes = _render(session, monkeypatch, t, [
        _v(passed=True, score=None, issues=["qc-unavailable"], unavailable=True,
           reason="Gemini daily quota is spent — it resets around midnight US-Pacific")])
    assert t.status == TaskStatus.AWAITING_REVIEW and not published
    buf = session.query(BufferPoolItem).filter_by(campaign_id=cam.id).one()
    assert buf.status == BufferStatus.awaiting_review
    qc = buf.metadata_json["qc"]
    assert qc["unavailable"] is True and "daily quota" in qc["unavailable_reason"]
    assert qc["attempts"] == 1                       # the one re-render was NOT spent on an absent judge
    assert any("could not run" in n for n in notes)


def test_failed_then_absent_always_parks_even_with_failopen_publish(session, user, channel,
                                                                    monkeypatch):
    """The indefensible path, closed: judge disliked it once → re-render → judge absent → the old
    code published it. Now it parks — regardless of the campaign's fail-open preference."""
    from database.types import TaskStatus

    cam, t = _mk_env(session, user, channel, qc_failopen="publish")
    published, _ = _render(session, monkeypatch, t, [
        _v(passed=False, score=4, issues=["blurry captions"]),
        _v(unavailable=True, score=None, reason="rate limited")])
    assert t.status == TaskStatus.AWAITING_REVIEW and not published
    from database.models import BufferPoolItem

    qc = session.query(BufferPoolItem).filter_by(campaign_id=cam.id).one().metadata_json["qc"]
    assert qc["unavailable"] and qc["prior_fail"] is True


def test_failopen_publish_keeps_the_old_behaviour_on_a_first_attempt(session, user, channel,
                                                                     monkeypatch):
    from database.types import TaskStatus

    cam, t = _mk_env(session, user, channel, qc_failopen="publish")
    published, _ = _render(session, monkeypatch, t, [
        _v(unavailable=True, score=None, reason="rate limited")])
    assert t.status == TaskStatus.COMPLETED and published    # the operator explicitly chose this


def test_run_qc_now_requalifies_in_place(session, user, channel, monkeypatch, tmp_path):
    from core import qc
    from database.models import BufferPoolItem
    from database.types import BufferStatus
    from workers import video_worker

    cam, t = _mk_env(session, user, channel)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    buf = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                         video_path=str(video), status=BufferStatus.awaiting_review,
                         metadata_json={"qc": {"passed": True, "score": None, "attempts": 1,
                                               "unavailable": True,
                                               "unavailable_reason": "rate limited"}})
    session.add(buf)
    session.commit()
    session.refresh(buf)

    monkeypatch.setattr(qc, "run_deterministic_qc", lambda p: _Det())
    monkeypatch.setattr(qc, "run_final_qc", lambda *a, **k: _v(passed=True, score=9))
    video_worker.requalify_task(buf.id)
    session.refresh(buf)
    report = buf.metadata_json["qc"]
    assert report["passed"] is True and report["score"] == 9
    assert "unavailable" not in report                     # the verdict is real now
    assert buf.status == BufferStatus.awaiting_review      # routing stays with review/autopilot


def test_review_page_shows_the_truth_not_a_green_pass(client_env):
    client, session, user, channel = client_env
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, CampaignStatus, TaskStatus

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=5,
                   status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)
    session.add(Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                     status=TaskStatus.AWAITING_REVIEW))
    session.add(BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                               video_path="/no/v.mp4", status=BufferStatus.awaiting_review,
                               metadata_json={"qc": {"passed": True, "score": None, "attempts": 2,
                                                     "unavailable": True, "prior_fail": True,
                                                     "unavailable_reason": "Gemini daily quota is spent"}}))
    session.commit()
    page = client.get("/assets").text
    assert "Auto-QC could not run" in page and "daily quota" in page
    assert "An earlier render of this episode failed QC" in page
    assert "Run QC now" in page
    assert "🟢 Auto-QC passed" not in page                 # never a green pass for an absent judge


def test_episode_page_replays_the_decision_journey(client_env):
    """Glass box (ADR-084): the judgments made during a render — gate, judge, QC per attempt —
    are replayable from the episode page, not buried in worker logs."""
    client, session, user, channel = client_env
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=5,
                   status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
             status=TaskStatus.AWAITING_REVIEW,
             render_json={"journey": [
                 {"step": "Script gate", "note": "clean — no similarity to recent episodes"},
                 {"step": "Script judge", "note": "8/10"},
                 {"step": "Auto-QC (attempt 1)", "note": "passed, 9/10"},
             ]})
    session.add(t)
    session.commit()
    session.refresh(t)
    page = client.get(f"/episodes/{t.id}").text
    assert "How this render was judged" in page
    assert "Script gate" in page and "no similarity to recent episodes" in page
    assert "8/10" in page and "Auto-QC (attempt 1)" in page


@pytest.fixture
def client_env(session, user, channel):
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c, session, user, channel
