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
