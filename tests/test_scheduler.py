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


def test_reap_stuck_tasks(session, user, channel):
    from datetime import timedelta
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from workers import scheduler as sch

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="A", total_episodes=9,
                   status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)
    now = datetime.utcnow()
    dead = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                status=TaskStatus.RENDERING, updated_at=now - timedelta(hours=3))
    alive = Task(campaign_id=cam.id, user_id=user.id, episode_number=2,
                 status=TaskStatus.RENDERING, updated_at=now - timedelta(minutes=5))
    # A PENDING_QUEUE task stuck far past any real backlog (3× job timeout ≈ 2.25h) is dead-lettered
    # (e.g. its job failed to acquire a stale lock) — reap it so Retry works.
    queued_stuck = Task(campaign_id=cam.id, user_id=user.id, episode_number=3,
                        status=TaskStatus.PENDING_QUEUE, updated_at=now - timedelta(hours=6))
    # A recently-queued task waiting behind a legitimate backlog is left alone.
    queued_recent = Task(campaign_id=cam.id, user_id=user.id, episode_number=4,
                         status=TaskStatus.PENDING_QUEUE, updated_at=now - timedelta(minutes=10))
    session.add_all([dead, alive, queued_stuck, queued_recent])
    session.commit()

    assert sch.reap_stuck_tasks(session, now=now) == 2
    for t in (dead, alive, queued_stuck, queued_recent):
        session.refresh(t)
    assert dead.status == TaskStatus.FAILED and "Retry" in dead.error_message
    assert queued_stuck.status == TaskStatus.FAILED   # long-stranded queue entry → recoverable
    assert alive.status == TaskStatus.RENDERING       # recent progress → untouched
    assert queued_recent.status == TaskStatus.PENDING_QUEUE  # legitimate backlog → untouched


def test_collect_stats_eligibility(session, user, channel, monkeypatch):
    from datetime import timedelta
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from services import analytics_service as an

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="A", total_episodes=9,
                   status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)
    now = datetime.utcnow()
    old = Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.COMPLETED,
               published_video_id="vidA", finished_at=now - timedelta(days=4))
    fresh = Task(campaign_id=cam.id, user_id=user.id, episode_number=2, status=TaskStatus.COMPLETED,
                 published_video_id="vidB", finished_at=now - timedelta(hours=3))  # too new
    session.add_all([old, fresh])
    session.commit()

    monkeypatch.setattr(an, "fetch_youtube_stats",
                        lambda ch, ids: {"vidA": {"views": 1200, "likes": 80, "avg_pct_viewed": 71.5}})
    assert an.collect_stats(session, now=now) == 1
    session.refresh(old)
    session.refresh(fresh)
    assert old.stats_json["views"] == 1200 and old.stats_json["avg_pct_viewed"] == 71.5
    assert fresh.stats_json is None  # 48h minimum age respected
    # Fetched <24h ago → not refetched.
    assert an.collect_stats(session, now=now) == 0


def test_collect_stats_stores_retention_drop(session, user, channel, monkeypatch):
    """When a curve is available and the task has its scene map, collect_stats attributes the biggest
    drop-off to a scene and stores the summary alongside the base stats."""
    from datetime import timedelta

    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from services import analytics_service as an

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="R", total_episodes=9,
                   status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)
    now = datetime.utcnow()
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.COMPLETED,
             published_video_id="vidR", finished_at=now - timedelta(days=3),
             render_json={"scenes": [{"index": 0, "start": 0.0, "end": 4.0, "dur": 4.0, "label": "intro"},
                                     {"index": 1, "start": 4.0, "end": 10.0, "dur": 6.0, "label": "the twist"}],
                          "duration": 10.0})
    session.add(t)
    session.commit()

    monkeypatch.setattr(an, "fetch_youtube_stats",
                        lambda ch, ids: {"vidR": {"views": 500, "likes": 20, "avg_pct_viewed": 55.0}})
    monkeypatch.setattr(an, "fetch_youtube_geography", lambda ch, ids: {})
    # Curve holds, then falls hard at 40% (= 4.0s → start of "the twist").
    monkeypatch.setattr(an, "fetch_youtube_retention",
                        lambda ch, ids: {"vidR": [[0.0, 1.0], [0.4, 0.7], [1.0, 0.6]]})
    assert an.collect_stats(session, now=now) == 1
    session.refresh(t)
    assert t.stats_json["retention_curve"] == [[0.0, 1.0], [0.4, 0.7], [1.0, 0.6]]
    assert "the twist" in t.stats_json["drop_summary"] and "0:04" in t.stats_json["drop_summary"]


def test_maybe_distill_guards_and_updates(session, user, channel, monkeypatch):
    from datetime import timedelta
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from workers import scheduler as sch
    import core.ai_engine as ai

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="A", total_episodes=30,
                   status=CampaignStatus.active,
                   learning_json={"reject_reasons": ["too slow"]})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    now = datetime.utcnow()

    # Guard: fewer than 5 measured episodes → no distillation.
    assert sch.maybe_distill_campaign(session, cam, now=now) is False

    for ep in range(1, 6):
        stats = {"views": 100 * ep, "avg_pct_viewed": 50 + ep, "likes": ep,
                 "fetched_at": now.isoformat()}
        if ep == 3:  # one episode carries a retention drop finding
            stats["drop_summary"] = "Biggest drop-off at 0:12 (scene 3 — “the twist”)"
        session.add(Task(campaign_id=cam.id, user_id=user.id, episode_number=ep,
                         status=TaskStatus.COMPLETED, synopsis=f"story {ep}", stats_json=stats))
    session.commit()

    # maybe_distill_campaign imports distill_playbook at call time, so patch it at its source.
    captured = {}

    def fake_distill(**k):
        captured.update(k)
        return ai.PlaybookUpdate(playbook=["Open with a question"], best_examples=["story 5"])

    monkeypatch.setattr(ai, "distill_playbook", fake_distill)

    assert sch.maybe_distill_campaign(session, cam, now=now) is True
    assert captured["drop_notes"] == ["Ep 3: Biggest drop-off at 0:12 (scene 3 — “the twist”)"]
    session.refresh(cam)
    assert cam.learning_json["playbook"] == ["Open with a question"]
    assert cam.learning_json["best_examples"] == ["story 5"]
    assert cam.learning_json["reject_reasons"] == ["too slow"]  # operator notes preserved
    # Guard: distilled recently → skip until DISTILL_EVERY_DAYS passes.
    assert sch.maybe_distill_campaign(session, cam, now=now + timedelta(days=1)) is False
    assert sch.maybe_distill_campaign(session, cam, now=now + timedelta(days=8)) is True


def test_posting_days_gate(session, user, channel, monkeypatch):
    """Weekday-gated campaigns publish only on their configured days (campaign timezone)."""
    from database.models import BufferPoolItem, Campaign
    from database.types import BufferStatus, CampaignStatus
    from workers import scheduler as sch

    now = datetime(2026, 7, 21, 21, 0)  # a Tuesday
    today = sch.WEEKDAY_KEYS[now.weekday()]
    other = sch.WEEKDAY_KEYS[(now.weekday() + 1) % 7]
    assert sch.is_posting_day([], now) is True            # empty = every day
    assert sch.is_posting_day([today], now) is True
    assert sch.is_posting_day([other], now) is False

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="D", total_episodes=5,
                   status=CampaignStatus.active,
                   config_json={"auto_publish": True, "posting_slots": ["21:00"],
                                "posting_days": [other]})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    buf = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                         video_path="/x.mp4", status=BufferStatus.ready)
    session.add(buf)
    session.commit()

    queued = []
    # Wrong day → slot matches but nothing publishes.
    assert sch.publish_due_campaign(session, cam, now=now, enqueue=queued.append) is None
    assert queued == []
    # Right day → publishes.
    cam.config_json = {**cam.config_json, "posting_days": [today]}
    session.commit()
    assert sch.publish_due_campaign(session, cam, now=now, enqueue=queued.append) == buf.id
    assert queued == [buf.id]


def test_expiry_stretched_for_day_gated_campaigns(session, user, channel, tmp_path):
    """A day-gated campaign's pre-render may wait most of a week — 4 days old must NOT expire
    (default 72h would have destroyed it before its publish day)."""
    from datetime import timedelta

    from database.models import BufferPoolItem, Campaign
    from database.types import BufferStatus, CampaignStatus
    from workers import scheduler as sch

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="W", total_episodes=3,
                   status=CampaignStatus.active,
                   config_json={"posting_slots": ["21:00"], "posting_days": ["mon"]})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    vp = tmp_path / "gated.mp4"
    vp.write_bytes(b"x")
    item = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                          video_path=str(vp), status=BufferStatus.ready)
    session.add(item)
    session.commit()
    session.refresh(item)
    now = datetime(2026, 7, 21, 10, 0)
    item.created_at = now - timedelta(days=4)     # past 72h, within the stretched week
    session.commit()

    assert sch.expire_stale_buffers(session, now=now) == 0
    session.refresh(item)
    assert item.status == BufferStatus.ready and vp.exists()

    item.created_at = now - timedelta(days=9)     # beyond even the stretched window
    session.commit()
    assert sch.expire_stale_buffers(session, now=now) == 1


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


def test_expire_recovers_scheduled_task(session, user, channel, tmp_path):
    """A slot-scheduled task whose pre-rendered buffer expires must not strand in SCHEDULED — it is
    failed so Retry can re-render it (otherwise no reaper/retry/publish path ever reaches it)."""
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, CampaignStatus, TaskStatus
    from workers import scheduler as sch

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="A", total_episodes=3,
                   status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)
    vp = str(tmp_path / "sched.mp4")
    open(vp, "w").write("x")
    item = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                          video_path=vp, status=BufferStatus.ready)
    task = Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.SCHEDULED)
    session.add_all([item, task])
    session.commit()
    session.refresh(item)
    item.created_at = datetime(2000, 1, 1)
    session.commit()

    sch.expire_stale_buffers(session, now=datetime(2026, 7, 17, 10, 0))
    session.refresh(task)
    assert task.status == TaskStatus.FAILED and "Retry" in task.error_message
