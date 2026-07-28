"""Channel growth series: daily snapshots, once-per-day collection, and the correlation view."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def _snap(session, channel, day, subs=None, views=None, videos=None):
    from database.models import ChannelSnapshot

    s = ChannelSnapshot(channel_id=channel.id, day=day, subscribers=subs, views=views, videos=videos)
    session.add(s)
    session.commit()
    return s


def _published(session, user, channel, episode, when):
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus

    camp = session.query(Campaign).filter_by(channel_id=channel.id).first()
    if camp is None:
        camp = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Growth",
                        total_episodes=30, status=CampaignStatus.active, config_json={})
        session.add(camp)
        session.commit()
        session.refresh(camp)
    session.add(Task(campaign_id=camp.id, user_id=user.id, episode_number=episode,
                     status=TaskStatus.COMPLETED, finished_at=when))
    session.commit()


# ── Collection ───────────────────────────────────────────────────────────────
def test_collection_writes_one_row_per_channel_per_day(session, channel, monkeypatch):
    from database.models import ChannelSnapshot
    from services import analytics_service as svc

    calls = []

    def fake(ch):
        calls.append(ch.id)
        return {"subscribers": 1000, "views": 50000, "videos": 12}

    monkeypatch.setattr(svc, "fetch_channel_totals", fake)

    assert svc.collect_channel_snapshots(session) == 1
    # Called again the same day: no row, and — the point — no API call spent.
    assert svc.collect_channel_snapshots(session) == 0
    assert calls == [channel.id]
    assert session.query(ChannelSnapshot).count() == 1


def test_a_failing_channel_never_blocks_the_others(session, user, channel, monkeypatch):
    from database.models import Channel
    from database.types import Platform
    from services import analytics_service as svc

    good = Channel(user_id=user.id, platform=Platform.youtube, channel_name="Good",
                   encrypted_credentials="{}")
    session.add(good)
    session.commit()
    session.refresh(good)

    def fake(ch):
        if ch.id == channel.id:
            raise RuntimeError("token revoked")
        return {"subscribers": 5, "views": 6, "videos": 7}

    monkeypatch.setattr(svc, "fetch_channel_totals", fake)
    assert svc.collect_channel_snapshots(session) == 1     # the healthy channel still got sampled


def test_a_hidden_subscriber_count_is_none_not_zero(monkeypatch):
    """"Hidden" and "zero subscribers" must not look the same."""
    from database.models import Channel
    from database.types import Platform
    from services import analytics_service as svc

    class FakeYouTube:
        def channels(self):
            return self

        def list(self, **_kw):
            return self

        def execute(self):
            return {"items": [{"statistics": {"hiddenSubscriberCount": "true",
                                              "viewCount": "9000", "videoCount": "40"}}]}

    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: FakeYouTube())
    monkeypatch.setattr("services.youtube_service.build_credentials", lambda ch: object())

    ch = Channel(id=1, user_id=1, platform=Platform.youtube, channel_name="Hidden",
                 encrypted_credentials="{}")
    totals = svc.fetch_youtube_channel_totals(ch)
    assert totals["subscribers"] is None
    assert totals["views"] == 9000 and totals["videos"] == 40


def test_the_snapshot_day_follows_the_channels_own_timezone(session, channel):
    from services import analytics_service as svc

    channel.profile_json = {"timezone": "Asia/Ho_Chi_Minh"}   # UTC+7
    session.commit()
    # 22:00 UTC is already the NEXT day in Ho Chi Minh City.
    assert svc._channel_local_day(channel, datetime(2026, 7, 28, 22, 0)) == date(2026, 7, 29)


def test_a_bad_stored_timezone_still_collects(session, channel):
    from services import analytics_service as svc

    channel.profile_json = {"timezone": "Not/AZone"}
    session.commit()
    assert svc._channel_local_day(channel, datetime(2026, 7, 28, 12, 0)) == date(2026, 7, 28)


# ── The correlation view ─────────────────────────────────────────────────────
def test_growth_pairs_daily_deltas_with_episodes_published_that_day(session, user, channel):
    from services import analytics_service as svc

    today = datetime.utcnow().date()
    _snap(session, channel, today - timedelta(days=2), subs=1000, views=40000)
    _snap(session, channel, today - timedelta(days=1), subs=1050, views=41500)
    _snap(session, channel, today, subs=1120, views=43000)
    _published(session, user, channel, 1, datetime.utcnow() - timedelta(days=1))
    _published(session, user, channel, 2, datetime.utcnow() - timedelta(days=1))
    _published(session, user, channel, 3, datetime.utcnow())

    g = svc.channel_growth(session, channel.id)
    assert g["days"] == 3 and g["measurable"] is True
    assert g["subscribers"] == 1120 and g["views"] == 43000
    assert [p["sub_delta"] for p in g["points"]] == [None, 50, 70]
    assert [p["view_delta"] for p in g["points"]] == [None, 1500, 1500]
    assert [p["published"] for p in g["points"]] == [0, 2, 1]
    assert g["sub_growth"] == 120 and g["view_growth"] == 3000
    assert g["published"] == 3


def test_the_first_sample_has_no_delta_rather_than_a_zero_one(session, channel):
    """A single sample means "unknown", which must not render as a flat line at zero."""
    from services import analytics_service as svc

    _snap(session, channel, datetime.utcnow().date(), subs=500, views=9000)
    g = svc.channel_growth(session, channel.id)
    assert g["days"] == 1 and g["measurable"] is False
    assert g["points"][0]["sub_delta"] is None
    assert g["sub_growth"] is None and g["view_growth"] is None


def test_a_hidden_count_never_fabricates_a_delta(session, channel):
    from services import analytics_service as svc

    today = datetime.utcnow().date()
    _snap(session, channel, today - timedelta(days=1), subs=None, views=1000)
    _snap(session, channel, today, subs=None, views=1400)
    g = svc.channel_growth(session, channel.id)
    assert g["sub_growth"] is None                  # never 0 from two hidden counts
    assert g["view_growth"] == 400


def test_growth_is_scoped_to_the_one_channel(session, user, channel):
    from database.models import Channel
    from database.types import Platform
    from services import analytics_service as svc

    other = Channel(user_id=user.id, platform=Platform.youtube, channel_name="Other",
                    encrypted_credentials="{}")
    session.add(other)
    session.commit()
    session.refresh(other)
    today = datetime.utcnow().date()
    _snap(session, channel, today, subs=10)
    _snap(session, other, today, subs=99999)

    assert svc.channel_growth(session, channel.id)["subscribers"] == 10


def test_old_samples_fall_outside_the_window(session, channel):
    from services import analytics_service as svc

    _snap(session, channel, datetime.utcnow().date() - timedelta(days=90), subs=1)
    assert svc.channel_growth(session, channel.id, days=30)["days"] == 0


def test_the_channels_page_renders_the_growth_chart(client, session, user, channel):
    today = datetime.utcnow().date()
    _snap(session, channel, today - timedelta(days=1), subs=1000, views=40000)
    _snap(session, channel, today, subs=1075, views=41000)
    _published(session, user, channel, 1, datetime.utcnow())

    body = " ".join(client.get("/channels").text.split())
    assert "1,075" in body and "+75" in body        # current subs and the growth delta
    assert "polyline" in body                       # the hand-rolled SVG line, no chart library
    assert "Bars = episodes published per day" in body


def test_a_single_sample_explains_itself_instead_of_drawing_a_flat_line(client, session, channel):
    _snap(session, channel, datetime.utcnow().date(), subs=1000, views=40000)

    body = client.get("/channels").text
    assert "the curve appears tomorrow" in body
    assert "polyline" not in body
