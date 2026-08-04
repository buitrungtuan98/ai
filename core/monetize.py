"""Monetization progress (ADR-080) — how far each channel is from being paid, in the platforms'
own currencies.

The thresholds are public and fixed; what was missing was the measurement. YouTube pays through the
Partner Program (long route: 1,000 subscribers + 4,000 watch-HOURS trailing 365d; Shorts route:
1,000 subscribers + 10M Shorts views trailing 90d). Facebook's in-stream program asks ~10,000
followers and 600,000 watched minutes over 60 days (its Reels bonus programs are invite-gated — no
number tracks an invite, so none is shown).

Honesty rules baked in:
  * `views_90d` on YouTube is ALL channel views in the window, not only Shorts — labeled as an
    approximation, because Analytics does not split it for free.
  * Facebook's 60-day minutes are summed from OUR published episodes' insights — a lower bound
    (older/other Page videos also count toward the real threshold), labeled as such.
  * No data → no bar. A progress bar over a guess is worse than no bar.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from database.models import Campaign, ChannelSnapshot, Task
from database.types import Platform, TaskStatus

YPP_SUBS = 1_000
YPP_WATCH_HOURS_365D = 4_000
YPP_SHORTS_VIEWS_90D = 10_000_000
FB_FOLLOWERS = 10_000
FB_WATCH_MINUTES_60D = 600_000

MILESTONES = (80, 100)   # % thresholds the autopilot announces, once each


def _pct(have: float | None, need: float) -> int | None:
    if have is None:
        return None
    return min(100, int(100 * have / need))


def channel_progress(db, channel) -> dict | None:
    """The monetization scoreboard for one channel, from the latest daily snapshot (+ episode
    insights on Facebook). None when nothing is measured yet — the card then explains itself."""
    snap = db.scalars(select(ChannelSnapshot).where(ChannelSnapshot.channel_id == channel.id)
                      .order_by(ChannelSnapshot.day.desc()).limit(1)).first()
    if channel.platform == Platform.youtube:
        if snap is None:
            return None
        hours = (snap.watch_minutes_365d or 0) / 60 if snap.watch_minutes_365d is not None else None
        rows = [
            {"key": "subs", "label": "Subscribers", "have": snap.subscribers, "need": YPP_SUBS,
             "pct": _pct(snap.subscribers, YPP_SUBS)},
            {"key": "hours", "label": "Watch hours (365d)", "have": round(hours) if hours is not None else None,
             "need": YPP_WATCH_HOURS_365D, "pct": _pct(hours, YPP_WATCH_HOURS_365D)},
            {"key": "views90", "label": "Views (90d) — Shorts route, approx.",
             "have": snap.views_90d, "need": YPP_SHORTS_VIEWS_90D,
             "pct": _pct(snap.views_90d, YPP_SHORTS_VIEWS_90D)},
        ]
        # Eligible via EITHER route; both routes share the subscriber bar.
        subs_ok = (snap.subscribers or 0) >= YPP_SUBS
        long_ok = hours is not None and hours >= YPP_WATCH_HOURS_365D
        shorts_ok = (snap.views_90d or 0) >= YPP_SHORTS_VIEWS_90D
        # A route is as far along as its WORST bar; the channel is as far along as its best route.
        long_route = min(rows[0]["pct"] or 0, rows[1]["pct"] or 0)
        shorts_route = min(rows[0]["pct"] or 0, rows[2]["pct"] or 0)
        return {"program": "YouTube Partner Program", "rows": rows,
                "eligible": subs_ok and (long_ok or shorts_ok),
                "note": ("Views (90d) counts ALL channel views — the free API does not split "
                         "Shorts out, so the Shorts-route bar is an approximation."),
                "overall_pct": max(long_route, shorts_route)}
    if channel.platform == Platform.facebook:
        followers = snap.subscribers if snap else None
        cutoff = datetime.utcnow() - timedelta(days=60)
        minutes = 0
        measured = False
        for (stats,) in db.execute(
                select(Task.stats_json).join(Campaign, Task.campaign_id == Campaign.id)
                .where(Campaign.channel_id == channel.id, Task.status == TaskStatus.COMPLETED,
                       Task.finished_at >= cutoff, Task.stats_json.isnot(None))).all():
            if (stats or {}).get("minutes_watched") is not None:
                minutes += stats["minutes_watched"]
                measured = True
        rows = [
            {"key": "followers", "label": "Followers", "have": followers, "need": FB_FOLLOWERS,
             "pct": _pct(followers, FB_FOLLOWERS)},
            {"key": "minutes", "label": "Watched minutes (60d) — our episodes only",
             "have": minutes if measured else None, "need": FB_WATCH_MINUTES_60D,
             "pct": _pct(minutes if measured else None, FB_WATCH_MINUTES_60D)},
        ]
        if followers is None and not measured:
            return None
        return {"program": "Facebook in-stream ads", "rows": rows,
                "eligible": (followers or 0) >= FB_FOLLOWERS and minutes >= FB_WATCH_MINUTES_60D,
                "note": ("Minutes are summed from this factory's episodes — a lower bound; the "
                         "Page's other videos also count toward Facebook's real threshold."),
                "overall_pct": min(r["pct"] or 0 for r in rows)}
    return None


def crossed_milestones(progress: dict | None, already: dict) -> list[tuple[str, int]]:
    """Which (program-row, milestone-%) pairs are newly crossed, given what was already announced.
    `already` = {"subs": 80, ...} from channel.autopilot_json — each key announces each level once."""
    out: list[tuple[str, int]] = []
    for row in (progress or {}).get("rows", []):
        pct = row.get("pct")
        if pct is None:
            continue
        for m in MILESTONES:
            if pct >= m and int(already.get(row["key"], 0)) < m:
                out.append((row["key"], m))
    return out
