"""R20 Batch X (ADR-085) — the bug sweep from the full-code audit.

One honest population for every statistic (compilations out of the learning loop), a Facebook
retry that verifies bytes actually landed before adopting an upload, an autopilot retry that
re-publishes instead of re-rendering when the video already exists, kind-aware reject re-renders,
quota failures that self-heal after the Pacific reset, and ONE budget-reserve definition.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


def _mk_campaign(session, user, channel, **cfg):
    from database.models import Campaign
    from database.types import CampaignStatus

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=20,
                   status=CampaignStatus.active, config_json={"language": "en", **cfg})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    return cam


def _done_task(session, cam, ep, *, stats=None, kind="episode", finished=None):
    from database.models import Task
    from database.types import TaskStatus

    t = Task(campaign_id=cam.id, user_id=cam.user_id, episode_number=ep, video_kind=kind,
             status=TaskStatus.COMPLETED, stats_json=stats,
             published_video_id=f"vid-{ep}",
             finished_at=finished or datetime.utcnow() - timedelta(days=1))
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


# ── X1: compilations stay out of every learning statistic ────────────────────
def test_flop_median_and_verdicts_ignore_compilations(session, user, channel):
    from core import flop
    from services.analytics_service import judge_flops

    cam = _mk_campaign(session, user, channel)
    for ep in range(1, 6):
        _done_task(session, cam, ep, stats={"views_24h": 100})
    # A long-form compilation with tiny first-day views — sentinel numbering, different format.
    comp = _done_task(session, cam, 9001, kind="compilation", stats={"views_24h": 1})

    from database.models import Task

    tasks = session.query(Task).filter_by(campaign_id=cam.id).all()
    assert flop.campaign_median_24h(tasks) == 100          # the 1-view outlier never entered
    judge_flops(session, cam)
    session.refresh(comp)
    assert comp.stats_json.get("flop") is None             # a compilation is never judged
    learning = (session.get(type(cam), cam.id).learning_json or {})
    assert not any("9001" in n for n in learning.get("flop_notes") or [])


def test_consecutive_flop_streak_ignores_the_sentinel_head(session, user, channel):
    """Sentinel numbers sort NEWEST: one compilation judged 'fine' at the head of the streak used
    to mask the flop breaker forever."""
    from core.autopilot import propose_actions

    cam = _mk_campaign(session, user, channel)
    cam.current_episode = 8
    session.commit()
    for ep in range(1, 6):
        _done_task(session, cam, ep, stats={"views_24h": 5, "flop": True})
    _done_task(session, cam, 9001, kind="compilation", stats={"views_24h": 500, "flop": False})

    from database.models import Task

    tasks = session.query(Task).filter_by(campaign_id=cam.id).all()
    verdict = {"label": "unknown", "baseline": None, "retention": None}
    kinds = [p["kind"] for p in propose_actions(cam, tasks, verdict)]
    assert "wind_down" in kinds                            # the breaker still fires


def test_classification_baseline_ignores_compilation_retention(session, user, channel):
    from core.autopilot import channel_baseline

    cam = _mk_campaign(session, user, channel)
    for ep in range(1, 4):
        _done_task(session, cam, ep, stats={"avg_pct_viewed": 80.0})
    _done_task(session, cam, 9001, kind="compilation", stats={"avg_pct_viewed": 10.0})
    assert channel_baseline(session, channel.id) == 80.0


def test_council_pack_excludes_compilations(session, user, channel):
    from core.council import evidence_pack

    cam = _mk_campaign(session, user, channel)
    for ep in range(1, 6):
        _done_task(session, cam, ep, stats={"views_24h": 100, "flop": False})
    _done_task(session, cam, 9001, kind="compilation", stats={"views_24h": 1, "flop": True})
    pack = evidence_pack(session, channel)
    flops = pack["campaigns"][0]["flops"]
    assert flops["total"] == 0 and flops["consecutive_latest"] == 0
    assert flops["measured_24h"] == 5                      # the compilation is not "measured"


def test_slot_guard_and_catchup_ignore_a_published_compilation(session, user, channel):
    from workers import scheduler

    cam = _mk_campaign(session, user, channel, posting_slots=["09:00"])
    _done_task(session, cam, 9001, kind="compilation", finished=datetime.utcnow())
    assert scheduler._recently_published(session, cam.id, 30) is False
    assert scheduler._published_today(session, cam.id, scheduler.local_now(), None) == 0


# ── X2: Facebook adopts an upload only when the bytes actually landed ─────────
class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _fb_channel(session, user):
    import json

    from database.models import Channel
    from database.types import Platform

    c = Channel(user_id=user.id, platform=Platform.facebook, channel_name="Page",
                encrypted_credentials=json.dumps(
                    {"page_id": "p1", "page_access_token": "tok"}))
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def test_reserved_id_with_no_bytes_is_not_adopted(session, user, monkeypatch):
    """`start` reserves the id BEFORE any byte goes up. A retry after a dead transfer used to see
    the id exists and mark the episode published — pointing at an empty draft."""
    import requests

    from services.facebook_service import find_existing_upload

    ch = _fb_channel(session, user)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, {
        "id": "555", "status": {"video_status": "processing",
                                "uploading_phase": {"status": "not_started"}}}))
    assert find_existing_upload(ch, video_id="555") is None


def test_completed_upload_is_adopted(session, user, monkeypatch):
    import requests

    from services.facebook_service import find_existing_upload

    ch = _fb_channel(session, user)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, {
        "id": "555", "status": {"video_status": "processing",
                                "uploading_phase": {"status": "complete"}}}))
    assert find_existing_upload(ch, video_id="555") == "555"
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, {
        "id": "556", "status": {"video_status": "ready", "uploading_phase": {}}}))
    assert find_existing_upload(ch, video_id="556") == "556"


# ── X3: the autopilot retries the step that failed, not the whole render ─────
def test_autopilot_retries_the_upload_when_the_video_already_exists(session, user, channel,
                                                                    monkeypatch, tmp_path):
    from database.models import BufferPoolItem, Task
    from database.types import BufferStatus, TaskStatus
    from workers import scheduler, task_queue

    cam = _mk_campaign(session, user, channel)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.FAILED,
             finished_at=datetime.utcnow(),
             error_message="Facebook upload failed: connection timed out")
    session.add(t)
    session.add(BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                               video_path=str(video), status=BufferStatus.awaiting_review,
                               metadata_json={"title": "T"}))
    session.commit()

    publishes, renders = [], []
    monkeypatch.setattr(task_queue, "enqueue_publish", lambda bid: publishes.append(bid))
    from workers import video_worker

    monkeypatch.setattr(video_worker, "enqueue_task", lambda t: renders.append(t.id) or "job")
    assert scheduler.autopilot_retry_channel(session, channel) == 1
    assert publishes and not renders                      # publish only — no wasted re-render
    assert video.exists()                                  # the good file was never touched


def test_autopilot_still_rerenders_when_no_file_survives(session, user, channel, monkeypatch):
    from database.models import Task
    from database.types import TaskStatus
    from workers import scheduler, task_queue, video_worker

    cam = _mk_campaign(session, user, channel)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.FAILED,
             finished_at=datetime.utcnow(), error_message="connection timed out mid-render")
    session.add(t)
    session.commit()
    publishes, renders = [], []
    monkeypatch.setattr(task_queue, "enqueue_publish", lambda bid: publishes.append(bid))
    monkeypatch.setattr(video_worker, "enqueue_task", lambda t: renders.append(t.id) or "job")
    assert scheduler.autopilot_retry_channel(session, channel) == 1
    assert renders and not publishes


# ── X4: a rejected compilation re-queues as a CONCAT, never as a script ──────
def test_reject_rerender_routes_by_kind(session, user, channel, monkeypatch):
    from database.models import BufferPoolItem, Task
    from database.types import BufferStatus, TaskStatus
    from workers import video_worker

    cam = _mk_campaign(session, user, channel)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=9001,
             video_kind="compilation", status=TaskStatus.AWAITING_REVIEW)
    session.add(t)
    buf = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=9001,
                         video_path="/no/v.mp4", status=BufferStatus.awaiting_review,
                         metadata_json={})
    session.add(buf)
    session.commit()
    session.refresh(buf)
    compiles, renders = [], []
    from workers import task_queue

    monkeypatch.setattr(task_queue, "enqueue_compile", lambda tid: compiles.append(tid) or "c")
    monkeypatch.setattr(video_worker, "enqueue_render", lambda tid: renders.append(tid) or "r")
    video_worker.apply_reject(session, buf, "wrong order", rerender=True, automatic=True)
    assert compiles == [t.id] and not renders


# ── X5: a spent quota becomes retryable after the Pacific midnight reset ──────
def test_quota_failures_self_heal_after_the_reset():
    from core import failure

    msg = "429 RESOURCE_EXHAUSTED: daily quota exceeded"
    failed_at = datetime(2026, 8, 3, 20, 0)               # 13:00 Pacific (UTC-7, August)
    same_day = datetime(2026, 8, 4, 5, 0)                 # still Aug 3 in Pacific (22:00)
    next_day = datetime(2026, 8, 4, 9, 0)                 # Aug 4 in Pacific (02:00)
    assert failure.is_transient(msg) is False              # unchanged: not retryable NOW
    assert failure.quota_reset_since(msg, failed_at, now=same_day) is False
    assert failure.quota_reset_since(msg, failed_at, now=next_day) is True
    # Only the quota class heals by waiting — a dead key never does.
    assert failure.quota_reset_since("invalid key", failed_at, now=next_day) is False


def test_autopilot_retries_a_quota_failure_only_after_the_reset(session, user, channel,
                                                                monkeypatch):
    from database.models import Task
    from database.types import TaskStatus
    from workers import scheduler, video_worker

    cam = _mk_campaign(session, user, channel)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.FAILED,
             finished_at=datetime.utcnow(),                # failed just now → same quota day
             error_message="429 RESOURCE_EXHAUSTED: quota exceeded")
    session.add(t)
    session.commit()
    renders: list = []
    monkeypatch.setattr(video_worker, "enqueue_task", lambda t: renders.append(t.id) or "job")
    assert scheduler.autopilot_retry_channel(session, channel) == 0   # not yet — quota still spent
    t.finished_at = datetime.utcnow() - timedelta(days=2)             # the reset has long passed
    session.commit()
    assert scheduler.autopilot_retry_channel(session, channel) == 1
    assert renders == [t.id]


# ── X6: one budget-reserve definition, with the app-wide fallback ─────────────
def test_reserve_reached_honours_the_global_fallback(monkeypatch):
    from core import usage
    from core.config import settings

    monkeypatch.setattr(settings, "GEMINI_DAILY_BUDGET", 10)
    monkeypatch.setattr(usage, "ai_calls_today", lambda: 8)
    assert usage.reserve_reached(None) is True             # 8 ≥ 80% of the env budget
    monkeypatch.setattr(usage, "ai_calls_today", lambda: 7)
    assert usage.reserve_reached(None) is False


def test_reserve_prefers_the_users_own_budget(monkeypatch, session, user):
    from core import usage
    from core.config import settings

    monkeypatch.setattr(settings, "GEMINI_DAILY_BUDGET", 1000)
    user.settings_json = {"ai_daily_budget": 10}
    session.commit()
    monkeypatch.setattr(usage, "ai_calls_today", lambda: 8)
    assert usage.reserve_reached(user) is True


@pytest.fixture
def client_env(session, user, channel):
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c, session, user, channel
