"""R26 (ADR-092) — the hook lands on the first frame, the title is a short curiosity gap, and the
generated poster becomes the platform cover where the platform allows one.

Two operator reports on Facebook: (1) the 3-second hook flash was invisible in the preview — Facebook
grabs the FIRST frame as the Reel cover and the old fade-IN left it blank; (2) the two-line title was
cut mid-word to "…" because the DRAWN title was the 12-15-word published title. The fix makes the
flash opaque from frame 0, gives the AI a short `billboard_hook` for the drawn teaser (the published
title is untouched), steers a curiosity cold open, and uploads the poster as a custom cover.
"""
from __future__ import annotations

import sys
import types

import pytest


# ── Fix 1: the hook is opaque from frame 0 (the Facebook cover) ──────────────
def test_the_flash_is_opaque_from_frame_zero_so_the_cover_shows_the_hook(tmp_path):
    """No fade-IN (\\fad(0,…)) so the platform's first-frame cover carries the hook; a gentle
    fade-OUT still clears the frame after the window."""
    from core.captions import HEADLINE_FADE_IN_MS, build_ass
    from core.tts import WordTiming

    assert HEADLINE_FADE_IN_MS == 0
    out = str(tmp_path / "h.ass")
    build_ass([WordTiming("hi", 0.0, 1.0)], out, clip_duration=6.0,
              headline="VUA BỎ NGAI VÀNG", headline_accent_hex="0xFF3B30")
    event = next(ln for ln in open(out, encoding="utf-8") if ",Headline," in ln)
    assert r"\fad(0,400)" in event          # opaque at t=0, fades out only
    assert r"\fad(150," not in event        # the old fade-in that blanked the cover is gone


# ── Fix 2: a short billboard hook, drawn whole (no "…") ──────────────────────
def _script(hook):
    from core.ai_engine import VideoScript

    return VideoScript(
        language="vi", topic="Lịch sử VN", synopsis="s",
        scenes=[{"index": i, "narration": "n", "pexels_keywords": ["k"]} for i in range(3)],
        metadata_variations=[
            {"variant": v, "title": "Một tiêu đề rất dài dành cho nền tảng để tối ưu tìm kiếm",
             "description": "d", "tags": ["a", "b", "c"], "billboard_hook": hook}
            for v in "ABC"
        ],
    )


def test_pick_metadata_draws_the_short_hook_not_the_long_published_title():
    from core.video_factory import pick_metadata

    vs = _script("VUA BỎ NGAI VÀNG — VÌ MỘT LỜI THỀ")
    meta = pick_metadata(vs, 1, ab_testing=False)
    assert meta["hook_title"] == "VUA BỎ NGAI VÀNG — VÌ MỘT LỜI THỀ"     # drawn = the short hook
    assert meta["title"].startswith("Một tiêu đề rất dài")               # published = untouched title

    # A brand prefix rides the PUBLISHED title, never the drawn hook (kept a clean hook).
    meta2 = pick_metadata(vs, 1, ab_testing=False, title_prefix="🔥 SỬ VIỆT |")
    assert meta2["hook_title"] == "VUA BỎ NGAI VÀNG — VÌ MỘT LỜI THỀ"
    assert meta2["title"].startswith("🔥 SỬ VIỆT |")


def test_pick_metadata_falls_back_to_the_title_when_the_hook_is_missing_or_blank():
    from core.video_factory import pick_metadata

    for hook in (None, "   "):
        vs = _script(hook)
        meta = pick_metadata(vs, 1, ab_testing=False)
        assert meta["hook_title"] == vs.metadata_variations[0].title    # legacy scripts still work


def test_a_short_hook_is_drawn_whole_without_an_ellipsis(tmp_path):
    """The reported symptom was the reverse: a long title cut to '…'. A short hook fits and is drawn
    in full — the win is the SOURCE being short, not a bigger cut."""
    from core.captions import build_ass
    from core.tts import WordTiming

    out = str(tmp_path / "s.ass")
    build_ass([WordTiming("hi", 0.0, 1.0)], out, clip_duration=6.0,
              headline="AI GIẾT VỊ VUA NÀY?", headline_accent_hex="0xFF3B30")
    event = next(ln for ln in open(out, encoding="utf-8") if ",Headline," in ln)
    text = event.split(",,", 2)[-1]
    assert "…" not in text                                              # nothing was cut
    assert "GIẾT" in text and "VUA" in text                             # the whole hook survives


def test_the_metadata_hook_field_is_optional_and_short():
    from pydantic import ValidationError

    from core.ai_engine import MetadataVariation

    base = dict(variant="A", title="t", description="d", tags=["a", "b", "c"])
    assert MetadataVariation(**base).billboard_hook is None             # optional — legacy safe
    assert MetadataVariation(**base, billboard_hook="Short hook").billboard_hook == "Short hook"
    with pytest.raises(ValidationError):
        MetadataVariation(**base, billboard_hook="x" * 49)              # capped so it never wraps big


# ── Fix 2 + 3: the prompt asks for a short hook and a curiosity cold open ─────
@pytest.mark.parametrize("video_format", ["short", "long"])
def test_the_script_prompt_asks_for_a_short_hook_and_a_cold_open(video_format):
    from core.ai_engine import build_script_prompt

    p = build_script_prompt(topic="Lịch sử VN", language="vi", total_episodes=10, episode=1,
                            video_format=video_format)
    assert "billboard_hook" in p                                        # the short drawn hook
    assert "COLD OPEN" in p and "MIDDLE OF THE ACTION" in p             # curiosity opening


def test_the_metadata_refresh_prompt_also_asks_for_the_hook():
    import inspect

    from core import ai_engine

    src = inspect.getsource(ai_engine.regenerate_metadata)
    assert "billboard_hook" in src


# ── Fix 4: the generated poster becomes the platform cover (fail-open) ───────
def _install_fake_youtube(monkeypatch, *, thumb_raises=False):
    """A fake googleapiclient whose videos().insert returns an id and whose thumbnails().set is
    recorded (or made to raise). Returns the list that captures set() video ids."""
    from services import youtube_service as ys

    set_calls: list[str] = []

    class FakeReq:
        def next_chunk(self):
            return (None, {"id": "vid123"})

    class FakeExec:
        def execute(self):
            if thumb_raises:
                raise RuntimeError("thumbnailsNotAvailable: channel not verified")
            return {}

    class FakeThumbs:
        def set(self, videoId, media_body):
            set_calls.append(videoId)
            return FakeExec()

    class FakeYouTube:
        def videos(self):
            insert = lambda part, body, media_body: FakeReq()  # noqa: E731
            return type("V", (), {"insert": staticmethod(insert)})()

        def thumbnails(self):
            return FakeThumbs()

    disc = types.ModuleType("googleapiclient.discovery")
    disc.build = lambda *a, **k: FakeYouTube()
    http = types.ModuleType("googleapiclient.http")
    http.MediaFileUpload = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", disc)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http)
    monkeypatch.setattr(ys, "build_credentials", lambda ch: object())
    return set_calls


def _yt_channel():
    from database.models import Channel
    from database.types import Platform

    return Channel(platform=Platform.youtube, channel_name="C", encrypted_credentials="{}")


def test_youtube_sets_the_generated_poster_as_the_custom_thumbnail(monkeypatch, tmp_path):
    from services import youtube_service as ys

    set_calls = _install_fake_youtube(monkeypatch)
    thumb = tmp_path / "p.jpg"
    thumb.write_bytes(b"jpegbytes")
    vid = ys.upload_video(_yt_channel(), str(tmp_path / "v.mp4"), {"title": "T"},
                          thumbnail_path=str(thumb))
    assert vid == "vid123"
    assert set_calls == ["vid123"]                                      # poster set on the video


def test_youtube_skips_the_thumbnail_when_none_is_given(monkeypatch, tmp_path):
    from services import youtube_service as ys

    set_calls = _install_fake_youtube(monkeypatch)
    ys.upload_video(_yt_channel(), str(tmp_path / "v.mp4"), {"title": "T"})   # no thumbnail_path
    assert set_calls == []


def test_a_failed_thumbnail_never_fails_the_youtube_upload(monkeypatch, tmp_path):
    """An unverified channel is refused a custom thumbnail — that must not sink a published video."""
    from services import youtube_service as ys

    _install_fake_youtube(monkeypatch, thumb_raises=True)
    thumb = tmp_path / "p.jpg"
    thumb.write_bytes(b"jpegbytes")
    vid = ys.upload_video(_yt_channel(), str(tmp_path / "v.mp4"), {"title": "T"},
                          thumbnail_path=str(thumb))
    assert vid == "vid123"                                              # publish survived, fail-open


def test_facebook_page_video_carries_the_poster_but_a_reel_has_no_cover(monkeypatch, tmp_path):
    """Long-form Page video ships the `thumb`; a Reel has no cover API, so the thumbnail is silently
    ignored there and the burned-in hook flash carries the opening instead."""
    import json

    import requests

    from database.models import Channel
    from database.types import Platform
    from services import facebook_service as fb

    ch = Channel(platform=Platform.facebook, channel_name="C",
                 encrypted_credentials=json.dumps({"page_id": "P", "page_access_token": "tok"}))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    thumb = tmp_path / "p.jpg"
    thumb.write_bytes(b"jpg")
    monkeypatch.setattr(fb, "find_existing_upload", lambda *a, **k: None)

    class FakeResp:
        def __init__(self, status=200, payload=None):
            self.status_code = status
            self._payload = payload or {}

        def json(self):
            return self._payload

    # Long-form Page video → the POST carries a `thumb` file part.
    file_keys: list[set] = []

    def fake_post(url, **kw):
        file_keys.append(set((kw.get("files") or {}).keys()))
        return FakeResp(200, {"id": "V1"})

    monkeypatch.setattr(requests, "post", fake_post)
    fb.upload_video(ch, str(video), {"video_format": "long"}, thumbnail_path=str(thumb))
    assert any("thumb" in keys for keys in file_keys)                   # cover uploaded for Page video

    # Vertical short → Reel: three-phase upload, and NO POST ever carries a `thumb`.
    reel_file_keys: list[set] = []

    def fake_reel_post(url, **kw):
        reel_file_keys.append(set((kw.get("files") or {}).keys()))
        d = kw.get("data") if isinstance(kw.get("data"), dict) else {}
        if d.get("upload_phase") == "start":
            return FakeResp(200, {"video_id": "R1", "upload_url": "https://rupload/x"})
        return FakeResp(200, {"id": "R1"})

    monkeypatch.setattr(requests, "post", fake_reel_post)
    vid = fb.upload_video(ch, str(video), {"video_format": "short"}, thumbnail_path=str(thumb))
    assert vid == "R1"
    assert all("thumb" not in keys for keys in reel_file_keys)          # a Reel has no cover slot


def test_publish_forwards_the_thumbnail_path_to_both_platforms(monkeypatch):
    from database.models import Channel
    from database.types import Platform
    from services import facebook_service, youtube_service
    from workers import video_worker

    seen: dict[str, str | None] = {}

    def fake_yt(channel, path, metadata, user, *, check_existing=False, thumbnail_path=None):
        seen["youtube"] = thumbnail_path
        return "y1"

    def fake_fb(channel, path, metadata, *, pending_video_id=None, on_pending=None,
                thumbnail_path=None):
        seen["facebook"] = thumbnail_path
        return "f1"

    monkeypatch.setattr(youtube_service, "upload_video", fake_yt)
    monkeypatch.setattr(facebook_service, "upload_video", fake_fb)

    yt = Channel(platform=Platform.youtube, channel_name="Y", encrypted_credentials="{}")
    fbc = Channel(platform=Platform.facebook, channel_name="F", encrypted_credentials="{}")
    video_worker._publish(yt, "/v.mp4", {}, None, thumbnail_path="/poster.jpg")
    video_worker._publish(fbc, "/v.mp4", {}, None, thumbnail_path="/poster.jpg")
    assert seen == {"youtube": "/poster.jpg", "facebook": "/poster.jpg"}
