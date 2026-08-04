"""R7 (ADR-069) — a slow image vendor heals instead of killing the render, and a killed render
resumes instead of starting over.

The reported failure: 8 scene images, 120s each; the vendor answers 5 then slows past 120s on the
6th — the whole render failed and everything (5 images, TTS, the script) was thrown away. These
tests pin the three layers of the fix: the retry WAITS LONGER (laddered timeout inside a per-episode
budget), the failure KEEPS ITS WORK (workspace checkpoint + persisted script), and the autopilot
CONTINUES it (retry classification shared with the bell/episode page).
"""
from __future__ import annotations

import os
import time

import pytest


# ── Layer 1: the retry waits longer, inside a budget ─────────────────────────
def test_the_second_attempt_waits_double_not_the_same_wait_again(tmp_path, monkeypatch):
    """A vendor that throttles after N requests needs a LONGER next attempt — retrying with the
    identical 120s is exactly the reported failure mode."""
    from core import ai_engine

    monkeypatch.setattr(ai_engine, "_BACKOFF_BASE_SECONDS", 0)
    waits = []

    def flaky(*, timeout, out_path, **_kw):
        waits.append(timeout)
        if len(waits) == 1:
            raise ai_engine.ImageGenError("Read timed out")
        open(out_path, "wb").write(b"P")
        return out_path

    monkeypatch.setattr(ai_engine, "_call_pollinations", flaky)
    out = ai_engine.generate_image(prompt="p", api_key="", out_path=str(tmp_path / "o.png"),
                                   model="pollinations:flux", timeout_s=100)
    assert out.endswith("o.png")
    assert waits == [100, 200]


def test_an_attempt_never_waits_past_the_episode_budget(monkeypatch):
    from core import ai_engine

    # 300s ladder step, but only ~50s of episode budget left → the attempt gets ~50s.
    t = ai_engine._attempt_timeout(0, 300, time.monotonic() + 50, None)
    assert 40 <= t <= 50


def test_a_spent_budget_fails_fast_and_reads_as_a_timeout(monkeypatch):
    """No vendor call is even started with a hopeless sliver of budget — and the wording matters:
    it must classify as transient (retry/resume) and never as a quota problem."""
    from core import ai_engine, failure

    called = []
    monkeypatch.setattr(ai_engine, "_call_pollinations",
                        lambda **kw: called.append(1))
    with pytest.raises(ai_engine.ImageGenError) as err:
        ai_engine._generate_pollinations_single(
            prompt="p", model="flux", out_path="/nope.png", token=None, width=10, height=10,
            max_retries=2, timeout_s=120, deadline=time.monotonic() + 3)
    assert not called
    msg = str(err.value)
    assert "timeout" in msg.lower()
    assert failure.is_transient(msg) is True
    assert failure.diagnose(msg)["cause"] == "A provider was unreachable"


def test_the_gemini_leg_gets_the_same_ladder(tmp_path, monkeypatch):
    from core import ai_engine

    monkeypatch.setattr(ai_engine, "_BACKOFF_BASE_SECONDS", 0)
    waits = []

    def flaky(*, timeout=None, out_path, **_kw):
        waits.append(timeout)
        if len(waits) == 1:
            raise RuntimeError("504 deadline exceeded upstream")
        open(out_path, "wb").write(b"P")
        return out_path

    monkeypatch.setattr(ai_engine, "_call_gemini_image", flaky)
    ai_engine._generate_image_single(prompt="p", api_key="k", model="img",
                                     out_path=str(tmp_path / "o.png"), reference_paths=[],
                                     max_retries=2, timeout_s=90)
    assert waits == [90, 180]


def test_image_writes_are_atomic(tmp_path):
    """The reuse guard trusts any existing non-empty still, so a fetch killed mid-write must never
    leave a half image at the final name."""
    from core import ai_engine

    out = tmp_path / "img.png"
    ai_engine._write_image_atomic(str(out), b"BYTES")
    assert out.read_bytes() == b"BYTES"
    assert not (tmp_path / "img.png.part").exists()


def test_the_settings_knob_reaches_the_render(session, user, channel, monkeypatch):
    """The operator's per-attempt timeout (Settings page) is what produce() actually renders with."""
    from core.video_factory import RenderResult
    from database.models import Campaign, Task
    from database.types import CampaignStatus
    from workers import video_worker

    user.settings_json = {"image_timeout_s": 45}
    session.commit()
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", current_episode=0,
                   total_episodes=1, status=CampaignStatus.active, config_json={"language": "en"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)

    captured: dict = {}

    def fake_produce(**k):
        captured.update(k)
        return RenderResult(master_path="/no/m.mp4", thumbnail_path="/no/t.jpg",
                            metadata={"title": "T", "variant": "A"}, duration=5.0, scene_count=1)

    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script())
    monkeypatch.setattr(video_worker.video_factory, "produce", fake_produce)
    monkeypatch.setattr(video_worker, "_publish", lambda *a, **k: "vid-1")
    video_worker.render_task(t.id)
    assert captured["image_timeout_s"] == 45


def test_settings_page_clamps_the_timeout(session, user):
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as client:
        client.post("/settings", data={"language": "", "video_format": "", "publish_mode": "",
                                       "posting_slots": "", "total_episodes": "",
                                       "ai_daily_budget": "", "image_timeout_s": "7"})
        session.refresh(user)
        assert user.settings_json["image_timeout_s"] == 30      # below the floor → clamped
        client.post("/settings", data={"language": "", "video_format": "", "publish_mode": "",
                                       "posting_slots": "", "total_episodes": "",
                                       "ai_daily_budget": "", "image_timeout_s": ""})
        session.refresh(user)
        assert user.settings_json is None                       # blank clears the override


# ── Layer 2: a failed render keeps its work and resumes ──────────────────────
def _stub_pipeline(monkeypatch):
    """Stub every ffmpeg/TTS/thumbnail touchpoint so only the orchestration logic runs (the same
    seam set the Studio produce() tests use)."""
    from core import video_factory
    from core.tts import WordTiming

    def fake_tts(text, out, **k):
        open(out, "w").write("audio")
        return [WordTiming("w", 0.0, 1.0)]

    monkeypatch.setattr(video_factory.tts, "synthesize_paced", fake_tts)
    monkeypatch.setattr(video_factory, "voice_check", lambda *a, **k: None)
    monkeypatch.setattr(video_factory.media, "probe_duration", lambda p: 5.0)
    monkeypatch.setattr(video_factory, "still_to_clip", lambda img, out, d, profile=None: out)
    monkeypatch.setattr(video_factory, "build_ass", lambda *a, **k: open(a[1], "w").write("ass"))
    monkeypatch.setattr(video_factory, "run_ffmpeg", lambda *a, **k: None)
    monkeypatch.setattr(video_factory, "pick_metadata",
                        lambda *a, **k: {"title": "T", "variant": "A", "description": "d"})
    monkeypatch.setattr(video_factory, "generate_thumbnail", lambda *a, **k: None)


def _studio_script(n=3, word="scene"):
    from core.ai_engine import VideoScript

    return VideoScript(
        language="en", topic="Robots", synopsis="Robots learn to dream",
        scenes=[{"index": i, "narration": f"{word} {i}", "pexels_keywords": [f"k{i}"]}
                for i in range(n)],
        metadata_variations=[{"variant": v, "title": f"T{v}", "description": "d",
                              "tags": ["a", "b", "c"]} for v in "ABC"],
    )


def _produce_kwargs(tmp_path, job_id):
    cast = [{"id": "hero", "name": "Hero", "description": "a hero", "style": "ink"}]
    return dict(episode_number=1, pexels_api_key="", job_id=job_id,
                output_dir=str(tmp_path / "out"), visual_source="studio", characters=cast,
                image_api_key="gkey", image_model="img-x",
                studio_sheet_dir=str(tmp_path / "sheets"))


def test_workspace_is_kept_on_failure_and_cleaned_on_success(tmp_path):
    from core.cleanup import RenderWorkspace

    with RenderWorkspace("ok-job", root=str(tmp_path)) as ws:
        open(ws.path("a.png"), "w").write("x")
    assert not os.path.exists(str(tmp_path / "ok-job"))

    with pytest.raises(RuntimeError):
        with RenderWorkspace("dead-job", root=str(tmp_path)) as ws:
            open(ws.path("a.png"), "w").write("x")
            raise RuntimeError("vendor died")
    assert os.path.exists(str(tmp_path / "dead-job" / "a.png"))


def test_an_interrupted_studio_render_resumes_from_its_stills(tmp_path, monkeypatch):
    """The reported case end-to-end: the vendor dies on scene 3 of 3 — the retry redraws ONLY the
    missing scene, and the workspace is cleaned once the episode finally succeeds."""
    from core import video_factory
    from core.ai_engine import ImageGenError
    from core.config import settings

    _stub_pipeline(monkeypatch)
    kwargs = _produce_kwargs(tmp_path, "resume-job")
    ws_dir = os.path.join(settings.WORK_ROOT, "resume-job")

    drawn: list[str] = []

    def dies_on_scene_2(*, prompt, out_path, **_kw):
        if "scene_2_still" in out_path:
            raise ImageGenError("Pollinations flux request failed: Read timed out")
        drawn.append(out_path)
        open(out_path, "w").write("PNG")
        return out_path

    with pytest.raises(ImageGenError):
        video_factory.produce(script=_studio_script(3), gen_image=dies_on_scene_2, **kwargs)
    # The checkpoint survives: the sheet + scenes 0 and 1 are on disk, named by prompt hash.
    assert os.path.isdir(ws_dir)
    kept = os.listdir(ws_dir)
    assert sum(1 for f in kept if "_still_" in f) == 2

    redrawn: list[str] = []

    def works(*, prompt, out_path, **_kw):
        redrawn.append(out_path)
        open(out_path, "w").write("PNG")
        return out_path

    result = video_factory.produce(script=_studio_script(3), gen_image=works, **kwargs)
    assert result.scene_count == 3
    # Only the missing scene was drawn — the sheet is cached in its stable dir, scenes 0/1 reused.
    assert len(redrawn) == 1 and "scene_2_still" in redrawn[0]
    assert not os.path.exists(ws_dir)                    # success consumed the checkpoint


def test_a_checkpoint_never_serves_a_different_script(tmp_path, monkeypatch):
    """Stills are named by prompt hash: a reroll (new script) must redraw everything rather than
    silently recycling frames of the OLD script."""
    from core import video_factory
    from core.ai_engine import ImageGenError
    from core.config import settings

    _stub_pipeline(monkeypatch)
    kwargs = _produce_kwargs(tmp_path, "reroll-job")

    def dies_late(*, prompt, out_path, **_kw):
        if "scene_2_still" in out_path:
            raise ImageGenError("Read timed out")
        open(out_path, "w").write("PNG")
        return out_path

    with pytest.raises(ImageGenError):
        video_factory.produce(script=_studio_script(3, word="alpha"), gen_image=dies_late, **kwargs)

    redrawn: list[str] = []

    def works(*, prompt, out_path, **_kw):
        redrawn.append(out_path)
        open(out_path, "w").write("PNG")
        return out_path

    video_factory.produce(script=_studio_script(3, word="beta"), gen_image=works, **kwargs)
    assert sum(1 for p in redrawn if "_still_" in p) == 3   # nothing stale was reused
    # Boy-scout: the succeeded run removed the workspace (and the stale alpha stills with it).
    assert not os.path.exists(os.path.join(settings.WORK_ROOT, "reroll-job"))


def test_scene_cache_key_is_stable_and_content_sensitive():
    from core import studio

    char = {"name": "Hero", "description": "a hero", "style": "ink"}
    k1 = studio.scene_cache_key(char, "a robot", mood="calm", style_override=None)
    assert k1 == studio.scene_cache_key(char, "a robot", mood="calm", style_override=None)
    assert k1 != studio.scene_cache_key(char, "a dragon", mood="calm", style_override=None)
    assert k1 != studio.scene_cache_key(char, "a robot", mood="calm", style_override="watercolor")
    assert k1 != studio.scene_cache_key(None, "a robot", mood="calm", style_override=None)


def test_retry_reuses_the_persisted_script_but_success_forgets_it(session, user, channel, monkeypatch):
    """A Retry rebuilds the SAME episode: no second script call (quota), and prompts that still match
    the checkpointed stills. The success path's render_json overwrite consumes the checkpoint."""
    from core.video_factory import RenderResult
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", current_episode=0,
                   total_episodes=1, status=CampaignStatus.active, config_json={"language": "en", "auto_qc": "off"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)

    script_calls = []
    monkeypatch.setattr(video_worker, "generate_script",
                        lambda **k: script_calls.append(1) or _script())
    monkeypatch.setattr(video_worker.video_factory, "produce",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("Read timed out")))
    video_worker.render_task(t.id)
    session.refresh(t)
    assert t.status == TaskStatus.FAILED
    assert script_calls == [1]
    assert (t.render_json or {}).get("script"), "the interrupted attempt persisted its script"

    # Retry (what the UI button / autopilot does): back to the queue, then render again.
    t.status = TaskStatus.PENDING_QUEUE
    session.commit()
    monkeypatch.setattr(video_worker.video_factory, "produce",
                        lambda **k: RenderResult(master_path="/no/m.mp4", thumbnail_path="/no/t.jpg",
                                                 metadata={"title": "T", "variant": "A"},
                                                 duration=5.0, scene_count=3))
    monkeypatch.setattr(video_worker, "_publish", lambda *a, **k: "vid-1")
    video_worker.render_task(t.id)
    session.refresh(t)
    assert t.status == TaskStatus.COMPLETED
    assert script_calls == [1], "the retry reused the persisted script — no second AI call"
    assert "script" not in (t.render_json or {}), "success consumed the checkpoint"


def _script():
    from core.ai_engine import VideoScript

    return VideoScript(
        language="en", topic="Robots", synopsis="Robots learn to dream",
        scenes=[{"index": i, "narration": "n", "pexels_keywords": ["k"]} for i in range(3)],
        metadata_variations=[{"variant": v, "title": f"T{v}", "description": "d",
                              "tags": ["a", "b", "c"]} for v in "ABC"],
    )


def test_a_reject_forgets_the_script_so_the_rerender_rerolls(session, user, channel):
    """Reject exists to get DIFFERENT content — rebuilding the same script would ignore the operator's
    own avoid-note."""
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, CampaignStatus, TaskStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=1,
                   status=CampaignStatus.active, config_json={})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
             status=TaskStatus.AWAITING_REVIEW, render_json={"script": {"x": 1}, "scenes": []})
    item = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                          status=BufferStatus.awaiting_review, video_path="/no/v.mp4",
                          metadata_json={})
    session.add_all([t, item])
    session.commit()

    video_worker.apply_reject(session, item, "too slow", rerender=False)
    session.refresh(t)
    assert "script" not in (t.render_json or {})
    assert (t.render_json or {}).get("scenes") == [], "only the checkpoint is dropped, not the rest"


def test_discard_and_rerender_forgets_the_script_too(session, user, channel, tmp_path):
    from starlette.testclient import TestClient

    import main
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, CampaignStatus, TaskStatus

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=1,
                   status=CampaignStatus.active, config_json={})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
             status=TaskStatus.AWAITING_REVIEW, render_json={"script": {"x": 1}})
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    item = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                          status=BufferStatus.awaiting_review, video_path=str(f), metadata_json={})
    session.add_all([t, item])
    session.commit()
    session.refresh(item)

    with TestClient(main.app) as client:
        r = client.post(f"/assets/{item.id}/rerender", follow_redirects=False)
    assert r.status_code == 303
    session.refresh(t)
    assert "script" not in (t.render_json or {})
    assert t.status == TaskStatus.PENDING_QUEUE


# ── Layer 3: the autopilot continues what was interrupted ─────────────────────
def _failed_task(session, user, channel, msg, retry_count=0):
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=3,
                   status=CampaignStatus.active, config_json={})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.FAILED,
             error_message=msg, retry_count=retry_count)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def test_autopilot_continues_a_timed_out_render(session, user, channel):
    """The reported failure ends with the machine fixing itself: vendor timeout → FAILED with a kept
    checkpoint → the next autopilot pass re-queues it, ghost progress cleared."""
    from database.types import TaskStatus
    from workers import scheduler, task_queue

    t = _failed_task(session, user, channel,
                     "image wait timeout: this episode's image-fetch budget is spent — the render "
                     "stops cleanly and resumes from the scenes already drawn.")
    task_queue.set_progress(t.id, 47)                       # ghost % from the dead attempt
    assert scheduler.autopilot_retry_channel(session, channel) == 1
    session.refresh(t)
    assert t.status == TaskStatus.PENDING_QUEUE and t.retry_count == 1
    assert task_queue.get_progress(t.id) in (None, 0)


def test_autopilot_does_not_retry_what_a_retry_cannot_fix(session, user, channel):
    """One classification with the bell/episode page (core.failure): a missing key or a safety block
    would fail identically forever — burning the retry cap on them strands the real fix."""
    from workers import scheduler

    _failed_task(session, user, channel,
                 "Missing Gemini API key (set per-user in the dashboard or in .env).")
    assert scheduler.autopilot_retry_channel(session, channel) == 0


def test_recently_failed_tasks_keep_their_checkpoints_from_the_sweeper(session, user, channel):
    """The orphan sweep (60 min) is faster than the autopilot cadence (hours) — without this skip,
    every autopilot 'resume' would silently be a from-scratch re-render."""
    from datetime import datetime, timedelta

    from workers import scheduler

    t = _failed_task(session, user, channel, "Read timed out")
    assert str(t.id) in scheduler.resume_checkpoint_ids(session)

    # An old failure nobody retried releases its disk.
    t.updated_at = datetime.utcnow() - timedelta(hours=scheduler.RESUME_KEEP_HOURS + 1)
    session.commit()
    assert str(t.id) not in scheduler.resume_checkpoint_ids(session)
