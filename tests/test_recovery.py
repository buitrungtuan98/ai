"""ADR-070 — the five defects the post-R7 audit found, each pinned by the failure it caused.

Every one of these passed the suite before: they are gaps in what was tested, not regressions. What
they share is a mechanism that was *almost* right — the watchdog cleaned up after a stall but not
after a restart, the autopilot retried failures but did not check whether the campaign still wanted
them, the diagnosis table matched on the wrong word first, Auto-QC re-rendered deterministically.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


def _campaign(session, user, channel, status=None, **cfg):
    from database.models import Campaign
    from database.types import CampaignStatus

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name=cfg.pop("topic", "Recover"),
                 total_episodes=3, status=status or CampaignStatus.active, config_json=cfg)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _task(session, campaign, user, episode=1, **kw):
    from database.models import Task

    t = Task(campaign_id=campaign.id, user_id=user.id, episode_number=episode, **kw)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


# ── 1. A restart no longer leaves a "Rendering 47%" nothing is working on ────
def test_boot_fails_the_render_the_previous_worker_abandoned(session, user, channel):
    """The watchdog does this bookkeeping on the STALL path only — a deliberate "Restart worker"
    click, or a redeploy whose 300s grace expired mid-encode, left the episode reading RENDERING
    until the stuck-task reaper noticed ~2 hours later."""
    from database.types import TaskStatus
    from workers import scheduler, task_queue

    camp = _campaign(session, user, channel)
    live = _task(session, camp, user, 1, status=TaskStatus.RENDERING, progress_pct=47)
    task_queue.set_progress(live.id, 47)

    assert scheduler.fail_orphaned_renders(session) == 1
    session.refresh(live)
    assert live.status == TaskStatus.FAILED
    assert live.finished_at is not None
    assert task_queue.get_progress(live.id) in (None, 0)      # no ghost % left behind


def test_every_working_stage_is_recovered_not_just_rendering(session, user, channel):
    """An episode can be abandoned while writing its script or while uploading, too."""
    from database.types import TaskStatus
    from workers import scheduler

    camp = _campaign(session, user, channel)
    for i, st in enumerate((TaskStatus.AI_GENERATION, TaskStatus.RENDERING,
                            TaskStatus.AUDIO_SYNCED, TaskStatus.PUBLISHING), start=1):
        _task(session, camp, user, i, status=st)
    assert scheduler.fail_orphaned_renders(session) == 4


def test_boot_recovery_leaves_settled_work_alone(session, user, channel):
    """Published, failed, cancelled, queued and awaiting-review episodes are not in flight — touching
    them would undo real outcomes (and resurrect an operator's cancel)."""
    from database.types import TaskStatus
    from workers import scheduler

    camp = _campaign(session, user, channel)
    keep = [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
            TaskStatus.PENDING_QUEUE, TaskStatus.AWAITING_REVIEW, TaskStatus.SCHEDULED]
    tasks = [_task(session, camp, user, i, status=st) for i, st in enumerate(keep, start=1)]

    assert scheduler.fail_orphaned_renders(session) == 0
    for t, st in zip(tasks, keep):
        session.refresh(t)
        assert t.status == st


def test_the_abandoned_render_reads_as_resumable_and_the_autopilot_takes_it(session, user, channel):
    """The whole point of failing it: the message classifies as transient, so the autopilot re-queues
    it — and with R7's kept workspace that retry is a resume, not a restart."""
    from database.types import TaskStatus
    from workers import scheduler

    camp = _campaign(session, user, channel)
    t = _task(session, camp, user, 1, status=TaskStatus.RENDERING)
    scheduler.fail_orphaned_renders(session)
    session.refresh(t)

    from core import failure

    assert failure.is_transient(t.error_message) is True
    assert failure.diagnose(t.error_message)["cause"] == "The worker stopped making progress"
    assert scheduler.autopilot_retry_channel(session, channel) == 1
    session.refresh(t)
    assert t.status == TaskStatus.PENDING_QUEUE


def test_boot_recovery_does_not_burn_the_retry_cap_itself(session, user, channel):
    """It marks work failed; it does NOT retry. Re-enqueueing here would outrank the cap and could
    crash-loop on the very episode that killed the worker."""
    from database.types import TaskStatus
    from workers import scheduler

    camp = _campaign(session, user, channel)
    t = _task(session, camp, user, 1, status=TaskStatus.RENDERING, retry_count=1)
    scheduler.fail_orphaned_renders(session)
    session.refresh(t)
    assert t.retry_count == 1


def test_the_restart_button_now_promises_what_actually_happens(client_ops):
    """The confirm said "it can be retried" while the episode in fact sat in RENDERING for hours."""
    body = client_ops.get("/operations?tab=worker").text
    form = body.split('action="/operations/restart-worker"', 1)[1].split(">", 1)[0]
    assert "marks it failed" in form
    assert "resumes from the scenes already rendered" in form


@pytest.fixture
def client_ops():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


# ── 2. The autopilot stops fighting the breaker and the lifecycle ────────────
def test_autopilot_does_not_resurrect_a_campaign_the_breaker_stopped(session, user, channel):
    """The consecutive-failure breaker exists to STOP a campaign whose config is broken; the autopilot
    was re-queueing the very episodes it stopped it for."""
    from database.types import CampaignStatus, TaskStatus
    from workers import scheduler

    camp = _campaign(session, user, channel, status=CampaignStatus.failed)
    _task(session, camp, user, 1, status=TaskStatus.FAILED, error_message="Read timed out")

    assert scheduler.autopilot_retry_channel(session, channel) == 0


def test_autopilot_does_not_publish_for_a_campaign_the_operator_finished(session, user, channel):
    """A completed campaign with a leftover failure would be re-rendered and — on auto-publish —
    actually uploaded, days after the operator considered it closed."""
    from database.types import CampaignStatus, TaskStatus
    from workers import scheduler

    camp = _campaign(session, user, channel, status=CampaignStatus.completed)
    _task(session, camp, user, 1, status=TaskStatus.FAILED, error_message="Read timed out")

    assert scheduler.autopilot_retry_channel(session, channel) == 0


def test_an_active_campaign_on_the_same_channel_is_still_retried(session, user, channel):
    """The filter must scope to the campaign, not silence the channel."""
    from database.types import CampaignStatus, TaskStatus
    from workers import scheduler

    dead = _campaign(session, user, channel, status=CampaignStatus.failed, topic="Stopped")
    _task(session, dead, user, 1, status=TaskStatus.FAILED, error_message="Read timed out")
    live = _campaign(session, user, channel, topic="Running")
    ok = _task(session, live, user, 1, status=TaskStatus.FAILED, error_message="Read timed out")

    assert scheduler.autopilot_retry_channel(session, channel) == 1
    session.refresh(ok)
    assert ok.status == TaskStatus.PENDING_QUEUE


# ── 3. A wedged worker is not reported as an unreachable provider ────────────
def test_a_stalled_render_is_diagnosed_as_the_worker_not_the_network():
    """Both the watchdog's and the reaper's wording contain "timeout" as well as "stalled"/"worker",
    and the network class matched first — so the bell blamed the vendor for this box's own fault."""
    from core import failure
    from workers import watchdog

    watchdog_msg = (
        "Render stalled — no progress for 51 minutes, past this job's own timeout. The worker was "
        "restarted automatically to free the queue. Use Retry to render this episode again.")
    assert failure.diagnose(watchdog_msg)["cause"] == "The worker stopped making progress"

    reaper_msg = ("Worker crashed, timed out, or the job was lost (no progress for a long time). "
                  "Use Retry.")
    assert failure.diagnose(reaper_msg)["cause"] == "The worker stopped making progress"
    assert watchdog is not None      # the module owns the first message (import kept meaningful)


def test_a_real_provider_timeout_is_still_a_provider_timeout():
    """The reorder must not swallow the class it was moved ahead of."""
    from core import failure

    for msg in ("Pollinations flux request failed: Read timed out",
                "image wait timeout: this episode's image-fetch budget is spent",
                "HTTPSConnectionPool: connection reset"):
        assert failure.diagnose(msg)["cause"] == "A provider was unreachable"


def test_credentials_and_quota_still_win_over_the_worker_class():
    """Order is load-bearing: the specific, non-retryable classes stay first."""
    from core import failure

    quota = "worker log: google.api_core.exceptions.ResourceExhausted: 429 quota exceeded"
    assert failure.diagnose(quota)["cause"] == "A free-tier quota ran out"
    assert failure.is_transient(quota) is False


# ── 4. Auto-QC's re-render actually re-draws ─────────────────────────────────
def test_the_free_provider_seed_changes_only_when_asked():
    """Determinism is right for a resume (same scene → same image, so a checkpoint matches) and wrong
    for a reroll (the QC-rejected video was rebuilt pixel-for-pixel and re-judged)."""
    from core import ai_engine

    base = ai_engine._pollinations_seed("a robot in the rain")
    assert base == ai_engine._pollinations_seed("a robot in the rain")     # resume-safe
    assert base == ai_engine._pollinations_seed("a robot in the rain", 0)  # salt 0 == no salt
    assert base != ai_engine._pollinations_seed("a robot in the rain", 1)  # QC retry differs


def test_the_salt_reaches_the_provider_call(tmp_path, monkeypatch):
    from core import ai_engine

    seeds = []

    def fake_poll(*, seed, out_path, **_kw):
        seeds.append(seed)
        open(out_path, "wb").write(b"P")
        return out_path

    monkeypatch.setattr(ai_engine, "_call_pollinations", fake_poll)
    for salt in (0, 1):
        ai_engine.generate_image(prompt="p", api_key="", out_path=str(tmp_path / f"{salt}.png"),
                                 model="pollinations:flux", seed_salt=salt)
    assert seeds[0] != seeds[1]


def test_the_qc_rerender_passes_a_salt_but_the_first_attempt_does_not(session, user, channel,
                                                                     monkeypatch):
    """Attempt 1 must stay salt-free so its stills remain reusable by a resume; attempt 2 rerolls."""
    from core.video_factory import RenderResult
    from database.types import TaskStatus
    from workers import video_worker

    camp = _campaign(session, user, channel, language="en")
    t = _task(session, camp, user, 1, status=TaskStatus.PENDING_QUEUE)

    salts = []

    def fake_produce(**k):
        salts.append(k["image_seed_salt"])
        return RenderResult(master_path="/no/m.mp4", thumbnail_path="/no/t.jpg",
                            metadata={"title": "T", "variant": "A"}, duration=5.0, scene_count=1)

    class _Verdict:
        passed = False
        score = 3
        issues = ["blurry"]
        unavailable = False           # a REAL verdict — the judge ran and disliked it (ADR-084)

    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script())
    monkeypatch.setattr(video_worker.video_factory, "produce", fake_produce)
    monkeypatch.setattr(video_worker, "_publish", lambda *a, **k: "vid-1")
    from core import qc

    monkeypatch.setattr(qc, "make_batch_vetter", lambda *a, **k: None)
    monkeypatch.setattr(qc, "run_deterministic_qc", lambda p: _Verdict())
    monkeypatch.setattr(qc, "run_final_qc", lambda *a, **k: _Verdict())

    video_worker.render_task(t.id)
    assert salts == [0, 1], "attempt 1 deterministic (resume-safe), attempt 2 rerolled"


def _script():
    from core.ai_engine import VideoScript

    return VideoScript(
        language="en", topic="Robots", synopsis="Robots learn to dream",
        scenes=[{"index": i, "narration": "n", "pexels_keywords": ["k"]} for i in range(3)],
        metadata_variations=[{"variant": v, "title": f"T{v}", "description": "d",
                              "tags": ["a", "b", "c"]} for v in "ABC"],
    )


# ── 5. An upload gets an hour ────────────────────────────────────────────────
def test_a_publish_job_is_allowed_a_full_hour():
    """A 15-minute long-form master on a slow uplink can exceed 30 minutes, and being killed
    mid-upload is the failure this box handles worst — the platform may hold a partial video."""
    from workers import task_queue

    job_id = task_queue.enqueue_publish(4321)
    from rq.job import Job

    job = Job.fetch(job_id, connection=task_queue.conn)
    assert job.timeout == 3600


# ── The palette answers Enter the same way whether or not you typed ──────────
def test_the_palette_preselects_its_first_row_on_open():
    """⌘K then Enter did nothing, while ⌘K, one letter, Enter worked — `render()` selects the first
    row and `open()` was un-selecting it again."""
    js = open("static/ui.js", encoding="utf-8").read()
    body = js.split("function open()", 1)[1].split("function close()", 1)[0]
    assert "sel = -1" not in body, "open() must keep render()'s first-row selection"
    assert "input.focus()" in body


def test_checkpoints_still_outlive_the_autopilot_cadence(session, user, channel):
    """Boot recovery writes FAILED rows; those are exactly the rows whose workspace must survive the
    orphan sweep, or every 'resume' is silently a from-scratch re-render."""
    from database.types import TaskStatus
    from workers import scheduler

    camp = _campaign(session, user, channel)
    t = _task(session, camp, user, 1, status=TaskStatus.RENDERING)
    scheduler.fail_orphaned_renders(session)
    assert str(t.id) in scheduler.resume_checkpoint_ids(session)

    t.updated_at = datetime.utcnow() - timedelta(hours=scheduler.RESUME_KEEP_HOURS + 1)
    session.commit()
    assert str(t.id) not in scheduler.resume_checkpoint_ids(session)
