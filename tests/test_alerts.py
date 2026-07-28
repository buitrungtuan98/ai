"""Header alert bell: cross-channel incidents derived from live state (no stored events)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def _campaign(session, user, channel, topic="Alerts", **cfg):
    from database.models import Campaign
    from database.types import CampaignStatus

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name=topic, total_episodes=9,
                 status=cfg.pop("status", CampaignStatus.active), config_json=cfg)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _keys(rows):
    return [r["key"] for r in rows]


def test_all_clear_yields_an_empty_actionable_feed(client, session, user, monkeypatch):
    """A healthy factory must produce ZERO actionable alerts, or the bell becomes noise."""
    import main
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    rows = main._alerts(session, user)
    assert [r for r in rows if r["level"] in ("red", "amber")] == []

    body = client.get("/api/alerts").json()
    assert body["actionable"] == 0 and body["worst"] == ""


def test_a_dead_worker_is_a_red_alert_pointing_at_operations(client, session, user, monkeypatch):
    import main
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: False)
    rows = main._alerts(session, user)
    worker = [r for r in rows if r["key"] == "worker-down"]
    assert worker and worker[0]["level"] == "red"
    assert worker[0]["href"] == "/operations?tab=worker"
    assert client.get("/api/alerts").json()["worst"] == "red"


def test_a_wedged_worker_reads_differently_from_a_dead_one(session, user, monkeypatch):
    """"Registered but not progressing" is a distinct fault with a distinct explanation."""
    import time

    import main
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    task_queue.set_progress(9, 10.0)
    task_queue.conn.hset("task:progress-ts", "9",
                         f"{time.time() - task_queue.stall_limit_seconds() - 60:.0f}")

    keys = _keys(main._alerts(session, user))
    assert "worker-stalled" in keys and "worker-down" not in keys


def test_a_failed_episode_names_its_channel_and_campaign_and_the_last_error_line(
        session, user, channel, monkeypatch):
    """The operator's format: [Channel] > [Campaign] > what went wrong > an action. An unrecognised
    error falls back to the traceback's LAST line — the actual error, not the "Traceback" header."""
    import main
    from database.models import Task
    from database.types import TaskStatus
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    camp = _campaign(session, user, channel, topic="Fact Ơi Là Fact")
    t = Task(campaign_id=camp.id, user_id=user.id, episode_number=7, status=TaskStatus.FAILED,
             finished_at=datetime.utcnow(),
             error_message="Traceback (most recent call last):\n  File x\nWeirdError: unknowable")
    session.add(t)
    session.commit()
    session.refresh(t)

    row = [r for r in main._alerts(session, user) if r["key"] == f"task-failed:{t.id}"][0]
    assert row["level"] == "red"
    assert row["channel"] == "Test Ch" and row["campaign"] == "Fact Ơi Là Fact"
    assert "Ep 7 failed" in row["text"] and "WeirdError: unknowable" in row["text"]
    assert "Traceback" not in row["text"]
    assert row["href"] == f"/episodes/{t.id}" and row["action"] == "Open"
    assert row["at"] is not None


def test_a_recognised_failure_reports_its_cause_not_the_stack_line(session, user, channel, monkeypatch):
    """A stack line is unreadable AND misleading about the fix: "429 ResourceExhausted" tells the
    operator nothing about retrying later. The bell says what the episode page says (ADR-068)."""
    import main
    from database.models import Task
    from database.types import TaskStatus
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    camp = _campaign(session, user, channel)
    t = Task(campaign_id=camp.id, user_id=user.id, episode_number=7, status=TaskStatus.FAILED,
             finished_at=datetime.utcnow(),
             error_message="google.api_core.exceptions.ResourceExhausted: 429 quota exceeded")
    session.add(t)
    session.commit()
    session.refresh(t)

    row = [r for r in main._alerts(session, user) if r["key"] == f"task-failed:{t.id}"][0]
    assert row["text"] == "Ep 7 failed — A free-tier quota ran out"


def test_a_failure_with_no_recorded_message_says_so(session, user, channel, monkeypatch):
    """Falling back to the word "failed" rendered as "Ep 7 failed — failed"."""
    import main
    from database.models import Task
    from database.types import TaskStatus
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    camp = _campaign(session, user, channel)
    t = Task(campaign_id=camp.id, user_id=user.id, episode_number=7, status=TaskStatus.FAILED,
             finished_at=datetime.utcnow(), error_message=None)
    session.add(t)
    session.commit()
    session.refresh(t)

    row = [r for r in main._alerts(session, user) if r["key"] == f"task-failed:{t.id}"][0]
    assert row["text"] == "Ep 7 failed — no error recorded"


def test_failed_episodes_are_capped_so_the_bell_stays_readable(session, user, channel, monkeypatch):
    import main
    from database.models import Task
    from database.types import TaskStatus
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    camp = _campaign(session, user, channel)
    for ep in range(1, 16):
        session.add(Task(campaign_id=camp.id, user_id=user.id, episode_number=ep,
                         status=TaskStatus.FAILED, finished_at=datetime.utcnow(),
                         error_message="boom"))
    session.commit()

    failed = [r for r in main._alerts(session, user) if r["key"].startswith("task-failed:")]
    assert len(failed) == main._ALERT_FAILED_LIMIT


def test_a_breaker_paused_campaign_is_its_own_alert(session, user, channel, monkeypatch):
    import main
    from database.types import CampaignStatus
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    camp = _campaign(session, user, channel, topic="Stopped", status=CampaignStatus.failed)

    row = [r for r in main._alerts(session, user) if r["key"] == f"campaign-paused:{camp.id}"][0]
    assert row["level"] == "red" and row["campaign"] == "Stopped"
    assert row["href"] == f"/campaigns/{camp.id}"


def test_review_and_autopilot_backlogs_are_amber_aggregates(session, user, channel, monkeypatch):
    """Per-item rows would flood the bell — a backlog is one line with a count."""
    import main
    from database.models import AutopilotAction, BufferPoolItem
    from database.types import BufferStatus
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    camp = _campaign(session, user, channel)
    for ep in (1, 2, 3):
        session.add(BufferPoolItem(campaign_id=camp.id, channel_id=channel.id, episode_number=ep,
                                   video_path=f"/v{ep}.mp4", status=BufferStatus.awaiting_review,
                                   metadata_json={}))
    session.add(AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=camp.id,
                                kind="extend", summary="s", evidence={}, params={}))
    session.commit()

    rows = {r["key"]: r for r in main._alerts(session, user)}
    assert rows["review"]["level"] == "amber" and "3 episodes" in rows["review"]["text"]
    assert rows["autopilot"]["level"] == "amber" and "1 autopilot proposal" in rows["autopilot"]["text"]


def test_an_imminent_slot_with_an_empty_buffer_warns_before_it_is_missed(
        session, user, channel, monkeypatch):
    """A missed slot cannot be recovered afterwards, so the warning has to come early."""
    import main
    from workers import scheduler, task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    # Freeze "now" just before a slot so the risk window is deterministic.
    monkeypatch.setattr(scheduler, "local_now",
                        lambda tz=None: datetime(2026, 7, 28, 20, 0))
    camp = _campaign(session, user, channel, topic="Empty Buffer", posting_slots=["21:00"])

    row = [r for r in main._alerts(session, user) if r["key"] == f"slot-risk:{camp.id}"][0]
    assert row["level"] == "amber" and "will be missed" in row["text"]
    assert row["campaign"] == "Empty Buffer" and row["href"] == "/operations?tab=queue"


def test_a_ready_episode_silences_the_slot_warning(session, user, channel, monkeypatch):
    import main
    from database.models import BufferPoolItem
    from database.types import BufferStatus
    from workers import scheduler, task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    monkeypatch.setattr(scheduler, "local_now", lambda tz=None: datetime(2026, 7, 28, 20, 0))
    camp = _campaign(session, user, channel, posting_slots=["21:00"])
    session.add(BufferPoolItem(campaign_id=camp.id, channel_id=channel.id, episode_number=1,
                               video_path="/v.mp4", status=BufferStatus.ready, metadata_json={}))
    session.commit()

    assert not [r for r in main._alerts(session, user) if r["key"].startswith("slot-risk:")]


def test_recent_publishes_appear_as_green_and_never_count_as_actionable(
        client, session, user, channel, monkeypatch):
    import main
    from database.models import Task
    from database.types import TaskStatus
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    camp = _campaign(session, user, channel)
    session.add(Task(campaign_id=camp.id, user_id=user.id, episode_number=1,
                     status=TaskStatus.COMPLETED, finished_at=datetime.utcnow(),
                     published_url="https://youtu.be/abc"))
    # Older than the 24h window — must not show.
    session.add(Task(campaign_id=camp.id, user_id=user.id, episode_number=2,
                     status=TaskStatus.COMPLETED,
                     finished_at=datetime.utcnow() - timedelta(days=3)))
    session.commit()

    rows = main._alerts(session, user)
    green = [r for r in rows if r["level"] == "green"]
    assert len(green) == 1 and "Ep 1 published" in green[0]["text"]
    assert green[0]["href"] == "https://youtu.be/abc"
    assert client.get("/api/alerts").json()["actionable"] == 0


def test_the_feed_is_ordered_red_then_amber_then_green(session, user, channel, monkeypatch):
    import main
    from database.models import BufferPoolItem, Task
    from database.types import BufferStatus, TaskStatus
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: False)   # red
    camp = _campaign(session, user, channel)
    session.add(Task(campaign_id=camp.id, user_id=user.id, episode_number=1,
                     status=TaskStatus.COMPLETED, finished_at=datetime.utcnow()))   # green
    session.add(BufferPoolItem(campaign_id=camp.id, channel_id=channel.id, episode_number=2,
                               video_path="/v.mp4", status=BufferStatus.awaiting_review,
                               metadata_json={}))                                   # amber
    session.commit()

    levels = [r["level"] for r in main._alerts(session, user)]
    assert levels == sorted(levels, key=lambda level: main._ALERT_ORDER[level])
    assert levels[0] == "red" and levels[-1] == "green"


def test_one_broken_source_never_empties_the_whole_bell(session, user, monkeypatch):
    """An empty bell reads as "everything is fine" — the worst possible failure mode."""
    import main
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: False)
    monkeypatch.setattr(main, "_work_alerts", lambda db, u: (_ for _ in ()).throw(RuntimeError("boom")))

    keys = _keys(main._alerts(session, user))
    assert "worker-down" in keys          # the infra source still reported


def test_alerts_are_tenant_scoped(client, session, user, channel, monkeypatch):
    import main
    from database.models import Channel, Task, User
    from database.types import Platform, TaskStatus
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    other = User(firebase_uid="someone-else", gemini_api_key="k")
    session.add(other)
    session.commit()
    session.refresh(other)
    och = Channel(user_id=other.id, platform=Platform.youtube, channel_name="Theirs",
                  encrypted_credentials="{}")
    session.add(och)
    session.commit()
    session.refresh(och)
    ocamp = _campaign(session, other, och, topic="Their Secret")
    session.add(Task(campaign_id=ocamp.id, user_id=other.id, episode_number=1,
                     status=TaskStatus.FAILED, finished_at=datetime.utcnow(), error_message="theirs"))
    session.commit()

    rows = main._alerts(session, user)
    assert not any("Their Secret" == r["campaign"] for r in rows)
    assert "Their Secret" not in client.get("/api/alerts").text


def test_the_bell_is_in_the_app_bar_on_every_page(client):
    body = client.get("/").text
    assert 'id="bell"' in body and 'id="bell-panel"' in body
    assert 'id="bell-count"' in body
