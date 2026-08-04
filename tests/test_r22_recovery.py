"""R22 — the failure-handling audit: honest diagnosis, reconcile-before-fail, and the end of
unreviewed auto-publishing.

The incident these tests pin down: an episode showing "Failed — The worker stopped making
progress" ABOVE its own finished, QC-10/10 video with an Approve button; approving looped back to
the same screen; and every unrelated failure wore the same misdiagnosed banner because the
classifier matched the word "worker" inside stored tracebacks' file paths.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import update


# ── Helpers ──────────────────────────────────────────────────────────────────
def _campaign(session, user, channel, **cfg):
    from database.models import Campaign
    from database.types import CampaignStatus

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name="R22",
                 current_episode=0, total_episodes=5, status=CampaignStatus.active,
                 config_json={"language": "en", **cfg})
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _task(session, camp, user, ep, status, **kw):
    from database.models import Task

    t = Task(campaign_id=camp.id, user_id=user.id, episode_number=ep, status=status, **kw)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def _buffer(session, camp, channel, ep, status, tmp_path=None, path=None, **kw):
    from database.models import BufferPoolItem

    if path is None:
        f = tmp_path / f"ep{ep}.mp4"
        f.write_bytes(b"video")
        path = str(f)
    kw.setdefault("metadata_json", {"title": f"Ep {ep}"})
    b = BufferPoolItem(campaign_id=camp.id, channel_id=channel.id, episode_number=ep,
                       video_path=path, status=status, **kw)
    session.add(b)
    session.commit()
    session.refresh(b)
    return b


def _fake_traceback(summary: str) -> str:
    """A realistic stored traceback: frame lines full of file paths, exception summary last."""
    return ("Traceback (most recent call last):\n"
            '  File "/app/workers/video_worker.py", line 1213, in publish_task\n'
            "    _publish_buffer(db, task, buf, campaign, channel, user)\n"
            '  File "/app/services/youtube_service.py", line 62, in build_credentials\n'
            "    creds.refresh(Request())\n"
            f"{summary}\n")


# ── Classification: the exception decides, never the frames' file paths ──────
def test_a_traceback_classifies_by_its_exception_not_its_file_paths():
    from core import failure

    msg = _fake_traceback("requests.exceptions.ConnectionError: Max retries exceeded with url: /x")
    diag = failure.diagnose(msg)
    assert diag["cause"] == "A provider was unreachable"     # not "worker stopped making progress"
    assert failure.is_transient(msg) is True
    assert failure.is_quota(msg) is False                    # "exceeded" is not a quota word (R22)


def test_a_safety_block_is_no_longer_a_transient_worker_stall():
    from core import failure

    msg = _fake_traceback("core.ai_engine.GeminiBlockedError: Gemini blocked the response "
                          "(finish_reason=SAFETY).")
    assert failure.diagnose(msg)["cause"] == "The safety filter blocked the content"
    assert failure.is_transient(msg) is False                # autopilot must NOT re-run it


def test_disk_full_points_at_the_disk_not_the_worker():
    from core import failure

    msg = _fake_traceback("OSError: [Errno 28] No space left on device (ENOSPC)")
    assert failure.diagnose(msg)["cause"] == "The box ran out of disk"


def test_the_stall_writers_still_classify_as_worker_stall():
    from core import failure

    for msg in (
        "Render stalled — no progress for 55 minutes, past this job's own timeout. The worker "
        "was restarted automatically to free the queue.",
        "Worker crashed, timed out, or the job was lost (no progress for a long time). Use Retry.",
        "The worker restarted while this episode was in flight (operator restart, redeploy or a "
        "crash), so the render was abandoned.",
    ):
        assert failure.diagnose(msg)["cause"] == "The worker stopped making progress", msg
        assert failure.is_infrastructure(msg) is True, msg


def test_an_interrupted_upload_never_reads_as_a_render_stall():
    from core import failure

    msg = ("The upload was interrupted by a worker restart (operator restart, redeploy or a "
           "crash). Retry re-attempts the upload — the platform is checked for a duplicate first.")
    diag = failure.diagnose(msg)
    assert diag["cause"] == "An upload was interrupted"
    assert "re-render" not in diag["fix"].lower() or "no re-render" in diag["fix"].lower()
    assert failure.is_infrastructure(msg) is True


def test_youtube_auth_death_has_its_own_class_and_is_not_retryable():
    from core import failure

    msg = _fake_traceback("google.auth.exceptions.RefreshError: ('invalid_grant: Token has been "
                          "expired or revoked.', {'error': 'invalid_grant'})")
    diag = failure.diagnose(msg)
    assert diag["cause"] == "The YouTube connection is no longer valid"
    assert diag["href"] == "/channels"
    assert failure.is_transient(msg) is False


def test_graph_rate_limit_is_transient_not_quota_class():
    from core import failure

    msg = _fake_traceback("services.facebook_service.FacebookError: Facebook temporarily "
                          "unavailable (rate limit / transient, code 4)")
    assert failure.diagnose(msg)["cause"] == "A provider was unreachable"
    assert failure.is_transient(msg) is True
    assert failure.is_quota(msg) is False                    # never deferred to Pacific midnight


def test_numeric_status_words_match_on_digit_boundaries():
    from core import failure

    assert failure.is_quota("task 14290 failed for reasons") is False   # "429" inside an id
    assert failure.is_quota("HTTP 429: RESOURCE_EXHAUSTED") is True


def test_reject_rows_classify_first_and_human_vs_auto_is_structural():
    from core import failure

    human = "Rejected in review: the intro timed out and looked blocked"
    auto = "Rejected in review (auto-review): pacing too slow"
    for msg in (human, auto):
        assert failure.diagnose(msg)["cause"] == "Rejected in review"   # free text can't reclassify
        assert failure.is_transient(msg) is False
        assert failure.is_reject(msg) is True
    assert failure.is_human_reject(human) is True
    assert failure.is_human_reject(auto) is False
    # A human merely MENTIONING auto-review is still a human (the old substring check flipped it).
    assert failure.is_human_reject("Rejected in review: auto-review missed this, intro silent") \
        is True


def test_model_not_found_and_expiry_have_actionable_rows():
    from core import failure

    m1 = _fake_traceback("core.ai_engine.GeminiError: Gemini model not found (gemini-9) — "
                         "update GEMINI_MODEL. 404")
    assert failure.diagnose(m1)["cause"] == "The configured AI model was retired or renamed"
    assert failure.is_transient(m1) is False
    m2 = "Rendered episode aged out before it could publish (waited more than 72h)."
    assert failure.diagnose(m2)["cause"] == "A rendered episode aged out before it could publish"
    assert failure.is_transient(m2) is False


# ── Reconcile-before-fail: finished work is never labeled FAILED ─────────────
def test_watchdog_restores_review_instead_of_failing_a_finished_render(session, user, channel,
                                                                       tmp_path):
    from database.types import BufferStatus, TaskStatus
    from workers import watchdog

    camp = _campaign(session, user, channel)
    t = _task(session, camp, user, 1, TaskStatus.RENDERING)
    _buffer(session, camp, channel, 1, BufferStatus.awaiting_review, tmp_path)

    assert watchdog.fail_stalled_task(session, t.id, 4000) is False
    session.refresh(t)
    assert t.status == TaskStatus.AWAITING_REVIEW            # the incident screen can't happen
    assert t.error_message is None


def test_watchdog_no_longer_fails_publishing_tasks(session, user, channel):
    from database.types import TaskStatus
    from workers import watchdog

    camp = _campaign(session, user, channel)
    t = _task(session, camp, user, 1, TaskStatus.PUBLISHING)
    assert watchdog.fail_stalled_task(session, t.id, 4000) is False
    session.refresh(t)
    assert t.status == TaskStatus.PUBLISHING                 # uploads answer to their RQ timeout


def test_boot_recovery_reconciles_survivors_and_speaks_upload_for_uploads(session, user, channel,
                                                                          tmp_path):
    from database.types import BufferStatus, TaskStatus
    from workers import scheduler

    camp = _campaign(session, user, channel)
    t_done = _task(session, camp, user, 1, TaskStatus.PUBLISHING, published_video_id="vid-1")
    t_ready = _task(session, camp, user, 2, TaskStatus.RENDERING)
    _buffer(session, camp, channel, 2, BufferStatus.ready, tmp_path)
    t_render = _task(session, camp, user, 3, TaskStatus.RENDERING)
    t_upload = _task(session, camp, user, 4, TaskStatus.PUBLISHING)

    scheduler.fail_orphaned_renders(session)
    for t in (t_done, t_ready, t_render, t_upload):
        session.refresh(t)
    assert t_done.status == TaskStatus.COMPLETED             # the upload had landed — never re-render
    assert t_ready.status == TaskStatus.SCHEDULED            # render finished — publish will retry
    assert t_render.status == TaskStatus.FAILED              # nothing survived — honest failure
    assert "render was abandoned" in t_render.error_message
    assert t_upload.status == TaskStatus.FAILED
    assert "upload was interrupted" in t_upload.error_message.lower()   # not render vocabulary


def test_reaper_leaves_a_genuinely_queued_backlog_task_alone(session, user, channel, monkeypatch):
    from database.types import TaskStatus
    from workers import scheduler, task_queue

    camp = _campaign(session, user, channel)
    t = _task(session, camp, user, 1, TaskStatus.PENDING_QUEUE, rq_job_id="job-live")
    session.execute(update(type(t)).where(type(t).id == t.id)
                    .values(updated_at=datetime.utcnow() - timedelta(hours=9)))
    session.commit()
    monkeypatch.setattr(task_queue.render_queue, "get_job_ids", lambda: ["job-live"])

    assert scheduler.reap_stuck_tasks(session) == 0
    session.refresh(t)
    assert t.status == TaskStatus.PENDING_QUEUE              # a deep backlog is not a corpse


def test_hourly_reconciler_heals_the_incident_rows(session, user, channel, tmp_path, monkeypatch):
    """The exact rows the operator had in production: FAILED task + finished awaiting_review video,
    and an approved SCHEDULED episode whose publish job was lost."""
    from database.types import BufferStatus, TaskStatus
    from workers import scheduler, task_queue

    camp = _campaign(session, user, channel, auto_publish=False)
    t_rev = _task(session, camp, user, 1, TaskStatus.FAILED,
                  error_message="Render stalled — no progress for 55 minutes")
    _buffer(session, camp, channel, 1, BufferStatus.awaiting_review, tmp_path)
    t_lost = _task(session, camp, user, 2, TaskStatus.SCHEDULED)
    _buffer(session, camp, channel, 2, BufferStatus.ready, tmp_path,
            metadata_json={"publish_requested_at": datetime.utcnow().isoformat()})
    session.execute(update(type(t_lost)).where(type(t_lost).id == t_lost.id)
                    .values(updated_at=datetime.utcnow() - timedelta(hours=1)))
    session.commit()
    enqueued = []
    monkeypatch.setattr(task_queue, "enqueue_publish", lambda bid: enqueued.append(bid) or "pj")

    healed = scheduler.reconcile_stranded_episodes(session)
    session.refresh(t_rev)
    assert healed["repaired"] == 1 and t_rev.status == TaskStatus.AWAITING_REVIEW
    assert healed["requeued"] == 1 and enqueued            # the lost publish was re-issued


def test_hourly_reconciler_never_resurrects_a_human_reject(session, user, channel, tmp_path):
    from database.types import BufferStatus, TaskStatus
    from workers import scheduler

    camp = _campaign(session, user, channel)
    t = _task(session, camp, user, 1, TaskStatus.FAILED,
              error_message="Rejected in review: wrong tone")
    _buffer(session, camp, channel, 1, BufferStatus.awaiting_review, tmp_path)
    scheduler.reconcile_stranded_episodes(session)
    session.refresh(t)
    assert t.status == TaskStatus.FAILED                     # the human's decision stands


# ── The approve → publish loop, closed ───────────────────────────────────────
def test_publish_failure_keeps_the_buffer_approved_and_names_the_publish(session, user, channel,
                                                                         tmp_path, monkeypatch):
    from core import failure
    from database.types import BufferStatus, TaskStatus
    from workers import video_worker

    camp = _campaign(session, user, channel)
    t = _task(session, camp, user, 1, TaskStatus.SCHEDULED)
    buf = _buffer(session, camp, channel, 1, BufferStatus.ready, tmp_path)

    def boom(channel, video_path, metadata, user, **k):
        raise RuntimeError("YouTube API error 500: backendError")

    monkeypatch.setattr(video_worker, "_publish", boom)
    video_worker.publish_task(buf.id)
    session.refresh(t)
    session.refresh(buf)
    assert t.status == TaskStatus.FAILED
    assert buf.status == BufferStatus.ready                  # approval survives a failed upload
    assert t.error_message.startswith("Publish failed")
    diag = failure.diagnose(t.error_message)
    assert diag is None or diag["cause"] != "The worker stopped making progress"


def test_publish_task_refuses_an_unapproved_buffer(session, user, channel, tmp_path, monkeypatch):
    from database.types import BufferStatus, TaskStatus
    from workers import video_worker

    camp = _campaign(session, user, channel)
    t = _task(session, camp, user, 1, TaskStatus.AWAITING_REVIEW)
    buf = _buffer(session, camp, channel, 1, BufferStatus.awaiting_review, tmp_path)
    uploads = []
    monkeypatch.setattr(video_worker, "_publish",
                        lambda channel, video_path, metadata, user, **k: uploads.append(1) or "v")
    video_worker.publish_task(buf.id)
    session.refresh(buf)
    session.refresh(t)
    assert not uploads and buf.status == BufferStatus.awaiting_review
    assert t.status == TaskStatus.AWAITING_REVIEW            # approval stays the one gate


def test_a_second_publish_attempt_arms_the_duplicate_guard(session, user, channel, tmp_path,
                                                           monkeypatch):
    from database.types import BufferStatus, TaskStatus
    from workers import video_worker

    camp = _campaign(session, user, channel)
    _task(session, camp, user, 1, TaskStatus.SCHEDULED)
    buf = _buffer(session, camp, channel, 1, BufferStatus.ready, tmp_path)
    seen = []

    def fail_then_succeed(channel, video_path, metadata, user, **k):
        seen.append(bool(k.get("retrying")))
        if len(seen) == 1:
            raise RuntimeError("connection timed out")
        return "vid-2"

    monkeypatch.setattr(video_worker, "_publish", fail_then_succeed)
    video_worker.publish_task(buf.id)                        # attempt 1: fails after marker commit
    session.refresh(buf)
    assert buf.status == BufferStatus.ready
    video_worker.publish_task(buf.id)                        # attempt 2: must check for duplicates
    assert seen == [False, True]


def test_apply_approve_records_intent_and_counts_the_failed_retry(session, user, channel, tmp_path):
    from database.types import BufferStatus, TaskStatus
    from workers import video_worker

    camp = _campaign(session, user, channel)
    t = _task(session, camp, user, 1, TaskStatus.FAILED, error_message="Publish failed: x",
              retry_count=0)
    buf = _buffer(session, camp, channel, 1, BufferStatus.awaiting_review, tmp_path)
    video_worker.apply_approve(session, buf)
    session.refresh(t)
    session.refresh(buf)
    assert buf.status == BufferStatus.ready and buf.ready_at is not None
    assert (buf.metadata_json or {}).get("publish_requested_at")   # durable intent for the reconciler
    assert t.status == TaskStatus.SCHEDULED and t.retry_count == 1
    steps = [j["step"] for j in (t.render_json or {}).get("journey", [])]
    assert "Review" in steps


def test_apply_approve_refuses_vanished_files_and_inflight_episodes(session, user, channel,
                                                                    tmp_path):
    from database.types import BufferStatus, TaskStatus
    from workers import video_worker

    camp = _campaign(session, user, channel)
    _task(session, camp, user, 1, TaskStatus.AWAITING_REVIEW)
    gone = _buffer(session, camp, channel, 1, BufferStatus.awaiting_review,
                   path="/nonexistent/v.mp4")
    with pytest.raises(video_worker.ReviewConflict):
        video_worker.apply_approve(session, gone)
    _task(session, camp, user, 2, TaskStatus.RENDERING)      # a re-render is in flight
    busy = _buffer(session, camp, channel, 2, BufferStatus.awaiting_review, tmp_path)
    with pytest.raises(video_worker.ReviewConflict):
        video_worker.apply_approve(session, busy)


# ── Expiry fairness ──────────────────────────────────────────────────────────
def test_expiry_ages_from_approval_and_spares_token_waits_and_inflight_publishes(
        session, user, channel, tmp_path, monkeypatch):
    from database.types import BufferStatus, ChannelStatus, TaskStatus
    from workers import scheduler, task_queue

    camp = _campaign(session, user, channel)
    old = datetime.utcnow() - timedelta(hours=100)
    # (1) rendered 100h ago but approved 1h ago → NOT stale.
    _task(session, camp, user, 1, TaskStatus.SCHEDULED)
    b1 = _buffer(session, camp, channel, 1, BufferStatus.ready, tmp_path,
                 created_at=old, ready_at=datetime.utcnow() - timedelta(hours=1))
    # (2) genuinely stale → expired, with the honest aged-out message.
    t2 = _task(session, camp, user, 2, TaskStatus.SCHEDULED)
    b2 = _buffer(session, camp, channel, 2, BufferStatus.ready, tmp_path, created_at=old)
    # (3) stale but its publish job is already queued → spared.
    _task(session, camp, user, 3, TaskStatus.SCHEDULED)
    b3 = _buffer(session, camp, channel, 3, BufferStatus.ready, tmp_path, created_at=old)
    monkeypatch.setattr(task_queue, "queued_publish_buffer_ids", lambda: {b3.id})

    scheduler.expire_stale_buffers(session)
    session.refresh(b1)
    session.refresh(b2)
    session.refresh(b3)
    session.refresh(t2)
    assert b1.status == BufferStatus.ready
    assert b3.status == BufferStatus.ready
    assert b2.status == BufferStatus.expired
    assert "aged out before it could publish" in t2.error_message

    # (4) an expired channel holds its finished episodes instead of destroying them.
    channel.status = ChannelStatus.expired
    session.commit()
    b4 = _buffer(session, camp, channel, 4, BufferStatus.ready, tmp_path, created_at=old)
    monkeypatch.setattr(task_queue, "queued_publish_buffer_ids", lambda: set())
    scheduler.expire_stale_buffers(session)
    session.refresh(b4)
    assert b4.status == BufferStatus.ready


# ── Circuit breaker: infrastructure is transparent ───────────────────────────
def test_breaker_streak_skips_infrastructure_and_reject_rows(session, user, channel):
    from database.types import TaskStatus
    from workers import video_worker

    camp = _campaign(session, user, channel)
    base = datetime.utcnow()
    infra = ("Render stalled — no progress for 55 minutes",
             "Worker crashed, timed out, or the job was lost (no progress for a long time).",
             "Rejected in review (auto-review): weak hook")
    for i, msg in enumerate(infra):
        _task(session, camp, user, i + 1, TaskStatus.FAILED, error_message=msg,
              finished_at=base + timedelta(minutes=i))
    _task(session, camp, user, 9, TaskStatus.FAILED,
          error_message=_fake_traceback("RuntimeError: bad api key / unauthorized"),
          finished_at=base + timedelta(minutes=9))
    # 3 infra/reject rows + 1 real failure = a streak of ONE, not four.
    assert video_worker.consecutive_failures(session, camp) == 1


# ── Campaign lifecycle guard ─────────────────────────────────────────────────
def test_late_publish_on_a_completed_campaign_activates_nothing_twice(session, user, channel):
    from database.models import Campaign
    from database.types import CampaignStatus
    from workers import video_worker

    camp = _campaign(session, user, channel)
    camp.total_episodes = 2
    camp.current_episode = 2
    camp.status = CampaignStatus.completed
    nxt = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Next",
                   current_episode=0, total_episodes=3, status=CampaignStatus.pending)
    session.add(nxt)
    session.commit()

    events = video_worker.advance_campaign(session, camp)
    session.refresh(nxt)
    session.refresh(camp)
    assert events.completed is False and events.activated_campaign_id is None
    assert nxt.status == CampaignStatus.pending              # not prematurely started
    assert camp.current_episode == 2                         # clamped at the total


# ── The worker's job timeout cannot be swallowed ─────────────────────────────
def test_the_death_penalty_is_a_baseexception_no_pipeline_handler_can_eat():
    import run_worker

    assert issubclass(run_worker.JobHardTimeout, BaseException)
    assert not issubclass(run_worker.JobHardTimeout, Exception)
    penalty = run_worker.FactoryWorker.death_penalty_class(5, Exception)
    with pytest.raises(run_worker.JobHardTimeout):
        penalty.handle_death_penalty(None, None)


# ── Publish jobs carry no stall-watchdog progress entry ──────────────────────
def test_a_publishing_episode_is_invisible_to_the_stall_watchdog(session, user, channel, tmp_path,
                                                                 monkeypatch):
    from database.types import BufferStatus, TaskStatus
    from workers import task_queue, video_worker

    camp = _campaign(session, user, channel)
    t = _task(session, camp, user, 1, TaskStatus.SCHEDULED)
    buf = _buffer(session, camp, channel, 1, BufferStatus.ready, tmp_path)
    entries_during_upload = []

    def slow_upload(channel, video_path, metadata, user, **k):
        entries_during_upload.append(task_queue.get_progress(t.id))
        return "vid-3"

    monkeypatch.setattr(video_worker, "_publish", slow_upload)
    video_worker.publish_task(buf.id)
    # During the upload the render-progress entry was already cleared — a >55-minute upload can
    # never read as a wedged render and be os._exit-killed mid-transfer (R22).
    assert entries_during_upload == [0.0]
