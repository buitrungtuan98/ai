"""ADR-079 — early-flop detection: name a flop on day one, learn from it automatically.

Retention (the king metric) lags ~2 days; by then two more episodes may have shipped into the same
hole. First-day views are available within hours, so the verdict lands while it still changes what
renders next. Every judgment is against the campaign's OWN median — a small channel's 40 views can
be healthy, a big channel's 4,000 can be a flop.
"""
from __future__ import annotations

import datetime as dt

from core import flop


def _t(views_24h=None, judged=None, ep=1, synopsis=None, variant=None):
    class T:
        stats_json = ({} if views_24h is None and judged is None
                      else {"views_24h": views_24h, **({"flop": judged} if judged is not None else {})})
        episode_number = ep
        ab_variant = variant
    T.synopsis = synopsis
    return T()


def test_median_needs_enough_measured_episodes():
    assert flop.campaign_median_24h([_t(100), _t(200)]) is None          # 2 < MIN_MEASURED_24H
    tasks = [_t(v) for v in (100, 300, 200, 500, 400)]
    assert flop.campaign_median_24h(tasks) == 300                        # odd count → middle
    assert flop.campaign_median_24h(tasks + [_t(600)]) == 350            # even → mean of middle two


def test_is_flop_three_states():
    assert flop.is_flop(50, 300) is True                                 # 50 < 0.3*300
    assert flop.is_flop(120, 300) is False
    assert flop.is_flop(None, 300) is None                               # no snapshot yet
    assert flop.is_flop(50, None) is None                                # not enough history


def test_snapshot_stamped_once_at_24h_then_judged(session, user, channel, monkeypatch):
    """The pipeline: hourly early stats → views_24h stamped exactly once past 24h → the campaign
    judged → the flop flagged + autopsied, the healthy episode marked fine — idempotently."""
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from services import analytics_service as A

    now = dt.datetime.utcnow()
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Sử", total_episodes=20,
                   status=CampaignStatus.active, config_json={"timezone": "Asia/Ho_Chi_Minh"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    # Five measured veterans (the median base: 300) + one fresh episode 25h old, tanking.
    for i, v in enumerate((280, 300, 320, 310, 290), start=1):
        session.add(Task(campaign_id=cam.id, user_id=user.id, episode_number=i,
                         status=TaskStatus.COMPLETED, published_video_id=f"v{i}",
                         finished_at=now - dt.timedelta(days=10),
                         stats_json={"views_24h": v, "flop": False}))
    fresh = Task(campaign_id=cam.id, user_id=user.id, episode_number=6, ab_variant="B",
                 synopsis="Chuyện cũ kể lại", status=TaskStatus.COMPLETED,
                 published_video_id="v6", finished_at=now - dt.timedelta(hours=25))
    session.add(fresh)
    session.commit()

    monkeypatch.setattr(A, "fetch_youtube_early_stats",
                        lambda ch, ids: {"v6": {"views": 42, "likes": 1, "comments": 0}})
    assert A.collect_early_stats(session, now=now) == 1
    session.refresh(fresh)
    s = fresh.stats_json
    assert s["views_24h"] == 42 and s["flop"] is True
    notes = session.get(Campaign, cam.id).learning_json["flop_notes"]
    assert len(notes) == 1
    # The autopsy is prompt-ready evidence: numbers, variant, local hour, premise.
    assert "episode 6 flopped (42 first-day views" in notes[0]
    assert "variant B" in notes[0] and "Chuyện cũ kể lại" in notes[0]

    # An hour later the views grew — the snapshot must NOT move, and nothing is re-judged.
    monkeypatch.setattr(A, "fetch_youtube_early_stats",
                        lambda ch, ids: {"v6": {"views": 90, "likes": 3, "comments": 0}})
    A.collect_early_stats(session, now=now + dt.timedelta(hours=1))
    session.refresh(fresh)
    assert fresh.stats_json["views_24h"] == 42                    # stamped once, forever
    assert len(session.get(Campaign, cam.id).learning_json["flop_notes"]) == 1


def test_no_verdict_without_enough_history(session, user, channel, monkeypatch):
    """Two measured episodes is not a baseline. Silence, not a guess."""
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from services import analytics_service as A

    now = dt.datetime.utcnow()
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=20,
                   status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)
    for i, v in enumerate((300, 320), start=1):
        session.add(Task(campaign_id=cam.id, user_id=user.id, episode_number=i,
                         status=TaskStatus.COMPLETED, published_video_id=f"v{i}",
                         finished_at=now - dt.timedelta(days=5), stats_json={"views_24h": v}))
    fresh = Task(campaign_id=cam.id, user_id=user.id, episode_number=3,
                 status=TaskStatus.COMPLETED, published_video_id="v3",
                 finished_at=now - dt.timedelta(hours=26))
    session.add(fresh)
    session.commit()
    monkeypatch.setattr(A, "fetch_youtube_early_stats",
                        lambda ch, ids: {"v3": {"views": 5, "likes": 0, "comments": 0}})
    A.collect_early_stats(session, now=now)
    session.refresh(fresh)
    assert fresh.stats_json["views_24h"] == 5
    assert "flop" not in fresh.stats_json                          # no verdict — not enough data
    assert not (session.get(Campaign, cam.id).learning_json or {}).get("flop_notes")


def test_late_autopsy_blames_the_hook_once(session, user, channel, monkeypatch):
    """When the retention curve arrives and its biggest drop is in scene 1, the flop note gains its
    cause — once, not on every daily refresh; and 'scene 1 ' never matches scene 10."""
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus
    from services import analytics_service as A

    assert flop.late_autopsy_hook_note(_t(ep=7), "Biggest drop-off at 0:02 (scene 1 — “hook”): "
                                                 "−40% of viewers left there.")
    assert flop.late_autopsy_hook_note(_t(), "Biggest drop-off at 5:00 (scene 10 — “x”): −20%…") is None

    now = dt.datetime.utcnow()
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=20,
                   status=CampaignStatus.active)
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=7, status=TaskStatus.COMPLETED,
             published_video_id="v7", finished_at=now - dt.timedelta(days=3),
             render_json={"scenes": [{"index": 0, "start": 0.0, "end": 3.0, "label": "hook"},
                                     {"index": 1, "start": 3.0, "end": 9.0, "label": "body"}]},
             stats_json={"views_24h": 10, "flop": True})
    session.add(t)
    session.commit()

    curve = [[0.0, 1.0], [0.2, 0.55], [0.6, 0.5], [1.0, 0.45]]    # cliff inside scene 1
    monkeypatch.setattr(A, "fetch_youtube_stats",
                        lambda ch, ids: {"v7": {"views": 60, "likes": 2, "avg_pct_viewed": 30.0}})
    monkeypatch.setattr(A, "fetch_youtube_geography", lambda ch, ids: {})
    monkeypatch.setattr(A, "fetch_youtube_retention", lambda ch, ids: {"v7": curve})
    A.collect_stats(session, now=now)
    session.refresh(t)
    notes = session.get(Campaign, cam.id).learning_json["flop_notes"]
    assert len(notes) == 1 and "the OPENING" in notes[0] and "hook failed" in notes[0]
    assert t.stats_json["hook_autopsy"] is True

    # The next daily refresh must not write it again.
    monkeypatch.setattr(A, "fetch_youtube_retention", lambda ch, ids: {})
    A.collect_stats(session, now=now + dt.timedelta(days=1, hours=1))
    assert len(session.get(Campaign, cam.id).learning_json["flop_notes"]) == 1
