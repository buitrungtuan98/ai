"""At-rest encryption for stored third-party credentials.

DRY: the cipher is constructed exactly once here; `database.types.EncryptedString` is the only
consumer, so no call site ever handles raw crypto. `MultiFernet` lets us rotate keys with zero
downtime — set `FERNET_KEY=newkey,oldkey`; new writes use the first key, reads try all keys.
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from core.config import settings

logger = logging.getLogger(__name__)


def _build_cipher() -> MultiFernet:
    keys = [k.strip() for k in settings.FERNET_KEY.split(",") if k.strip()]
    if not keys:
        raise ValueError("FERNET_KEY is empty — cannot build the encryption cipher.")
    return MultiFernet([Fernet(k.encode()) for k in keys])


_cipher = _build_cipher()


def encrypt(value: str | None) -> str | None:
    """Encrypt a plaintext string; passes None through unchanged."""
    if value is None:
        return None
    return _cipher.encrypt(value.encode()).decode()


def decrypt(token: str | None) -> str | None:
    """Decrypt a token; None-safe. Returns None on an unreadable token (rotated-out/corrupt)
    rather than raising, so a single bad row never crashes a page load."""
    if token is None:
        return None
    try:
        return _cipher.decrypt(token.encode()).decode()
    except InvalidToken:
        logger.warning("Failed to decrypt a stored credential (rotated-out or corrupt key).")
        return None
