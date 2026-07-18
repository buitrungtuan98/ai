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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
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


app = FastAPI(title="AI Video Factory", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    max_age=settings.SESSION_MAX_AGE_DAYS * 86400,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["settings"] = settings  # e.g. MULTI_TENANT_MODE toggles the sign-out chip

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
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
    if request.query_params.get("state") != request.session.pop("login_state", None):
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
    TaskStatus.RENDERING, TaskStatus.PUBLISHING,
]


def _system_health(db) -> dict:
    """Live infrastructure signals for the dashboard health strip. Never raises — a dead Redis
    should show as red, not take the page down."""
    health = {"redis": False, "worker": False, "queue_depth": None, "buffer_ready": 0, "disk_pct": None}
    try:
        health["redis"] = bool(task_queue.conn.ping())
        health["worker"] = task_queue.worker_alive()
        health["queue_depth"] = len(task_queue.render_queue)
    except Exception:  # noqa: BLE001
        pass
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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: CurrentUser, db: DbDep):
    channels = db.scalars(select(Channel).where(Channel.user_id == user.id)).all()
    campaigns = db.scalars(select(Campaign).where(Campaign.user_id == user.id)).all()
    tasks = db.scalars(
        select(Task).where(Task.user_id == user.id).order_by(Task.id.desc()).limit(10)
    ).all()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request, "user": user, "channels": channels, "campaigns": campaigns,
            "tasks": tasks, "nav": "dashboard",
            "health": _system_health(db),
            "counts": _task_counts(db, user.id),
            "camp_by_id": {c.id: c for c in campaigns},
            "chan_by_id": {c.id: c for c in channels},
        },
    )


# ── Channels Manager ─────────────────────────────────────────────────────────
@app.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request, user: CurrentUser, db: DbDep):
    channels = db.scalars(select(Channel).where(Channel.user_id == user.id)).all()
    return templates.TemplateResponse(
        request, "channels.html", {"request": request, "user": user, "channels": channels, "nav": "channels"}
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

    if request.query_params.get("state") != request.session.pop("oauth_state", None):
        raise HTTPException(400, "OAuth state mismatch")
    flow = _google_flow(YOUTUBE_SCOPES, "/oauth/google/callback")
    # Exchange by code (not the full callback URL) — robust behind the HTTP-origin tunnel.
    flow.fetch_token(code=request.query_params.get("code"))
    creds = flow.credentials
    user_id = request.session.get("oauth_user")

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
@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request, user: CurrentUser, db: DbDep):
    campaigns = db.scalars(select(Campaign).where(Campaign.user_id == user.id)).all()
    channels = {c.id: c for c in db.scalars(select(Channel).where(Channel.user_id == user.id)).all()}
    return templates.TemplateResponse(
        request,
        "campaigns.html",
        {"request": request, "user": user, "campaigns": campaigns, "channels": channels, "nav": "campaigns"},
    )


@app.get("/campaigns/new", response_class=HTMLResponse)
def campaign_new_form(request: Request, user: CurrentUser, db: DbDep):
    channels = db.scalars(select(Channel).where(Channel.user_id == user.id)).all()
    return templates.TemplateResponse(
        request, "campaign_new.html", {"request": request, "user": user, "channels": channels, "nav": "campaigns"}
    )


def _build_campaign_config(
    *, language: str, system_prompt: str, voice: str, rate_pct: int, subtitle_style: str,
    music_path: str, music_volume: float, posting_slots: str, ab_testing: bool, cta: str,
    privacy: str, publish_mode: str, buffer_size: str,
    watermark_path: str, tint_color: str, tint_opacity: float, mirror: bool,
) -> dict:
    """One place turns the campaign form into config_json (DRY: shared by create and edit)."""
    config: dict = {
        "language": language, "system_prompt": system_prompt, "voice": voice or None,
        "rate_pct": rate_pct, "subtitle_style": subtitle_style,
        "music_path": music_path or None, "music_volume": music_volume,
        "posting_slots": [s.strip() for s in posting_slots.split(",") if s.strip()],
        "ab_testing": ab_testing, "cta": cta or None,
        "privacy": privacy, "auto_publish": publish_mode != "review",
        "buffer_size": int(buffer_size) if buffer_size.strip().isdigit() else None,
    }
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
) -> dict:
    return {
        "topic_name": topic_name, "channel_id": channel_id, "total_episodes": total_episodes,
        "config": _build_campaign_config(
            language=language, system_prompt=system_prompt, voice=voice, rate_pct=rate_pct,
            subtitle_style=subtitle_style, music_path=music_path, music_volume=music_volume,
            posting_slots=posting_slots, ab_testing=ab_testing, cta=cta, privacy=privacy,
            publish_mode=publish_mode, buffer_size=buffer_size, watermark_path=watermark_path,
            tint_color=tint_color, tint_opacity=tint_opacity, mirror=mirror,
        ),
    }


@app.post("/campaigns")
def create_campaign(user: CurrentUser, db: DbDep, form: dict = Depends(_campaign_form)):
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
    db.add(user)
    db.commit()
    return RedirectResponse("/credentials", status_code=303)


# ── Asset Pool Cache (+ preview & review) ────────────────────────────────────
@app.get("/assets", response_class=HTMLResponse)
def assets_page(request: Request, user: CurrentUser, db: DbDep):
    items = db.scalars(
        select(BufferPoolItem)
        .join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        .where(Campaign.user_id == user.id)
        .order_by(BufferPoolItem.id.desc())
    ).all()
    campaigns = db.scalars(select(Campaign).where(Campaign.user_id == user.id)).all()
    previewable = {i.id for i in items if i.video_path and os.path.exists(i.video_path)}
    return templates.TemplateResponse(
        request, "assets.html",
        {"request": request, "user": user, "items": items, "nav": "assets",
         "camp_by_id": {c.id: c for c in campaigns}, "previewable": previewable},
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
        try:
            raw_start, _, raw_end = range_header[6:].partition("-")
            start = int(raw_start) if raw_start else 0
            end = int(raw_end) if raw_end else file_size - 1
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


@app.post("/assets/{item_id}/approve")
def approve_asset(db: DbDep, item=Depends(get_owned_buffer_item)):
    if item.status != BufferStatus.awaiting_review:
        raise HTTPException(400, "Only items awaiting review can be approved")
    task = db.scalar(select(Task).where(
        Task.campaign_id == item.campaign_id, Task.episode_number == item.episode_number))
    if task is not None:
        task.status = TaskStatus.PENDING_QUEUE  # publish job will drive it to PUBLISHING
        task.error_message = None
        db.commit()
    task_queue.enqueue_publish(item.id)
    return RedirectResponse("/assets", status_code=303)


@app.post("/assets/{item_id}/reject")
def reject_asset(db: DbDep, item=Depends(get_owned_buffer_item)):
    if item.status != BufferStatus.awaiting_review:
        raise HTTPException(400, "Only items awaiting review can be rejected")
    for path in (item.video_path, item.thumbnail_path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            logger.warning("Could not remove %s", path)
    item.status = BufferStatus.rejected
    task = db.scalar(select(Task).where(
        Task.campaign_id == item.campaign_id, Task.episode_number == item.episode_number))
    if task is not None:
        task.status = TaskStatus.FAILED
        task.error_message = "Rejected in review by the operator. Use Retry to re-render."
    db.commit()
    return RedirectResponse("/assets", status_code=303)


# ── Real-Time Task Logs ──────────────────────────────────────────────────────
@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, user: CurrentUser):
    return templates.TemplateResponse(request, "tasks.html", {"request": request, "user": user, "nav": "tasks"})


@app.get("/api/tasks")
def api_tasks(user: CurrentUser, db: DbDep):
    rows = db.scalars(
        select(Task).where(Task.user_id == user.id).order_by(Task.id.desc()).limit(50)
    ).all()
    campaigns = {c.id: c for c in db.scalars(
        select(Campaign).where(Campaign.user_id == user.id)).all()}
    channels = {c.id: c for c in db.scalars(
        select(Channel).where(Channel.user_id == user.id)).all()}
    terminal = ("COMPLETED", "FAILED", "AWAITING_REVIEW")
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
    return {"tasks": out}


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: int, user: CurrentUser, db: DbDep):
    """Retry a failed episode. If the rendered file still exists (e.g. the upload failed or the
    item was awaiting review), only the publish step is retried — no re-render."""
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
        return {"ok": True, "mode": "publish"}
    db.commit()
    task.rq_job_id = task_queue.enqueue_render(task.id)
    db.commit()
    return {"ok": True, "mode": "render"}
