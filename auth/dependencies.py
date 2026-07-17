"""Authentication dependencies and tenant-isolation guards.

One `get_current_user` handles both modes and returns the same `User` type, so every route is
mode-agnostic (DRY — no `if MULTI_TENANT_MODE` scattered through routes, and the solo path cannot
diverge and skip a check the public path enforces).

Tenant isolation is structural: routes NEVER accept `user_id` from the client; it comes only from
`get_current_user`. Ownership guards return 404 (not 403) on a foreign id so existence isn't leaked.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import firebase
from core.config import settings
from database.db_session import get_db
from database.models import Campaign, Channel, User

SOLO_UID = "solo-admin"

DbDep = Annotated[Session, Depends(get_db)]


def _get_or_create(db: Session, *, firebase_uid: str, is_admin: bool) -> User:
    user = db.scalar(select(User).where(User.firebase_uid == firebase_uid))
    if user is None:
        user = User(firebase_uid=firebase_uid, is_admin=is_admin)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def get_current_user(
    db: DbDep,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    if not settings.MULTI_TENANT_MODE:
        # Solo/dogfood: no Firebase, single built-in admin.
        return _get_or_create(db, firebase_uid=SOLO_UID, is_admin=True)

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        decoded = firebase.verify_id_token(token)
    except Exception as exc:  # noqa: BLE001 — any verification failure is a 401
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
    return _get_or_create(db, firebase_uid=decoded["uid"], is_admin=False)


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_owned_campaign(
    campaign_id: int,
    user: CurrentUser,
    db: DbDep,
) -> Campaign:
    """Resolve a campaign the current user owns, or 404. The single tenant-isolation choke point
    for campaign/task routes — Task/BufferPool are reached through their campaign."""
    campaign = db.get(Campaign, campaign_id)
    if campaign is None or campaign.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return campaign


def get_owned_channel(
    channel_id: int,
    user: CurrentUser,
    db: DbDep,
) -> Channel:
    """Resolve a channel the current user owns, or 404."""
    channel = db.get(Channel, channel_id)
    if channel is None or channel.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Channel not found")
    return channel


OwnedCampaign = Annotated[Campaign, Depends(get_owned_campaign)]
OwnedChannel = Annotated[Channel, Depends(get_owned_channel)]
