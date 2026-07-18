"""Worker: render lock, hydration, campaign state machine, full job, failure path."""
from __future__ import annotations

import pytest


def _script():
    from core.ai_engine import VideoScript

    return VideoScript(
        language="en", topic="Robots",
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

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="A", total_episodes=2,
                   current_episode=1, status=CampaignStatus.active)
    nxt = Campaign(user_id=user.id, channel_id=channel.id, topic_name="B", total_episodes=2,
                   status=CampaignStatus.pending)
    session.add_all([cam, nxt])
    session.commit()

    assert not video_worker.advance_campaign(session, cam).completed  # -> 2, still active
    ev = video_worker.advance_campaign(session, cam)                  # -> 3 > 2 => completed
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
