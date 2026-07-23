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


# ── Phase III: the brain — proposals + apply ──────────────────────────────────
def test_propose_actions_by_classification():
    from core import autopilot
    from types import SimpleNamespace

    # Winner near its cap → extend.
    win = SimpleNamespace(topic_name="W", status=SimpleNamespace(value="active"),
                          total_episodes=10, current_episode=9)
    props = autopilot.propose_actions(win, [], {"label": "winner", "baseline": 50, "retention": 80})
    assert [p["kind"] for p in props] == ["extend"] and props[0]["params"]["total_episodes"] > 10

    # Healthy near its cap → plan a successor.
    heal = SimpleNamespace(topic_name="H", status=SimpleNamespace(value="active"),
                           total_episodes=10, current_episode=9)
    props = autopilot.propose_actions(heal, [], {"label": "healthy", "baseline": 50, "retention": 52})
    assert [p["kind"] for p in props] == ["successor"]

    # Underperformer with a long low streak → wind down to its current episode.
    lag = SimpleNamespace(topic_name="L", status=SimpleNamespace(value="active"),
                          total_episodes=20, current_episode=8)
    tasks = [SimpleNamespace(episode_number=i, stats_json={"avg_pct_viewed": 10}) for i in range(1, 9)]
    props = autopilot.propose_actions(lag, tasks, {"label": "underperforming", "baseline": 50, "retention": 10})
    assert [p["kind"] for p in props] == ["wind_down"] and props[0]["params"]["total_episodes"] == 8


def test_propose_channel_is_idempotent_and_apply_extend():
    from database.models import AutopilotAction, Task
    from database.types import TaskStatus
    from workers import scheduler

    from database.models import Campaign
    from database.types import CampaignStatus

    db, user, ch, camp = _seed_ch("copilot")
    camp.total_episodes = 10
    camp.current_episode = 9
    db.commit()
    # Three measured episodes, all high.
    for i in range(1, 4):
        db.add(Task(campaign_id=camp.id, user_id=user.id, episode_number=i,
                    status=TaskStatus.COMPLETED, stats_json={"avg_pct_viewed": 90}))
    # A laggard sibling drags the channel baseline down so `camp` reads as a genuine winner
    # (a lone campaign equals its own baseline and would only ever be "healthy").
    lag = Campaign(user_id=user.id, channel_id=ch.id, topic_name="Lag", total_episodes=20,
                   status=CampaignStatus.completed)
    db.add(lag)
    db.commit()
    db.refresh(lag)
    for i in range(1, 7):
        db.add(Task(campaign_id=lag.id, user_id=user.id, episode_number=i,
                    status=TaskStatus.COMPLETED, stats_json={"avg_pct_viewed": 10}))
    db.commit()

    assert scheduler.autopilot_propose_channel(db, ch) == 1          # files one "extend"
    assert scheduler.autopilot_propose_channel(db, ch) == 0          # idempotent — no duplicate
    action = db.query(AutopilotAction).filter_by(kind="extend").one()
    assert action.status == "proposed"

    assert scheduler.apply_autopilot_action(db, action) is True
    db.refresh(action), db.refresh(camp)
    assert action.status == "applied" and camp.total_episodes > 10   # extended, reversible config only


def test_apply_wind_down_and_successor():
    from database.models import AutopilotAction, Campaign
    from database.types import CampaignStatus
    from workers import scheduler

    db, user, ch, camp = _seed_ch("copilot")
    camp.config_json = {"language": "vi", "voice": "v"}
    camp.current_episode = 6
    db.commit()

    wind = AutopilotAction(user_id=user.id, channel_id=ch.id, campaign_id=camp.id, kind="wind_down",
                           summary="wind down", evidence={}, params={"total_episodes": 6})
    succ = AutopilotAction(user_id=user.id, channel_id=ch.id, campaign_id=camp.id, kind="successor",
                           summary="successor", evidence={}, params={})
    db.add_all([wind, succ])
    db.commit()

    assert scheduler.apply_autopilot_action(db, wind) is True
    db.refresh(camp)
    assert camp.total_episodes == 6  # wound down to current — stops new work, deletes nothing

    assert scheduler.apply_autopilot_action(db, succ) is True
    db.refresh(succ)
    new_id = succ.params["created_campaign_id"]
    new = db.get(Campaign, new_id)
    assert new.status == CampaignStatus.pending and new.config_json == {"language": "vi", "voice": "v"}
    assert new.topic_name.endswith(" II")
    db.close()


# ── Phase IV: full-auto auto-apply + guardrails + strategist ──────────────────
def test_full_auto_applies_with_guardrails():
    from database.models import AutopilotAction, Campaign
    from database.types import CampaignStatus
    from workers import scheduler

    db, user, ch, camp = _seed_ch("autopilot")
    camp.config_json = {"auto_publish": True, "language": "vi"}
    db.commit()
    ext = AutopilotAction(user_id=user.id, channel_id=ch.id, campaign_id=camp.id, kind="extend",
                          summary="extend", evidence={}, params={"total_episodes": 30})
    suc = AutopilotAction(user_id=user.id, channel_id=ch.id, campaign_id=camp.id, kind="successor",
                          summary="successor", evidence={}, params={})
    db.add_all([ext, suc])
    db.commit()

    applied = scheduler.autopilot_autoapply_channel(db, ch)
    assert applied["extend"] == 1 and applied["successor"] == 1
    db.refresh(camp)
    assert camp.total_episodes == 30
    # The auto-created successor starts active but with training wheels: review-first, not auto-publish.
    new = db.query(Campaign).filter(Campaign.topic_name.endswith(" II")).one()
    assert new.status == CampaignStatus.active and new.config_json["auto_publish"] is False
    db.close()


def test_full_auto_successor_respects_max_active_cap():
    from database.models import AutopilotAction, Campaign
    from workers import scheduler

    db, user, ch, camp = _seed_ch("autopilot")
    ch.autopilot_json = {"mode": "autopilot", "max_active": 1}  # already at the cap (camp is active)
    db.commit()
    suc = AutopilotAction(user_id=user.id, channel_id=ch.id, campaign_id=camp.id, kind="successor",
                          summary="successor", evidence={}, params={})
    db.add(suc)
    db.commit()

    assert scheduler.autopilot_autoapply_channel(db, ch)["successor"] == 0  # capped → not applied
    db.refresh(suc)
    assert suc.status == "proposed"  # left for the operator
    assert db.query(Campaign).filter(Campaign.topic_name.endswith(" II")).count() == 0
    db.close()


def test_strategist_files_tune_proposal_guarded(monkeypatch):
    from core import ai_engine
    from core.ai_engine import ChannelTune
    from core.config import settings
    from database.models import AutopilotAction
    from workers import scheduler

    db, user, ch, camp = _seed_ch("autopilot")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(settings, "GEMINI_DAILY_BUDGET", None)
    monkeypatch.setattr(ai_engine, "suggest_channel_tune",
                        lambda **k: ChannelTune(caption_theme="neon", rationale="neon lifts retention"))

    assert scheduler.autopilot_strategist_channel(db, user, ch, respect_cadence=False) == 1
    tune = db.query(AutopilotAction).filter_by(kind="tune").one()
    assert tune.status == "proposed" and tune.params["caption_theme"] == "neon"
    # Applying a tune mutates the campaign config (creative direction stays operator-confirmed).
    assert scheduler.apply_autopilot_action(db, tune) is True
    db.refresh(camp)
    assert camp.config_json["caption_theme"] == "neon"

    # No Gemini key → the strategist stands down (0 AI calls, no proposal).
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    db.query(AutopilotAction).delete()
    db.commit()
    assert scheduler.autopilot_strategist_channel(db, user, ch, respect_cadence=False) == 0
    db.close()

