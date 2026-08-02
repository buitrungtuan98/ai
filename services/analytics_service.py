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

    from services.facebook_service import GRAPH, FacebookAuthError, raise_for_graph
    from services.facebook_service import scrub as _scrub

    data = _json.loads(channel.encrypted_credentials or "{}")
    token = data.get("page_access_token")
    if not token:
        return {}
    ids = video_ids[:50]
    if not ids:
        return {}
    # ONE batched Graph call instead of one request per video (ADR-073). Fifty round trips every
    # stats pass was fifty chances to be rate-limited and fifty times the latency, for data that
    # arrives in a single response. Graph caps a batch at 50, which is the cap already applied above.
    batch = [{"method": "GET", "relative_url": f"{vid}/video_insights/total_video_views"}
             for vid in ids]
    try:
        resp = requests.post(GRAPH, data={"access_token": token, "batch": _json.dumps(batch)},
                             timeout=60)
        raise_for_graph(resp, token=token, what="Facebook insights")
        results = resp.json() or []
    except FacebookAuthError:
        # A dead token fails identically for every video — let the caller retire the channel rather
        # than log the same failure fifty times (ADR-072).
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("FB insights batch failed: %s", _scrub(str(exc), token))
        return {}
    out: dict[str, dict] = {}
    for vid, item in zip(ids, results):
        # Each entry carries its OWN status code: one video the Page can no longer read must not
        # discard the other forty-nine.
        if not isinstance(item, dict) or item.get("code") != 200:
            logger.warning("FB insights unavailable for video %s", vid)
            continue
        try:
            rows = (_json.loads(item.get("body") or "{}") or {}).get("data", [])
            views = rows[0]["values"][0]["value"] if rows else 0
            out[vid] = {"views": int(views)}
        except Exception:  # noqa: BLE001 — an unexpected shape for one video, not for all of them
            logger.warning("FB insights unreadable for video %s", vid)
    return out


EARLY_REFRESH_MINUTES = 55  # young videos (< MIN_AGE_HOURS) get near-real-time views ~hourly


def fetch_youtube_early_stats(channel: Channel, video_ids: list[str]) -> dict[str, dict]:
    """Return {video_id: {views, likes, comments}} via the YouTube **Data** API (v3
    `videos.list?part=statistics`) — near-real-time (minutes, not the ~2-day Analytics lag) and cheap
    (50 ids/call, 1 quota unit). Retention is NOT available here; that's Analytics-only."""
    from googleapiclient.discovery import build

    from services.youtube_service import build_credentials

    creds = build_credentials(channel)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        batch = [v for v in video_ids[i:i + 50] if v]
        if not batch:
            continue
        resp = yt.videos().list(part="statistics", id=",".join(batch), maxResults=50).execute()
        for item in resp.get("items", []) or []:
            s = item.get("statistics", {}) or {}
            out[item["id"]] = {"views": int(s.get("viewCount", 0)),
                               "likes": int(s.get("likeCount", 0)),
                               "comments": int(s.get("commentCount", 0))}
    return out


def collect_early_stats(db, now: datetime | None = None) -> int:
    """Near-real-time views/likes for YOUNG episodes (published < MIN_AGE_HOURS) — the window the
    Analytics API is still blind in. Merges {views, likes, early, early_fetched_at} into stats_json
    WITHOUT touching retention fields (a separate `early_fetched_at` clock, so this never starves the
    retention refresh). Never sets `avg_pct_viewed`, so autopilot + the playbook ignore it (T4).
    Returns tasks updated. Best-effort: a channel failure is skipped, never fatal."""
    now = now or datetime.utcnow()
    tasks = db.scalars(
        select(Task).where(
            Task.status == TaskStatus.COMPLETED,
            Task.published_video_id.isnot(None),
            Task.finished_at > now - timedelta(hours=MIN_AGE_HOURS),  # too young for retention
        )
    ).all()
    due = [
        t for t in tasks
        if not (t.stats_json or {}).get("early_fetched_at")
        or datetime.fromisoformat(t.stats_json["early_fetched_at"])
        <= now - timedelta(minutes=EARLY_REFRESH_MINUTES)
    ]
    if not due:
        return 0
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
        try:
            if channel.platform == Platform.youtube:
                stats = fetch_youtube_early_stats(channel, ids)
            else:
                stats = fetch_facebook_stats(channel, ids)
        except Exception:  # noqa: BLE001 — early stats must never break the factory
            logger.warning("Early-stats fetch failed for channel %s", channel_id, exc_info=True)
            continue
        for t in channel_tasks:
            if t.published_video_id not in stats:
                continue
            # Merge onto whatever is there; never clobber retention. `early` = no retention yet.
            merged = {**(t.stats_json or {}), **stats[t.published_video_id],
                      "early_fetched_at": now.isoformat()}
            merged["early"] = merged.get("avg_pct_viewed") is None
            t.stats_json = merged
            updated += 1
    if updated:
        db.commit()
        logger.info("collect_early_stats updated %d young episode(s)", updated)
    return updated


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


# ── Channel-level growth series (ADR-063) ────────────────────────────────────
# Per-episode stats answer "did this video work?". They can NEVER answer "is publishing this much
# actually growing the channel?", because a channel's totals are not the sum of the episodes we made
# (older videos, feed spillover, content we didn't publish). So the channel's own totals are sampled
# once a day into `ChannelSnapshot`, and the deltas between days are the growth curve.
def fetch_youtube_channel_totals(channel: Channel) -> dict | None:
    """{subscribers, views, videos} for a YouTube channel via ONE Data-API call, or None.

    `hiddenSubscriberCount` is honoured — a channel that hides its count reports None rather than 0,
    so a hidden channel never renders as "0 subscribers, no growth"."""
    from googleapiclient.discovery import build

    from services.youtube_service import build_credentials

    creds = build_credentials(channel)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = youtube.channels().list(part="statistics", mine=True).execute().get("items") or []
    if not items:
        return None
    stats = items[0].get("statistics") or {}
    hidden = str(stats.get("hiddenSubscriberCount", "false")).lower() == "true"
    return {
        "subscribers": None if hidden else int(stats.get("subscriberCount", 0)),
        "views": int(stats.get("viewCount", 0)),
        "videos": int(stats.get("videoCount", 0)),
    }


def fetch_facebook_page_totals(channel: Channel) -> dict | None:
    """{subscribers (followers), views (None), videos (None)} for a Facebook Page, or None.

    The Graph API exposes a follower count cheaply but no lifetime page view total comparable to
    YouTube's, so `views` stays None instead of inventing a number that means something else."""
    import requests

    from services.facebook_service import GRAPH, _load, raise_for_graph

    page_id, token = _load(channel)
    resp = requests.get(
        f"{GRAPH}/{page_id}",
        params={"fields": "followers_count,fan_count", "access_token": token}, timeout=20)
    raise_for_graph(resp, token=token, what="Facebook page")
    data = resp.json()
    followers = data.get("followers_count", data.get("fan_count"))
    return {"subscribers": int(followers) if followers is not None else None,
            "views": None, "videos": None}


def fetch_channel_totals(channel: Channel) -> dict | None:
    if channel.platform == Platform.youtube:
        return fetch_youtube_channel_totals(channel)
    if channel.platform == Platform.facebook:
        return fetch_facebook_page_totals(channel)
    return None


def _channel_local_day(channel: Channel, now: datetime | None = None):
    """Today on the CHANNEL's own clock (its profile timezone, else the server default), so a
    snapshot lands on the day the operator would call it."""
    from zoneinfo import ZoneInfo

    from core.config import settings

    tz_name = (channel.profile_json or {}).get("timezone") or settings.TIMEZONE
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — a bad stored zone must not skip collection
        tz = ZoneInfo("UTC")
    return (now or datetime.utcnow()).replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()


def collect_channel_snapshots(db, now: datetime | None = None) -> int:
    """Sample every channel's totals once per local day. Returns rows written.

    Safe to call as often as the tick runs: the day row is checked first and `uq_channel_snapshot_day`
    is the backstop, so extra calls cost one cheap SELECT and no API quota. Best-effort per channel —
    one revoked token must not stop the others."""
    from database.models import ChannelSnapshot
    from database.types import ChannelStatus
    from services.facebook_service import FacebookAuthError
    from services.facebook_service import scrub as _scrub

    written = 0
    for channel in db.scalars(select(Channel)).all():
        day = _channel_local_day(channel, now)
        if db.scalar(select(ChannelSnapshot).where(
                ChannelSnapshot.channel_id == channel.id, ChannelSnapshot.day == day).limit(1)):
            continue  # already sampled today — don't spend a call
        try:
            totals = fetch_channel_totals(channel)
        except FacebookAuthError as exc:
            # The token is dead, not the network: retire the channel so the UI stops claiming it
            # works, instead of failing this same call every hour forever (ADR-072).
            if channel.status != ChannelStatus.expired:
                channel.status = ChannelStatus.expired
                db.commit()
                logger.warning("Channel %s marked expired during snapshot: %s", channel.id, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — a dead token/quota must not break the pass
            # `exc_info` is deliberately absent: a raw requests traceback embeds the Graph URL, and
            # that URL carries the access token straight into the log file (ADR-072).
            logger.warning("channel-totals fetch failed for channel %s: %s",
                           channel.id, _scrub(str(exc)))
            continue
        if not totals:
            continue
        db.add(ChannelSnapshot(channel_id=channel.id, day=day, **totals))
        try:
            db.commit()
            written += 1
        except Exception:  # noqa: BLE001 — a concurrent tick won the unique constraint; fine
            db.rollback()
    return written


def channel_growth(db, channel_id: int, days: int = 30, now: datetime | None = None) -> dict:
    """The growth series for ONE channel, ready to plot: per-day subscriber/view deltas next to how
    many episodes that channel published that day — the correlation the operator actually wants
    ("does publishing more move the needle?").

    Deltas need two consecutive samples, so the first collected day contributes a point with no delta
    (reported as None, never as 0 — "unknown" and "flat" must not look alike).
    """
    from datetime import timedelta

    from database.models import ChannelSnapshot

    end = (now or datetime.utcnow()).date()
    start = end - timedelta(days=days)
    rows = db.scalars(
        select(ChannelSnapshot)
        .where(ChannelSnapshot.channel_id == channel_id, ChannelSnapshot.day >= start)
        .order_by(ChannelSnapshot.day)).all()
    # Episodes published per day for this channel (a Task's channel comes via its campaign).
    published: dict = {}
    for (finished,) in db.execute(
            select(Task.finished_at)
            .join(Campaign, Task.campaign_id == Campaign.id)
            .where(Campaign.channel_id == channel_id, Task.status == TaskStatus.COMPLETED,
                   Task.finished_at.isnot(None))).all():
        published[finished.date()] = published.get(finished.date(), 0) + 1
    points, prev = [], None
    for row in rows:
        points.append({
            "day": row.day,
            "subscribers": row.subscribers,
            "views": row.views,
            "sub_delta": (row.subscribers - prev.subscribers
                          if prev is not None and row.subscribers is not None
                          and prev.subscribers is not None else None),
            "view_delta": (row.views - prev.views
                           if prev is not None and row.views is not None
                           and prev.views is not None else None),
            "published": published.get(row.day, 0),
        })
        prev = row
    latest = rows[-1] if rows else None
    span_subs = ([p["sub_delta"] for p in points if p["sub_delta"] is not None])
    span_views = ([p["view_delta"] for p in points if p["view_delta"] is not None])
    return {
        "points": points,
        "subscribers": latest.subscribers if latest else None,
        "views": latest.views if latest else None,
        "videos": latest.videos if latest else None,
        "sub_growth": sum(span_subs) if span_subs else None,
        "view_growth": sum(span_views) if span_views else None,
        "published": sum(p["published"] for p in points),
        "days": len(points),
        # Two samples are the minimum for any delta at all — say so instead of drawing a flat line.
        "measurable": len(points) >= 2,
    }
