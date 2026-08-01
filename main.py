"""FastAPI web app — dashboard, channel/campaign/credential management, and the task-log API.

Routes are tenant-scoped through the `CurrentUser` dependency (solo mode injects the built-in admin;
multi-tenant verifies a Firebase token). Server-rendered Jinja templates + a small polling script
(static/app.js) drive the real-time task log — no runtime CDN (CSP-friendly).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import String, cast, false, func, or_, select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from auth import firebase
from auth.dependencies import (
    CurrentUser,
    DbDep,
    get_or_create_user,
    get_owned_buffer_item,
    get_owned_campaign,
    get_owned_channel,
)
from core import autopilot, failure, retention, timezones
from core.config import settings
from core.tts import QUOTE_VOICES, VOICE_CHOICES
from core.video_factory import COLOR_GRADE_CHOICES
from database.db_session import get_db, init_db
from database.models import AutopilotAction, BufferPoolItem, Campaign, Channel, Task
from database.types import BufferStatus, CampaignStatus, ChannelStatus, Platform, TaskStatus
from services import analytics_service
from workers import task_queue, video_worker

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # ensure schema exists on boot
    yield


class CachedStaticFiles(StaticFiles):
    """Serve /static with a long, immutable cache for content-hashed (?v=) URLs. static_url() appends
    a per-file hash that changes whenever the file changes, so the browser may cache forever and skip
    revalidation entirely; un-versioned requests keep the default (validated) behaviour."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if b"v=" in scope.get("query_string", b"") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app = FastAPI(title="AI Video Factory", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    max_age=settings.SESSION_MAX_AGE_DAYS * 86400,
    same_site="lax",
    https_only=True,  # ingress is always HTTPS via the Cloudflare Tunnel — mark the cookie Secure
)
app.mount("/static", CachedStaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["settings"] = settings  # e.g. MULTI_TENANT_MODE toggles the sign-out chip
templates.env.globals["voice_choices"] = VOICE_CHOICES  # campaign form: per-language voice picker
# The soft-delivery voices per language (ADR-071) — the form marks them 🌙 and the Quote style
# auto-picks one, so "which voices suit a whispered read" has one definition, in core/tts.py.
templates.env.globals["quote_voices"] = QUOTE_VOICES
templates.env.globals["grade_choices"] = COLOR_GRADE_CHOICES  # one list: dropdown + whitelist
templates.env.globals["voice_names"] = {  # id → short friendly name for compact chips
    v: label.split(" — ")[0] for vs in VOICE_CHOICES.values() for v, label in vs}
templates.env.globals["tz_choices"] = timezones.tz_choices  # grouped timezone picker (offsets live)


def _ago(value) -> str:
    """Compact server-rendered relative time ('just now' / '5m ago' / '3h ago' / '2d ago') from an
    ISO string or datetime. 'never' when empty — used for the autopilot 'last ran' heartbeat."""
    from datetime import datetime as _dt
    if not value:
        return "never"
    try:
        dt = _dt.fromisoformat(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return ""
    secs = max(0.0, (_dt.utcnow() - dt).total_seconds())
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{int(round(secs / 60))}m ago"
    if secs < 129600:
        return f"{int(round(secs / 3600))}h ago"
    return f"{int(round(secs / 86400))}d ago"


templates.env.globals["ago"] = _ago  # relative "last ran" time for the autopilot heartbeat


def _query_string(**params: object) -> str:
    """Build a URL query string from kwargs, dropping empty/None values and URL-encoding — so the
    shared filter-bar macro can compose chip/search links (status + search + scope) safely."""
    from urllib.parse import urlencode

    return urlencode({k: v for k, v in params.items() if v not in (None, "", [])})


templates.env.globals["query_string"] = _query_string


def _nav_channels(request: Request) -> list[dict]:
    """The current user's channels, for the topbar scope switcher. Best-effort: any failure (no
    session, DB hiccup) returns [] so `base.html` always renders. Reuses the auth user-resolution
    so solo and multi-tenant modes behave identically."""
    try:
        from auth.dependencies import SOLO_UID, get_or_create_user
        from database.db_session import SessionLocal

        with SessionLocal() as db:
            if not settings.MULTI_TENANT_MODE:
                user = get_or_create_user(db, firebase_uid=SOLO_UID, is_admin=True)
            else:
                uid = request.session.get("uid") if "session" in request.scope else None
                if not uid:
                    return []
                user = get_or_create_user(db, firebase_uid=uid)
            return [{"id": c.id, "name": c.channel_name}
                    for c in db.scalars(select(Channel).where(Channel.user_id == user.id))]
    except Exception:  # noqa: BLE001 — the switcher is a convenience; never break page render
        return []


templates.env.globals["nav_channels"] = _nav_channels


_asset_versions: dict[str, str] = {}


def static_url(filename: str) -> str:
    """Cache-busted URL for a bundled static file: /static/<name>?v=<content-hash>. The hash is
    computed once per process (deploys restart the app), so a new build always invalidates the
    browser cache — no more stale CSS/JS after an update."""
    import hashlib

    v = _asset_versions.get(filename)
    if v is None:
        try:
            with open(os.path.join("static", filename), "rb") as fh:
                v = hashlib.sha1(fh.read()).hexdigest()[:8]
        except OSError:
            v = "dev"
        _asset_versions[filename] = v
    return f"/static/{filename}?v={v}"


templates.env.globals["static_url"] = static_url

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    # Read-only performance data (retention/views) for the self-improvement loop. Channels
    # connected before this scope existed need a one-click reconnect to grant it.
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
LOGIN_SCOPES = [  # Google SSO login (identity only — no YouTube access)
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


_ERROR_COPY = {
    404: ("Nothing here", "That page or episode doesn’t exist — it may have been deleted, or the "
                          "link may be from an older version of the app."),
    403: ("Not yours", "That belongs to another account."),
    400: ("That request didn’t work", "Something in the request was wrong — go back and try again."),
    500: ("Something broke on our side", "The error was logged. Try again; if it keeps happening, "
                                         "check the worker and the server log."),
}


@app.exception_handler(StarletteHTTPException)
async def _auth_aware_http_exception(request: Request, exc: StarletteHTTPException):
    """Browsers navigating unauthenticated get sent to /login; API callers keep the raw JSON.

    A browser that asked for HTML also gets an HTML error page with the navigation intact (ADR-068) —
    a bare `{"detail":"Not found"}` gave a dead end with no way back and read as "the app is broken"."""
    wants_html = "text/html" in (request.headers.get("accept") or "")
    if exc.status_code == 401 and settings.MULTI_TENANT_MODE and wants_html:
        return RedirectResponse("/login", status_code=303)
    if wants_html and exc.status_code >= 400:
        title, body = _ERROR_COPY.get(
            exc.status_code, ("Something went wrong", "That request could not be completed."))
        try:
            return templates.TemplateResponse(
                request, "error.html",
                {"request": request, "code": exc.status_code, "title": title, "body": body,
                 "detail": str(exc.detail or ""), "nav": ""},
                status_code=exc.status_code)
        except Exception:  # noqa: BLE001 — an error page that errors must still answer the request
            logger.warning("Error page render failed for %s", exc.status_code, exc_info=True)
    return await http_exception_handler(request, exc)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ── Login & sessions (multi-tenant mode) ─────────────────────────────────────
class SessionPayload(BaseModel):
    id_token: str


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not settings.MULTI_TENANT_MODE or request.session.get("uid"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "web_api_key": settings.FIREBASE_WEB_API_KEY,
            "google_enabled": bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET),
        },
    )


@app.post("/auth/session")
def create_session(payload: SessionPayload, request: Request, db: DbDep):
    """Verify a Firebase ID token (obtained by the /login page) and mint the browser session."""
    if not settings.MULTI_TENANT_MODE:
        return {"ok": True, "mode": "solo"}
    try:
        decoded = firebase.verify_id_token(payload.id_token)
    except Exception as exc:  # noqa: BLE001 — any verification failure is a 401
        raise HTTPException(401, "Invalid Firebase token") from exc
    get_or_create_user(db, firebase_uid=decoded["uid"])  # JIT-provision on first login
    request.session["uid"] = decoded["uid"]
    request.session["email"] = decoded.get("email")
    return {"ok": True}


@app.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login" if settings.MULTI_TENANT_MODE else "/", status_code=303)


@app.get("/auth/google/login")
def google_login_start(request: Request):
    """CDN-free "Continue with Google": server-side OAuth for identity only (see ADR-009)."""
    if not settings.MULTI_TENANT_MODE:
        return RedirectResponse("/", status_code=303)
    if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET):
        raise HTTPException(400, "Google sign-in is not configured")
    flow = _google_flow(LOGIN_SCOPES, "/auth/google/callback")
    auth_url, state = flow.authorization_url(prompt="select_account")
    request.session["login_state"] = state
    return RedirectResponse(auth_url)


@app.get("/auth/google/callback")
def google_login_callback(request: Request, db: DbDep):
    incoming = request.query_params.get("state")
    stored = request.session.pop("login_state", None)
    # Require BOTH present and equal — `None != None` is False, so a missing state on both sides
    # would otherwise slip through and defeat the CSRF protection.
    if not incoming or not stored or incoming != stored:
        raise HTTPException(400, "OAuth state mismatch")
    flow = _google_flow(LOGIN_SCOPES, "/auth/google/callback")
    flow.fetch_token(code=request.query_params.get("code"))
    # Exchange the Google id_token for a Firebase sign-in, then verify it like any login.
    data = firebase.sign_in_with_google_id_token(flow.credentials.id_token)
    decoded = firebase.verify_id_token(data["idToken"])
    get_or_create_user(db, firebase_uid=decoded["uid"])
    request.session["uid"] = decoded["uid"]
    request.session["email"] = decoded.get("email") or data.get("email")
    return RedirectResponse("/", status_code=303)


# ── Dashboard ────────────────────────────────────────────────────────────────
_WORKING_STATUSES = [
    TaskStatus.PENDING_QUEUE, TaskStatus.AI_GENERATION, TaskStatus.AUDIO_SYNCED,
    TaskStatus.RENDERING, TaskStatus.SCHEDULED, TaskStatus.PUBLISHING,
]


def _system_health(db, user=None) -> dict:
    """Live infrastructure signals for the dashboard health strip. Never raises — a dead Redis
    should show as red, not take the page down. The AI daily budget is the user's Settings value
    when set, else the app-wide GEMINI_DAILY_BUDGET fallback."""
    budget = (user.settings_json or {}).get("ai_daily_budget") if user is not None else None
    health = {"redis": False, "worker": False, "worker_stalled": False, "queue_depth": None,
              "buffer_ready": 0, "disk_pct": None, "ai_calls": 0,
              "ai_budget": budget or settings.GEMINI_DAILY_BUDGET}
    try:
        health["redis"] = bool(task_queue.conn.ping())
        health["worker"] = task_queue.worker_alive()
        # A worker that hung mid-render still registers, so liveness alone read as healthy for hours
        # (ADR-057). Surface "registered but wedged" as its own signal.
        health["worker_stalled"] = task_queue.stalled_render() is not None
        health["queue_depth"] = len(task_queue.render_queue)
    except Exception:  # noqa: BLE001
        pass
    from core.usage import ai_calls_today

    health["ai_calls"] = ai_calls_today()  # quota meter (Pacific day, matches Google's reset)
    try:
        health["buffer_ready"] = db.scalar(
            select(func.count()).select_from(BufferPoolItem).where(
                BufferPoolItem.status.in_([BufferStatus.ready, BufferStatus.awaiting_review])
            )
        ) or 0
    except Exception:  # noqa: BLE001
        pass
    try:
        path = settings.MEDIA_ROOT if os.path.exists(settings.MEDIA_ROOT) else "/"
        usage = shutil.disk_usage(path)
        health["disk_pct"] = round(usage.used / usage.total * 100)
    except OSError:
        pass
    return health


def _task_counts(db, user_id: int) -> dict:
    rows = db.execute(
        select(Task.status, func.count()).where(Task.user_id == user_id).group_by(Task.status)
    ).all()
    by_status = {status: count for status, count in rows}
    # "Awaiting review" is the buffer-pool review queue (what the Review page + triage inbox act on),
    # NOT the task-status count — the two can diverge, so there is ONE source of truth here.
    awaiting = db.scalar(
        select(func.count()).select_from(BufferPoolItem)
        .join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        .where(Campaign.user_id == user_id, BufferPoolItem.status == BufferStatus.awaiting_review)
    ) or 0
    return {
        "published": by_status.get(TaskStatus.COMPLETED, 0),
        "working": sum(by_status.get(s, 0) for s in _WORKING_STATUSES),
        "awaiting_review": awaiting,
        # CANCELLED is excluded on purpose — an operator's own decision is not a failure (ADR-064).
        "failed": by_status.get(TaskStatus.FAILED, 0),
        "cancelled": by_status.get(TaskStatus.CANCELLED, 0),
    }


def _attention_count(db, user_id: int, counts: dict | None = None) -> int:
    """THE number of things asking for a human, used by every badge in the app (ADR-064).

    One rule, computed once: failed episodes + episodes awaiting review + open autopilot proposals.
    Before this, the hamburger counted failed+review, the sidebar something else, the bell its own
    (grouped) row count and the triage card its own capped list — four numbers for one question,
    visible simultaneously, which taught the operator to trust none of them."""
    counts = counts if counts is not None else _task_counts(db, user_id)
    return (counts["failed"] + counts["awaiting_review"]
            + _autopilot_proposed_count(db, user_id))


def _setup_state(user, channels, campaigns) -> dict:
    """How far through first-run setup this account is (ADR-068).

    The dashboard used to open on "All clear — nothing needs you right now" for an account with no
    channel, no keys and no campaign: literally true (no work is failing) and completely wrong as the
    first thing a new operator reads. The three steps that make the factory able to work are hoisted
    to the top of the page until they are done, and "All clear" waits its turn."""
    # `api_keys`, not `keys`: in Jinja `setup.keys` resolves to the dict's own `.keys` method, which
    # is truthy — the checklist showed step 2 as done on an account with no keys at all.
    have_keys = bool((user.gemini_api_key or settings.GEMINI_API_KEY)
                     and (user.pexels_api_key or settings.PEXELS_API_KEY))
    state = {"channels": bool(channels), "api_keys": have_keys, "campaigns": bool(campaigns)}
    state["done"] = all(state.values())
    # The step to point at: the first one not done.
    state["next"] = next((k for k in ("channels", "api_keys", "campaigns") if not state[k]), "")
    return state


def _autopilot_proposed_count(db, user_id: int) -> int:
    """Open autopilot proposals awaiting a decision — feeds the Autopilot nav badge + triage inbox."""
    return db.scalar(select(func.count()).select_from(AutopilotAction).where(
        AutopilotAction.user_id == user_id, AutopilotAction.status == "proposed")) or 0


def _buffer_counts(db, user_id: int) -> dict:
    """Per-campaign buffer tallies for the rollup links: {campaign_id: {ready, awaiting_review}}."""
    rows = db.execute(
        select(BufferPoolItem.campaign_id, BufferPoolItem.status, func.count())
        .join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        .where(Campaign.user_id == user_id)
        .group_by(BufferPoolItem.campaign_id, BufferPoolItem.status)
    ).all()
    out: dict = {}
    for cid, status, n in rows:
        d = out.setdefault(cid, {"ready": 0, "awaiting_review": 0})
        if status == BufferStatus.ready:
            d["ready"] += n
        elif status == BufferStatus.awaiting_review:
            d["awaiting_review"] += n
    return out


def _campaigns_with_empty_buffer(db, user_id: int) -> int:
    """Active auto-publish campaigns with posting slots and NOTHING rendered — each one is a slot
    that will be missed. The dashboard leads with this instead of an average that averages it away."""
    ready_by_camp = dict(db.execute(
        select(BufferPoolItem.campaign_id, func.count())
        .join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        .where(Campaign.user_id == user_id, BufferPoolItem.status == BufferStatus.ready)
        .group_by(BufferPoolItem.campaign_id)).all())
    n = 0
    for c in db.scalars(select(Campaign).where(
            Campaign.user_id == user_id, Campaign.status == CampaignStatus.active)).all():
        cfg = c.config_json or {}
        if cfg.get("auto_publish", True) and (cfg.get("posting_slots") or []) \
                and not ready_by_camp.get(c.id):
            n += 1
    return n


def _activity_feed(tasks, camp_by_id, chan_by_id) -> list[dict]:
    """Collapse runs of identical consecutive events into one row (ADR-066).

    A burst of published episodes produced ten near-identical lines — "Published — <same campaign> ·
    Ep 51x · 50m ago" over and over — which is where the dashboard's last two phone screens went. One
    row per run ("6 episodes published") says the same thing and leaves room for what changed."""
    feed: list[dict] = []
    for task in tasks:
        campaign = camp_by_id.get(task.campaign_id)
        last = feed[-1] if feed else None
        if (last is not None and last["status"] == task.status.value
                and last["campaign_id"] == task.campaign_id):
            last["episodes"].append(task.episode_number)
            last["count"] += 1
            continue
        feed.append({
            "status": task.status.value, "campaign_id": task.campaign_id,
            "topic": campaign.topic_name if campaign else f"C{task.campaign_id}",
            "channel": (chan_by_id.get(campaign.channel_id).channel_name
                        if campaign and campaign.channel_id in chan_by_id else None),
            "episodes": [task.episode_number], "count": 1,
            "at": task.updated_at, "task_id": task.id,
            "published_url": task.published_url,
        })
    return feed


def _scorecard(db, user_id: int) -> dict:
    """Trajectory signals for the dashboard: 7-day publish throughput, buffer runway, and
    week-over-week retention. Read-only; answers 'is the factory winning?'."""
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    today = now.date()
    recent = db.scalars(
        select(Task).where(Task.user_id == user_id, Task.status == TaskStatus.COMPLETED,
                           Task.updated_at >= now - timedelta(days=14))
    ).all()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    per_day = {d: 0 for d in days}
    ret_this, ret_prev = [], []
    for t in recent:
        when = t.finished_at or t.updated_at
        if not when:
            continue
        if when.date() in per_day:
            per_day[when.date()] += 1
        r = (t.stats_json or {}).get("avg_pct_viewed") if t.stats_json else None
        if r is not None:
            (ret_this if (now - when).days < 7 else ret_prev).append(r)
    thr = [per_day[d] for d in days]
    active = db.scalars(select(Campaign).where(
        Campaign.user_id == user_id, Campaign.status == CampaignStatus.active)).all()
    demand = sum(len((c.config_json or {}).get("posting_slots") or []) or 1 for c in active)
    ready = db.scalar(
        select(func.count()).select_from(BufferPoolItem)
        .join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        .where(Campaign.user_id == user_id, BufferPoolItem.status == BufferStatus.ready)) or 0
    return {
        "throughput": thr, "throughput_days": [d.strftime("%a") for d in days],
        "throughput_max": max(thr) if thr else 0, "published_7d": sum(thr),
        "ready": ready, "runway_days": round(ready / demand, 1) if demand else None,
        # The AVERAGE hid emergencies: "≈1.0 day of runway" read as fine while two campaigns had an
        # empty buffer and were about to miss tonight's slots. Report the worst case too (ADR-066).
        "empty_campaigns": _campaigns_with_empty_buffer(db, user_id),
        "retention_this": round(sum(ret_this) / len(ret_this), 1) if ret_this else None,
        "retention_prev": round(sum(ret_prev) / len(ret_prev), 1) if ret_prev else None,
    }


def _campaign_tz_name(campaign) -> str:
    """The clock a campaign's operator thinks in — its configured zone, else the server default.
    One definition, so posting slots, the calendar and a publish-time override all agree."""
    if campaign is None:
        return settings.TIMEZONE
    return (campaign.config_json or {}).get("timezone") or settings.TIMEZONE


def _campaign_tz(campaign):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(_campaign_tz_name(campaign))
    except Exception:  # noqa: BLE001 — a bad stored zone must never break a page or an action
        return ZoneInfo("UTC")


def _to_campaign_tz(naive_utc, campaign):
    """A stored naive-UTC timestamp as an aware datetime on the campaign's own clock."""
    from zoneinfo import ZoneInfo

    return naive_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(_campaign_tz(campaign))


def _upcoming_slots(campaign, count: int = 1, now=None) -> list:
    """The next `count` posting-slot datetimes for an auto-publish campaign, in its own timezone
    (weekday gate applied). Empty for continuous / review-first / slot-less campaigns.

    ONE definition of "when will this campaign post next", so the dashboard's next-slot chip and the
    Operations publish queue's per-episode projection can never disagree."""
    from datetime import timedelta

    from workers.scheduler import WEEKDAY_KEYS, local_now

    cfg = campaign.config_json or {}
    if not cfg.get("auto_publish", True):
        return []
    slots = sorted(cfg.get("posting_slots") or [])
    if not slots:
        return []
    allowed = cfg.get("posting_days") or []
    now_local = now or local_now(cfg.get("timezone"))
    out = []
    for dd in range(0, 60):  # a horizon long enough for any weekday-gated pattern
        day = now_local + timedelta(days=dd)
        if allowed and WEEKDAY_KEYS[day.weekday()] not in allowed:
            continue
        for s in slots:
            try:
                hh, mm = (int(x) for x in s.split(":"))
            except ValueError:
                continue
            cand = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand > now_local:
                out.append(cand)
                if len(out) >= count:
                    return out
    return out


def _next_slot(campaign) -> dict | None:
    """The soonest upcoming posting slot for ONE active auto-publish campaign, in its own timezone.
    Returns {in_hours, slot, when} or None (continuous / review / no slots / no upcoming day)."""
    from workers.scheduler import local_now

    upcoming = _upcoming_slots(campaign, 1)
    if not upcoming:
        return None
    cand = upcoming[0]
    now_local = local_now((campaign.config_json or {}).get("timezone"))
    return {"in_hours": round((cand - now_local).total_seconds() / 3600, 1),
            "slot": cand.strftime("%H:%M"), "when": cand.strftime("%a %H:%M")}


def _next_publish(db, user_id: int):
    """Soonest upcoming posting slot across active campaigns (each in its own tz) — the earliest of
    the per-campaign `_next_slot`s, tagged with the campaign name."""
    active = db.scalars(select(Campaign).where(
        Campaign.user_id == user_id, Campaign.status == CampaignStatus.active)).all()
    best = None
    for c in active:
        ns = _next_slot(c)
        if ns is not None and (best is None or ns["in_hours"] < best["in_hours"]):
            best = {**ns, "campaign": c.topic_name}
    return best


# Task statuses that mean "an episode is actively rendering right now" (one machine-wide at a time).
_RENDERING_STATUSES = (TaskStatus.AI_GENERATION, TaskStatus.AUDIO_SYNCED, TaskStatus.RENDERING)


def _campaign_ops(db, user_id: int, campaigns) -> dict:
    """Per-campaign operational snapshot — the "what's it doing now / next" facts the cards, the hub
    strip and the dashboard "Running now" panel all render: the in-flight render task (with its
    progress), queued count, ready-buffer + awaiting-review tallies, and the next posting slot."""
    ops = {c.id: {"rendering": None, "queued": 0, "ready": 0, "awaiting_review": 0, "next_slot": None}
           for c in campaigns}
    ids = list(ops)
    if not ids:
        return ops
    for t in db.scalars(select(Task).where(
            Task.user_id == user_id, Task.campaign_id.in_(ids),
            Task.status.in_(_RENDERING_STATUSES)).order_by(Task.id.desc())):
        if ops[t.campaign_id]["rendering"] is None:  # newest in-flight render wins
            ops[t.campaign_id]["rendering"] = t
    for cid, n in db.execute(select(Task.campaign_id, func.count()).where(
            Task.user_id == user_id, Task.campaign_id.in_(ids),
            Task.status == TaskStatus.PENDING_QUEUE).group_by(Task.campaign_id)).all():
        ops[cid]["queued"] = n
    for cid, st, n in db.execute(select(
            BufferPoolItem.campaign_id, BufferPoolItem.status, func.count())
            .where(BufferPoolItem.campaign_id.in_(ids))
            .group_by(BufferPoolItem.campaign_id, BufferPoolItem.status)).all():
        if st == BufferStatus.ready:
            ops[cid]["ready"] = n
        elif st == BufferStatus.awaiting_review:
            ops[cid]["awaiting_review"] = n
    for c in campaigns:
        if c.status == CampaignStatus.active:
            ops[c.id]["next_slot"] = _next_slot(c)
    return ops


def _server_day_start_utc(now=None):
    """Midnight of "today" on the server's configured clock, as naive UTC (what the DB stores).
    Reuses `_campaign_tz(None)`, so the dashboard's day and a campaign's day agree on the zone."""
    from zoneinfo import ZoneInfo

    now_local = (now or datetime.utcnow()).replace(tzinfo=ZoneInfo("UTC")).astimezone(_campaign_tz(None))
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _factory_vitals(db, user_id: int, now=None) -> dict:
    """Factory-wide operating numbers (ADR-062) — the macro view the per-campaign pages cannot give:
    how much reach the whole factory has produced, whether today's renders are actually succeeding,
    how much machine time they cost, and whether the box itself is saturated.

    Views are reported WITH the number of measured episodes, because YouTube Analytics lags ~2 days:
    a bare total would silently read as "the whole catalogue" when it only covers what has data.
    """
    from core import host

    day_start = _server_day_start_utc(now)
    views, measured = 0, 0
    for (stats,) in db.execute(
            select(Task.stats_json).where(Task.user_id == user_id, Task.stats_json.isnot(None))).all():
        if not stats:
            continue
        got = stats.get("views")
        if got is None:
            continue
        views += int(got)
        measured += 1
    published_total = db.scalar(select(func.count()).select_from(Task).where(
        Task.user_id == user_id, Task.status == TaskStatus.COMPLETED)) or 0

    # Today's render outcomes. A render that FINISHED today counts, whatever day it started.
    finished_today = db.scalars(select(Task).where(
        Task.user_id == user_id, Task.finished_at.isnot(None),
        Task.finished_at >= day_start)).all()
    failed = sum(1 for t in finished_today if t.status == TaskStatus.FAILED)
    total = len(finished_today)
    render_seconds = sum(int((t.render_json or {}).get("render_seconds") or 0) for t in finished_today)
    return {
        "views": views, "measured": measured, "published_total": published_total,
        "renders_today": total, "failed_today": failed,
        "fail_pct_today": round(100 * failed / total) if total else None,
        "render_minutes_today": round(render_seconds / 60) if render_seconds else 0,
        "host": host.snapshot(),
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: CurrentUser, db: DbDep):
    channels = db.scalars(select(Channel).where(Channel.user_id == user.id)).all()
    campaigns = db.scalars(select(Campaign).where(Campaign.user_id == user.id)).all()
    tasks = db.scalars(
        select(Task).where(Task.user_id == user.id).order_by(Task.id.desc()).limit(12)
    ).all()
    # Triage inbox: the concrete items that need a human, most-recent first.
    attention_failed = db.scalars(
        select(Task).where(Task.user_id == user.id, Task.status == TaskStatus.FAILED)
        .order_by(Task.updated_at.desc()).limit(8)
    ).all()
    attention_review = db.scalars(
        select(BufferPoolItem)
        .join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        .where(Campaign.user_id == user.id, BufferPoolItem.status == BufferStatus.awaiting_review)
        .order_by(BufferPoolItem.id.desc()).limit(8)
    ).all()
    # Resolve each awaiting-review buffer item to its episode (Task) so triage links to the episode's
    # single home rather than a filtered Asset Pool grid.
    rev_pairs = {(i.campaign_id, i.episode_number) for i in attention_review}
    review_ids: dict = {}
    if rev_pairs:
        for t in db.scalars(select(Task).where(
                Task.user_id == user.id,
                Task.campaign_id.in_({c for c, _ in rev_pairs}),
                Task.episode_number.in_({e for _, e in rev_pairs}))):
            if (t.campaign_id, t.episode_number) in rev_pairs:
                review_ids[(t.campaign_id, t.episode_number)] = t.id
    # "Running now" panel: one row per active campaign (what each is doing + when it posts next).
    active_campaigns = [c for c in campaigns if c.status == CampaignStatus.active]
    autopilot_proposed = _autopilot_proposed_count(db, user.id)
    counts = _task_counts(db, user.id)
    setup = _setup_state(user, channels, campaigns)
    # One failure reads the same in triage, in the bell and on the episode page (ADR-068).
    fail_causes = {t.id: _diagnose_failure(t.error_message) for t in attention_failed}
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request, "user": user, "channels": channels, "campaigns": campaigns,
            "tasks": tasks, "nav": "dashboard",
            "health": _system_health(db, user),
            "counts": counts,
            "attention": _attention_count(db, user.id, counts),
            "attention_failed": attention_failed, "attention_review": attention_review,
            "review_ids": review_ids,
            "scorecard": _scorecard(db, user.id), "next_publish": _next_publish(db, user.id),
            "vitals": _factory_vitals(db, user.id),
            "active_running": active_campaigns,
            "autopilot_proposed": autopilot_proposed,
            "ops": _campaign_ops(db, user.id, active_campaigns),
            "camp_by_id": {c.id: c for c in campaigns},
            "chan_by_id": {c.id: c for c in channels},
            "feed": _activity_feed(tasks, {c.id: c for c in campaigns}, {c.id: c for c in channels}),
            "setup": setup, "fail_causes": fail_causes,
        },
    )


# ── Channels Manager ─────────────────────────────────────────────────────────
_CHANNEL_STATUS_FILTERS = ("active", "expired")


@app.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request, user: CurrentUser, db: DbDep, status: str = "", q: str = "",
                  flash: str = "", flash_reason: str = ""):
    status_counts = {s.value: n for s, n in db.execute(
        select(Channel.status, func.count())
        .where(Channel.user_id == user.id).group_by(Channel.status)).all()}
    total_all = sum(status_counts.values())
    status = status if status in _CHANNEL_STATUS_FILTERS else ""
    q = q.strip()
    stmt = select(Channel).where(Channel.user_id == user.id)
    if status:
        stmt = stmt.where(Channel.status == ChannelStatus(status))
    if q:
        stmt = stmt.where(Channel.channel_name.ilike(f"%{q}%"))
    channels = db.scalars(stmt).all()
    # Rollup: campaigns per channel (total + active) for the drill-down links.
    camp_counts: dict = {}
    for chid, cstatus, n in db.execute(
        select(Campaign.channel_id, Campaign.status, func.count())
        .where(Campaign.user_id == user.id).group_by(Campaign.channel_id, Campaign.status)
    ).all():
        d = camp_counts.setdefault(chid, {"total": 0, "active": 0})
        d["total"] += n
        if cstatus == CampaignStatus.active:
            d["active"] += n
    chips = [{"label": "All", "value": "", "count": total_all}] + [
        {"label": s.title(), "value": s, "count": status_counts.get(s, 0)}
        for s in _CHANNEL_STATUS_FILTERS]
    return templates.TemplateResponse(
        request, "channels.html",
        {"request": request, "user": user, "channels": channels, "nav": "channels",
         "camp_counts": camp_counts, "chips": chips, "status": status, "q": q, "total_all": total_all,
         "ap": {c.id: (c.autopilot_json or {}) for c in channels},
         # Growth series per channel (ADR-063): does publishing this much actually move subs/views?
         "growth": {c.id: analytics_service.channel_growth(db, c.id) for c in channels},
         "characters": {c.id: _sanitize_characters(c.characters_json) for c in channels},
         "flash": flash if flash in ("profile", "autopilot", "character", "char_img_ok",
                                     "char_img_fail", "no_google_client", "fb_added",
                                     "fb_added_unverified", "fb_rejected", "fb_token_ok",
                                     "fb_token_unverified", "fb_token_bad") else "",
         # Facebook's own words for why it refused. Truncated and escaped by Jinja on the way out;
         # never contains the token (see services/verification.check_facebook_page).
         "flash_reason": flash_reason[:200]},
    )


@app.post("/channels/facebook")
def add_facebook_channel(
    user: CurrentUser,
    db: DbDep,
    channel_name: str = Form(""),
    page_id: str = Form(...),
    page_access_token: str = Form(...),
    avatar_url: str = Form(""),
):
    """Connect a Facebook Page, verifying it first (ADR-068/072).

    A made-up Page id and token used to save as "● Active" and count as a connected channel; the lie
    only surfaced weeks later when a publish failed. One cheap Graph call decides — and a network
    hiccup must not block a real operator, so only a DEFINITE rejection stops the save."""
    from services import verification

    page_id = verification.normalize_page_id(page_id)
    page_access_token = (page_access_token or "").strip()
    if not page_id or not page_access_token:
        return RedirectResponse(
            "/channels?flash=fb_rejected&flash_reason="
            + quote("A Page ID and a Page Access Token are both required."), status_code=303)
    verdict = None
    try:
        verdict = verification.check_facebook_page(page_id, page_access_token)
    except Exception:  # noqa: BLE001 — verification is a guard, never a gate on our own bugs
        logger.warning("Facebook page verification raised", exc_info=True)
    if verdict is not None and verdict.ok is False:
        return RedirectResponse(
            "/channels?flash=fb_rejected&flash_reason=" + quote(verdict.detail[:200]), status_code=303)
    verified = verdict is not None and verdict.ok is True
    if verified:
        # Store the CANONICAL numeric id Graph resolved, not the username or URL that was typed: a
        # username silently breaks the day the operator renames the Page.
        page_id = verdict.page_id or page_id
    # The operator may leave the label and avatar blank and take the Page's own (ADR-072) — retyping
    # what Facebook just told us is busywork, and a hand-typed name drifts from the real Page.
    name = (channel_name or "").strip() or (verdict.name if verified else "") or f"Page {page_id}"
    avatar = (avatar_url or "").strip() or (verdict.picture if verified else None)
    creds = json.dumps({"page_id": page_id, "page_access_token": page_access_token})
    channel = Channel(
        user_id=user.id, platform=Platform.facebook, channel_name=name[:120],
        avatar_url=avatar or None, encrypted_credentials=creds, status=ChannelStatus.active,
    )
    db.add(channel)
    db.commit()
    # Only say "verified" when it actually was. Claiming Facebook accepted a token we never managed to
    # ask about is exactly the lie this check exists to remove.
    return RedirectResponse(
        "/channels?flash=" + ("fb_added" if verified else "fb_added_unverified"), status_code=303)


@app.post("/channels/{channel_id}/facebook-token")
def replace_facebook_token(db: DbDep, channel=Depends(get_owned_channel),
                           page_access_token: str = Form(...)):
    """Paste a fresh Page Access Token for an already-connected Page (ADR-072).

    Without this, marking a channel `expired` would be a dead end: the only way back was Remove +
    re-add, which deletes the channel's campaigns and rendered videos. A token is a credential that
    rotates; losing a year of campaigns because one expired is not a trade anyone would choose.
    A verified token also clears the expired flag, so the fix is visible immediately."""
    from services import verification

    if channel.platform != Platform.facebook:
        raise HTTPException(400, "Only a Facebook Page has a Page Access Token")
    token = (page_access_token or "").strip()
    if not token:
        return RedirectResponse("/channels?flash=fb_token_bad&flash_reason="
                                + quote("Paste the new token first."), status_code=303)
    creds = json.loads(channel.encrypted_credentials or "{}")
    page_id = creds.get("page_id") or ""
    verdict = None
    try:
        verdict = verification.check_facebook_page(page_id, token)
    except Exception:  # noqa: BLE001 — the guard must never be the thing that blocks a fix
        logger.warning("Facebook token replacement verification raised", exc_info=True)
    if verdict is not None and verdict.ok is False:
        return RedirectResponse("/channels?flash=fb_token_bad&flash_reason="
                                + quote(verdict.detail[:200]), status_code=303)
    creds["page_access_token"] = token
    if verdict is not None and verdict.ok is True and verdict.page_id:
        creds["page_id"] = verdict.page_id      # a re-verified Page confirms its canonical id
    channel.encrypted_credentials = json.dumps(creds)
    # Only a VERIFIED token may clear the expired flag. Storing an unverified one and declaring the
    # channel healthy would recreate exactly the lie ADR-068 removed.
    verified = verdict is not None and verdict.ok is True
    if verified:
        channel.status = ChannelStatus.active
    db.commit()
    return RedirectResponse(
        "/channels?flash=" + ("fb_token_ok" if verified else "fb_token_unverified"), status_code=303)


@app.post("/channels/{channel_id}/delete")
def delete_channel(channel=Depends(get_owned_channel), db=Depends(get_db)):
    db.delete(channel)
    db.commit()
    return RedirectResponse("/channels", status_code=303)


_AUTOPILOT_FEED_PAGE = 25


@app.get("/autopilot", response_class=HTMLResponse)
def autopilot_page(request: Request, user: CurrentUser, db: DbDep, page: int = 1):
    """The autopilot mission control: a per-channel run status strip (mode + 'last ran'), strategy
    proposals to approve/dismiss, and the full activity log of every autonomous decision with the
    data evidence + reasoning that drove it. ADR-044. The feed is paginated so it never bloats."""
    channels = db.scalars(select(Channel).where(Channel.user_id == user.id)).all()
    chan_by_id = {c.id: c for c in channels}
    # Status strip: one row per channel that has autopilot on (mode + heartbeat).
    ap_channels = [c for c in channels if (c.autopilot_json or {}).get("mode", "off") != "off"]

    proposed = db.scalars(select(AutopilotAction).where(
        AutopilotAction.user_id == user.id, AutopilotAction.status == "proposed")
        .order_by(AutopilotAction.id.desc())).all()

    # Activity feed = everything already resolved (done/applied/dismissed/failed), newest first.
    page = max(1, page)
    feed_where = (AutopilotAction.user_id == user.id, AutopilotAction.status != "proposed")
    total = db.scalar(select(func.count()).select_from(AutopilotAction).where(*feed_where)) or 0
    feed = db.scalars(select(AutopilotAction).where(*feed_where)
                      .order_by(AutopilotAction.id.desc())
                      .limit(_AUTOPILOT_FEED_PAGE).offset((page - 1) * _AUTOPILOT_FEED_PAGE)).all()
    return templates.TemplateResponse(
        request, "autopilot.html",
        {"request": request, "user": user, "nav": "autopilot", "proposed": proposed,
         "history": feed, "ap_channels": ap_channels, "chan_by_id": chan_by_id,
         "page": page, "feed_pages": max(1, -(-total // _AUTOPILOT_FEED_PAGE)), "feed_total": total},
    )


@app.post("/autopilot/{action_id}/approve")
def approve_autopilot_action(action_id: int, user: CurrentUser, db: DbDep):
    action = db.get(AutopilotAction, action_id)
    if action is None or action.user_id != user.id:
        raise HTTPException(404, "Action not found")
    if action.status == "proposed":
        from workers import scheduler

        scheduler.apply_autopilot_action(db, action)
    return RedirectResponse("/autopilot", status_code=303)


@app.post("/autopilot/{action_id}/dismiss")
def dismiss_autopilot_action(action_id: int, user: CurrentUser, db: DbDep):
    from datetime import datetime

    action = db.get(AutopilotAction, action_id)
    if action is None or action.user_id != user.id:
        raise HTTPException(404, "Action not found")
    if action.status == "proposed":
        action.status = "dismissed"
        action.resolved_at = datetime.utcnow()
        db.commit()
    return RedirectResponse("/autopilot", status_code=303)


_PROFILE_LANGS = ("vi", "en", "es")
_valid_timezone = timezones.is_valid  # single definition of "is this a real IANA zone" (DRY)


def _channel_profile_cfg(channel) -> dict:
    """Cfg-shaped localization defaults from a channel's profile (language / voice / timezone) — used
    to seed a new campaign so it inherits the channel's persona. Empty if the channel has no profile
    or is None. See ADR-045."""
    p = (channel.profile_json or {}) if channel else {}
    cfg: dict = {}
    if p.get("language") in _PROFILE_LANGS:
        cfg["language"] = p["language"]
    if p.get("voice"):
        cfg["voice"] = p["voice"]
    if p.get("timezone"):
        cfg["timezone"] = p["timezone"]
    return cfg


@app.post("/channels/{channel_id}/profile")
def set_channel_profile(channel=Depends(get_owned_channel), db=Depends(get_db),
                        audience: str = Form(""), language: str = Form(""),
                        timezone: str = Form(""), voice: str = Form(""),
                        style: str = Form(""), vision: str = Form("")):
    """Set a channel's persona / localization profile (ADR-045). Everything is validated/whitelisted
    like the campaign form — a bad value is dropped, never stored, so it can't break rendering."""
    allowed_voices = {v for vs in VOICE_CHOICES.values() for v, _label in vs}
    p: dict = {}
    if audience.strip():
        p["audience"] = audience.strip()[:80]
    if language in _PROFILE_LANGS:
        p["language"] = language
    if timezone.strip() and _valid_timezone(timezone.strip()):
        p["timezone"] = timezone.strip()
    if voice.strip() and voice.strip() in allowed_voices:
        p["voice"] = voice.strip()
    if style.strip():
        p["style"] = style.strip()[:200]
    if vision.strip():
        p["vision"] = vision.strip()[:200]
    channel.profile_json = p or None
    db.commit()
    return RedirectResponse("/channels?flash=profile", status_code=303)


_MAX_CHARACTERS = 12  # per channel — plenty for random casting, small enough to keep the UI/quota sane


_MAX_CHARACTER_IMAGE_BYTES = 15 * 1024 * 1024  # 15 MB cap — phone photos are big; we downscale anyway


def _sanitize_characters(raw) -> list[dict]:
    """Coerce stored characters_json into the canonical shape, dropping anything malformed. One place
    the character record schema is enforced (DRY) — used by the render path and the manager UI."""
    out: list[dict] = []
    for c in raw or []:
        if not isinstance(c, dict) or not str(c.get("name", "")).strip():
            continue
        out.append({
            "id": str(c.get("id") or "")[:32],
            "name": str(c["name"]).strip()[:60],
            "description": str(c.get("description") or "").strip()[:300],
            "style": str(c.get("style") or "").strip()[:120],
            # Operator-uploaded reference image (W4/ADR-054): the PERMANENT identity anchor the Studio
            # render uses directly as the character sheet. NULL = the AI draws + caches its own sheet.
            "ref_image": (str(c["ref_image"]) if c.get("ref_image") else None),
            # Unguessable token → the PUBLIC url a Pollinations image-editing model (kontext) fetches
            # the reference from (ADR-055). Random, so it can't be enumerated. NULL = no uploaded image.
            "ref_token": (str(c["ref_token"]) if c.get("ref_token") else None),
            # Path to the once-generated character reference sheet (set by the Studio render path).
            "sheet_path": (str(c["sheet_path"]) if c.get("sheet_path") else None),
        })
    return out


def _public_ref_dir() -> str:
    # Reference images live under one token-keyed dir; the public /studio/ref route serves from here.
    return os.path.join(settings.MEDIA_ROOT, "studio", "public_refs")


def _save_character_image(upload: UploadFile) -> tuple[str, str] | None:
    """Re-encode an uploaded reference image to a normalized PNG keyed by a random token; return
    (path, token) or None if it isn't a valid image / too big. Re-encoding via PIL strips metadata
    and guarantees a real image (never trusts the client's content-type or filename). The random token
    is the file's name AND its public URL slug, so the public route needs no DB lookup and the image
    can't be enumerated."""
    import io
    import secrets

    from PIL import Image

    try:  # opportunistic HEIC/HEIF (iPhone photos) support — no-op if pillow-heif isn't installed
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001
        pass

    data = upload.file.read(_MAX_CHARACTER_IMAGE_BYTES + 1)
    if not data:
        logger.warning("Character image upload was empty (filename=%r)", upload.filename)
        return None
    if len(data) > _MAX_CHARACTER_IMAGE_BYTES:
        logger.warning("Character image too big: %.1f MB > %d MB limit (filename=%r)",
                       len(data) / 1024 / 1024, _MAX_CHARACTER_IMAGE_BYTES // 1024 // 1024, upload.filename)
        return None
    try:
        Image.open(io.BytesIO(data)).verify()  # reject a non-image / truncated upload before trusting it
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001 — any decode failure = not a usable image
        logger.warning("Character image not a readable image (filename=%r) — needs PNG/JPG/WebP",
                       upload.filename)
        return None
    img.thumbnail((1024, 1024))  # cap dimensions — a reference sheet needs no more
    token = secrets.token_hex(16)
    os.makedirs(_public_ref_dir(), exist_ok=True)
    path = os.path.join(_public_ref_dir(), f"{token}.png")
    img.save(path, "PNG")
    return path, token


@app.post("/channels/{channel_id}/characters")
def add_channel_character(channel=Depends(get_owned_channel), db=Depends(get_db),
                          name: str = Form(...), description: str = Form(""),
                          style: str = Form(""), image: UploadFile | None = File(None)):
    """Add a Studio-Mode character to a channel's cast (ADR-052/054). Each episode drawn in Studio
    Mode picks one at random and keeps its face/style consistent across every frame. An optional
    uploaded reference image becomes the PERMANENT identity anchor (best consistency); without one the
    AI draws a sheet from the description. Fictional characters only — a creative brief, not a real
    person (a real photo is allowed only for yourself / with explicit consent)."""
    cast = _sanitize_characters(channel.characters_json)
    flash = "character"
    if name.strip() and len(cast) < _MAX_CHARACTERS:
        import uuid

        attempted = bool(image and image.filename)
        saved = _save_character_image(image) if attempted else None
        if attempted:
            flash = "char_img_ok" if saved else "char_img_fail"   # tell the operator if the image stuck
        cast.append({
            "id": uuid.uuid4().hex[:8],
            "name": name.strip()[:60],
            "description": description.strip()[:300],
            "style": style.strip()[:120],
            "ref_image": saved[0] if saved else None,
            "ref_token": saved[1] if saved else None,
            "sheet_path": None,
        })
        channel.characters_json = cast
        db.commit()
    return RedirectResponse(f"/channels?flash={flash}", status_code=303)


def _remove_ref_file(path: str | None) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            logger.warning("Could not remove character image %s", path)


@app.post("/channels/{channel_id}/characters/{char_id}/image")
def set_channel_character_image(char_id: str, channel=Depends(get_owned_channel),
                                db=Depends(get_db), image: UploadFile = File(...)):
    """Replace a character's reference image later (W4). Re-encoded + stored like the add path; the
    old file is removed so a stale token stops resolving."""
    cast = _sanitize_characters(channel.characters_json)
    ch = next((c for c in cast if c["id"] == char_id), None)
    flash = "character"
    if ch is not None and image and image.filename:
        saved = _save_character_image(image)
        flash = "char_img_ok" if saved else "char_img_fail"
        if saved:
            _remove_ref_file(ch.get("ref_image"))
            ch["ref_image"], ch["ref_token"] = saved
            channel.characters_json = cast
            db.commit()
    return RedirectResponse(f"/channels?flash={flash}", status_code=303)


@app.get("/channels/{channel_id}/characters/{char_id}/image")
def get_channel_character_image(char_id: str, channel=Depends(get_owned_channel)):
    """Serve a character's uploaded reference image for the cast-list preview (ownership-checked)."""
    ch = next((c for c in _sanitize_characters(channel.characters_json) if c["id"] == char_id), None)
    if not ch or not ch.get("ref_image") or not os.path.exists(ch["ref_image"]):
        raise HTTPException(404, "No reference image")
    return FileResponse(ch["ref_image"], media_type="image/png")


@app.get("/studio/ref/{token}")
def public_character_reference(token: str):
    """PUBLIC (no auth) reference-image endpoint so a Pollinations image-editing model (kontext) can
    fetch a character's uploaded reference over the internet (ADR-055). The token is a 32-hex random
    slug = the file name, so there's no DB lookup and no enumeration; the regex bars path traversal.
    This is the one intentional public exposure of a reference image (the operator opted in)."""

    if not re.fullmatch(r"[a-f0-9]{8,64}", token):
        raise HTTPException(404, "Not found")
    path = os.path.join(_public_ref_dir(), f"{token}.png")
    if not os.path.exists(path):
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="image/png")


@app.post("/channels/{channel_id}/characters/{char_id}/delete")
def delete_channel_character(char_id: str, channel=Depends(get_owned_channel), db=Depends(get_db)):
    """Remove a character from a channel's cast, deleting its uploaded reference image. Any generated
    sheet is left for the normal media cleanup sweep — no episode references it once it's gone."""
    cast = _sanitize_characters(channel.characters_json)
    keep = [c for c in cast if c["id"] != char_id]
    for c in cast:
        if c["id"] == char_id:
            _remove_ref_file(c.get("ref_image"))
    channel.characters_json = keep or None
    db.commit()
    return RedirectResponse("/channels?flash=character", status_code=303)


@app.post("/channels/{channel_id}/autopilot")
def set_channel_autopilot(channel=Depends(get_owned_channel), db=Depends(get_db),
                          mode: str = Form("off"), interval_hours: str = Form(""),
                          approve_min: str = Form(""), reject_max: str = Form("")):
    """Set a channel's autopilot mode + cadence + review strictness (per-channel, ADR-044).
    Off = the operator drives everything (default). Values are validated/clamped like every form."""
    cfg = dict(channel.autopilot_json or {})
    cfg["mode"] = mode if mode in autopilot.MODES else "off"
    if interval_hours.strip().isdigit():
        cfg["interval_hours"] = max(1, min(int(interval_hours), 24))
    review = dict(cfg.get("review") or {})
    if approve_min.strip().isdigit():
        review["approve_min"] = max(1, min(int(approve_min), 10))
    if reject_max.strip().isdigit():
        review["reject_max"] = max(0, min(int(reject_max), 9))
    if review:
        # Keep stored values consistent with how the engine reads them: approve must sit strictly
        # above reject, so the saved config the page shows is exactly the one the AI acts on.
        if "approve_min" in review and "reject_max" in review:
            review["approve_min"] = min(10, max(review["approve_min"], review["reject_max"] + 1))
        cfg["review"] = review
    channel.autopilot_json = cfg
    db.commit()
    return RedirectResponse("/channels?flash=autopilot", status_code=303)


# ── Google OAuth2 web flow (connect a YouTube channel) ───────────────────────
@app.get("/oauth/google/start")
def google_oauth_start(request: Request, user: CurrentUser):
    """Begin the YouTube connect flow — or explain what is missing (ADR-068).

    Without a configured Google client this used to redirect to accounts.google.com with
    `client_id=None`, so a first-time operator's very first click landed on Google's "Error 400:
    invalid_request" page with nothing to tell them the app itself needed setting up first."""
    if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET):
        return RedirectResponse("/channels?flash=no_google_client", status_code=303)
    flow = _google_flow(YOUTUBE_SCOPES, "/oauth/google/callback")
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    request.session["oauth_state"] = state
    request.session["oauth_user"] = user.id
    return RedirectResponse(auth_url)


@app.get("/oauth/google/callback")
def google_oauth_callback(request: Request, db: DbDep):
    from googleapiclient.discovery import build

    incoming = request.query_params.get("state")
    stored = request.session.pop("oauth_state", None)
    if not incoming or not stored or incoming != stored:
        raise HTTPException(400, "OAuth state mismatch")
    # Pop (don't just read) the pending user, and require it — a stale/absent value must not let a
    # crafted callback attach an attacker's channel to whoever last connected one.
    user_id = request.session.pop("oauth_user", None)
    if not user_id:
        raise HTTPException(400, "No pending channel connection for this session")

    flow = _google_flow(YOUTUBE_SCOPES, "/oauth/google/callback")
    # Exchange by code (not the full callback URL) — robust behind the HTTP-origin tunnel.
    flow.fetch_token(code=request.query_params.get("code"))
    creds = flow.credentials

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    info = youtube.channels().list(part="snippet", mine=True).execute()
    item = (info.get("items") or [{}])[0]
    snippet = item.get("snippet", {})
    name = snippet.get("title", "YouTube Channel")
    avatar = snippet.get("thumbnails", {}).get("default", {}).get("url")

    bundle = json.dumps({
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        # Persist expiry so build_credentials can proactively refresh + persist (not rely on a 401).
        "token_expiry": creds.expiry.isoformat() if creds.expiry else None,
    })
    channel = Channel(
        user_id=user_id, platform=Platform.youtube, channel_name=name, avatar_url=avatar,
        encrypted_credentials=bundle, status=ChannelStatus.active,
    )
    db.add(channel)
    db.commit()
    return RedirectResponse("/channels", status_code=303)


def _google_flow(scopes: list[str], redirect_path: str):
    """Build a Google OAuth flow. Reused by the YouTube-connect flow and the SSO login flow."""
    from google_auth_oauthlib.flow import Flow

    client_config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    flow = Flow.from_client_config(client_config, scopes=scopes)
    flow.redirect_uri = settings.OAUTH_REDIRECT_BASE.rstrip("/") + redirect_path
    return flow


# ── Campaigns Manager ────────────────────────────────────────────────────────
_CAMPAIGN_STATUS_FILTERS = ("active", "pending", "failed", "completed")


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request, user: CurrentUser, db: DbDep, channel: int | None = None,
                   status: str = "", q: str = ""):
    conds = [Campaign.user_id == user.id]
    if channel:  # scoped to one channel (drill-down from Channels)
        conds.append(Campaign.channel_id == channel)
    # True per-status counts for the chips (over the current scope, before the status filter).
    status_counts = {s.value: n for s, n in db.execute(
        select(Campaign.status, func.count()).where(*conds).group_by(Campaign.status)).all()}
    total_all = sum(status_counts.values())
    status = status if status in _CAMPAIGN_STATUS_FILTERS else ""
    q = q.strip()
    stmt = select(Campaign).where(*conds)
    if status:
        stmt = stmt.where(Campaign.status == CampaignStatus(status))
    if q:
        stmt = stmt.where(Campaign.topic_name.ilike(f"%{q}%"))
    campaigns = db.scalars(stmt.order_by(Campaign.id.desc())).all()
    # Operational sort: the ones that are running (or need starting) first, finished ones last —
    # newest-first within each group. So "what's live" is at the top, not buried by creation order.
    _rank = {"active": 0, "pending": 1, "failed": 2, "completed": 3}
    campaigns = sorted(campaigns, key=lambda c: (_rank.get(c.status.value, 9), -c.id))
    channels = {c.id: c for c in db.scalars(select(Channel).where(Channel.user_id == user.id)).all()}
    chips = [{"label": "All", "value": "", "count": total_all}] + [
        {"label": s.title(), "value": s, "count": status_counts.get(s, 0)}
        for s in _CAMPAIGN_STATUS_FILTERS]
    return templates.TemplateResponse(
        request,
        "campaigns.html",
        {"request": request, "user": user, "campaigns": campaigns, "channels": channels, "nav": "campaigns",
         "ops": _campaign_ops(db, user.id, campaigns),
         "cls": autopilot.classify_campaigns(db, campaigns),  # data-driven performance verdict
         "scope_channel": channels.get(channel) if channel else None,
         "chips": chips, "status": status, "q": q, "total_all": total_all,
         "scope_hidden": {"channel": channel} if channel else {}},
    )


@app.get("/campaigns/new", response_class=HTMLResponse)
def campaign_new_form(request: Request, user: CurrentUser, db: DbDep,
                      from_id: int | None = None, channel: int | None = None):
    """New-campaign form. With ?from_id=<campaign>, prefills from an owned campaign (Duplicate —
    e.g. same horror persona in another language to another channel). With ?channel=<id> (carried
    from the scoped list), preselects that channel. A fresh form seeds its defaults from the user's
    Settings (new-campaign defaults) so the common choices are already right."""
    channels = db.scalars(select(Channel).where(Channel.user_id == user.id)).all()
    ctx: dict = {"request": request, "user": user, "channels": channels, "nav": "campaigns"}
    sel_channel: int | None = None
    if from_id is not None:
        source = db.get(Campaign, from_id)
        if source is not None and source.user_id == user.id:
            ctx["source"] = source
            ctx["cfg"] = source.config_json or {}
            sel_channel = source.channel_id  # Duplicate targets the source's channel by default
    else:  # a fresh form starts from the user's saved new-campaign defaults (Settings page)
        ctx["cfg"] = _new_campaign_defaults(user)
        ctx["default_episodes"] = (user.settings_json or {}).get("total_episodes") or 10
        # The very first campaign defaults to Review-first (ADR-068). Auto-publish is the right
        # steady state, but as a *first* experience it uploads to a real channel before the operator
        # has ever seen what this factory produces. An explicit Settings choice always wins.
        if not (user.settings_json or {}).get("publish_mode"):
            first = db.scalar(select(func.count()).select_from(Campaign)
                              .where(Campaign.user_id == user.id)) == 0
            if first:
                ctx["cfg"] = {**ctx["cfg"], "auto_publish": False}
                ctx["first_campaign"] = True
    if sel_channel is None and channel is not None:  # follow the scoped channel if the user owns it
        ch = db.get(Channel, channel)
        if ch is not None and ch.user_id == user.id:
            sel_channel = channel
    # Localize from the selected channel's profile (profile > user Settings), and expose every
    # channel's localization so the client re-localizes when the operator switches the channel.
    if sel_channel is not None and not ctx.get("source"):
        sel = next((c for c in channels if c.id == sel_channel), None)
        if sel is not None:
            ctx["cfg"] = {**(ctx.get("cfg") or {}), **_channel_profile_cfg(sel)}
    ctx["sel_channel"] = sel_channel
    ctx["channel_profiles"] = {c.id: _channel_profile_cfg(c) for c in channels}
    return templates.TemplateResponse(request, "campaign_new.html", ctx)


@app.post("/campaigns/preview-script")
def preview_script(
    user: CurrentUser,
    topic_name: str = Form(""),
    language: str = Form("en"),
    system_prompt: str = Form(""),
    persona: str = Form(""),
    style_examples: str = Form(""),
    catchphrase_open: str = Form(""),
    catchphrase_close: str = Form(""),
    catchphrase_open_on: bool = Form(False),
    catchphrase_close_on: bool = Form(False),
    rate_pct: int = Form(0),
    duration_min_s: str = Form(""),
    duration_max_s: str = Form(""),
):
    """Dry-run: generate ONE script from the current (possibly unsaved) form values so the
    operator can tune the persona cheaply — 1 AI call, nothing rendered, nothing stored."""
    from core import ai_engine

    key = user.gemini_api_key or settings.GEMINI_API_KEY
    if not key:
        return JSONResponse({"error": "Add a Gemini API key first (Credentials page or .env)."},
                            status_code=400)
    if not topic_name.strip():
        return JSONResponse({"error": "Enter a topic name first."}, status_code=400)
    lang = language if language in ("en", "vi", "es") else "en"
    lo = int(duration_min_s) if duration_min_s.strip().isdigit() else None
    hi = int(duration_max_s) if duration_max_s.strip().isdigit() else None
    try:
        script = ai_engine.generate_script(
            topic=topic_name.strip(), language=lang, total_episodes=10, episode=1, api_key=key,
            custom_system_prompt=system_prompt.strip() or None,
            persona=persona.strip() or None,
            style_examples=style_examples.strip() or None,
            catchphrase_open=(catchphrase_open.strip() or None) if catchphrase_open_on else None,
            catchphrase_close=(catchphrase_close.strip() or None) if catchphrase_close_on else None,
            self_critique=False,  # preview stays cheap: 1 call (2 if the length fix fires)
            duration_min_s=lo if lo and hi else None,
            duration_max_s=hi if lo and hi else None,
            rate_pct=rate_pct,
            model=user.gemini_model or settings.GEMINI_MODEL,
        )
    except Exception as exc:  # noqa: BLE001 — clean retry message, never a stack trace
        logger.warning("Script preview failed: %s", type(exc).__name__)
        return JSONResponse({"error": "AI preview failed — please try again."}, status_code=502)
    narration = " ".join(s.narration for s in script.scenes)
    return {
        "scenes": [{"narration": s.narration, "keywords": s.pexels_keywords}
                   for s in script.scenes],
        "title": script.metadata_variations[0].title,
        "synopsis": script.synopsis,
        "est_seconds": round(ai_engine.estimate_speech_seconds(narration, lang, rate_pct)),
    }


@app.post("/campaigns/propose")
def propose_campaign_route(user: CurrentUser, db: DbDep, topic: str = Form(""),
                           language: str = Form(""), video_format: str = Form("short"),
                           channel_id: str = Form(""), content_style: str = Form("story")):
    """AI-design a whole campaign config from a title (or from scratch). Returns JSON the New
    Campaign form fills in for review — nothing is saved until the user clicks Create. The form's
    video_format is an explicit constraint (short vs long, forced onto the result); the selected
    channel's profile localizes the whole design to that channel's audience/country (ADR-045)."""
    import random

    from core import ai_engine

    key = user.gemini_api_key or settings.GEMINI_API_KEY
    if not key:
        return JSONResponse({"error": "Add a Gemini API key first (Credentials page or .env)."},
                            status_code=400)
    lang = language if language in ("en", "vi", "es") else None
    fmt = "long" if video_format == "long" else "short"
    profile = None
    if channel_id.strip().isdigit():
        ch = db.get(Channel, int(channel_id))
        if ch is not None and ch.user_id == user.id:
            profile = ch.profile_json
    try:
        proposal = ai_engine.propose_campaign(
            topic=topic.strip() or None, language=lang, video_format=fmt, profile=profile, api_key=key,
            content_style="quote" if content_style == "quote" else "story",
            model=user.gemini_model or settings.GEMINI_MODEL,
            nonce=random.randint(1, 1_000_000),
        )
    except Exception as exc:  # noqa: BLE001 — return a clean retry message, not a stack trace
        logger.warning("Campaign proposal failed: %s", type(exc).__name__)
        return JSONResponse({"error": "AI proposal failed — please try again."}, status_code=502)
    if proposal.music_mode == "auto" and not settings.FREESOUND_API_KEY:
        # Config truth: never propose a mode this box cannot run (auto music needs a Freesound key
        # in .env; without it every episode would fail loudly at render time).
        proposal.music_mode = "none"
    return proposal.model_dump()


def _build_campaign_config(
    *, language: str, system_prompt: str, voice: str, rate_pct: int, subtitle_style: str,
    music_path: str, music_volume: float, posting_slots: str, ab_testing: bool, cta: str,
    privacy: str, publish_mode: str, buffer_size: str,
    watermark_path: str, tint_color: str, tint_opacity: float, mirror: bool,
    persona: str, style_examples: str, catchphrase_open: str, catchphrase_close: str,
    continuity: str, timezone: str,
    catchphrase_open_on: bool = True, catchphrase_close_on: bool = True,
    motion: str = "on", caption_theme: str = "highlight", self_critique: str = "on",
    script_depth: str = "standard", video_format: str = "short",
    visual_source: str = "stock", visual_style: str = "", title_overlay: str = "off",
    content_style: str = "story", signature: str = "", voice_delivery: str = "normal",
    music_mode: str = "none", music_mood: str = "",
    color_grade: str = "", auto_qc: str = "on",
    max_per_day: str = "", min_per_day: str = "",
    title_prefix: str = "",
    posting_days: list[str] | None = None,
    duration_min_s: str = "", duration_max_s: str = "",
    affiliate_url: str = "", affiliate_label: str = "",
) -> dict:
    """One place turns the campaign form into config_json (DRY: shared by create and edit)."""
    config: dict = {
        # Whitelist to the supported set (VideoScript.language is a Literal) — an unsupported value
        # would make every episode fail generation. Default to English.
        "language": language if language in ("en", "vi", "es") else "en",
        "system_prompt": system_prompt, "voice": voice or None,
        # Voice delivery (ADR-071): "soft" is the intimate, confiding read the aesthetic quote
        # style needs — slower + lower pitch + a softening audio pass, applied in core/tts.py.
        "voice_delivery": "soft" if voice_delivery == "soft" else "normal",
        "rate_pct": rate_pct, "subtitle_style": subtitle_style,
        "music_path": music_path or None, "music_volume": music_volume,
        "posting_slots": [s.strip() for s in posting_slots.split(",") if s.strip()],
        # Weekday gate for the slots (empty = every day). Whitelisted to real day keys.
        "posting_days": [d for d in (posting_days or [])
                         if d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")],
        "ab_testing": ab_testing, "cta": cta or None,
        "privacy": privacy, "auto_publish": publish_mode != "review",
        "buffer_size": int(buffer_size) if buffer_size.strip().isdigit() else None,
        # Persona / humanization + series memory (ADR-011)
        "persona": persona or None,
        "style_examples": style_examples or None,
        # The text is always stored; a per-campaign on/off flag decides whether it's applied — so the
        # operator can pause a catchphrase without losing it (default on = existing behaviour).
        "catchphrase_open": catchphrase_open or None,
        "catchphrase_open_on": bool(catchphrase_open_on),
        "catchphrase_close": catchphrase_close or None,
        "catchphrase_close_on": bool(catchphrase_close_on),
        "continuity": continuity if continuity in ("none", "no_repeat", "serial") else "none",
        # Validate like the profile: a bad zone is dropped to None (server default) rather than
        # silently stored and then misinterpreted as UTC by the scheduler.
        "timezone": timezone.strip() if timezone.strip() and _valid_timezone(timezone.strip()) else None,
        # Cinema Polish + critic loop — "on"/"off" strings so an absent field means ON (default).
        "motion": "off" if motion == "off" else "on",
        "caption_theme": caption_theme if caption_theme in ("classic", "highlight", "boxed", "neon") else "highlight",
        "self_critique": "off" if self_critique == "off" else "on",
        # Script depth: "deep" adds a research/brief Gemini pass for fact-rich storytelling.
        "script_depth": "deep" if script_depth == "deep" else "standard",
        # Output format: "short" = vertical 1080×1920 clips; "long" = horizontal 16:9 multi-minute.
        "video_format": "long" if video_format == "long" else "short",
        # Visual source (ADR-052): "stock" = Pexels footage (default); "studio" = AI-drawn keyframes
        # of the channel's consistent characters, animated by Ken-Burns motion. `visual_style` is an
        # optional per-campaign art-style override (blank = the character's own style / channel default).
        "visual_source": "studio" if visual_source == "studio" else "stock",
        "visual_style": visual_style.strip()[:200] or None,
        # Billboard title (ADR-054): burn the hook title into the video (top, two-tone) AND draw it as
        # a poster-style thumbnail — the reference-channel look. One toggle drives both. Default off.
        "title_overlay": "on" if title_overlay == "on" else "off",
        # Content style (ADR-056): "story" = normal narrated video (default); "quote" = aesthetic
        # poem-per-video with a per-episode Vibe roll, centered quote text, drawn visuals + scribble
        # cover. `signature` is an optional custom on-screen text mark (channel name), drawn small
        # lower-centre on every frame + the thumbnail (blank = none).
        "content_style": "quote" if content_style == "quote" else "story",
        "signature": signature.strip()[:40] or None,
        # Music: none | auto (random CC0 by mood, per episode) | file (operator-supplied path).
        "music_mode": music_mode if music_mode in ("none", "auto", "file") else "none",
        "music_mood": music_mood.strip() or None,
        # Auto-QC gate (ADR-013): colour grade baked into the encode; machine review of output.
        # "vintage" (grain + sepia + vignette) is the quote look from ADR-056 — it was written and
        # rendered correctly but omitted here, so every campaign that asked for it silently got no
        # grade at all (ADR-071).
        "color_grade": color_grade if color_grade in COLOR_GRADE_CHOICES else None,
        "auto_qc": "off" if auto_qc == "off" else "on",
        # Daily pacing: cap NEW renders per local day (quota rationing across campaigns), and a
        # published-minimum watchdog that alerts (it cannot force publishes).
        "max_per_day": int(max_per_day) if max_per_day.strip().isdigit() and int(max_per_day) > 0 else None,
        "min_per_day": int(min_per_day) if min_per_day.strip().isdigit() and int(min_per_day) > 0 else None,
        # Optional channel brand mark prepended to every AI title (titles themselves never carry
        # the series name / episode number — they must stand alone as hooks).
        "title_prefix": title_prefix.strip()[:40] or None,
        # Monetization: an affiliate/product link auto-appended to every description and pinned
        # comment, always with a disclosure marker. Only http(s) URLs are accepted.
        "affiliate_url": affiliate_url.strip()[:300]
        if affiliate_url.strip().startswith(("http://", "https://")) else None,
        "affiliate_label": affiliate_label.strip()[:30] or None,
    }
    # Target spoken length range (seconds). Stored only when BOTH bounds are valid; auto-ordered.
    # Bounds depend on format: shorts cap at 180s; long-form allows up to 15 min.
    lo = int(duration_min_s) if duration_min_s.strip().isdigit() else None
    hi = int(duration_max_s) if duration_max_s.strip().isdigit() else None
    if lo and hi:
        floor, ceil = (60, 900) if config["video_format"] == "long" else (10, 180)
        lo, hi = sorted((max(floor, min(lo, ceil)), max(floor, min(hi, ceil))))
        config["duration_min_s"], config["duration_max_s"] = lo, hi
    else:
        config["duration_min_s"] = config["duration_max_s"] = None
    if watermark_path or (tint_color and tint_opacity > 0) or mirror:
        config["branding"] = {
            "watermark_path": watermark_path or None,
            "tint_color": tint_color or None,
            "tint_opacity": tint_opacity,
            "mirror": mirror,
        }
    return config


# The full campaign form field set (create and edit share it — and every field is honored by the
# pipeline; no silent no-ops).
def _campaign_form(  # noqa: PLR0913 — mirrors the 3-tab form
    topic_name: str = Form(...),
    channel_id: int = Form(...),
    total_episodes: int = Form(...),
    language: str = Form("en"),
    system_prompt: str = Form(""),
    voice: str = Form(""),
    voice_delivery: str = Form("normal"),
    rate_pct: int = Form(0),
    subtitle_style: str = Form("word"),
    music_path: str = Form(""),
    music_volume: float = Form(0.15),
    posting_slots: str = Form(""),
    ab_testing: bool = Form(False),
    cta: str = Form(""),
    privacy: str = Form("public"),
    publish_mode: str = Form("auto"),
    buffer_size: str = Form(""),
    watermark_path: str = Form(""),
    tint_color: str = Form(""),
    tint_opacity: float = Form(0.0),
    mirror: bool = Form(False),
    persona: str = Form(""),
    style_examples: str = Form(""),
    catchphrase_open: str = Form(""),
    catchphrase_close: str = Form(""),
    catchphrase_open_on: bool = Form(False),
    catchphrase_close_on: bool = Form(False),
    continuity: str = Form("none"),
    timezone: str = Form(""),
    motion: str = Form("on"),
    caption_theme: str = Form("highlight"),
    self_critique: str = Form("on"),
    script_depth: str = Form("standard"),
    video_format: str = Form("short"),
    visual_source: str = Form("stock"),
    visual_style: str = Form(""),
    title_overlay: str = Form("off"),
    content_style: str = Form("story"),
    signature: str = Form(""),
    music_mode: str = Form("none"),
    music_mood: str = Form(""),
    color_grade: str = Form(""),
    auto_qc: str = Form("on"),
    max_per_day: str = Form(""),
    min_per_day: str = Form(""),
    title_prefix: str = Form(""),
    posting_days: list[str] = Form([]),
    duration_min_s: str = Form(""),
    duration_max_s: str = Form(""),
    affiliate_url: str = Form(""),
    affiliate_label: str = Form(""),
) -> dict:
    return {
        "topic_name": topic_name, "channel_id": channel_id, "total_episodes": total_episodes,
        "config": _build_campaign_config(
            language=language, system_prompt=system_prompt, voice=voice, rate_pct=rate_pct,
            subtitle_style=subtitle_style, music_path=music_path, music_volume=music_volume,
            posting_slots=posting_slots, ab_testing=ab_testing, cta=cta, privacy=privacy,
            publish_mode=publish_mode, buffer_size=buffer_size, watermark_path=watermark_path,
            tint_color=tint_color, tint_opacity=tint_opacity, mirror=mirror,
            persona=persona, style_examples=style_examples, catchphrase_open=catchphrase_open,
            catchphrase_close=catchphrase_close, continuity=continuity, timezone=timezone,
            catchphrase_open_on=catchphrase_open_on, catchphrase_close_on=catchphrase_close_on,
            motion=motion, caption_theme=caption_theme, self_critique=self_critique,
            script_depth=script_depth, video_format=video_format,
            visual_source=visual_source, visual_style=visual_style, title_overlay=title_overlay,
            content_style=content_style, signature=signature, voice_delivery=voice_delivery,
            music_mode=music_mode, music_mood=music_mood,
            color_grade=color_grade, auto_qc=auto_qc,
            max_per_day=max_per_day, min_per_day=min_per_day,
            title_prefix=title_prefix, posting_days=posting_days,
            duration_min_s=duration_min_s, duration_max_s=duration_max_s,
            affiliate_url=affiliate_url, affiliate_label=affiliate_label,
        ),
    }


def _campaigns_redirect(channel_id) -> RedirectResponse:
    """Back to the campaigns list, preserving the channel scope the operator was in (so an action
    taken while filtered to a channel doesn't dump them back to 'all campaigns')."""
    return RedirectResponse(f"/campaigns?channel={channel_id}" if channel_id else "/campaigns",
                            status_code=303)


def _campaign_return(return_to: str) -> str | None:
    """Safe internal campaign-hub path (`/campaigns/<digits>` or `/campaigns/<digits>/<tab>`) if
    `return_to` is one, else None — lets a hub action land back on the hub instead of the list."""

    return return_to if re.fullmatch(r"/campaigns/\d+(?:/[a-z]+)?", return_to or "") else None


@app.post("/campaigns")
def create_campaign(user: CurrentUser, db: DbDep, form: dict = Depends(_campaign_form),
                    start_now: str = Form("")):
    # Verify the target channel belongs to the user (tenant isolation).
    channel = db.get(Channel, form["channel_id"])
    if channel is None or channel.user_id != user.id:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    campaign = Campaign(
        user_id=user.id, channel_id=form["channel_id"], topic_name=form["topic_name"],
        total_episodes=form["total_episodes"], status=CampaignStatus.pending,
        config_json=form["config"],
    )
    db.add(campaign)
    db.commit()
    if start_now:  # "Create & Start" — same path as the standalone start route
        campaign.status = CampaignStatus.active
        db.commit()
        video_worker.hydrate_buffers(db)
    # Land on the new campaign's hub — you see what you just made (and, if started, it rendering)
    # instead of hunting for it in the list.
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@app.get("/campaigns/{campaign_id}/settings", response_class=HTMLResponse)
def campaign_settings(request: Request, user: CurrentUser, db: DbDep,
                      campaign=Depends(get_owned_campaign)):
    """Settings tab of the campaign hub — the edit form, wrapped in the shared hub header/tabs."""
    channels = db.scalars(select(Channel).where(Channel.user_id == user.id)).all()
    return templates.TemplateResponse(
        request, "campaign_new.html",
        {"request": request, "user": user, "channels": channels, "nav": "campaigns",
         "campaign": campaign, "cfg": campaign.config_json or {}, "hub_active": "settings",
         "sel_channel": campaign.channel_id,
         "channel": db.get(Channel, campaign.channel_id),
         "asset_count": _buffer_counts(db, user.id).get(campaign.id, {"ready": 0, "awaiting_review": 0})},
    )


@app.get("/campaigns/{campaign_id}/edit")
def campaign_edit_form(campaign=Depends(get_owned_campaign)):
    """Legacy edit URL → the hub's Settings tab (301 permanent: bookmarks/history settle on the
    canonical URL instead of the redirect lingering in the Back chain)."""
    return RedirectResponse(f"/campaigns/{campaign.id}/settings", status_code=301)


@app.post("/campaigns/{campaign_id}/edit")
def update_campaign(user: CurrentUser, db: DbDep, campaign=Depends(get_owned_campaign),
                    form: dict = Depends(_campaign_form)):
    channel = db.get(Channel, form["channel_id"])
    if channel is None or channel.user_id != user.id:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    campaign.topic_name = form["topic_name"]
    campaign.channel_id = form["channel_id"]
    campaign.total_episodes = form["total_episodes"]
    campaign.config_json = form["config"]
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)  # back to the hub Overview


@app.post("/campaigns/{campaign_id}/start")
def start_campaign(campaign=Depends(get_owned_campaign), db=Depends(get_db),
                   scope_channel: str = Form(""), return_to: str = Form("")):
    campaign.status = CampaignStatus.active
    db.commit()
    video_worker.hydrate_buffers(db)  # queue the first episodes immediately
    dest = _campaign_return(return_to)  # started from the hub → stay on the hub and watch it render
    if dest is not None:
        return RedirectResponse(dest, status_code=303)
    return _campaigns_redirect(scope_channel.strip() or None)


@app.post("/campaigns/{campaign_id}/delete")
def delete_campaign(campaign=Depends(get_owned_campaign), db=Depends(get_db),
                    scope_channel: str = Form("")):
    db.delete(campaign)
    db.commit()
    return _campaigns_redirect(scope_channel.strip() or None)


# ── Cloud Credentials ────────────────────────────────────────────────────────
@app.get("/credentials", response_class=HTMLResponse)
def credentials_page(request: Request, user: CurrentUser):
    return templates.TemplateResponse(
        request, "credentials.html", {"request": request, "user": user, "nav": "credentials"}
    )


@app.get("/credentials/gemini-models")
def gemini_models(user: CurrentUser):
    """Live Gemini model list (one un-metered REST call with the user's key), annotated with the
    curated free-tier RPM/TPM/RPD table — so the model chain is chosen with real information in
    the UI instead of by editing .env blind."""
    from core import ai_engine

    key = user.gemini_api_key or settings.GEMINI_API_KEY
    if not key:
        return JSONResponse({"error": "Add a Gemini API key first (save it above, then retry)."},
                            status_code=400)
    try:
        live = ai_engine.list_gemini_models(api_key=key)
    except Exception as exc:  # noqa: BLE001 — the error text can embed ?key=…; never expose it
        logger.warning("Gemini model listing failed: %s", type(exc).__name__)
        return JSONResponse({"error": "Could not list models — check the key/network and retry."},
                            status_code=502)
    rows = []
    for m in live:
        limits = ai_engine.GEMINI_MODEL_CATALOG.get(m["id"], {})
        rows.append({**m, "rpm": limits.get("rpm"), "tpm": limits.get("tpm"),
                     "rpd": limits.get("rpd"), "note": limits.get("note")})
    # Models with known quota numbers first (they're the sensible picks), then alphabetical.
    rows.sort(key=lambda r: (r["rpd"] is None, r["id"]))
    return {"models": rows, "limits_as_of": ai_engine.CATALOG_AS_OF,
            "current": user.gemini_model or "", "server_default": settings.GEMINI_MODEL}


@app.post("/credentials/test/{provider}")
def test_credential(provider: str, user: CurrentUser):
    """One cheap live call to verify a saved key (PRD: 'save and verify')."""
    from services import verification

    if provider == "gemini":
        key = user.gemini_api_key or settings.GEMINI_API_KEY
        ok, detail = verification.verify_gemini(key) if key else (False, "No Gemini key saved.")
    elif provider == "pexels":
        key = user.pexels_api_key or settings.PEXELS_API_KEY
        ok, detail = verification.verify_pexels(key) if key else (False, "No Pexels key saved.")
    elif provider == "telegram":
        token = user.telegram_token or settings.TELEGRAM_BOT_TOKEN
        chat = user.telegram_chat_id or settings.TELEGRAM_CHAT_ID
        ok, detail = verification.verify_telegram(token, chat) if token else (False, "No Telegram token saved.")
    elif provider == "freesound":
        key = settings.FREESOUND_API_KEY  # server-wide (.env) — powers Auto background music
        ok, detail = verification.verify_freesound(key) if key else \
            (False, "FREESOUND_API_KEY is not set in .env — Auto background music can't run.")
    elif provider == "pollinations":
        # Keyless is valid — this tests reachability (+ the token if one is saved). Studio image fallback.
        token = user.pollinations_token or settings.POLLINATIONS_TOKEN
        ok, detail = verification.verify_pollinations(token)
    else:
        raise HTTPException(404, "Unknown provider")
    return {"ok": ok, "detail": detail}


@app.post("/credentials")
def save_credentials(
    user: CurrentUser,
    db: DbDep,
    gemini_api_key: str = Form(""),
    pexels_api_key: str = Form(""),
    telegram_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    pollinations_token: str = Form(""),
    gemini_model: str | None = Form(None),
    gemini_image_model: str | None = Form(None),
):
    # Only overwrite fields that were provided (blank keeps the existing stored value).
    if gemini_api_key:
        user.gemini_api_key = gemini_api_key
    if pexels_api_key:
        user.pexels_api_key = pexels_api_key
    if telegram_token:
        user.telegram_token = telegram_token
    if telegram_chat_id:
        user.telegram_chat_id = telegram_chat_id
    if pollinations_token:
        user.pollinations_token = pollinations_token
    # Model chain is NOT a secret and has its own form: when the field is present, the submitted
    # value replaces the stored one — an EMPTY submission means "back to the server default".
    if gemini_model is not None:
        cleaned = ",".join(m.strip() for m in gemini_model.split(",") if m.strip())[:200]
        user.gemini_model = cleaned or None
    # The image model is managed separately from the text model (own field, own quota).
    if gemini_image_model is not None:
        cleaned = ",".join(m.strip() for m in gemini_image_model.split(",") if m.strip())[:200]
        user.gemini_image_model = cleaned or None
    db.add(user)
    db.commit()
    return RedirectResponse("/credentials", status_code=303)


# ── Settings (per-user preferences — NOT secrets; keys live on Credentials) ──
_SETTINGS_LANGS = ("vi", "en", "es")


def _new_campaign_defaults(user) -> dict:
    """Seed a fresh New-Campaign form from the user's saved defaults (Settings page). Returns a
    cfg-shaped dict the template already reads via `cfg.get(...)`, so no form surgery is needed.
    AI Propose still overrides anything the operator asks it to design."""
    s = user.settings_json or {}
    cfg: dict = {}
    if s.get("language") in _SETTINGS_LANGS:
        cfg["language"] = s["language"]
    if s.get("video_format") in ("short", "long"):
        cfg["video_format"] = s["video_format"]
    if s.get("publish_mode") in ("auto", "review"):
        cfg["auto_publish"] = s["publish_mode"] != "review"
    if isinstance(s.get("posting_slots"), list) and s["posting_slots"]:
        cfg["posting_slots"] = s["posting_slots"]
    return cfg


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: CurrentUser):
    """Per-user preferences: the defaults a new campaign starts from + the AI daily budget shown on
    the dashboard quota meter. Credentials (secrets) stay on their own page."""
    return templates.TemplateResponse(
        request, "settings.html",
        {"request": request, "user": user, "nav": "settings", "s": user.settings_json or {}},
    )


@app.post("/settings")
def save_settings(user: CurrentUser, db: DbDep, language: str = Form(""),
                  video_format: str = Form(""), publish_mode: str = Form(""),
                  posting_slots: str = Form(""), total_episodes: str = Form(""),
                  ai_daily_budget: str = Form(""), image_timeout_s: str = Form("")):
    """Save the whole preferences form (a blank field clears that default — the form always submits
    every field). Values are whitelisted/validated exactly like the campaign form does."""

    s: dict = {}
    # Per-attempt image-vendor wait for Studio renders (ADR-069). Clamped: below 30s even a healthy
    # vendor gets cut off mid-draw; above 600s a single scene could eat the whole per-episode image
    # budget in one attempt.
    if image_timeout_s.strip().isdigit():
        s["image_timeout_s"] = min(600, max(30, int(image_timeout_s)))
    if language in _SETTINGS_LANGS:
        s["language"] = language
    if video_format in ("short", "long"):
        s["video_format"] = video_format
    if publish_mode in ("auto", "review"):
        s["publish_mode"] = publish_mode
    slots = [x.strip() for x in posting_slots.split(",")
             if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", x.strip())]
    if slots:
        s["posting_slots"] = slots
    if total_episodes.strip().isdigit() and int(total_episodes) > 0:
        s["total_episodes"] = int(total_episodes)
    if ai_daily_budget.strip().isdigit() and int(ai_daily_budget) > 0:
        s["ai_daily_budget"] = int(ai_daily_budget)
    user.settings_json = s or None
    db.add(user)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


# ── Asset Pool Cache (+ preview & review) ────────────────────────────────────
_ASSET_STATUS_FILTERS = ("awaiting_review", "ready", "consumed")  # chip filters; others fall under "All"
_ASSETS_PER_PAGE = 24


@app.get("/assets", response_class=HTMLResponse)
def assets_page(request: Request, user: CurrentUser, db: DbDep,
                flash: str = "", flash_reason: str = "",
                campaign: int | None = None, channel: int | None = None,
                status: str = "", page: int = 1, q: str = ""):
    q = q.strip()
    # Scope conditions (channel/campaign drill-down) — chip counts are computed over THIS, without the
    # search, so the chips read as "how many exist here" (consistent with Campaigns/Channels). Search +
    # status narrow only the visible items and the paging count.
    scope_conds = [Campaign.user_id == user.id]
    if campaign:
        scope_conds.append(BufferPoolItem.campaign_id == campaign)
    if channel:
        scope_conds.append(BufferPoolItem.channel_id == channel)

    def joined(stmt, conds):
        return stmt.select_from(BufferPoolItem).join(
            Campaign, BufferPoolItem.campaign_id == Campaign.id).where(*conds)

    status_counts = {s.value: n for s, n in db.execute(
        joined(select(BufferPoolItem.status, func.count()), scope_conds)
        .group_by(BufferPoolItem.status)).all()}
    pool_total = sum(status_counts.values())  # is there anything in this scope at all?
    total_all = pool_total                    # chip "All" count (scope, search-independent)

    status = status if status in _ASSET_STATUS_FILTERS else ""
    # Filtered conditions drive the visible items + the paging count (status + search applied).
    item_conds = list(scope_conds)
    if status:
        item_conds.append(BufferPoolItem.status == BufferStatus(status))
    if q:
        item_conds.append(or_(Campaign.topic_name.ilike(f"%{q}%"),
                              cast(BufferPoolItem.episode_number, String).ilike(f"%{q}%")))
    total = db.scalar(joined(select(func.count()), item_conds)) or 0
    pages = max(1, -(-total // _ASSETS_PER_PAGE))  # ceil-divide
    page = min(max(page, 1), pages)
    items = db.scalars(joined(select(BufferPoolItem), item_conds)
                       .order_by(BufferPoolItem.id.desc())
                       .limit(_ASSETS_PER_PAGE).offset((page - 1) * _ASSETS_PER_PAGE)).all()

    campaigns = db.scalars(select(Campaign).where(Campaign.user_id == user.id)).all()
    chan_by_id = {c.id: c for c in db.scalars(select(Channel).where(Channel.user_id == user.id)).all()}
    camp_by_id = {c.id: c for c in campaigns}
    # Only items that can still have a file on disk are worth stat-ing (skip consumed/rejected/expired).
    previewable = {i.id for i in items
                   if i.status in (BufferStatus.ready, BufferStatus.awaiting_review)
                   and i.video_path and os.path.exists(i.video_path)}
    # Map each shown item to its episode (Task) so a card can link to the episode's single home.
    task_by_ep: dict = {}
    if items:
        for t in db.scalars(select(Task).where(
                Task.user_id == user.id,
                Task.campaign_id.in_({i.campaign_id for i in items}),
                Task.episode_number.in_({i.episode_number for i in items}))):
            task_by_ep[(t.campaign_id, t.episode_number)] = t.id
    scope_qs = (f"campaign={campaign}&" if campaign else (f"channel={channel}&" if channel else ""))
    if q:  # keep the search term on pager links too
        from urllib.parse import quote

        scope_qs += f"q={quote(q)}&"
    scope_hidden = {"campaign": campaign} if campaign else ({"channel": channel} if channel else {})
    chips = [{"label": "All", "value": "", "count": total_all},
             {"label": "Awaiting review", "value": "awaiting_review",
              "count": status_counts.get("awaiting_review", 0)},
             {"label": "Ready", "value": "ready", "count": status_counts.get("ready", 0)},
             {"label": "Published", "value": "consumed", "count": status_counts.get("consumed", 0)}]
    return templates.TemplateResponse(
        request, "assets.html",
        {"request": request, "user": user, "items": items, "nav": "assets",
         "camp_by_id": camp_by_id, "chan_by_id": chan_by_id, "previewable": previewable,
         "task_by_ep": task_by_ep,
         "scope_campaign": camp_by_id.get(campaign) if campaign else None,
         "scope_channel": chan_by_id.get(channel) if channel else None,
         "status": status, "status_counts": status_counts, "total_all": total_all,
         "pool_total": pool_total, "chips": chips, "q": q, "scope_hidden": scope_hidden,
         "page": page, "pages": pages, "scope_qs": scope_qs,
         "stage_counts": _episode_stage_counts(db, user, campaign=campaign, channel=channel),
         # Post-action feedback (whitelisted — never echo arbitrary input back into the page).
         "flash": flash if flash in ("publish", "rerender", "rejected", "missing") else "",
         "flash_reason": flash_reason[:200]},
    )


def _iter_file(path: str, start: int, end: int) -> Iterator[bytes]:
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(1 << 16, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _ranged_file_response(path: str, request: Request, media_type: str) -> StreamingResponse:
    """Minimal single-range streaming (RFC 7233) so the <video> preview can scrub."""
    file_size = os.path.getsize(path)
    range_header = request.headers.get("range", "")
    start, end = 0, file_size - 1
    status_code = 200
    if range_header.startswith("bytes="):
        raw_start, _, raw_end = range_header[6:].partition("-")
        try:
            if not raw_start:
                # Suffix range "bytes=-N" → the last N bytes.
                start = max(0, file_size - int(raw_end)) if raw_end else 0
                end = file_size - 1
            else:
                start = int(raw_start)
                end = int(raw_end) if raw_end else file_size - 1
            end = min(end, file_size - 1)
            # Unsatisfiable (past EOF or reversed) → 416, not a broken 206 with negative length.
            if start > end or start >= file_size:
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{file_size}", "Accept-Ranges": "bytes"},
                )
            status_code = 206
        except ValueError:
            start, end = 0, file_size - 1
    end = min(end, file_size - 1)
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    return StreamingResponse(
        _iter_file(path, start, end), status_code=status_code, media_type=media_type, headers=headers
    )


@app.get("/assets/{item_id}/video")
def asset_video(request: Request, item=Depends(get_owned_buffer_item)):
    if not item.video_path or not os.path.exists(item.video_path):
        raise HTTPException(404, "Video file no longer on disk")
    import mimetypes

    media_type = mimetypes.guess_type(item.video_path)[0] or "video/mp4"
    return _ranged_file_response(item.video_path, request, media_type)


@app.get("/assets/{item_id}/thumb")
def asset_thumb(request: Request, item=Depends(get_owned_buffer_item)):
    if not item.thumbnail_path or not os.path.exists(item.thumbnail_path):
        raise HTTPException(404, "Thumbnail no longer on disk")
    return _ranged_file_response(item.thumbnail_path, request, "image/jpeg")


def _episode_return(return_to: str) -> str | None:
    """Return a safe internal episode path (`/episodes/<digits>`) if `return_to` is one, else None.
    Lets asset/task actions posted from the Episode view redirect back to it instead of /assets."""
    tail = (return_to or "").removeprefix("/episodes/")
    return return_to if return_to.startswith("/episodes/") and tail.isdigit() else None


def _safe_return(return_to: str) -> str | None:
    """An internal path a shared action may bounce back to, or None. Allow-list only (never an
    arbitrary `return_to`, which would be an open redirect): the Episode view, and the Operations
    page — whose Publish-queue tab reuses the same asset actions and must not dump the operator on
    /assets afterwards."""
    ep = _episode_return(return_to)
    if ep is not None:
        return ep
    if (return_to or "").split("?", 1)[0] in ("/operations", "/calendar"):
        return return_to
    return None


def _action_redirect(return_to: str, flash: str, default: str, flash_reason: str = "") -> RedirectResponse:
    """Redirect an asset/task action to the page it came from (Episode view / Operations) or the
    default /assets URL (unchanged behavior when no return path is supplied)."""
    target = _safe_return(return_to)
    if target is None:
        return RedirectResponse(default, status_code=303)
    sep = "&" if "?" in target else "?"
    qs = f"{sep}flash={flash}" if flash else ""
    if flash_reason:
        from urllib.parse import quote
        qs += ("&" if qs else sep) + f"flash_reason={quote(flash_reason)}"
    return RedirectResponse(target + qs, status_code=303)


@app.post("/assets/{item_id}/approve")
def approve_asset(db: DbDep, item=Depends(get_owned_buffer_item), return_to: str = Form("")):
    if item.status != BufferStatus.awaiting_review:
        raise HTTPException(400, "Only items awaiting review can be approved")
    if not (item.video_path and os.path.exists(item.video_path)):
        return _action_redirect(return_to, "missing", "/assets?flash=missing")
    video_worker.apply_approve(db, item)
    return _action_redirect(return_to, "publish", "/assets")


@app.post("/assets/{item_id}/publish-now")
def publish_asset_now(db: DbDep, item=Depends(get_owned_buffer_item), return_to: str = Form("")):
    """Skip the posting slot: publish a pre-rendered (`ready`) episode immediately."""
    if item.status != BufferStatus.ready:
        raise HTTPException(400, "Only pre-rendered (ready) items can be published now")
    if not (item.video_path and os.path.exists(item.video_path)):
        return _action_redirect(return_to, "missing", "/assets?flash=missing")
    task_queue.enqueue_publish(item.id)
    return _action_redirect(return_to, "publish", "/assets?flash=publish")


@app.post("/assets/{item_id}/rerender")
def rerender_asset(db: DbDep, item=Depends(get_owned_buffer_item), return_to: str = Form("")):
    """Discard a rendered episode (delete its files) and immediately queue a fresh render of the
    same episode — for when a render is wrong (bad footage, missing subtitles, …) but the episode
    itself should still exist."""
    if item.status not in (BufferStatus.ready, BufferStatus.awaiting_review):
        raise HTTPException(400, "Only ready / awaiting-review items can be re-rendered")
    for path in (item.video_path, item.thumbnail_path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            logger.warning("Could not remove %s", path)
    item.status = BufferStatus.rejected  # the re-render replaces this row on completion
    task = db.scalar(select(Task).where(
        Task.campaign_id == item.campaign_id, Task.episode_number == item.episode_number))
    if task is None:
        raise HTTPException(409, "No task row for this episode — use Task Logs")
    task.status = TaskStatus.PENDING_QUEUE
    task.error_message = None
    task.progress_pct = 0
    task.retry_count += 1
    # Discard & re-render is a REROLL: drop the resume checkpoint so the render writes a fresh
    # script (a plain Retry keeps it and rebuilds the same episode — ADR-069).
    video_worker.drop_script_checkpoint(task)
    db.commit()
    task.rq_job_id = task_queue.enqueue_render(task.id)
    db.commit()
    return _action_redirect(return_to, "rerender", "/assets?flash=rerender")


@app.post("/assets/{item_id}/reject")
def reject_asset(db: DbDep, item=Depends(get_owned_buffer_item), reason: str = Form(""),
                 return_to: str = Form("")):
    if item.status != BufferStatus.awaiting_review:
        raise HTTPException(400, "Only items awaiting review can be rejected")
    reason = reason.strip()[:200]
    # Manual reject leaves the episode FAILED for an explicit Retry (rerender=False, unchanged).
    video_worker.apply_reject(db, item, reason, rerender=False)
    ep = _episode_return(return_to)
    if ep is not None:
        return _action_redirect(return_to, "rejected", "/assets", flash_reason=reason)
    from urllib.parse import quote

    return RedirectResponse(
        "/assets?flash=rejected" + (f"&flash_reason={quote(reason)}" if reason else ""),
        status_code=303,
    )


# ── Episodes (the pipeline list — every episode by stage) + the per-episode view ──
# Lifecycle stage → the task statuses it groups. The Episodes list filters/tabs by stage; a stage is
# a friendlier bucket than the 9 raw statuses.
_STAGE_STATUSES: dict[str, tuple[str, ...]] = {
    "queued": ("PENDING_QUEUE",),
    "rendering": ("AI_GENERATION", "AUDIO_SYNCED", "RENDERING"),
    "review": ("AWAITING_REVIEW",),
    "scheduled": ("SCHEDULED",),
    "published": ("PUBLISHING", "COMPLETED"),
    "failed": ("FAILED",),
    "cancelled": ("CANCELLED",),
}
_STATUS_TO_STAGE = {st: stage for stage, sts in _STAGE_STATUSES.items() for st in sts}
_EPISODES_LIST_PER_PAGE = 25


def _review_episode_keys(db, user_id: int, *, campaign: int | None = None,
                         channel: int | None = None) -> set[tuple[int, int]]:
    """(campaign_id, episode_number) for every episode whose rendered video is waiting for approval.

    The BUFFER is the review queue — the same source the attention badge already used — so deriving
    the Review stage from it makes the chip and the badge agree by construction (ADR-065). Reading it
    from `Task.status` instead is what produced the audit's worst finding: the chip said "Review (0)"
    while two videos sat waiting, because a Retry had moved one task on while its buffer row stayed
    `awaiting_review`. A task status can drift from the queue; the queue cannot drift from itself."""
    stmt = (select(BufferPoolItem.campaign_id, BufferPoolItem.episode_number)
            .join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
            .where(Campaign.user_id == user_id,
                   BufferPoolItem.status == BufferStatus.awaiting_review))
    if campaign:
        stmt = stmt.where(BufferPoolItem.campaign_id == campaign)
    elif channel:
        stmt = stmt.where(Campaign.channel_id == channel)
    return {(cid, ep) for cid, ep in db.execute(stmt).all()}


def _apply_stage_filter(stmt, stage: str, review_keys: set[tuple[int, int]]):
    """Restrict an episode query to ONE stage, with Review taken from the buffer.

    Review membership WINS over the task status, and every other stage excludes those episodes — so
    an episode belongs to exactly one stage and can never be listed as both "Queued" and "Review"
    (the audit found one episode reading as three different stages across surfaces)."""
    from sqlalchemy import tuple_

    key = tuple_(Task.campaign_id, Task.episode_number)
    if stage == "review":
        return stmt.where(key.in_(review_keys)) if review_keys else stmt.where(false())
    stmt = stmt.where(Task.status.in_([TaskStatus(st) for st in _STAGE_STATUSES[stage]]))
    return stmt.where(key.notin_(review_keys)) if review_keys else stmt


def _stage_counts_from(raw: dict, review_keys: set, review_by_stage: dict) -> dict:
    """Fold raw {status: n} into stage counts, with Review buffer-derived and its episodes removed
    from the stage their task status would otherwise put them in (so the totals still add up)."""
    counts = {stage: sum(raw.get(st, 0) for st in sts) for stage, sts in _STAGE_STATUSES.items()}
    for stage, n in review_by_stage.items():
        counts[stage] = max(0, counts.get(stage, 0) - n)
    counts["review"] = len(review_keys)
    return counts


def _episode_stage_counts(db, user, *, campaign: int | None = None,
                          channel: int | None = None) -> dict:
    """{stage: count} (+ 'all') across the user's episodes in scope. Powers the unified stage-tab bar
    shared by /episodes, /assets and /tasks so all three show one consistent set of stage counts."""
    conds = [Task.user_id == user.id]
    if campaign:
        conds.append(Task.campaign_id == campaign)
    elif channel:
        camp_ids = list(db.scalars(select(Campaign.id).where(
            Campaign.user_id == user.id, Campaign.channel_id == channel)))
        conds.append(Task.campaign_id.in_(camp_ids or [-1]))
    raw = {s.value: n for s, n in db.execute(
        select(Task.status, func.count()).where(*conds).group_by(Task.status)).all()}
    review_keys = _review_episode_keys(db, user.id, campaign=campaign, channel=channel)
    # Which stage each review episode WOULD have landed in, so removing them keeps the totals honest.
    review_by_stage: dict[str, int] = {}
    if review_keys:
        from sqlalchemy import tuple_

        for status, n in db.execute(
                select(Task.status, func.count()).where(
                    *conds, tuple_(Task.campaign_id, Task.episode_number).in_(review_keys))
                .group_by(Task.status)).all():
            stage = _STATUS_TO_STAGE.get(status.value)
            if stage and stage != "review":
                review_by_stage[stage] = review_by_stage.get(stage, 0) + n
    counts = _stage_counts_from(raw, review_keys, review_by_stage)
    counts["all"] = sum(raw.values())
    return counts


def _episode_list_ctx(db, user, *, campaign: int | None = None, channel: int | None = None,
                      status: str = "", q: str = "", page: int = 1) -> dict:
    """Shared episode-list query + chip/pager context. Powers both the global /episodes list and the
    campaign-hub Episodes tab, so the stage grammar (tabs, counts, search, pagination) has one home."""
    scope_conds = [Task.user_id == user.id]
    if campaign:
        scope_conds.append(Task.campaign_id == campaign)
    camp_by_id = {c.id: c for c in db.scalars(select(Campaign).where(Campaign.user_id == user.id)).all()}
    chan_by_id = {c.id: c for c in db.scalars(select(Channel).where(Channel.user_id == user.id)).all()}
    if channel:  # scope by channel via the episode's campaign
        scope_conds.append(Task.campaign_id.in_(
            [cid for cid, c in camp_by_id.items() if c.channel_id == channel]))

    def joined(stmt, conds):
        return stmt.select_from(Task).join(Campaign, Task.campaign_id == Campaign.id).where(*conds)

    # Per-stage counts over the scope (search-independent, like the other chip bars).
    raw_counts = {s.value: n for s, n in db.execute(
        joined(select(Task.status, func.count()), scope_conds).group_by(Task.status)).all()}
    review_keys = _review_episode_keys(db, user.id, campaign=campaign, channel=channel)
    review_by_stage: dict[str, int] = {}
    if review_keys:
        from sqlalchemy import tuple_

        for st_val, n in db.execute(joined(
                select(Task.status, func.count()),
                scope_conds + [tuple_(Task.campaign_id, Task.episode_number).in_(review_keys)])
                .group_by(Task.status)).all():
            st_stage = _STATUS_TO_STAGE.get(st_val.value)
            if st_stage and st_stage != "review":
                review_by_stage[st_stage] = review_by_stage.get(st_stage, 0) + n
    stage_counts = _stage_counts_from(raw_counts, review_keys, review_by_stage)
    total_all = sum(raw_counts.values())

    stage = status if status in _STAGE_STATUSES else ""
    q = q.strip()
    item_conds = list(scope_conds)
    if q:
        item_conds.append(or_(Campaign.topic_name.ilike(f"%{q}%"),
                              Task.synopsis.ilike(f"%{q}%"),
                              cast(Task.episode_number, String).ilike(f"%{q}%"),
                              cast(Task.status, String).ilike(f"%{q}%")))
    def staged(stmt):
        stmt = joined(stmt, item_conds)
        return _apply_stage_filter(stmt, stage, review_keys) if stage else stmt

    total = db.scalar(staged(select(func.count()))) or 0
    pages = max(1, -(-total // _EPISODES_LIST_PER_PAGE))
    page = min(max(page, 1), pages)
    episodes = db.scalars(staged(select(Task))
                          .order_by(Task.updated_at.desc(), Task.id.desc())
                          .limit(_EPISODES_LIST_PER_PAGE)
                          .offset((page - 1) * _EPISODES_LIST_PER_PAGE)).all()

    chips = [{"label": "All", "value": "", "count": total_all}] + [
        {"label": stage.replace("_", " ").title(), "value": stage, "count": stage_counts.get(stage, 0)}
        for stage in _STAGE_STATUSES]
    return {"episodes": episodes, "camp_by_id": camp_by_id, "chan_by_id": chan_by_id,
            "status_to_stage": _STATUS_TO_STAGE, "chips": chips, "status": stage, "q": q,
            "total_all": total_all, "total": total, "page": page, "pages": pages,
            "stage_counts": {**stage_counts, "all": total_all}}


@app.get("/episodes", response_class=HTMLResponse)
def episodes_list(request: Request, user: CurrentUser, db: DbDep,
                  channel: int | None = None, campaign: int | None = None,
                  status: str = "", q: str = "", page: int = 1):
    """The pipeline: every episode as one row, grouped by lifecycle stage — the unified view that
    merges what used to be split between Task Logs (render) and Asset Pool (review). Row → the
    Episode detail page. Server-rendered + stage tabs + search + scope + pagination (one grammar)."""
    ctx = _episode_list_ctx(db, user, campaign=campaign, channel=channel, status=status, q=q, page=page)
    ctx["worker_ok"] = task_queue.worker_alive()  # the render-log warning follows the filter (ADR-065)
    scope_hidden = {"campaign": campaign} if campaign else ({"channel": channel} if channel else {})
    scope_qs = _query_string(**scope_hidden, status=ctx["status"], q=ctx["q"])
    return templates.TemplateResponse(
        request, "episodes.html",
        {"request": request, "user": user, "nav": "episodes",
         "scope_hidden": scope_hidden, "scope_qs": scope_qs,
         "scope_campaign": ctx["camp_by_id"].get(campaign) if campaign else None,
         "scope_channel": ctx["chan_by_id"].get(channel) if channel else None, **ctx},
    )


# ── Episode view (one home per episode: the whole lifecycle in one place) ────
_EPISODE_STAGES = ("Queued", "Rendering", "Review", "Scheduled", "Published")
# Task status → index in _EPISODE_STAGES (None = FAILED, shown off-track).
_STAGE_INDEX = {
    "PENDING_QUEUE": 0, "AI_GENERATION": 1, "AUDIO_SYNCED": 1, "RENDERING": 1,
    "AWAITING_REVIEW": 2, "SCHEDULED": 3, "PUBLISHING": 4, "COMPLETED": 4,
}


@app.get("/episodes/{task_id}", response_class=HTMLResponse)
def episode_view(request: Request, user: CurrentUser, db: DbDep, task_id: int,
                 flash: str = "", flash_reason: str = ""):
    """One episode's whole story in one page: lifecycle timeline, preview, stage-aware actions,
    render/QC history and (once live) published stats — so an episode has a single home instead of
    being scattered across Task Logs / Asset Pool / Calendar / Performance."""
    task = db.get(Task, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "Episode not found")
    campaign = db.get(Campaign, task.campaign_id)
    channel = db.get(Channel, campaign.channel_id) if campaign else None
    # The live buffer row for this episode (newest first), if one exists.
    buffer = db.scalar(select(BufferPoolItem).where(
        BufferPoolItem.campaign_id == task.campaign_id,
        BufferPoolItem.episode_number == task.episode_number,
    ).order_by(BufferPoolItem.id.desc()))
    previewable = bool(
        buffer and buffer.status in (BufferStatus.ready, BufferStatus.awaiting_review)
        and buffer.video_path and os.path.exists(buffer.video_path))
    stage_index = _STAGE_INDEX.get(task.status.value)
    diagnosis = _diagnose_failure(task.error_message) if task.status == TaskStatus.FAILED else None
    # Retention drop-off markers: attribute the measured curve to the scene that lost viewers.
    curve = (task.stats_json or {}).get("retention_curve")
    scenes = (task.render_json or {}).get("scenes")
    retention_drops = (
        retention.drop_points(curve, scenes) if curve and scenes else [])
    return templates.TemplateResponse(
        request, "episode.html",
        {"request": request, "user": user, "nav": "episodes", "task": task, "campaign": campaign,
         "channel": channel, "buffer": buffer, "previewable": previewable,
         "stages": _EPISODE_STAGES, "stage_index": stage_index,
         "retention_curve": curve, "retention_drops": retention_drops,
         "failed": task.status == TaskStatus.FAILED, "diagnosis": diagnosis,
         "cancelled": task.status == TaskStatus.CANCELLED,
         "flash": flash if flash in ("publish", "rerender", "rejected", "missing") else "",
         "flash_reason": flash_reason[:200]},
    )


# One failure classification for the whole app (ADR-069): the same table also tells the autopilot
# which failures a retry can actually fix, so it lives in core/failure.py — these are aliases.
_FAILURE_PATTERNS = failure.PATTERNS
_diagnose_failure = failure.diagnose


# ── Performance & learning (self-improvement transparency) ──────────────────
def ab_variant_summary(episodes) -> list[dict]:
    """Aggregate measured episodes per A/B metadata variant — the closed A/B loop: which title/
    description style actually retains viewers. Only episodes with both a recorded variant and
    fetched stats count. Returns [] until there is anything to compare."""
    groups: dict[str, list] = {}
    for t in episodes:
        if t.ab_variant and t.stats_json:
            groups.setdefault(t.ab_variant, []).append(t)
    summary = []
    for variant in sorted(groups):
        rows = groups[variant]
        retention = [r.stats_json["avg_pct_viewed"] for r in rows
                     if r.stats_json.get("avg_pct_viewed") is not None]
        views = [r.stats_json["views"] for r in rows if r.stats_json.get("views") is not None]
        summary.append({
            "variant": variant,
            "episodes": len(rows),
            "avg_retention": round(sum(retention) / len(retention), 1) if retention else None,
            "avg_views": round(sum(views) / len(views)) if views else None,
        })
    return summary


def _hub_context(db, user, campaign) -> dict:
    """Shared campaign-hub context (parent channel + buffer tally) for the hub header/tabs partial."""
    return {"channel": db.get(Channel, campaign.channel_id),
            "asset_count": _buffer_counts(db, user.id).get(
                campaign.id, {"ready": 0, "awaiting_review": 0})}


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_overview(request: Request, user: CurrentUser, db: DbDep,
                      campaign=Depends(get_owned_campaign)):
    """Campaign hub — Overview tab: what this channel has learned from its own results (playbook,
    A/B variant retention, trend, scorecard). Per-episode detail lives under the Episodes tab."""
    episodes = db.scalars(
        select(Task).where(Task.campaign_id == campaign.id).order_by(Task.episode_number)
    ).all()
    # "Measured" = retention present. Early views (near-real-time, no retention yet) carry stats_json
    # but no avg_pct_viewed, so they must NOT count toward the scorecard/best-retention (T4).
    measured = [t for t in episodes if t.stats_json and t.stats_json.get("avg_pct_viewed") is not None]
    best = max(measured, key=lambda t: t.stats_json.get("avg_pct_viewed", 0), default=None)
    hub = _hub_context(db, user, campaign)  # single channel fetch, reused for the audience line
    return templates.TemplateResponse(
        request, "performance.html",
        {"request": request, "user": user, "nav": "campaigns", "campaign": campaign,
         "episodes": episodes, "learning": campaign.learning_json or {},
         "best_id": best.id if best else None,
         "best_ret": best.stats_json.get("avg_pct_viewed") if best else None,
         "measured_count": len(measured), "variants": ab_variant_summary(episodes),
         "op": _campaign_ops(db, user.id, [campaign])[campaign.id],  # Now & next strip
         "cls": autopilot.classify_campaigns(db, [campaign]).get(campaign.id),  # performance verdict
         "autopilot_min": autopilot.MIN_MEASURED,
         "audience": autopilot.audience_summary(  # measured viewer country vs the channel target
             episodes, hub["channel"].profile_json if hub["channel"] else None),
         "hub_active": "overview", **hub},
    )


@app.get("/campaigns/{campaign_id}/episodes", response_class=HTMLResponse)
def campaign_episodes(request: Request, user: CurrentUser, db: DbDep,
                      campaign=Depends(get_owned_campaign),
                      status: str = "", q: str = "", page: int = 1):
    """Campaign hub — Episodes tab: this campaign's episodes as a stage-tabbed list (same grammar as
    the global /episodes view), scoped in-page so the hub tabs stay visible."""
    ctx = _episode_list_ctx(db, user, campaign=campaign.id, status=status, q=q, page=page)
    return templates.TemplateResponse(
        request, "campaign_episodes.html",
        {"request": request, "user": user, "nav": "campaigns", "campaign": campaign,
         "hub_active": "episodes", **_hub_context(db, user, campaign), **ctx},
    )


@app.get("/campaigns/{campaign_id}/performance")
def campaign_performance_redirect(campaign=Depends(get_owned_campaign)):
    """Legacy performance URL → the hub's Overview tab (301 permanent so it settles on the canonical
    URL instead of lingering in the Back chain)."""
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=301)


@app.post("/campaigns/{campaign_id}/learning/reset")
def reset_learning(db: DbDep, campaign=Depends(get_owned_campaign)):
    campaign.learning_json = None
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


# ── Content calendar ─────────────────────────────────────────────────────────
def _calendar_row_cells(campaign: Campaign, ready_eps: list[int], overrides=None,
                        week: int = 0, days: int = 7) -> list[dict] | None:
    """Week-planner cells: per day, per slot, what will HAPPEN — not just the time.

    The slot→episode assignment comes from the SHARED `_upcoming_slots` (ADR-067). This function used
    to re-implement it with its own day-walk and `pool.pop(0)`, which made it the only duplicated
    business rule in the codebase — and it had already drifted: the calendar's ready count disagreed
    with the campaign hub's after a reschedule.

    `overrides` are (local datetime, episode) pairs for episodes an operator moved to their own time.
    They no longer compete for a slot, so they are drawn in their own day cell marked `own` instead of
    silently vanishing from the only page whose job is "what publishes when".
    Returns per-day {gate, slots:[{t,state,ep}]} or None (non-slotted).
    """
    from datetime import timedelta

    from workers.scheduler import WEEKDAY_KEYS, local_now

    cfg = campaign.config_json or {}
    slots = sorted(cfg.get("posting_slots") or [])
    if not slots or not cfg.get("auto_publish", True):
        return None
    # ONE definition of "which episode lands in which slot": the next N free slots, lowest-numbered
    # episode first — exactly what the scheduler does and what the publish list shows.
    assigned = dict(zip(_upcoming_slots(campaign, len(ready_eps)), ready_eps))
    own_by_slot: dict = {}
    for when, ep in (overrides or []):
        own_by_slot.setdefault(when.replace(second=0, microsecond=0), ep)

    allowed = cfg.get("posting_days") or []
    now_l = local_now(cfg.get("timezone"))
    start = now_l + timedelta(days=week * 7)
    rows: list[dict] = []
    for d in range(days):
        day = start + timedelta(days=d)
        cells = []
        # An operator-set time ignores the weekday gate (it outranks the schedule — ADR-059), so its
        # chip is drawn even on a gated day.
        for when, ep in sorted(own_by_slot.items()):
            if when.date() == day.date():
                cells.append({"t": when.strftime("%H:%M"), "state": "own", "ep": ep})
        if allowed and WEEKDAY_KEYS[day.weekday()] not in allowed:
            rows.append({"gate": True, "slots": cells})
            continue
        for s in slots:
            try:
                hh, mm = (int(x) for x in s.split(":"))
            except ValueError:
                continue
            slot_dt = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if slot_dt < now_l:
                cells.append({"t": s, "state": "past", "ep": None})
            elif slot_dt in assigned:
                cells.append({"t": s, "state": "filled", "ep": assigned[slot_dt]})
            else:
                cells.append({"t": s, "state": "missed", "ep": None})
        cells.sort(key=lambda c: c["t"])
        rows.append({"gate": False, "slots": cells})
    return rows


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, user: CurrentUser, db: DbDep, week: int = 0,
                  channel: int | None = None, view: str = "grid", flash: str = ""):
    """The one answer to "what publishes when" (ADR-067): a week grid, or the same episodes as an
    actionable list (`?view=list`, which absorbed the Operations publish-queue tab)."""
    from datetime import timedelta

    from workers.scheduler import local_now

    week = max(-8, min(week, 12))  # bound the navigation to a sane range
    camp_q = select(Campaign).where(
        Campaign.user_id == user.id, Campaign.status == CampaignStatus.active)
    if channel:  # scope to one channel (from the workspace scope switcher)
        camp_q = camp_q.where(Campaign.channel_id == channel)
    campaigns = db.scalars(camp_q).all()
    chan_by_id = {c.id: c for c in db.scalars(select(Channel).where(Channel.user_id == user.id)).all()}
    # Ready buffer episode numbers per campaign (lowest first) — the pool the planner assigns to slots.
    ready_by_camp: dict[int, list[int]] = {}
    for cid, epn in db.execute(
            select(BufferPoolItem.campaign_id, BufferPoolItem.episode_number)
            .where(BufferPoolItem.status == BufferStatus.ready,
                   # An episode with an operator-set publish time no longer competes for a slot
                   # (ADR-059), so it must not be projected into one here either.
                   BufferPoolItem.publish_at.is_(None))
            .order_by(BufferPoolItem.episode_number)).all():
        ready_by_camp.setdefault(cid, []).append(epn)
    # Episodes an operator moved to their own time: they no longer compete for a slot, but they ARE
    # still going out, so the grid draws them and the runway counts them (ADR-067).
    override_by_camp: dict[int, list] = {}
    for cid, epn, at in db.execute(
            select(BufferPoolItem.campaign_id, BufferPoolItem.episode_number,
                   BufferPoolItem.publish_at)
            .join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
            .where(Campaign.user_id == user.id, BufferPoolItem.status == BufferStatus.ready,
                   BufferPoolItem.publish_at.isnot(None))
            .order_by(BufferPoolItem.publish_at)).all():
        campaign_obj = next((c for c in campaigns if c.id == cid), None)
        if campaign_obj is not None:
            override_by_camp.setdefault(cid, []).append((_to_campaign_tz(at, campaign_obj), epn))
    slotted, unslotted = [], []
    for c in campaigns:
        overrides = override_by_camp.get(c.id, [])
        cells = _calendar_row_cells(c, ready_by_camp.get(c.id, []), overrides, week=week)
        entry = {"campaign": c,
                 # Everything rendered and waiting — slot-bound AND own-time. Counting only the
                 # slot-bound ones made the calendar disagree with the hub after a reschedule.
                 "ready": len(ready_by_camp.get(c.id, [])) + len(overrides),
                 "own_time": len(overrides),
                 "channel": chan_by_id.get(c.channel_id),
                 "fmt": (c.config_json or {}).get("video_format", "short"),
                 "tz": (c.config_json or {}).get("timezone") or settings.TIMEZONE,
                 "mode": "review" if not (c.config_json or {}).get("auto_publish", True) else "continuous"}
        if cells is not None:
            entry["cells"] = cells
            slotted.append(entry)
        else:
            unslotted.append(entry)
    base = local_now() + timedelta(days=week * 7)
    # Header cells carry a `today` flag so the current day's column is highlighted (only week 0, d 0).
    day_headers = [{"label": (base + timedelta(days=d)).strftime("%a %d/%m"),
                    "today": week == 0 and d == 0} for d in range(7)]
    label = "This week" if week == 0 else (f"In {week} week{'s' if week != 1 else ''}" if week > 0
                                           else f"{-week} week{'s' if week != -1 else ''} ago")
    return templates.TemplateResponse(
        request, "calendar.html",
        {"request": request, "user": user, "nav": "calendar", "slotted": slotted,
         "unslotted": unslotted, "day_headers": day_headers,
         "week": week, "week_label": label, "scope_cid": channel,
         "view": "list" if view == "list" else "grid", "flash": flash,
         # Same rows the Operations publish tab used to show — one implementation, one place.
         "publish_rows": _ops_publish_rows(db, user.id) if view == "list" else None,
         "scope_channel": db.get(Channel, channel) if channel else None},
    )


# ── Real-Time Task Logs ──────────────────────────────────────────────────────
@app.get("/tasks")
def tasks_redirect(campaign: int | None = None, channel: int | None = None):
    """Gone: the render log is now the Rendering filter of the one episode list (ADR-065).

    It used to be a second table over the same episodes — its own layout, its own search grammar and
    its own status words ("Pending Queue" where the rest of the app said "Queued") — reached from
    chips that looked like filters. A permanent redirect keeps every old link and bookmark working.
    `/api/tasks` is unchanged: it is still the data source, now for the live rows inside that list."""
    scope = _query_string(status="rendering", campaign=campaign, channel=channel)
    return RedirectResponse("/episodes?" + scope, status_code=301)


_TASKS_PER_PAGE = 25


@app.get("/api/tasks")
def api_tasks(user: CurrentUser, db: DbDep, page: int = 1, q: str = "",
              campaign: int | None = None, channel: int | None = None, live: bool = False):
    # Full task history, newest first, paginated — page 1 carries the live/active jobs. Scope
    # (?campaign / ?channel) and search (?q) run in SQL so they span ALL history, not just the page.
    #
    # `live=1` narrows to episodes still in a working stage (ADR-065). The episode table polls that:
    # a rendering episode can sit behind hundreds of published ones, so paging the history to find it
    # was both wasteful and unreliable.
    base = select(Task).where(Task.user_id == user.id)
    if live:
        base = base.where(Task.status.in_(_WORKING_STATUSES))
    if campaign:
        base = base.where(Task.campaign_id == campaign)
    if channel:  # scope by the episode's campaign's channel
        base = base.where(Task.campaign_id.in_(
            select(Campaign.id).where(Campaign.user_id == user.id, Campaign.channel_id == channel)))
    q = q.strip()
    if q:
        like = f"%{q}%"
        base = (base.outerjoin(Campaign, Task.campaign_id == Campaign.id)
                    .outerjoin(Channel, Campaign.channel_id == Channel.id)
                    .where(or_(cast(Task.id, String).ilike(like),
                               cast(Task.status, String).ilike(like),
                               Campaign.topic_name.ilike(like),
                               Channel.channel_name.ilike(like))))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    pages = max(1, -(-total // _TASKS_PER_PAGE))  # ceil-divide
    page = min(max(page, 1), pages)
    rows = db.scalars(base.order_by(Task.id.desc())
                      .limit(_TASKS_PER_PAGE).offset((page - 1) * _TASKS_PER_PAGE)).all()
    campaigns = {c.id: c for c in db.scalars(
        select(Campaign).where(Campaign.user_id == user.id)).all()}
    channels = {c.id: c for c in db.scalars(
        select(Channel).where(Channel.user_id == user.id)).all()}
    # Statuses where a job is ACTIVELY working — the only ones whose live Redis % is meaningful. A
    # queued/parked/terminal task uses its durable column, so a re-queued task never shows ghost
    # progress left in Redis by a crashed prior attempt (that skipped clear_progress).
    rendering = ("AI_GENERATION", "AUDIO_SYNCED", "RENDERING", "PUBLISHING")
    out = []
    for t in rows:
        working = t.status.value in rendering
        live = task_queue.get_progress(t.id) if working else t.progress_pct
        campaign = campaigns.get(t.campaign_id)
        channel = channels.get(campaign.channel_id) if campaign else None
        # Duration = RENDER time, not the wait-for-slot. `finished_at` is later overwritten with the
        # publish time for slot-scheduled / review episodes, so a 2-min render that publishes 14h
        # later would otherwise read "886m". Prefer the stored render_seconds; live-elapse while
        # working; fall back to started→finished only for legacy rows without render_seconds.
        render_seconds = (t.render_json or {}).get("render_seconds")
        if working and t.started_at:
            duration_s = max(0, int((datetime.utcnow() - t.started_at).total_seconds()))
        elif render_seconds is not None:
            duration_s = max(0, int(render_seconds))
        elif t.started_at:
            end = t.finished_at or datetime.utcnow()
            duration_s = max(0, int((end - t.started_at).total_seconds()))
        else:
            duration_s = None
        out.append({
            "id": t.id, "campaign_id": t.campaign_id, "episode": t.episode_number,
            "topic": campaign.topic_name if campaign else f"C{t.campaign_id}",
            "channel": channel.channel_name if channel else "—",
            "platform": channel.platform.value if channel else None,
            "status": t.status.value, "progress": round(live or t.progress_pct, 1),
            "error": t.error_message, "published_url": t.published_url,
            "duration_s": duration_s, "retry_count": t.retry_count,
            "can_retry": t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED),
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        })
    return {"tasks": out, "page": page, "pages": pages, "total": total}


@app.get("/api/summary")
def api_summary(user: CurrentUser, db: DbDep):
    """Read-only live snapshot for the header attention badge + dashboard auto-refresh. Reuses the
    same helpers the dashboard renders from, so polled values never diverge from a full reload."""
    channels = db.scalar(
        select(func.count()).select_from(Channel).where(Channel.user_id == user.id)) or 0
    active = db.scalar(
        select(func.count()).select_from(Campaign).where(
            Campaign.user_id == user.id, Campaign.status == CampaignStatus.active)) or 0
    counts = _task_counts(db, user.id)
    return {"health": _system_health(db, user), "counts": counts,
            "channels": channels, "active_campaigns": active,
            "autopilot_proposed": _autopilot_proposed_count(db, user.id),
            # Every badge renders THIS number — see `_attention_count` (ADR-064).
            "attention": _attention_count(db, user.id, counts)}


def _fold(s: str) -> str:
    """Lowercase and strip Vietnamese diacritics for accent-insensitive matching (ADR-068).

    Titles here are routinely Vietnamese ("Lịch sử Việt Nam") and are routinely *typed* without
    diacritics, because that is how people type on a phone. A plain `ilike` matched neither direction,
    so the palette looked broken on exactly the content this box was built for. `đ` has no combining
    form, so it is mapped by hand."""
    import unicodedata

    flat = unicodedata.normalize("NFD", (s or "").lower().replace("đ", "d"))
    return "".join(c for c in flat if not unicodedata.combining(c))


# "ep 3", "Ep.3", "episode 3", "tập 3" — how an operator actually refers to an episode. The bare
# number was the only thing that used to work.
_EP_QUERY = re.compile(r"^(?:ep|eps|episode|tap)\s*[.#·:-]?\s*(\d{1,6})$")


@app.get("/api/search")
def api_search(user: CurrentUser, db: DbDep, q: str = ""):
    """One read-only search across the whole workspace (channels, campaigns, episodes) for the ⌘K
    palette — so 'find that thing' is one box, not 'which page do I search on?'. Tenant-scoped.

    Names are matched accent-insensitively (`_fold`) in Python rather than by SQL `ilike`: SQLite has
    no unaccent, and a solo box has tens of channels/campaigns, not thousands. Episodes stay in SQL —
    that table does grow — and are reachable by number ("ep 3") or by synopsis text."""
    q = q.strip()
    if len(q) < 2:
        return {"results": []}
    needle = _fold(q)
    like = f"%{q}%"
    results: list[dict] = []
    for c in db.scalars(select(Channel).where(Channel.user_id == user.id)):
        if needle in _fold(c.channel_name):
            results.append({"type": "Channel", "label": c.channel_name,
                            "sub": c.platform.value, "href": f"/campaigns?channel={c.id}"})
        if len(results) >= 5:
            break
    campaigns = list(db.scalars(select(Campaign).where(Campaign.user_id == user.id)
                                .order_by(Campaign.id.desc())))
    camp_names = {c.id: c.topic_name for c in campaigns}
    hits = 0
    for c in campaigns:
        if needle in _fold(c.topic_name):
            results.append({"type": "Campaign", "label": c.topic_name,
                            "sub": c.status.value, "href": f"/campaigns/{c.id}"})
            hits += 1
            if hits >= 6:
                break
    ep_match = _EP_QUERY.match(needle)
    ep_no = int(ep_match.group(1)) if ep_match else None
    where = [Task.synopsis.ilike(like)]
    if ep_no is not None:
        where.append(Task.episode_number == ep_no)
    else:
        where.append(cast(Task.episode_number, String).ilike(like))
    for t in db.scalars(select(Task).where(Task.user_id == user.id, or_(*where))
                        .order_by(Task.id.desc()).limit(8)):
        topic = camp_names.get(t.campaign_id, f"C{t.campaign_id}")
        label = f"Ep {t.episode_number} · {topic}"
        results.append({"type": "Episode", "label": label,
                        "sub": (t.synopsis or t.status.value)[:60], "href": f"/episodes/{t.id}"})
    return {"results": results}


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: int, user: CurrentUser, db: DbDep, return_to: str = Form("")):
    """Retry a failed episode. If the rendered file still exists (e.g. the upload failed or the
    item was awaiting review), only the publish step is retried — no re-render.

    Returns JSON for the Task Logs poller (fetch, no `return_to`); a form POST from the Episode view
    passes `return_to` and gets a 303 back to that page instead."""
    task = db.get(Task, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "Task not found")
    # CANCELLED is retryable too: cancelling is a pause the operator can undo (ADR-064).
    if task.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise HTTPException(400, "Only failed or cancelled episodes can be retried")
    task.error_message = None
    task.retry_count += 1
    task.progress_pct = 0
    task.status = TaskStatus.PENDING_QUEUE
    task_queue.clear_progress(task.id)  # drop any ghost % from a crashed prior attempt (F1)
    buf = db.scalar(select(BufferPoolItem).where(
        BufferPoolItem.campaign_id == task.campaign_id,
        BufferPoolItem.episode_number == task.episode_number,
        BufferPoolItem.status.in_([BufferStatus.ready, BufferStatus.awaiting_review]),
    ))
    if buf is not None and buf.video_path and os.path.exists(buf.video_path):
        db.commit()
        task_queue.enqueue_publish(buf.id)
        # File still on disk → only the publish is retried (no re-render): say so.
        return _action_redirect(return_to, "publish", "") if _episode_return(return_to) \
            else {"ok": True, "mode": "publish"}
    db.commit()
    task.rq_job_id = task_queue.enqueue_render(task.id)
    db.commit()
    return _action_redirect(return_to, "rerender", "") if _episode_return(return_to) \
        else {"ok": True, "mode": "render"}


# ── Operations: the factory floor (render queue · worker · publish queue) ─────
# The one place that answers "what is the machine doing, and why is nothing moving?" — and lets the
# operator intervene from the browser instead of SSHing into the box (ADR-058). Everything here
# reads live queue/worker state, so every lookup fails soft: a dead Redis renders an empty,
# explained page rather than a 500.
_OPS_TABS = ("queue", "worker")


def _ops_job_rows(db, user_id: int) -> tuple[list[dict], int]:
    """Queued jobs (true queue order) joined to their episode, scoped to this user. Returns the
    render rows and how many publish jobs are queued — a render waiting behind uploads is a real
    thing an operator needs to see, so it is reported rather than hidden."""
    jobs = task_queue.queued_jobs()
    render_ids = [j["arg"] for j in jobs if j["kind"] == "render" and j["arg"] is not None]
    tasks = {t.id: t for t in db.scalars(
        select(Task).where(Task.user_id == user_id, Task.id.in_(render_ids or [-1]))).all()}
    campaigns = {c.id: c for c in db.scalars(
        select(Campaign).where(Campaign.user_id == user_id)).all()}
    channels = {c.id: c for c in db.scalars(
        select(Channel).where(Channel.user_id == user_id)).all()}
    rows, publish_queued = [], 0
    for job in jobs:
        if job["kind"] == "publish":
            publish_queued += 1
            continue
        task = tasks.get(job["arg"])
        if task is None:  # another tenant's job (or a vanished task) — never leak it
            continue
        campaign = campaigns.get(task.campaign_id)
        rows.append({
            "position": job["position"], "job_id": job["job_id"], "task": task,
            "campaign": campaign,
            "channel": channels.get(campaign.channel_id) if campaign else None,
            "enqueued_at": job["enqueued_at"],
        })
    return rows, publish_queued


def _ops_worker_card(db, user_id: int) -> dict:
    """The single worker's state, plus whatever it is rendering right now. `state` is the operator-
    facing verdict: down (nothing registered) · stalled (progress past the watchdog limit) ·
    busy · idle."""
    snap = task_queue.worker_snapshot()
    stalled = task_queue.stalled_render()
    live = []
    for raw_id in sorted(task_queue.active_render_task_ids()):
        try:
            task = db.get(Task, int(raw_id))
        except (TypeError, ValueError):
            continue
        if task is None or task.user_id != user_id:
            continue
        campaign = db.get(Campaign, task.campaign_id)
        age = task_queue.progress_age_seconds(task.id)
        live.append({"task": task, "campaign": campaign,
                     "pct": round(task_queue.get_progress(task.id), 1),
                     "idle_min": int(age // 60) if age is not None else None})
    # Liveness comes from the same helper the dashboard health strip uses (one definition of "the
    # worker is up"); the snapshot only adds detail and may be unavailable on a partial registration.
    if not task_queue.worker_alive():
        state = "down"
    elif stalled is not None:
        state = "stalled"
    elif live:
        state = "busy"
    else:
        state = "idle"
    return {"snapshot": snap, "state": state, "live": live,
            "lock_held": task_queue.render_lock_held(),
            "stall_limit_min": task_queue.stall_limit_seconds() // 60,
            "stalled_min": int(stalled[1] // 60) if stalled else None,
            "restart_pending": task_queue.restart_requested(),
            "queue_depth": len(task_queue.queued_jobs())}


def _ops_publish_rows(db, user_id: int) -> list[dict]:
    """Rendered episodes waiting to go out, with WHEN each will publish.

    Three states an operator must be able to tell apart: `override` (a time they set — ADR-059),
    `slot` (projected onto the campaign's next free slots, lowest episode first, exactly the rule the
    scheduler follows), and `review` (waiting for approval, so no time exists yet). `missing` flags a
    row whose file left the disk, because Publish now would fail on it."""
    items = db.scalars(
        select(BufferPoolItem)
        .join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        .where(Campaign.user_id == user_id,
               BufferPoolItem.status.in_([BufferStatus.ready, BufferStatus.awaiting_review]))
        .order_by(BufferPoolItem.campaign_id, BufferPoolItem.episode_number)).all()
    if not items:
        return []
    campaigns = {c.id: c for c in db.scalars(
        select(Campaign).where(Campaign.user_id == user_id)).all()}
    channels = {c.id: c for c in db.scalars(
        select(Channel).where(Channel.user_id == user_id)).all()}
    # Project slot times per campaign: only un-overridden ready items queue for slots, so the Nth
    # such episode lands in the Nth upcoming slot.
    slot_queue: dict[int, list] = {}
    for cid, campaign in campaigns.items():
        waiting = [i for i in items if i.campaign_id == cid
                   and i.status == BufferStatus.ready and i.publish_at is None]
        if waiting:
            slot_queue[cid] = _upcoming_slots(campaign, len(waiting))
    slot_cursor: dict[int, int] = {}
    rows = []
    for item in items:
        campaign = campaigns.get(item.campaign_id)
        when, state = None, "review"
        if item.status == BufferStatus.awaiting_review:
            state = "review"
        elif item.publish_at is not None:
            # Stored naive UTC → the campaign's own clock, so every row below is comparable.
            when, state = _to_campaign_tz(item.publish_at, campaign), "override"
        else:
            taken = slot_cursor.get(item.campaign_id, 0)
            upcoming = slot_queue.get(item.campaign_id) or []
            if taken < len(upcoming):
                when = upcoming[taken]
            slot_cursor[item.campaign_id] = taken + 1
            # No slots configured → continuous mode already published at render time, so anything
            # still ready here is simply waiting for the next tick.
            state = "slot" if when is not None else "now"
        rows.append({
            "item": item, "campaign": campaign,
            "channel": channels.get(item.channel_id),
            "when": when, "state": state,
            # Both rendered in the campaign's timezone: a human label, and the value a browser
            # `datetime-local` field expects when the operator opens the reschedule form.
            "when_label": when.strftime("%a %d/%m %H:%M") if when else None,
            "input_value": when.strftime("%Y-%m-%dT%H:%M") if when else "",
            "tz": _campaign_tz_name(campaign),
            "missing": not (item.video_path and os.path.exists(item.video_path)),
        })
    return rows


@app.get("/operations", response_class=HTMLResponse)
def operations_page(request: Request, user: CurrentUser, db: DbDep, tab: str = "queue",
                    flash: str = ""):
    # The publish queue moved to the Calendar's list view (ADR-067): two pages answered "what
    # publishes when" and disagreed. Old links keep working.
    if tab == "publish":
        return RedirectResponse("/calendar?view=list", status_code=301)
    tab = tab if tab in _OPS_TABS else "queue"
    ctx = {"request": request, "user": user, "nav": "operations", "tab": tab, "flash": flash,
           "worker": _ops_worker_card(db, user.id)}
    if tab == "queue":
        ctx["job_rows"], ctx["publish_queued"] = _ops_job_rows(db, user.id)
    return templates.TemplateResponse(request, "operations.html", ctx)


def _calendar_redirect(flash: str = "") -> RedirectResponse:
    """Publish-time actions land back on the one scheduling surface (ADR-067)."""
    return RedirectResponse("/calendar?view=list" + (f"&flash={flash}" if flash else ""),
                            status_code=303)


def _ops_redirect(tab: str, flash: str = "") -> RedirectResponse:
    qs = f"?tab={tab}" + (f"&flash={flash}" if flash else "")
    return RedirectResponse("/operations" + qs, status_code=303)


def _owned_queued_task(db, user_id: int, job_id: str) -> Task:
    """The Task behind a queued render job, or 404. Tenancy is checked through the Task row, so one
    operator can never reorder or cancel another's queue."""
    for job in task_queue.queued_jobs():
        if job["job_id"] != job_id:
            continue
        if job["kind"] != "render":
            raise HTTPException(400, "Only queued renders can be reordered or cancelled")
        task = db.get(Task, job["arg"]) if job["arg"] is not None else None
        if task is None or task.user_id != user_id:
            raise HTTPException(404, "Job not found")
        return task
    raise HTTPException(404, "That job is no longer queued")


@app.post("/operations/jobs/{job_id}/front")
def ops_move_job_front(job_id: str, user: CurrentUser, db: DbDep):
    """Render this episode next — for when a posting slot is close and the buffer is empty."""
    _owned_queued_task(db, user.id, job_id)
    return _ops_redirect("queue", "prioritised" if task_queue.move_job_to_front(job_id) else "gone")


@app.post("/operations/jobs/{job_id}/cancel")
def ops_cancel_job(job_id: str, user: CurrentUser, db: DbDep):
    """Drop a queued render. The Task becomes CANCELLED — not deleted, and deliberately NOT failed
    (ADR-064): a choice the operator made must not inflate the failure rate, raise an alert, or be
    auto-retried behind their back. Retry still works whenever they want it back."""
    task = _owned_queued_task(db, user.id, job_id)
    if not task_queue.cancel_job(job_id):
        return _ops_redirect("queue", "gone")
    task.status = TaskStatus.CANCELLED
    task.finished_at = datetime.utcnow()
    task.error_message = "Cancelled from the Operations page before it started. Use Retry to queue it again."
    task_queue.clear_progress(task.id)
    db.commit()
    return _ops_redirect("queue", "cancelled")


@app.post("/operations/recover")
def ops_recover(user: CurrentUser, db: DbDep):
    """Fail definitively-dead tasks and free a render lock left by a crashed worker, now instead of
    at the next hourly tick. The render-concurrency-1 guard still applies: a live render is never
    touched."""
    from workers.scheduler import recover_now

    result = recover_now(db)
    flash = "recovered" if (result["reaped"] or result["lock_cleared"]) else "nothing"
    return _ops_redirect("worker", flash)


@app.post("/operations/restart-worker")
def ops_restart_worker(user: CurrentUser, db: DbDep):
    """Ask the worker to exit so its container is recreated (compose's restart policy does the rest).

    A flag in Redis, NOT a Docker command: the web container is the internet-facing service and must
    never hold the Docker socket, which is host-root (ADR-057). The worker's watchdog thread sees the
    flag within a minute and leaves cleanly."""
    task_queue.request_worker_restart()
    return _ops_redirect("worker", "restarting")


@app.post("/operations/buffer/{item_id}/reschedule")
def ops_reschedule(db: DbDep, item=Depends(get_owned_buffer_item), publish_at: str = Form("")):
    """Set (or clear) THIS episode's publish time — dodge another channel's peak hour without moving
    the whole campaign's slots (ADR-059).

    The input is a browser `datetime-local` value, i.e. the operator's own wall clock, so it is
    interpreted in the CAMPAIGN's timezone (the same clock its posting slots use) and stored as naive
    UTC like every other timestamp. An empty value clears the override and the episode rejoins the
    normal slot queue."""
    if item.status != BufferStatus.ready:
        raise HTTPException(400, "Only pre-rendered (ready) episodes can be rescheduled")
    raw = (publish_at or "").strip()
    if not raw:
        item.publish_at = None
        db.commit()
        return _calendar_redirect("resched_cleared")
    try:
        naive_local = datetime.fromisoformat(raw)
    except ValueError:
        return _calendar_redirect("resched_bad")
    from zoneinfo import ZoneInfo

    campaign = db.get(Campaign, item.campaign_id)
    item.publish_at = (naive_local.replace(tzinfo=_campaign_tz(campaign))
                       .astimezone(ZoneInfo("UTC")).replace(tzinfo=None))
    db.commit()
    return _calendar_redirect("rescheduled")


# ── Cross-channel alert feed (the header bell) ────────────────────────────────
# ONE inbox for "what is wrong across all my channels", derived from live state rather than stored as
# events (ADR-060): every row is recomputed from the same helpers the pages render, so an alert can
# never disagree with reality or linger after the problem is fixed. Levels: red (nothing is moving /
# something needs a decision), amber (heading for trouble), green (it worked).
_ALERT_ORDER = {"red": 0, "amber": 1, "green": 2}
_ALERT_FAILED_LIMIT = 8      # per-episode failures listed individually before it stops being useful
_ALERT_GREEN_LIMIT = 3       # a little "it's working" evidence, not a publish log
_QUOTA_WARN_RATIO = 0.8
_SLOT_RISK_HOURS = 6         # a slot this close with an empty buffer is an actionable warning


def _alert(level: str, key: str, text: str, *, channel: str = "", campaign: str = "",
           href: str = "", action: str = "", at=None) -> dict:
    """One alert row. `key` is stable for the same underlying problem so the client can de-dupe
    across polls; `at` is an ISO string when the row has a real timestamp."""
    return {"level": level, "key": key, "channel": channel, "campaign": campaign,
            "text": text, "href": href, "action": action,
            "at": at.isoformat() + "Z" if at is not None else None}


def _infra_alerts(db, user) -> list[dict]:
    """Faults that stop the whole factory, plus the two resource limits that quietly stop it later."""
    health = _system_health(db, user)
    out = []
    if not health["redis"]:
        out.append(_alert("red", "redis-down",
                          "Redis is unreachable — nothing can be queued, rendered or published.",
                          href="/operations?tab=worker", action="Operations"))
    if not health["worker"]:
        out.append(_alert("red", "worker-down",
                          "No render worker is registered — renders and uploads are both paused.",
                          href="/operations?tab=worker", action="Operations"))
    elif health["worker_stalled"]:
        out.append(_alert("red", "worker-stalled",
                          "The render worker is wedged — it has stopped making progress and will "
                          "restart itself.", href="/operations?tab=worker", action="Operations"))
    disk = health["disk_pct"]
    if disk is not None and disk >= settings.DISK_PRESSURE_PCT:
        out.append(_alert("amber", "disk", f"Disk is {disk}% full — old renders are being swept "
                                          "aggressively to make room.",
                          href="/operations?tab=publish", action="Publish queue"))
    budget, calls = health["ai_budget"], health["ai_calls"]
    if budget and calls >= budget * _QUOTA_WARN_RATIO:
        out.append(_alert("amber", "quota",
                          f"AI calls today: {calls}/{budget}. Renders start failing when the daily "
                          "quota runs out.", href="/settings", action="Settings"))
    return out


def _credential_alerts(db, user) -> list[dict]:
    """An ACTIVE campaign whose required API keys are missing (ADR-068).

    Nothing said this before: a first-time operator could create and start a campaign with no keys at
    all, watch it queue three episodes, and read "All clear" on the dashboard while every render was
    doomed. The keys are checked per campaign because the requirement differs — Studio/quote campaigns
    draw their visuals and need no Pexels key, stock-footage campaigns do.

    Also surfaces a channel whose stored token Facebook has since refused (ADR-072): that one is not
    about a campaign at all — every episode on that channel will fail to publish until it is fixed."""
    out: list[dict] = []
    for ch in db.scalars(select(Channel).where(
            Channel.user_id == user.id, Channel.status == ChannelStatus.expired)).all():
        out.append(_alert("red", f"channel-expired:{ch.id}",
                          "Its access token was refused — nothing can publish here until you paste a "
                          "fresh one. Rendered episodes keep waiting, they are not lost.",
                          channel=ch.channel_name, href="/channels", action="Fix the channel"))
    active = db.scalars(select(Campaign).where(
        Campaign.user_id == user.id, Campaign.status == CampaignStatus.active)).all()
    if not active:
        return out
    has_gemini = bool(user.gemini_api_key or settings.GEMINI_API_KEY)
    has_pexels = bool(user.pexels_api_key or settings.PEXELS_API_KEY)
    needs_pexels = [c for c in active
                    if (c.config_json or {}).get("visual_source") != "studio"
                    and (c.config_json or {}).get("content_style") != "quote"]
    if not has_gemini:
        out.append(_alert("red", "missing-gemini",
                          f"No Gemini API key — every render for your {len(active)} active "
                          f"campaign{'s' if len(active) != 1 else ''} will fail. It is free to get.",
                          href="/credentials", action="Add key"))
    if needs_pexels and not has_pexels:
        out.append(_alert("red", "missing-pexels",
                          f"No Pexels API key — {len(needs_pexels)} active campaign"
                          f"{'s' if len(needs_pexels) != 1 else ''} use stock footage and cannot "
                          "render without it. It is free to get.",
                          href="/credentials", action="Add key"))
    return out


def _work_alerts(db, user) -> list[dict]:
    """Things that need a human: failed episodes, a campaign the breaker stopped, review, proposals."""
    campaigns = {c.id: c for c in db.scalars(
        select(Campaign).where(Campaign.user_id == user.id)).all()}
    channels = {c.id: c for c in db.scalars(
        select(Channel).where(Channel.user_id == user.id)).all()}

    def names(campaign):
        channel = channels.get(campaign.channel_id) if campaign else None
        return (channel.channel_name if channel else ""), (campaign.topic_name if campaign else "")

    out = []
    for task in db.scalars(
            select(Task).where(Task.user_id == user.id, Task.status == TaskStatus.FAILED)
            .order_by(Task.finished_at.desc().nullslast(), Task.id.desc())
            .limit(_ALERT_FAILED_LIMIT)).all():
        campaign = campaigns.get(task.campaign_id)
        chan, camp = names(campaign)
        # Prefer the plain-language cause the episode page shows, so one failure reads the same
        # everywhere. Falling back to the stack trace's LAST line: that is the actual error, the rest
        # is noise in a one-line alert. A task can legitimately carry no message (an old row, a
        # cleared retry) — say so rather than rendering "Ep 7 failed — failed".
        lines = (task.error_message or "").strip().splitlines()
        diag = _diagnose_failure(task.error_message)
        reason = diag["cause"] if diag else (lines[-1][:120] if lines else "no error recorded")
        out.append(_alert("red", f"task-failed:{task.id}", f"Ep {task.episode_number} failed — {reason}",
                          channel=chan, campaign=camp, href=f"/episodes/{task.id}", action="Open",
                          at=task.finished_at or task.updated_at))
    for campaign in campaigns.values():
        if campaign.status == CampaignStatus.failed:
            chan, camp = names(campaign)
            out.append(_alert("red", f"campaign-paused:{campaign.id}",
                              "Campaign stopped after repeated failures — no new episodes will "
                              "render until you start it again.",
                              channel=chan, campaign=camp,
                              href=f"/campaigns/{campaign.id}", action="Open"))
    review = _task_counts(db, user.id)["awaiting_review"]
    if review:
        out.append(_alert("amber", "review",
                          f"{review} episode{'s' if review != 1 else ''} waiting for your review.",
                          href="/assets", action="Review"))
    proposed = _autopilot_proposed_count(db, user.id)
    if proposed:
        out.append(_alert("amber", "autopilot",
                          f"{proposed} autopilot proposal{'s' if proposed != 1 else ''} awaiting a "
                          "decision.", href="/autopilot", action="Autopilot"))
    return out


def _schedule_alerts(db, user) -> list[dict]:
    """A posting slot about to be missed because nothing is ready — the failure you want to hear
    about BEFORE it happens, since a missed slot cannot be recovered after the fact."""
    ready = dict(db.execute(
        select(BufferPoolItem.campaign_id, func.count())
        .join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        .where(Campaign.user_id == user.id, BufferPoolItem.status == BufferStatus.ready)
        .group_by(BufferPoolItem.campaign_id)).all())
    channels = {c.id: c for c in db.scalars(
        select(Channel).where(Channel.user_id == user.id)).all()}
    out = []
    for campaign in db.scalars(select(Campaign).where(
            Campaign.user_id == user.id, Campaign.status == CampaignStatus.active)).all():
        if ready.get(campaign.id):
            continue  # something is ready to go out
        nxt = _next_slot(campaign)
        if nxt is None or nxt["in_hours"] > _SLOT_RISK_HOURS:
            continue
        channel = channels.get(campaign.channel_id)
        out.append(_alert("amber", f"slot-risk:{campaign.id}",
                          f"Next post is {nxt['when']} but nothing is rendered yet — that slot will "
                          "be missed.", channel=channel.channel_name if channel else "",
                          campaign=campaign.topic_name, href="/operations?tab=queue",
                          action="Render queue"))
    return out


def _success_alerts(db, user) -> list[dict]:
    """A little evidence the factory is working, so a quiet bell means "healthy", not "broken feed"."""
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(hours=24)
    campaigns = {c.id: c for c in db.scalars(
        select(Campaign).where(Campaign.user_id == user.id)).all()}
    channels = {c.id: c for c in db.scalars(
        select(Channel).where(Channel.user_id == user.id)).all()}
    out = []
    for task in db.scalars(
            select(Task).where(Task.user_id == user.id, Task.status == TaskStatus.COMPLETED,
                               Task.finished_at.isnot(None), Task.finished_at >= cutoff)
            .order_by(Task.finished_at.desc()).limit(_ALERT_GREEN_LIMIT)).all():
        campaign = campaigns.get(task.campaign_id)
        channel = channels.get(campaign.channel_id) if campaign else None
        out.append(_alert("green", f"published:{task.id}",
                          f"Ep {task.episode_number} published.",
                          channel=channel.channel_name if channel else "",
                          campaign=campaign.topic_name if campaign else "",
                          href=task.published_url or f"/episodes/{task.id}",
                          action="View", at=task.finished_at))
    return out


def _alerts(db, user) -> list[dict]:
    """The whole feed, most urgent first. Fail-soft per source: one broken query must not empty the
    bell, because an empty bell reads as "everything is fine"."""
    rows: list[dict] = []
    for source in (_infra_alerts, _credential_alerts, _work_alerts, _schedule_alerts,
                   _success_alerts):
        try:
            rows += source(db, user)
        except Exception:  # noqa: BLE001
            logger.warning("alert source %s failed", source.__name__, exc_info=True)
    rows.sort(key=lambda a: (_ALERT_ORDER.get(a["level"], 9), a["at"] or "", a["key"]))
    return rows


@app.get("/api/alerts")
def api_alerts(user: CurrentUser, db: DbDep):
    """Feed for the header bell. `actionable` counts red+amber — the badge number, so it can never
    disagree with the list length."""
    rows = _alerts(db, user)
    return {"alerts": rows,
            # The badge number is the SHARED attention count, not this feed's row count: the panel
            # groups backlogs into one row, so counting rows produced a number that disagreed with
            # every other badge for the same facts (ADR-064).
            "attention": _attention_count(db, user.id),
            "actionable": sum(1 for a in rows if a["level"] in ("red", "amber")),
            "worst": ("red" if any(a["level"] == "red" for a in rows)
                      else "amber" if any(a["level"] == "amber" for a in rows) else "")}
