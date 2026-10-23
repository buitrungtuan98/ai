"""R37 — best time to post: views at fixed ages, a weekday × daypart audience map, and an engine
that moves one posting slot at a time toward it (and undoes moves that did worse)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import best_time as bt


def _samples(spec):
    """[(weekday, hour, views, count)] → samples."""
    return [bt.Sample(d, h, v) for d, h, v, n in spec for _ in range(n)]


# ── the pure engine ──────────────────────────────────────────────────────────
def test_dayparts_cover_the_whole_day():
    assert [bt.daypart_of(h) for h in (0, 4, 5, 8, 9, 12, 17, 21, 23)] == [
        "late_night", "late_night", "early_morning", "early_morning", "morning", "afternoon",
        "evening", "night", "night"]
    assert bt.daypart_of_slot("06:30") == "early_morning" and bt.daypart_of_slot("x") is None


def test_grid_shrinks_small_cells_toward_normal():
    grid = bt.build_grid(_samples([("mon", 7, 300, 1), ("tue", 19, 100, 9)]))
    assert grid["median_24h"] == 100
    early = grid["dayparts"]["early_morning"]
    assert early["n"] == 1 and 1.0 < early["score"] < 3.0   # one 3× video is not a 3× daypart
    assert grid["cells"]["wed"]["morning"] == {"n": 0, "score": 1.0, "mean": None}


def test_grid_caps_a_viral_outlier():
    grid = bt.build_grid(_samples([("mon", 7, 1_000_000, 1), ("tue", 19, 100, 9)]))
    assert grid["dayparts"]["early_morning"]["score"] <= (bt.SCORE_CAP + 3) / 4


def test_too_few_videos_moves_nothing():
    grid = bt.build_grid(_samples([("mon", 7, 500, 5), ("mon", 19, 50, 5)]))
    assert bt.plan_move(grid, {"posting_slots": ["19:00"]}, iso_week=1, channel_id=1) is None


def test_weakest_slot_moves_to_a_clearly_better_daypart():
    grid = bt.build_grid(_samples([(d, 7, 400, 1) for d in bt.WEEKDAYS]
                                  + [(d, 19, 100, 1) for d in bt.WEEKDAYS]
                                  + [(d, 22, 120, 1) for d in bt.WEEKDAYS]))
    move = bt.plan_move(grid, {"posting_slots": ["19:00", "22:00"]}, iso_week=1, channel_id=1)
    assert (move.scope, move.frm, move.to, move.explore) == ("all", "19:00", "06:30", False)


def test_a_small_gain_is_not_worth_a_move():
    grid = bt.build_grid(_samples([(d, 7, 105, 2) for d in bt.WEEKDAYS]
                                  + [(d, 19, 100, 2) for d in bt.WEEKDAYS]))
    assert bt.plan_move(grid, {"posting_slots": ["19:00"]}, iso_week=1, channel_id=1) is None


def test_one_strong_weekday_gets_its_own_slot():
    """Mondays at 06:30 are strong; the rest of the week mornings are not → a Monday-only move."""
    spec = [(d, 19, 100, 2) for d in bt.WEEKDAYS] + [("mon", 7, 600, 4)]
    spec += [(d, 7, 80, 1) for d in bt.WEEKDAYS if d != "mon"]
    grid = bt.build_grid(_samples(spec))
    move = bt.plan_move(grid, {"posting_slots": ["19:00"]}, iso_week=1, channel_id=1)
    assert (move.scope, move.frm, move.to) == ("mon", "19:00", "06:30")
    # Once Monday has its own slot, the strong Monday mornings must not drag the WHOLE week there.
    after = {"posting_slots": ["19:00"], "slots_by_day": {"mon": ["06:30"]}}
    nxt = bt.plan_move(grid, after, iso_week=1, channel_id=1)
    assert nxt is None or nxt.scope != "all"


def test_explore_week_tries_the_least_measured_time():
    grid = bt.build_grid(_samples([(d, 19, 100, 2) for d in bt.WEEKDAYS]
                                  + [(d, 22, 100, 1) for d in bt.WEEKDAYS]))
    week = next(w for w in range(1, 10) if (w + 1) % bt.EXPLORE_EVERY == 0)
    move = bt.plan_move(grid, {"posting_slots": ["19:00", "22:00"]}, iso_week=week, channel_id=1)
    assert move.explore and move.to not in ("19:00", "22:00")
    # …but never with a campaign's only slot.
    assert bt.plan_move(grid, {"posting_slots": ["19:00"]}, iso_week=week, channel_id=1) is None


def test_judging_a_move():
    ev, params = {"median_24h": 100, "from_score": 1.0}, {"from": "19:00", "to": "06:30"}
    worse = _samples([("mon", 6, 50, 4)])
    better = _samples([("mon", 6, 150, 4)])
    assert bt.judge_move(ev, params, worse, days_since=7) == "wait"
    assert bt.judge_move(ev, params, worse, days_since=15) == "revert"
    assert bt.judge_move(ev, params, better, days_since=15) == "keep"
    assert bt.judge_move(ev, params, worse[:1], days_since=15) == "wait"
    assert bt.judge_move(ev, params, worse[:1], days_since=30) == "keep"


# ── views at fixed ages ──────────────────────────────────────────────────────
def test_snapshot_windows():
    from services.analytics_service import pending_snapshots

    assert pending_snapshots({}, 1) == []
    assert pending_snapshots({}, 3) == ["views_2h"]
    assert pending_snapshots({}, 30) == ["views_24h"]
    assert pending_snapshots({"views_24h": 5}, 30) == []
    assert pending_snapshots({}, 80) == ["views_72h"]
    assert pending_snapshots({}, 170) == ["views_7d"]
    assert pending_snapshots({}, 200) == []          # a 9-day count is not a 7-day count


def _published(session, campaign, ep, when, **stats):
    from database.models import Task
    from database.types import TaskStatus

    t = Task(campaign_id=campaign.id, user_id=campaign.user_id, episode_number=ep,
             status=TaskStatus.COMPLETED, published_video_id=f"v{campaign.id}-{ep}",
             published_at=when, finished_at=when, stats_json=stats or None)
    session.add(t)
    session.commit()
    return t


def _campaign(session, user, channel, **cfg):
    from database.models import Campaign
    from database.types import CampaignStatus

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=99,
                 current_episode=0, status=CampaignStatus.active,
                 config_json={"timezone": "UTC", **cfg})
    session.add(c)
    session.commit()
    return c


def test_a_three_day_old_video_gets_its_72h_snapshot_without_touching_views(
        session, user, channel, monkeypatch):
    from services import analytics_service as an

    camp = _campaign(session, user, channel, posting_slots=["19:00"])
    now = datetime(2026, 10, 20, 12)
    old = _published(session, camp, 1, now - timedelta(hours=80), views=900, views_24h=500)
    monkeypatch.setattr(an, "fetch_youtube_early_stats",
                        lambda ch, ids: {i: {"views": 1234, "likes": 1, "comments": 0} for i in ids})
    monkeypatch.setattr(an, "judge_flops", lambda db, c: 0)
    assert an.collect_early_stats(session, now=now) == 1
    session.refresh(old)
    assert old.stats_json["views_72h"] == 1234
    assert old.stats_json["views"] == 900          # the mature pass owns `views` past 48h
    assert an.collect_early_stats(session, now=now + timedelta(hours=2)) == 0   # nothing owed


# ── per-weekday slots ────────────────────────────────────────────────────────
def test_a_weekday_override_replaces_that_days_slots_only():
    from types import SimpleNamespace

    from workers.scheduler import slots_for_day, upcoming_slots

    cfg = {"posting_slots": ["19:00"], "slots_by_day": {"mon": ["06:30"]}, "timezone": "UTC"}
    monday, tuesday = datetime(2026, 10, 19), datetime(2026, 10, 20)
    assert slots_for_day(cfg, monday) == ["06:30"] and slots_for_day(cfg, tuesday) == ["19:00"]
    nxt = upcoming_slots(SimpleNamespace(config_json=cfg), 3, now=datetime(2026, 10, 18, 20))
    assert [d.strftime("%a %H:%M") for d in nxt] == ["Mon 06:30", "Tue 19:00", "Wed 19:00"]


def test_applying_a_weekday_move_and_moving_it_back(session, user, channel):
    from database.models import AutopilotAction
    from workers.scheduler import apply_autopilot_action

    camp = _campaign(session, user, channel, posting_slots=["19:00", "22:00"])

    def _apply(frm, to, scope):
        a = AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=camp.id,
                            kind="slot_change", summary="s", evidence={},
                            params={"from": frm, "to": to, "scope": scope})
        session.add(a)
        session.commit()
        assert apply_autopilot_action(session, a)
        session.refresh(camp)

    _apply("19:00", "06:30", "mon")
    assert camp.config_json["slots_by_day"] == {"mon": ["06:30", "22:00"]}
    assert camp.config_json["posting_slots"] == ["19:00", "22:00"]
    _apply("06:30", "19:00", "mon")
    assert camp.config_json["slots_by_day"] == {}          # back to the daily slots


# ── the engine end to end ────────────────────────────────────────────────────
def test_engine_files_one_move_then_undoes_it_when_it_did_worse(session, user, channel):
    from database.models import AutopilotAction
    from workers.scheduler import apply_autopilot_action, autopilot_best_time_channel

    camp = _campaign(session, user, channel, posting_slots=["19:00", "22:00"])
    now = datetime(2026, 10, 21, 12)   # ISO week 43: not an explore week for this channel id
    ep = 0
    for back in range(1, 15):
        day = now - timedelta(days=back)
        for hour, views in ((7, 400), (19, 100), (22, 110)):
            ep += 1
            _published(session, camp, ep, day.replace(hour=hour), views_24h=views)
    if (now.isocalendar()[1] + channel.id) % bt.EXPLORE_EVERY == 0:
        now += timedelta(days=7)
    assert autopilot_best_time_channel(session, channel, now=now) == 1
    a = session.query(AutopilotAction).filter_by(kind="slot_change").one()
    assert a.params == {"from": "19:00", "to": "06:30", "scope": "all"}
    assert a.evidence["source"] == "best_time" and a.evidence["median_24h"]
    assert autopilot_best_time_channel(session, channel, now=now) == 0   # one pending is enough

    assert apply_autopilot_action(session, a)
    a.resolved_at = now
    session.commit()
    later = now + timedelta(days=15)
    for back in range(1, 6):   # the new 06:30 slot did badly
        ep += 1
        _published(session, camp, ep, (later - timedelta(days=back)).replace(hour=6, minute=30),
                   views_24h=20)
    assert autopilot_best_time_channel(session, channel, now=later) == 1
    undo = (session.query(AutopilotAction).filter_by(kind="slot_change", status="proposed").one())
    assert undo.params == {"from": "06:30", "to": "19:00", "scope": "all"}
    assert undo.evidence["revert"] and undo.evidence["revert_of"] == a.id
    session.refresh(a)
    assert a.evidence["judged"] == "revert"


def test_a_dismissed_move_is_not_refiled_every_pass(session, user, channel):
    from database.models import AutopilotAction
    from workers.scheduler import autopilot_best_time_channel

    camp = _campaign(session, user, channel, posting_slots=["19:00", "22:00"])
    now = datetime(2026, 10, 21, 12)
    if (now.isocalendar()[1] + channel.id) % bt.EXPLORE_EVERY == 0:
        now += timedelta(days=7)
    ep = 0
    for back in range(1, 15):
        for hour, views in ((7, 400), (19, 100), (22, 110)):
            ep += 1
            _published(session, camp, ep, (now - timedelta(days=back)).replace(hour=hour),
                       views_24h=views)
    assert autopilot_best_time_channel(session, channel, now=now) == 1
    a = session.query(AutopilotAction).filter_by(kind="slot_change").one()
    a.status, a.resolved_at = "dismissed", now
    session.commit()
    assert autopilot_best_time_channel(session, channel, now=now + timedelta(hours=3)) == 0


# ── the style each video was made with ───────────────────────────────────────
def test_style_snapshot_keeps_the_set_knobs_and_the_topic():
    from workers.video_worker import style_snapshot

    snap = style_snapshot({"voice": "vi-VN-HoaiMyNeural", "color_grade": "", "rate_pct": -5,
                           "persona": "long private text"}, "Ăn sáng")
    assert snap == {"voice": "vi-VN-HoaiMyNeural", "rate_pct": -5, "topic": "Ăn sáng"}


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def test_channels_page_shows_the_audience_map(client, session, user, channel):
    camp = _campaign(session, user, channel, posting_slots=["19:00"])
    for i in range(3):
        _published(session, camp, i + 1, datetime.utcnow() - timedelta(days=i + 1), views_24h=100)
    r = client.get("/channels")
    assert r.status_code == 200
    assert "Best time to post" in r.text and "learning — 3/12 videos" in r.text
