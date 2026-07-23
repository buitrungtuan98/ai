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
