"""Factory vitals: cumulative reach, today's render outcomes, and stdlib host readings."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def _campaign(session, user, channel):
    from database.models import Campaign
    from database.types import CampaignStatus

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Vitals", total_episodes=9,
                 status=CampaignStatus.active, config_json={})
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _task(session, campaign, user, episode, **kw):
    from database.models import Task

    t = Task(campaign_id=campaign.id, user_id=user.id, episode_number=episode, **kw)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


# ── Host readings (stdlib only — no psutil dependency) ───────────────────────
def test_host_snapshot_always_has_every_key():
    from core import host

    snap = host.snapshot()
    for key in ("cores", "load", "load_pct", "ram_used_mb", "ram_total_mb", "ram_pct"):
        assert key in snap
    assert snap["cores"] >= 1


def test_load_percent_is_per_core_and_capped(monkeypatch):
    from core import host

    monkeypatch.setattr(host, "cpu_cores", lambda: 4)
    monkeypatch.setattr(host, "load_average", lambda: 2.0)
    assert host.snapshot()["load_pct"] == 50          # 2 of 4 cores busy

    monkeypatch.setattr(host, "load_average", lambda: 40.0)
    assert host.snapshot()["load_pct"] == 100         # capped — "how far past saturated" is noise


def test_memory_excludes_reclaimable_page_cache(tmp_path, monkeypatch):
    """Total − Free would show this box at ~95% while it is perfectly healthy."""
    from core import host

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       24000000 kB\nMemFree:          500000 kB\n"
                       "MemAvailable:   18000000 kB\nBuffers:          100000 kB\n")
    monkeypatch.setattr(host, "_MEMINFO", str(meminfo))

    used_mb, total_mb = host.memory()
    assert total_mb == 24000000 // 1024
    assert used_mb == (24000000 - 18000000) // 1024   # Available, not Free
    assert host.snapshot()["ram_pct"] == 25


def test_unreadable_host_values_degrade_to_none(monkeypatch):
    """A non-Linux dev box must render "—", never raise."""
    from core import host

    monkeypatch.setattr(host, "_MEMINFO", "/definitely/not/here")
    monkeypatch.setattr(host, "load_average", lambda: None)
    snap = host.snapshot()
    assert snap["load"] is None and snap["load_pct"] is None
    assert snap["ram_pct"] is None and snap["ram_used_mb"] is None


# ── Factory-wide numbers ─────────────────────────────────────────────────────
def test_views_are_summed_and_qualified_by_how_many_episodes_are_measured(session, user, channel):
    """A bare total would read as "the whole catalogue" when analytics lag ~2 days behind."""
    import main
    from database.types import TaskStatus

    camp = _campaign(session, user, channel)
    _task(session, camp, user, 1, status=TaskStatus.COMPLETED, finished_at=datetime.utcnow(),
          stats_json={"views": 1200, "avg_pct_viewed": 61.0})
    _task(session, camp, user, 2, status=TaskStatus.COMPLETED, finished_at=datetime.utcnow(),
          stats_json={"views": 800})
    # Published but not measured yet: counts as published, not as views.
    _task(session, camp, user, 3, status=TaskStatus.COMPLETED, finished_at=datetime.utcnow())
    # A stats row without views (early fetch shape) must not inflate `measured`.
    _task(session, camp, user, 4, status=TaskStatus.COMPLETED, finished_at=datetime.utcnow(),
          stats_json={"avg_pct_viewed": 40.0})

    v = main._factory_vitals(session, user.id)
    assert v["views"] == 2000 and v["measured"] == 2
    assert v["published_total"] == 4


def test_todays_failure_rate_and_machine_time(session, user, channel):
    import main
    from database.types import TaskStatus

    camp = _campaign(session, user, channel)
    now = datetime.utcnow()
    _task(session, camp, user, 1, status=TaskStatus.COMPLETED, finished_at=now,
          render_json={"render_seconds": 600})
    _task(session, camp, user, 2, status=TaskStatus.FAILED, finished_at=now,
          render_json={"render_seconds": 120})
    _task(session, camp, user, 3, status=TaskStatus.COMPLETED, finished_at=now)

    v = main._factory_vitals(session, user.id)
    assert v["renders_today"] == 3 and v["failed_today"] == 1
    assert v["fail_pct_today"] == 33
    assert v["render_minutes_today"] == 12          # 720s of real render time


def test_yesterdays_renders_do_not_count_toward_today(session, user, channel):
    import main
    from database.types import TaskStatus

    camp = _campaign(session, user, channel)
    _task(session, camp, user, 1, status=TaskStatus.FAILED,
          finished_at=datetime.utcnow() - timedelta(days=2), render_json={"render_seconds": 999})

    v = main._factory_vitals(session, user.id)
    assert v["renders_today"] == 0 and v["fail_pct_today"] is None
    assert v["render_minutes_today"] == 0


def test_an_unfinished_render_is_not_counted_either(session, user, channel):
    """In-flight work has no outcome yet — including it would drag the success rate down."""
    import main
    from database.types import TaskStatus

    camp = _campaign(session, user, channel)
    _task(session, camp, user, 1, status=TaskStatus.RENDERING)

    assert main._factory_vitals(session, user.id)["renders_today"] == 0


def test_vitals_are_tenant_scoped(session, user, channel):
    import main
    from database.models import Channel, User
    from database.types import Platform, TaskStatus

    camp = _campaign(session, user, channel)
    _task(session, camp, user, 1, status=TaskStatus.COMPLETED, finished_at=datetime.utcnow(),
          stats_json={"views": 10})

    other = User(firebase_uid="nosy", gemini_api_key="k")
    session.add(other)
    session.commit()
    session.refresh(other)
    och = Channel(user_id=other.id, platform=Platform.youtube, channel_name="Theirs",
                  encrypted_credentials="{}")
    session.add(och)
    session.commit()
    session.refresh(och)
    ocamp = _campaign(session, other, och)
    _task(session, ocamp, other, 1, status=TaskStatus.COMPLETED, finished_at=datetime.utcnow(),
          stats_json={"views": 999999})

    assert main._factory_vitals(session, user.id)["views"] == 10


def test_the_dashboard_reports_the_factory_in_one_card(client, session, user, channel):
    """Two adjacent cards with the same layout ("scorecard" + "vitals") became one "Factory" card,
    and CPU/RAM moved into the health strip where the rest of the infra signals live (ADR-066)."""
    from database.types import TaskStatus

    camp = _campaign(session, user, channel)
    _task(session, camp, user, 1, status=TaskStatus.COMPLETED, finished_at=datetime.utcnow(),
          stats_json={"views": 4321})

    body = " ".join(client.get("/").text.split())    # collapse template line breaks
    assert ">Factory<" in body
    assert "Factory vitals" not in body and "Factory scorecard" not in body
    assert "4,321" in body                     # thousands-separated, one measured episode
    assert "across 1 measured episode" in body
    # Host vitals live in the health strip now, not in a card of their own.
    strip = body.split('id="health-strip"', 1)[1].split("</div>", 1)[0]
    assert "CPU" in strip or "RAM" in strip


def test_the_factory_card_explains_itself_with_no_data_yet(client):
    body = client.get("/").text
    assert "no analytics collected yet" in body
    assert "nothing finished today yet" in body


def test_the_six_stat_tiles_are_gone(client):
    """Every number they showed already lived in the triage card, the health strip or the Factory
    card — six tiles were a screen of scrolling that answered nothing new (ADR-066)."""
    body = client.get("/").text
    assert 'class="stats"' not in body
    for retired in ("Connected channels", "Active campaigns", "Videos published", "In pipeline"):
        assert retired not in body
