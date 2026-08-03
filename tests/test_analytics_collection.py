"""ADR-076 — the measurement half of the loop must not lose what it measured.

`collect_stats` rebuilt each episode's `stats_json` from scratch every pass, so anything the CURRENT
pass didn't return was deleted. Two everyday things trigger that: a rate-limited bonus fetch, and an
episode simply falling past the per-video retention cap. What vanished were the retention curve, the
scene-level drop attribution and the top-viewer country — the inputs to the playbook distiller and
the audience-match verdict. Nothing reported it, because losing data looks exactly like not having
collected it yet.
"""
from __future__ import annotations

import datetime as dt

import pytest


def _seed(db_extra_tasks=1):
    from database.db_session import SessionLocal
    from database.models import Campaign, Channel, Task, User
    from database.types import CampaignStatus, Platform, TaskStatus

    db = SessionLocal()
    u = User()
    db.add(u)
    db.commit()
    db.refresh(u)
    ch = Channel(user_id=u.id, platform=Platform.youtube, channel_name="C",
                 encrypted_credentials="{}")
    db.add(ch)
    db.commit()
    db.refresh(ch)
    c = Campaign(user_id=u.id, channel_id=ch.id, topic_name="T", total_episodes=20,
                 status=CampaignStatus.active)
    db.add(c)
    db.commit()
    db.refresh(c)
    now = dt.datetime.utcnow()
    tasks = []
    for i in range(1, db_extra_tasks + 1):
        t = Task(campaign_id=c.id, user_id=u.id, episode_number=i, status=TaskStatus.COMPLETED,
                 published_video_id=f"vid{i}", finished_at=now - dt.timedelta(days=3),
                 render_json={"scenes": [{"start": 0.0, "end": 5.0, "label": "hook"}]})
        db.add(t)
        tasks.append(t)
    db.commit()
    return db, ch, c, tasks, now


@pytest.fixture
def patched(monkeypatch):
    from services import analytics_service as A
    return A, monkeypatch


def test_a_flaky_bonus_fetch_does_not_erase_what_was_already_measured(patched):
    A, mp = patched
    db, ch, _c, (t,), now = _seed()
    t.stats_json = {"views": 100, "likes": 5, "avg_pct_viewed": 55.0,
                    "top_country": "VN", "top_country_pct": 88,
                    "retention_curve": [[0.0, 1.0], [0.5, 0.6]],
                    "drop_summary": "Lost 40% at 0:02 (hook)",
                    "fetched_at": (now - dt.timedelta(days=2)).isoformat()}
    db.commit()

    def boom(*_a, **_k):
        raise RuntimeError("HTTP 429 rate limited")

    mp.setattr(A, "fetch_youtube_stats",
               lambda _ch, _ids: {"vid1": {"views": 200, "likes": 9, "avg_pct_viewed": 57.0}})
    mp.setattr(A, "fetch_youtube_geography", boom)
    mp.setattr(A, "fetch_youtube_retention", boom)

    assert A.collect_stats(db, now=now) == 1
    db.refresh(t)
    assert t.stats_json["views"] == 200                       # the fresh numbers landed
    assert t.stats_json["retention_curve"] == [[0.0, 1.0], [0.5, 0.6]]   # …and the rest survived
    assert t.stats_json["drop_summary"] == "Lost 40% at 0:02 (hook)"
    assert t.stats_json["top_country"] == "VN" and t.stats_json["top_country_pct"] == 88
    db.close()


def test_curves_already_stored_are_not_re_fetched(patched):
    """One HTTP round trip per video, sequentially, in the scheduler thread — asking again for a
    curve that cannot change was the whole steady-state cost of the pass."""
    A, mp = patched
    db, ch, _c, tasks, now = _seed(db_extra_tasks=3)
    tasks[0].stats_json = {"retention_curve": [[0.0, 1.0]]}     # already measured
    db.commit()
    asked: list[list[str]] = []

    mp.setattr(A, "fetch_youtube_stats",
               lambda _ch, ids: {v: {"views": 1, "likes": 0, "avg_pct_viewed": 50.0} for v in ids})
    mp.setattr(A, "fetch_youtube_geography", lambda _ch, _ids: {})
    mp.setattr(A, "fetch_youtube_retention", lambda _ch, ids: (asked.append(list(ids)), {})[1])

    A.collect_stats(db, now=now)
    assert asked == [["vid2", "vid3"]]                          # vid1 not asked for again
    db.close()


def test_a_capped_pass_says_what_it_dropped_and_rotates(patched, caplog):
    """A cap that is not logged reads as 'we covered everything'; a cap in a fixed order starves the
    same tail forever."""
    A, mp = patched
    db, _ch, _c, tasks, now = _seed(db_extra_tasks=3)
    # Two already measured at different times, one never — the never-measured must lead, then oldest.
    tasks[0].stats_json = {"fetched_at": (now - dt.timedelta(days=2)).isoformat()}
    tasks[1].stats_json = {"fetched_at": (now - dt.timedelta(days=5)).isoformat()}
    db.commit()
    order: list[list[str]] = []
    mp.setattr(A, "fetch_youtube_stats", lambda _ch, ids: (order.append(list(ids)), {})[1])
    mp.setattr(A, "fetch_youtube_geography", lambda _ch, _ids: {})
    mp.setattr(A, "fetch_youtube_retention", lambda _ch, _ids: {})

    A.collect_stats(db, now=now)
    assert order == [["vid3", "vid2", "vid1"]]      # never-measured, then oldest measurement first

    with caplog.at_level("INFO"):
        assert A._capped(["a", "b", "c"], 2, "retention curves") == ["a", "b"]
    assert "Capped retention curves to 2 of 3" in caplog.text
    db.close()


class _Resp:
    def __init__(self, status):
        self.status = status


def test_a_channel_that_may_not_read_analytics_says_so_once(patched):
    """403 forever is not a blip. Publishing keeps working, so the only symptom is retention that
    never arrives — and the only trace a warning in a log nobody opens."""
    A, mp = patched
    db, ch, _c, (t,), now = _seed()

    class Denied(Exception):
        resp = _Resp(403)

    mp.setattr(A, "fetch_youtube_stats", lambda *_a, **_k: (_ for _ in ()).throw(Denied()))
    A.collect_stats(db, now=now)
    db.refresh(ch)
    assert ch.analytics_error and "Reconnect" in ch.analytics_error
    assert "403" not in ch.analytics_error          # operator-readable, never the raw error

    # …and it clears itself the moment the reconnect works, without needing a button.
    mp.setattr(A, "fetch_youtube_stats", lambda _ch, ids: {v: {"views": 1, "likes": 0,
                                                               "avg_pct_viewed": 50.0} for v in ids})
    mp.setattr(A, "fetch_youtube_geography", lambda _ch, _ids: {})
    mp.setattr(A, "fetch_youtube_retention", lambda _ch, _ids: {})
    A.collect_stats(db, now=now)
    db.refresh(ch)
    assert ch.analytics_error is None
    db.close()


def test_an_ordinary_network_error_is_not_blamed_on_the_scope(patched):
    A, mp = patched
    db, ch, _c, (_t,), now = _seed()
    mp.setattr(A, "fetch_youtube_stats",
               lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("connection reset")))
    A.collect_stats(db, now=now)
    db.refresh(ch)
    assert ch.analytics_error is None               # a blip must not tell the operator to reconnect
    db.close()


def test_growth_reports_the_span_and_the_sample_count_separately():
    """A snapshot lands once per local day only while the box is up. Calling 3 samples across a
    30-day gap 'the last 3 days' misdates the entire chart."""
    from database.models import ChannelSnapshot
    from services import analytics_service as A

    db, ch, _c, _t, now = _seed()
    today = now.date()
    for offset, subs in ((20, 100), (19, 110), (2, 300)):   # a 17-day hole in the middle
        db.add(ChannelSnapshot(channel_id=ch.id, day=today - dt.timedelta(days=offset),
                               subscribers=subs, views=None, videos=None))
    db.commit()

    g = A.channel_growth(db, ch.id, days=30, now=now)
    assert g["samples"] == 3
    assert g["days"] == 19                          # first sample to last, inclusive
    db.close()
