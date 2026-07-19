"""Pytest fixtures. Sets required env BEFORE any app module imports (config reads env at import),
points the DB at a temp file, and injects fakeredis so the whole suite runs with no external
services or secrets."""
from __future__ import annotations

import os
import tempfile

from cryptography.fernet import Fernet

_TMP = tempfile.gettempdir()
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMP, 'aivf_pytest.db')}")
os.environ.setdefault("MEDIA_ROOT", os.path.join(_TMP, "aivf_pytest_media"))
os.environ.setdefault("WORK_ROOT", os.path.join(_TMP, "aivf_pytest_media", "work"))

import fakeredis  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_env():
    """Recreate a clean schema and a fresh fakeredis for every test."""
    from database.db_session import engine
    from database.models import Base
    from workers import task_queue

    task_queue.set_connection(fakeredis.FakeStrictRedis())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def session():
    from database.db_session import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def user(session):
    from database.models import User

    u = User(firebase_uid="solo-admin", is_admin=True, gemini_api_key="gkey", pexels_api_key="pkey")
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


@pytest.fixture
def channel(session, user):
    from database.models import Channel
    from database.types import Platform

    c = Channel(user_id=user.id, platform=Platform.youtube, channel_name="Test Ch",
                encrypted_credentials="{}")
    session.add(c)
    session.commit()
    session.refresh(c)
    return c
