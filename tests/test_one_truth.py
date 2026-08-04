"""R1 — one truth: a single attention count, one stage vocabulary, and CANCELLED as its own state.

These are the invariants the UX audit showed were broken: four badges reported four numbers for the
same facts, one episode could read as two stages at once, and an operator's cancel looked like a
system failure (and came back on its own).
"""
from __future__ import annotations

from datetime import datetime

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def _campaign(session, user, channel, topic="Truth", **cfg):
    from database.models import Campaign
    from database.types import CampaignStatus

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name=topic, total_episodes=20,
                 status=cfg.pop("status", CampaignStatus.active), config_json=cfg)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _task(session, campaign, user, episode, **kw):
    from database.models import Task

    t = Task(campaign_id=campaign.id, user_id=user.id, episode_number=episode, **kw)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def _buffer(session, campaign, channel, episode, status, path=None):
    import tempfile

    from database.models import BufferPoolItem

    # A real file by default (R22): apply_approve and the retry paths verify the video exists on
    # disk before acting on it. Pass an explicit path to model a vanished file.
    if path is None:
        f = tempfile.NamedTemporaryFile(suffix=f"-ep{episode}.mp4", delete=False)
        f.write(b"video-bytes")
        f.close()
        path = f.name
    b = BufferPoolItem(campaign_id=campaign.id, channel_id=channel.id, episode_number=episode,
                       video_path=path, status=status, metadata_json={"title": f"Ep {episode}"})
    session.add(b)
    session.commit()
    session.refresh(b)
    return b


# ── One attention count ──────────────────────────────────────────────────────
def test_every_badge_reads_the_same_attention_number(client, session, user, channel, monkeypatch):
    """The bell, /api/summary and the dashboard triage pill must agree — they used to differ because
    the bell counted its (grouped) rows while the others counted items."""
    import main
    from database.models import AutopilotAction
    from database.types import BufferStatus, TaskStatus
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: True)  # no infra rows in the way
    camp = _campaign(session, user, channel)
    _task(session, camp, user, 1, status=TaskStatus.FAILED, finished_at=datetime.utcnow(),
          error_message="boom")
    _buffer(session, camp, channel, 2, BufferStatus.awaiting_review)
    _buffer(session, camp, channel, 3, BufferStatus.awaiting_review)
    session.add(AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=camp.id,
                                kind="extend", summary="s", evidence={}, params={}))
    session.commit()

    expected = 1 + 2 + 1                      # failed + awaiting review + open proposals
    assert main._attention_count(session, user.id) == expected
    assert client.get("/api/summary").json()["attention"] == expected
    # The bell badges the SAME number even though it groups the two reviews into ONE row — which is
    # precisely why its row count must not be the badge.
    alerts = client.get("/api/alerts").json()
    assert alerts["attention"] == expected
    assert alerts["actionable"] == 3          # failed + "2 episodes waiting" + proposal
    assert alerts["actionable"] != expected
    body = client.get("/").text
    assert f'id="triage-count">{expected}<' in body


def test_a_cancelled_episode_is_not_asking_for_attention(client, session, user, channel):
    import main
    from database.types import TaskStatus

    camp = _campaign(session, user, channel)
    _task(session, camp, user, 1, status=TaskStatus.CANCELLED, finished_at=datetime.utcnow(),
          error_message="Cancelled from the Operations page before it started.")

    assert main._attention_count(session, user.id) == 0
    assert client.get("/api/summary").json()["counts"]["failed"] == 0
    assert "Cancelled" not in client.get("/api/alerts").text


# ── CANCELLED semantics ──────────────────────────────────────────────────────
def test_cancelled_is_excluded_from_the_failure_kpi(session, user, channel):
    """The vitals card said "25% failed" because a deliberate cancel counted as a failed render."""
    import main
    from database.types import TaskStatus

    camp = _campaign(session, user, channel)
    now = datetime.utcnow()
    _task(session, camp, user, 1, status=TaskStatus.COMPLETED, finished_at=now)
    _task(session, camp, user, 2, status=TaskStatus.CANCELLED, finished_at=now)

    v = main._factory_vitals(session, user.id)
    assert v["failed_today"] == 0 and v["fail_pct_today"] == 0


def test_autopilot_never_resurrects_a_cancelled_episode(session, user, channel):
    """A cancel that comes back a few minutes later is worse than no cancel button at all."""
    from database.types import TaskStatus
    from workers import scheduler as sch

    camp = _campaign(session, user, channel)
    cancelled = _task(session, camp, user, 1, status=TaskStatus.CANCELLED,
                      finished_at=datetime.utcnow(), error_message="Cancelled from the Operations page")
    genuine = _task(session, camp, user, 2, status=TaskStatus.FAILED,
                    finished_at=datetime.utcnow(), error_message="RuntimeError: boom")

    assert sch.autopilot_retry_channel(session, channel) == 1   # only the genuine failure
    session.refresh(cancelled)
    session.refresh(genuine)
    assert cancelled.status == TaskStatus.CANCELLED
    assert genuine.status == TaskStatus.PENDING_QUEUE


def test_a_cancelled_episode_can_still_be_retried(client, session, user, channel):
    from database.types import TaskStatus

    camp = _campaign(session, user, channel)
    t = _task(session, camp, user, 1, status=TaskStatus.CANCELLED, finished_at=datetime.utcnow())

    r = client.post(f"/api/tasks/{t.id}/retry")
    assert r.status_code == 200
    session.refresh(t)
    assert t.status == TaskStatus.PENDING_QUEUE


def test_cancelling_frees_the_episode_slot_for_hydration(session, user, channel):
    """A CANCELLED episode is a finished outcome: leaving it "active" starved the buffer forever."""
    from database.models import Task
    from database.types import TaskStatus
    from workers import video_worker

    camp = _campaign(session, user, channel)
    _task(session, camp, user, 1, status=TaskStatus.CANCELLED, finished_at=datetime.utcnow())

    created = video_worker.hydrate_campaign(session, camp, buffer_size=1, enqueue=lambda tid: f"j{tid}")
    assert created, "hydration should queue a fresh episode once the cancelled one is out of the way"
    assert session.get(Task, created[0]).episode_number != 1


def test_cancelled_shows_as_its_own_stage_with_a_neutral_pill(client, session, user, channel):
    from database.types import TaskStatus

    camp = _campaign(session, user, channel, topic="Cancelled Stage")
    _task(session, camp, user, 4, status=TaskStatus.CANCELLED, finished_at=datetime.utcnow())

    body = client.get("/episodes?status=cancelled").text
    assert "Cancelled (1)" in body            # its own chip, only shown when non-zero
    assert "Ep 4" in body
    assert "pill CANCELLED" in body           # grey, not the red FAILED styling


# ── One stage per episode ────────────────────────────────────────────────────
def test_approving_removes_the_episode_from_the_review_queue_at_once(session, user, channel):
    """It used to stay `awaiting_review` until the upload finished, so one episode read as both
    "approved" and "still waiting for review" — and invited a second approve click."""
    from database.types import BufferStatus, TaskStatus
    from workers import task_queue, video_worker

    camp = _campaign(session, user, channel)
    task = _task(session, camp, user, 5, status=TaskStatus.AWAITING_REVIEW, progress_pct=90)
    item = _buffer(session, camp, channel, 5, BufferStatus.awaiting_review)

    video_worker.apply_approve(session, item)

    session.refresh(item)
    session.refresh(task)
    assert item.status == BufferStatus.ready
    # SCHEDULED, not PENDING_QUEUE: it is rendered and waiting to go out, and calling it "queued"
    # both mislabelled the stage and inflated the render-queue count with a non-render job.
    assert task.status == TaskStatus.SCHEDULED
    assert len(task_queue.queued_jobs()) == 1       # the publish job really was enqueued


def test_an_approved_episode_no_longer_counts_as_awaiting_review(client, session, user, channel):
    import main
    from database.types import BufferStatus, TaskStatus
    from workers import video_worker

    camp = _campaign(session, user, channel)
    _task(session, camp, user, 5, status=TaskStatus.AWAITING_REVIEW)
    item = _buffer(session, camp, channel, 5, BufferStatus.awaiting_review)
    assert main._task_counts(session, user.id)["awaiting_review"] == 1

    video_worker.apply_approve(session, item)
    assert main._task_counts(session, user.id)["awaiting_review"] == 0
    assert client.get("/api/summary").json()["attention"] == 0


def test_the_stage_vocabulary_has_one_word_per_stage(client, session, user, channel):
    """"Pending Queue"/"In pipeline"/"Completed" were synonyms for stages that already had names."""
    import pathlib

    app_js = pathlib.Path("static/app.js").read_text()
    labels = app_js.split("var STAGE_LABELS = {", 1)[1].split("};", 1)[0]
    assert 'PENDING_QUEUE: "Queued"' in labels
    assert 'COMPLETED: "Published"' in labels
    assert 'CANCELLED: "Cancelled"' in labels
    # The retired synonyms must be gone from the map itself (the comment above it may name them).
    for retired in ("Pending Queue", "Audio Synced", "Completed", "Awaiting Review", "AI Generation"):
        assert retired not in labels, f"{retired} is a synonym for a stage that already has a name"


def test_no_dead_element_hooks_remain_in_ui_js():
    """ui.js was still writing to #hv-buffer and toggling #banner-failed/#banner-review, which no
    template renders — leftovers from a removed surface."""
    import pathlib

    ui_js = pathlib.Path("static/ui.js").read_text()
    for dead in ("hv-buffer", "banner-failed", "banner-review"):
        assert dead not in ui_js, f"{dead} has no element to write to"


def test_the_breadcrumb_renders_only_in_the_app_bar(client, session, user, channel):
    """tasks.html kept a second, content-flow copy for campaign scope (ADR-061 says once)."""
    camp = _campaign(session, user, channel, topic="Crumb Once")
    _task(session, camp, user, 1)

    body = client.get(f"/tasks?campaign={camp.id}").text
    assert body.count('class="crumbs"') == 1
    assert "Crumb Once" in body               # the trail really did render


def test_the_campaign_counter_says_what_it_counts(client, session, user, channel):
    """"Episode 0 / 30" sat beside a "32 Episodes" tile and could not be decoded."""
    camp = _campaign(session, user, channel, topic="Counter")
    body = client.get(f"/campaigns/{camp.id}").text
    assert "of 20 published" in body
    assert "Episode 0 / 20" not in body


# ── R2: one episode list (ADR-065) ───────────────────────────────────────────
def test_the_review_chip_counts_the_actual_review_queue(client, session, user, channel):
    """The audit's worst finding: the chip read "Review (0)" while two videos sat waiting, because it
    counted Task.status while the queue itself lives in the buffer."""
    import main
    from database.types import BufferStatus, TaskStatus

    camp = _campaign(session, user, channel, topic="Chip Truth")
    # Drift case 1: a Retry moved the task on, but the video is still awaiting approval.
    _task(session, camp, user, 5, status=TaskStatus.PENDING_QUEUE, progress_pct=90)
    _buffer(session, camp, channel, 5, BufferStatus.awaiting_review)
    # Drift case 2: the normal case, both in step.
    _task(session, camp, user, 6, status=TaskStatus.AWAITING_REVIEW)
    _buffer(session, camp, channel, 6, BufferStatus.awaiting_review)

    counts = main._episode_stage_counts(session, user)
    assert counts["review"] == 2                      # the queue, not the task column
    assert counts["review"] == main._task_counts(session, user.id)["awaiting_review"]
    # ...and the drifted episode is NOT also counted as queued (one episode, one stage).
    assert counts["queued"] == 0
    body = client.get("/episodes").text
    assert "Review (2)" in body and "Queued (0)" in body


def test_the_review_filter_lists_what_is_really_awaiting_approval(client, session, user, channel):
    from database.types import BufferStatus, TaskStatus

    camp = _campaign(session, user, channel, topic="Review Filter")
    _task(session, camp, user, 5, status=TaskStatus.PENDING_QUEUE)   # drifted task…
    _buffer(session, camp, channel, 5, BufferStatus.awaiting_review)  # …but really in review
    _task(session, camp, user, 9, status=TaskStatus.PENDING_QUEUE)   # genuinely queued

    review = client.get("/episodes?status=review").text
    assert "Ep 5" in review and "Ep 9" not in review
    queued = client.get("/episodes?status=queued").text
    assert "Ep 9" in queued and "Ep 5" not in queued   # never in two stages at once


def test_the_live_rows_carry_a_hook_only_while_they_can_move(client, session, user, channel):
    """Settled rows must not be polled — that was half the point of retiring the second table."""
    from database.types import TaskStatus

    camp = _campaign(session, user, channel, topic="Live Hook")
    _task(session, camp, user, 1, status=TaskStatus.RENDERING, progress_pct=42)
    _task(session, camp, user, 2, status=TaskStatus.COMPLETED, finished_at=datetime.utcnow())

    body = client.get("/episodes").text
    assert body.count("data-live-task=") == 1          # only the rendering episode
    assert 'data-live="pill"' in body and 'data-live="progress"' in body


def test_api_tasks_live_returns_only_working_episodes(client, session, user, channel):
    """A rendering episode can sit behind hundreds of published ones; paging to find it was wrong."""
    from database.types import TaskStatus

    camp = _campaign(session, user, channel)
    _task(session, camp, user, 1, status=TaskStatus.RENDERING, progress_pct=10)
    for ep in range(2, 12):
        _task(session, camp, user, ep, status=TaskStatus.COMPLETED, finished_at=datetime.utcnow())

    live = client.get("/api/tasks?live=1").json()["tasks"]
    assert [t["episode"] for t in live] == [1]
    assert len(client.get("/api/tasks").json()["tasks"]) == 11   # unfiltered is unchanged


def test_banners_render_as_prose_not_columns():
    """`display:flex` made every inline child its own column — a sentence became side-by-side
    fragments, unreadable at 375px."""
    import pathlib

    css = pathlib.Path("static/app.css").read_text()
    rule = css.split(".banner {", 1)[1].split("}", 1)[0]
    assert "display: block" in rule and "display: flex" not in rule


# ── R3: an honest, short dashboard (ADR-066) ─────────────────────────────────
def test_runway_reports_the_worst_campaign_not_a_soothing_average(client, session, user, channel):
    """"≈1.0 day of runway" read as fine while two campaigns were about to miss tonight's slots."""
    import main
    from database.models import BufferPoolItem
    from database.types import BufferStatus

    stocked = _campaign(session, user, channel, topic="Stocked", posting_slots=["21:00"])
    _campaign(session, user, channel, topic="Empty A", posting_slots=["20:00"])
    _campaign(session, user, channel, topic="Empty B", posting_slots=["11:30"])
    session.add(BufferPoolItem(campaign_id=stocked.id, channel_id=channel.id, episode_number=1,
                               video_path="/v.mp4", status=BufferStatus.ready, metadata_json={}))
    session.commit()

    assert main._campaigns_with_empty_buffer(session, user.id) == 2
    body = " ".join(client.get("/").text.split())
    assert "2<span class=\"u\"> at zero</span>".replace('"', '"') in body or "at zero" in body
    assert "those slots will be missed" in body


def test_all_clear_never_shows_beside_a_red_degraded_banner(client, session, user, channel, monkeypatch):
    """They used to render together: a green "everything is fine" directly under "factory degraded"."""
    from workers import task_queue

    monkeypatch.setattr(task_queue, "worker_alive", lambda: False)
    body = client.get("/").text
    assert "The factory is degraded" in body
    allclear = body.split('id="allclear-card"', 1)[1].split(">", 1)[0]
    assert "hidden" in allclear


def test_repeated_activity_rows_collapse_into_one(session, user, channel):
    """A burst of publishes was ten near-identical lines — the last two phone screens of the page."""
    import main
    from database.types import TaskStatus

    camp = _campaign(session, user, channel, topic="Burst")
    tasks = [_task(session, camp, user, ep, status=TaskStatus.COMPLETED,
                   finished_at=datetime.utcnow()) for ep in (519, 520, 521)]
    tasks.append(_task(session, camp, user, 522, status=TaskStatus.FAILED,
                       finished_at=datetime.utcnow(), error_message="boom"))

    feed = main._activity_feed(list(reversed(tasks)), {camp.id: camp}, {channel.id: channel})
    assert len(feed) == 2                                  # one failure row + one publish run
    assert feed[1]["count"] == 3 and feed[1]["episodes"] == [521, 520, 519]


def test_the_scope_switcher_is_visibly_inert_on_the_whole_factory_dashboard(client, session, user, channel):
    """Selecting a channel there changed nothing while every number still showed the whole factory."""
    body = client.get("/").text
    switcher = body.split('id="scope-switcher"', 1)[1].split(">", 1)[0]
    assert "disabled" in switcher
    assert "Whole factory" in body
    # On a scoped page it works normally.
    assert "disabled" not in client.get("/episodes").text.split('id="scope-switcher"', 1)[1].split(">", 1)[0]


# ── R4: one scheduling surface (ADR-067) ─────────────────────────────────────
def test_the_calendar_uses_the_shared_slot_projection(session, user, channel):
    """It used to re-implement the scheduler's assignment rule with its own day-walk and pool.pop —
    the only duplicated business rule in the codebase, and it had already drifted."""
    import main

    camp = _campaign(session, user, channel, posting_slots=["09:00", "21:00"], timezone="UTC")
    rows = main._calendar_row_cells(camp, [4, 5])
    filled = [(c["t"], c["ep"]) for day in rows for c in day["slots"] if c["state"] == "filled"]
    upcoming = main._upcoming_slots(camp, 2)
    # Same slots, same order, lowest episode first — because both read _upcoming_slots.
    assert [t for t, _ in filled][:2] == [d.strftime("%H:%M") for d in upcoming]
    assert [ep for _, ep in filled][:2] == [4, 5]


def test_a_rescheduled_episode_is_drawn_on_the_grid_and_counted(client, session, user, channel):
    """It vanished from the one page whose job is "what publishes when", and the ready count dropped."""
    from datetime import timedelta

    from database.types import BufferStatus

    camp = _campaign(session, user, channel, topic="Own Time", posting_slots=["21:00"], timezone="UTC")
    item = _buffer(session, camp, channel, 7, BufferStatus.ready)
    item.publish_at = datetime.utcnow() + timedelta(hours=20)
    session.commit()

    body = client.get("/calendar").text
    assert "Ep 7" in body and "✏" in body
    assert "1 ready" in body and "at your own time" in body


def test_the_publish_queue_moved_to_the_calendar(client):
    """Two pages answered "what publishes when" and their ready counts disagreed."""
    r = client.get("/operations?tab=publish", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/calendar?view=list"
    ops = client.get("/operations").text
    assert "Publish queue" not in ops                 # Operations is the machine now
    assert 'href="/calendar?view=list"' in ops        # …and points at the scheduling surface
    cal = client.get("/calendar?view=list").text
    assert "Week grid" in cal and "List &amp; actions" in cal


def test_publish_now_asks_before_it_publishes(client, session, user, channel, tmp_path):
    """The most irreversible action in the app was a bare POST here, while /assets always confirmed."""
    from database.types import BufferStatus

    camp = _campaign(session, user, channel, posting_slots=["21:00"])
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    item = _buffer(session, camp, channel, 1, BufferStatus.ready, path=str(f))

    body = client.get("/calendar?view=list").text
    # The guard must be on THIS form, not merely somewhere on the page.
    form = body.split(f'action="/assets/{item.id}/publish-now"', 1)[1].split(">", 1)[0]
    assert "data-confirm=" in form
    assert "can&#39;t be undone" in form or "can't be undone" in form
