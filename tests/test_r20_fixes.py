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


# ── Batch U: the autopilot heals its own no-verdict parks (ADR-086) ──────────
def _parked_unavailable(session, cam, channel, *, age_hours=3.0, qc=None):
    from database.models import BufferPoolItem
    from database.types import BufferStatus

    item = BufferPoolItem(
        campaign_id=cam.id, channel_id=channel.id, episode_number=1,
        video_path="/no/v.mp4", status=BufferStatus.awaiting_review,
        metadata_json={"qc": qc or {"passed": True, "score": None, "attempts": 1,
                                    "unavailable": True, "unavailable_reason": "rate limited"}})
    session.add(item)
    session.commit()
    session.refresh(item)
    item.created_at = datetime.utcnow() - timedelta(hours=age_hours)
    session.commit()
    return item


def test_autopilot_requalifies_a_no_verdict_park_once(session, user, channel, monkeypatch):
    from workers import scheduler, task_queue

    cam = _mk_campaign(session, user, channel)
    item = _parked_unavailable(session, cam, channel)
    queued: list = []
    monkeypatch.setattr(task_queue, "enqueue_requalify", lambda bid: queued.append(bid))
    assert scheduler.autopilot_requalify_channel(session, channel) == 1
    assert queued == [item.id]
    session.refresh(item)
    assert item.metadata_json.get("auto_requalify")        # the once-marker is set…
    assert scheduler.autopilot_requalify_channel(session, channel) == 0   # …and respected


def test_requalify_waits_out_the_outage_and_the_budget(session, user, channel, monkeypatch):
    from core import usage
    from workers import scheduler, task_queue

    cam = _mk_campaign(session, user, channel)
    _parked_unavailable(session, cam, channel, age_hours=0.5)   # too fresh — outage may persist
    queued: list = []
    monkeypatch.setattr(task_queue, "enqueue_requalify", lambda bid: queued.append(bid))
    assert scheduler.autopilot_requalify_channel(session, channel) == 0

    cam2 = _mk_campaign(session, user, channel)
    _parked_unavailable(session, cam2, channel)                 # old enough…
    monkeypatch.setattr(usage, "reserve_reached", lambda u=None: True)
    assert scheduler.autopilot_requalify_channel(session, channel) == 0   # …but budget says no
    assert not queued


def test_requalify_leaves_real_verdicts_alone(session, user, channel, monkeypatch):
    from workers import scheduler, task_queue

    cam = _mk_campaign(session, user, channel)
    _parked_unavailable(session, cam, channel,
                        qc={"passed": False, "score": 4, "attempts": 2})   # a REAL fail
    queued: list = []
    monkeypatch.setattr(task_queue, "enqueue_requalify", lambda bid: queued.append(bid))
    assert scheduler.autopilot_requalify_channel(session, channel) == 0
    assert not queued


# ── Batch V: the autopilot is visible in context (ADR-086) ───────────────────
def test_campaign_hub_shows_and_decides_proposals_in_place(client_env):
    client, session, user, channel = client_env
    from database.models import AutopilotAction

    cam = _mk_campaign(session, user, channel)
    a = AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=cam.id,
                        kind="extend", summary="Extend to 25 episodes — it's a winner.",
                        evidence={"confidence": 0.8}, params={"total_episodes": 25})
    session.add(a)
    session.add(AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=cam.id,
                                kind="retried", status="done", summary="Retried Ep 3.",
                                evidence={}, params={}))
    session.commit()
    session.refresh(a)
    page = client.get(f"/campaigns/{cam.id}").text
    assert "Autopilot on this campaign" in page
    assert "Extend to 25 episodes" in page and "confidence 80%" in page
    assert "Retried Ep 3." in page
    # Deciding from the hub keeps the operator on the hub.
    resp = client.post(f"/autopilot/{a.id}/dismiss",
                       data={"return_to": f"/campaigns/{cam.id}"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == f"/campaigns/{cam.id}"
    session.refresh(a)
    assert a.status == "dismissed"
    # An arbitrary return_to is never followed (open-redirect guard).
    resp = client.post(f"/autopilot/{a.id}/approve",
                       data={"return_to": "https://evil.example"}, follow_redirects=False)
    assert resp.headers["location"] == "/autopilot"


def test_calendar_marks_a_pending_slot_change(client_env):
    client, session, user, channel = client_env
    from database.models import AutopilotAction

    cam = _mk_campaign(session, user, channel, posting_slots=["09:00"])
    session.add(AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=cam.id,
                                kind="slot_change", summary="Move 09:00 to 21:00.",
                                evidence={}, params={"from": "09:00", "to": "21:00"}))
    session.commit()
    page = client.get("/calendar").text
    assert "proposed: 09:00 →" in page and "21:00" in page


# ── Batch W: the council knows operations, remembers dismissals, runs on demand ─
def test_council_pack_carries_operational_health(session, user, channel):
    from core.council import evidence_pack
    from database.models import BufferPoolItem, Task
    from database.types import BufferStatus, TaskStatus

    cam = _mk_campaign(session, user, channel, posting_slots=["09:00", "21:00"])
    session.add(Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                     status=TaskStatus.FAILED, finished_at=datetime.utcnow(),
                     error_message="boom"))
    for ep in (2, 3, 4):
        session.add(BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=ep,
                                   video_path="/no/v.mp4", status=BufferStatus.ready,
                                   metadata_json={}))
    session.add(BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=5,
                               video_path="/no/v.mp4", status=BufferStatus.awaiting_review,
                               metadata_json={"qc": {"passed": True, "score": None,
                                                     "unavailable": True}}))
    session.commit()
    ops = evidence_pack(session, channel)["campaigns"][0]["operations"]
    assert ops["failed_renders_7d"] == 1 and ops["consecutive_failures"] == 1
    assert ops["buffer_ready"] == 3 and ops["slots_per_day"] == 2
    assert ops["runway_days"] == 1.5
    assert ops["qc_no_verdict_recent"] == 1


def test_council_respects_a_recent_dismissal(session, user, channel):
    from core.council import _already_proposed, evidence_pack
    from database.models import AutopilotAction

    cam = _mk_campaign(session, user, channel)
    session.add(AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=cam.id,
                                kind="extend", status="dismissed",
                                resolved_at=datetime.utcnow() - timedelta(days=2),
                                summary="no", evidence={}, params={}))
    session.commit()
    assert _already_proposed(session, cam.id, "extend") is True     # inside the cooldown
    assert "extend" in evidence_pack(session, channel)["campaigns"][0][
        "operator_recently_dismissed"]                              # and the model is told why
    # An OLD dismissal ages out — the idea may return with fresh evidence.
    session.query(AutopilotAction).update(
        {"resolved_at": datetime.utcnow() - timedelta(days=45)})
    session.commit()
    assert _already_proposed(session, cam.id, "extend") is False


def test_run_council_now_files_and_reports(client_env, monkeypatch):
    client, session, user, channel = client_env
    from core import council

    _mk_campaign(session, user, channel)
    channel.autopilot_json = {"mode": "copilot"}   # the status strip (and its button) needs it on
    session.commit()
    calls: list = []
    monkeypatch.setattr(council, "run_council",
                        lambda db, ch, *, api_key, model: calls.append(ch.id) or
                        {"filed": 2, "refused": 0, "held": 0, "skipped_unchanged": False})
    resp = client.post(f"/channels/{channel.id}/council-now", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/autopilot?flash=council_filed"
    assert calls == [channel.id]
    page = client.get("/autopilot?flash=council_filed").text
    assert "The council ran" in page and "Run council now" in page


def test_run_council_now_is_budget_guarded_and_honest_on_cache(client_env, monkeypatch):
    client, session, user, channel = client_env
    from core import council, usage

    _mk_campaign(session, user, channel)
    monkeypatch.setattr(council, "run_council",
                        lambda db, ch, *, api_key, model:
                        {"filed": 0, "refused": 0, "held": 0, "skipped_unchanged": True})
    resp = client.post(f"/channels/{channel.id}/council-now", follow_redirects=False)
    assert resp.headers["location"] == "/autopilot?flash=council_unchanged"
    monkeypatch.setattr(usage, "reserve_reached", lambda u=None: True)
    resp = client.post(f"/channels/{channel.id}/council-now", follow_redirects=False)
    assert resp.headers["location"] == "/autopilot?flash=council_budget"


# ── Batch Y: robustness — expiry knows the schedule, campaigns finish honestly ─
def _ready_item(session, cam, channel, ep, *, age_hours, publish_at=None):
    from database.models import BufferPoolItem
    from database.types import BufferStatus

    item = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=ep,
                          video_path="/no/v.mp4", status=BufferStatus.ready,
                          publish_at=publish_at, metadata_json={})
    session.add(item)
    session.commit()
    session.refresh(item)
    item.created_at = datetime.utcnow() - timedelta(hours=age_hours)
    session.commit()
    return item


def test_expiry_respects_the_slot_runway(session, user, channel):
    """Five ready items on a one-slot-per-day campaign, all 80h old: the two at the head really
    are stale (they missed 3+ days of slots — the original 72h semantics), but the TAIL is simply
    waiting its turn. The flat cutoff destroyed the tail too, and the autopilot then re-rendered
    the same episodes into the same fate."""
    from database.types import BufferStatus
    from workers.scheduler import expire_stale_buffers

    cam = _mk_campaign(session, user, channel, posting_slots=["09:00"])
    items = [_ready_item(session, cam, channel, ep, age_hours=80) for ep in range(1, 6)]
    assert expire_stale_buffers(session) == 2              # only the genuinely-stale head
    for i in items[2:]:
        session.refresh(i)
        assert i.status == BufferStatus.ready              # the tail waits for its turn


def test_expiry_never_kills_an_operator_scheduled_item_before_its_time(session, user, channel):
    from database.types import BufferStatus
    from workers.scheduler import expire_stale_buffers

    cam = _mk_campaign(session, user, channel, posting_slots=["09:00"])
    item = _ready_item(session, cam, channel, 1, age_hours=100,
                       publish_at=datetime.utcnow() + timedelta(days=3))
    assert expire_stale_buffers(session) == 0
    session.refresh(item)
    assert item.status == BufferStatus.ready               # it waits for ITS time (ADR-059)


def test_expiry_lands_in_the_autopilot_feed(session, user, channel):
    from database.models import AutopilotAction
    from workers.scheduler import expire_stale_buffers

    cam = _mk_campaign(session, user, channel)              # no slots → flat 72h cutoff
    _ready_item(session, cam, channel, 1, age_hours=100)
    assert expire_stale_buffers(session) == 1
    row = session.query(AutopilotAction).filter_by(kind="expired").one()
    assert "Expired Ep 1" in row.summary


def test_stranded_campaign_completes_honestly(session, user, channel):
    """All planned episodes terminal, one dead beyond any automatic retry → the campaign closes
    at N-1/N instead of sitting 'active' forever — and the next pending campaign activates."""
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from workers.scheduler import finish_stranded_campaign

    cam = _mk_campaign(session, user, channel)
    cam.total_episodes = 3
    cam.current_episode = 2
    for ep in (1, 2):
        _done_task(session, cam, ep)
    dead = Task(campaign_id=cam.id, user_id=user.id, episode_number=3,
                status=TaskStatus.FAILED, finished_at=datetime.utcnow(),
                auto_retry_count=2, error_message="safety filter blocked the content")
    session.add(dead)
    nxt = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Next",
                   total_episodes=5, status=CampaignStatus.pending)
    session.add(nxt)
    session.commit()
    assert finish_stranded_campaign(session, cam) is True
    session.refresh(cam)
    session.refresh(nxt)
    assert cam.status == CampaignStatus.completed
    assert nxt.status == CampaignStatus.active


def test_a_campaign_the_autopilot_can_still_save_is_not_stranded(session, user, channel):
    from database.models import Task
    from database.types import CampaignStatus, TaskStatus
    from workers.scheduler import finish_stranded_campaign

    cam = _mk_campaign(session, user, channel)
    cam.total_episodes = 2
    cam.current_episode = 1
    _done_task(session, cam, 1)
    session.add(Task(campaign_id=cam.id, user_id=user.id, episode_number=2,
                     status=TaskStatus.FAILED, finished_at=datetime.utcnow(),
                     auto_retry_count=0, error_message="connection timed out"))
    session.commit()
    assert finish_stranded_campaign(session, cam) is False   # a transient retry is coming
    session.refresh(cam)
    assert cam.status == CampaignStatus.active


def test_youtube_retry_adopts_an_upload_that_already_landed(session, user, channel, monkeypatch):
    from services import youtube_service

    monkeypatch.setattr(youtube_service, "find_existing_upload",
                        lambda ch, title: "yt-999" if title == "My title" else None)
    vid = youtube_service.upload_video(channel, "/no/v.mp4", {"title": "My title"},
                                       check_existing=True)
    assert vid == "yt-999"                                  # adopted — nothing re-uploaded


def test_compilation_gets_the_free_sanity_check(session, user, channel, monkeypatch, tmp_path):
    from core import compilation, qc
    from database.models import BufferPoolItem, Task
    from workers import video_worker

    cam = _mk_campaign(session, user, channel)
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(compilation, "library_dir", lambda cid: str(lib))
    monkeypatch.setattr(compilation, "episode_master_path",
                        lambda cid, ep: str(lib / f"ep_{ep}.mp4"))
    for ep in (1, 2, 3):
        (lib / f"ep_{ep}.mp4").write_bytes(b"x")
        _done_task(session, cam, ep, stats={"views": 10, "avg_pct_viewed": 60.0})
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=9001,
             video_kind="compilation", render_json={"top_n": 3})
    session.add(t)
    session.commit()
    session.refresh(t)
    monkeypatch.setattr("core.media.probe_duration", lambda p: 30.0)
    monkeypatch.setattr("core.ffmpeg_runner.run_ffmpeg",
                        lambda args: open(args[-1], "wb").write(b"m"))  # args[-1] = the out path
    monkeypatch.setattr("core.thumbnail.generate_thumbnail", lambda *a, **k: None)
    monkeypatch.setattr(qc, "run_deterministic_qc",
                        lambda p: qc.QCResult(passed=False, score=None, issues=["silence for 5.0s"]))
    video_worker.compile_task(t.id)
    buf = session.query(BufferPoolItem).filter_by(campaign_id=cam.id, episode_number=9001).one()
    report = buf.metadata_json["qc"]
    assert report["deterministic_only"] is True and report["passed"] is False
    assert report["score"] is None                          # score-less → review ALWAYS escalates
    from core.autopilot import review_decision

    action, _why = review_decision(report, 7, 4)
    assert action == "escalate"                             # never auto-rejected/approved


@pytest.fixture
def client_env(session, user, channel):
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c, session, user, channel
