"""Credential verification — one cheap live call per provider so a wrong key is caught at save
time, not at 2am when a render fails. Each returns (ok, detail) and never raises."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TIMEOUT = 15


def verify_gemini(api_key: str) -> tuple[bool, str]:
    try:
        import requests

        resp = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": api_key, "pageSize": 1},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            return True, "Gemini key is valid."
        return False, f"Gemini rejected the key (HTTP {resp.status_code})."
    except Exception as exc:  # noqa: BLE001 — the exception text embeds the URL (?key=…); never expose it
        logger.warning("Gemini verification network error: %s", type(exc).__name__)
        return False, "Could not reach Gemini (network error)."


def verify_pexels(api_key: str) -> tuple[bool, str]:
    try:
        import requests

        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": api_key},
            params={"query": "nature", "per_page": 1},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            return True, "Pexels key is valid."
        return False, f"Pexels rejected the key (HTTP {resp.status_code})."
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pexels verification network error: %s", type(exc).__name__)
        return False, "Could not reach Pexels (network error)."


def verify_freesound(api_key: str) -> tuple[bool, str]:
    """One tiny search — proves the key works AND that CC0 results come back (auto music path)."""
    try:
        import requests

        resp = requests.get(
            "https://freesound.org/apiv2/search/text/",
            params={"query": "ambient", "filter": 'license:"Creative Commons 0"',
                    "page_size": 1, "fields": "id", "token": api_key},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            return True, "Freesound key is valid — Auto background music will work."
        return False, f"Freesound rejected the key (HTTP {resp.status_code})."
    except Exception as exc:  # noqa: BLE001 — the exception text embeds the URL (?token=…); never expose it
        logger.warning("Freesound verification network error: %s", type(exc).__name__)
        return False, "Could not reach Freesound (network error)."


def verify_pollinations(token: str | None = None) -> tuple[bool, str]:
    """Draw one tiny image — proves Pollinations is reachable (and, if a token is set, that it's
    accepted). The keyless anonymous tier is valid too, so no token is not a failure."""
    try:
        import requests

        params: dict = {"model": "flux", "width": 256, "height": 256, "safe": "true"}
        # Backend auth is the Authorization: Bearer header (a `token` query param is not documented
        # and gets ignored — the request would silently test the anonymous tier instead of the key).
        # gen.pollinations.ai/image is the current API endpoint (same one the render path calls).
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = requests.get("https://gen.pollinations.ai/image/a%20tiny%20test%20icon",
                            params=params, headers=headers, timeout=90)
        ctype = resp.headers.get("content-type", "")
        if resp.status_code == 200 and ctype.startswith("image/") and resp.content:
            tier = "token accepted" if token else "keyless anonymous tier"
            return True, f"Pollinations is reachable ({tier}) — Studio Mode can draw for free."
        return False, f"Pollinations did not return an image (HTTP {resp.status_code}, {ctype!r})."
    except Exception as exc:  # noqa: BLE001 — the exception text can embed the token; never expose it
        logger.warning("Pollinations verification network error: %s", type(exc).__name__)
        return False, "Could not reach Pollinations (network error)."


def verify_telegram(token: str, chat_id: str | None = None) -> tuple[bool, str]:
    try:
        import requests

        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=TIMEOUT)
        if resp.status_code != 200 or not resp.json().get("ok"):
            return False, "Telegram rejected the bot token."
        bot = resp.json()["result"].get("username", "bot")
        if chat_id:
            msg = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": "✅ AI Video Factory: Telegram alerts are working."},
                timeout=TIMEOUT,
            )
            if msg.status_code != 200:
                return False, f"Token OK (@{bot}) but sending to chat {chat_id} failed — check the chat ID."
            return True, f"Token OK (@{bot}) — test message sent."
        return True, f"Token OK (@{bot}). Add a chat ID to test delivery."
    except Exception as exc:  # noqa: BLE001 — the exception text embeds the URL (/bot<token>/); never expose it
        logger.warning("Telegram verification network error: %s", type(exc).__name__)
        return False, "Could not reach Telegram (network error)."


_PAGE_URL = re.compile(
    r"^(?:https?://)?(?:www\.|m\.|web\.|business\.)?facebook\.com/(?:pg/)?(?P<slug>[^/?#]+)", re.I)
_PROFILE_URL_ID = re.compile(r"profile\.php\?id=(?P<id>\d+)", re.I)


def normalize_page_id(raw: str) -> str:
    """Accept whatever an operator pastes and return the bare Page identifier (ADR-072).

    People paste `https://www.facebook.com/MyPage`, `facebook.com/profile.php?id=123`, `@MyPage`, or
    the plain id. Everything but the plain id used to be stored verbatim: a full URL made every Graph
    call 404 (and the save went through anyway because "could not tell" passes), while a username
    happened to work until Facebook renamed it. The canonical NUMERIC id is resolved separately by
    `check_facebook_page` — this is only the "make it a lookup key at all" step."""
    s = (raw or "").strip()
    if not s:
        return ""
    m = _PROFILE_URL_ID.search(s)
    if m:
        return m.group("id")
    m = _PAGE_URL.match(s)
    if m:
        s = m.group("slug")
    return s.lstrip("@").strip("/")


@dataclass(frozen=True)
class PageCheck:
    """The verdict AND the Page's real identity, so the caller can store the canonical id and offer
    the official name/avatar instead of asking the operator to retype them."""

    ok: bool | None      # True verified · False definitely rejected · None could not tell
    detail: str
    page_id: str | None = None
    name: str | None = None
    picture: str | None = None


# Graph's own words when a token points at something that is not a Page and we ask for a Page-only
# field: "(#100) Tried accessing nonexisting field (category) on node type (User)". The node type in
# those brackets is the most direct answer available to "what did I actually paste?".
_NONEXISTENT_FIELD = "nonexisting field"
_NODE_TYPE = re.compile(r"on node type \(([A-Za-z]+)\)")
# metadata.type / the bracketed node type → what to tell the operator they pasted.
_NODE_ADVICE = {
    "user": ("That is a personal User token, not a Page Access Token.",
             "In the Graph API Explorer, switch the token dropdown to your Page before generating it."),
    "application": ("That is an App token, not a Page Access Token.",
                    "An app token cannot post as a Page. Generate a Page Access Token for the Page "
                    "itself — see the steps below."),
}


def _graph_message(resp) -> str:
    """The `error.message` Graph put in the body, or "" — never raises on a non-JSON body."""
    try:
        return ((resp.json() or {}).get("error") or {}).get("message") or ""
    except ValueError:
        return ""


def _graph_code(resp) -> int | None:
    """The numeric `error.code`, or None — the ONLY field that reliably classifies a Graph error."""
    try:
        return ((resp.json() or {}).get("error") or {}).get("code")
    except ValueError:
        return None


def _fb():
    from services import facebook_service

    return facebook_service


def _wrong_node_kind(message: str) -> str | None:
    """`""`/node name if this error means "the token does not point at a Page"; None otherwise.

    Returns a string (possibly empty, when Graph did not name the node) so the caller can tell
    "definitely not a Page, kind unknown" apart from "not this kind of error at all"."""
    if _NONEXISTENT_FIELD not in (message or "").lower():
        return None
    m = _NODE_TYPE.search(message)
    return m.group(1) if m else ""


def _not_a_page(node: str, name: str = "") -> PageCheck:
    """One refusal message for "this token is not for a Page", whichever way we found out."""
    head, how = _NODE_ADVICE.get(node.lower(), (
        f"That token belongs to a {node or 'different kind of object'}, not to a Facebook Page.",
        "Generate a permanent Page Access Token for the Page itself — see the steps below."))
    who = f" It belongs to “{name}”." if name else ""
    return PageCheck(False, f"{head}{who} {how}")


def check_facebook_page(page_id: str, token: str) -> PageCheck:
    """Is this really a Page Access Token for this Page? THREE-state on purpose (ADR-068/072):
    `True` verified · `False` definitely rejected · `None` could not tell.

    The other checks here answer a Test button, where "couldn't reach it" may as well be a failure.
    This one gates a save, so the distinction matters: a made-up token must be refused, but a network
    hiccup must never stop a real operator from connecting their Page. Never raises.

    It asks `/me` rather than `/{page_id}`, because that is the only question worth asking. Reading a
    Page's public name proves nothing — a short-lived USER token (what people copy out of the Graph
    Explorer, and the single most common mistake) reads it happily, so the channel saved as verified
    and died hours later at publish time. With a PAGE token `/me` IS the Page.

    HOW we tell a Page from a person changed (ADR-077). The original idea — ask for `category`, which
    only a Page has — was right about Graph's data model and wrong about its API: Graph does not
    answer a User node with `category: null`, it REFUSES THE WHOLE REQUEST with "(#100) Tried
    accessing nonexisting field (category) on node type (User)". So the carefully-worded "that is a
    personal User token" branch was unreachable, and an operator who made exactly the mistake this
    function exists to catch got Graph's raw complaint about a field they never asked for. Now the
    request only names fields every node type has and adds `metadata=1`, Graph's own introspection,
    which reports the node type as data instead of as an error.
    """
    from services.facebook_service import GRAPH, scrub

    wanted = normalize_page_id(page_id)
    token = (token or "").strip()
    # Catch the mistake locally, before spending a call, because Graph's answer for it is useless:
    # pasting the Page ID into the token box comes back as "Cannot parse access token", which tells
    # the operator nothing about WHICH box is wrong (ADR-074). A real Page token is a long opaque
    # string (they start "EAA…"); a short all-digit value is an id, not a credential.
    if token and token == wanted:
        return PageCheck(False, "You pasted the Page ID into the token box — the two fields need "
                                "different values. The token is the long secret string that starts "
                                "with “EAA”; see “How do I get a permanent Page Access Token?” below.")
    if token.isdigit():
        return PageCheck(False, "That looks like an ID, not an access token. A Page Access Token is "
                                "a long string of letters and numbers starting with “EAA”.")
    try:
        import requests

        # Only fields that exist on EVERY node type, so the request itself can never be the thing
        # that fails; `metadata=1` is what actually identifies what the token points at.
        resp = requests.get(
            f"{GRAPH}/me",
            params={"fields": "id,name,picture.type(large)", "metadata": 1, "access_token": token},
            timeout=TIMEOUT)
        if resp.status_code == 200:
            data = resp.json() or {}
            got_id, name = str(data.get("id") or ""), data.get("name") or ""
            node = str((data.get("metadata") or {}).get("type") or "").lower()
            if node and node != "page":
                return _not_a_page(node, name)
            if not node:
                # Introspection did not answer. Rather than trust an unidentified token — the whole
                # point of this function — ask the one Page-only question directly, where a #100 IS
                # the answer instead of an accident.
                probe = requests.get(f"{GRAPH}/me", params={"fields": "category",
                                                            "access_token": token}, timeout=TIMEOUT)
                if probe.status_code != 200:
                    kind = _wrong_node_kind(_graph_message(probe))
                    if kind is not None:
                        return _not_a_page(kind, name)
                    return PageCheck(None, "Could not confirm this is a Page token — saved without "
                                           "checking.")
            # A numeric id the operator typed must match; a username/URL is resolved by Graph instead.
            if wanted.isdigit() and got_id and wanted != got_id:
                return PageCheck(False, f"This token belongs to the Page “{name}” (id {got_id}), "
                                        f"not to the Page id you entered ({wanted}).")
            pic = (((data.get("picture") or {}).get("data") or {}).get("url")) or None
            return PageCheck(True, f"Verified: {name or got_id or wanted}.",
                             page_id=got_id or wanted, name=name or None, picture=pic)
        # Graph answers a bad token with 400/401/403 and an explanation of its own.
        if resp.status_code in (400, 401, 403, 404):
            detail = _graph_message(resp)
            # Rate limits and temporary API errors arrive with the SAME 4xx statuses (and Graph
            # stamps them "OAuthException" too). They mean "not now", never "not this token" — a
            # definite rejection here retires channels and blocks saves, so it is reserved for
            # errors that are actually about the credential (ADR-083).
            code = _graph_code(resp)
            if code in _fb()._TRANSIENT_ERROR_CODES:
                return PageCheck(None, "Facebook is rate-limiting or temporarily unavailable — "
                                       "could not verify right now. The token was NOT rejected.")
            # A field-does-not-exist complaint is never news the operator can use — it is about OUR
            # request, not their token. Translate it into what it actually means (ADR-077).
            kind = _wrong_node_kind(detail)
            if kind is not None:
                return _not_a_page(kind)
            return PageCheck(False, scrub(detail, token)
                             or f"Facebook rejected these details (HTTP {resp.status_code}).")
        return PageCheck(None, f"Facebook answered HTTP {resp.status_code} — could not verify now.")
    except Exception as exc:  # noqa: BLE001 — the URL carries the token; never surface the raw text
        logger.warning("Facebook page verification network error: %s", type(exc).__name__)
        return PageCheck(None, "Could not reach Facebook to verify — saved without checking.")
