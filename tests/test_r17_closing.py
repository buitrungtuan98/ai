"""ADR-079/080/081 closing pieces: the flop breaker, the AI script judge, the manager report, and
the series playlist — each simulated end to end, caps and fail-open behaviour pinned."""
from __future__ import annotations

import pytest

# Captured at collection time, before conftest's autouse stub replaces it (same pattern as
# test_facebook's _REAL_CHECK): these tests drive the REAL playlist function with a faked client.
from services.youtube_service import add_to_series_playlist as _REAL_ADD


# ── B3: the flop breaker ─────────────────────────────────────────────────────
def _camp_tasks(flops):
    """Fake tasks newest-last: True = flopped, False = fine, None = unjudged."""
    class T:
        def __init__(self, ep, verdict):
            self.episode_number = ep
            self.stats_json = ({"views_24h": 10 if verdict else 500, "flop": verdict}
                               if verdict is not None else {})
            self.synopsis = None
            self.ab_variant = None
    return [T(i + 1, v) for i, v in enumerate(flops)]


class _Camp:
    class _S:
        value = "active"
    status = _S()
    topic_name = "Sử Việt"
    total_episodes = 30
    current_episode = 8


def test_three_straight_first_day_flops_propose_the_pause():
    from core import autopilot

    tasks = _camp_tasks([False, False, True, None, True, True])   # newest run: 3 flops (None skipped)
    verdict = {"label": "healthy", "baseline": None, "retention": None}
    props = autopilot.propose_actions(_Camp(), tasks, verdict)
    winds = [p for p in props if p["kind"] == "wind_down"]
    assert len(winds) == 1
    assert winds[0]["evidence"]["consecutive_flops"] == 3
    assert winds[0]["params"]["total_episodes"] == _Camp.current_episode   # stop NEW, delete nothing

    # Two flops → silence; a healthy latest episode resets the streak.
    assert not [p for p in autopilot.propose_actions(
        _Camp(), _camp_tasks([True, True, False]), verdict) if p["kind"] == "wind_down"]
    # Below the judging floor (median needs 5 measured) → the breaker never fires on thin data.
    assert not [p for p in autopilot.propose_actions(
        _Camp(), _camp_tasks([True, True, True]), verdict) if p["kind"] == "wind_down"]


# ── C2: the AI script judge shares the one regenerate budget ────────────────
def _judge_env(session, user, channel):
    from database.models import Campaign, Task
    from database.types import CampaignStatus

    # auto_qc off: these tests exercise the SCRIPT judge; with vision QC on, an unreachable judge
    # now parks for review instead of completing (ADR-084) — that path has its own tests.
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=5,
                   status=CampaignStatus.active, config_json={"language": "vi", "auto_qc": "off"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)
    return cam, t


def _fresh_script(k):
    from core.ai_engine import VideoScript

    return VideoScript(
        language="vi", topic="T", synopsis=f"s{k}",
        scenes=[{"index": i, "narration": f"Chuyện số {k} cảnh {i}: một chi tiết rất cụ thể "
                                          f"về nhân vật {k * 10 + i}.", "pexels_keywords": ["k"]}
                for i in range(3)],
        metadata_variations=[{"variant": v, "title": f"Tựa {k}-{v}", "description": "d",
                              "tags": ["a", "b", "c"]} for v in "ABC"])


def test_judge_reject_burns_the_one_regenerate_then_fails(session, user, channel, monkeypatch):
    from core.ai_engine import ScriptVerdict
    from database.types import TaskStatus
    from workers import video_worker

    cam, t = _judge_env(session, user, channel)
    n = {"gen": 0}
    monkeypatch.setattr(video_worker, "generate_script",
                        lambda **k: (n.__setitem__("gen", n["gen"] + 1), _fresh_script(n["gen"]))[1])
    monkeypatch.setattr(video_worker, "_judge_script_safe",
                        lambda *a, **k: ScriptVerdict(score=2, issues=["hook mơ hồ"]))
    monkeypatch.setattr(video_worker.video_factory, "produce",
                        lambda **k: pytest.fail("a judged-bad script must not render"))

    video_worker.render_task(t.id)
    session.refresh(t)
    assert t.status == TaskStatus.FAILED and "quality gate twice" in t.error_message
    assert "2/10" in t.error_message and n["gen"] == 2            # exactly one regenerate


def test_judge_outage_is_no_verdict_not_a_stalled_factory(session, user, channel, monkeypatch):
    from core.video_factory import RenderResult
    from database.types import TaskStatus
    from workers import video_worker

    cam, t = _judge_env(session, user, channel)
    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _fresh_script(1))
    monkeypatch.setattr(video_worker, "_judge_script_safe", lambda *a, **k: None)  # outage/off
    monkeypatch.setattr(video_worker.video_factory, "produce", lambda **k: RenderResult(
        master_path="/no/m.mp4", thumbnail_path="/no/t.jpg",
        metadata={"title": "x", "variant": "A"}, duration=9.0, scene_count=3))
    monkeypatch.setattr(video_worker, "_publish", lambda *a, **k: "vid-1")
    video_worker.render_task(t.id)
    session.refresh(t)
    assert t.status == TaskStatus.COMPLETED


def test_judge_guards_budget_and_toggle(session, user, channel, monkeypatch):

    # conftest stubs `_judge_script_safe`; undo() restores the real one for this test.
    import importlib

    vw = importlib.import_module("workers.video_worker")
    monkeypatch.undo()
    fp = {"narration": "x", "title": "t"}
    assert vw._judge_script_safe(user, channel, {"script_judge": "off"}, fp, "k", "m") is None

    # Budget reserve: at/over 80% of the daily budget → no judging call is even attempted.
    user.settings_json = {"ai_daily_budget": 10}
    monkeypatch.setattr("core.usage.ai_calls_today", lambda: 8)
    called = []
    monkeypatch.setattr("core.ai_engine.judge_script",
                        lambda *a, **k: called.append(1))
    assert vw._judge_script_safe(user, channel, {}, fp, "k", "m") is None
    assert not called


# ── D4: the manager report ───────────────────────────────────────────────────
def test_council_run_delivers_the_manager_report(session, user, channel, monkeypatch):
    import core.council as C
    from workers import scheduler, video_worker

    from database.models import Campaign
    from database.types import CampaignStatus

    session.add(Campaign(user_id=user.id, channel_id=channel.id, topic_name="T",
                         total_episodes=5, status=CampaignStatus.active))
    session.commit()
    user.gemini_api_key = "k"
    session.commit()

    def fake_run(db, ch, api_key, model):
        cfg = dict(ch.autopilot_json or {})
        cfg["council"] = {"at": "2020-01-01T00:00:00", "summary": "Khung giờ tối đang thắng rõ.",
                          "watching": ["retention tập 9"]}
        ch.autopilot_json = cfg
        db.commit()
        return {"filed": 2, "refused": 0, "held": 1, "skipped_unchanged": False}

    monkeypatch.setattr(C, "run_council", fake_run)
    notes, logged = [], []
    monkeypatch.setattr(video_worker, "_notify", lambda u, m: notes.append(m))
    monkeypatch.setattr(scheduler, "_log_action",
                        lambda db, ch, kind, summary, **k: logged.append((kind, summary)))

    scheduler.autopilot_council_channel(session, user, channel)
    assert logged and logged[0][0] == "report"
    assert "Khung giờ tối" in logged[0][1] and "filed 2" in logged[0][1]
    assert "Watching: retention tập 9" in logged[0][1]
    assert notes and notes[0].startswith("🧠")                    # filed > 0 → phone note

    # Nothing filed → the report is logged but the phone stays quiet.
    def quiet_run(db, ch, api_key, model):
        cfg = dict(ch.autopilot_json or {})
        cfg["council"] = {"at": "2020-01-02T00:00:00", "summary": "Không có gì đáng đổi.",
                          "watching": []}
        ch.autopilot_json = cfg
        db.commit()
        return {"filed": 0, "refused": 0, "held": 0, "skipped_unchanged": False}

    monkeypatch.setattr(C, "run_council", quiet_run)
    notes.clear()
    logged.clear()
    scheduler.autopilot_council_channel(session, user, channel)
    assert logged and "no changes proposed" in logged[0][1]
    assert notes == []


# ── A4: the series playlist ──────────────────────────────────────────────────
def test_playlist_created_once_cached_and_reused(session, user, channel, monkeypatch):
    from database.models import Campaign
    from database.types import CampaignStatus
    from services import youtube_service as yt

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Sử Việt",
                   total_episodes=5, status=CampaignStatus.active, config_json={})
    session.add(cam)
    session.commit()
    session.refresh(cam)

    created, added = [], []

    class FakeYT:
        def playlists(self):
            class P:
                def insert(self, part, body):
                    class E:
                        @staticmethod
                        def execute():
                            created.append(body["snippet"]["title"])
                            return {"id": "PL123"}
                    return E()
            return P()

        def playlistItems(self):
            class Items:
                def insert(self, part, body):
                    class E:
                        @staticmethod
                        def execute():
                            added.append((body["snippet"]["playlistId"],
                                          body["snippet"]["resourceId"]["videoId"]))
                            return {}
                    return E()
            return Items()

    monkeypatch.setattr(yt, "build_credentials", lambda ch: object())
    import googleapiclient.discovery as gd

    monkeypatch.setattr(gd, "build", lambda *a, **k: FakeYT())

    # First publish creates + caches; second reuses the cached id — one playlist per campaign.
    for vid in ("vidA", "vidB"):
        _REAL_ADD(channel, cam, session, vid)
    assert created == ["Sử Việt"]
    assert added == [("PL123", "vidA"), ("PL123", "vidB")]
    assert cam.config_json["yt_playlist_id"] == "PL123"


def test_playlist_failure_never_bubbles(session, user, channel, monkeypatch):
    from database.models import Campaign
    from database.types import CampaignStatus
    from services import youtube_service as yt

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T",
                   total_episodes=5, status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    monkeypatch.setattr(yt, "build_credentials",
                        lambda ch: (_ for _ in ()).throw(RuntimeError("no creds")))
    _REAL_ADD(channel, cam, session, "vidX")                     # must not raise
