"""Worker: render lock, hydration, campaign state machine, full job, failure path."""
from __future__ import annotations

import pytest


def _script():
    from core.ai_engine import VideoScript

    return VideoScript(
        language="en", topic="Robots", synopsis="Robots learn to dream",
        scenes=[{"index": i, "narration": "n", "pexels_keywords": ["k"]} for i in range(3)],
        metadata_variations=[{"variant": v, "title": f"T{v}", "description": "d", "tags": ["a", "b", "c"]} for v in "ABC"],
    )


def _result():
    from core.video_factory import RenderResult

    return RenderResult(master_path="/no/m.mp4", thumbnail_path="/no/t.jpg",
                        metadata={"title": "TA", "variant": "A"}, duration=12.0, scene_count=3)


def test_render_lock_mutual_exclusion(session, user, channel):
    from workers import task_queue, video_worker

    task_queue.conn.set(task_queue.LOCK_KEY, "1")  # hold the lock
    with pytest.raises(RuntimeError, match="global lock"):
        video_worker.render_task(1234)
    task_queue.conn.delete(task_queue.LOCK_KEY)


def test_hydrate_idempotent(session, user, channel):
    from database.models import Campaign, Task
    from database.types import CampaignStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="A", total_episodes=5, status=CampaignStatus.active)
    session.add(cam)
    session.commit()

    created = video_worker.hydrate_buffers(session, buffer_size=2, enqueue=lambda t: f"j{t}")
    assert len(created) == 2
    assert sorted(t.episode_number for t in session.query(Task).all()) == [1, 2]
    assert video_worker.hydrate_buffers(session, buffer_size=2, enqueue=lambda t: "x") == []


def test_advance_campaign_and_autoactivate(session, user, channel):
    from database.models import Campaign
    from database.types import CampaignStatus
    from workers import video_worker

    # current_episode counts episodes published, starting at 0 (as in production). A 2-episode
    # campaign completes when the count REACHES 2 (>=), not 3 — otherwise it never completes.
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="A", total_episodes=2,
                   current_episode=0, status=CampaignStatus.active)
    nxt = Campaign(user_id=user.id, channel_id=channel.id, topic_name="B", total_episodes=2,
                   status=CampaignStatus.pending)
    session.add_all([cam, nxt])
    session.commit()

    assert not video_worker.advance_campaign(session, cam).completed  # -> 1, still active
    ev = video_worker.advance_campaign(session, cam)                  # -> 2 >= 2 => completed
    assert ev.completed and cam.status == CampaignStatus.completed
    assert ev.activated_campaign_id == nxt.id
    session.refresh(nxt)
    assert nxt.status == CampaignStatus.active


def test_render_task_full_flow_and_failure(session, user, channel, monkeypatch):
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, CampaignStatus, TaskStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Robots",
                   current_episode=0, total_episodes=3, status=CampaignStatus.active, config_json={"language": "en"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)

    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script())
    monkeypatch.setattr(video_worker.video_factory, "produce", lambda **k: _result())
    published = []
    monkeypatch.setattr(video_worker, "_publish",
                        lambda channel, video_path, metadata, user: published.append(1) or "vid-1")

    video_worker.render_task(t.id)
    session.refresh(t)
    session.refresh(cam)
    assert t.status == TaskStatus.COMPLETED and t.progress_pct == 100 and published
    # Transparency: published link + timing recorded on the task.
    assert t.published_video_id == "vid-1"
    assert t.published_url and "vid-1" in t.published_url
    assert t.started_at is not None and t.finished_at is not None
    assert t.ab_variant == "A"  # closed A/B loop: the variant that went live is recorded
    buf = session.query(BufferPoolItem).filter_by(campaign_id=cam.id, episode_number=1).one()
    assert buf.status == BufferStatus.consumed and cam.current_episode == 1

    # render_task self-hydrated the next episodes
    upcoming = [x.episode_number for x in session.query(Task).filter_by(campaign_id=cam.id).all()
                if x.status != TaskStatus.COMPLETED]
    assert sorted(upcoming) == [2, 3]

    # failure path on ep2
    t2 = session.query(Task).filter_by(campaign_id=cam.id, episode_number=2).one()

    def boom(*a, **k):
        raise RuntimeError("upload exploded")

    monkeypatch.setattr(video_worker, "_publish", boom)
    video_worker.render_task(t2.id)
    session.refresh(t2)
    assert t2.status == TaskStatus.FAILED and "upload exploded" in (t2.error_message or "")


def test_review_mode_awaits_then_publishes(session, user, channel, monkeypatch, tmp_path):
    """auto_publish=False parks the render for review; publish_task completes it after approval."""
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, CampaignStatus, TaskStatus
    from workers import video_worker

    video_file = tmp_path / "m.mp4"
    video_file.write_bytes(b"fake-video")

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Review Me",
                   current_episode=0, total_episodes=2, status=CampaignStatus.active,
                   config_json={"language": "en", "auto_publish": False})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)

    from core.video_factory import RenderResult

    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script())
    monkeypatch.setattr(
        video_worker.video_factory, "produce",
        lambda **k: RenderResult(master_path=str(video_file), thumbnail_path="/no/t.jpg",
                                 metadata={"title": "TA", "variant": "A"}, duration=10.0, scene_count=3),
    )
    published = []
    monkeypatch.setattr(video_worker, "_publish",
                        lambda channel, video_path, metadata, user: published.append(video_path) or "vid-9")

    # Render: must STOP at review, not publish.
    video_worker.render_task(t.id)
    session.refresh(t)
    session.refresh(cam)
    assert t.status == TaskStatus.AWAITING_REVIEW and not published
    assert cam.current_episode == 0  # not advanced until actually published
    buf = session.query(BufferPoolItem).filter_by(campaign_id=cam.id, episode_number=1).one()
    assert buf.status == BufferStatus.awaiting_review and video_file.exists()

    # Approval path: publish_task uploads, completes, advances.
    video_worker.publish_task(buf.id)
    session.refresh(t)
    session.refresh(buf)
    session.refresh(cam)
    assert published == [str(video_file)]
    assert t.status == TaskStatus.COMPLETED and t.published_video_id == "vid-9"
    assert buf.status == BufferStatus.consumed and cam.current_episode == 1
    assert not video_file.exists()  # cleaned up after publish


def test_slot_scheduled_mode_parks_ready(session, user, channel, monkeypatch):
    """Auto mode WITH posting slots renders into the buffer (SCHEDULED) — the scheduler publishes
    at slot time, so a full buffer can never dump all episodes at once (ADR-011)."""
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, CampaignStatus, TaskStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Daily", total_episodes=5,
                   status=CampaignStatus.active,
                   config_json={"language": "en", "auto_publish": True, "posting_slots": ["21:00"]})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)

    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script())
    monkeypatch.setattr(video_worker.video_factory, "produce", lambda **k: _result())
    monkeypatch.setattr(video_worker, "_publish",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not publish at render time")))

    video_worker.render_task(t.id)
    session.refresh(t)
    assert t.status == TaskStatus.SCHEDULED
    buf = session.query(BufferPoolItem).filter_by(campaign_id=cam.id, episode_number=1).one()
    assert buf.status == BufferStatus.ready


def test_episode_memory_flows_into_prompt(session, user, channel, monkeypatch):
    """The worker stores each episode's synopsis and feeds prior ones into the next generation."""
    from database.models import Campaign, Task
    from database.types import CampaignStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Horror", total_episodes=9,
                   status=CampaignStatus.active,
                   config_json={"language": "vi", "continuity": "no_repeat",
                                "persona": "Chú Ba miền Tây"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t1 = Task(campaign_id=cam.id, user_id=user.id, episode_number=1, synopsis="Con ma chợ nổi")
    t2 = Task(campaign_id=cam.id, user_id=user.id, episode_number=2, synopsis="Chiếc ghe không người lái")
    t3 = Task(campaign_id=cam.id, user_id=user.id, episode_number=3)
    session.add_all([t1, t2, t3])
    session.commit()
    session.refresh(t3)

    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        script = _script()
        script.synopsis = "Căn nhà cuối xóm có tiếng ru"
        return script

    monkeypatch.setattr(video_worker, "generate_script", fake_generate)
    monkeypatch.setattr(video_worker.video_factory, "produce", lambda **k: _result())
    monkeypatch.setattr(video_worker, "_publish", lambda *a, **k: "vid-3")

    video_worker.render_task(t3.id)
    # Prior synopses reached the generator, in episode order, with persona + continuity mode.
    assert captured["previous_synopses"] == ["Con ma chợ nổi", "Chiếc ghe không người lái"]
    assert captured["continuity"] == "no_repeat"
    assert captured["persona"] == "Chú Ba miền Tây"
    # And this episode's synopsis was stored for the NEXT one.
    session.refresh(t3)
    assert t3.synopsis == "Căn nhà cuối xóm có tiếng ru"


def test_resolve_music_config_truth(monkeypatch):
    """music_mode=auto without a FREESOUND_API_KEY is a deterministic misconfiguration → the
    episode fails LOUDLY (like a missing music file) instead of silently publishing without
    music. A transient Freesound failure still degrades to no music."""
    import pytest as _pytest

    from core.config import settings
    from services import music_service
    from workers import video_worker

    monkeypatch.setattr(settings, "FREESOUND_API_KEY", None)
    with _pytest.raises(RuntimeError, match="FREESOUND_API_KEY"):
        video_worker._resolve_music({"music_mode": "auto"})
    # Other modes are unaffected by the missing key.
    assert video_worker._resolve_music({}) == (None, None)
    assert video_worker._resolve_music({"music_mode": "file", "music_path": "/x.mp3"}) == ("/x.mp3", None)

    # Key present but Freesound yields nothing (down / no results) → transient → degrade.
    monkeypatch.setattr(settings, "FREESOUND_API_KEY", "fs-key")
    monkeypatch.setattr(music_service, "pick_music", lambda mood, key, cache: None)
    assert video_worker._resolve_music({"music_mode": "auto"}) == (None, None)


def test_synopsis_falls_back_to_title(session, user, channel, monkeypatch):
    """Episode memory must never be empty: a script whose synopsis slipped through blank still
    leaves the variant-A title as memory, so continuity never silently skips an episode."""
    from database.models import Campaign, Task
    from database.types import CampaignStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="M", total_episodes=3,
                   status=CampaignStatus.active, config_json={"language": "en"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)

    script = _script()
    script.synopsis = ""  # simulate a blank slipping past generation
    monkeypatch.setattr(video_worker, "generate_script", lambda **k: script)
    monkeypatch.setattr(video_worker.video_factory, "produce", lambda **k: _result())
    monkeypatch.setattr(video_worker, "_publish", lambda *a, **k: "vid-m")

    video_worker.render_task(t.id)
    session.refresh(t)
    assert t.synopsis == "TA"  # variant-A title stored as the fallback memory


def test_auto_music_flows_into_render(session, user, channel, monkeypatch, tmp_path):
    """music_mode=auto resolves a CC0 track and the credit lands in the episode metadata."""
    from core.config import settings
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import CampaignStatus
    from services import music_service
    from workers import video_worker

    monkeypatch.setattr(settings, "FREESOUND_API_KEY", "fs-key")
    track = tmp_path / "freesound_101.mp3"
    track.write_bytes(b"mp3")
    credit = {"source": "freesound", "id": 101, "title": "Dark Drone", "author": "artistA", "license": "CC0"}
    monkeypatch.setattr(music_service, "pick_music", lambda mood, key, cache: (str(track), credit))

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Horror", total_episodes=3,
                   status=CampaignStatus.active,
                   config_json={"language": "vi", "auto_publish": False,
                                "music_mode": "auto", "music_mood": "dark ambient"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)

    captured = {}

    def fake_produce(**kwargs):
        captured.update(kwargs)
        return _result()

    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script())
    monkeypatch.setattr(video_worker.video_factory, "produce", fake_produce)

    video_worker.render_task(t.id)
    assert captured["music_path"] == str(track)  # the picked CC0 file reached the renderer
    buf = session.query(BufferPoolItem).filter_by(campaign_id=cam.id, episode_number=1).one()
    assert buf.metadata_json["music_credit"]["title"] == "Dark Drone"  # per-episode transparency


def _qc_campaign(session, user, channel, **cfg_extra):
    from database.models import Campaign, Task
    from database.types import CampaignStatus

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="QC", total_episodes=3,
                   status=CampaignStatus.active, config_json={"language": "en", **cfg_extra})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)
    return cam, t


def test_auto_qc_pass_publishes_with_verdict(session, user, channel, monkeypatch):
    from core import qc
    from database.models import BufferPoolItem
    from database.types import TaskStatus
    from workers import video_worker

    cam, t = _qc_campaign(session, user, channel)
    produce_calls = []
    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script())
    monkeypatch.setattr(video_worker.video_factory, "produce",
                        lambda **k: produce_calls.append(k) or _result())
    monkeypatch.setattr(qc, "run_final_qc",
                        lambda path, *, api_key, context="": qc.QCResult(passed=True, score=9))
    monkeypatch.setattr(video_worker, "_publish", lambda *a, **k: "vid-qc")

    video_worker.render_task(t.id)
    session.refresh(t)
    assert t.status == TaskStatus.COMPLETED
    assert len(produce_calls) == 1 and produce_calls[0]["vet_batch"] is not None  # batch vetter wired in
    buf = session.query(BufferPoolItem).filter_by(campaign_id=cam.id, episode_number=1).one()
    assert buf.metadata_json["qc"] == {"passed": True, "score": 9, "issues": [], "attempts": 1}


def test_auto_qc_double_failure_parks_for_review(session, user, channel, monkeypatch):
    """QC fail → one re-render; fail again → park for human review, never publish (ADR-013)."""
    from core import qc
    from database.models import BufferPoolItem
    from database.types import BufferStatus, TaskStatus
    from workers import video_worker

    cam, t = _qc_campaign(session, user, channel, auto_publish=True)
    produce_calls = []
    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script())
    monkeypatch.setattr(video_worker.video_factory, "produce",
                        lambda **k: produce_calls.append(k) or _result())
    monkeypatch.setattr(qc, "run_final_qc",
                        lambda path, *, api_key, context="": qc.QCResult(
                            passed=False, score=3, issues=["captions clipped"]))
    monkeypatch.setattr(video_worker, "_publish",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not publish")))

    video_worker.render_task(t.id)
    session.refresh(t)
    assert len(produce_calls) == 2  # exactly one automatic re-render
    assert t.status == TaskStatus.AWAITING_REVIEW  # human review is the backstop
    buf = session.query(BufferPoolItem).filter_by(campaign_id=cam.id, episode_number=1).one()
    assert buf.status == BufferStatus.awaiting_review
    assert buf.metadata_json["qc"] == {"passed": False, "score": 3,
                                       "issues": ["captions clipped"], "attempts": 2}


def test_auto_qc_off_skips_gate(session, user, channel, monkeypatch):
    from core import qc
    from database.types import TaskStatus
    from workers import video_worker

    cam, t = _qc_campaign(session, user, channel, auto_qc="off")
    produce_calls = []
    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script())
    monkeypatch.setattr(video_worker.video_factory, "produce",
                        lambda **k: produce_calls.append(k) or _result())
    monkeypatch.setattr(qc, "run_final_qc",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("QC must not run")))
    monkeypatch.setattr(video_worker, "_publish", lambda *a, **k: "vid-x")

    video_worker.render_task(t.id)
    session.refresh(t)
    assert t.status == TaskStatus.COMPLETED
    assert produce_calls[0]["vet_batch"] is None  # no vision vetting when the gate is off


def test_rerender_replaces_existing_buffer(session, user, channel, monkeypatch, tmp_path):
    """Re-render (Retry after a reject) must REPLACE the prior buffer row for the episode, not
    collide on the (campaign, episode) unique constraint — otherwise Retry loops FAILED forever."""
    from core.video_factory import RenderResult
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, CampaignStatus, TaskStatus
    from workers import video_worker

    old_file = tmp_path / "old.mp4"
    old_file.write_bytes(b"old")
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="R", total_episodes=3,
                   status=CampaignStatus.active,
                   config_json={"language": "en", "auto_publish": False, "auto_qc": "off"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    # A leftover rejected buffer row for this episode, as the reject route leaves behind.
    stale = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                           video_path=str(old_file), status=BufferStatus.rejected, metadata_json={})
    session.add_all([t, stale])
    session.commit()
    session.refresh(t)

    new_file = tmp_path / "new.mp4"
    new_file.write_bytes(b"new")
    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script())
    monkeypatch.setattr(
        video_worker.video_factory, "produce",
        lambda **k: RenderResult(master_path=str(new_file), thumbnail_path="/no/t.jpg",
                                 metadata={"title": "T", "variant": "A"}, duration=5.0, scene_count=1),
    )

    video_worker.render_task(t.id)  # must NOT raise IntegrityError on the unique constraint

    # Drop cached objects: SQLite reuses the deleted row's rowid, so the replacement buffer can take
    # the stale row's PK — expire so the query re-reads the row's real (new) attributes from disk.
    session.expire_all()
    session.refresh(t)
    assert t.status == TaskStatus.AWAITING_REVIEW
    bufs = session.query(BufferPoolItem).filter_by(campaign_id=cam.id, episode_number=1).all()
    assert len(bufs) == 1 and bufs[0].video_path == str(new_file)  # replaced, not duplicated
    assert not old_file.exists()  # the superseded render's file was cleaned up


def test_rerender_same_path_keeps_fresh_file(session, user, channel, monkeypatch, tmp_path):
    """Renders write to a deterministic per-episode path, so on a re-render the OLD buffer row
    points at the SAME path as the NEW file. The replacement cleanup must not delete it —
    this shipped once and produced Ready cards whose video had been silently destroyed."""
    from core.video_factory import RenderResult
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, CampaignStatus, TaskStatus
    from workers import video_worker

    shared = tmp_path / "episode_1.mp4"  # the deterministic path both rows share
    shared.write_bytes(b"fresh-render")
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="SamePath", total_episodes=3,
                   status=CampaignStatus.active,
                   config_json={"language": "en", "auto_publish": False, "auto_qc": "off"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    stale = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                           video_path=str(shared), status=BufferStatus.rejected, metadata_json={})
    session.add_all([t, stale])
    session.commit()
    session.refresh(t)

    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script())
    monkeypatch.setattr(
        video_worker.video_factory, "produce",
        lambda **k: RenderResult(master_path=str(shared), thumbnail_path="/no/t.jpg",
                                 metadata={"title": "T", "variant": "A"}, duration=5.0, scene_count=1),
    )

    video_worker.render_task(t.id)

    session.expire_all()
    session.refresh(t)
    assert t.status == TaskStatus.AWAITING_REVIEW
    assert shared.exists() and shared.read_bytes() == b"fresh-render"  # NOT deleted by the cleanup
    bufs = session.query(BufferPoolItem).filter_by(campaign_id=cam.id, episode_number=1).all()
    assert len(bufs) == 1 and bufs[0].video_path == str(shared)


def test_publish_task_idempotent_on_consumed(session, user, channel, monkeypatch):
    """A double-enqueued publish (slot re-tick or double-clicked Approve) must not upload twice."""
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, CampaignStatus, TaskStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="P", total_episodes=3,
                   current_episode=1, status=CampaignStatus.active, config_json={"language": "en"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    task = Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.COMPLETED)
    buf = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                         video_path="/gone.mp4", status=BufferStatus.consumed, metadata_json={})
    session.add_all([task, buf])
    session.commit()
    session.refresh(buf)

    published = []
    monkeypatch.setattr(video_worker, "_publish", lambda *a, **k: published.append(1) or "vid")
    video_worker.publish_task(buf.id)  # buffer already consumed → guard bails
    assert published == []  # no second upload


def test_hydrate_respects_max_per_day(session, user, channel):
    """max_per_day caps how many renders a campaign may START per (local) day — the Gemini-quota
    rationing knob for running several campaigns/accounts side by side."""
    from database.models import Campaign, Task
    from database.types import CampaignStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Capped", total_episodes=10,
                   status=CampaignStatus.active,
                   config_json={"buffer_size": 5, "max_per_day": 2})
    session.add(cam)
    session.commit()

    created = video_worker.hydrate_buffers(session, enqueue=lambda t: f"j{t}")
    assert len(created) == 2  # cap wins over the buffer size (5)
    # Re-hydrating the same day creates nothing more — today's budget is spent.
    assert video_worker.hydrate_buffers(session, enqueue=lambda t: "x") == []
    assert session.query(Task).count() == 2


def test_min_per_day_watchdog_alerts(session, user, channel, monkeypatch):
    """A campaign below its min_per_day in the last 24h triggers one Telegram alert; a campaign
    meeting its minimum stays silent."""
    from datetime import datetime, timedelta

    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from workers import scheduler as sch
    from workers import video_worker

    now = datetime.utcnow()
    behind = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Behind", total_episodes=9,
                      status=CampaignStatus.active, config_json={"min_per_day": 2})
    ok = Campaign(user_id=user.id, channel_id=channel.id, topic_name="OnTrack", total_episodes=9,
                  status=CampaignStatus.active, config_json={"min_per_day": 1})
    session.add_all([behind, ok])
    session.commit()
    session.refresh(behind)
    session.refresh(ok)
    session.add_all([
        Task(campaign_id=behind.id, user_id=user.id, episode_number=1,
             status=TaskStatus.COMPLETED, finished_at=now - timedelta(hours=3)),   # 1 of 2 → behind
        Task(campaign_id=ok.id, user_id=user.id, episode_number=1,
             status=TaskStatus.COMPLETED, finished_at=now - timedelta(hours=3)),   # 1 of 1 → fine
    ])
    session.commit()

    alerts = []
    monkeypatch.setattr(video_worker, "_notify", lambda u, msg: alerts.append(msg))
    assert sch.check_daily_minimums(session, now=now) == 1
    assert len(alerts) == 1 and "1/2" in alerts[0] and "Behind" in alerts[0]


def test_daily_heartbeat_digest(session, user, channel, monkeypatch):
    """Operators with an active campaign get one daily Telegram digest of the last 24h."""
    from datetime import datetime, timedelta

    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from workers import scheduler as sch
    from workers import video_worker

    now = datetime.utcnow()
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="HB", total_episodes=9,
                   status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)
    session.add_all([
        Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
             status=TaskStatus.COMPLETED, finished_at=now - timedelta(hours=2)),
        Task(campaign_id=cam.id, user_id=user.id, episode_number=2,
             status=TaskStatus.FAILED, finished_at=now - timedelta(hours=1)),
        Task(campaign_id=cam.id, user_id=user.id, episode_number=3,
             status=TaskStatus.AWAITING_REVIEW),
    ])
    session.commit()

    digests = []
    monkeypatch.setattr(video_worker, "_notify", lambda u, msg: digests.append(msg))
    assert sch.send_daily_heartbeat(session, now=now) == 1
    assert len(digests) == 1
    assert "published 1" in digests[0] and "failed 1" in digests[0] and "awaiting review 1" in digests[0]
    assert "AI calls today" in digests[0]


def test_circuit_breaker_pauses_after_three_consecutive_failures(session, user, channel, monkeypatch):
    """3 consecutive failed episodes trip the breaker: the campaign stops (status `failed`, which
    hydration/publishing skip and the ▶ Start button resumes) and the operator is alerted ONCE."""
    from datetime import datetime, timedelta

    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Fragile", total_episodes=9,
                   status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)
    now = datetime.utcnow()
    session.add_all([
        Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.FAILED,
             finished_at=now - timedelta(minutes=30)),
        Task(campaign_id=cam.id, user_id=user.id, episode_number=2, status=TaskStatus.FAILED,
             finished_at=now - timedelta(minutes=20)),
    ])
    t3 = Task(campaign_id=cam.id, user_id=user.id, episode_number=3)
    session.add(t3)
    session.commit()
    session.refresh(t3)

    alerts = []
    monkeypatch.setattr(video_worker, "_notify", lambda u, msg: alerts.append(msg))
    video_worker._fail_task(session, t3, user, cam, RuntimeError("boom"), "render_task")

    session.refresh(cam)
    assert cam.status == CampaignStatus.failed  # tripped: no new renders will start
    assert any("3 consecutive failures" in m for m in alerts)

    # A further failure on the already-paused campaign must not re-alert the breaker.
    t4 = Task(campaign_id=cam.id, user_id=user.id, episode_number=4)
    session.add(t4)
    session.commit()
    session.refresh(t4)
    alerts.clear()
    video_worker._fail_task(session, t4, user, cam, RuntimeError("boom"), "render_task")
    assert not any("consecutive failures" in m for m in alerts)


def test_circuit_breaker_success_resets_streak(session, user, channel, monkeypatch):
    """Any non-failed outcome (publish, parked review, scheduled render) between failures proves
    the pipeline works — the streak restarts from zero and the campaign stays active."""
    from datetime import datetime, timedelta

    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Wobbly", total_episodes=9,
                   status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)
    now = datetime.utcnow()
    session.add_all([
        Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.FAILED,
             finished_at=now - timedelta(minutes=40)),
        Task(campaign_id=cam.id, user_id=user.id, episode_number=2, status=TaskStatus.FAILED,
             finished_at=now - timedelta(minutes=30)),
        Task(campaign_id=cam.id, user_id=user.id, episode_number=3, status=TaskStatus.COMPLETED,
             finished_at=now - timedelta(minutes=20)),  # success breaks the streak
    ])
    t4 = Task(campaign_id=cam.id, user_id=user.id, episode_number=4)
    session.add(t4)
    session.commit()
    session.refresh(t4)

    monkeypatch.setattr(video_worker, "_notify", lambda u, msg: None)
    video_worker._fail_task(session, t4, user, cam, RuntimeError("boom"), "render_task")
    session.refresh(cam)
    assert cam.status == CampaignStatus.active  # streak is 1, not 3 — breaker stays closed
    assert video_worker.consecutive_failures(session, cam) == 1


def test_hydrate_respects_campaign_buffer_size(session, user, channel):
    from database.models import Campaign, Task
    from database.types import CampaignStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="A", total_episodes=10,
                   status=CampaignStatus.active, config_json={"buffer_size": 5})
    session.add(cam)
    session.commit()

    created = video_worker.hydrate_buffers(session, enqueue=lambda t: f"j{t}")
    assert len(created) == 5  # per-campaign size wins over the global default (3)
    assert sorted(t.episode_number for t in session.query(Task).all()) == [1, 2, 3, 4, 5]
