"""FastAPI web app — dashboard, channel/campaign/credential management, and the task-log API.

Routes are tenant-scoped through the `CurrentUser` dependency (solo mode injects the built-in admin;
multi-tenant verifies a Firebase token). Server-rendered Jinja templates + a small polling script
(static/app.js) drive the real-time task log — no runtime CDN (CSP-friendly).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import String, cast, func, or_, select
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
from core.config import settings
from core.tts import VOICE_CHOICES
from database.db_session import get_db, init_db
from database.models import BufferPoolItem, Campaign, Channel, Task
from database.types import BufferStatus, CampaignStatus, ChannelStatus, Platform, TaskStatus
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


@app.exception_handler(StarletteHTTPException)
async def _auth_aware_http_exception(request: Request, exc: StarletteHTTPException):
    """Browsers navigating unauthenticated get sent to /login; API callers keep the raw 401."""
    if (
        exc.status_code == 401
        and settings.MULTI_TENANT_MODE
        and "text/html" in (request.headers.get("accept") or "")
    ):
        return RedirectResponse("/login", status_code=303)
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


def _system_health(db) -> dict:
    """Live infrastructure signals for the dashboard health strip. Never raises — a dead Redis
    should show as red, not take the page down."""
    health = {"redis": False, "worker": False, "queue_depth": None, "buffer_ready": 0,
              "disk_pct": None, "ai_calls": 0, "ai_budget": settings.GEMINI_DAILY_BUDGET}
    try:
        health["redis"] = bool(task_queue.conn.ping())
        health["worker"] = task_queue.worker_alive()
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
    return {
        "published": by_status.get(TaskStatus.COMPLETED, 0),
        "working": sum(by_status.get(s, 0) for s in _WORKING_STATUSES),
        "awaiting_review": by_status.get(TaskStatus.AWAITING_REVIEW, 0),
        "failed": by_status.get(TaskStatus.FAILED, 0),
    }


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
        "retention_this": round(sum(ret_this) / len(ret_this), 1) if ret_this else None,
        "retention_prev": round(sum(ret_prev) / len(ret_prev), 1) if ret_prev else None,
    }


def _next_publish(db, user_id: int):
    """Soonest upcoming posting slot across active auto-publish campaigns (each in its own tz)."""
    from datetime import timedelta

    from workers.scheduler import WEEKDAY_KEYS, local_now

    active = db.scalars(select(Campaign).where(
        Campaign.user_id == user_id, Campaign.status == CampaignStatus.active)).all()
    best = None
    for c in active:
        cfg = c.config_json or {}
        if not cfg.get("auto_publish", True):
            continue
        slots = sorted(cfg.get("posting_slots") or [])
        if not slots:
            continue
        allowed = cfg.get("posting_days") or []
        now_local = local_now(cfg.get("timezone"))
        found = None
        for dd in range(0, 8):
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
                    found = (cand, s)
                    break
            if found:
                break
        if not found:
            continue
        hours = (found[0] - now_local).total_seconds() / 3600
        if best is None or hours < best["in_hours"]:
            best = {"in_hours": round(hours, 1), "slot": found[1],
                    "campaign": c.topic_name, "when": found[0].strftime("%a %H:%M")}
    return best


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
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request, "user": user, "channels": channels, "campaigns": campaigns,
            "tasks": tasks, "nav": "dashboard",
            "health": _system_health(db),
            "counts": _task_counts(db, user.id),
            "attention_failed": attention_failed, "attention_review": attention_review,
            "scorecard": _scorecard(db, user.id), "next_publish": _next_publish(db, user.id),
            "camp_by_id": {c.id: c for c in campaigns},
            "chan_by_id": {c.id: c for c in channels},
        },
    )


# ── Channels Manager ─────────────────────────────────────────────────────────
_CHANNEL_STATUS_FILTERS = ("active", "expired")


@app.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request, user: CurrentUser, db: DbDep, status: str = "", q: str = ""):
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
         "camp_counts": camp_counts, "chips": chips, "status": status, "q": q, "total_all": total_all},
    )


@app.post("/channels/facebook")
def add_facebook_channel(
    user: CurrentUser,
    db: DbDep,
    channel_name: str = Form(...),
    page_id: str = Form(...),
    page_access_token: str = Form(...),
    avatar_url: str = Form(""),
):
    creds = json.dumps({"page_id": page_id, "page_access_token": page_access_token})
    channel = Channel(
        user_id=user.id, platform=Platform.facebook, channel_name=channel_name,
        avatar_url=avatar_url or None, encrypted_credentials=creds, status=ChannelStatus.active,
    )
    db.add(channel)
    db.commit()
    return RedirectResponse("/channels", status_code=303)


@app.post("/channels/{channel_id}/delete")
def delete_channel(channel=Depends(get_owned_channel), db=Depends(get_db)):
    db.delete(channel)
    db.commit()
    return RedirectResponse("/channels", status_code=303)


# ── Google OAuth2 web flow (connect a YouTube channel) ───────────────────────
@app.get("/oauth/google/start")
def google_oauth_start(request: Request, user: CurrentUser):
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
    channels = {c.id: c for c in db.scalars(select(Channel).where(Channel.user_id == user.id)).all()}
    chips = [{"label": "All", "value": "", "count": total_all}] + [
        {"label": s.title(), "value": s, "count": status_counts.get(s, 0)}
        for s in _CAMPAIGN_STATUS_FILTERS]
    return templates.TemplateResponse(
        request,
        "campaigns.html",
        {"request": request, "user": user, "campaigns": campaigns, "channels": channels, "nav": "campaigns",
         "asset_counts": _buffer_counts(db, user.id),
         "scope_channel": channels.get(channel) if channel else None,
         "chips": chips, "status": status, "q": q, "total_all": total_all,
         "scope_hidden": {"channel": channel} if channel else {}},
    )


@app.get("/campaigns/new", response_class=HTMLResponse)
def campaign_new_form(request: Request, user: CurrentUser, db: DbDep, from_id: int | None = None):
    """New-campaign form. With ?from_id=<campaign>, prefills from an owned campaign (Duplicate —
    e.g. same horror persona in another language to another channel)."""
    channels = db.scalars(select(Channel).where(Channel.user_id == user.id)).all()
    ctx: dict = {"request": request, "user": user, "channels": channels, "nav": "campaigns"}
    if from_id is not None:
        source = db.get(Campaign, from_id)
        if source is not None and source.user_id == user.id:
            ctx["source"] = source
            ctx["cfg"] = source.config_json or {}
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
            catchphrase_open=catchphrase_open.strip() or None,
            catchphrase_close=catchphrase_close.strip() or None,
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
def propose_campaign_route(user: CurrentUser, topic: str = Form(""), language: str = Form("")):
    """AI-design a whole campaign config from a title (or from scratch). Returns JSON the New
    Campaign form fills in for review — nothing is saved until the user clicks Create."""
    import random

    from core import ai_engine

    key = user.gemini_api_key or settings.GEMINI_API_KEY
    if not key:
        return JSONResponse({"error": "Add a Gemini API key first (Credentials page or .env)."},
                            status_code=400)
    lang = language if language in ("en", "vi", "es") else None
    try:
        proposal = ai_engine.propose_campaign(
            topic=topic.strip() or None, language=lang, api_key=key,
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
    motion: str = "on", caption_theme: str = "highlight", self_critique: str = "on",
    script_depth: str = "standard", video_format: str = "short",
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
        "catchphrase_open": catchphrase_open or None,
        "catchphrase_close": catchphrase_close or None,
        "continuity": continuity if continuity in ("none", "no_repeat", "serial") else "none",
        "timezone": timezone.strip() or None,
        # Cinema Polish + critic loop — "on"/"off" strings so an absent field means ON (default).
        "motion": "off" if motion == "off" else "on",
        "caption_theme": caption_theme if caption_theme in ("classic", "highlight", "boxed", "neon") else "highlight",
        "self_critique": "off" if self_critique == "off" else "on",
        # Script depth: "deep" adds a research/brief Gemini pass for fact-rich storytelling.
        "script_depth": "deep" if script_depth == "deep" else "standard",
        # Output format: "short" = vertical 1080×1920 clips; "long" = horizontal 16:9 multi-minute.
        "video_format": "long" if video_format == "long" else "short",
        # Music: none | auto (random CC0 by mood, per episode) | file (operator-supplied path).
        "music_mode": music_mode if music_mode in ("none", "auto", "file") else "none",
        "music_mood": music_mood.strip() or None,
        # Auto-QC gate (ADR-013): colour grade baked into the encode; machine review of output.
        "color_grade": color_grade if color_grade in ("cinematic", "warm", "cool", "vivid", "noir") else None,
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
    continuity: str = Form("none"),
    timezone: str = Form(""),
    motion: str = Form("on"),
    caption_theme: str = Form("highlight"),
    self_critique: str = Form("on"),
    script_depth: str = Form("standard"),
    video_format: str = Form("short"),
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
            motion=motion, caption_theme=caption_theme, self_critique=self_critique,
            script_depth=script_depth, video_format=video_format,
            music_mode=music_mode, music_mood=music_mood,
            color_grade=color_grade, auto_qc=auto_qc,
            max_per_day=max_per_day, min_per_day=min_per_day,
            title_prefix=title_prefix, posting_days=posting_days,
            duration_min_s=duration_min_s, duration_max_s=duration_max_s,
            affiliate_url=affiliate_url, affiliate_label=affiliate_label,
        ),
    }


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
    return RedirectResponse("/campaigns", status_code=303)


@app.get("/campaigns/{campaign_id}/edit", response_class=HTMLResponse)
def campaign_edit_form(request: Request, user: CurrentUser, db: DbDep,
                       campaign=Depends(get_owned_campaign)):
    channels = db.scalars(select(Channel).where(Channel.user_id == user.id)).all()
    return templates.TemplateResponse(
        request, "campaign_new.html",
        {"request": request, "user": user, "channels": channels, "nav": "campaigns",
         "campaign": campaign, "cfg": campaign.config_json or {}},
    )


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
    return RedirectResponse("/campaigns", status_code=303)


@app.post("/campaigns/{campaign_id}/start")
def start_campaign(campaign=Depends(get_owned_campaign), db=Depends(get_db)):
    campaign.status = CampaignStatus.active
    db.commit()
    video_worker.hydrate_buffers(db)  # queue the first episodes immediately
    return RedirectResponse("/campaigns", status_code=303)


@app.post("/campaigns/{campaign_id}/delete")
def delete_campaign(campaign=Depends(get_owned_campaign), db=Depends(get_db)):
    db.delete(campaign)
    db.commit()
    return RedirectResponse("/campaigns", status_code=303)


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
    gemini_model: str | None = Form(None),
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
    # Model chain is NOT a secret and has its own form: when the field is present, the submitted
    # value replaces the stored one — an EMPTY submission means "back to the server default".
    if gemini_model is not None:
        cleaned = ",".join(m.strip() for m in gemini_model.split(",") if m.strip())[:200]
        user.gemini_model = cleaned or None
    db.add(user)
    db.commit()
    return RedirectResponse("/credentials", status_code=303)


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
         "scope_campaign": camp_by_id.get(campaign) if campaign else None,
         "scope_channel": chan_by_id.get(channel) if channel else None,
         "status": status, "status_counts": status_counts, "total_all": total_all,
         "pool_total": pool_total, "chips": chips, "q": q, "scope_hidden": scope_hidden,
         "page": page, "pages": pages, "scope_qs": scope_qs,
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


def _action_redirect(return_to: str, flash: str, default: str, flash_reason: str = "") -> RedirectResponse:
    """Redirect an asset/task action to its Episode view (when it came from there) or the default
    /assets URL (unchanged behavior when no return path is supplied)."""
    ep = _episode_return(return_to)
    if ep is None:
        return RedirectResponse(default, status_code=303)
    qs = f"?flash={flash}" if flash else ""
    if flash_reason:
        from urllib.parse import quote
        qs += f"&flash_reason={quote(flash_reason)}"
    return RedirectResponse(ep + qs, status_code=303)


@app.post("/assets/{item_id}/approve")
def approve_asset(db: DbDep, item=Depends(get_owned_buffer_item), return_to: str = Form("")):
    if item.status != BufferStatus.awaiting_review:
        raise HTTPException(400, "Only items awaiting review can be approved")
    if not (item.video_path and os.path.exists(item.video_path)):
        return _action_redirect(return_to, "missing", "/assets?flash=missing")
    task = db.scalar(select(Task).where(
        Task.campaign_id == item.campaign_id, Task.episode_number == item.episode_number))
    if task is not None:
        task.status = TaskStatus.PENDING_QUEUE  # publish job will drive it to PUBLISHING
        task.error_message = None
        db.commit()
    task_queue.enqueue_publish(item.id)
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
    db.commit()
    task.rq_job_id = task_queue.enqueue_render(task.id)
    db.commit()
    return _action_redirect(return_to, "rerender", "/assets?flash=rerender")


@app.post("/assets/{item_id}/reject")
def reject_asset(db: DbDep, item=Depends(get_owned_buffer_item), reason: str = Form(""),
                 return_to: str = Form("")):
    if item.status != BufferStatus.awaiting_review:
        raise HTTPException(400, "Only items awaiting review can be rejected")
    for path in (item.video_path, item.thumbnail_path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            logger.warning("Could not remove %s", path)
    item.status = BufferStatus.rejected
    reason = reason.strip()[:200]
    task = db.scalar(select(Task).where(
        Task.campaign_id == item.campaign_id, Task.episode_number == item.episode_number))
    if task is not None:
        task.status = TaskStatus.FAILED
        task.error_message = ("Rejected in review: " + reason) if reason else \
            "Rejected in review by the operator. Use Retry to re-render."
    # Feed the operator's reason into the campaign's avoid-list (Loop 1 learning signal).
    if reason:
        campaign = db.get(Campaign, item.campaign_id)
        if campaign is not None:
            learning = dict(campaign.learning_json or {})
            reasons = (learning.get("reject_reasons") or [])[-9:]
            learning["reject_reasons"] = reasons + [reason]
            campaign.learning_json = learning
    db.commit()
    ep = _episode_return(return_to)
    if ep is not None:
        return _action_redirect(return_to, "rejected", "/assets", flash_reason=reason)
    from urllib.parse import quote

    return RedirectResponse(
        "/assets?flash=rejected" + (f"&flash_reason={quote(reason)}" if reason else ""),
        status_code=303,
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
    return templates.TemplateResponse(
        request, "episode.html",
        {"request": request, "user": user, "nav": "tasks", "task": task, "campaign": campaign,
         "channel": channel, "buffer": buffer, "previewable": previewable,
         "stages": _EPISODE_STAGES, "stage_index": stage_index,
         "failed": task.status == TaskStatus.FAILED,
         "flash": flash if flash in ("publish", "rerender", "rejected", "missing") else "",
         "flash_reason": flash_reason[:200]},
    )


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


_EPISODES_PER_PAGE = 20


@app.get("/campaigns/{campaign_id}/performance", response_class=HTMLResponse)
def campaign_performance(request: Request, user: CurrentUser, db: DbDep,
                         campaign=Depends(get_owned_campaign), page: int = 1):
    # Full list drives the aggregates (variant summary, sparkline, best episode); the episodes TABLE
    # is paginated (newest first) so a long-running campaign doesn't render hundreds of rows at once.
    episodes = db.scalars(
        select(Task).where(Task.campaign_id == campaign.id).order_by(Task.episode_number)
    ).all()
    measured = [t for t in episodes if t.stats_json]
    best = max(measured, key=lambda t: t.stats_json.get("avg_pct_viewed", 0), default=None)
    ep_recent = list(reversed(episodes))
    pages = max(1, -(-len(ep_recent) // _EPISODES_PER_PAGE))
    page = min(max(page, 1), pages)
    episodes_page = ep_recent[(page - 1) * _EPISODES_PER_PAGE: page * _EPISODES_PER_PAGE]
    return templates.TemplateResponse(
        request, "performance.html",
        {"request": request, "user": user, "nav": "campaigns", "campaign": campaign,
         "episodes": episodes, "episodes_page": episodes_page, "page": page, "pages": pages,
         "learning": campaign.learning_json or {},
         "best_id": best.id if best else None, "variants": ab_variant_summary(episodes),
         # Campaign-hub context: parent channel + its buffer tally for the drill-down links.
         "channel": db.get(Channel, campaign.channel_id),
         "asset_count": _buffer_counts(db, user.id).get(campaign.id, {"ready": 0, "awaiting_review": 0})},
    )


@app.post("/campaigns/{campaign_id}/learning/reset")
def reset_learning(db: DbDep, campaign=Depends(get_owned_campaign)):
    campaign.learning_json = None
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign.id}/performance", status_code=303)


# ── Content calendar ─────────────────────────────────────────────────────────
def upcoming_slot_cells(campaign: Campaign, days: int = 7) -> list[list[str]] | None:
    """Per-day slot times for the next `days` days in the campaign's own timezone, honoring its
    posting_days. None = the campaign doesn't slot-publish (continuous or review mode)."""
    from datetime import timedelta

    from workers.scheduler import WEEKDAY_KEYS, local_now

    cfg = campaign.config_json or {}
    slots = sorted(cfg.get("posting_slots") or [])
    if not slots or not cfg.get("auto_publish", True):
        return None
    allowed = cfg.get("posting_days") or []
    start = local_now(cfg.get("timezone"))
    cells: list[list[str]] = []
    for d in range(days):
        day = start + timedelta(days=d)
        key = WEEKDAY_KEYS[day.weekday()]
        cells.append(slots if (not allowed or key in allowed) else [])
    return cells


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, user: CurrentUser, db: DbDep):
    from datetime import timedelta

    from workers.scheduler import local_now

    campaigns = db.scalars(select(Campaign).where(
        Campaign.user_id == user.id, Campaign.status == CampaignStatus.active)).all()
    ready_counts = dict(db.execute(
        select(BufferPoolItem.campaign_id, func.count())
        .where(BufferPoolItem.status == BufferStatus.ready)
        .group_by(BufferPoolItem.campaign_id)
    ).all())
    slotted, unslotted = [], []
    for c in campaigns:
        cells = upcoming_slot_cells(c)
        entry = {"campaign": c, "ready": ready_counts.get(c.id, 0),
                 "tz": (c.config_json or {}).get("timezone") or settings.TIMEZONE,
                 "mode": "review" if not (c.config_json or {}).get("auto_publish", True) else "continuous"}
        if cells is not None:
            entry["cells"] = cells
            slotted.append(entry)
        else:
            unslotted.append(entry)
    base = local_now()
    day_headers = [(base + timedelta(days=d)).strftime("%a %d/%m") for d in range(7)]
    return templates.TemplateResponse(
        request, "calendar.html",
        {"request": request, "user": user, "nav": "calendar", "slotted": slotted,
         "unslotted": unslotted, "day_headers": day_headers},
    )


# ── Real-Time Task Logs ──────────────────────────────────────────────────────
@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, user: CurrentUser, db: DbDep, campaign: int | None = None):
    scope = None
    if campaign:  # drill-down scope from a campaign (client-side filter over the live feed)
        scope = db.get(Campaign, campaign)
        if scope is not None and scope.user_id != user.id:
            scope = None
    return templates.TemplateResponse(
        request, "tasks.html",
        {"request": request, "user": user, "nav": "tasks", "scope_campaign": scope})


_TASKS_PER_PAGE = 25


@app.get("/api/tasks")
def api_tasks(user: CurrentUser, db: DbDep,
              page: int = 1, q: str = "", campaign: int | None = None):
    # Full task history, newest first, paginated — page 1 carries the live/active jobs. Scope (?campaign)
    # and search (?q) run in SQL so they span ALL history, not just the page currently in the browser.
    base = select(Task).where(Task.user_id == user.id)
    if campaign:
        base = base.where(Task.campaign_id == campaign)
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
    terminal = ("COMPLETED", "FAILED", "AWAITING_REVIEW", "SCHEDULED")
    out = []
    for t in rows:
        # Live % comes from Redis while running; fall back to the durable column.
        live = task_queue.get_progress(t.id) if t.status.value not in terminal else t.progress_pct
        campaign = campaigns.get(t.campaign_id)
        channel = channels.get(campaign.channel_id) if campaign else None
        duration_s = None
        if t.started_at:
            end = t.finished_at or datetime.utcnow()
            duration_s = max(0, int((end - t.started_at).total_seconds()))
        out.append({
            "id": t.id, "campaign_id": t.campaign_id, "episode": t.episode_number,
            "topic": campaign.topic_name if campaign else f"C{t.campaign_id}",
            "channel": channel.channel_name if channel else "—",
            "platform": channel.platform.value if channel else None,
            "status": t.status.value, "progress": round(live or t.progress_pct, 1),
            "error": t.error_message, "published_url": t.published_url,
            "duration_s": duration_s, "retry_count": t.retry_count,
            "can_retry": t.status == TaskStatus.FAILED,
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
    return {"health": _system_health(db), "counts": _task_counts(db, user.id),
            "channels": channels, "active_campaigns": active}


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: int, user: CurrentUser, db: DbDep, return_to: str = Form("")):
    """Retry a failed episode. If the rendered file still exists (e.g. the upload failed or the
    item was awaiting review), only the publish step is retried — no re-render.

    Returns JSON for the Task Logs poller (fetch, no `return_to`); a form POST from the Episode view
    passes `return_to` and gets a 303 back to that page instead."""
    task = db.get(Task, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "Task not found")
    if task.status != TaskStatus.FAILED:
        raise HTTPException(400, "Only failed tasks can be retried")
    task.error_message = None
    task.retry_count += 1
    task.progress_pct = 0
    task.status = TaskStatus.PENDING_QUEUE
    buf = db.scalar(select(BufferPoolItem).where(
        BufferPoolItem.campaign_id == task.campaign_id,
        BufferPoolItem.episode_number == task.episode_number,
        BufferPoolItem.status.in_([BufferStatus.ready, BufferStatus.awaiting_review]),
    ))
    if buf is not None and buf.video_path and os.path.exists(buf.video_path):
        db.commit()
        task_queue.enqueue_publish(buf.id)
        return _action_redirect(return_to, "rerender", "") if _episode_return(return_to) \
            else {"ok": True, "mode": "publish"}
    db.commit()
    task.rq_job_id = task_queue.enqueue_render(task.id)
    db.commit()
    return _action_redirect(return_to, "rerender", "") if _episode_return(return_to) \
        else {"ok": True, "mode": "render"}
