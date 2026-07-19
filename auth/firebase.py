"""Firebase Admin wrapper — the only module that touches `firebase_admin`.

Lazy-initialised: the SDK is imported and the app created on first use, so solo mode
(`MULTI_TENANT_MODE=false`) never needs Firebase configured, and the test suite never imports it.
"""
from __future__ import annotations

import threading

from core.config import settings

_lock = threading.Lock()
_app = None


def _ensure_app():
    global _app
    if _app is not None:
        return _app
    with _lock:
        if _app is not None:
            return _app
        import firebase_admin
        from firebase_admin import credentials

        if not settings.FIREBASE_CREDENTIALS_PATH:
            raise RuntimeError("FIREBASE_CREDENTIALS_PATH is not set (required in multi-tenant mode).")
        cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS_PATH)
        _app = firebase_admin.initialize_app(cred)
        return _app


def verify_id_token(token: str) -> dict:
    """Verify a Firebase ID token and return its decoded claims (raises on invalid)."""
    _ensure_app()
    from firebase_admin import auth as fb_auth

    return fb_auth.verify_id_token(token)


def sign_in_with_google_id_token(google_id_token: str) -> dict:
    """Exchange a Google OAuth id_token for a Firebase sign-in via the Auth REST API
    (`accounts:signInWithIdp`). Returns the response dict (contains `idToken`, `localId`,
    `email`, …). Used by the CDN-free server-side "Continue with Google" login flow."""
    import requests

    if not settings.FIREBASE_WEB_API_KEY:
        raise RuntimeError("FIREBASE_WEB_API_KEY is not set (required for Google sign-in).")
    resp = requests.post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp",
        params={"key": settings.FIREBASE_WEB_API_KEY},
        json={
            "postBody": f"id_token={google_id_token}&providerId=google.com",
            "requestUri": settings.OAUTH_REDIRECT_BASE,
            "returnSecureToken": True,
            "returnIdpCredential": True,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
