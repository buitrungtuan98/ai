"""FastAPI web app — dashboard, channel/campaign/credential management, and the task-log API.

Routes are tenant-scoped through the `CurrentUser` dependency (solo mode injects the built-in admin;
multi-tenant verifies a Firebase token). Server-rendered Jinja templates + a small polling script
(static/app.js) drive the real-time task log — no runtime CDN (CSP-friendly).
"""
from __future__ import annotations

import json
import logging

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from auth.dependencies import CurrentUser, DbDep, get_owned_campaign, get_owned_channel
from core.config import settings
from database.db_session import get_db, init_db
from database.models import BufferPoolItem, Campaign, Channel, Task
from database.types import CampaignStatus, ChannelStatus, Platform
from workers import task_queue, video_worker

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # ensure schema exists on boot
    yield


app = FastAPI(title="AI Video Factory", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ── Dashboard ────────────────────────────────────────────────────────────────
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
        {"request": request, "user": user, "channels": channels, "campaigns": campaigns,
         "tasks": tasks, "nav": "dashboard"},
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
    from google_auth_oauthlib.flow import Flow

    flow = _google_flow()
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    request.session["oauth_state"] = state
    request.session["oauth_user"] = user.id
    return RedirectResponse(auth_url)


@app.get("/oauth/google/callback")
def google_oauth_callback(request: Request, db: DbDep):
    from googleapiclient.discovery import build
    from google_auth_oauthlib.flow import Flow

    flow = _google_flow(state=request.session.get("oauth_state"))
    flow.fetch_token(authorization_response=str(request.url))
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


def _google_flow(state: str | None = None):
    from google_auth_oauthlib.flow import Flow

    client_config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    flow = Flow.from_client_config(client_config, scopes=YOUTUBE_SCOPES, state=state)
    flow.redirect_uri = settings.OAUTH_REDIRECT_BASE.rstrip("/") + "/oauth/google/callback"
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


@app.post("/campaigns")
def create_campaign(
    user: CurrentUser,
    db: DbDep,
    topic_name: str = Form(...),
    channel_id: int = Form(...),
    total_episodes: int = Form(...),
    language: str = Form("en"),
    system_prompt: str = Form(""),
    voice: str = Form(""),
    rate_pct: int = Form(0),
    subtitle_style: str = Form("word"),
    music_path: str = Form(""),
    posting_slots: str = Form(""),
    ab_testing: bool = Form(False),
    cta: str = Form(""),
):
    # Verify the target channel belongs to the user (tenant isolation).
    channel = db.get(Channel, channel_id)
    if channel is None or channel.user_id != user.id:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    config = {
        "language": language, "system_prompt": system_prompt, "voice": voice or None,
        "rate_pct": rate_pct, "subtitle_style": subtitle_style, "music_path": music_path or None,
        "posting_slots": [s.strip() for s in posting_slots.split(",") if s.strip()],
        "ab_testing": ab_testing, "cta": cta or None,
    }
    campaign = Campaign(
        user_id=user.id, channel_id=channel_id, topic_name=topic_name,
        total_episodes=total_episodes, status=CampaignStatus.pending, config_json=config,
    )
    db.add(campaign)
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


# ── Asset Pool Cache ─────────────────────────────────────────────────────────
@app.get("/assets", response_class=HTMLResponse)
def assets_page(request: Request, user: CurrentUser, db: DbDep):
    items = db.scalars(
        select(BufferPoolItem)
        .join(Campaign, BufferPoolItem.campaign_id == Campaign.id)
        .where(Campaign.user_id == user.id)
        .order_by(BufferPoolItem.id.desc())
    ).all()
    return templates.TemplateResponse(
        request, "assets.html", {"request": request, "user": user, "items": items, "nav": "assets"}
    )


# ── Real-Time Task Logs ──────────────────────────────────────────────────────
@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, user: CurrentUser):
    return templates.TemplateResponse(request, "tasks.html", {"request": request, "user": user, "nav": "tasks"})


@app.get("/api/tasks")
def api_tasks(user: CurrentUser, db: DbDep):
    rows = db.scalars(
        select(Task).where(Task.user_id == user.id).order_by(Task.id.desc()).limit(50)
    ).all()
    out = []
    for t in rows:
        # Live % comes from Redis while running; fall back to the durable column.
        live = task_queue.get_progress(t.id) if t.status.value not in ("COMPLETED", "FAILED") else t.progress_pct
        out.append({
            "id": t.id, "campaign_id": t.campaign_id, "episode": t.episode_number,
            "status": t.status.value, "progress": round(live or t.progress_pct, 1),
            "error": t.error_message, "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        })
    return {"tasks": out}
