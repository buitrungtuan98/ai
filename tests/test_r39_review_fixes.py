"""R39 — fixes from the independent review of R35–R38: nothing the AI layers or a slot move do may
stall publishing, double-post a day, or delete a library a compilation still needs."""
from __future__ import annotations

import os
import time
from datetime import datetime

import pytest


def _campaign(session, user, channel, **cfg):
    from database.models import Campaign
    from database.types import CampaignStatus

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=20,
                 current_episode=3, status=CampaignStatus.active,
                 config_json={"timezone": "UTC", **cfg})
    session.add(c)
    session.commit()
    return c


def _move(session, user, channel, camp, frm, to, scope="all"):
    from database.models import AutopilotAction
    from workers.scheduler import apply_autopilot_action

    a = AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=camp.id,
                        kind="slot_change", summary="s", evidence={},
                        params={"from": frm, "to": to, "scope": scope})
    session.add(a)
    session.commit()
    assert apply_autopilot_action(session, a)
    session.refresh(camp)


def test_a_daily_move_carries_into_weekday_overrides(session, user, channel):
    camp = _campaign(session, user, channel, posting_slots=["12:00", "19:00"])
    _move(session, user, channel, camp, "19:00", "06:30", scope="mon")
    assert camp.config_json["slots_by_day"] == {"mon": ["12:00", "06:30"]}
    _move(session, user, channel, camp, "12:00", "14:00")           # every day, incl. Monday
    assert camp.config_json["posting_slots"] == ["14:00", "19:00"]
    assert camp.config_json["slots_by_day"] == {"mon": ["14:00", "06:30"]}


def _published_at(session, camp, ep, when):
    from database.models import Task
    from database.types import TaskStatus

    session.add(Task(campaign_id=camp.id, user_id=camp.user_id, episode_number=ep,
                     status=TaskStatus.COMPLETED, published_video_id=f"v{ep}",
                     published_at=when, finished_at=when))
    session.commit()


def _ready(session, camp, ep):
    from database.models import BufferPoolItem
    from database.types import BufferStatus

    session.add(BufferPoolItem(campaign_id=camp.id, channel_id=camp.channel_id, episode_number=ep,
                               status=BufferStatus.ready,
                               video_path="/x.mp4", metadata_json={}))
    session.commit()


def test_a_slot_moved_today_does_not_post_the_day_twice(session, user, channel):
    from workers.scheduler import publish_due_campaign

    today = datetime.utcnow().replace(hour=6, minute=30, second=0, microsecond=0)
    camp = _campaign(session, user, channel, posting_slots=["06:30"], auto_publish=True)
    _published_at(session, camp, 1, today)                              # 06:30 already posted
    _move(session, user, channel, camp, "06:30", "19:00")               # …then moved to 19:00
    _ready(session, camp, 2)
    queued = []
    at_19 = today.replace(hour=19, minute=0)
    assert publish_due_campaign(session, camp, now=at_19, enqueue=queued.append) is None
    assert queued == []


def test_catch_up_waits_on_the_day_a_slot_moved(session, user, channel):
    from workers.scheduler import catch_up_due

    camp = _campaign(session, user, channel, posting_slots=["19:00"], auto_publish=True)
    _move(session, user, channel, camp, "19:00", "06:30")
    _ready(session, camp, 1)
    ten = datetime.utcnow().replace(hour=10, minute=0, second=0, microsecond=0)
    assert catch_up_due(session, camp, now=ten) is None


def test_library_survives_while_a_compilation_still_needs_it(session, user, channel, tmp_path,
                                                             monkeypatch):
    from core.config import settings
    from database.models import Task
    from database.types import CampaignStatus, TaskStatus
    from workers import scheduler

    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path))
    camp = _campaign(session, user, channel)
    camp.status = CampaignStatus.completed
    session.add(Task(campaign_id=camp.id, user_id=user.id, episode_number=1000001,
                     video_kind="compilation", status=TaskStatus.PENDING_QUEUE))
    session.commit()
    lib = tmp_path / "library" / str(camp.id)
    lib.mkdir(parents=True)
    old = lib / "ep1.mp4"
    old.write_bytes(b"x")
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))
    scheduler.prune_media(session)
    assert old.exists()


def test_a_retained_master_ages_from_publish_not_render(tmp_path, monkeypatch):
    from core import compilation
    from core.config import settings

    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path))
    src = tmp_path / "render.mp4"
    src.write_bytes(b"x")
    rendered = time.time() - 30 * 86400
    os.utime(src, (rendered, rendered))
    dest = compilation.retain_master(7, 1, str(src))
    assert os.path.getmtime(dest) > time.time() - 60


def test_a_failed_review_is_not_reasked_every_pass_in_copilot(session, user, channel,
                                                              monkeypatch):
    from core import ai_engine
    from database.models import AutopilotAction
    from workers import scheduler

    camp = _campaign(session, user, channel)
    session.add(AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=camp.id,
                                kind="wind_down", summary="w", evidence={},
                                params={"total_episodes": 3}))
    session.commit()
    calls = []

    def boom(**kw):
        calls.append(1)
        raise RuntimeError("503")

    monkeypatch.setattr(ai_engine, "generate_structured", boom)
    scheduler.autopilot_review_rules_channel(session, user, channel, "copilot")
    scheduler.autopilot_review_rules_channel(session, user, channel, "copilot")
    assert len(calls) == 1


def test_successor_waits_when_the_pass_ai_budget_is_spent(session, user, channel):
    from database.models import AutopilotAction
    from workers import scheduler

    camp = _campaign(session, user, channel)
    a = AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=camp.id,
                        kind="successor", summary="s",
                        evidence={"ai_review": {"verdict": "approve"}}, params={})
    session.add(a)
    session.commit()
    assert scheduler.autopilot_autoapply_channel(session, channel, allow_ai=False)["successor"] == 0
    session.refresh(a)
    assert a.status == "proposed"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_the_tick_loop_sleeps_only_the_rest_of_the_interval(monkeypatch):
    from workers import scheduler

    slept = []

    def fake_tick():
        fake_tick.t += 50.0

    fake_tick.t = 0.0
    monkeypatch.setattr(scheduler, "periodic_tick", fake_tick)
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: fake_tick.t)

    def stop(seconds):
        slept.append(seconds)
        raise SystemExit

    monkeypatch.setattr(scheduler.time, "sleep", stop)
    th = scheduler.run_scheduler_thread(interval=3600)
    th.join(timeout=5)
    assert slept == [pytest.approx(3550.0)]
