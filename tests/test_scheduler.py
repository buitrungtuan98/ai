"""Scheduler: posting-slot gating, slot-gated hydration, buffer expiry."""
from __future__ import annotations

import os
from datetime import datetime


def test_is_within_slot():
    from workers import scheduler as sch

    assert sch.is_within_slot([], datetime(2026, 7, 17, 3, 0)) is True
    assert sch.is_within_slot(["09:00"], datetime(2026, 7, 17, 9, 15), 30) is True
    assert sch.is_within_slot(["09:00"], datetime(2026, 7, 17, 12, 0), 30) is False
    assert sch.is_within_slot(["23:50"], datetime(2026, 7, 17, 0, 5), 30) is True  # midnight wrap


def test_local_now_uses_configured_timezone(monkeypatch):
    from core.config import settings
    from workers import scheduler as sch

    monkeypatch.setattr(settings, "TIMEZONE", "Asia/Ho_Chi_Minh")
    now = sch.local_now()
    assert now.tzinfo is not None and now.utcoffset().total_seconds() == 7 * 3600  # ICT = UTC+7

    # An invalid timezone degrades to UTC instead of killing the scheduler.
    monkeypatch.setattr(settings, "TIMEZONE", "Not/AZone")
    assert sch.local_now() is not None


def test_periodic_tick_renders_eagerly_for_all(session, user, channel):
    """Hydration is no longer slot-gated — buffers fill ahead so slot publishing is instant."""
    from database.models import Campaign, Task
    from database.types import CampaignStatus
    from workers import scheduler as sch, task_queue

    task_queue.enqueue_render = lambda tid: f"j{tid}"
    slotted = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Slotted", total_episodes=5,
                       status=CampaignStatus.active, config_json={"posting_slots": ["09:00"]})
    always = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Always", total_episodes=5,
                      status=CampaignStatus.active, config_json={})
    session.add_all([slotted, always])
    session.commit()

    sch.periodic_tick(db=session, now=datetime(2026, 7, 17, 15, 0))  # far from any slot
    assert session.query(Task).filter_by(campaign_id=slotted.id).count() > 0
    assert session.query(Task).filter_by(campaign_id=always.id).count() > 0


def _ready_item(session, campaign, channel, episode, path="/no/v.mp4"):
    from database.models import BufferPoolItem
    from database.types import BufferStatus

    item = BufferPoolItem(campaign_id=campaign.id, channel_id=channel.id, episode_number=episode,
                          video_path=path, status=BufferStatus.ready, metadata_json={"title": "T"})
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_publish_due_campaign_one_per_slot(session, user, channel):
    """Exactly one ready episode publishes per slot; outside the slot nothing publishes; a recent
    publish blocks double-posting within the same slot window."""
    from datetime import timedelta
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from workers import scheduler as sch

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Daily", total_episodes=10,
                   status=CampaignStatus.active,
                   config_json={"posting_slots": ["21:00"], "auto_publish": True})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    first = _ready_item(session, cam, channel, 1)
    _ready_item(session, cam, channel, 2)

    queued = []
    enq = lambda bid: queued.append(bid)  # noqa: E731

    # Outside the slot → nothing.
    assert sch.publish_due_campaign(session, cam, now=datetime(2026, 7, 17, 12, 0), enqueue=enq) is None
    # Within the slot → exactly the OLDEST ready episode, once.
    got = sch.publish_due_campaign(session, cam, now=datetime(2026, 7, 17, 21, 5), enqueue=enq)
    assert got == first.id and queued == [first.id]

    # Simulate that episode published moments ago → guard blocks a second post in the same window.
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.COMPLETED,
             finished_at=datetime.utcnow() - timedelta(minutes=5))
    session.add(t)
    session.commit()
    assert sch.publish_due_campaign(session, cam, now=datetime(2026, 7, 17, 21, 20), enqueue=enq) is None
    assert queued == [first.id]


def test_publish_due_campaign_skips_continuous_and_review(session, user, channel):
    from database.models import Campaign
    from database.types import CampaignStatus
    from workers import scheduler as sch

    continuous = Campaign(user_id=user.id, channel_id=channel.id, topic_name="C", total_episodes=5,
                          status=CampaignStatus.active, config_json={})  # no slots
    review = Campaign(user_id=user.id, channel_id=channel.id, topic_name="R", total_episodes=5,
                      status=CampaignStatus.active,
                      config_json={"posting_slots": ["21:00"], "auto_publish": False})
    session.add_all([continuous, review])
    session.commit()
    _ready_item(session, review, channel, 1)

    enq = lambda bid: (_ for _ in ()).throw(AssertionError("must not publish"))  # noqa: E731
    assert sch.publish_due_campaign(session, continuous, now=datetime(2026, 7, 17, 21, 0), enqueue=enq) is None
    assert sch.publish_due_campaign(session, review, now=datetime(2026, 7, 17, 21, 0), enqueue=enq) is None


def test_expire_stale_buffers(session, user, channel, tmp_path):
    from database.models import BufferPoolItem, Campaign
    from database.types import BufferStatus, CampaignStatus
    from workers import scheduler as sch

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="A", total_episodes=3, status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)

    vp = str(tmp_path / "old.mp4")
    open(vp, "w").write("x")
    item = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                          video_path=vp, status=BufferStatus.ready)
    session.add(item)
    session.commit()
    session.refresh(item)
    item.created_at = datetime(2000, 1, 1)
    session.commit()

    n = sch.expire_stale_buffers(session, now=datetime(2026, 7, 17, 10, 0))
    session.refresh(item)
    assert n == 1 and item.status == BufferStatus.expired and not os.path.exists(vp)
