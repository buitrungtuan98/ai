"""R28 — the factory switches: auto-create, auto-improve, auto-resume (full-auto only, each OFF by
default).

Full-auto already stopped laggards, extended winners and planned successors near a cap. What still
needed a human: a channel left with nothing running, a creative tweak waiting for a click, and a
campaign the circuit breaker stopped for a fault that had long since healed.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def quiet(monkeypatch):
    """No AI design, no render queue — these tests are about the decisions, not their side jobs."""
    from workers import scheduler, video_worker

    monkeypatch.setattr(scheduler, "_design_successor", lambda db, parent: None)
    monkeypatch.setattr(video_worker, "hydrate_campaign", lambda db, c, **kw: [])


def _switch(session, channel, **cfg):
    channel.autopilot_json = {"mode": "autopilot", **cfg}
    session.commit()


def _campaign(session, user, channel, status, topic="Formula", **cfg):
    from database.models import Campaign

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name=topic, total_episodes=10,
                 current_episode=10, status=status,
                 config_json={"language": "en", "auto_publish": True, **cfg})
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


# ── Settings ─────────────────────────────────────────────────────────────────
def test_switches_default_off_and_bounds_clamp():
    from core import autopilot

    fs = autopilot.factory_settings(SimpleNamespace(autopilot_json={"mode": "autopilot"}))
    assert not (fs["auto_create"] or fs["auto_tune"] or fs["auto_resume"])
    assert (fs["min_active"], fs["max_active"]) == (1, autopilot.DEFAULT_MAX_ACTIVE)
    fs = autopilot.factory_settings(SimpleNamespace(autopilot_json={"max_active": 99,
                                                                    "min_active": 50}))
    assert fs["max_active"] == autopilot.MAX_ACTIVE_CEILING and fs["min_active"] == fs["max_active"]


def test_refill_parent_is_the_best_formula_and_a_laggard_only_last():
    from core import autopilot

    a, b, c = (SimpleNamespace(id=i) for i in (1, 2, 3))
    classes = {1: {"label": "healthy", "retention": 40}, 2: {"label": "winner", "retention": 55},
               3: {"label": "underperforming", "retention": 10}}
    assert autopilot.pick_refill_parent([a, b, c], classes) is b
    assert autopilot.pick_refill_parent([c], classes) is c       # last resort only…
    assert not autopilot.is_proven_parent(classes[3])            # …and never as a clone


# ── Auto-create ──────────────────────────────────────────────────────────────
def test_refill_creates_one_review_first_campaign_then_cools_down(session, user, channel, quiet):
    from database.models import Campaign
    from database.types import CampaignStatus
    from workers import scheduler

    parent = _campaign(session, user, channel, CampaignStatus.completed)
    assert scheduler.autopilot_refill_channel(session, channel) == 0      # switch off → nothing

    _switch(session, channel, auto_create=True)
    assert scheduler.autopilot_refill_channel(session, channel) == 1
    new = session.query(Campaign).filter(Campaign.id != parent.id).one()
    assert new.status == CampaignStatus.active
    assert new.config_json["auto_publish"] is False                      # training wheels

    new.status = CampaignStatus.completed                                 # channel idle again…
    session.commit()
    assert scheduler.autopilot_refill_channel(session, channel) == 0      # …but inside cooldown


def test_refill_leaves_a_pending_campaign_to_the_operator(session, user, channel, quiet):
    from database.types import CampaignStatus
    from workers import scheduler

    _campaign(session, user, channel, CampaignStatus.completed)
    _campaign(session, user, channel, CampaignStatus.pending, topic="Queued by a human")
    _switch(session, channel, auto_create=True)
    assert scheduler.autopilot_refill_channel(session, channel) == 0


# ── Auto-improve ─────────────────────────────────────────────────────────────
def test_tune_auto_applies_only_with_the_switch_and_keeps_the_old_values(session, user, channel,
                                                                         quiet):
    from database.models import AutopilotAction
    from database.types import CampaignStatus
    from workers import scheduler

    camp = _campaign(session, user, channel, CampaignStatus.active, caption_theme="classic")
    _switch(session, channel)
    tune = AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=camp.id,
                           kind="tune", summary="Try neon captions", evidence={},
                           params={"caption_theme": "neon"})
    session.add(tune)
    session.commit()
    scheduler.autopilot_autoapply_channel(session, channel)
    session.refresh(tune)
    assert tune.status == "proposed"                                      # off → still waiting

    _switch(session, channel, auto_tune=True)
    scheduler.autopilot_autoapply_channel(session, channel)
    session.refresh(tune)
    session.refresh(camp)
    assert tune.status == "applied" and camp.config_json["caption_theme"] == "neon"
    assert tune.evidence["previous"] == {"caption_theme": "classic"}


# ── Auto-resume ──────────────────────────────────────────────────────────────
def _failed(session, camp, user, msg, hours_ago, ep=1):
    from database.models import Task
    from database.types import TaskStatus

    t = Task(campaign_id=camp.id, user_id=user.id, episode_number=ep, status=TaskStatus.FAILED,
             error_message=msg, finished_at=datetime.utcnow() - timedelta(hours=hours_ago))
    session.add(t)
    session.commit()


def test_resume_restarts_a_healed_fault_and_never_a_human_one(session, user, channel, quiet):
    from database.types import CampaignStatus
    from workers import scheduler

    _switch(session, channel, auto_resume=True)
    blip = _campaign(session, user, channel, CampaignStatus.failed, topic="Blip")
    _failed(session, blip, user, "ConnectionResetError: connection reset by peer", 25)
    fresh = _campaign(session, user, channel, CampaignStatus.failed, topic="Fresh")
    _failed(session, fresh, user, "ConnectionResetError: connection reset by peer", 2)
    voice = _campaign(session, user, channel, CampaignStatus.failed, topic="Voice")
    _failed(session, voice, user,
            "aiohttp.client_exceptions.WSServerHandshakeError: 403, message='Invalid'", 48)

    assert scheduler.autopilot_resume_channel(session, channel) == 1
    for c in (blip, fresh, voice):
        session.refresh(c)
    assert blip.status == CampaignStatus.active
    assert fresh.status == CampaignStatus.failed      # still inside the 24h wait
    assert voice.status == CampaignStatus.failed      # needs a deploy, not a retry


def test_resume_gives_up_after_the_cap_without_a_success(session, user, channel, quiet):
    from database.types import CampaignStatus
    from workers import scheduler

    _switch(session, channel, auto_resume=True)
    camp = _campaign(session, user, channel, CampaignStatus.failed)
    _failed(session, camp, user, "TimeoutError: timed out", 30)
    for _ in range(scheduler.MAX_AUTO_RESUMES):
        assert scheduler.autopilot_resume_channel(session, channel) == 1
        camp.status = CampaignStatus.failed           # the breaker trips again
        session.commit()
    assert scheduler.autopilot_resume_channel(session, channel) == 0


# ── The form ─────────────────────────────────────────────────────────────────
def test_channel_form_saves_the_switches(client):
    from database.db_session import SessionLocal
    from database.models import Channel

    client.post("/channels/facebook", data={"channel_name": "F", "page_id": "1",
                                            "page_access_token": "t"}, follow_redirects=False)
    db = SessionLocal()
    cid = db.query(Channel).first().id
    db.close()
    client.post(f"/channels/{cid}/autopilot", data={
        "mode": "autopilot", "auto_create": "on", "auto_resume": "on",
        "min_active": "9", "max_active": "3"}, follow_redirects=False)
    db = SessionLocal()
    cfg = db.query(Channel).first().autopilot_json
    db.close()
    assert cfg["auto_create"] and cfg["auto_resume"] and not cfg["auto_tune"]
    assert (cfg["min_active"], cfg["max_active"]) == (3, 3)


# ── R29: a stopped campaign is finished, and auto-create replaces it ──────────
def test_a_wind_down_completes_the_campaign_and_auto_create_replaces_it(session, user, channel,
                                                                        quiet, monkeypatch):
    from types import SimpleNamespace

    from core import autopilot
    from database.models import AutopilotAction, Campaign
    from database.types import CampaignStatus
    from workers import scheduler

    flop = _campaign(session, user, channel, CampaignStatus.active, topic="Flop")
    flop.current_episode = 7
    session.commit()
    _switch(session, channel, auto_create=True)
    stop = AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=flop.id,
                           kind="wind_down", summary="Wind down Flop", evidence={},
                           params={"total_episodes": 7})
    session.add(stop)
    session.commit()
    scheduler.autopilot_review_rules_channel(session, None, channel, "autopilot")   # R38, no key
    scheduler.autopilot_autoapply_channel(session, channel)
    assert scheduler.finish_stranded_campaign(session, flop) is True     # nothing left to publish
    session.refresh(flop)
    assert flop.status == CampaignStatus.completed      # was: "active" at 7/7 forever

    # Its only campaign was stopped: a replacement needs a FRESH angle, never a clone — even
    # when the stop came from the flop breaker and the label still reads "healthy" (R30).
    monkeypatch.setattr(autopilot, "classify_campaigns",
                        lambda db, cs: {c.id: {"label": "healthy"} for c in cs})
    assert scheduler.autopilot_refill_channel(session, channel) == 0   # no AI design → wait

    fresh = SimpleNamespace(topic_name="A New Angle", total_episodes=30,
                            **{k: None for k in scheduler._SUCCESSOR_CREATIVE_KEYS})
    monkeypatch.setattr(scheduler, "_design_successor", lambda db, parent: fresh)
    assert scheduler.autopilot_refill_channel(session, channel) == 1
    new = session.query(Campaign).filter(Campaign.id != flop.id).one()
    assert new.topic_name == "A New Angle" and new.status == CampaignStatus.active
    assert new.total_episodes == 30          # the fresh plan's length, not the flop's 7-episode stop


def test_a_wound_down_campaign_publishes_its_extras_before_it_closes(session, user, channel, quiet,
                                                                      tmp_path):
    from database.models import BufferPoolItem
    from database.types import BufferStatus, CampaignStatus
    from workers import scheduler

    camp = _campaign(session, user, channel, CampaignStatus.active)   # 10/10 after a wind-down
    f = tmp_path / "x.mp4"
    f.write_bytes(b"v")
    session.add(BufferPoolItem(campaign_id=camp.id, channel_id=channel.id, episode_number=11,
                               video_path=str(f), status=BufferStatus.ready))
    session.commit()
    assert scheduler.finish_stranded_campaign(session, camp) is False   # the extra goes out first
    session.refresh(camp)
    assert camp.status == CampaignStatus.active


def test_a_breaker_stopped_campaign_holds_its_slot(session, user, channel, quiet):
    from database.types import CampaignStatus
    from workers import scheduler

    _campaign(session, user, channel, CampaignStatus.completed)
    _campaign(session, user, channel, CampaignStatus.failed, topic="Dead key")
    _switch(session, channel, auto_create=True)
    assert scheduler.autopilot_refill_channel(session, channel) == 0    # no replacement spawned


def test_resume_reads_the_real_cause_and_respects_max_active(session, user, channel, quiet):
    from database.types import CampaignStatus
    from workers import scheduler

    _switch(session, channel, auto_resume=True, max_active=1)
    camp = _campaign(session, user, channel, CampaignStatus.failed)
    _failed(session, camp, user, "ConnectionResetError: connection reset by peer", 30, ep=1)
    _failed(session, camp, user, "Rejected in review: weak hook", 26, ep=2)   # newer, not the cause
    busy = _campaign(session, user, channel, CampaignStatus.active, topic="Running")
    assert scheduler.autopilot_resume_channel(session, channel) == 0      # channel already full
    busy.status = CampaignStatus.completed
    session.commit()
    assert scheduler.autopilot_resume_channel(session, channel) == 1      # the reject didn't block it


# ── R34: disk retention ──────────────────────────────────────────────────────
def test_prune_media_frees_dead_libraries_orphan_buffers_and_stale_music(session, user, channel,
                                                                         tmp_path, monkeypatch):
    import os
    import time

    from core.config import settings
    from database.types import CampaignStatus
    from workers import scheduler

    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path))
    live = _campaign(session, user, channel, CampaignStatus.active)
    done_old = _campaign(session, user, channel, CampaignStatus.completed, topic="Old")
    done_new = _campaign(session, user, channel, CampaignStatus.completed, topic="New")
    old = time.time() - 30 * 86400

    def lib(cid, aged=False):
        d = tmp_path / "library" / str(cid)
        d.mkdir(parents=True)
        f = d / "ep_1.mp4"
        f.write_bytes(b"v")
        if aged:
            os.utime(f, (old, old))
        return d

    keep_live, keep_new = lib(live.id, aged=True), lib(done_new.id)
    gone_done, gone_deleted = lib(done_old.id, aged=True), lib(9999)
    orphan_buf = tmp_path / "buffer" / "9999"
    orphan_buf.mkdir(parents=True)
    live_buf = tmp_path / "buffer" / str(live.id)
    live_buf.mkdir(parents=True)
    music = tmp_path / "music_cache"
    music.mkdir()
    (music / "fresh.mp3").write_bytes(b"m")
    stale = music / "stale.mp3"
    stale.write_bytes(b"m")
    os.utime(stale, (old, old))

    out = scheduler.prune_media(session)
    assert out == {"library": 2, "buffer": 1, "music": 1}
    assert keep_live.exists() and keep_new.exists() and live_buf.exists()
    assert not gone_done.exists() and not gone_deleted.exists() and not orphan_buf.exists()
    assert (music / "fresh.mp3").exists() and not stale.exists()


def test_a_paused_campaign_holds_its_videos_for_two_weeks_not_forever(session, user, channel,
                                                                      tmp_path, monkeypatch):
    from database.models import BufferPoolItem
    from database.types import BufferStatus, CampaignStatus
    from workers import scheduler, task_queue

    monkeypatch.setattr(task_queue, "queued_publish_buffer_ids", lambda: set())
    camp = _campaign(session, user, channel, CampaignStatus.failed)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"v")
    t0 = datetime.utcnow()
    item = BufferPoolItem(campaign_id=camp.id, channel_id=channel.id, episode_number=1,
                          video_path=str(f), status=BufferStatus.ready, metadata_json={},
                          ready_at=t0 - timedelta(days=1))
    session.add(item)
    session.commit()
    scheduler.expire_stale_buffers(session, now=t0)                          # hold starts
    scheduler.expire_stale_buffers(session, now=t0 + timedelta(days=10))     # still held
    session.refresh(item)
    assert item.status == BufferStatus.ready
    scheduler.expire_stale_buffers(session, now=t0 + timedelta(days=15))     # hold is over
    session.refresh(item)
    assert item.status == BufferStatus.expired and not f.exists()
