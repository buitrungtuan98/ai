"""One definition of an episode's clock — when each stage of its life actually happened.

The lifecycle rail on the episode page has always known WHERE an episode is (Queued → Rendering →
Review → Scheduled → Published). It could not say WHEN, because no stage had a recorded time:
`Task.finished_at` is the terminal stamp and is rewritten by whichever step ends the episode, so a
slot publish hours later overwrote the render's own finish and the render time was simply gone
(ADR-091).

`Task.rendered_at` / `reviewed_at` / `published_at` now record those three moments, each written
once by the step that owns it. This module turns them — plus the two that were never columns at all
(the enqueue, and the slot an episode is still waiting for) — into one ordered list of milestones,
on the campaign's own clock, so the answer is the same wherever it is read.

Deliberately read-only and free of side effects: it explains history, it never changes it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from core.config import settings
from database.types import BufferStatus, TaskStatus

# The moments an operator asks about, in the order they happen. Queued and Started are two events,
# not one: the gap between them IS the worker backlog, and collapsing them hides the most common
# reason an episode is late while nothing at all appears to be wrong.
STAGE_LABELS = {
    "queued": "Queued",
    "started": "Render started",
    "rendered": "Render finished",
    "reviewed": "Reviewed",
    "scheduled": "Scheduled",
    "published": "Published",
}


@dataclass
class Milestone:
    """One moment in an episode's life. `at` is naive UTC (as stored); `local` is the same instant
    on the campaign's clock — the one the operator set posting slots in.

    `estimated` marks a time that has not happened yet: the slot an episode is waiting for is a
    projection of the schedule, recomputed on every read, and saying so is the difference between
    a forecast and a lie. `note` carries the one-line why ("waiting for approval").
    """

    key: str
    label: str
    at: datetime | None
    local: datetime | None
    estimated: bool = False
    note: str = ""

    @property
    def done(self) -> bool:
        return self.at is not None and not self.estimated


def _tz(campaign) -> ZoneInfo:
    name = ((campaign.config_json or {}).get("timezone") if campaign else None) or settings.TIMEZONE
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — a bad stored zone must never break a page
        return ZoneInfo("UTC")


def _local(at: datetime | None, tz: ZoneInfo) -> datetime | None:
    if at is None:
        return None
    return at.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)


def _scheduled_for(db, task, campaign, buffer):
    """When this episode is due to go out, as (datetime|None, note).

    Three sources, in the order they override each other: an operator's per-episode `publish_at`
    (ADR-059) beats the schedule; otherwise the campaign's own next free slot, counted the same way
    the scheduler publishes and the calendar draws (ADR-090); otherwise nothing, because a campaign
    with no posting slots publishes the moment its gate clears and has no future time to name.
    """
    if buffer is not None and buffer.publish_at is not None:
        return buffer.publish_at, "moved to this exact time"
    if campaign is None or not (campaign.config_json or {}).get("posting_slots"):
        return None, ""
    from workers.video_worker import approved_publish_time

    if buffer is not None and buffer.status in (BufferStatus.ready, BufferStatus.awaiting_review):
        # `approved_publish_time` answers for a real buffer row, queue position included — the same
        # projection the approve button promises, so the page and the button cannot disagree.
        when = approved_publish_time(db, campaign, buffer)
        if when is not None:
            note = ("waiting for approval first" if buffer.status == BufferStatus.awaiting_review
                    else "")
            return when, note
        return None, ""
    from workers.scheduler import upcoming_slots

    slots = upcoming_slots(campaign, 1)
    return (slots[0], "if it renders in time") if slots else (None, "")


def episode_timeline(db, task, campaign, buffer=None) -> list[Milestone]:
    """The five milestones of ONE episode, oldest first, with the not-yet-happened ones marked.

    Legacy rows keep most of their story: episodes written before the three columns shipped fall
    back to what those rows DO have — `finished_at` means "published" on a COMPLETED task and
    "rendered" on any other, and a consumed buffer row remembers the publish instant in
    `consumed_at`. A stage with no recoverable time is reported with `at=None` rather than guessed.
    """
    tz = _tz(campaign)
    status = task.status.value if hasattr(task.status, "value") else task.status
    completed = status == TaskStatus.COMPLETED.value
    finished = task.finished_at

    published_at = task.published_at or (buffer.consumed_at if buffer is not None else None)
    if published_at is None and completed:
        published_at = finished
    # `finished_at` is the render's finish only while nothing has overwritten it — i.e. on any task
    # that has not published. Once it has, that value IS the publish time and must not be reported
    # twice under two different labels.
    rendered_at = task.rendered_at
    if rendered_at is None and finished is not None and not completed:
        rendered_at = finished
    reviewed_at = task.reviewed_at
    if reviewed_at is None and buffer is not None and buffer.status != BufferStatus.awaiting_review:
        # A reviewed item's `ready_at` is its approval (R22) — but an auto-publish render is stamped
        # `ready_at` at creation and was never reviewed, so it is only evidence of a review when it
        # is LATER than the render. That comparison needs a render time to compare against: with no
        # `rendered_at` (a pre-ADR-091 published row) the fallback would read the buffer's creation
        # stamp as an approval and invent a review that never happened. Blank beats invented.
        ready = buffer.ready_at
        if ready is not None and rendered_at is not None and ready > rendered_at:
            reviewed_at = ready

    out = [
        Milestone("queued", STAGE_LABELS["queued"], task.created_at, _local(task.created_at, tz),
                  note="render job enqueued"),
        Milestone("started", STAGE_LABELS["started"], task.started_at,
                  _local(task.started_at, tz), note="the worker picked it up"),
        Milestone("rendered", STAGE_LABELS["rendered"], rendered_at, _local(rendered_at, tz),
                  note="master finished encoding"),
        Milestone("reviewed", STAGE_LABELS["reviewed"], reviewed_at, _local(reviewed_at, tz),
                  note="approved — the gate was passed"),
    ]
    if published_at is not None:
        out.append(Milestone("published", STAGE_LABELS["published"], published_at,
                             _local(published_at, tz), note="live on the platform"))
        return out
    due, note = _scheduled_for(db, task, campaign, buffer)
    out.append(Milestone("scheduled", STAGE_LABELS["scheduled"], due, _local(due, tz),
                         estimated=True, note=note))
    out.append(Milestone("published", STAGE_LABELS["published"], None, None, estimated=True))
    return out
