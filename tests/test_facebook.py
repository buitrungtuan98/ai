"""ADR-072 — the Facebook surface: verify the right thing, say the truth, and stay fixable.

The whole Facebook journey used to be verified by reading a Page's PUBLIC name, which proves nothing:
the wrong kind of token passed, the banner said "verified" even when nothing had been checked, and
when the token later died the operator got "400 Client Error" on episode after episode while the
Channels page still showed "● Active" — with no way back except deleting the channel and its
campaigns with it.
"""
from __future__ import annotations

import json

import pytest

from services import verification as _verification

# Captured at import (collection time), before conftest's autouse `no_live_credential_checks` fixture
# replaces it: the tests below drive the REAL check with a faked `requests`, so they need it back.
_REAL_CHECK = _verification.check_facebook_page


@pytest.fixture
def real_check(monkeypatch):
    monkeypatch.setattr(_verification, "check_facebook_page", _REAL_CHECK)


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


class FakeResp:
    """Minimal `requests.Response` stand-in — Graph is never reachable from the test sandbox."""

    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload or {})
        self.headers: dict = {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _page(**over):
    data = {"id": "1234567890", "name": "Mẹo Bếp Nhà Mình", "category": "Food & Beverage",
            "picture": {"data": {"url": "https://scontent.example/pic.jpg"}}}
    data.update(over)
    return data


def _graph_error(message, code=None, etype=None, status=400):
    err = {"message": message}
    if code is not None:
        err["code"] = code
    if etype:
        err["type"] = etype
    return FakeResp(status, {"error": err})


# ── F1: the check asks the only question that matters ────────────────────────
def test_a_user_token_is_refused_however_readable_the_page_is(monkeypatch, real_check):
    """THE bug: the old check read `/{page_id}?fields=id,name`, which a short-lived USER token — the
    one the Graph Explorer hands you by default — reads perfectly. The channel saved as verified and
    died hours later at publish time."""
    import requests

    from services import verification

    # A user token's /me is a person: it has no `category`. That single field separates them.
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: FakeResp(200, {"id": "777", "name": "Trung Tuấn"}))
    check = verification.check_facebook_page("1234567890", "user-token")
    assert check.ok is False
    assert "not a Page Access Token" in check.detail
    assert "Trung Tuấn" in check.detail          # says WHOSE token it is


def test_a_real_page_token_verifies_and_returns_the_page_identity(monkeypatch, real_check):
    import requests

    from services import verification

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, _page()))
    check = verification.check_facebook_page("1234567890", "page-token")
    assert check.ok is True
    assert check.page_id == "1234567890"
    assert check.name == "Mẹo Bếp Nhà Mình"
    assert check.picture == "https://scontent.example/pic.jpg"


def test_a_page_token_for_the_wrong_page_is_refused(monkeypatch, real_check):
    """Two Pages, two tokens, one copy-paste slip — this would have published to the wrong Page."""
    import requests

    from services import verification

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, _page(id="999")))
    check = verification.check_facebook_page("1234567890", "other-page-token")
    assert check.ok is False
    assert "999" in check.detail and "1234567890" in check.detail


def test_the_check_asks_me_not_the_public_page(monkeypatch, real_check):
    import requests

    from services import verification

    seen = {}

    def spy(url, **kw):
        seen["url"] = url
        seen["fields"] = (kw.get("params") or {}).get("fields", "")
        return FakeResp(200, _page())

    monkeypatch.setattr(requests, "get", spy)
    verification.check_facebook_page("1234567890", "t")
    assert seen["url"].endswith("/me")
    assert "category" in seen["fields"]          # the field that identifies a Page


def test_graphs_own_rejection_reaches_the_operator_without_the_token(monkeypatch, real_check):
    import requests

    from services import verification

    monkeypatch.setattr(requests, "get", lambda *a, **k: _graph_error(
        "Error validating access token: Session has expired. access_token=SEKRET",
        code=190, etype="OAuthException", status=400))
    check = verification.check_facebook_page("1", "SEKRET")
    assert check.ok is False
    assert "Session has expired" in check.detail
    assert "SEKRET" not in check.detail


def test_a_server_hiccup_is_still_could_not_tell(monkeypatch, real_check):
    import requests

    from services import verification

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(503, {}))
    assert verification.check_facebook_page("1", "t").ok is None


# ── F5: paste whatever you have ──────────────────────────────────────────────
@pytest.mark.parametrize(("raw", "want"), [
    ("1234567890", "1234567890"),
    ("https://www.facebook.com/MyPage", "MyPage"),
    ("http://m.facebook.com/pg/MyPage/posts/", "MyPage"),
    ("facebook.com/profile.php?id=98765", "98765"),
    ("@MyPage", "MyPage"),
    ("  MyPage  ", "MyPage"),
    ("", ""),
])
def test_a_pasted_page_url_becomes_a_usable_id(raw, want):
    """A full URL used to be stored verbatim, so every later Graph call 404'd — and the save went
    through anyway, because "could not tell" is allowed to pass."""
    from services import verification

    assert verification.normalize_page_id(raw) == want


def test_the_canonical_numeric_id_is_stored_not_the_username(client, session, monkeypatch):
    """A username works until the day the operator renames the Page; the numeric id is forever."""
    from services import verification

    monkeypatch.setattr(verification, "check_facebook_page",
                        lambda page_id, token: verification.PageCheck(
                            True, "Verified: My Page.", page_id="1234567890", name="My Page"))
    client.post("/channels/facebook", data={
        "page_id": "https://facebook.com/MyPage", "page_access_token": "tok"},
        follow_redirects=False)
    creds = json.loads(_first_channel(session).encrypted_credentials)
    assert creds["page_id"] == "1234567890"


def _first_channel(session):
    from sqlalchemy import select

    from database.models import Channel

    return session.scalars(select(Channel)).first()


# ── F2: the banner tells the truth ───────────────────────────────────────────
def test_an_unverified_save_does_not_claim_it_was_verified(client, session, monkeypatch):
    from services import verification

    monkeypatch.setattr(verification, "check_facebook_page",
                        lambda page_id, token: verification.PageCheck(None, "unreachable"))
    r = client.post("/channels/facebook", data={
        "channel_name": "P", "page_id": "1", "page_access_token": "t"}, follow_redirects=False)
    assert r.headers["location"] == "/channels?flash=fb_added_unverified"
    page = client.get("/channels?flash=fb_added_unverified").text
    assert "not verified" in page
    assert "Facebook confirmed" not in page


def test_a_verified_save_may_say_verified(client, monkeypatch):
    from services import verification

    monkeypatch.setattr(verification, "check_facebook_page",
                        lambda page_id, token: verification.PageCheck(
                            True, "ok", page_id="1", name="P"))
    r = client.post("/channels/facebook", data={
        "page_id": "1", "page_access_token": "t"}, follow_redirects=False)
    assert r.headers["location"] == "/channels?flash=fb_added"
    assert "Facebook confirmed" in client.get("/channels?flash=fb_added").text


def test_empty_input_is_refused_before_anything_is_stored(client, session):
    r = client.post("/channels/facebook", data={
        "channel_name": "X", "page_id": "   ", "page_access_token": "  "}, follow_redirects=False)
    assert "flash=fb_rejected" in r.headers["location"]
    assert _first_channel(session) is None


# ── F7: the Page's own name and picture ──────────────────────────────────────
def test_a_verified_page_fills_in_its_own_name_and_avatar(client, session, monkeypatch):
    """Retyping what Facebook just told us is busywork, and a hand-typed name drifts from the Page."""
    from services import verification

    monkeypatch.setattr(verification, "check_facebook_page",
                        lambda page_id, token: verification.PageCheck(
                            True, "ok", page_id="1", name="Mẹo Bếp Nhà Mình",
                            picture="https://scontent.example/pic.jpg"))
    client.post("/channels/facebook", data={
        "page_id": "1", "page_access_token": "t"}, follow_redirects=False)
    ch = _first_channel(session)
    assert ch.channel_name == "Mẹo Bếp Nhà Mình"
    assert ch.avatar_url == "https://scontent.example/pic.jpg"


def test_the_operators_own_label_always_wins(client, session, monkeypatch):
    from services import verification

    monkeypatch.setattr(verification, "check_facebook_page",
                        lambda page_id, token: verification.PageCheck(
                            True, "ok", page_id="1", name="Official Name", picture="https://x/p.jpg"))
    client.post("/channels/facebook", data={
        "channel_name": "My own label", "page_id": "1", "page_access_token": "t"},
        follow_redirects=False)
    assert _first_channel(session).channel_name == "My own label"


# ── F3: Facebook's own words, and the right retry verdict ────────────────────
def test_a_graph_failure_carries_facebooks_explanation_not_400_bad_request():
    from services import facebook_service as fb

    with pytest.raises(fb.FacebookError) as err:
        fb.raise_for_graph(_graph_error("(#100) Missing video file", code=100), what="Facebook upload")
    assert "Missing video file" in str(err.value)


def test_a_dead_token_raises_the_auth_type_and_reads_as_a_credential_problem():
    from core import failure
    from services import facebook_service as fb

    with pytest.raises(fb.FacebookAuthError) as err:
        fb.raise_for_graph(
            _graph_error("Error validating access token: Session has expired on Tuesday.",
                         code=190, etype="OAuthException"),
            token="SEKRET", what="Facebook upload")
    msg = str(err.value)
    assert "OAuth error 190" in msg
    # The classification the autopilot reads: retrying cannot mint a new token.
    assert failure.is_transient(msg) is False
    diag = failure.diagnose(msg)
    assert diag["cause"] == "The Facebook Page token is no longer valid"
    assert diag["href"] == "/channels"          # the fix is on the channel, not /credentials


def test_a_missing_api_key_still_points_at_credentials():
    """The two credential classes must not collapse into one — their fixes are on different pages."""
    from core import failure

    assert failure.diagnose("Missing Gemini API key")["href"] == "/credentials"


def test_the_token_never_survives_into_an_error_string():
    from services import facebook_service as fb

    leak = ("HTTPSConnectionPool: POST https://graph-video.facebook.com/v23.0/1/videos"
            "?access_token=EAAsecret failed")
    cleaned = fb.scrub(leak, "EAAsecret")
    assert "EAAsecret" not in cleaned and "access_token=***" in cleaned
    # Even a token we were not handed is stripped by the URL pattern.
    assert "other" not in fb.scrub("?access_token=other", None)


def test_a_stored_render_error_is_scrubbed(session, user, channel, monkeypatch):
    """Task.error_message is rendered on the episode page AND in the alert bell."""
    from database.models import Campaign, Task
    from database.types import CampaignStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=1,
                   status=CampaignStatus.active, config_json={})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)

    monkeypatch.setattr(video_worker, "_notify", lambda *a, **k: None)
    exc = RuntimeError("boom https://graph.facebook.com/v23.0/1?access_token=EAAsecret")
    video_worker._fail_task(session, t, user, cam, exc, "render_task")
    session.refresh(t)
    assert "EAAsecret" not in (t.error_message or "")


# ── F4: an expired channel says so, and can be fixed ─────────────────────────
def test_a_dead_token_retires_the_channel(session, user, channel, monkeypatch):
    """Nothing ever set ChannelStatus.expired, so the pill and the filter chip were decoration and a
    channel that could not publish anything still showed "● Active"."""
    from database.models import Campaign, Task
    from database.types import CampaignStatus, ChannelStatus, Platform
    from services.facebook_service import FacebookAuthError
    from workers import video_worker

    channel.platform = Platform.facebook
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=1,
                   status=CampaignStatus.active, config_json={})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)

    notes = []
    monkeypatch.setattr(video_worker, "_notify", lambda user, msg: notes.append(msg))
    video_worker._fail_task(session, t, user, cam, FacebookAuthError("Facebook upload: OAuth error 190"),
                            "render_task")
    session.refresh(channel)
    assert channel.status == ChannelStatus.expired
    assert any("expired" in n for n in notes)      # the operator is told, not left to notice


def test_an_ordinary_render_failure_leaves_the_channel_alone(session, user, channel, monkeypatch):
    from database.models import Campaign, Task
    from database.types import CampaignStatus, ChannelStatus
    from workers import video_worker

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=1,
                   status=CampaignStatus.active, config_json={})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add(t)
    session.commit()
    session.refresh(t)

    monkeypatch.setattr(video_worker, "_notify", lambda *a, **k: None)
    video_worker._fail_task(session, t, user, cam, RuntimeError("ffmpeg died"), "render_task")
    session.refresh(channel)
    assert channel.status == ChannelStatus.active


def test_an_expired_channel_is_a_red_alert(session, user, channel):
    import main
    from database.types import ChannelStatus

    channel.status = ChannelStatus.expired
    session.commit()
    rows = {r["key"]: r for r in main._credential_alerts(session, user)}
    row = rows[f"channel-expired:{channel.id}"]
    assert row["level"] == "red" and row["href"] == "/channels"
    assert row["channel"] == channel.channel_name


def test_a_fresh_verified_token_revives_the_channel(client, session, user, channel, monkeypatch):
    """Marking a channel expired without a way back would be a dead end — and the only previous way
    back, Remove + re-add, deletes the channel's campaigns and rendered videos with it."""
    from database.types import ChannelStatus, Platform
    from services import verification

    channel.platform = Platform.facebook
    channel.status = ChannelStatus.expired
    channel.encrypted_credentials = json.dumps({"page_id": "1", "page_access_token": "old"})
    session.commit()

    monkeypatch.setattr(verification, "check_facebook_page",
                        lambda page_id, token: verification.PageCheck(
                            True, "ok", page_id="1", name="P"))
    r = client.post(f"/channels/{channel.id}/facebook-token",
                    data={"page_access_token": "fresh"}, follow_redirects=False)
    assert r.headers["location"] == "/channels?flash=fb_token_ok"
    session.expire_all()
    session.refresh(channel)
    assert channel.status == ChannelStatus.active
    assert json.loads(channel.encrypted_credentials)["page_access_token"] == "fresh"


def test_a_refused_replacement_keeps_the_old_token(client, session, channel, monkeypatch):
    """Losing a working token to a typo would be worse than the problem being fixed."""
    from database.types import ChannelStatus, Platform
    from services import verification

    channel.platform = Platform.facebook
    channel.encrypted_credentials = json.dumps({"page_id": "1", "page_access_token": "good"})
    session.commit()

    monkeypatch.setattr(verification, "check_facebook_page",
                        lambda page_id, token: verification.PageCheck(False, "Invalid token."))
    r = client.post(f"/channels/{channel.id}/facebook-token",
                    data={"page_access_token": "typo"}, follow_redirects=False)
    assert "flash=fb_token_bad" in r.headers["location"]
    session.expire_all()
    session.refresh(channel)
    assert json.loads(channel.encrypted_credentials)["page_access_token"] == "good"
    assert channel.status == ChannelStatus.active


def test_an_unverified_replacement_is_stored_but_does_not_declare_success(client, session, channel,
                                                                         monkeypatch):
    from database.types import ChannelStatus, Platform
    from services import verification

    channel.platform = Platform.facebook
    channel.status = ChannelStatus.expired
    channel.encrypted_credentials = json.dumps({"page_id": "1", "page_access_token": "old"})
    session.commit()

    monkeypatch.setattr(verification, "check_facebook_page",
                        lambda page_id, token: verification.PageCheck(None, "unreachable"))
    r = client.post(f"/channels/{channel.id}/facebook-token",
                    data={"page_access_token": "maybe"}, follow_redirects=False)
    assert r.headers["location"] == "/channels?flash=fb_token_unverified"
    session.expire_all()
    session.refresh(channel)
    assert json.loads(channel.encrypted_credentials)["page_access_token"] == "maybe"
    assert channel.status == ChannelStatus.expired      # unverified never clears the flag


def test_a_youtube_channel_has_no_page_token_to_replace(client, channel):
    r = client.post(f"/channels/{channel.id}/facebook-token",
                    data={"page_access_token": "x"}, follow_redirects=False)
    assert r.status_code == 400


def test_the_expired_card_tells_a_page_owner_the_right_thing(client, session, channel):
    """It used to tell every expired channel to "reconnect via OAuth" — a button that does not exist
    for a Facebook Page."""
    from database.types import ChannelStatus, Platform

    channel.platform = Platform.facebook
    channel.status = ChannelStatus.expired
    session.commit()
    body = client.get("/channels").text
    assert "permanent Page Access Token" in body
    assert "reconnect it via OAuth" not in body
    assert f"/channels/{channel.id}/facebook-token" in body


# ── F6: one Graph version ────────────────────────────────────────────────────
def test_no_graph_url_is_hardcoded_anywhere():
    """It was hardcoded in four places — the same copy-the-list pattern that hid the vintage grade.
    Now every Graph URL is built from one constant, so a version bump is a one-line change."""
    import pathlib
    import re

    hits = []
    for path in pathlib.Path(".").glob("**/*.py"):
        if any(part in {".git", "__pycache__", "tests"} for part in path.parts):
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"graph(-video)?\.facebook\.com/v\d+\.\d+", line):
                hits.append(f"{path}:{i}")
    assert hits == [], f"a hardcoded Graph version escaped facebook_service: {hits}"


def test_every_facebook_call_site_uses_the_shared_constant():
    from services import analytics_service, facebook_service, verification

    assert facebook_service.GRAPH.endswith(facebook_service.GRAPH_VERSION)
    for mod in (analytics_service, verification):
        src = open(mod.__file__, encoding="utf-8").read()
        assert "graph.facebook.com/v" not in src, f"{mod.__name__} builds its own Graph URL"


def test_the_pinned_version_is_not_ancient():
    """Meta keeps a version alive ~2 years; v20 (May 2024) is past that."""
    from services.facebook_service import GRAPH_VERSION

    assert int(GRAPH_VERSION.lstrip("v").split(".")[0]) >= 21


# ── Guidance: the step everyone gets stuck on ────────────────────────────────
def test_the_form_explains_how_to_get_a_permanent_page_token(client):
    body = client.get("/channels").text
    assert "How do I get a permanent Page Access Token?" in body
    assert "User or Page" in body                  # the dropdown that decides everything
    assert "Extend Access Token" in body           # the step that makes it permanent
    assert "pages_manage_posts" in body


def test_the_help_lives_in_one_partial():
    """Add-a-Page and Replace-token must not drift into two different sets of instructions."""
    import pathlib

    src = pathlib.Path("templates/channels.html").read_text(encoding="utf-8")
    assert src.count('include "_fb_token_help.html"') == 2
    assert "Extend Access Token" not in src        # the text itself lives only in the partial


# ── G1: a permalink that actually resolves ───────────────────────────────────
def test_a_facebook_permalink_is_a_real_video_url():
    """`facebook.com/{video_id}` is not a video permalink, so every "View ↗" for a Facebook publish
    led nowhere — on the episode page and in the dashboard feed alike."""
    from services import facebook_service as fb

    assert fb.permalink("123", reel=True) == "https://www.facebook.com/reel/123"
    assert fb.permalink("123", reel=False) == "https://www.facebook.com/watch/?v=123"


def test_the_permalink_follows_the_format_on_both_platforms():
    from database.types import Platform
    from workers.video_worker import published_url_for

    assert published_url_for(Platform.facebook, "9", "short").endswith("/reel/9")
    assert published_url_for(Platform.facebook, "9", "long").endswith("watch/?v=9")
    assert published_url_for(Platform.youtube, "9", "short").endswith("/shorts/9")
    # A 15-minute video is not a Short; /shorts/ was being built for those too.
    assert published_url_for(Platform.youtube, "9", "long").endswith("watch?v=9")


# ── G2: a vertical short is a REEL ───────────────────────────────────────────
def test_a_short_goes_to_the_reels_endpoint_and_long_form_does_not():
    from services import facebook_service as fb

    assert fb.is_reel({"video_format": "short"}) is True
    assert fb.is_reel({}) is True                       # short is the product's default
    assert fb.is_reel({"video_format": "long"}) is False


def test_a_reel_is_uploaded_in_three_phases(monkeypatch, tmp_path, channel):
    """Posting a 9:16 clip to /videos makes an ordinary Page video that never enters Reels
    distribution — the entire reason this product renders vertical video."""
    import requests

    from services import facebook_service as fb

    channel.encrypted_credentials = json.dumps({"page_id": "P", "page_access_token": "tok"})
    f = tmp_path / "v.mp4"
    f.write_bytes(b"video-bytes")
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw.get("data") if isinstance(kw.get("data"), dict) else "<bytes>",
                      kw.get("headers") or {}))
        if url.endswith("/video_reels") and (kw.get("data") or {}).get("upload_phase") == "start":
            return FakeResp(200, {"video_id": "REEL1", "upload_url": "https://rupload/x"})
        return FakeResp(200, {"success": True})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(fb, "find_existing_upload", lambda *a, **k: None)
    vid = fb.upload_video(channel, str(f), {"video_format": "short", "description": "d"})
    assert vid == "REEL1"
    phases = [(c[1] or {}).get("upload_phase") for c in calls if isinstance(c[1], dict)]
    assert "start" in phases and "finish" in phases
    # The bytes go to the reserved upload URL with an offset header — resumable, not one big POST.
    transfer = [c for c in calls if c[0] == "https://rupload/x"][0]
    assert transfer[2]["offset"] == "0" and "file_size" in transfer[2]
    assert transfer[2]["Authorization"].startswith("OAuth ")


def test_long_form_still_goes_to_the_page_video_endpoint(monkeypatch, tmp_path, channel):
    import requests

    from services import facebook_service as fb

    channel.encrypted_credentials = json.dumps({"page_id": "P", "page_access_token": "tok"})
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["data"] = kw.get("data")
        return FakeResp(200, {"id": "VID9"})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(fb, "find_existing_upload", lambda *a, **k: None)
    assert fb.upload_video(channel, str(f), {"video_format": "long", "title": "T"}) == "VID9"
    assert seen["url"].endswith("/P/videos")


# ── The comparison with YouTube: privacy and the CTA ─────────────────────────
def test_a_private_campaign_does_not_publish_publicly_on_facebook(monkeypatch, tmp_path, channel):
    """Found by diffing the two publish paths: YouTube honoured `privacy`, Facebook ignored it — so a
    campaign set to private posted PUBLICLY to the Page (ADR-073)."""
    import requests

    from services import facebook_service as fb

    channel.encrypted_credentials = json.dumps({"page_id": "P", "page_access_token": "tok"})
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    states = []

    def fake_post(url, **kw):
        d = kw.get("data") if isinstance(kw.get("data"), dict) else {}
        if d.get("upload_phase") == "start":
            return FakeResp(200, {"video_id": "R1", "upload_url": "https://rupload/x"})
        if d.get("upload_phase") == "finish":
            states.append(d.get("video_state"))
        return FakeResp(200, {"id": "R1"})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(fb, "find_existing_upload", lambda *a, **k: None)

    fb.upload_video(channel, str(f), {"video_format": "short", "privacy": "private"})
    fb.upload_video(channel, str(f), {"video_format": "short", "privacy": "public"})
    assert states == ["DRAFT", "PUBLISHED"]


def test_an_unlisted_long_video_is_not_published(monkeypatch, tmp_path, channel):
    import requests

    from services import facebook_service as fb

    channel.encrypted_credentials = json.dumps({"page_id": "P", "page_access_token": "tok"})
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    seen = {}
    monkeypatch.setattr(requests, "post",
                        lambda url, **kw: (seen.update(kw.get("data") if isinstance(kw.get("data"), dict) else {}),
                                           FakeResp(200, {"id": "V"}))[1])
    monkeypatch.setattr(fb, "find_existing_upload", lambda *a, **k: None)
    fb.upload_video(channel, str(f), {"video_format": "long", "privacy": "unlisted"})
    assert seen["published"] == "false"


def test_the_cta_is_posted_as_a_comment_like_youtube_does(monkeypatch, tmp_path, channel):
    """YouTube has always posted the CTA (which carries the affiliate link) as a comment; Facebook
    dropped it silently, so monetization simply did not exist on half the channels."""
    import requests

    from services import facebook_service as fb

    channel.encrypted_credentials = json.dumps({"page_id": "P", "page_access_token": "tok"})
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    comments = []

    def fake_post(url, **kw):
        d = kw.get("data") if isinstance(kw.get("data"), dict) else {}
        if url.endswith("/comments"):
            comments.append(d.get("message"))
            return FakeResp(200, {"id": "c1"})
        if d.get("upload_phase") == "start":
            return FakeResp(200, {"video_id": "R1", "upload_url": "https://rupload/x"})
        return FakeResp(200, {"id": "R1"})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(fb, "find_existing_upload", lambda *a, **k: None)
    fb.upload_video(channel, str(f), {"video_format": "short", "cta": "Shop → https://x (affiliate)"})
    assert comments == ["Shop → https://x (affiliate)"]


def test_a_failed_cta_comment_never_fails_a_published_video(monkeypatch, tmp_path, channel):
    import requests

    from services import facebook_service as fb

    channel.encrypted_credentials = json.dumps({"page_id": "P", "page_access_token": "tok"})
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")

    def fake_post(url, **kw):
        d = kw.get("data") if isinstance(kw.get("data"), dict) else {}
        if url.endswith("/comments"):
            return _graph_error("comments disabled", code=200)
        if d.get("upload_phase") == "start":
            return FakeResp(200, {"video_id": "R1", "upload_url": "https://rupload/x"})
        return FakeResp(200, {"id": "R1"})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(fb, "find_existing_upload", lambda *a, **k: None)
    assert fb.upload_video(channel, str(f), {"video_format": "short", "cta": "hi"}) == "R1"


# ── G3: never post the same episode twice ────────────────────────────────────
def test_an_upload_that_already_landed_is_adopted_not_reposted(monkeypatch, tmp_path, channel):
    """An upload that succeeds server-side but times out client-side looks exactly like a failure, so
    the retry used to put a second copy on the Page."""
    from services import facebook_service as fb

    channel.encrypted_credentials = json.dumps({"page_id": "P", "page_access_token": "tok"})
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(fb, "find_existing_upload", lambda *a, **k: "ALREADY")

    import requests

    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: pytest.fail("must not upload a second copy"))
    assert fb.upload_video(channel, str(f), {"video_format": "short"},
                           pending_video_id="ALREADY") == "ALREADY"


def test_the_reel_id_is_persisted_before_the_bytes_go_up(monkeypatch, tmp_path, channel):
    """That is what makes the retry check possible at all: the id exists before the risky part."""
    import requests

    from services import facebook_service as fb

    channel.encrypted_credentials = json.dumps({"page_id": "P", "page_access_token": "tok"})
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    order = []

    def fake_post(url, **kw):
        d = kw.get("data") if isinstance(kw.get("data"), dict) else {}
        if d.get("upload_phase") == "start":
            return FakeResp(200, {"video_id": "R7", "upload_url": "https://rupload/x"})
        order.append("transfer" if url == "https://rupload/x" else "other")
        return FakeResp(200, {"id": "R7"})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(fb, "find_existing_upload", lambda *a, **k: None)
    fb.upload_video(channel, str(f), {"video_format": "short"},
                    on_pending=lambda vid: order.append(f"pending:{vid}"))
    assert order[0] == "pending:R7", order      # persisted first, uploaded second


def test_the_worker_stores_the_pending_id_before_uploading(session, user, channel, monkeypatch,
                                                           tmp_path):
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, CampaignStatus, Platform
    from workers import video_worker

    channel.platform = Platform.facebook
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=1,
                   status=CampaignStatus.active, config_json={"video_format": "short"})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    buf = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                         status=BufferStatus.ready, video_path=str(f), metadata_json={"title": "T"})
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1)
    session.add_all([buf, t])
    session.commit()
    session.refresh(buf)
    session.refresh(t)

    def fake_publish(channel, path, metadata, user, *, pending_video_id=None, on_pending=None):
        assert metadata["video_format"] == "short"      # the format reaches the uploader
        on_pending("R42")
        return "R42"

    monkeypatch.setattr(video_worker, "_publish", fake_publish)
    monkeypatch.setattr(video_worker, "_notify", lambda *a, **k: None)
    video_worker._publish_buffer(session, t, buf, cam, channel, user)
    session.refresh(t)
    assert t.published_url == "https://www.facebook.com/reel/R42"


# ── G5: one batched insights call ────────────────────────────────────────────
def test_insights_are_fetched_in_one_batch(monkeypatch, channel):
    """Fifty round trips every stats pass was fifty chances to be rate-limited, for data that fits
    in one response."""
    import requests

    from services import analytics_service

    channel.encrypted_credentials = json.dumps({"page_id": "P", "page_access_token": "tok"})
    posts = []

    def fake_post(url, **kw):
        posts.append(kw.get("data") or {})
        return FakeResp(200, [{"code": 200, "body": json.dumps({"data": [{"values": [{"value": 12}]}]})},
                              {"code": 200, "body": json.dumps({"data": [{"values": [{"value": 7}]}]})}])

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("should not fetch one by one"))
    out = analytics_service.fetch_facebook_stats(channel, ["v1", "v2"])
    assert out == {"v1": {"views": 12}, "v2": {"views": 7}}
    assert len(posts) == 1 and "batch" in posts[0]


def test_one_unreadable_video_does_not_discard_the_others(monkeypatch, channel):
    import requests

    from services import analytics_service

    channel.encrypted_credentials = json.dumps({"page_id": "P", "page_access_token": "tok"})
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, [
        {"code": 400, "body": json.dumps({"error": {"message": "no access"}})},
        {"code": 200, "body": json.dumps({"data": [{"values": [{"value": 5}]}]})},
    ]))
    assert analytics_service.fetch_facebook_stats(channel, ["bad", "good"]) == {"good": {"views": 5}}


# ── G6: only an https avatar reaches the page ────────────────────────────────
@pytest.mark.parametrize(("raw", "want"), [
    ("https://scontent.example/p.jpg", "https://scontent.example/p.jpg"),
    ("http://insecure.example/p.jpg", None),      # mixed content over the tunnel
    ("javascript:alert(1)", None),
    ("not a url", None),
    ("", None),
])
def test_only_an_https_avatar_is_stored(raw, want):
    import main

    assert main._safe_avatar(raw) == want


# ── G7: an expired channel parks the episode instead of failing it ───────────
def test_an_expired_channel_keeps_the_episode_in_the_buffer(session, user, channel, monkeypatch,
                                                            tmp_path):
    """The credential is what is broken, not this episode — failing it would burn a retry and read as
    a broken render, and the rendered video is perfectly good."""
    from database.models import BufferPoolItem, Campaign, Task
    from database.types import BufferStatus, CampaignStatus, ChannelStatus, Platform, TaskStatus
    from workers import video_worker

    channel.platform = Platform.facebook
    channel.status = ChannelStatus.expired
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="T", total_episodes=1,
                   status=CampaignStatus.active, config_json={})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    buf = BufferPoolItem(campaign_id=cam.id, channel_id=channel.id, episode_number=1,
                         status=BufferStatus.ready, video_path=str(f), metadata_json={})
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=1, status=TaskStatus.SCHEDULED)
    session.add_all([buf, t])
    session.commit()
    session.refresh(buf)
    session.refresh(t)

    monkeypatch.setattr(video_worker, "_publish",
                        lambda *a, **k: pytest.fail("must not upload to an expired channel"))
    video_worker.publish_task(buf.id)
    session.expire_all()
    session.refresh(buf)
    session.refresh(t)
    assert buf.status == BufferStatus.ready          # still there, ready for the next slot
    assert t.status != TaskStatus.FAILED             # not a failure of this episode
    assert t.retry_count == 0                        # and it did not burn a retry


# ── ADR-074: the refusal an operator reported as "the screen flashed, nothing happened" ──────
def test_pasting_the_page_id_into_the_token_box_is_named_exactly(real_check):
    """The real report. Graph answers this with "Cannot parse access token", which is true and
    useless — it never says WHICH of the two boxes is wrong. Caught locally, before any call."""
    from services import verification

    check = verification.check_facebook_page("1175508495653784", "1175508495653784")
    assert check.ok is False
    assert "pasted the Page ID into the token box" in check.detail
    assert "EAA" in check.detail


def test_a_bare_number_is_never_mistaken_for_a_token(real_check, monkeypatch):
    import requests

    from services import verification

    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("no call needed to know this"))
    check = verification.check_facebook_page("MyPage", "9876543210")
    assert check.ok is False and "not an access token" in check.detail


def test_a_real_looking_token_still_goes_to_facebook(real_check, monkeypatch):
    """The local guard must only catch the obvious mistake, never shortcut a genuine token."""
    import requests

    from services import verification

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, _page()))
    assert verification.check_facebook_page("1234567890", "EAAabc123").ok is True


def test_a_refusal_returns_the_operator_to_the_form_with_their_values(client, session, monkeypatch):
    """The disclosure sits at the bottom of a long page. Reloading to the top with the form collapsed
    and emptied is why a refusal read as "nothing happened"."""
    from services import verification

    monkeypatch.setattr(verification, "check_facebook_page",
                        lambda page_id, token: verification.PageCheck(False, "Cannot parse token"))
    r = client.post("/channels/facebook", data={
        "page_id": "1175508495653784", "page_access_token": "1175508495653784",
        "channel_name": "Trang Bếp", "avatar_url": ""}, follow_redirects=False)
    loc = r.headers["location"]
    assert loc.endswith("#fb-form")                      # scrolls back to the form that failed
    assert "fb_page_id=1175508495653784" in loc
    assert "fb_name=Trang" in loc                        # their label survives too
    assert "1175508495653784&fb" not in loc.split("fb_page_id=")[0]   # sanity: no stray duplication


def test_the_token_is_never_echoed_into_the_url(client, monkeypatch):
    """Everything else comes back; a credential must not travel in a query string."""
    from services import verification

    monkeypatch.setattr(verification, "check_facebook_page",
                        lambda page_id, token: verification.PageCheck(False, "nope"))
    r = client.post("/channels/facebook", data={
        "page_id": "1", "page_access_token": "EAAsupersecret"}, follow_redirects=False)
    assert "EAAsupersecret" not in r.headers["location"]


def test_the_form_reopens_prefilled_and_shows_the_error_next_to_it(client):
    page = client.get("/channels?flash=fb_rejected&flash_reason=Cannot+parse+access+token"
                      "&fb_page_id=1175508495653784&fb_name=Trang+B%E1%BA%BFp").text
    form = page.split('id="fb-form"', 1)[1]
    assert form.lstrip().startswith("open")              # the disclosure is open again
    assert 'value="1175508495653784"' in form            # the Page id is still there
    assert 'value="Trang Bếp"' in form                   # and the label
    # The error is repeated AT the form, not only in a banner two screens above it.
    assert "Not connected" in form and "Cannot parse access token" in form
    # The token field is empty — it is never round-tripped.
    token_field = form.split('name="page_access_token"', 1)[1].split(">", 1)[0]
    assert "value=" not in token_field


# ── ADR-075 — no password manager may touch a credential box ────────────────────────────────
# Reported live: an operator connected a Page, saw the screen flash, and no channel appeared. The
# POST carried `page_id=1175508495653784&page_access_token=1175508495653784` — the Page ID in BOTH
# boxes — yet they were certain they had pasted a real token. Nothing in our JS touches these fields
# and the two forms are siblings, not nested, so the value was substituted by the browser: /channels
# offered a text input directly above a `type="password"` input (a login form, as far as Chrome is
# concerned) plus one more token box per connected Page, and not one of them said "do not manage
# this". A saved entry then refills a field the operator is not looking at.

def _password_inputs(html: str) -> list[str]:
    """Every `<input …type="password"…>` tag in a rendered page, as raw tag text."""
    import re

    return [m.group(0) for m in re.finditer(r"<input\b[^>]*>", html)
            if 'type="password"' in m.group(0)]


# `new-password` is the load-bearing one — Chrome ignores `off` on a password field. The rest are
# cheap insurance for third-party managers. NOT asserted: autocorrect/autocapitalize/spellcheck,
# which the platform already disables on type="password" (they were noise, and are gone).
SUPPRESSORS = ('autocomplete="new-password"', "data-1p-ignore", 'data-lpignore="true"',
               "data-bwignore", 'data-form-type="other"')


@pytest.mark.parametrize("path", ["/credentials", "/channels"])
def test_no_credential_box_is_left_for_a_password_manager_to_fill(client, session, user, path):
    """Every secret box on every page, including the per-Page token panels."""
    from database.models import Channel
    from database.types import ChannelStatus, Platform

    session.add(Channel(user_id=user.id, platform=Platform.facebook, channel_name="Trang Bếp",
                        encrypted_credentials="{}", status=ChannelStatus.expired))
    session.commit()

    boxes = _password_inputs(client.get(path).text)
    assert boxes, f"{path} renders no password input — did the fixture stop seeding?"
    for tag in boxes:
        for attr in SUPPRESSORS:
            assert attr in tag, f"{path}: credential box missing {attr} → {tag}"


def test_the_page_id_box_is_not_offered_as_a_username(client):
    """It sits directly above the token box, which is exactly the pair Chrome saves and refills."""
    form = client.get("/channels").text.split('action="/channels/facebook"', 1)[1]
    assert 'autocomplete="off"' in form.split(">", 1)[0]          # …on the form itself
    page_id = form.split('name="page_id"', 1)[1].split(">", 1)[0]
    assert 'autocomplete="off"' in page_id                        # …and on the field


def test_a_page_id_cannot_even_be_submitted_as_a_token(client, session, user):
    """Defence in depth: the server rejects `token == page_id`, but the browser should never let it
    leave. A Page Access Token is ~200 characters; a Page ID is ~16 digits."""
    from database.models import Channel
    from database.types import Platform

    session.add(Channel(user_id=user.id, platform=Platform.facebook, channel_name="Trang Bếp",
                        encrypted_credentials="{}"))
    session.commit()

    for tag in _password_inputs(client.get("/channels").text):
        assert 'minlength="40"' in tag, f"token box accepts a short value → {tag}"


def test_secret_boxes_are_rendered_by_one_macro(client):
    """Six hand-written copies is five chances to forget an attribute — the exact failure mode that
    hid the `vintage` grade and hardcoded four Graph versions. There is one definition."""
    import pathlib

    offenders = [p.name for p in pathlib.Path("templates").glob("*.html")
                 if 'type="password"' in p.read_text(encoding="utf-8")
                 and p.name not in {"macros.html", "login.html"}]   # login IS a login: it may save
    assert not offenders, f"hand-written secret input(s) — use ui.secret(): {offenders}"
