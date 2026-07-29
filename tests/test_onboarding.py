"""R5 — surviving the first hour, and never being told to do something impossible.

Every test here is a place a first-time (or unlucky) operator previously hit a wall with no way out:
a "connect YouTube" button that led to a Google error page, a Facebook Page that saved as Active with
a made-up token, a campaign started with no API keys and a dashboard that answered "All clear", a
render failure printed as a stack trace with a single Retry button, and a 404 with no navigation.
"""
from __future__ import annotations

from datetime import datetime

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def _campaign(session, user, channel, **cfg):
    from database.models import Campaign
    from database.types import CampaignStatus

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name=cfg.pop("topic", "Onboard"),
                 total_episodes=5, status=cfg.pop("status", CampaignStatus.active), config_json=cfg)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


# ── First-run checklist ──────────────────────────────────────────────────────
def test_setup_state_reports_the_first_unfinished_step(user, channel):
    import main

    empty = main._setup_state(user, [], [])
    assert empty["done"] is False and empty["next"] == "channels"

    with_channel = main._setup_state(user, [channel], [])
    assert with_channel["channels"] is True and with_channel["next"] == "campaigns"  # keys are set

    class NoKeys:
        gemini_api_key = None
        pexels_api_key = None

    assert main._setup_state(NoKeys(), [channel], [])["next"] == "api_keys"


def test_a_fresh_dashboard_leads_with_setup_and_does_not_claim_all_clear(client):
    """"All clear — nothing needs you right now" was the first sentence an account with no channel,
    no keys and no campaign ever read."""
    body = client.get("/").text
    assert "Finish setting up your factory" in body
    assert "0 of 3 done" in body
    # The all-clear card is still in the DOM (the poller reveals it) but must start hidden.
    allclear = body.split('id="allclear-card"', 1)[1].split(">", 1)[0]
    assert "hidden" in allclear


def test_the_checklist_disappears_once_the_factory_can_work(client, session, user, channel):
    _campaign(session, user, channel)
    body = client.get("/").text
    assert "Finish setting up your factory" not in body


def test_the_three_steps_are_not_printed_twice_on_one_screen(client):
    """The activity card used to carry its own copy of the same checklist."""
    body = client.get("/").text
    assert body.count("Connect a channel") == 1
    assert body.count("Add your API keys") == 1


def test_setup_counts_a_server_wide_key_as_done(user, channel, monkeypatch):
    """Keys can come from the server's .env instead of the user row — that is a configured box, not
    an unfinished setup."""
    import main
    from core.config import settings

    class NoKeys:
        gemini_api_key = None
        pexels_api_key = None

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "env-gemini", raising=False)
    monkeypatch.setattr(settings, "PEXELS_API_KEY", "env-pexels", raising=False)
    assert main._setup_state(NoKeys(), [channel], ["c"])["done"] is True


# ── Dead ends that used to lead off-site ─────────────────────────────────────
def test_youtube_connect_explains_itself_instead_of_sending_you_to_a_google_error(client, monkeypatch):
    """With no OAuth client configured this redirected to accounts.google.com with client_id=None —
    Google answered "Error 400: invalid_request" and the operator had no idea it was our setup."""
    from core.config import settings

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "", raising=False)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "", raising=False)

    r = client.get("/oauth/google/start", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/channels?flash=no_google_client"

    page = client.get("/channels?flash=no_google_client").text
    assert "isn’t set up on this server yet" in page
    assert "GOOGLE_CLIENT_ID" in page


def test_a_page_facebook_rejects_is_not_saved_as_a_working_channel(client, session, user, monkeypatch):
    """It used to store anything and show "● Active"; the lie surfaced weeks later, when a publish
    failed."""
    from database.models import Channel
    from services import verification

    monkeypatch.setattr(verification, "check_facebook_page",
                        lambda page_id, token: (False, "Invalid OAuth access token."))

    r = client.post("/channels/facebook", data={
        "channel_name": "Fake", "page_id": "1", "page_access_token": "made-up"},
        follow_redirects=False)
    assert r.status_code == 303
    assert "flash=fb_rejected" in r.headers["location"]
    assert session.scalars(select_channels()).first() is None

    page = client.get("/channels?flash=fb_rejected&flash_reason=Invalid+OAuth+access+token.").text
    assert "the Page was not connected" in page
    assert "Invalid OAuth access token." in page          # Facebook's own words, escaped by Jinja
    assert isinstance(Channel.__table__.name, str)        # (import used)


def test_a_network_hiccup_never_blocks_connecting_a_real_page(client, session, monkeypatch):
    """The verification gates a SAVE, so "could not tell" must let the operator through — otherwise a
    flaky minute locks them out of their own Page."""
    from services import verification

    monkeypatch.setattr(verification, "check_facebook_page",
                        lambda page_id, token: (None, "Could not reach Facebook to verify"))

    r = client.post("/channels/facebook", data={
        "channel_name": "Real Page", "page_id": "123", "page_access_token": "tok"},
        follow_redirects=False)
    assert r.headers["location"] == "/channels?flash=fb_added"
    assert session.scalars(select_channels()).first().channel_name == "Real Page"


def test_the_verifier_never_raises_and_never_leaks_the_token(monkeypatch):
    """The Graph URL carries the access token, so raw exception text must never reach the operator."""
    from services import verification

    def boom(*a, **kw):
        raise RuntimeError("connection to graph.facebook.com/v20.0/1?access_token=SECRET failed")

    import requests

    monkeypatch.setattr(requests, "get", boom)
    ok, detail = verification.check_facebook_page("1", "SECRET")
    assert ok is None                       # could not tell — not a rejection
    assert "SECRET" not in detail and "access_token" not in detail


def select_channels():
    from sqlalchemy import select

    from database.models import Channel

    return select(Channel)


# ── Missing keys are named before they cost a render ─────────────────────────
def test_an_active_campaign_with_no_gemini_key_is_a_red_alert(session, user, channel, monkeypatch):
    """A campaign could be started with no keys at all: three episodes queued, every one doomed, and
    the dashboard said "All clear"."""
    import main
    from core.config import settings

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "", raising=False)
    monkeypatch.setattr(settings, "PEXELS_API_KEY", "", raising=False)
    user.gemini_api_key = None
    user.pexels_api_key = None
    session.commit()
    _campaign(session, user, channel)

    rows = {r["key"]: r for r in main._credential_alerts(session, user)}
    assert rows["missing-gemini"]["level"] == "red"
    assert rows["missing-gemini"]["href"] == "/credentials"
    assert rows["missing-pexels"]["level"] == "red"


def test_a_studio_campaign_does_not_ask_for_a_pexels_key(session, user, channel, monkeypatch):
    """Studio and Quote campaigns draw their own visuals — demanding a stock-footage key would be a
    red alert the operator cannot act on and does not need."""
    import main
    from core.config import settings

    monkeypatch.setattr(settings, "PEXELS_API_KEY", "", raising=False)
    user.pexels_api_key = None
    session.commit()
    _campaign(session, user, channel, visual_source="studio")

    assert "missing-pexels" not in {r["key"] for r in main._credential_alerts(session, user)}


def test_nothing_is_demanded_before_a_campaign_is_actually_running(session, user, channel, monkeypatch):
    """Nagging about keys on an empty account is what the setup checklist is for."""
    import main
    from core.config import settings

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "", raising=False)
    user.gemini_api_key = None
    session.commit()

    assert main._credential_alerts(session, user) == []


def test_credentials_page_says_where_to_get_each_free_key(client):
    """The blocker was never "which field" — it was "where do I get one"."""
    body = client.get("/credentials").text
    assert body.count("Get your free key") >= 2
    assert "aistudio.google.com/app/apikey" in body
    assert "pexels.com/api/new" in body
    # Expert tuning starts collapsed so the page opens on the two keys that matter.
    assert "Gemini model chain" in body
    chain = body.split("Gemini model chain", 1)[0]
    assert chain.rstrip().endswith("<summary><span class=\"sum\"><span class=\"sum-l\">🧠")


# ── A failure that tells you what to do about it ─────────────────────────────
def test_a_spent_quota_is_not_reported_as_a_stack_trace():
    import main

    d = main._diagnose_failure("google.api_core.exceptions.ResourceExhausted: 429 quota exceeded")
    assert d["cause"] == "A free-tier quota ran out"
    assert "retry" in d["fix"].lower()


def test_each_diagnosis_ends_somewhere_the_operator_can_act():
    """A cause with no fix is still a dead end."""
    import main

    for words, cause, fix, href, action, transient in main._FAILURE_PATTERNS:
        assert words and cause and fix
        assert bool(href) == bool(action)           # a link needs a label and vice versa
        assert not href or href.startswith("/")     # never off-site
        assert isinstance(transient, bool)          # the autopilot reads this — no maybes


def test_an_unrecognised_error_is_not_given_a_confident_wrong_cause():
    import main

    assert main._diagnose_failure("WeirdError: something nobody predicted") is None
    assert main._diagnose_failure("") is None
    assert main._diagnose_failure(None) is None


def test_the_episode_page_names_the_cause_and_links_to_the_fix(client, session, user, channel):
    from database.models import Task
    from database.types import TaskStatus

    camp = _campaign(session, user, channel)
    t = Task(campaign_id=camp.id, user_id=user.id, episode_number=1, status=TaskStatus.FAILED,
             finished_at=datetime.utcnow(),
             error_message="RuntimeError: 429 rate limit for gemini-2.5-flash")
    session.add(t)
    session.commit()
    session.refresh(t)

    body = client.get(f"/episodes/{t.id}").text
    assert "A free-tier quota ran out" in body
    assert 'href="/credentials"' in body
    # The raw text is still available — folded away, not thrown away.
    assert "What the render reported" in body
    assert "429 rate limit" in body


# ── Review-first for the very first campaign ─────────────────────────────────
def test_the_first_campaign_defaults_to_review_first(client, channel):
    """Auto-publish is the right steady state, but as a FIRST experience it uploads to a real channel
    before the operator has seen a single thing this factory makes."""
    body = client.get("/campaigns/new").text
    review = body.split('name="publish_mode"', 1)[1].split("</select>", 1)[0]
    assert 'value="review" selected' in review.replace("selected>", "selected >")
    assert "Your first campaign starts in Review mode" in body


def test_a_second_campaign_keeps_the_hands_off_default(client, session, user, channel):
    _campaign(session, user, channel)
    body = client.get("/campaigns/new").text
    modes = body.split('name="publish_mode"', 1)[1].split("</select>", 1)[0]
    assert 'value="auto" selected' in modes.replace("selected>", "selected >")
    assert "Your first campaign starts in Review mode" not in body


def test_an_explicit_settings_choice_always_wins(client, session, user, channel):
    """An operator who has already said "auto-publish by default" is not a beginner."""
    user.settings_json = {"publish_mode": "auto"}
    session.commit()
    body = client.get("/campaigns/new").text
    modes = body.split('name="publish_mode"', 1)[1].split("</select>", 1)[0]
    assert 'value="auto" selected' in modes.replace("selected>", "selected >")


# ── Finding things the way they are actually typed ───────────────────────────
def test_search_folds_vietnamese_diacritics_both_ways(client, session, user, channel):
    """Titles here are Vietnamese and are typed without diacritics, because that is how people type
    on a phone. `ilike` matched neither direction."""
    _campaign(session, user, channel, topic="Lịch sử Việt Nam")

    hits = client.get("/api/search?q=lich su").json()["results"]
    assert any(r["label"] == "Lịch sử Việt Nam" for r in hits)
    # And with the diacritics typed in full.
    assert any(r["label"] == "Lịch sử Việt Nam"
               for r in client.get("/api/search?q=Lịch sử").json()["results"])


def test_d_with_a_stroke_folds_too(client, session, user, channel):
    """`đ` has no combining form, so NFD alone leaves it — "dang" would never find "Đăng"."""
    _campaign(session, user, channel, topic="Đặng Văn Kể Chuyện")

    assert client.get("/api/search?q=dang van").json()["results"]


def test_ep_3_finds_episode_3(client, session, user, channel):
    """"ep 3" is how an operator refers to an episode; only a bare "3" used to work."""
    from database.models import Task
    from database.types import TaskStatus

    camp = _campaign(session, user, channel)
    for n in (3, 13):
        session.add(Task(campaign_id=camp.id, user_id=user.id, episode_number=n,
                         status=TaskStatus.COMPLETED))
    session.commit()

    labels = [r["label"] for r in client.get("/api/search?q=ep 3").json()["results"]]
    assert any(label.startswith("Ep 3 ·") for label in labels)
    # Exact number, not a substring match — "ep 3" is not a request for Ep 13.
    assert not any(label.startswith("Ep 13") for label in labels)


def test_the_palette_can_navigate_not_only_find():
    """Content search cannot answer "where do I set my keys" — the destinations are in the client so
    they answer instantly and survive a failed request."""
    js = open("static/ui.js", encoding="utf-8").read()
    assert "CMD_PAGES" in js
    for href in ('href: "/credentials"', 'href: "/operations"', 'href: "/campaigns/new"'):
        assert href in js
    # Same folding rule as the server, so the two halves of one result list agree.
    assert 'replace(/đ/g, "d")' in js


# ── Flashes describe the past, so they must not survive the URL ──────────────
def test_a_flash_is_stripped_from_the_address_bar():
    """Reloading or sharing `?flash=publish` re-showed a success for an action nobody took."""
    js = open("static/ui.js", encoding="utf-8").read()
    flash = js.split("function initFlash()", 1)[1].split("\n  }", 1)[0]
    assert 'searchParams.delete("flash")' in flash
    assert 'searchParams.delete("flash_reason")' in flash
    assert "replaceState" in flash                      # rewrite the URL, never reload the page
    assert "initFlash();" in js                         # and it is actually wired up


# ── Errors keep the operator inside the app ──────────────────────────────────
def test_a_browser_404_is_a_page_with_navigation_not_bare_json(client):
    r = client.get("/episodes/999999", headers={"accept": "text/html"})
    assert r.status_code == 404
    assert "Nothing here" in r.text
    assert "AI Video Factory" in r.text                 # the shell, so there is a way back
    assert 'href="/campaigns"' in r.text


def test_api_callers_still_get_json(client):
    r = client.get("/episodes/999999", headers={"accept": "application/json"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Episode not found"


# ── Irreversible actions say what they will do, to what ─────────────────────
def test_the_confirm_button_is_labelled_with_the_action(client, session, user, channel, tmp_path):
    """"Confirm" is the word the operator clicks — it should be the word for what happens."""
    from database.models import BufferPoolItem
    from database.types import BufferStatus

    camp = _campaign(session, user, channel, posting_slots=["21:00"])
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    item = BufferPoolItem(campaign_id=camp.id, channel_id=channel.id, episode_number=1,
                          status=BufferStatus.ready, video_path=str(f), metadata_json={})
    session.add(item)
    session.commit()

    body = client.get("/assets").text
    form = body.split(f'action="/assets/{item.id}/publish-now"', 1)[1].split(">", 1)[0]
    assert 'data-confirm-verb="Publish now"' in form
    # And it names the episode: a grid of cards makes "this episode" ambiguous.
    assert "Ep 1" in form and camp.topic_name in form


def test_every_confirm_in_the_app_carries_a_verb():
    """One generic "Confirm" for delete-a-campaign and publish-now trained the reflex to click it."""
    import glob
    import re

    missing = []
    for path in glob.glob("templates/*.html"):
        src = open(path, encoding="utf-8").read()
        for form in re.findall(r"<form[^>]*data-confirm=[^>]*>", src, re.S):
            if "data-confirm-verb=" not in form:
                missing.append(path)
    assert not missing, f"confirm dialogs with no verb label: {sorted(set(missing))}"


# ── Thumbs, not cursors ──────────────────────────────────────────────────────
def test_row_actions_are_thumb_sized_on_a_phone():
    """Every row action ("↻ Retry", "⚡ Now", "Approve") is a `.btn.sm`, which sat at 40px."""
    css = open("static/app.css", encoding="utf-8").read()
    phone = css.split("@media (max-width: 720px)", 1)[1]
    assert ".btn, .btn.sm { min-height: 44px; }" in phone
    assert ".icon-btn, .nav-toggle { width: 44px; height: 44px; }" in phone


def test_the_save_bar_stacks_instead_of_clipping_its_primary_button():
    """One non-wrapping row could not fit two submit buttons, a Cancel link and the hint at 375px, so
    flex shrank them all and the most important control on the page read "Create &"."""
    css = open("static/app.css", encoding="utf-8").read()
    phone = css.split("@media (max-width: 720px)", 1)[1]
    bar = phone.split(".savebar {", 1)[1].split("\n  ." + "hint", 1)[0]
    assert "flex-wrap: wrap" in bar
    assert ".savebar > button:first-of-type { flex: 1 0 100%; }" in phone   # primary gets a row
    assert "white-space: nowrap" in phone.split(".savebar .btn {", 1)[1].split("}", 1)[0]
    # Desktop keeps the single row: these rules live inside the phone media query only.
    desktop = css.split("@media (max-width: 720px)", 1)[0]
    assert "flex-wrap" not in desktop.split(".savebar {", 1)[1].split("}", 1)[0]
