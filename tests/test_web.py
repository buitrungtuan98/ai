"""Web app (solo mode): pages render, forms persist (encrypted), campaign start queues, 404 guard."""
from __future__ import annotations

import os
import sqlite3

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_ranged_response_suffix_and_unsatisfiable(tmp_path):
    """The preview streamer handles suffix ranges (last N bytes) and rejects unsatisfiable ranges
    with a 416 instead of a broken 206 with a negative Content-Length."""
    import main

    f = tmp_path / "v.bin"
    f.write_bytes(b"0123456789")  # 10 bytes

    def req(rng):
        return type("R", (), {"headers": {"range": rng}})()

    r = main._ranged_file_response(str(f), req("bytes=0-"), "application/octet-stream")
    assert r.status_code == 206 and r.headers["Content-Range"] == "bytes 0-9/10"

    r = main._ranged_file_response(str(f), req("bytes=-3"), "application/octet-stream")  # last 3 bytes
    assert r.status_code == 206 and r.headers["Content-Range"] == "bytes 7-9/10"
    assert r.headers["Content-Length"] == "3"

    r = main._ranged_file_response(str(f), req("bytes=1000-"), "application/octet-stream")  # past EOF
    assert r.status_code == 416 and r.headers["Content-Range"] == "bytes */10"


def test_all_pages_render(client):
    for path in ["/", "/channels", "/campaigns", "/campaigns/new", "/credentials", "/assets", "/tasks"]:
        r = client.get(path)
        assert r.status_code == 200 and "AI Video Factory" in r.text


def test_add_facebook_channel_encrypts(client):
    r = client.post("/channels/facebook",
                    data={"channel_name": "My Page", "page_id": "P1", "page_access_token": "sekret"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/channels"
    assert "My Page" in client.get("/channels").text
    db_path = os.environ["DATABASE_URL"].replace("sqlite:///", "")
    raw = sqlite3.connect(db_path).execute("SELECT encrypted_credentials FROM channels").fetchone()[0]
    assert "sekret" not in raw and raw.startswith("gAAAA")


def test_create_and_start_campaign_queues(client):
    from database.db_session import SessionLocal
    from database.models import Campaign, Channel

    client.post("/channels/facebook", data={"channel_name": "P", "page_id": "1", "page_access_token": "t"},
                follow_redirects=False)
    db = SessionLocal()
    cid = db.query(Channel).first().id
    db.close()
    client.post("/campaigns", data={"topic_name": "Space", "channel_id": str(cid), "total_episodes": "5",
                                    "language": "en", "cta": "Follow"}, follow_redirects=False)
    db = SessionLocal()
    camid = db.query(Campaign).first().id
    db.close()
    assert client.post(f"/campaigns/{camid}/start", follow_redirects=False).status_code == 303
    tasks = client.get("/api/tasks").json()["tasks"]
    assert len(tasks) >= 1 and tasks[0]["status"] == "PENDING_QUEUE"


def test_ownership_guard_404(client):
    assert client.post("/campaigns/99999/delete", follow_redirects=False).status_code == 404


def test_campaign_form_voice_dropdown(client):
    """The voice picker is a per-language dropdown fed by the ONE catalog in core/tts.py; an
    edit form keeps the saved voice via data-current (JS re-selects it on load)."""
    client.post("/channels/facebook", data={"channel_name": "P", "page_id": "1", "page_access_token": "t"},
                follow_redirects=False)
    page = client.get("/campaigns/new")
    assert 'id="voice-select"' in page.text
    assert "vi-VN-HoaiMyNeural" in page.text and "en-US-AriaNeural" in page.text  # catalog JSON

    from database.db_session import SessionLocal
    from database.models import Campaign, Channel

    db = SessionLocal()
    ch = db.query(Channel).first()
    cam = Campaign(user_id=ch.user_id, channel_id=ch.id, topic_name="V", total_episodes=3,
                   config_json={"language": "vi", "voice": "vi-VN-NamMinhNeural"})
    db.add(cam)
    db.commit()
    db.refresh(cam)
    db.close()
    edit = client.get(f"/campaigns/{cam.id}/edit")
    assert 'data-current="vi-VN-NamMinhNeural"' in edit.text


def test_gemini_model_chain_save_semantics(client):
    """The model chain is NOT a secret: submitted value replaces, blank resets to the server
    default, an absent field (the keys-only form) keeps the stored value."""
    from database.db_session import SessionLocal
    from database.models import User

    def stored():
        db = SessionLocal()
        v = db.query(User).first().gemini_model
        db.close()
        return v

    client.post("/credentials", data={"gemini_model": " gemini-3.1-flash-lite ,  gemini-flash-latest "},
                follow_redirects=False)
    assert stored() == "gemini-3.1-flash-lite,gemini-flash-latest"  # normalized

    client.post("/credentials", data={"pexels_api_key": "p"}, follow_redirects=False)  # no field
    assert stored() == "gemini-3.1-flash-lite,gemini-flash-latest"  # kept

    client.post("/credentials", data={"gemini_model": ""}, follow_redirects=False)  # blank
    assert stored() is None  # back to the server default


def test_gemini_models_endpoint(client, monkeypatch):
    """Live model list annotated with curated free-tier limits; known-quota models sort first;
    no key anywhere → 400 with a actionable message."""
    from core import ai_engine
    from core.config import settings

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(ai_engine, "list_gemini_models", lambda *, api_key: [
        {"id": "weird-experimental", "display_name": "Weird", "description": "no known limits"},
        {"id": "gemini-3.1-flash-lite", "display_name": "Flash-Lite", "description": "fast"},
    ])
    j = client.get("/credentials/gemini-models").json()
    assert [m["id"] for m in j["models"]] == ["gemini-3.1-flash-lite", "weird-experimental"]
    assert j["models"][0]["rpd"] == 500 and j["models"][0]["rpm"] == 15  # curated limits attached
    assert j["models"][1]["rpd"] is None                                 # unknown stays unknown
    assert j["server_default"] == settings.GEMINI_MODEL

    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    assert client.get("/credentials/gemini-models").status_code == 400


def test_credentials_test_freesound(client, monkeypatch):
    """The Credentials page can live-test the server's Freesound key; a missing key explains
    itself instead of failing at render time."""
    from core.config import settings
    from services import verification

    monkeypatch.setattr(settings, "FREESOUND_API_KEY", None)
    r = client.post("/credentials/test/freesound").json()
    assert not r["ok"] and "FREESOUND_API_KEY" in r["detail"]

    monkeypatch.setattr(settings, "FREESOUND_API_KEY", "fs-key")
    monkeypatch.setattr(verification, "verify_freesound", lambda k: (True, "key ok"))
    assert client.post("/credentials/test/freesound").json()["ok"]


def _seed_ready_asset(client, tmp_path, status="ready"):
    from database.db_session import SessionLocal
    from database.models import BufferPoolItem, Campaign, Channel, Task
    from database.types import BufferStatus, CampaignStatus, TaskStatus

    client.post("/channels/facebook", data={"channel_name": "P", "page_id": "1", "page_access_token": "t"},
                follow_redirects=False)
    db = SessionLocal()
    ch = db.query(Channel).first()
    cam = Campaign(user_id=ch.user_id, channel_id=ch.id, topic_name="Slotted", total_episodes=3,
                   status=CampaignStatus.active, config_json={"posting_slots": ["21:00"]})
    db.add(cam)
    db.commit()
    db.refresh(cam)
    video = tmp_path / "ep1.mp4"
    video.write_bytes(b"vid")
    buf = BufferPoolItem(campaign_id=cam.id, channel_id=ch.id, episode_number=1,
                         video_path=str(video), metadata_json={"title": "T"},
                         status=BufferStatus[status])
    task = Task(campaign_id=cam.id, user_id=ch.user_id, episode_number=1,
                status=TaskStatus.SCHEDULED)
    db.add_all([buf, task])
    db.commit()
    db.refresh(buf)
    db.refresh(task)
    db.close()
    return buf, task, video


def test_publish_now_skips_the_slot(client, monkeypatch, tmp_path):
    """A `ready` (slot-parked) item can be published immediately from the Asset Pool."""
    import main

    buf, _task, _video = _seed_ready_asset(client, tmp_path)
    queued = []
    monkeypatch.setattr(main.task_queue, "enqueue_publish", lambda bid: queued.append(bid) or "j1")
    r = client.post(f"/assets/{buf.id}/publish-now", follow_redirects=False)
    assert r.status_code == 303 and queued == [buf.id]
    assert "flash=publish" in r.headers["location"]
    # The follow-up page explains what "queued" means (worker publishes when free).
    page = client.get(r.headers["location"]).text
    assert "Publish queued" in page

    # Guard: a consumed item can't be re-published.
    from database.db_session import SessionLocal
    from database.models import BufferPoolItem
    from database.types import BufferStatus

    db = SessionLocal()
    db.get(BufferPoolItem, buf.id).status = BufferStatus.consumed
    db.commit()
    db.close()
    assert client.post(f"/assets/{buf.id}/publish-now", follow_redirects=False).status_code == 400


def test_publish_now_missing_file_flashes_not_queues(client, monkeypatch, tmp_path):
    """If the rendered file vanished from disk, Publish now must explain (flash=missing) instead
    of queueing an upload doomed to FileNotFoundError."""
    import main

    buf, _task, video = _seed_ready_asset(client, tmp_path)
    video.unlink()  # simulate the file having been destroyed
    queued = []
    monkeypatch.setattr(main.task_queue, "enqueue_publish", lambda bid: queued.append(bid) or "j")
    r = client.post(f"/assets/{buf.id}/publish-now", follow_redirects=False)
    assert r.status_code == 303 and "flash=missing" in r.headers["location"]
    assert queued == []  # nothing enqueued
    assert "no longer on disk" in client.get(r.headers["location"]).text


def test_rerender_discards_and_requeues(client, monkeypatch, tmp_path):
    """Discard & re-render deletes the bad render and queues a fresh render of the episode."""
    import main
    from database.db_session import SessionLocal
    from database.models import BufferPoolItem, Task
    from database.types import BufferStatus, TaskStatus

    buf, task, video = _seed_ready_asset(client, tmp_path)
    renders = []
    monkeypatch.setattr(main.task_queue, "enqueue_render", lambda tid: renders.append(tid) or "j2")
    r = client.post(f"/assets/{buf.id}/rerender", follow_redirects=False)
    assert r.status_code == 303 and renders == [task.id]
    assert "flash=rerender" in r.headers["location"]
    assert not video.exists()  # bad render's file removed
    db = SessionLocal()
    assert db.get(BufferPoolItem, buf.id).status == BufferStatus.rejected
    t = db.get(Task, task.id)
    assert t.status == TaskStatus.PENDING_QUEUE and t.retry_count == 1
    db.close()


def test_preview_script_route(client, monkeypatch):
    """The dry-run returns scenes + estimated duration from the current form values (1 AI call,
    nothing rendered or stored)."""
    from core import ai_engine
    from core.config import settings

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "k")
    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        from core.ai_engine import VideoScript

        return VideoScript(
            language="vi", topic="ma", synopsis="Chuyện chiếc ghe",
            scenes=[{"index": i, "narration": "mười từ " * 5, "pexels_keywords": ["river"]}
                    for i in range(3)],
            metadata_variations=[{"variant": v, "title": "Nghe kỹ nè", "description": "d",
                                  "tags": ["a", "b", "c"]} for v in "ABC"],
        )

    monkeypatch.setattr(ai_engine, "generate_script", lambda **k: fake_generate(**k))
    r = client.post("/campaigns/preview-script",
                    data={"topic_name": "chuyện ma", "language": "vi", "persona": "Chú Ba"})
    assert r.status_code == 200
    j = r.json()
    assert len(j["scenes"]) == 3 and j["title"] == "Nghe kỹ nè" and j["est_seconds"] > 0
    assert captured["persona"] == "Chú Ba" and captured["self_critique"] is False

    # No topic → friendly 400, no AI call.
    assert client.post("/campaigns/preview-script", data={"topic_name": ""}).status_code == 400


def test_calendar_page_and_slot_cells(client):
    """The calendar shows slot times on allowed days and dashes on gated days."""

    import main
    from database.db_session import SessionLocal
    from database.models import Campaign, Channel
    from database.types import CampaignStatus
    from workers.scheduler import WEEKDAY_KEYS, local_now

    client.post("/channels/facebook", data={"channel_name": "P", "page_id": "1", "page_access_token": "t"},
                follow_redirects=False)
    db = SessionLocal()
    ch = db.query(Channel).first()
    today_key = WEEKDAY_KEYS[local_now().weekday()]
    cam = Campaign(user_id=ch.user_id, channel_id=ch.id, topic_name="CalCam", total_episodes=5,
                   status=CampaignStatus.active,
                   config_json={"posting_slots": ["21:00"], "posting_days": [today_key]})
    db.add(cam)
    db.commit()
    db.refresh(cam)

    cells = main.upcoming_slot_cells(cam)
    assert cells[0] == ["21:00"]                       # today is an allowed day
    assert [] in cells                                  # other days are gated off
    db.close()

    page = client.get("/calendar")
    assert page.status_code == 200 and "CalCam" in page.text and "21:00" in page.text


def test_propose_campaign_route(client, monkeypatch):
    """The AI designer returns a full config as JSON for the form to fill (nothing is saved)."""
    from core import ai_engine
    from core.ai_engine import CampaignProposal
    from core.config import settings

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "k")
    captured = {}

    def fake_propose(**kwargs):
        captured.update(kwargs)
        return CampaignProposal(
            topic_name="Midnight Mekong ghost tales", language="vi", total_episodes=30,
            persona="Chú Ba miền Tây kể chuyện đêm khuya", continuity="no_repeat",
            caption_theme="neon", color_grade="noir", music_mode="auto",
            music_mood="dark ambient drone", posting_slots="22:00",
            rationale="Nostalgic regional horror with a familiar storyteller.",
        )

    monkeypatch.setattr(ai_engine, "propose_campaign", lambda **k: fake_propose(**k))
    monkeypatch.setattr(settings, "FREESOUND_API_KEY", "fs-key")  # box CAN run auto music
    r = client.post("/campaigns/propose", data={"topic": "ghost stories", "language": "vi"})
    assert r.status_code == 200
    j = r.json()
    assert j["persona"].startswith("Chú Ba") and j["caption_theme"] == "neon"
    assert j["music_mode"] == "auto" and j["posting_slots"] == "22:00"
    assert captured["topic"] == "ghost stories" and captured["language"] == "vi"

    # Config truth: with no Freesound key on the box, an "auto" music proposal is downgraded to
    # "none" — the designer must never propose a mode whose every episode would fail.
    monkeypatch.setattr(settings, "FREESOUND_API_KEY", None)
    j = client.post("/campaigns/propose", data={"topic": "ghost stories", "language": "vi"}).json()
    assert j["music_mode"] == "none"


def test_propose_campaign_needs_key(client, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    r = client.post("/campaigns/propose", data={"topic": "x"})
    assert r.status_code == 400 and "Gemini" in r.json()["error"]


def _seed_campaign(client):
    from database.db_session import SessionLocal
    from database.models import Campaign, Channel

    client.post("/channels/facebook", data={"channel_name": "P", "page_id": "1", "page_access_token": "t"},
                follow_redirects=False)
    db = SessionLocal()
    cid = db.query(Channel).first().id
    db.close()
    client.post("/campaigns", data={"topic_name": "Space", "channel_id": str(cid), "total_episodes": "5",
                                    "language": "en", "publish_mode": "review", "privacy": "unlisted",
                                    "buffer_size": "2", "watermark_path": "/data/logo.png",
                                    "tint_opacity": "0.1", "tint_color": "#1e90ff",
                                    "color_grade": "cinematic", "auto_qc": "off",
                                    "posting_days": ["mon", "fri", "bogus-day"],
                                    "duration_min_s": "60", "duration_max_s": "30"},
                follow_redirects=False)
    db = SessionLocal()
    cam = db.query(Campaign).first()
    db.close()
    return cam


def test_campaign_config_persists_all_settings(client):
    cam = _seed_campaign(client)
    cfg = cam.config_json
    assert cfg["auto_publish"] is False          # review mode
    assert cfg["privacy"] == "unlisted"
    assert cfg["buffer_size"] == 2
    assert cfg["branding"]["watermark_path"] == "/data/logo.png"
    assert cfg["branding"]["tint_opacity"] == 0.1
    assert cfg["color_grade"] == "cinematic" and cfg["auto_qc"] == "off"  # Phase 17 fields honored
    assert cfg["posting_days"] == ["mon", "fri"]  # weekday gate persisted; bogus value dropped
    assert (cfg["duration_min_s"], cfg["duration_max_s"]) == (30, 60)  # reversed bounds auto-ordered


def test_persona_and_continuity_persist_and_duplicate(client):
    from database.db_session import SessionLocal
    from database.models import Campaign, Channel

    client.post("/channels/facebook", data={"channel_name": "P", "page_id": "1", "page_access_token": "t"},
                follow_redirects=False)
    db = SessionLocal()
    cid = db.query(Channel).first().id
    db.close()
    client.post("/campaigns", data={
        "topic_name": "Horror đêm khuya", "channel_id": str(cid), "total_episodes": "30",
        "language": "vi", "continuity": "no_repeat", "timezone": "Asia/Ho_Chi_Minh",
        "persona": "Chú Ba miền Tây kể chuyện", "style_examples": "Khuya nay kể nha...",
        "catchphrase_open": "Tắt đèn chưa?", "catchphrase_close": "Ngủ ngon nha.",
        "posting_slots": "21:00",
    }, follow_redirects=False)
    db = SessionLocal()
    cam = db.query(Campaign).filter_by(topic_name="Horror đêm khuya").one()
    cfg = cam.config_json
    db.close()
    assert cfg["persona"] == "Chú Ba miền Tây kể chuyện"
    assert cfg["continuity"] == "no_repeat" and cfg["timezone"] == "Asia/Ho_Chi_Minh"
    assert cfg["catchphrase_open"] == "Tắt đèn chưa?"

    # Duplicate: the new-campaign form comes prefilled with the source persona.
    page = client.get(f"/campaigns/new?from_id={cam.id}")
    assert page.status_code == 200 and "Duplicate Campaign" in page.text
    assert "Chú Ba miền Tây kể chuyện" in page.text and "Tắt đèn chưa?" in page.text
    # Foreign/missing source is ignored gracefully.
    assert "Duplicate Campaign" not in client.get("/campaigns/new?from_id=99999").text


def test_edit_campaign(client):
    cam = _seed_campaign(client)
    r = client.get(f"/campaigns/{cam.id}/edit")
    assert r.status_code == 200 and "Edit Campaign" in r.text and "Space" in r.text

    r = client.post(f"/campaigns/{cam.id}/edit",
                    data={"topic_name": "Deep Space", "channel_id": str(cam.channel_id),
                          "total_episodes": "8", "language": "vi", "publish_mode": "auto",
                          "privacy": "public"},
                    follow_redirects=False)
    assert r.status_code == 303
    from database.db_session import SessionLocal
    from database.models import Campaign

    db = SessionLocal()
    cam2 = db.get(Campaign, cam.id)
    assert cam2.topic_name == "Deep Space" and cam2.total_episodes == 8
    assert cam2.config_json["language"] == "vi" and cam2.config_json["auto_publish"] is True
    db.close()


def test_retry_route(client):
    from database.db_session import SessionLocal
    from database.models import Task
    from database.types import TaskStatus

    cam = _seed_campaign(client)
    db = SessionLocal()
    t = Task(campaign_id=cam.id, user_id=cam.user_id, episode_number=1,
             status=TaskStatus.FAILED, error_message="boom")
    db.add(t)
    db.commit()
    db.refresh(t)
    db.close()

    r = client.post(f"/api/tasks/{t.id}/retry")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["mode"] == "render"

    db = SessionLocal()
    t2 = db.get(Task, t.id)
    assert t2.status == TaskStatus.PENDING_QUEUE and t2.retry_count == 1 and t2.error_message is None
    db.close()

    # non-failed tasks can't be retried; foreign/missing ids 404
    assert client.post(f"/api/tasks/{t.id}/retry").status_code == 400
    assert client.post("/api/tasks/99999/retry").status_code == 404


def test_asset_review_flow(client, tmp_path):
    from database.db_session import SessionLocal
    from database.models import BufferPoolItem, Task
    from database.types import BufferStatus, TaskStatus

    cam = _seed_campaign(client)
    video = tmp_path / "ep1.mp4"
    video.write_bytes(b"0123456789abcdef")

    db = SessionLocal()
    t = Task(campaign_id=cam.id, user_id=cam.user_id, episode_number=1,
             status=TaskStatus.AWAITING_REVIEW)
    buf = BufferPoolItem(campaign_id=cam.id, channel_id=cam.channel_id, episode_number=1,
                         video_path=str(video), status=BufferStatus.awaiting_review,
                         metadata_json={"title": "T"})
    db.add_all([t, buf])
    db.commit()
    db.refresh(buf)
    db.close()

    # Assets page shows the review card with a player.
    page = client.get("/assets")
    assert page.status_code == 200 and "Approve" in page.text and f"/assets/{buf.id}/video" in page.text

    # Streaming: full body and a byte range.
    full = client.get(f"/assets/{buf.id}/video")
    assert full.status_code == 200 and full.content == b"0123456789abcdef"
    part = client.get(f"/assets/{buf.id}/video", headers={"Range": "bytes=4-7"})
    assert part.status_code == 206 and part.content == b"4567"
    assert part.headers["content-range"] == "bytes 4-7/16"

    # Approve → publish job enqueued (fakeredis), task queued.
    r = client.post(f"/assets/{buf.id}/approve", follow_redirects=False)
    assert r.status_code == 303
    from workers import task_queue

    assert len(task_queue.render_queue) == 1

    # Reject path: reset to awaiting_review and reject → file removed, task failed.
    db = SessionLocal()
    b2 = db.get(BufferPoolItem, buf.id)
    b2.status = BufferStatus.awaiting_review
    db.commit()
    db.close()
    r = client.post(f"/assets/{buf.id}/reject", follow_redirects=False)
    assert r.status_code == 303 and not video.exists()
    db = SessionLocal()
    assert db.get(BufferPoolItem, buf.id).status == BufferStatus.rejected
    t2 = db.query(Task).filter_by(campaign_id=cam.id, episode_number=1).one()
    assert t2.status == TaskStatus.FAILED and "Rejected" in t2.error_message
    db.close()


def test_reject_reason_feeds_learning(client, tmp_path):
    from database.db_session import SessionLocal
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, TaskStatus

    cam = _seed_campaign(client)
    video = tmp_path / "e.mp4"
    video.write_bytes(b"x")
    db = SessionLocal()
    t = Task(campaign_id=cam.id, user_id=cam.user_id, episode_number=1, status=TaskStatus.AWAITING_REVIEW)
    buf = BufferPoolItem(campaign_id=cam.id, channel_id=cam.channel_id, episode_number=1,
                         video_path=str(video), status=BufferStatus.awaiting_review)
    db.add_all([t, buf])
    db.commit()
    db.refresh(buf)
    db.close()

    r = client.post(f"/assets/{buf.id}/reject", data={"reason": "mở đầu chậm quá"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    cam2 = db.get(Campaign, cam.id)
    assert cam2.learning_json["reject_reasons"] == ["mở đầu chậm quá"]
    t2 = db.query(Task).filter_by(campaign_id=cam.id, episode_number=1).one()
    assert "mở đầu chậm quá" in t2.error_message
    db.close()


def test_performance_page_and_reset(client):
    from database.db_session import SessionLocal
    from database.models import Campaign, Task
    from database.types import TaskStatus

    cam = _seed_campaign(client)
    db = SessionLocal()
    cam_db = db.get(Campaign, cam.id)
    cam_db.learning_json = {"playbook": ["Open with a question"], "best_examples": ["Ex1"],
                            "distilled_at": "2026-07-18T00:00:00"}
    db.add(Task(campaign_id=cam.id, user_id=cam.user_id, episode_number=1,
                status=TaskStatus.COMPLETED, synopsis="the floating market ghost",
                published_url="https://www.youtube.com/shorts/x1",
                stats_json={"views": 900, "avg_pct_viewed": 72.5, "likes": 40,
                            "fetched_at": "2026-07-18T00:00:00"}))
    db.commit()
    db.close()

    page = client.get(f"/campaigns/{cam.id}/performance")
    assert page.status_code == 200
    assert "Open with a question" in page.text and "72.5%" in page.text and "🏆" in page.text

    r = client.post(f"/campaigns/{cam.id}/learning/reset", follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    assert db.get(Campaign, cam.id).learning_json is None
    db.close()


def test_ab_variant_summary_closes_the_loop():
    """Per-variant aggregation: only episodes with BOTH a recorded variant and stats count;
    metrics average within each variant so the operator sees which style actually retains."""
    from types import SimpleNamespace

    from main import ab_variant_summary

    episodes = [
        SimpleNamespace(ab_variant="A", stats_json={"avg_pct_viewed": 60, "views": 100}),
        SimpleNamespace(ab_variant="A", stats_json={"avg_pct_viewed": 50, "views": 300}),
        SimpleNamespace(ab_variant="B", stats_json={"avg_pct_viewed": 70}),      # no view count yet
        SimpleNamespace(ab_variant="C", stats_json=None),                        # no stats yet
        SimpleNamespace(ab_variant=None, stats_json={"views": 5}),               # pre-feature episode
    ]
    summary = ab_variant_summary(episodes)
    assert [s["variant"] for s in summary] == ["A", "B"]
    assert summary[0] == {"variant": "A", "episodes": 2, "avg_retention": 55.0, "avg_views": 200}
    assert summary[1]["avg_retention"] == 70.0 and summary[1]["avg_views"] is None
    assert ab_variant_summary([]) == []


def test_performance_page_shows_variant_results(client):
    from database.db_session import SessionLocal
    from database.models import Task
    from database.types import TaskStatus

    cam = _seed_campaign(client)
    db = SessionLocal()
    db.add_all([
        Task(campaign_id=cam.id, user_id=cam.user_id, episode_number=1, ab_variant="A",
             status=TaskStatus.COMPLETED, stats_json={"avg_pct_viewed": 62.0, "views": 500}),
        Task(campaign_id=cam.id, user_id=cam.user_id, episode_number=2, ab_variant="B",
             status=TaskStatus.COMPLETED, stats_json={"avg_pct_viewed": 48.0, "views": 200}),
    ])
    db.commit()
    db.close()

    page = client.get(f"/campaigns/{cam.id}/performance")
    assert page.status_code == 200
    assert "A/B Variant Results" in page.text
    assert "62.0%" in page.text and "48.0%" in page.text


def test_asset_stream_404s(client, tmp_path):
    # Missing item and missing file both 404 (never leak paths).
    assert client.get("/assets/424242/video").status_code == 404


def test_credentials_test_endpoint(client, monkeypatch):
    from services import verification

    client.post("/credentials", data={"gemini_api_key": "k"}, follow_redirects=False)
    monkeypatch.setattr(verification, "verify_gemini", lambda key: (True, "Gemini key is valid."))
    r = client.post("/credentials/test/gemini")
    assert r.status_code == 200 and r.json() == {"ok": True, "detail": "Gemini key is valid."}
    # No pexels key saved and no env fallback → clean failure message.
    from core.config import settings

    monkeypatch.setattr(settings, "PEXELS_API_KEY", None)
    r = client.post("/credentials/test/pexels")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert client.post("/credentials/test/unknown").status_code == 404


def test_api_tasks_returns_names_and_transparency_fields(client):
    from database.db_session import SessionLocal
    from database.models import Task
    from database.types import TaskStatus
    from datetime import datetime

    cam = _seed_campaign(client)
    db = SessionLocal()
    t = Task(campaign_id=cam.id, user_id=cam.user_id, episode_number=1,
             status=TaskStatus.COMPLETED, progress_pct=100,
             published_url="https://www.youtube.com/shorts/x1",
             started_at=datetime(2026, 7, 18, 10, 0, 0),
             finished_at=datetime(2026, 7, 18, 10, 12, 30))
    db.add(t)
    db.commit()
    db.close()

    data = client.get("/api/tasks").json()["tasks"][0]
    assert data["topic"] == "Space" and data["channel"] == "P"
    assert data["published_url"].endswith("/x1")
    assert data["duration_s"] == 750 and data["can_retry"] is False


def test_api_summary_snapshot(client):
    """The live snapshot feeding the header attention badge + dashboard auto-refresh reuses the
    dashboard helpers, so its counts match a full reload."""
    from database.db_session import SessionLocal
    from database.models import Task
    from database.types import TaskStatus

    cam = _seed_campaign(client)
    db = SessionLocal()
    db.add(Task(campaign_id=cam.id, user_id=cam.user_id, episode_number=1,
                status=TaskStatus.FAILED, progress_pct=40))
    db.add(Task(campaign_id=cam.id, user_id=cam.user_id, episode_number=2,
                status=TaskStatus.AWAITING_REVIEW, progress_pct=100))
    db.commit()
    db.close()

    d = client.get("/api/summary").json()
    assert set(d) == {"health", "counts", "channels", "active_campaigns"}
    assert d["counts"]["failed"] == 1 and d["counts"]["awaiting_review"] == 1
    assert d["channels"] == 1  # _seed_campaign creates one channel
    assert set(d["health"]) >= {"redis", "worker", "buffer_ready", "ai_budget"}
