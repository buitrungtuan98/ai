"""One answer to "did this episode's work actually finish?" — asked BEFORE anything marks it FAILED.

The failure writers (watchdog stall path, boot recovery, the stuck-task reaper, the autopilot's
retry pass) used to judge a task by its status alone. But the render pipeline commits its evidence
first — the buffer row with the finished video, the published_video_id after an upload — and the
task's own status commit can be lost to a crash in between. Judging by status alone then produced
the operator-facing contradiction of R22's incident: an episode "Failed — the worker stopped making
progress" sitting above its own finished, QC-passed video with an Approve button.

DRY: every failure writer and every retry path calls `reconcile_task_outcome` first; only when it
returns None is FAILED an honest verdict.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from sqlalchemy import select

from database.models import BufferPoolItem, Task
from database.types import BufferStatus, TaskStatus

logger = logging.getLogger(__name__)

# Statuses whose work reconcile may finalize. CANCELLED is deliberately absent everywhere in this
# module: an operator's decision is never overridden by bookkeeping (ADR-064).
RECONCILABLE = (TaskStatus.PENDING_QUEUE, TaskStatus.AI_GENERATION, TaskStatus.AUDIO_SYNCED,
                TaskStatus.RENDERING, TaskStatus.PUBLISHING, TaskStatus.FAILED,
                TaskStatus.SCHEDULED)


def live_buffer(db, task: Task) -> BufferPoolItem | None:
    """This episode's newest buffer row that still holds a real video file, or None."""
    buf = db.scalar(select(BufferPoolItem).where(
        BufferPoolItem.campaign_id == task.campaign_id,
        BufferPoolItem.episode_number == task.episode_number,
        BufferPoolItem.status.in_([BufferStatus.ready, BufferStatus.awaiting_review]),
    ).order_by(BufferPoolItem.id.desc()))
    if buf is not None and buf.video_path and os.path.exists(buf.video_path):
        return buf
    return None


def reconcile_task_outcome(db, task: Task, *, now: datetime | None = None) -> str | None:
    """Repair a task whose recorded status lags the work that actually happened. Returns what the
    evidence proved ('published' | 'review' | 'scheduled') and leaves the row committed-ready
    (caller commits), or None when there is nothing to prove and failing is honest.

    - `published_video_id` set (or a consumed buffer row): the upload landed — the task is
      COMPLETED, whatever interrupted the bookkeeping afterwards. Never re-render it: that is how
      one episode ends up on the channel twice.
    - a live `awaiting_review` buffer: the render finished and parked for review — the honest state
      is AWAITING_REVIEW, not FAILED; there is nothing to retry.
    - a live `ready` buffer: rendered and approved (or pre-rendered for a slot) — the honest state
      is SCHEDULED; the publish path re-attempts the upload with the duplicate guard armed.
    """
    now = now or datetime.utcnow()
    if task.status not in RECONCILABLE:
        return None
    consumed = task.published_video_id or db.scalar(
        select(BufferPoolItem.id).where(
            BufferPoolItem.campaign_id == task.campaign_id,
            BufferPoolItem.episode_number == task.episode_number,
            BufferPoolItem.status == BufferStatus.consumed))
    if consumed:
        if task.status != TaskStatus.COMPLETED:
            task.status = TaskStatus.COMPLETED
            task.progress_pct = 100
            task.error_message = None
            task.finished_at = task.finished_at or now
            logger.warning("Reconcile: task %s had already published — finalized COMPLETED", task.id)
        return "published"
    buf = live_buffer(db, task)
    if buf is None:
        return None
    if buf.status == BufferStatus.awaiting_review:
        if task.status != TaskStatus.AWAITING_REVIEW:
            task.status = TaskStatus.AWAITING_REVIEW
            task.progress_pct = 90
            task.error_message = None
            task.finished_at = task.finished_at or now
            logger.warning("Reconcile: task %s has a finished render parked for review — "
                           "restored AWAITING_REVIEW", task.id)
        return "review"
    if task.status not in (TaskStatus.SCHEDULED, TaskStatus.PUBLISHING):
        task.status = TaskStatus.SCHEDULED
        task.progress_pct = 90
        task.error_message = None
        task.finished_at = task.finished_at or now
        logger.warning("Reconcile: task %s has a finished, publish-ready render — "
                       "restored SCHEDULED", task.id)
    elif task.status == TaskStatus.PUBLISHING:
        # The worker died mid-upload. The buffer keeps the file, so SCHEDULED is honest — the next
        # publish attempt re-checks the platform for a duplicate before uploading again.
        task.status = TaskStatus.SCHEDULED
        task.progress_pct = 90
        task.error_message = None
    return "scheduled"
