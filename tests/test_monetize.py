"""ADR-080 — the monetization scoreboard: distance to being paid, in the platforms' currencies.

The thresholds are public; what was missing was the measurement (watch TIME was collected nowhere).
These tests pin the progress math, its honesty rules (no data → no bar, never a guess), and the
once-per-level milestone announcements.
"""
from __future__ import annotations

import datetime as dt

from core import monetize


def _snap(session, channel, *, subs=None, watch_min=None, views90=None, day=None):
    from database.models import ChannelSnapshot

    session.add(ChannelSnapshot(channel_id=channel.id, day=day or dt.date.today(),
                                subscribers=subs, views=None, videos=None,
                                watch_minutes_365d=watch_min, views_90d=views90))
    session.commit()


def test_youtube_progress_uses_the_better_route(session, user, channel):
    # 800 subs (80%), 1,000 watch-hours (25%), 8M 90d-views (80%) → shorts route leads at 80%.
    _snap(session, channel, subs=800, watch_min=60_000, views90=8_000_000)
    p = monetize.channel_progress(session, channel)
    assert p["program"].startswith("YouTube")
    by_key = {r["key"]: r for r in p["rows"]}
    assert by_key["subs"]["pct"] == 80 and by_key["hours"]["pct"] == 25
    assert by_key["views90"]["pct"] == 80
    assert p["overall_pct"] == 80 and p["eligible"] is False
    assert "approximation" in p["note"]                     # 90d views aren't Shorts-only — said


def test_eligibility_needs_subs_plus_either_route(session, user, channel):
    _snap(session, channel, subs=1_500, watch_min=4_000 * 60, views90=1_000)
    assert monetize.channel_progress(session, channel)["eligible"] is True   # long route


def test_no_snapshot_means_no_scoreboard(session, user, channel):
    assert monetize.channel_progress(session, channel) is None


def test_facebook_minutes_are_a_lower_bound_from_our_episodes(session, user):
    from database.models import Campaign, Channel, Task
    from database.types import CampaignStatus, Platform, TaskStatus

    fb = Channel(user_id=user.id, platform=Platform.facebook, channel_name="Trang",
                 encrypted_credentials="{}")
    session.add(fb)
    session.commit()
    session.refresh(fb)
    _snap(session, fb, subs=9_000)
    cam = Campaign(user_id=user.id, channel_id=fb.id, topic_name="T", total_episodes=9,
                   status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)
    now = dt.datetime.utcnow()
    session.add_all([
        Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.COMPLETED,
             finished_at=now - dt.timedelta(days=10), stats_json={"minutes_watched": 111}),
        Task(campaign_id=cam.id, user_id=user.id, episode_number=2, status=TaskStatus.COMPLETED,
             finished_at=now - dt.timedelta(days=90),                     # outside the window
             stats_json={"minutes_watched": 999}),
    ])
    session.commit()
    p = monetize.channel_progress(session, fb)
    by_key = {r["key"]: r for r in p["rows"]}
    assert by_key["followers"]["pct"] == 90
    assert by_key["minutes"]["have"] == 111                 # 60d window only
    assert "lower bound" in p["note"]


def test_milestones_fire_once_per_level(session, user, channel, monkeypatch):
    from workers import scheduler, video_worker

    _snap(session, channel, subs=850, watch_min=10_000, views90=100)   # subs at 85% → one 80% event
    notes = []
    monkeypatch.setattr(video_worker, "_notify", lambda u, msg: notes.append(msg))
    logged = []
    monkeypatch.setattr(scheduler, "_log_action",
                        lambda db, ch, kind, summary, **k: logged.append((kind, summary)))

    assert scheduler.check_monetization_milestones(session) == 1
    assert logged and logged[0][0] == "milestone" and "80%" in logged[0][1]
    assert notes == []                                       # 80% is inbox-only, not phone-worthy

    # Same numbers tomorrow → silence.
    assert scheduler.check_monetization_milestones(session) == 0

    # Subs cross the line → the 100% event fires once, with the phone ping.
    _snap(session, channel, subs=1_200, watch_min=10_000, views90=100,
          day=dt.date.today() + dt.timedelta(days=1))
    assert scheduler.check_monetization_milestones(session) == 1
    assert notes and "💰" in notes[0]
    assert scheduler.check_monetization_milestones(session) == 0
