"""ADR-081 — the strategy council: Gemini interprets, code measures and validates.

The boundary under test: every number is computed by code, the model chooses from a CLOSED action
menu, and the rails refuse anything out of bounds — including statistics the model invented. An AI
failure or refusal leaves the deterministic autopilot fully functional (the council is a strategy
layer, never the safety net).
"""
from __future__ import annotations

import datetime as dt


from core import council
from core.council import CouncilDecision, CouncilVerdict


def _seed(session, user, channel, *, slots=None, published=8, planned=10):
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Sử Việt",
                   current_episode=published, total_episodes=planned,
                   status=CampaignStatus.active,
                   config_json={"language": "vi", "posting_slots": slots or ["09:00", "21:00"]})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    now = dt.datetime.utcnow()
    for i in range(1, published + 1):
        session.add(Task(campaign_id=cam.id, user_id=user.id, episode_number=i,
                         status=TaskStatus.COMPLETED, published_video_id=f"v{i}",
                         finished_at=now.replace(hour=21 if i % 2 else 9, minute=0)
                         - dt.timedelta(days=i),
                         stats_json={"views_24h": 800 if i % 2 else 90, "flop": not (i % 2),
                                     "avg_pct_viewed": 60.0 if i % 2 else 25.0}))
    session.commit()
    session.refresh(cam)
    return cam


def test_evidence_pack_is_computed_not_guessed(session, user, channel):
    cam = _seed(session, user, channel)
    pack = council.evidence_pack(session, channel)
    c = pack["campaigns"][0]
    assert c["campaign_id"] == cam.id
    assert c["flops"]["total"] == 4 and c["flops"]["measured_24h"] == 8
    hours = c["publish_hours"]
    assert hours["21:00"]["avg_views_24h"] == 800 and hours["09:00"]["avg_views_24h"] == 90
    assert c["current_slots"] == ["09:00", "21:00"]
    assert pack["channel"]["platform"] == "youtube"
    # The hash is stable for identical data and moves when data moves.
    h1 = council.pack_hash(pack)
    assert h1 == council.pack_hash(council.evidence_pack(session, channel))


def _decision(cam_id, action="slot_change", reason=None, evidence=None, **params):
    return CouncilDecision(
        action=action, campaign_id=cam_id,
        reason=reason or "Slot 09:00 avg 90 first-day views vs 800 at 21:00 — move the slot.",
        evidence=evidence or ["publish_hours 09:00 avg 90", "publish_hours 21:00 avg 800"],
        confidence=0.8, params=params)


def test_rails_refuse_what_must_be_refused(session, user, channel):
    import json

    cam = _seed(session, user, channel)
    pack = council.evidence_pack(session, channel)
    pj = json.dumps(pack, ensure_ascii=False, default=str)

    ok = council.validate_decision(session, _decision(cam.id, **{"from": "09:00", "to": "21:30"}),
                                   pack, pj)
    assert ok is None                                             # a sound decision passes

    cases = [
        (_decision(cam.id, action="delete_everything"), "unknown action"),
        (_decision(999, **{"from": "09:00", "to": "21:30"}), "not in the evidence pack"),
        (_decision(cam.id, **{"from": "10:00", "to": "11:00"}), "not one of the campaign's"),
        (_decision(cam.id, **{"from": "09:00", "to": "9am"}), "HH:MM"),
        (_decision(cam.id, **{"from": "09:00", "to": "09:00"}), "changes nothing"),
        (_decision(cam.id, action="extend", total_episodes=100), "out of bounds"),
        (_decision(cam.id, action="wind_down", total_episodes=3), "published count"),
        (_decision(cam.id, action="tune", volume=11), "subset"),
    ]
    for d, expect in cases:
        problem = council.validate_decision(session, d, pack, pj)
        assert problem and expect in problem, f"{d.action}/{d.params} → {problem!r}"


def test_rails_cut_off_invented_statistics(session, user, channel):
    """The model may only interpret measured numbers. A statistic that exists nowhere in the pack
    is refused — this is the line between 'reads like it understands' and 'makes things up'."""
    import json

    cam = _seed(session, user, channel)
    pack = council.evidence_pack(session, channel)
    pj = json.dumps(pack, ensure_ascii=False, default=str)
    d = _decision(cam.id, reason="Views collapsed 97531 to nothing after episode four this month.",
                  **{"from": "09:00", "to": "21:30"})
    problem = council.validate_decision(session, d, pack, pj)
    assert problem and "97531" in problem


def test_run_council_files_through_rails_and_caches_on_pack_hash(session, user, channel,
                                                                 monkeypatch):
    from core import ai_engine
    from database.models import AutopilotAction

    cam = _seed(session, user, channel)
    calls = []

    def fake_llm(*, prompt, schema, **k):
        calls.append(1)
        return CouncilVerdict(
            channel_summary="Khung 21:00 đang gấp gần chín lần khung 09:00 — nên dồn lịch đăng.",
            decisions=[
                _decision(cam.id, **{"from": "09:00", "to": "21:30"}),
                _decision(cam.id, action="extend", total_episodes=99),   # rails must refuse
                CouncilDecision(action="hold", campaign_id=cam.id, confidence=0.9,
                                reason="Chưa đủ bằng chứng để đổi hướng nội dung của campaign này.",
                                evidence=["classification healthy"]),
            ],
            watching=["retention tập 8"])

    monkeypatch.setattr(ai_engine, "generate_structured", fake_llm)
    r = council.run_council(session, channel, api_key="k", model="m")
    assert r == {"filed": 1, "refused": 1, "held": 1, "skipped_unchanged": False}

    filed = session.query(AutopilotAction).filter_by(kind="slot_change").all()
    assert len(filed) == 1 and filed[0].status == "proposed"
    assert filed[0].evidence["by"] == "council" and filed[0].evidence["confidence"] == 0.8
    state = session.get(type(channel), channel.id).autopilot_json["council"]
    assert "21:00" in state["summary"] and state["watching"] == ["retention tập 8"]

    # Same data → the pack hash matches → no second AI call, ever.
    r2 = council.run_council(session, channel, api_key="k", model="m")
    assert r2["skipped_unchanged"] is True and len(calls) == 1
    # And an identical proposal is never filed twice even if data DID change.
    assert session.query(AutopilotAction).filter_by(kind="slot_change").count() == 1


def test_ai_garbage_cannot_reach_the_inbox(session, user, channel, monkeypatch):
    """A verdict full of nonsense files nothing and the deterministic world is untouched."""
    from core import ai_engine
    from database.models import AutopilotAction

    cam = _seed(session, user, channel)
    monkeypatch.setattr(ai_engine, "generate_structured", lambda **k: CouncilVerdict(
        channel_summary="Everything is amazing, trust me, delete the laggards and go viral.",
        decisions=[_decision(999, **{"from": "09:00", "to": "10:00"}),
                   _decision(cam.id, action="wind_down", total_episodes=1)],
        watching=[]))
    r = council.run_council(session, channel, api_key="k", model="m")
    assert r["filed"] == 0 and r["refused"] == 2
    assert session.query(AutopilotAction).count() == 0


def test_scheduler_guards_key_budget_and_daily_cadence(session, user, channel, monkeypatch):
    from workers import scheduler

    _seed(session, user, channel)
    ran = []
    monkeypatch.setattr(scheduler, "settings", scheduler.settings)

    import core.council as C
    monkeypatch.setattr(C, "run_council",
                        lambda db, ch, api_key, model: (ran.append(1),
                                                        {"filed": 0, "refused": 0, "held": 0,
                                                         "skipped_unchanged": False})[1])
    # No key anywhere → never runs.
    monkeypatch.setattr(scheduler.settings, "GEMINI_API_KEY", "", raising=False)
    user.gemini_api_key = None
    session.commit()
    assert scheduler.autopilot_council_channel(session, user, channel)["filed"] == 0
    assert not ran

    # Key + already ran today → skipped.
    user.gemini_api_key = "k"
    cfg = dict(channel.autopilot_json or {})
    cfg["council"] = {"at": dt.datetime.utcnow().isoformat()}
    channel.autopilot_json = cfg
    session.commit()
    scheduler.autopilot_council_channel(session, user, channel)
    assert not ran

    # Yesterday's run → due again.
    cfg["council"] = {"at": (dt.datetime.utcnow() - dt.timedelta(days=1)).isoformat()}
    channel.autopilot_json = cfg
    session.commit()
    scheduler.autopilot_council_channel(session, user, channel)
    assert ran == [1]


def test_slot_change_applies_reversibly_and_respects_edits(session, user, channel):
    from database.models import AutopilotAction, Campaign
    from workers import scheduler

    cam = _seed(session, user, channel)
    a = AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=cam.id,
                        kind="slot_change", summary="move it",
                        params={"from": "09:00", "to": "21:30"})
    session.add(a)
    session.commit()
    assert scheduler.apply_autopilot_action(session, a) is True
    assert session.get(Campaign, cam.id).config_json["posting_slots"] == ["21:30", "21:00"]

    # The operator already removed that slot → the apply fails cleanly, marked failed.
    b = AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=cam.id,
                        kind="slot_change", summary="stale",
                        params={"from": "09:00", "to": "10:00"})
    session.add(b)
    session.commit()
    assert scheduler.apply_autopilot_action(session, b) is False
    assert b.status == "failed"
