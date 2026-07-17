"""Database engine, session factory, and the FastAPI `get_db` dependency.

Synchronous SQLAlchemy (ADR-007): SQLite serializes writes regardless of async, and the render
worker is inherently synchronous, so async buys nothing here.

The `connect` event listener applies WAL + timeouts + foreign keys on EVERY physical connection.
WAL is a persistent DB property, but `busy_timeout`/`synchronous`/`foreign_keys` are per-connection,
so setting them in the listener guarantees consistent behaviour across BOTH the web and worker
processes that share the one .db file.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import settings
from database.models import Base

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

# For sqlite:////abs/path/x.db, make sure the parent directory exists before connecting.
if _is_sqlite:
    db_file = settings.DATABASE_URL.split("sqlite:///", 1)[-1]
    if db_file and db_file != ":memory:":
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)

engine: Engine = create_engine(
    settings.DATABASE_URL,
    connect_args=(
        {"timeout": 30.0, "check_same_thread": False} if _is_sqlite else {}
    ),
    pool_pre_ping=True,
    future=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _connection_record) -> None:
    if not _is_sqlite:
        return
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")      # readers don't block the single writer
    cur.execute("PRAGMA busy_timeout=30000")    # ms — wait-on-lock instead of instant failure
    cur.execute("PRAGMA synchronous=NORMAL")    # safe under WAL, far fewer fsyncs (ARM SSD win)
    cur.execute("PRAGMA foreign_keys=ON")       # per-connection; needed for ON DELETE CASCADE
    cur.close()


SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, class_=Session
)


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call at startup (idempotent)."""
    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
