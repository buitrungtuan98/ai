"""Worker watchdog: progress-staleness detection, stalled-task bookkeeping, restart requests."""
from __future__ import annotations

import time


def _task(session, user, channel, episode=1, status=None):
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus

    campaign = session.query(Campaign).filter_by(channel_id=channel.id).first()
    if campaign is None:
        campaign = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Watchdog",
                            total_episodes=5, status=CampaignStatus.active, config_json={})
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
    task = Task(campaign_id=campaign.id, user_id=user.id, episode_number=episode,
                status=status or TaskStatus.RENDERING)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


# ── Progress-staleness primitive ─────────────────────────────────────────────
def test_no_render_in_flight_is_never_stalled():
    from workers import task_queue

    assert task_queue.stalled_render() is None
    assert task_queue.worker_healthy() is False  # no worker registered either


def test_progress_stamp_only_moves_when_the_value_changes():
    """The stamp is the whole mechanism: re-reporting the SAME percentage must not look like work."""
    from workers import task_queue

    task_queue.set_progress(7, 10.0)
    first = task_queue.conn.hget("task:progress-ts", "7")
    task_queue.set_progress(7, 10.0)                     # same value → not progress
    assert task_queue.conn.hget("task:progress-ts", "7") == first

    task_queue.conn.hset("task:progress-ts", "7", "1")   # pretend the stamp is ancient
    task_queue.set_progress(7, 42.0)                     # a real move refreshes it
    assert task_queue.conn.hget("task:progress-ts", "7") != b"1"


def test_stall_is_detected_only_past_the_job_timeout():
    """The limit sits BEHIND RQ's own timeout so a slow-but-alive render is RQ's to kill, not ours."""
    from core.config import settings
    from workers import task_queue

    assert task_queue.stall_limit_seconds() > settings.JOB_TIMEOUT_SECONDS

    task_queue.set_progress(11, 10.0)
    # Just inside the limit: still considered a (slow) live render.
    task_queue.conn.hset("task:progress-ts", "11", f"{time.time() - task_queue.stall_limit_seconds() + 120:.0f}")
    assert task_queue.stalled_render() is None

    # Past the limit: wedged.
    task_queue.conn.hset("task:progress-ts", "11", f"{time.time() - task_queue.stall_limit_seconds() - 60:.0f}")
    stalled = task_queue.stalled_render()
    assert stalled is not None and stalled[0] == 11 and stalled[1] > task_queue.stall_limit_seconds()


def test_progress_entry_without_a_stamp_is_never_treated_as_stalled():
    """A render started by a previous build carries no stamp — it must not be killed mid-deploy."""
    from workers import task_queue

    task_queue.conn.hset("task:progress", "13", "55.0")  # value only, no companion stamp
    assert task_queue.stalled_render() is None


def test_clear_progress_drops_the_stamp_too():
    from workers import task_queue

    task_queue.set_progress(5, 30.0)
    task_queue.clear_progress(5)
    assert task_queue.conn.hget("task:progress-ts", "5") is None

    task_queue.set_progress(6, 30.0)
    task_queue.clear_all_progress()
    assert task_queue.conn.hgetall("task:progress") == {}
    assert task_queue.conn.hgetall("task:progress-ts") == {}


# ── Watchdog pass ────────────────────────────────────────────────────────────
def test_check_once_is_quiet_when_nothing_is_wrong(session):
    from workers import watchdog

    exits = []
    assert watchdog.check_once(session, exit_fn=exits.append) is None
    assert exits == []


def test_stalled_render_fails_the_task_releases_the_lock_and_exits(session, user, channel, monkeypatch):
    from database.types import TaskStatus
    from workers import task_queue, video_worker, watchdog

    task = _task(session, user, channel)
    task_queue.set_progress(task.id, 10.0)
    task_queue.conn.hset("task:progress-ts", str(task.id),
                         f"{time.time() - task_queue.stall_limit_seconds() - 300:.0f}")
    task_queue.conn.set(task_queue.LOCK_KEY, "1")
    alerts = []
    monkeypatch.setattr(video_worker, "_notify", lambda u, m: alerts.append(m))

    exits = []
    assert watchdog.check_once(session, exit_fn=exits.append) == "stalled"

    session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert "stalled" in (task.error_message or "").lower()
    assert task.finished_at is not None
    assert exits == [1]                                        # the process was asked to leave
    assert task_queue.conn.get(task_queue.LOCK_KEY) is None    # queue is not wedged behind the lock
    assert task_queue.get_progress(task.id) == 0.0             # no ghost % survives
    assert alerts and "restarted automatically" in alerts[0]


def test_stall_does_not_trip_the_campaign_circuit_breaker(session, user, channel, monkeypatch):
    """A wedged worker is infrastructure, not a broken campaign — it must not pause the campaign."""
    from database.models import Campaign
    from database.types import CampaignStatus
    from workers import task_queue, video_worker, watchdog

    task = _task(session, user, channel)
    campaign = session.get(Campaign, task.campaign_id)
    task_queue.set_progress(task.id, 10.0)
    task_queue.conn.hset("task:progress-ts", str(task.id),
                         f"{time.time() - task_queue.stall_limit_seconds() - 300:.0f}")
    monkeypatch.setattr(video_worker, "_notify", lambda u, m: None)

    watchdog.check_once(session, exit_fn=lambda code: None)

    session.refresh(campaign)
    assert campaign.status == CampaignStatus.active


def test_already_finished_task_is_left_alone(session, user, channel, monkeypatch):
    from database.types import TaskStatus
    from workers import task_queue, video_worker, watchdog

    task = _task(session, user, channel, status=TaskStatus.COMPLETED)
    task_queue.set_progress(task.id, 100.0)
    task_queue.conn.hset("task:progress-ts", str(task.id),
                         f"{time.time() - task_queue.stall_limit_seconds() - 300:.0f}")
    monkeypatch.setattr(video_worker, "_notify", lambda u, m: None)

    exits = []
    assert watchdog.check_once(session, exit_fn=exits.append) == "stalled"
    session.refresh(task)
    assert task.status == TaskStatus.COMPLETED   # not rewritten to FAILED
    assert exits == [1]                          # but the wedged process still leaves


def test_restart_request_exits_and_consumes_the_flag(session):
    from workers import task_queue, watchdog

    task_queue.request_worker_restart()
    assert task_queue.restart_requested() is True

    exits = []
    assert watchdog.check_once(session, exit_fn=exits.append) == "restart"
    assert exits == [0]                                   # a requested restart is a clean exit
    # Consumed, so the replacement worker does not immediately exit again.
    assert task_queue.restart_requested() is False


def test_restart_request_wins_over_a_stalled_render(session, user, channel, monkeypatch):
    """An explicit operator request should not also mark the episode failed."""
    from database.types import TaskStatus
    from workers import task_queue, video_worker, watchdog

    task = _task(session, user, channel)
    task_queue.set_progress(task.id, 10.0)
    task_queue.conn.hset("task:progress-ts", str(task.id),
                         f"{time.time() - task_queue.stall_limit_seconds() - 300:.0f}")
    task_queue.request_worker_restart()
    monkeypatch.setattr(video_worker, "_notify", lambda u, m: None)

    assert watchdog.check_once(session, exit_fn=lambda code: None) == "restart"
    session.refresh(task)
    assert task.status == TaskStatus.RENDERING


def test_worker_healthy_reports_a_wedged_render_as_unhealthy(monkeypatch):
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    assert task_queue.worker_healthy() is True

    task_queue.set_progress(21, 10.0)
    task_queue.conn.hset("task:progress-ts", "21",
                         f"{time.time() - task_queue.stall_limit_seconds() - 60:.0f}")
    assert task_queue.worker_healthy() is False
