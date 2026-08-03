"""ADR-082 — best-of compilations: the long-form money format built from work already done.

Shorts RPM is cents; long-form carries mid-rolls past 8 minutes. These tests pin the library
retention at publish (capped), the compile job (stream-copy concat + chapters + review-always),
the sentinel numbering that never advances the campaign, and the kind-aware retry routing.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest

from core import compilation


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path), raising=False)
    return tmp_path


def _campaign(session, user, channel, **cfg):
    from database.models import Campaign
    from database.types import CampaignStatus

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Sử Việt",
                   current_episode=12, total_episodes=30, status=CampaignStatus.active,
                   config_json={"language": "vi", **cfg})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    return cam


def _published(session, cam, ep, *, retention=None, views=100, synopsis=None):
    from database.models import Task
    from database.types import TaskStatus

    t = Task(campaign_id=cam.id, user_id=cam.user_id, episode_number=ep,
             status=TaskStatus.COMPLETED, published_video_id=f"v{ep}",
             finished_at=dt.datetime.utcnow() - dt.timedelta(days=ep),
             synopsis=synopsis or f"Chuyện tập {ep}",
             stats_json={"views": views, "avg_pct_viewed": retention})
    session.add(t)
    session.commit()
    return t


def test_retain_master_moves_into_the_library_and_caps_it(media_root):
    src = media_root / "ep.mp4"
    for n in range(1, compilation.LIBRARY_CAP_PER_CAMPAIGN + 3):
        src.write_bytes(b"vid%d" % n)
        dest = compilation.retain_master(7, n, str(src))
        assert dest and os.path.exists(dest) and not src.exists()
    kept = sorted(os.listdir(compilation.library_dir(7)))
    assert len(kept) == compilation.LIBRARY_CAP_PER_CAMPAIGN     # oldest trimmed
    assert "ep_1.mp4" not in kept and f"ep_{compilation.LIBRARY_CAP_PER_CAMPAIGN + 2}.mp4" in kept

    # Fail-open: a missing source is a no-op, never an error.
    assert compilation.retain_master(7, 99, str(media_root / "gone.mp4")) is None


def test_compilable_episodes_best_retention_first_library_only(session, user, channel, media_root):
    cam = _campaign(session, user, channel)
    for ep, ret in ((1, 40.0), (2, 70.0), (3, 55.0), (4, None)):
        _published(session, cam, ep, retention=ret)
        if ep != 3:  # ep 3's master never made it into the library
            os.makedirs(compilation.library_dir(cam.id), exist_ok=True)
            open(compilation.episode_master_path(cam.id, ep), "wb").write(b"x")
    picked = compilation.compilable_episodes(session, cam)
    assert [t.episode_number for t in picked] == [2, 1, 4]       # by retention; unmeasured last
    # And a compilation task itself is never raw material for another compilation.
    from database.models import Task
    from database.types import TaskStatus

    session.add(Task(campaign_id=cam.id, user_id=user.id, episode_number=9001,
                     video_kind="compilation", status=TaskStatus.COMPLETED,
                     stats_json={"views": 1}))
    session.commit()
    assert [t.episode_number for t in compilation.compilable_episodes(session, cam)] == [2, 1, 4]


def test_metadata_builds_chapters_in_campaign_language(session, user, channel):
    cam = _campaign(session, user, channel)
    t1 = _published(session, cam, 1, synopsis="Vị vua cuối cùng")
    t2 = _published(session, cam, 2, synopsis="Chiếu dời đô")
    md = compilation.compilation_metadata(cam, [t1, t2], [65.0, 58.0])
    assert md["title"].startswith("Tuyển tập hay nhất: Sử Việt")
    assert md["video_format"] == "long"                          # a normal video, never a Reel
    lines = md["description"].splitlines()
    assert lines[0] == "0:00 Vị vua cuối cùng" and lines[1] == "1:05 Chiếu dời đô"


def test_compile_task_concats_parks_for_review_and_never_advances(session, user, channel,
                                                                  media_root, monkeypatch):
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, TaskStatus
    from workers import video_worker

    cam = _campaign(session, user, channel)
    os.makedirs(compilation.library_dir(cam.id), exist_ok=True)
    for ep in (1, 2, 3):
        _published(session, cam, ep, retention=50.0 + ep)
        open(compilation.episode_master_path(cam.id, ep), "wb").write(b"seg")
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=9001,
             video_kind="compilation", render_json={"top_n": 2})
    session.add(t)
    session.commit()
    session.refresh(t)

    ran = {}

    def fake_ffmpeg(args, **k):
        ran["args"] = list(args)
        open(args[-1], "wb").write(b"MASTER")

    monkeypatch.setattr("core.ffmpeg_runner.run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr("core.media.probe_duration", lambda p: 60.0)
    monkeypatch.setattr("core.thumbnail.generate_thumbnail",
                        lambda v, out, title, **k: open(out, "wb").write(b"J") or out)
    notes = []
    monkeypatch.setattr(video_worker, "_notify", lambda u, m: notes.append(m))

    video_worker.compile_task(t.id)
    session.refresh(t)
    assert t.status == TaskStatus.AWAITING_REVIEW                # review-always, every mode
    assert "-c" in ran["args"] and "copy" in ran["args"]         # stream copy — no re-encode
    buf = session.query(BufferPoolItem).filter_by(campaign_id=cam.id, episode_number=9001).one()
    assert buf.status == BufferStatus.awaiting_review
    assert buf.metadata_json["video_format"] == "long"
    assert buf.metadata_json["compiled_from"] == [3, 2]          # top-2 by retention
    assert session.get(Campaign, cam.id).current_episode == 12   # campaign untouched
    assert any("review" in n for n in notes)


def test_too_thin_a_library_fails_honestly(session, user, channel, media_root, monkeypatch):
    from database.models import Task
    from database.types import TaskStatus
    from workers import video_worker

    cam = _campaign(session, user, channel)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=9001, video_kind="compilation")
    session.add(t)
    session.commit()
    monkeypatch.setattr(video_worker, "_notify", lambda u, m: None)
    video_worker.compile_task(t.id)
    session.refresh(t)
    assert t.status == TaskStatus.FAILED
    assert "compilable episode" in t.error_message


def test_apply_compile_action_creates_and_queues_the_build(session, user, channel, monkeypatch):
    from database.models import AutopilotAction, Task
    from workers import scheduler

    cam = _campaign(session, user, channel)
    queued = []
    monkeypatch.setattr(scheduler.task_queue, "enqueue_compile",
                        lambda tid: queued.append(tid) or "job-c")
    a = AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=cam.id,
                        kind="compile", summary="build a best-of", params={"top_n": 8})
    session.add(a)
    session.commit()
    assert scheduler.apply_autopilot_action(session, a) is True
    t = session.query(Task).filter_by(campaign_id=cam.id, video_kind="compilation").one()
    assert t.episode_number == 9001 and queued == [t.id]
    assert t.render_json["top_n"] == 8 and a.params["created_task_id"] == t.id

    # A second approved compile numbers itself after the first — unique(campaign, episode) holds.
    b = AutopilotAction(user_id=user.id, channel_id=channel.id, campaign_id=cam.id,
                        kind="compile", summary="another", params={})
    session.add(b)
    session.commit()
    assert scheduler.apply_autopilot_action(session, b) is True
    nums = sorted(x.episode_number for x in
                  session.query(Task).filter_by(video_kind="compilation").all())
    assert nums == [9001, 9002]


def test_retry_routes_by_kind(session, user, channel, monkeypatch):
    from database.models import Task
    from workers import video_worker

    cam = _campaign(session, user, channel)
    comp = Task(campaign_id=cam.id, user_id=user.id, episode_number=9001,
                video_kind="compilation")
    ep = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add_all([comp, ep])
    session.commit()
    routed = []
    monkeypatch.setattr("workers.task_queue.enqueue_compile",
                        lambda tid: routed.append(("compile", tid)) or "jc")
    monkeypatch.setattr(video_worker, "enqueue_render",
                        lambda tid: routed.append(("render", tid)) or "jr")
    video_worker.enqueue_task(comp)
    video_worker.enqueue_task(ep)
    assert routed == [("compile", comp.id), ("render", ep.id)]


def test_pending_compilation_does_not_shrink_the_episode_buffer(session, user, channel):
    from database.models import Task
    from database.types import TaskStatus
    from workers import video_worker

    cam = _campaign(session, user, channel)
    session.add(Task(campaign_id=cam.id, user_id=user.id, episode_number=9001,
                     video_kind="compilation", status=TaskStatus.AWAITING_REVIEW))
    session.commit()
    created = video_worker.hydrate_campaign(session, cam, buffer_size=2, enqueue=lambda t: "j")
    assert len(created) == 2                                     # the sentinel didn't count
