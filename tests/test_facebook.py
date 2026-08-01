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
