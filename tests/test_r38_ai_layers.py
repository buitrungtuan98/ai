"""R38 — more AI where it helps, rails where it matters: the weekly AI analyst (insights + topic
ideas), an AI second opinion on every rule-made decision, and the audience map + style × time of
day in the evidence the AI reads. Gemini is mocked: these tests are about the wiring and the rails."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import best_time as bt


# ── evidence the AI reads ────────────────────────────────────────────────────
def test_style_table_scores_each_style_overall_and_per_daypart():
    night = {"music_mood": "calm"}
    day = {"music_mood": "upbeat"}
    samples = ([bt.Sample("mon", 22, 300, night)] * 3 + [bt.Sample("mon", 10, 100, day)] * 3
               + [bt.Sample("tue", 10, 100, None)])
    rows = bt.style_table(samples, 100)
    calm = {r["daypart"]: r for r in rows if r["value"] == "calm"}
    assert set(calm) == {"all", "night"} and calm["night"]["n"] == 3
    upbeat = next(r for r in rows if r["value"] == "upbeat" and r["daypart"] == "all")
    assert calm["night"]["score"] > 1.0 >= upbeat["score"]
    assert bt.style_table(samples, None) == []


def test_audience_summary_lists_strongest_and_weakest_cells():
    grid = bt.build_grid([bt.Sample("mon", 7, 400, None)] * 2 + [bt.Sample("tue", 19, 50, None)] * 2
                         + [bt.Sample(d, 14, 100, None) for d in bt.WEEKDAYS for _ in range(2)])
    summary = bt.audience_summary(grid)
    assert summary["strongest"][0]["day"] == "mon" and summary["weakest"][0]["day"] == "tue"


def test_the_number_rail_is_shared():
    from core.council import numbers_missing

    assert numbers_missing("views rose to 1,234", '{"v": 1234}') is None
    assert "4321" in numbers_missing("views rose to 4321", '{"v": 1234}')


def _campaign(session, user, channel, **cfg):
    from database.models import Campaign
    from database.types import CampaignStatus

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Space facts",
                 total_episodes=20, current_episode=5, status=CampaignStatus.active,
                 config_json={"timezone": "UTC", "posting_slots": ["19:00"], **cfg})
    session.add(c)
    session.commit()
    return c


def _published(session, campaign, ep, when, views_24h, style=None):
    from database.models import Task
    from database.types import TaskStatus

    session.add(Task(campaign_id=campaign.id, user_id=campaign.user_id, episode_number=ep,
                     status=TaskStatus.COMPLETED, published_video_id=f"v{ep}", published_at=when,
                     finished_at=when, stats_json={"views_24h": views_24h},
                     render_json={"style": style} if style else None))
    session.commit()


def test_council_pack_carries_the_audience_map_and_styles(session, user, channel):
    from core import council

    camp = _campaign(session, user, channel)
    for i in range(4):
        _published(session, camp, i + 1, datetime.utcnow() - timedelta(days=i + 1, hours=2),
                   1400 + i, {"music_mood": "calm"})
    pack = council.evidence_pack(session, channel)
    assert pack["audience_map"]["videos"] == 4
    assert any(r["value"] == "calm" for r in pack["style_by_daypart"])


# ── the weekly AI analyst ────────────────────────────────────────────────────
def _fake_report():
    from core.analyst import AnalystReport

    return AnalystReport.model_validate({
        "headline": "Evenings carry the channel this week",
        "insights": [
            {"title": "Calm music wins", "detail": "Calm-music videos scored best in the evening.",
             "evidence": ["style_by_daypart calm"]},
            {"title": "Invented", "detail": "Views jumped to 98765 on Tuesday, a record.",
             "evidence": ["made up"]}],
        "recommendations": ["Keep the calm mood on evening slots"],
        "topic_ideas": [{"topic": "Deep space mysteries", "why": "Builds on Space facts"}]})


def test_analyst_writes_a_report_and_drops_invented_numbers(session, user, channel, monkeypatch):
    from core import ai_engine, analyst

    camp = _campaign(session, user, channel)
    _published(session, camp, 1, datetime.utcnow() - timedelta(days=2), 120)
    monkeypatch.setattr(ai_engine, "generate_structured", lambda **kw: _fake_report())
    state = analyst.run_analyst(session, channel, api_key="k", model="m")
    assert [i["title"] for i in state["insights"]] == ["Calm music wins"]
    assert state["dropped"] == 1 and state["topic_ideas"][0]["topic"] == "Deep space mysteries"
    assert channel.autopilot_json["analyst"]["headline"].startswith("Evenings")
    assert not analyst.due(channel) and analyst.due(channel, datetime.utcnow() + timedelta(days=8))


def test_analyst_is_weekly_and_needs_a_key(session, user, channel, monkeypatch):
    from core import ai_engine
    from workers import scheduler

    _campaign(session, user, channel)
    calls = []
    monkeypatch.setattr(ai_engine, "generate_structured",
                        lambda **kw: calls.append(1) or _fake_report())
    assert scheduler.autopilot_analyst_channel(session, None, channel) == 0      # no key
    assert scheduler.autopilot_analyst_channel(session, user, channel) == 1
    assert scheduler.autopilot_analyst_channel(session, user, channel) == 0      # this week: done
    assert len(calls) == 1


def test_topic_ideas_steer_the_next_campaign_design(session, user, channel, monkeypatch):
    from core import ai_engine
    from workers import scheduler

    parent = _campaign(session, user, channel)
    channel.autopilot_json = {"analyst": {"topic_ideas": [
        {"topic": "Deep space mysteries", "why": "Builds on Space facts"}]}}
    session.commit()
    seen = {}
    monkeypatch.setattr(ai_engine, "propose_campaign",
                        lambda **kw: seen.setdefault("ctx", kw["extra_context"]))
    scheduler._design_successor(session, parent)
    assert "Deep space mysteries" in seen["ctx"]


# ── AI review of rule-made decisions ─────────────────────────────────────────
def _verdict(verdict, reason="The recent videos are recovering, so stopping now is premature."):
    from core.decision_review import ReviewVerdict

    return ReviewVerdict(verdict=verdict, reason=reason, evidence=["recent views"])


def _wind_down(session, user, channel, camp, **evidence):
    from database.models import AutopilotAction

    a = AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=camp.id,
                        kind="wind_down", summary="Wind down Space facts", evidence=evidence,
                        params={"total_episodes": camp.current_episode})
    session.add(a)
    session.commit()
    return a


@pytest.mark.parametrize("verdict,status", [("veto", "dismissed"), ("approve", "applied")])
def test_full_auto_applies_only_what_the_ai_review_lets_through(
        session, user, channel, monkeypatch, verdict, status):
    from core import ai_engine
    from workers import scheduler

    camp = _campaign(session, user, channel)
    a = _wind_down(session, user, channel, camp)
    assert scheduler.autopilot_autoapply_channel(session, channel) == {
        "extend": 0, "wind_down": 0, "successor": 0}            # waits for its review first
    monkeypatch.setattr(ai_engine, "generate_structured", lambda **kw: _verdict(verdict))
    assert scheduler.autopilot_review_rules_channel(session, user, channel, "autopilot") == 1
    scheduler.autopilot_autoapply_channel(session, channel)
    session.refresh(a)
    assert a.status == status and a.evidence["ai_review"]["verdict"] == verdict


def test_copilot_only_attaches_the_opinion(session, user, channel, monkeypatch):
    from core import ai_engine
    from workers import scheduler

    camp = _campaign(session, user, channel)
    a = _wind_down(session, user, channel, camp)
    monkeypatch.setattr(ai_engine, "generate_structured", lambda **kw: _verdict("veto"))
    scheduler.autopilot_review_rules_channel(session, user, channel, "copilot")
    session.refresh(a)
    assert a.status == "proposed" and a.evidence["ai_review"]["verdict"] == "veto"


def test_an_ai_outage_never_stalls_full_auto(session, user, channel, monkeypatch):
    from core import ai_engine
    from workers import scheduler

    camp = _campaign(session, user, channel)
    a = _wind_down(session, user, channel, camp)

    def boom(**kw):
        raise RuntimeError("503")

    monkeypatch.setattr(ai_engine, "generate_structured", boom)
    scheduler.autopilot_review_rules_channel(session, user, channel, "autopilot")
    scheduler.autopilot_autoapply_channel(session, channel)
    session.refresh(a)
    assert a.status == "applied" and a.evidence["ai_review"]["verdict"] == "unavailable"


def test_a_review_citing_invented_numbers_is_discarded(session, user, channel, monkeypatch):
    from core import ai_engine
    from workers import scheduler

    camp = _campaign(session, user, channel)
    a = _wind_down(session, user, channel, camp)
    monkeypatch.setattr(ai_engine, "generate_structured", lambda **kw: _verdict(
        "veto", "Views fell 87654 below last week, so this must stop now."))
    scheduler.autopilot_review_rules_channel(session, user, channel, "autopilot")
    scheduler.autopilot_autoapply_channel(session, channel)
    session.refresh(a)
    assert a.evidence["ai_review"]["verdict"] == "unreliable" and a.status == "applied"


def test_council_decisions_and_undos_are_not_reviewed_again(session, user, channel):
    from core.decision_review import needs_review

    camp = _campaign(session, user, channel)
    assert not needs_review(_wind_down(session, user, channel, camp, by="council"))
    assert not needs_review(_wind_down(session, user, channel, camp, revert=True))
    assert needs_review(_wind_down(session, user, channel, camp))


# ── UI ───────────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def test_channels_page_shows_the_analysis_and_ideas_link_to_the_wizard(client, session, user,
                                                                       channel):
    channel.autopilot_json = {"analyst": {
        "at": "2026-10-23T08:00:00", "headline": "Evenings carry the channel", "dropped": 1,
        "insights": [{"title": "Calm music wins", "detail": "Calm music scored best at night.",
                      "evidence": ["style_by_daypart"]}],
        "recommendations": ["Keep calm music at night"],
        "topic_ideas": [{"topic": "Deep space mysteries", "why": "Builds on Space facts"}]}}
    session.commit()
    r = client.get("/channels")
    assert "Evenings carry the channel" in r.text and "1 point dropped" in r.text
    assert "topic=Deep%20space%20mysteries" in r.text
    r = client.get(f"/campaigns/new?channel={channel.id}&topic=Deep%20space%20mysteries")
    assert 'value="Deep space mysteries"' in r.text


def test_analyse_now_without_a_key_explains_itself(client, session, user, channel, monkeypatch):
    user.gemini_api_key = None
    session.commit()
    r = client.post(f"/channels/{channel.id}/analyst-now", follow_redirects=False)
    assert r.headers["location"] == "/channels?flash=analyst_no_key"


# ── safety: the AI never stalls the factory ──────────────────────────────────
def test_a_review_that_never_arrives_lets_the_rule_apply(session, user, channel):
    from workers import scheduler

    camp = _campaign(session, user, channel)
    a = _wind_down(session, user, channel, camp)
    a.created_at = datetime.utcnow() - timedelta(hours=13)
    session.commit()
    scheduler.autopilot_autoapply_channel(session, channel)
    session.refresh(a)
    assert a.status == "applied" and a.evidence["ai_review"]["verdict"] == "unavailable"


def test_reviews_stop_when_the_pass_ai_time_is_spent(session, user, channel, monkeypatch):
    import time

    from core import ai_engine
    from workers import scheduler

    camp = _campaign(session, user, channel)
    a = _wind_down(session, user, channel, camp)
    monkeypatch.setattr(ai_engine, "generate_structured", lambda **kw: pytest.fail("no AI call"))
    assert scheduler.autopilot_review_rules_channel(
        session, user, channel, "autopilot", deadline=time.monotonic() - 1) == 0
    session.refresh(a)
    assert "ai_review" not in a.evidence and a.status == "proposed"


def test_a_failed_analyst_is_not_retried_every_ten_minutes(session, user, channel, monkeypatch):
    from core import ai_engine
    from workers import scheduler

    _campaign(session, user, channel)
    calls = []

    def boom(**kw):
        calls.append(1)
        raise RuntimeError("429 quota")

    monkeypatch.setattr(ai_engine, "generate_structured", boom)
    with pytest.raises(RuntimeError):
        scheduler.autopilot_analyst_channel(session, user, channel)
    assert scheduler.autopilot_analyst_channel(session, user, channel) == 0   # backed off
    assert len(calls) == 1
