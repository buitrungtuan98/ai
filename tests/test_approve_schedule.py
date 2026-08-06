"""ADR-090 — approving a review render is a GATE, not a publish trigger.

The report these tests pin down: a campaign configured "Review first" with a 21:00 posting slot
published the instant its Approve button was clicked. `auto_publish` was one boolean answering two
questions — "does a human approve?" and "who owns the timing?" — so choosing review silently deleted
the operator's own schedule, and the approve click became the only thing left that could publish.

The second half is what happens with MORE than one episode, and with campaigns that post on
particular weekdays: five approvals must land in five different slots on the campaign's own posting
days, not pile onto one moment.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


# ── Helpers ──────────────────────────────────────────────────────────────────
def _campaign(session, user, channel, **cfg):
    from database.models import Campaign
    from database.types import CampaignStatus

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Nightly",
                 current_episode=0, total_episodes=20, status=CampaignStatus.active,
                 config_json={"language": "en", **cfg})
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _parked(session, camp, channel, ep, tmp_path, status=None):
    """A rendered episode sitting in the review queue with a real file on disk."""
    from database.models import BufferPoolItem
    from database.types import BufferStatus

    f = tmp_path / f"ep{ep}.mp4"
    f.write_bytes(b"video")
    b = BufferPoolItem(campaign_id=camp.id, channel_id=channel.id, episode_number=ep,
                       video_path=str(f), metadata_json={"title": f"Ep {ep}"},
                       status=status or BufferStatus.awaiting_review)
    session.add(b)
    session.commit()
    session.refresh(b)
    return b


def _no_enqueue(monkeypatch):
    """Record publish enqueues instead of touching Redis."""
    from workers import video_worker

    sent: list[int] = []
    monkeypatch.setattr(video_worker, "enqueue_publish", sent.append)
    return sent


# ── The core behaviour ───────────────────────────────────────────────────────
def test_approving_a_slotted_campaign_schedules_it_instead_of_publishing(
        session, user, channel, tmp_path, monkeypatch):
    from database.types import BufferStatus, TaskStatus
    from workers import video_worker

    sent = _no_enqueue(monkeypatch)
    camp = _campaign(session, user, channel, auto_publish=False, posting_slots=["21:00"])
    buf = _parked(session, camp, channel, 1, tmp_path)

    video_worker.apply_approve(session, buf)
    session.refresh(buf)

    assert sent == []                                   # THE fix: no upload on the click
    assert buf.status == BufferStatus.ready             # …but it has left the review queue
    assert buf.ready_at is not None
    # The reconciler re-issues any publish carrying this marker within the hour, which would undo
    # the schedule — a slot-scheduled approval must not carry one.
    assert "publish_requested_at" not in (buf.metadata_json or {})

    task = _task_for(session, camp, 1)
    if task is not None:
        assert task.status == TaskStatus.SCHEDULED


def test_approving_a_campaign_with_no_slots_still_publishes_at_once(
        session, user, channel, tmp_path, monkeypatch):
    """No slots means no clock to wait for, so approval IS the release. Unchanged behaviour."""
    from database.types import BufferStatus
    from workers import video_worker

    sent = _no_enqueue(monkeypatch)
    camp = _campaign(session, user, channel, auto_publish=False)   # no posting_slots
    buf = _parked(session, camp, channel, 1, tmp_path)

    video_worker.apply_approve(session, buf)
    session.refresh(buf)

    assert sent == [buf.id]
    assert buf.status == BufferStatus.ready
    assert (buf.metadata_json or {}).get("publish_requested_at")   # durable intent (R22) still set


def test_the_reconciler_leaves_a_slot_scheduled_approval_alone(
        session, user, channel, tmp_path, monkeypatch):
    """The pairing that makes the fix hold: without it, the hourly "lost publish job" repair would
    re-issue every approved episode within 15 minutes and publish it early anyway."""
    from database.models import Task
    from database.types import TaskStatus
    from workers import scheduler, task_queue, video_worker

    _no_enqueue(monkeypatch)
    monkeypatch.setattr(task_queue, "queued_publish_buffer_ids", lambda: set())
    requeued: list[int] = []
    monkeypatch.setattr(task_queue, "enqueue_publish", lambda bid: requeued.append(bid) or "job")

    camp = _campaign(session, user, channel, auto_publish=False, posting_slots=["21:00"])
    buf = _parked(session, camp, channel, 1, tmp_path)
    task = Task(campaign_id=camp.id, user_id=user.id, episode_number=1,
                status=TaskStatus.AWAITING_REVIEW)
    session.add(task)
    session.commit()

    video_worker.apply_approve(session, buf)
    # Age the task past the 15-minute grace so the repair pass considers it.
    session.query(Task).filter(Task.id == task.id).update(
        {"updated_at": datetime.utcnow() - timedelta(hours=2)})
    session.commit()

    healed = scheduler.reconcile_stranded_episodes(session)
    assert requeued == [] and healed["requeued"] == 0


# ── The operator's question: days, not just times ────────────────────────────
def test_approvals_queue_one_per_slot_and_only_on_posting_days(
        session, user, channel, tmp_path, monkeypatch):
    """Five episodes approved in one sitting get five DIFFERENT times, each on a posting day.

    This is the pile-up the fix has to answer: a campaign posts 21:00 on Mon/Wed/Fri, so approving
    a batch must spread across those weekdays — never five posts at once, and never a Tuesday.
    """
    from workers import video_worker

    _no_enqueue(monkeypatch)
    camp = _campaign(session, user, channel, auto_publish=False,
                     posting_slots=["21:00"], posting_days=["mon", "wed", "fri"],
                     timezone="Asia/Ho_Chi_Minh")
    items = [_parked(session, camp, channel, ep, tmp_path) for ep in range(1, 6)]

    times = []
    for buf in items:
        when = video_worker.approved_publish_time(session, camp, buf)
        times.append(when)
        video_worker.apply_approve(session, buf)

    assert all(t is not None for t in times)
    assert times == sorted(times), "each approval must take a LATER slot than the last"
    assert len(set(times)) == 5, "five approvals must not share a slot"
    assert {t.strftime("%H:%M") for t in times} == {"21:00"}
    assert {t.weekday() for t in times} <= {0, 2, 4}, "Mon/Wed/Fri only"


def test_two_campaigns_keep_their_own_days_and_hours(session, user, channel, tmp_path, monkeypatch):
    """Approving across campaigns cannot make them drift onto each other's schedule — every
    projection is read from the campaign that owns the episode."""
    from workers import video_worker

    _no_enqueue(monkeypatch)
    mwf = _campaign(session, user, channel, auto_publish=False,
                    posting_slots=["21:00"], posting_days=["mon", "wed", "fri"])
    tue_thu = _campaign(session, user, channel, auto_publish=False,
                        posting_slots=["08:00"], posting_days=["tue", "thu"])

    a = [video_worker.approved_publish_time(session, mwf, _parked(session, mwf, channel, e, tmp_path))
         for e in (1, 2)]
    b = [video_worker.approved_publish_time(session, tue_thu,
                                            _parked(session, tue_thu, channel, e, tmp_path))
         for e in (1, 2)]

    assert {t.strftime("%H:%M") for t in a} == {"21:00"}
    assert {t.weekday() for t in a} <= {0, 2, 4}
    assert {t.strftime("%H:%M") for t in b} == {"08:00"}
    assert {t.weekday() for t in b} <= {1, 3}


def test_an_episode_moved_to_its_own_time_does_not_push_the_queue_back(
        session, user, channel, tmp_path, monkeypatch):
    """An operator-set publish time takes the episode OUT of the slot queue (ADR-059), so it must
    not occupy a slot that a freshly approved episode is entitled to."""
    import main
    from database.types import BufferStatus
    from workers import video_worker

    _no_enqueue(monkeypatch)
    camp = _campaign(session, user, channel, auto_publish=False, posting_slots=["21:00"])
    parked = _parked(session, camp, channel, 1, tmp_path, status=BufferStatus.ready)
    parked.publish_at = datetime.utcnow() + timedelta(days=30)   # moved far out, on purpose
    session.commit()

    fresh = _parked(session, camp, channel, 2, tmp_path)

    # The very NEXT 21:00 — the parked episode holds no slot, so it cannot displace this one.
    assert video_worker.approved_publish_time(session, camp, fresh) == main._upcoming_slots(camp, 1)[0]

    # Whereas an episode that IS in the slot queue does push the next approval back one slot.
    _parked(session, camp, channel, 3, tmp_path, status=BufferStatus.ready)
    assert video_worker.approved_publish_time(session, camp, fresh) == main._upcoming_slots(camp, 2)[1]


def test_the_approval_promise_matches_where_the_scheduler_actually_publishes(
        session, user, channel, tmp_path, monkeypatch):
    """The time shown on approval and the time the scheduler fires must come from one rule — this is
    the drift that ADR-067 removed for the calendar and ADR-090 extends to the approve path."""
    import main
    from workers import video_worker

    _no_enqueue(monkeypatch)
    camp = _campaign(session, user, channel, auto_publish=False,
                     posting_slots=["07:30", "21:00"], posting_days=["mon", "thu"])
    first = _parked(session, camp, channel, 1, tmp_path)
    second = _parked(session, camp, channel, 2, tmp_path)

    expected = main._upcoming_slots(camp, 2)      # read once, before anything moves
    promised = [video_worker.approved_publish_time(session, camp, first)]
    video_worker.apply_approve(session, first)
    promised.append(video_worker.approved_publish_time(session, camp, second))

    assert promised == expected


# ── Small utilities ──────────────────────────────────────────────────────────
def _local_now(camp):
    from workers.scheduler import local_now

    return local_now((camp.config_json or {}).get("timezone"))


def _task_for(session, camp, ep):
    from sqlalchemy import select

    from database.models import Task

    return session.scalar(select(Task).where(Task.campaign_id == camp.id,
                                             Task.episode_number == ep))


def test_upcoming_slots_no_longer_hides_a_review_campaigns_schedule(session, user, channel):
    """The projection every schedule surface reads. Returning [] for review-first campaigns is how
    one setting could disappear from the calendar, the next-slot chip and the publish queue at once."""
    import main

    review = _campaign(session, user, channel, auto_publish=False, posting_slots=["21:00"])
    auto = _campaign(session, user, channel, auto_publish=True, posting_slots=["21:00"])
    none_set = _campaign(session, user, channel, auto_publish=False)

    assert main._upcoming_slots(review, 3) == main._upcoming_slots(auto, 3) != []
    assert main._upcoming_slots(none_set, 3) == []


def test_upcoming_slots_respects_a_campaigns_timezone(session, user, channel):
    """21:00 means 21:00 where the operator lives — the weekday gate is applied on that clock too."""
    import main

    camp = _campaign(session, user, channel, auto_publish=False, posting_slots=["21:00"],
                     posting_days=["sat"], timezone="Asia/Ho_Chi_Minh")
    got = main._upcoming_slots(camp, 2)

    assert [d.strftime("%H:%M") for d in got] == ["21:00", "21:00"]
    assert {d.weekday() for d in got} == {5}
    assert all(d.tzinfo == ZoneInfo("Asia/Ho_Chi_Minh") for d in got)
    assert (got[1] - got[0]).days == 7


# ── The surfaces that used to hide these campaigns ───────────────────────────
def test_the_publish_list_gives_a_pending_review_episode_its_projected_time(
        session, user, channel, tmp_path):
    """A review campaign's rows used to read "After you approve it" with no time at all, so a queue
    of five was five identical rows. Each now carries the slot it takes if approved in turn."""
    import main
    from database.types import BufferStatus

    camp = _campaign(session, user, channel, auto_publish=False,
                     posting_slots=["07:00", "21:00"], timezone="Asia/Ho_Chi_Minh")
    _parked(session, camp, channel, 1, tmp_path, status=BufferStatus.ready)   # approved already
    _parked(session, camp, channel, 2, tmp_path)                              # still waiting
    _parked(session, camp, channel, 3, tmp_path)

    rows = main._ops_publish_rows(session, user.id)
    assert [r["state"] for r in rows] == ["slot", "review", "review"]
    whens = [r["when"] for r in rows]
    assert all(w is not None for w in whens)
    assert whens == sorted(whens) and len(set(whens)) == 3   # three distinct, ordered slots
    assert rows[0]["tz"] == "Asia/Ho_Chi_Minh"


def test_the_calendar_draws_a_review_campaign_instead_of_dropping_it(
        client, session, user, channel, tmp_path):
    """`_calendar_row_cells` returned None for review-first campaigns, so the one page whose job is
    "what publishes when" showed nothing at all for them."""
    import main

    camp = _campaign(session, user, channel, auto_publish=False, posting_slots=["21:00"])
    _parked(session, camp, channel, 1, tmp_path)

    cells = main._calendar_row_cells(camp, [], review_eps=[1])
    assert cells is not None
    states = [c["state"] for day in cells for c in day["slots"]]
    assert "pending" in states, "a rendered-but-unapproved episode owns its slot on the grid"

    body = client.get("/calendar").text
    assert "Nightly" in body and "takes this slot once you approve it" in body


def test_approving_from_the_web_reports_the_scheduled_time_not_publish_queued(
        client, session, user, channel, tmp_path, monkeypatch):
    _no_enqueue(monkeypatch)
    camp = _campaign(session, user, channel, auto_publish=False, posting_slots=["21:00"])
    buf = _parked(session, camp, channel, 1, tmp_path)

    r = client.post(f"/assets/{buf.id}/approve", follow_redirects=False)
    assert r.status_code == 303
    assert "flash=scheduled" in r.headers["location"]
    assert "flash_reason" in r.headers["location"]      # the actual time travels with it

    assert "publishes" in client.get(r.headers["location"]).text


def test_approving_a_slotless_campaign_from_the_web_still_says_publish_queued(
        client, session, user, channel, tmp_path, monkeypatch):
    _no_enqueue(monkeypatch)
    camp = _campaign(session, user, channel, auto_publish=False)
    buf = _parked(session, camp, channel, 1, tmp_path)

    r = client.post(f"/assets/{buf.id}/approve", follow_redirects=False)
    assert r.status_code == 303 and "flash=publish" in r.headers["location"]


def test_a_review_campaigns_schedule_is_visible_on_its_card(client, session, user, channel):
    """The card said only "Review-first" and dropped the schedule — half the reason an instant
    publish was a surprise is that nothing on screen ever mentioned the 21:00 slot."""
    _campaign(session, user, channel, auto_publish=False,
              posting_slots=["21:00"], posting_days=["mon", "fri"])

    body = client.get("/campaigns").text
    assert "Review-first" in body and "21:00" in body and "Mon,Fri" in body


def test_a_compilation_still_publishes_on_approval_and_takes_no_slot(
        session, user, channel, tmp_path, monkeypatch):
    """ADR-085 keeps compilations OUTSIDE the slot schedule, and ADR-090 must not drag them in.

    Two ways this would break: a compilation's sentinel episode number sorts last, so parking one in
    the slot queue delays it behind every regular episode indefinitely; and it would occupy a slot
    the next regular episode was waiting for. Compilations always park for review, so this is not a
    corner case — it is every compilation.
    """
    import main
    from core.compilation import COMPILATION_EPISODE_BASE
    from workers import video_worker

    sent = _no_enqueue(monkeypatch)
    camp = _campaign(session, user, channel, auto_publish=False, posting_slots=["21:00"])
    regular = _parked(session, camp, channel, 1, tmp_path)
    comp = _parked(session, camp, channel, COMPILATION_EPISODE_BASE + 1, tmp_path)

    assert video_worker.approved_publish_time(session, camp, comp) is None
    video_worker.apply_approve(session, comp)
    assert sent == [comp.id]        # published on approval, exactly as before

    # And it did not consume the slot the regular episode is entitled to.
    assert video_worker.approved_publish_time(session, camp, regular) == main._upcoming_slots(camp, 1)[0]

    rows = {r["item"].episode_number: r for r in main._ops_publish_rows(session, user.id)}
    assert rows[1]["when"] is not None
    assert rows[COMPILATION_EPISODE_BASE + 1]["when"] is None   # never drawn on a slot
