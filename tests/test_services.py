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


def test_pick_music_cc0_search_and_cache(monkeypatch, tmp_path):
    import random

    import requests

    from services import music_service as ms

    downloads = []

    class SearchResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [
                {"id": 101, "name": "Dark Drone", "username": "artistA",
                 "previews": {"preview-hq-mp3": "https://cdn.example/101.mp3"}},
                {"id": 202, "name": "No Preview", "username": "artistB", "previews": {}},
            ]}

    class DownloadResp:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            return iter([b"mp3-bytes"])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_get(url, params=None, stream=False, timeout=None):
        if url == ms.FREESOUND_SEARCH_URL:
            assert 'license:"Creative Commons 0"' in params["filter"]  # CC0-only, always
            assert params["query"] == "dark ambient horror"
            return SearchResp()
        downloads.append(url)
        return DownloadResp()

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(random, "choice", lambda pool: pool[0])  # deterministic for the test

    cache = str(tmp_path / "cache")
    path, credit = ms.pick_music("dark ambient horror", "fs-key", cache)
    assert path.endswith("freesound_101.mp3") and open(path, "rb").read() == b"mp3-bytes"
    assert credit["license"] == "CC0" and credit["author"] == "artistA"
    assert downloads == ["https://cdn.example/101.mp3"]

    # Cached: second pick of the same track downloads nothing.
    path2, _ = ms.pick_music("dark ambient horror", "fs-key", cache)
    assert path2 == path and downloads == ["https://cdn.example/101.mp3"]

    # Any failure degrades to None (episode renders without music, never fails).
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down")))
    assert ms.pick_music("mood", "fs-key", cache) is None


def test_pick_music_falls_back_to_generic_mood(monkeypatch, tmp_path):
    """A niche/non-English mood with zero CC0 matches retries ONCE with the generic query, so the
    episode gets *generic* music instead of *no* music."""
    from services import music_service as ms

    searches = []

    def fake_search(query, key):
        searches.append(query)
        if query == ms.FALLBACK_MOOD:
            return [{"id": 7, "name": "Calm Pad", "username": "artistC",
                     "previews": {"preview-hq-mp3": "https://cdn.example/7.mp3"}}]
        return []  # the specific mood finds nothing

    monkeypatch.setattr(ms, "_search_cc0", fake_search)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "freesound_7.mp3").write_bytes(b"mp3")  # pre-cached → no network download

    path, credit = ms.pick_music("nhạc buồn miền Tây", "fs-key", str(cache))
    assert searches == ["nhạc buồn miền Tây", ms.FALLBACK_MOOD]
    assert path.endswith("freesound_7.mp3") and credit["id"] == 7

    # Nothing at all (even generic) → None, and no third search.
    monkeypatch.setattr(ms, "_search_cc0", lambda q, k: [])
    assert ms.pick_music("anything", "fs-key", str(cache)) is None


def test_verify_freesound(monkeypatch):
    import requests

    from services import verification

    class Resp:
        def __init__(self, code):
            self.status_code = code

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp(200))
    ok, detail = verification.verify_freesound("good-key")
    assert ok and "Auto background music" in detail

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp(401))
    ok, detail = verification.verify_freesound("bad-key")
    assert not ok and "401" in detail


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
