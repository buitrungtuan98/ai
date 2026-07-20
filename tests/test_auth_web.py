"""Multi-tenant login: /login page, session mint/logout, 401 routing, Google SSO exchange."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def mt_client(monkeypatch):
    """A TestClient with MULTI_TENANT_MODE flipped on (runtime flag; same settings object app-wide).
    Uses an https base_url so the Secure session cookie (SessionMiddleware https_only=True) is
    stored and returned — production always serves over HTTPS via the Cloudflare Tunnel."""
    import main
    from core.config import settings

    monkeypatch.setattr(settings, "MULTI_TENANT_MODE", True)
    with TestClient(main.app, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def solo_client():
    import main

    with TestClient(main.app, base_url="https://testserver") as c:
        yield c


def _fake_verify(monkeypatch, uid="u-9", email="user@example.com"):
    from auth import firebase

    monkeypatch.setattr(firebase, "verify_id_token", lambda tok: {"uid": uid, "email": email})


def test_solo_login_redirects_home(solo_client):
    r = solo_client.get("/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_login_page_renders(mt_client):
    r = mt_client.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text and "Create account" in r.text


def test_browser_nav_redirects_to_login(mt_client):
    r = mt_client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_api_gets_plain_401(mt_client):
    assert mt_client.get("/api/tasks").status_code == 401


def test_session_login_logout_flow(mt_client, monkeypatch):
    from database.db_session import SessionLocal
    from database.models import User

    _fake_verify(monkeypatch, uid="u-9", email="user@example.com")

    # Mint the session from an ID token (what the /login page does after Firebase auth).
    r = mt_client.post("/auth/session", json={"id_token": "tok"})
    assert r.status_code == 200 and r.json()["ok"] is True

    # The session cookie now authenticates page loads and the API.
    assert mt_client.get("/").status_code == 200
    assert mt_client.get("/api/tasks").status_code == 200
    # /login bounces straight back home while signed in.
    r = mt_client.get("/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"

    # First login JIT-provisioned the user row.
    db = SessionLocal()
    assert db.query(User).filter_by(firebase_uid="u-9").count() == 1
    db.close()

    # Logout clears the session.
    r = mt_client.post("/auth/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = mt_client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_invalid_token_is_401(mt_client, monkeypatch):
    from auth import firebase

    def boom(tok):
        raise ValueError("bad token")

    monkeypatch.setattr(firebase, "verify_id_token", boom)
    assert mt_client.post("/auth/session", json={"id_token": "junk"}).status_code == 401


def test_bearer_header_still_works(mt_client, monkeypatch):
    _fake_verify(monkeypatch, uid="api-user")
    r = mt_client.get("/api/tasks", headers={"Authorization": "Bearer some-token"})
    assert r.status_code == 200 and r.json() == {"tasks": []}


def test_sign_in_with_google_id_token_unit(monkeypatch):
    import requests

    from auth import firebase
    from core.config import settings

    monkeypatch.setattr(settings, "FIREBASE_WEB_API_KEY", "web-key")
    captured = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"idToken": "fidt", "localId": "g-1", "email": "g@x.com"}

    def fake_post(url, params=None, json=None, timeout=None):
        captured.update(url=url, params=params, json=json)
        return R()

    monkeypatch.setattr(requests, "post", fake_post)
    data = firebase.sign_in_with_google_id_token("google-idt")
    assert data["idToken"] == "fidt"
    assert "accounts:signInWithIdp" in captured["url"]
    assert captured["params"] == {"key": "web-key"}
    assert "id_token=google-idt" in captured["json"]["postBody"]

    monkeypatch.setattr(settings, "FIREBASE_WEB_API_KEY", None)
    with pytest.raises(RuntimeError, match="FIREBASE_WEB_API_KEY"):
        firebase.sign_in_with_google_id_token("google-idt")


def test_google_login_callback_mints_session(mt_client, monkeypatch):
    import main
    from auth import firebase
    from core.config import settings
    from database.db_session import SessionLocal
    from database.models import User

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "csec")

    class FakeFlow:
        def authorization_url(self, **kw):
            return ("https://accounts.example/o/oauth2", "st-1")

        def fetch_token(self, code=None):
            assert code == "the-code"
            self.credentials = SimpleNamespace(id_token="google-idt")

    monkeypatch.setattr(main, "_google_flow", lambda scopes, path: FakeFlow())
    monkeypatch.setattr(firebase, "sign_in_with_google_id_token",
                        lambda t: {"idToken": "fidt", "email": "g@x.com"})
    monkeypatch.setattr(firebase, "verify_id_token", lambda t: {"uid": "g-1", "email": "g@x.com"})

    # Start stores the state in the session, then the callback must match it.
    r = mt_client.get("/auth/google/login", follow_redirects=False)
    assert r.status_code == 307 or r.status_code == 302 or r.status_code == 303
    r = mt_client.get("/auth/google/callback?state=st-1&code=the-code", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert mt_client.get("/").status_code == 200  # session works

    db = SessionLocal()
    assert db.query(User).filter_by(firebase_uid="g-1").count() == 1
    db.close()

    # A mismatched state is rejected.
    r2 = mt_client.get("/auth/google/callback?state=WRONG&code=x", follow_redirects=False)
    assert r2.status_code == 400
