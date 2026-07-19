"""Custom column types and shared enums.

`EncryptedString` gives transparent at-rest encryption (ADR-005): a secret column is just
`mapped_column(EncryptedString)`, and call sites read/write plain `str`. Encryption is applied at
the single binding point below, so no code path can forget to encrypt or accidentally persist a
plaintext secret.

Enums are stored as human-readable strings (`native_enum=False` at the column) — SQLite has no
native enum, and strings keep the raw .db / SQL dump legible.
"""
from __future__ import annotations

import enum

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from core import security


class EncryptedString(TypeDecorator):
    """A Text column whose value is Fernet-encrypted on write and decrypted on read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:  # write path
        return security.encrypt(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:  # read path
        return security.decrypt(value)


class Platform(str, enum.Enum):
    youtube = "youtube"
    facebook = "facebook"


class ChannelStatus(str, enum.Enum):
    active = "active"
    expired = "expired"


class CampaignStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    completed = "completed"
    failed = "failed"


class TaskStatus(str, enum.Enum):
    """Granular pipeline state surfaced in the Real-Time Task Logs panel."""

    PENDING_QUEUE = "PENDING_QUEUE"
    AI_GENERATION = "AI_GENERATION"
    AUDIO_SYNCED = "AUDIO_SYNCED"
    RENDERING = "RENDERING"
    AWAITING_REVIEW = "AWAITING_REVIEW"  # review-mode: rendered, waiting for operator approval
    SCHEDULED = "SCHEDULED"              # rendered into the buffer, waiting for its posting slot
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class BufferStatus(str, enum.Enum):
    ready = "ready"                      # pre-rendered, waiting for its publish slot
    awaiting_review = "awaiting_review"  # review-mode: waiting for operator approve/reject
    rejected = "rejected"                # operator rejected in review (files removed)
    consumed = "consumed"                # published
    expired = "expired"                  # aged out / invalidated
