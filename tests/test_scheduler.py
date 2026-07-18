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


def test_periodic_tick_slot_gating(session, user, channel):
    from database.models import Campaign, Task
    from database.types import CampaignStatus
    from workers import scheduler as sch, task_queue

    task_queue.enqueue_render = lambda tid: f"j{tid}"
    within = Campaign(user_id=user.id, channel_id=channel.id, topic_name="In", total_episodes=5,
                      status=CampaignStatus.active, config_json={"posting_slots": ["09:00"]})
    always = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Always", total_episodes=5,
                      status=CampaignStatus.active, config_json={})
    outside = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Out", total_episodes=5,
                       status=CampaignStatus.active, config_json={"posting_slots": ["09:00"]})
    session.add_all([within, always])
    session.commit()

    sch.periodic_tick(db=session, now=datetime(2026, 7, 17, 9, 10))
    assert session.query(Task).filter_by(campaign_id=within.id).count() > 0
    assert session.query(Task).filter_by(campaign_id=always.id).count() > 0

    session.add(outside)
    session.commit()
    sch.periodic_tick(db=session, now=datetime(2026, 7, 17, 15, 0))
    assert session.query(Task).filter_by(campaign_id=outside.id).count() == 0


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
