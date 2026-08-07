"""R25 — the render buffer stops lying about why it is idle, and an episode keeps its clock.

Two production reports, one cause: the factory knew things about itself that no surface could say.
It stopped rendering and called that an empty buffer; it published an episode and overwrote the
only record of when that episode had rendered.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from database.models import BufferPoolItem, Campaign, Task
from database.types import BufferStatus, CampaignStatus, TaskStatus
from workers import video_worker


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def _campaign(session, user, channel, **cfg):
    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Topic",
                 total_episodes=50, current_episode=0, status=CampaignStatus.active,
                 config_json={"posting_slots": ["21:00"], **cfg})
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _park(session, task, campaign, channel, *, status=BufferStatus.awaiting_review):
    """Put an episode where a human decision is the only thing that moves it."""
    task.status = TaskStatus.AWAITING_REVIEW
    task.rendered_at = datetime.utcnow()
    session.add(BufferPoolItem(campaign_id=campaign.id, channel_id=channel.id,
                               episode_number=task.episode_number,
                               video_path=f"/tmp/ep{task.episode_number}.mp4",
                               metadata_json={}, status=status))
    session.commit()


# ── The reported stall ───────────────────────────────────────────────────────
def test_qc_parked_episodes_no_longer_eat_an_auto_campaigns_runway(session, user, channel):
    """The bug: on an AUTO-publish campaign, episodes Auto-QC had parked counted as buffer depth.
    Two parked left room for one render instead of three, and three stopped the factory dead —
    while every page reported the buffer as empty and pointed at the renderer."""
    cam = _campaign(session, user, channel, auto_publish=True)
    first = video_worker.hydrate_campaign(session, cam, enqueue=lambda t: f"j{t}")
    assert len(first) == 3

    tasks = session.query(Task).order_by(Task.episode_number).all()
    for t in tasks[:2]:
        _park(session, t, cam, channel)          # two held back by Auto-QC
    tasks[2].status = TaskStatus.SCHEDULED       # one rendered and waiting for its slot
    session.commit()

    state = video_worker.buffer_state(session, cam)
    assert (state.parked, state.flowing) == (2, 1)
    # The two parked episodes cannot reach a slot on their own, so they do not count as runway:
    # the campaign is still owed two episodes that publish themselves.
    created = video_worker.hydrate_campaign(session, cam, enqueue=lambda t: f"j{t}")
    assert len(created) == 2
    assert video_worker.buffer_state(session, cam).flowing == 3


def test_a_full_review_queue_pauses_rendering_and_says_so(session, user, channel):
    """Pausing is still right — an operator who owes three decisions does not need a fourth video.
    What must never happen again is pausing silently."""
    cam = _campaign(session, user, channel, auto_publish=True)
    video_worker.hydrate_campaign(session, cam, enqueue=lambda t: f"j{t}")
    for t in session.query(Task).all():
        _park(session, t, cam, channel)

    state = video_worker.buffer_state(session, cam)
    assert state.parked == 3
    assert state.next_episodes == []
    assert state.reason == "review"
    assert video_worker.hydrate_campaign(session, cam, enqueue=lambda t: f"j{t}") == []


def test_review_first_campaigns_keep_exactly_the_depth_they_had(session, user, channel):
    """The split must not deepen a review-first campaign. Every render there parks, so work still
    in flight is a parked episode that has not landed yet and counts against the same cap —
    total in-flight stays at the buffer size, as it always was."""
    cam = _campaign(session, user, channel, auto_publish=False)
    video_worker.hydrate_campaign(session, cam, enqueue=lambda t: f"j{t}")
    tasks = session.query(Task).order_by(Task.episode_number).all()
    for t in tasks[:2]:
        _park(session, t, cam, channel)          # two awaiting review, one still rendering
    session.commit()

    state = video_worker.buffer_state(session, cam)
    assert (state.parked, state.flowing) == (2, 1)
    assert state.next_episodes == []             # 3 in flight is the whole buffer — not 5
    assert video_worker.hydrate_campaign(session, cam, enqueue=lambda t: f"j{t}") == []


def test_buffer_state_names_the_other_reasons_it_is_idle(session, user, channel):
    cam = _campaign(session, user, channel, auto_publish=True)
    video_worker.hydrate_campaign(session, cam, enqueue=lambda t: f"j{t}")
    assert video_worker.buffer_state(session, cam).reason == "full"

    capped = _campaign(session, user, channel, auto_publish=True, max_per_day=1)
    video_worker.hydrate_campaign(session, capped, enqueue=lambda t: f"j{t}")
    assert video_worker.buffer_state(session, capped).reason == "daily_cap"

    short = _campaign(session, user, channel, auto_publish=True)
    short.total_episodes = 2
    session.commit()
    video_worker.hydrate_campaign(session, short, enqueue=lambda t: f"j{t}")
    assert video_worker.buffer_state(session, short).reason == "planned_out"


def test_cancelled_and_failed_episodes_free_the_buffer(session, user, channel):
    """ADR-064's guarantee, re-asserted against the new accounting: a terminal outcome is neither
    flowing nor parked, so it can never starve the buffer."""
    cam = _campaign(session, user, channel, auto_publish=True)
    video_worker.hydrate_campaign(session, cam, enqueue=lambda t: f"j{t}")
    tasks = session.query(Task).order_by(Task.episode_number).all()
    tasks[0].status = TaskStatus.CANCELLED
    tasks[1].status = TaskStatus.FAILED
    session.commit()

    state = video_worker.buffer_state(session, cam)
    assert (state.flowing, state.parked) == (1, 0)
    assert len(video_worker.hydrate_campaign(session, cam, enqueue=lambda t: f"j{t}")) == 2


def test_a_compilation_awaiting_review_never_blocks_ordinary_episodes(session, user, channel):
    """ADR-082: a best-of is extra content. One parked for review must not spend the review cap the
    campaign's real episodes need."""
    from core.compilation import COMPILATION_EPISODE_BASE

    cam = _campaign(session, user, channel, auto_publish=True)
    comp = Task(campaign_id=cam.id, user_id=user.id, episode_number=COMPILATION_EPISODE_BASE + 1,
                video_kind="compilation", status=TaskStatus.AWAITING_REVIEW)
    session.add(comp)
    session.commit()

    state = video_worker.buffer_state(session, cam)
    assert state.parked == 0
    assert len(video_worker.hydrate_campaign(session, cam, enqueue=lambda t: f"j{t}")) == 3


# ── The surfaces that were blaming the renderer ──────────────────────────────
def test_the_slot_alert_points_at_the_review_queue_when_that_is_the_blocker(session, user, channel):
    """"Next post is 21:00 but nothing is rendered yet" sent operators to Operations to debug a
    worker that was idle on purpose, over episodes that were rendered and waiting for them."""
    import main

    cam = _campaign(session, user, channel, auto_publish=True,
                    posting_slots=[(datetime.utcnow() + timedelta(hours=2)).strftime("%H:%M")])
    video_worker.hydrate_campaign(session, cam, enqueue=lambda t: f"j{t}")
    for t in session.query(Task).all():
        _park(session, t, cam, channel)

    alerts = [a for a in main._schedule_alerts(session, user) if a["key"].startswith("slot-risk")]
    assert len(alerts) == 1
    assert "waiting for your approval" in alerts[0]["text"]
    assert "nothing is rendered" not in alerts[0]["text"]
    assert alerts[0]["href"] == "/assets"


def test_a_genuinely_starved_campaign_still_points_at_the_render_queue(session, user, channel):
    import main

    _campaign(session, user, channel, auto_publish=True,
              posting_slots=[(datetime.utcnow() + timedelta(hours=2)).strftime("%H:%M")])
    alerts = [a for a in main._schedule_alerts(session, user) if a["key"].startswith("slot-risk")]
    assert len(alerts) == 1
    assert "nothing is rendered yet" in alerts[0]["text"]
    assert alerts[0]["href"] == "/operations?tab=queue"


def test_the_dashboard_splits_starved_from_waiting_on_you(session, user, channel):
    import main

    starved = _campaign(session, user, channel, auto_publish=True)
    blocked = _campaign(session, user, channel, auto_publish=True)
    video_worker.hydrate_campaign(session, blocked, enqueue=lambda t: f"j{t}")
    for t in session.query(Task).filter(Task.campaign_id == blocked.id).all():
        _park(session, t, blocked, channel)

    card = main._scorecard(session, user.id)
    assert card["empty_campaigns"] == 2          # both will miss their slot — unchanged
    assert card["starved_campaigns"] == 1        # …but only one of them is the renderer's fault
    assert card["review_blocked_campaigns"] == 1
    assert main._campaigns_with_empty_buffer(session, user.id) == 2
    assert starved.id != blocked.id


# ── The clock ────────────────────────────────────────────────────────────────
def test_publishing_no_longer_destroys_the_render_time(session, user, channel):
    """`finished_at` is the terminal stamp and the publish rewrites it, so before R25 an episode
    that went live had no record of when it had rendered."""
    from core import timeline

    cam = _campaign(session, user, channel, auto_publish=True)
    rendered = datetime(2026, 8, 1, 13, 2)
    published = datetime(2026, 8, 2, 14, 0)
    task = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                status=TaskStatus.COMPLETED, started_at=datetime(2026, 8, 1, 12, 40),
                rendered_at=rendered, published_at=published, finished_at=published)
    session.add(task)
    session.commit()

    by_key = {m.key: m for m in timeline.episode_timeline(session, task, cam)}
    assert by_key["rendered"].at == rendered
    assert by_key["published"].at == published
    assert by_key["started"].at == datetime(2026, 8, 1, 12, 40)
    assert by_key["reviewed"].at is None          # auto-publish: no gate ran, so no review time
    assert all(m.done for m in (by_key["queued"], by_key["rendered"], by_key["published"]))


def test_the_timeline_reads_on_the_campaigns_own_clock(session, user, channel):
    from core import timeline

    cam = _campaign(session, user, channel, timezone="Asia/Ho_Chi_Minh", auto_publish=True)
    task = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                status=TaskStatus.COMPLETED, rendered_at=datetime(2026, 8, 1, 13, 0),
                published_at=datetime(2026, 8, 1, 14, 0))
    session.add(task)
    session.commit()

    by_key = {m.key: m for m in timeline.episode_timeline(session, task, cam)}
    assert by_key["published"].local.strftime("%H:%M") == "21:00"   # 14:00 UTC = 21:00 +07
    assert by_key["published"].at.strftime("%H:%M") == "14:00"      # stored value untouched


def test_a_parked_episode_reports_the_slot_it_is_waiting_for(session, user, channel):
    """The operator's actual question — "when does this go out?" — answered for an episode that has
    not been approved yet, and marked as a forecast rather than printed like a fact."""
    from core import timeline

    cam = _campaign(session, user, channel, auto_publish=False)
    task = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                status=TaskStatus.AWAITING_REVIEW, rendered_at=datetime.utcnow())
    session.add(task)
    session.commit()
    buf = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                         video_path="/tmp/e1.mp4", metadata_json={},
                         status=BufferStatus.awaiting_review)
    session.add(buf)
    session.commit()

    by_key = {m.key: m for m in timeline.episode_timeline(session, task, cam, buf)}
    assert by_key["scheduled"].local.strftime("%H:%M") == "21:00"
    assert by_key["scheduled"].estimated and not by_key["scheduled"].done
    assert "approval" in by_key["scheduled"].note
    assert by_key["published"].at is None


def test_an_operator_override_outranks_the_slot_on_the_timeline(session, user, channel):
    """ADR-059: a per-episode publish time replaces the schedule, so the timeline must name it and
    not the slot the campaign would otherwise have used."""
    from core import timeline

    cam = _campaign(session, user, channel, auto_publish=True)
    when = datetime.utcnow() + timedelta(days=3)
    task = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                status=TaskStatus.SCHEDULED, rendered_at=datetime.utcnow())
    session.add(task)
    session.commit()
    buf = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                         video_path="/tmp/e1.mp4", metadata_json={}, status=BufferStatus.ready,
                         publish_at=when)
    session.add(buf)
    session.commit()

    by_key = {m.key: m for m in timeline.episode_timeline(session, task, cam, buf)}
    assert by_key["scheduled"].at == when
    assert by_key["scheduled"].note == "moved to this exact time"


def test_approval_stamps_the_review_time(session, user, channel):
    from core import timeline

    cam = _campaign(session, user, channel, auto_publish=False)
    task = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                status=TaskStatus.AWAITING_REVIEW, rendered_at=datetime.utcnow())
    session.add(task)
    session.commit()
    path = "/tmp/aivf_pytest_media/r25-approve.mp4"
    import os

    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(b"x")
    buf = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                         video_path=path, metadata_json={}, status=BufferStatus.awaiting_review)
    session.add(buf)
    session.commit()

    video_worker.apply_approve(session, buf)
    session.refresh(task)
    assert task.reviewed_at is not None
    by_key = {m.key: m for m in timeline.episode_timeline(session, task, cam, buf)}
    assert by_key["reviewed"].done


def test_a_pre_r25_episode_keeps_the_story_its_row_still_holds(session, user, channel):
    """Rows written before the three columns shipped have only `finished_at` — which means
    "published" on a COMPLETED task and "rendered" on any other. Fall back rather than show blanks
    over an entire back catalogue, and never report one value under two labels."""
    from core import timeline

    cam = _campaign(session, user, channel, auto_publish=True)
    legacy_done = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                       status=TaskStatus.COMPLETED, finished_at=datetime(2026, 7, 1, 10, 0))
    legacy_parked = Task(campaign_id=cam.id, user_id=user.id, episode_number=2,
                         status=TaskStatus.AWAITING_REVIEW, finished_at=datetime(2026, 7, 1, 11, 0))
    session.add_all([legacy_done, legacy_parked])
    session.commit()

    done = {m.key: m for m in timeline.episode_timeline(session, legacy_done, cam)}
    assert done["published"].at == datetime(2026, 7, 1, 10, 0)
    assert done["rendered"].at is None            # not the same instant under a second label
    assert done["reviewed"].at is None

    parked = {m.key: m for m in timeline.episode_timeline(session, legacy_parked, cam)}
    assert parked["rendered"].at == datetime(2026, 7, 1, 11, 0)
    assert parked["published"].at is None

    # …and a legacy PUBLISHED auto-render must not have a review invented for it out of the buffer
    # row's creation stamp, which is the render instant wearing a different name.
    consumed = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                              video_path="/tmp/gone.mp4", metadata_json={},
                              status=BufferStatus.consumed,
                              ready_at=datetime(2026, 7, 1, 9, 55),
                              consumed_at=datetime(2026, 7, 1, 10, 0))
    session.add(consumed)
    session.commit()
    again = {m.key: m for m in timeline.episode_timeline(session, legacy_done, cam, consumed)}
    assert again["reviewed"].at is None
    assert again["published"].at == datetime(2026, 7, 1, 10, 0)


def test_the_episode_page_prints_every_milestone(client, session, user, channel):
    """The operator's request in one assertion: open one episode, read when it was queued, when it
    rendered, when it was reviewed, when it is due out and when it actually went live."""
    cam = _campaign(session, user, channel, timezone="Asia/Ho_Chi_Minh", auto_publish=False)
    task = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                status=TaskStatus.COMPLETED,
                started_at=datetime(2026, 8, 1, 12, 40), rendered_at=datetime(2026, 8, 1, 13, 2),
                reviewed_at=datetime(2026, 8, 1, 13, 30), published_at=datetime(2026, 8, 1, 14, 0),
                finished_at=datetime(2026, 8, 1, 14, 0))
    session.add(task)
    session.commit()
    session.refresh(task)

    body = client.get(f"/episodes/{task.id}").text
    for label in ("Queued", "Render started", "Render finished", "Reviewed", "Published"):
        assert label in body
    assert "2026-08-01 20:02" in body      # rendered 13:02 UTC, shown on the campaign's +07 clock
    assert "2026-08-01 21:00" in body      # published 14:00 UTC
    assert "Times in Asia/Ho_Chi_Minh" in body


def test_the_episode_page_dates_the_slot_an_unapproved_render_is_waiting_for(
        client, session, user, channel):
    cam = _campaign(session, user, channel, auto_publish=False)
    task = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                status=TaskStatus.AWAITING_REVIEW, rendered_at=datetime.utcnow())
    session.add(task)
    session.commit()
    session.refresh(task)
    session.add(BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                               video_path="/tmp/e1.mp4", metadata_json={},
                               status=BufferStatus.awaiting_review))
    session.commit()

    body = client.get(f"/episodes/{task.id}").text
    assert "Scheduled" in body
    assert "21:00 <span class=\"hint\">(due)</span>" in body   # a forecast, labelled as one
    assert "waiting for approval first" in body


@pytest.mark.parametrize("status", [TaskStatus.FAILED, TaskStatus.CANCELLED])
def test_the_timeline_survives_an_episode_that_never_finished(session, user, channel, status):
    from core import timeline

    cam = _campaign(session, user, channel, auto_publish=True)
    task = Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=status)
    session.add(task)
    session.commit()

    marks = timeline.episode_timeline(session, task, cam)
    assert [m.key for m in marks][:2] == ["queued", "started"]
    assert marks[0].done                          # it was queued, and that much is known
    assert not any(m.key == "published" and m.done for m in marks)
