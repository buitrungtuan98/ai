"""Autopilot decision-engine tests. Phase I: campaign classification vs the channel baseline.

Pure-engine tests: seed a User/Channel/Campaigns/Tasks directly (the autouse `fresh_env` fixture
gives each test a clean schema) — no HTTP client needed.
"""


def _seed(rets_by_name):
    """Create a user + channel + one active campaign per {name: [retentions]}. Returns (db, ch, ids)."""
    from database.db_session import SessionLocal
    from database.models import Campaign, Channel, Task, User
    from database.types import CampaignStatus, Platform, TaskStatus

    db = SessionLocal()
    user = User()
    db.add(user)
    db.commit()
    db.refresh(user)
    ch = Channel(user_id=user.id, platform=Platform.youtube, channel_name="C")
    db.add(ch)
    db.commit()
    db.refresh(ch)
    ids = {}
    for name, rets in rets_by_name.items():
        c = Campaign(user_id=user.id, channel_id=ch.id, topic_name=name, total_episodes=20,
                     status=CampaignStatus.active)
        db.add(c)
        db.commit()
        db.refresh(c)
        for i, r in enumerate(rets, 1):
            db.add(Task(campaign_id=c.id, user_id=user.id, episode_number=i,
                        status=TaskStatus.COMPLETED,
                        stats_json={"avg_pct_viewed": r} if r is not None else None))
        db.commit()
        ids[name] = c.id
    return db, ch, ids


def test_classify_campaigns_vs_channel_baseline():
    """Each campaign is labelled relative to the channel's own average retention."""
    from core import autopilot

    db, ch, ids = _seed({
        "winner": [80, 82, 78],      # well above the channel avg
        "laggard": [20, 22, 18],     # well below
        "healthy": [50, 52, 48],     # around the avg
        "early": [60],               # too few to judge
    })
    # Baseline = mean of all measured episodes: [80,82,78,20,22,18,50,52,48,60] -> 51.0
    assert autopilot.channel_baseline(db, ch.id) == 51.0

    cls = autopilot.classify_campaigns(db, db.query(autopilot.Campaign).all())
    assert cls[ids["winner"]]["label"] == "winner"            # 80 ≥ 51*1.15
    assert cls[ids["laggard"]]["label"] == "underperforming"  # 20 < 51*0.6
    assert cls[ids["healthy"]]["label"] == "healthy"          # in-band
    assert cls[ids["early"]]["label"] == "too_early"          # <3 measured
    assert cls[ids["winner"]]["retention"] == 80.0 and cls[ids["winner"]]["baseline"] == 51.0
    db.close()


def test_classification_needs_a_baseline():
    """With too few measured episodes channel-wide, there is no trustworthy baseline — a campaign is
    never wrongly flagged, and the baseline is None."""
    from core import autopilot

    db, ch, ids = _seed({"solo": [90, 10]})  # 2 measured < MIN_MEASURED
    assert autopilot.channel_baseline(db, ch.id) is None
    v = autopilot.classify_campaigns(db, db.query(autopilot.Campaign).all())[ids["solo"]]
    assert v["label"] == "too_early" and v["baseline"] is None
    db.close()


# ── Phase II: the hands (review / auto-reject / retry / catch-up) ──────────────
def test_review_decision_thresholds():
    from core.autopilot import review_decision

    assert review_decision({"score": 8, "passed": True}, 7, 4)[0] == "approve"
    assert review_decision({"score": 3, "passed": True}, 7, 4)[0] == "reject"   # low score
    assert review_decision({"score": 9, "passed": False}, 7, 4)[0] == "reject"  # failed QC beats score
    assert review_decision({"score": 5, "passed": True}, 7, 4)[0] == "escalate"  # middle band
    assert review_decision(None, 7, 4)[0] == "escalate"                          # no verdict
    assert review_decision({"score": None}, 7, 4)[0] == "escalate"


def _seed_ch(mode):
    from database.db_session import SessionLocal
    from database.models import Campaign, Channel, User
    from database.types import CampaignStatus, Platform

    db = SessionLocal()
    user = User()
    db.add(user)
    db.commit()
    db.refresh(user)
    ch = Channel(user_id=user.id, platform=Platform.youtube, channel_name="AP",
                 autopilot_json={"mode": mode})
    db.add(ch)
    db.commit()
    db.refresh(ch)
    camp = Campaign(user_id=user.id, channel_id=ch.id, topic_name="T", total_episodes=20,
                    status=CampaignStatus.active)
    db.add(camp)
    db.commit()
    db.refresh(camp)
    return db, user, ch, camp


def _review_item(db, ch, camp, ep, score, passed=True):
    from database.models import BufferPoolItem, Task
    from database.types import BufferStatus, TaskStatus

    t = Task(campaign_id=camp.id, user_id=ch.user_id, episode_number=ep,
             status=TaskStatus.AWAITING_REVIEW)
    b = BufferPoolItem(campaign_id=camp.id, channel_id=ch.id, episode_number=ep,
                       video_path=f"/nonexistent/{ep}.mp4", status=BufferStatus.awaiting_review,
                       metadata_json={"qc": {"score": score, "passed": passed}})
    db.add_all([t, b])
    db.commit()
    db.refresh(t)
    db.refresh(b)
    return t, b


def test_autopilot_review_full_auto_approves_rejects_escalates():
    from database.models import Campaign
    from database.types import BufferStatus, TaskStatus
    from workers import scheduler

    db, user, ch, camp = _seed_ch("autopilot")
    t_hi, b_hi = _review_item(db, ch, camp, 1, 9)   # approve → publish
    t_lo, b_lo = _review_item(db, ch, camp, 2, 2)   # reject → re-render + learn
    t_mid, b_mid = _review_item(db, ch, camp, 3, 5)  # escalate → left for human

    counts = scheduler.autopilot_review_channel(db, ch, "autopilot", 7, 4)
    assert counts == {"approved": 1, "rejected": 1, "recommended": 0, "escalated": 1}

    db.refresh(b_hi), db.refresh(b_lo), db.refresh(b_mid), db.refresh(t_hi), db.refresh(t_lo)
    assert t_hi.status == TaskStatus.PENDING_QUEUE          # approved → queued to publish
    assert b_lo.status == BufferStatus.rejected            # rejected → files gone, row rejected
    assert t_lo.status == TaskStatus.PENDING_QUEUE          # rejected → re-render queued
    assert b_mid.status == BufferStatus.awaiting_review     # escalated → still waiting
    assert b_mid.metadata_json["ap_hint"]["action"] == "escalate"
    # The reject reason is fed into the campaign's avoid-list (learning loop).
    assert db.get(Campaign, camp.id).learning_json["reject_reasons"]
    db.close()


def test_autopilot_review_copilot_recommends_but_does_not_publish():
    from database.types import BufferStatus
    from workers import scheduler

    db, user, ch, camp = _seed_ch("copilot")
    t_hi, b_hi = _review_item(db, ch, camp, 1, 9)   # high → recommend only (NOT published)
    t_lo, b_lo = _review_item(db, ch, camp, 2, 2)   # low → still auto-rejected in copilot

    counts = scheduler.autopilot_review_channel(db, ch, "copilot", 7, 4)
    assert counts["recommended"] == 1 and counts["approved"] == 0 and counts["rejected"] == 1

    db.refresh(b_hi), db.refresh(b_lo)
    assert b_hi.status == BufferStatus.awaiting_review           # copilot never auto-publishes
    assert b_hi.metadata_json["ap_hint"]["action"] == "approve"  # but recommends approve
    assert b_lo.status == BufferStatus.rejected                  # auto-reject still fires
    db.close()


def test_autopilot_retry_skips_quota_and_operator_rejects():
    from database.models import Task
    from database.types import TaskStatus
    from workers import scheduler

    db, user, ch, camp = _seed_ch("autopilot")
    genuine = Task(campaign_id=camp.id, user_id=user.id, episode_number=1,
                   status=TaskStatus.FAILED, error_message="ffmpeg exited 1", retry_count=0)
    quota = Task(campaign_id=camp.id, user_id=user.id, episode_number=2,
                 status=TaskStatus.FAILED, error_message="429 quota exhausted", retry_count=0)
    rejected = Task(campaign_id=camp.id, user_id=user.id, episode_number=3,
                    status=TaskStatus.FAILED, error_message="Rejected in review: slow open",
                    retry_count=0)
    capped = Task(campaign_id=camp.id, user_id=user.id, episode_number=4,
                  status=TaskStatus.FAILED, error_message="ffmpeg exited 1",
                  retry_count=scheduler.AUTOPILOT_MAX_RETRIES)
    db.add_all([genuine, quota, rejected, capped])
    db.commit()

    assert scheduler.autopilot_retry_channel(db, ch) == 1  # only the genuine, non-capped failure
    ids = {"g": genuine.id, "q": quota.id, "r": rejected.id, "c": capped.id}
    for t in db.query(Task).all():
        if t.id == ids["g"]:
            assert t.status == TaskStatus.PENDING_QUEUE and t.retry_count == 1
        else:
            assert t.status == TaskStatus.FAILED  # quota / operator-reject / capped all untouched
    db.close()


def test_catch_up_due_recovers_a_missed_slot():
    from datetime import datetime

    from database.models import BufferPoolItem
    from database.types import BufferStatus
    from workers import scheduler

    db, user, ch, camp = _seed_ch("autopilot")
    camp.config_json = {"auto_publish": True, "posting_slots": ["00:01"]}  # slot early today
    b = BufferPoolItem(campaign_id=camp.id, channel_id=ch.id, episode_number=1,
                       video_path="/nonexistent/1.mp4", status=BufferStatus.ready)
    db.add(b)
    db.commit()
    db.refresh(b)

    noon = datetime(2026, 7, 23, 12, 0)  # well past the 00:01 slot, nothing published today
    assert scheduler.catch_up_due(db, camp, now=noon).id == b.id          # a ready item to recover
    # Within a live slot the normal publisher handles it — catch-up stands down.
    early = datetime(2026, 7, 23, 0, 1)
    assert scheduler.catch_up_due(db, camp, now=early) is None
    db.close()


def test_autopilot_pass_respects_per_channel_cadence():
    from workers import scheduler

    db, user, ch, camp = _seed_ch("autopilot")
    _review_item(db, ch, camp, 1, 9)  # a high-score item to approve

    first = scheduler.autopilot_pass(db, respect_cadence=True)
    assert first["channels"] == 1 and first["approved"] == 1
    # The Redis NX guard means an immediate second pass skips this channel (not due yet).
    second = scheduler.autopilot_pass(db, respect_cadence=True)
    assert second["channels"] == 0
    # An 'off' channel is never touched.
    ch.autopilot_json = {"mode": "off"}
    db.commit()
    assert scheduler.autopilot_pass(db, respect_cadence=False)["channels"] == 0
    db.close()

