"""Crypto round-trip, transparent encrypted columns, and tenant isolation."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import HTTPException


def test_fernet_round_trip():
    from core import security

    ct = security.encrypt("secret")
    assert ct != "secret"
    assert security.decrypt(ct) == "secret"
    assert security.encrypt(None) is None
    assert security.decrypt(None) is None


def test_encrypted_column_is_ciphertext_at_rest(session, user):
    import os

    db_path = os.environ["DATABASE_URL"].replace("sqlite:///", "")
    raw = sqlite3.connect(db_path).execute("SELECT gemini_api_key FROM users WHERE id=?", (user.id,)).fetchone()[0]
    assert raw != "gkey" and raw.startswith("gAAAA")
    # ORM decrypts transparently on read
    from database.models import User

    assert session.get(User, user.id).gemini_api_key == "gkey"


def test_wal_enabled(session):
    assert session.execute(__import__("sqlalchemy").text("PRAGMA journal_mode")).scalar().lower() == "wal"


def test_tenant_isolation_404(session, user, channel):
    from auth.dependencies import get_owned_campaign
    from database.models import Campaign, User

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=3)
    session.add(cam)
    session.commit()
    session.refresh(cam)

    # owner ok
    assert get_owned_campaign(campaign_id=cam.id, user=user, db=session).id == cam.id

    other = User(firebase_uid="other")
    session.add(other)
    session.commit()
    session.refresh(other)
    with pytest.raises(HTTPException) as ei:
        get_owned_campaign(campaign_id=cam.id, user=other, db=session)
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei2:
        get_owned_campaign(campaign_id=99999, user=user, db=session)
    assert ei2.value.status_code == 404


def test_solo_user_get_or_create_idempotent(session):
    from auth.dependencies import SOLO_UID, get_current_user

    u1 = get_current_user(db=session, authorization=None)
    u2 = get_current_user(db=session, authorization=None)
    assert u1.id == u2.id and u1.firebase_uid == SOLO_UID and u1.is_admin
