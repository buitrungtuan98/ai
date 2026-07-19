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
                                    "color_grade": "cinematic", "auto_qc": "off"},
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
