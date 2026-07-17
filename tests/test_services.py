"""Publishing services: YouTube OAuth refresh/persist, Facebook creds load, Telegram send."""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime

import pytest


def _install_fake_google(monkeypatch):
    class FakeCreds:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self._valid = False
            self.token = kw.get("token")
            self.expiry = None

        @property
        def valid(self):
            return self._valid

        def refresh(self, request):
            self.token = "NEW_ACCESS"
            self.expiry = datetime(2030, 1, 1)
            self._valid = True

    for name in ["google", "google.oauth2", "google.oauth2.credentials",
                 "google.auth", "google.auth.transport", "google.auth.transport.requests"]:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["google.oauth2.credentials"].Credentials = FakeCreds
    sys.modules["google.auth.transport.requests"].Request = type("Request", (), {})


def test_youtube_refresh_persists(monkeypatch):
    _install_fake_google(monkeypatch)
    from database.models import Channel
    from database.types import Platform
    from services import youtube_service as ys

    ch = Channel(platform=Platform.youtube, channel_name="C",
                 encrypted_credentials=json.dumps({"refresh_token": "rt"}))
    creds = ys.build_credentials(ch)
    assert creds.token == "NEW_ACCESS"
    stored = json.loads(ch.encrypted_credentials)
    assert stored["access_token"] == "NEW_ACCESS" and "token_expiry" in stored


def test_youtube_missing_refresh_token(monkeypatch):
    _install_fake_google(monkeypatch)
    from database.models import Channel
    from database.types import Platform
    from services import youtube_service as ys

    with pytest.raises(RuntimeError, match="refresh_token"):
        ys.build_credentials(Channel(platform=Platform.youtube, channel_name="C", encrypted_credentials="{}"))


def test_facebook_load():
    from database.models import Channel
    from database.types import Platform
    from services import facebook_service as fs

    ok = Channel(platform=Platform.facebook, channel_name="P",
                 encrypted_credentials=json.dumps({"page_id": "P1", "page_access_token": "T"}))
    assert fs._load(ok) == ("P1", "T")
    with pytest.raises(RuntimeError):
        fs._load(Channel(platform=Platform.facebook, channel_name="X", encrypted_credentials="{}"))


def test_telegram_send(monkeypatch):
    import requests

    from services import telegram_bot as tb

    class R:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "post", lambda *a, **k: R())
    assert tb.send("tok", "chat", "hi") is True

    def boom(*a, **k):
        raise RuntimeError("net down")

    monkeypatch.setattr(requests, "post", boom)
    assert tb.send("tok", "chat", "hi") is False  # never raises
