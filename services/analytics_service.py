"""Performance stats collection — the measurement half of the self-improvement loop.

Pulls per-video metrics (retention % is the king metric for Shorts, then views/likes) from the
free YouTube Analytics API and Facebook video insights, and stores them on each Task's
`stats_json`. Everything is best-effort: a failed fetch logs and moves on — stats never break
the factory. Requires the `yt-analytics.readonly` scope on YouTube channels (channels connected
before this feature need a one-click reconnect).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from core import retention
from database.models import Campaign, Channel, Task
from database.types import Platform, TaskStatus

logger = logging.getLogger(__name__)

MIN_AGE_HOURS = 48        # Shorts stats are meaningless before ~2 days
MAX_AGE_DAYS = 30         # stop refreshing month-old episodes
REFRESH_HOURS = 24        # re-fetch at most daily


def fetch_youtube_stats(channel: Channel, video_ids: list[str]) -> dict[str, dict]:
    """Return {video_id: {views, avg_pct_viewed, likes}} via the YouTube Analytics API."""
    from googleapiclient.discovery import build

    from services.youtube_service import build_credentials

    creds = build_credentials(channel)
    analytics = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    resp = analytics.reports().query(
        ids="channel==MINE",
        startDate="2020-01-01",
        endDate=datetime.utcnow().strftime("%Y-%m-%d"),
        metrics="views,likes,averageViewedPercentage",
        dimensions="video",
        filters="video==" + ",".join(video_ids[:200]),
        maxResults=200,
    ).execute()
    out: dict[str, dict] = {}
    for row in resp.get("rows", []) or []:
        vid, views, likes, avg_pct = row[0], row[1], row[2], row[3]
        out[vid] = {"views": int(views), "likes": int(likes), "avg_pct_viewed": round(float(avg_pct), 1)}
    return out


def fetch_youtube_geography(channel: Channel, video_ids: list[str]) -> dict[str, dict]:
    """Return {video_id: {top_country, top_country_pct}} — the single biggest viewer country per
    video and its share of views (YouTube Analytics, dimensions video+country). Powers audience-match
    verification (ADR-045): are we actually reaching the country the channel targets?"""
    from googleapiclient.discovery import build

    from services.youtube_service import build_credentials

    creds = build_credentials(channel)
    analytics = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    resp = analytics.reports().query(
        ids="channel==MINE", startDate="2020-01-01",
        endDate=datetime.utcnow().strftime("%Y-%m-%d"),
        metrics="views", dimensions="video,country",
        filters="video==" + ",".join(video_ids[:200]), maxResults=1000,
    ).execute()
    by_video: dict[str, list[tuple[str, int]]] = {}
    for row in resp.get("rows", []) or []:
        vid, country, views = row[0], row[1], int(row[2])
        by_video.setdefault(vid, []).append((country, views))
    out: dict[str, dict] = {}
    for vid, pairs in by_video.items():
        total = sum(v for _, v in pairs) or 1
        top_country, top_views = max(pairs, key=lambda x: x[1])
        out[vid] = {"top_country": top_country, "top_country_pct": round(100 * top_views / total)}
    return out


def fetch_youtube_retention(channel: Channel, video_ids: list[str]) -> dict[str, list]:
    """Return {video_id: [[pos, watch_ratio], …]} — the free second-by-second retention curve
    (`elapsedVideoTimeRatio` × `audienceWatchRatio`). This dimension is per-video, so each video is
    one small query; a failure on one video is skipped, never fatal. Bounded to keep the pass cheap."""
    from googleapiclient.discovery import build

    from services.youtube_service import build_credentials

    creds = build_credentials(channel)
    analytics = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    end = datetime.utcnow().strftime("%Y-%m-%d")
    out: dict[str, list] = {}
    for vid in video_ids[:50]:
        try:
            resp = analytics.reports().query(
                ids="channel==MINE", startDate="2020-01-01", endDate=end,
                metrics="audienceWatchRatio", dimensions="elapsedVideoTimeRatio",
                filters=f"video=={vid}", maxResults=200,
            ).execute()
            curve = [[round(float(r[0]), 4), round(float(r[1]), 4)]
                     for r in (resp.get("rows") or []) if len(r) >= 2]
            if curve:
                out[vid] = curve
        except Exception:  # noqa: BLE001 — one video's curve missing must not drop the rest
            logger.debug("Retention curve fetch failed for video %s", vid, exc_info=True)
    return out


def fetch_facebook_stats(channel: Channel, video_ids: list[str]) -> dict[str, dict]:
    """Return {video_id: {views}} via the Graph API (FB exposes less than YouTube)."""
    import json as _json

    import requests

    data = _json.loads(channel.encrypted_credentials or "{}")
    token = data.get("page_access_token")
    if not token:
        return {}
    out: dict[str, dict] = {}
    for vid in video_ids[:50]:
        try:
            resp = requests.get(
                f"https://graph.facebook.com/v20.0/{vid}/video_insights/total_video_views",
                params={"access_token": token}, timeout=20,
            )
            resp.raise_for_status()
            rows = resp.json().get("data", [])
            views = rows[0]["values"][0]["value"] if rows else 0
            out[vid] = {"views": int(views)}
        except Exception:  # noqa: BLE001
            logger.warning("FB insights failed for video %s", vid)
    return out


def collect_stats(db, now: datetime | None = None) -> int:
    """Fetch/refresh stats for eligible published episodes. Returns how many tasks were updated."""
    now = now or datetime.utcnow()
    tasks = db.scalars(
        select(Task).where(
            Task.status == TaskStatus.COMPLETED,
            Task.published_video_id.isnot(None),
            Task.finished_at <= now - timedelta(hours=MIN_AGE_HOURS),
            Task.finished_at >= now - timedelta(days=MAX_AGE_DAYS),
        )
    ).all()
    due = [
        t for t in tasks
        if not t.stats_json
        or datetime.fromisoformat(t.stats_json.get("fetched_at", "2000-01-01T00:00:00"))
        <= now - timedelta(hours=REFRESH_HOURS)
    ]
    if not due:
        return 0

    # Group by channel so each platform is called once per channel.
    by_channel: dict[int, list[Task]] = {}
    campaigns = {c.id: c for c in db.scalars(select(Campaign)).all()}
    for t in due:
        campaign = campaigns.get(t.campaign_id)
        if campaign:
            by_channel.setdefault(campaign.channel_id, []).append(t)

    updated = 0
    for channel_id, channel_tasks in by_channel.items():
        channel = db.get(Channel, channel_id)
        if channel is None:
            continue
        ids = [t.published_video_id for t in channel_tasks]
        curves: dict[str, list] = {}
        try:
            if channel.platform == Platform.youtube:
                stats = fetch_youtube_stats(channel, ids)
                try:  # geography is a bonus signal — never let it block the core stats
                    geo = fetch_youtube_geography(channel, ids)
                    for vid, g in geo.items():
                        if vid in stats:
                            stats[vid].update(g)
                except Exception:  # noqa: BLE001
                    logger.warning("Geography fetch failed for channel %s", channel_id)
                try:  # retention curve → drop-off analysis; also a bonus, never fatal
                    curves = fetch_youtube_retention(channel, ids)
                except Exception:  # noqa: BLE001
                    logger.warning("Retention fetch failed for channel %s", channel_id)
            else:
                stats = fetch_facebook_stats(channel, ids)
        except Exception:  # noqa: BLE001 — stats must never break the factory
            logger.warning("Stats fetch failed for channel %s", channel_id, exc_info=True)
            continue
        for t in channel_tasks:
            if t.published_video_id not in stats:
                continue
            entry = {**stats[t.published_video_id], "fetched_at": now.isoformat()}
            curve = curves.get(t.published_video_id)
            scenes = (t.render_json or {}).get("scenes")
            if curve:
                entry["retention_curve"] = curve
                if scenes:  # attribute the biggest drop to a scene — the actionable signal
                    summary = retention.summarize_drop(curve, scenes)
                    if summary:
                        entry["drop_summary"] = summary
            t.stats_json = entry
            updated += 1
    if updated:
        db.commit()
        logger.info("collect_stats updated %d episode(s)", updated)
    return updated
