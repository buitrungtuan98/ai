"""ORM models — the relational schema.

Relationship chain (One-to-Many all the way down):
    User 1─▶M Channel 1─▶M Campaign 1─▶M {Task, BufferPoolItem}

`user_id` is denormalized onto Campaign/Task/BufferPoolItem (reachable via the chain anyway) so
every tenant-scoped query filters on one indexed column with no join on the hot path. This is the
main normalization trade-off and is deliberate (see the backend design notes).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from database.types import (
    BufferStatus,
    CampaignStatus,
    ChannelStatus,
    EncryptedString,
    Platform,
    TaskStatus,
)


class Base(DeclarativeBase):
    pass


# Store enums as VARCHAR + CHECK (portable; legible in the dump).
def _enum(py_enum) -> Enum:
    return Enum(py_enum, native_enum=False, validate_strings=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firebase_uid: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Per-user provider credentials — encrypted at rest.
    gemini_api_key: Mapped[str | None] = mapped_column(EncryptedString)
    pexels_api_key: Mapped[str | None] = mapped_column(EncryptedString)
    telegram_token: Mapped[str | None] = mapped_column(EncryptedString)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))  # identifier, not a secret

    channels: Mapped[list["Channel"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    campaigns: Mapped[list["Campaign"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    platform: Mapped[Platform] = mapped_column(_enum(Platform), nullable=False)
    channel_name: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    # OAuth token bundle / Page access token (JSON string) — encrypted at rest.
    encrypted_credentials: Mapped[str | None] = mapped_column(EncryptedString)
    status: Mapped[ChannelStatus] = mapped_column(
        _enum(ChannelStatus), default=ChannelStatus.active
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="channels")
    campaigns: Mapped[list["Campaign"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        # Dashboard "my active campaigns" query.
        Index("ix_campaigns_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True, nullable=False
    )
    topic_name: Mapped[str] = mapped_column(String(255), nullable=False)
    current_episode: Mapped[int] = mapped_column(Integer, default=0)
    total_episodes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        _enum(CampaignStatus), default=CampaignStatus.pending, index=True
    )
    # Generation params: language, system_prompt, voice, rate, subtitle style, branding, slots, CTA…
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="campaigns")
    channel: Mapped["Channel"] = relationship(back_populates="campaigns")
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    buffer_items: Mapped[list["BufferPoolItem"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        # Idempotent enqueue — one task per (campaign, episode).
        UniqueConstraint("campaign_id", "episode_number", name="uq_task_campaign_episode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(  # denormalized tenant scope
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        _enum(TaskStatus), default=TaskStatus.PENDING_QUEUE, index=True
    )
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    rq_job_id: Mapped[str | None] = mapped_column(String(64))  # correlate row ↔ RQ job
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    campaign: Mapped["Campaign"] = relationship(back_populates="tasks")


class BufferPoolItem(Base):
    """A pre-rendered episode parked on disk, waiting for its publish slot.

    Decouples slow CPU rendering from time-based publishing and smooths the single-writer load.
    The video bytes live on the filesystem (`video_path`); only metadata is in the DB.
    """

    __tablename__ = "buffer_pool"
    __table_args__ = (
        UniqueConstraint("campaign_id", "episode_number", name="uq_buffer_campaign_episode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True, nullable=False
    )
    channel_id: Mapped[int] = mapped_column(  # denormalized for publish routing
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    video_path: Mapped[str] = mapped_column(String(512), nullable=False)
    thumbnail_path: Mapped[str | None] = mapped_column(String(512))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)  # chosen title/desc/tags
    status: Mapped[BufferStatus] = mapped_column(
        _enum(BufferStatus), default=BufferStatus.ready, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)

    campaign: Mapped["Campaign"] = relationship(back_populates="buffer_items")
