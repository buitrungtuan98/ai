"""Operations page: queue introspection + order, tenancy, prioritise/cancel, recovery, restart."""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def _campaign(session, user, channel, topic="Ops"):
    from database.models import Campaign
    from database.types import CampaignStatus

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name=topic, total_episodes=9,
                 status=CampaignStatus.active, config_json={})
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _queued(session, campaign, user, episode):
    """A PENDING_QUEUE task with a real RQ job behind it, like hydration produces."""
    from database.models import Task
    from workers import task_queue

    t = Task(campaign_id=campaign.id, user_id=user.id, episode_number=episode)
    session.add(t)
    session.commit()
    session.refresh(t)
    t.rq_job_id = task_queue.enqueue_render(t.id)
    session.commit()
    return t


# ── Queue introspection ──────────────────────────────────────────────────────
def test_queued_jobs_reports_true_queue_order_and_both_kinds(session, user, channel):
    from workers import task_queue

    campaign = _campaign(session, user, channel)
    t1 = _queued(session, campaign, user, 1)
    t2 = _queued(session, campaign, user, 2)
    task_queue.enqueue_publish(77)

    jobs = task_queue.queued_jobs()
    assert [j["position"] for j in jobs] == [1, 2, 3]
    assert [j["kind"] for j in jobs] == ["render", "render", "publish"]
    assert [j["arg"] for j in jobs] == [t1.id, t2.id, 77]
    assert all(j["enqueued_at"] is not None for j in jobs)


def test_move_to_front_reorders_without_replacing_the_job(session, user, channel):
    """The Job id must survive, or Task.rq_job_id would point at a job that no longer exists."""
    from workers import task_queue

    campaign = _campaign(session, user, channel)
    t1 = _queued(session, campaign, user, 1)
    t2 = _queued(session, campaign, user, 2)

    assert task_queue.move_job_to_front(t2.rq_job_id) is True
    assert [j["arg"] for j in task_queue.queued_jobs()] == [t2.id, t1.id]
    session.refresh(t2)
    assert t2.rq_job_id in task_queue.render_queue.get_job_ids()

    # A job that already left the queue cannot be reordered.
    task_queue.cancel_job(t2.rq_job_id)
    assert task_queue.move_job_to_front(t2.rq_job_id) is False


def test_cancel_job_removes_it_from_the_queue(session, user, channel):
    from workers import task_queue

    campaign = _campaign(session, user, channel)
    t1 = _queued(session, campaign, user, 1)
    assert task_queue.cancel_job(t1.rq_job_id) is True
    assert task_queue.queued_jobs() == []
    assert task_queue.cancel_job(t1.rq_job_id) is False  # idempotent


def test_queue_helpers_fail_soft_without_redis(monkeypatch):
    """The Operations page must render an explained empty state, never a 500, when Redis is gone."""
    from workers import task_queue

    class Dead:
        def __getattr__(self, _name):
            def boom(*a, **k):
                raise RuntimeError("no redis")
            return boom

    monkeypatch.setattr(task_queue, "render_queue", Dead())
    monkeypatch.setattr(task_queue, "conn", Dead())
    assert task_queue.queued_jobs() == []
    assert task_queue.move_job_to_front("x") is False
    assert task_queue.cancel_job("x") is False
    assert task_queue.worker_snapshot() is None
    assert task_queue.render_lock_held() is False
    assert task_queue.stalled_render() is None
    assert task_queue.progress_age_seconds(1) is None


# ── Page rendering ───────────────────────────────────────────────────────────
def test_operations_page_lists_the_queue_in_order(client, session, user, channel):
    campaign = _campaign(session, user, channel, topic="Queue Order")
    _queued(session, campaign, user, 1)
    _queued(session, campaign, user, 2)

    body = client.get("/operations").text
    assert "Operations" in body and "Render queue" in body
    assert "Queue Order" in body
    assert body.index("Ep 1") < body.index("Ep 2")   # queue order, not insertion order


def test_operations_page_empty_state_and_unknown_tab_falls_back(client):
    body = client.get("/operations?tab=nonsense").text
    assert "Nothing queued" in body          # unknown tab → the default queue tab
    assert "Render queue" in body


def test_worker_tab_reports_down_when_no_worker_is_registered(client):
    body = client.get("/operations?tab=worker").text
    assert "Down — not registered" in body
    assert "Recover stuck renders" in body and "Restart worker" in body


def test_worker_tab_shows_the_live_render_and_its_progress(client, session, user, channel, monkeypatch):
    from database.models import Task
    from database.types import TaskStatus
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    campaign = _campaign(session, user, channel, topic="Live Render")
    t = Task(campaign_id=campaign.id, user_id=user.id, episode_number=4,
             status=TaskStatus.RENDERING)
    session.add(t)
    session.commit()
    session.refresh(t)
    task_queue.set_progress(t.id, 42.0)

    body = client.get("/operations?tab=worker").text
    assert "Busy — rendering" in body
    assert "Ep 4" in body and "Live Render" in body and "42.0" in body


def test_worker_tab_flags_a_wedged_render(client, session, user, channel, monkeypatch):
    from database.models import Task
    from database.types import TaskStatus
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    campaign = _campaign(session, user, channel)
    t = Task(campaign_id=campaign.id, user_id=user.id, episode_number=5,
             status=TaskStatus.RENDERING)
    session.add(t)
    session.commit()
    session.refresh(t)
    task_queue.set_progress(t.id, 10.0)
    task_queue.conn.hset("task:progress-ts", str(t.id),
                         f"{time.time() - task_queue.stall_limit_seconds() - 60:.0f}")

    body = client.get("/operations?tab=worker").text
    assert "Stalled — wedged" in body
    assert "no progress for" in body


# ── Actions ──────────────────────────────────────────────────────────────────
def test_prioritise_moves_the_render_to_the_front(client, session, user, channel):
    from workers import task_queue

    campaign = _campaign(session, user, channel)
    t1 = _queued(session, campaign, user, 1)
    t2 = _queued(session, campaign, user, 2)

    r = client.post(f"/operations/jobs/{t2.rq_job_id}/front", follow_redirects=False)
    assert r.status_code == 303 and "flash=prioritised" in r.headers["location"]
    assert [j["arg"] for j in task_queue.queued_jobs()] == [t2.id, t1.id]


def test_cancel_marks_the_task_failed_so_retry_still_works(client, session, user, channel):
    from database.types import TaskStatus
    from workers import task_queue

    campaign = _campaign(session, user, channel)
    t = _queued(session, campaign, user, 1)

    r = client.post(f"/operations/jobs/{t.rq_job_id}/cancel", follow_redirects=False)
    assert r.status_code == 303 and "flash=cancelled" in r.headers["location"]
    assert task_queue.queued_jobs() == []
    session.refresh(t)
    assert t.status == TaskStatus.FAILED          # visible + retryable, not deleted
    assert "Cancelled" in t.error_message
    assert t.finished_at is not None


def test_actions_404_on_a_job_that_is_no_longer_queued(client):
    assert client.post("/operations/jobs/does-not-exist/front").status_code == 404
    assert client.post("/operations/jobs/does-not-exist/cancel").status_code == 404


def test_a_publish_job_cannot_be_reordered_as_a_render(client):
    from workers import task_queue

    job_id = task_queue.enqueue_publish(5)
    assert client.post(f"/operations/jobs/{job_id}/front").status_code == 400


def test_another_tenants_job_is_neither_listed_nor_controllable(client, session, user, channel):
    """Tenancy is enforced through the Task row — a job id must not be a way around it."""
    from database.models import Channel, Task, User
    from database.types import Platform, TaskStatus
    from workers import task_queue

    other = User(firebase_uid="intruder", gemini_api_key="k")
    session.add(other)
    session.commit()
    session.refresh(other)
    och = Channel(user_id=other.id, platform=Platform.youtube, channel_name="Theirs",
                  encrypted_credentials="{}")
    session.add(och)
    session.commit()
    session.refresh(och)
    ocamp = _campaign(session, other, och, topic="Secret Campaign")
    ot = Task(campaign_id=ocamp.id, user_id=other.id, episode_number=1,
              status=TaskStatus.PENDING_QUEUE)
    session.add(ot)
    session.commit()
    session.refresh(ot)
    ot.rq_job_id = task_queue.enqueue_render(ot.id)
    session.commit()

    body = client.get("/operations").text
    assert "Secret Campaign" not in body
    assert client.post(f"/operations/jobs/{ot.rq_job_id}/cancel").status_code == 404
    assert task_queue.queued_jobs()  # still queued — the intruder's cancel did nothing


def test_recover_frees_an_orphaned_lock_and_fails_a_dead_task(client, session, user, channel):
    from datetime import datetime, timedelta

    from core.config import settings
    from database.models import Task
    from database.types import TaskStatus
    from workers import task_queue

    campaign = _campaign(session, user, channel)
    dead = Task(campaign_id=campaign.id, user_id=user.id, episode_number=1,
                status=TaskStatus.RENDERING)
    session.add(dead)
    session.commit()
    session.refresh(dead)
    # Untouched for far longer than any real render — definitively dead.
    old = datetime.utcnow() - timedelta(seconds=settings.JOB_TIMEOUT_SECONDS * 3)
    session.query(Task).filter_by(id=dead.id).update({"updated_at": old})
    session.commit()
    task_queue.conn.set(task_queue.LOCK_KEY, "1")

    r = client.post("/operations/recover", follow_redirects=False)
    assert r.status_code == 303 and "flash=recovered" in r.headers["location"]
    session.refresh(dead)
    assert dead.status == TaskStatus.FAILED
    assert task_queue.conn.get(task_queue.LOCK_KEY) is None


def test_recover_never_touches_a_live_render(client, session, user, channel):
    """The render-concurrency-1 guard: a progressing render keeps its lock, whoever clicks."""
    from database.models import Task
    from database.types import TaskStatus
    from workers import task_queue

    campaign = _campaign(session, user, channel)
    live = Task(campaign_id=campaign.id, user_id=user.id, episode_number=1,
                status=TaskStatus.RENDERING)
    session.add(live)
    session.commit()
    session.refresh(live)
    task_queue.set_progress(live.id, 30.0)
    task_queue.conn.set(task_queue.LOCK_KEY, "1")

    r = client.post("/operations/recover", follow_redirects=False)
    assert "flash=nothing" in r.headers["location"]
    session.refresh(live)
    assert live.status == TaskStatus.RENDERING
    assert task_queue.conn.get(task_queue.LOCK_KEY) is not None


def test_restart_worker_only_raises_a_redis_flag(client):
    """No Docker socket: the web container must never be able to command the daemon."""
    from workers import task_queue

    r = client.post("/operations/restart-worker", follow_redirects=False)
    assert r.status_code == 303 and "flash=restarting" in r.headers["location"]
    assert task_queue.restart_requested() is True
    assert task_queue.conn.ttl(task_queue.RESTART_KEY) > 0   # self-expiring, never a stale kill


def test_restart_pending_is_shown_back_to_the_operator(client):
    from workers import task_queue

    task_queue.request_worker_restart()
    assert "Restart already requested" in client.get("/operations?tab=worker").text


def test_health_reports_a_wedged_worker_separately(client, session, user, monkeypatch):
    import main
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)
    task_queue.set_progress(31, 10.0)
    task_queue.conn.hset("task:progress-ts", "31",
                         f"{time.time() - task_queue.stall_limit_seconds() - 60:.0f}")

    health = main._system_health(session, user)
    assert health["worker"] is True and health["worker_stalled"] is True
    assert client.get("/api/summary").json()["health"]["worker_stalled"] is True
