"""Data-driven channel autopilot — the decision engine.

Phase I (this surface) is read-only: it classifies each campaign against its channel's OWN retention
baseline, so the operator (and, in later phases, the acting loop) can see at a glance what is winning,
what is healthy, and what is dragging. No AI calls, no side effects — pure functions over the stats
already collected into the DB. Later phases layer the acting loop (AI review / publish / retry /
catch-up) and the weekly strategist on top of these same signals.

The bar is the channel's own average retention (avg % viewed) — a campaign is judged relative to its
sibling campaigns on the same channel, not against an absolute number, so a "good" score adapts to
each channel's niche and audience.
"""
from __future__ import annotations

from sqlalchemy import select

from database.models import Campaign, Task

# Minimum measured episodes before a verdict is trustworthy — below this a campaign is "too early".
MIN_MEASURED = 3
WINNER_RATIO = 1.15    # ≥115% of the channel baseline retention → a winner
LAGGARD_RATIO = 0.6    # <60% of the channel baseline → underperforming

# label → (icon, short name, one-line meaning). Winner uses 🚀 (NOT 🏆 — that marks the single best
# episode on the Performance card; these are campaign-level verdicts).
CLASSIFICATIONS: dict[str, tuple[str, str, str]] = {
    "winner": ("🚀", "Winner", "beats the channel average retention"),
    "healthy": ("✅", "Healthy", "performing around the channel average"),
    "underperforming": ("📉", "Underperforming", "well below the channel average"),
    "too_early": ("🌱", "Too early", "not enough measured episodes to judge yet"),
    "unmeasured": ("·", "No data", "no measured episodes yet"),
}


def _label(rets: list[float], baseline: float | None) -> dict:
    """Turn a campaign's measured retentions + the channel baseline into a classification dict."""
    measured = len(rets)
    retention = round(sum(rets) / len(rets), 1) if rets else None
    if measured == 0:
        label = "unmeasured"
    elif measured < MIN_MEASURED:
        label = "too_early"
    elif baseline is None:
        label = "healthy"  # no channel basis to judge against → assume it's fine
    elif retention >= baseline * WINNER_RATIO:
        label = "winner"
    elif retention < baseline * LAGGARD_RATIO:
        label = "underperforming"
    else:
        label = "healthy"
    icon, name, _meaning = CLASSIFICATIONS[label]
    return {"label": label, "icon": icon, "name": name,
            "retention": retention, "measured": measured, "baseline": baseline}


def _retention(stats) -> float | None:
    return (stats or {}).get("avg_pct_viewed") if stats else None


def classify_campaigns(db, campaigns) -> dict[int, dict]:
    """Classify a set of campaigns (possibly spanning channels) against each one's channel baseline.
    Returns {campaign_id: classification}. Batched: one stats query, baselines computed per channel
    (a channel needs ≥ MIN_MEASURED measured episodes before its baseline is trusted)."""
    if not campaigns:
        return {}
    channel_ids = {c.channel_id for c in campaigns}
    per_campaign: dict[int, list[float]] = {}
    per_channel: dict[int, list[float]] = {}
    for camp_id, chan_id, stats in db.execute(
            select(Task.campaign_id, Campaign.channel_id, Task.stats_json)
            .join(Campaign, Task.campaign_id == Campaign.id)
            .where(Campaign.channel_id.in_(channel_ids))).all():
        r = _retention(stats)
        if r is None:
            continue
        per_channel.setdefault(chan_id, []).append(r)
        per_campaign.setdefault(camp_id, []).append(r)
    baselines = {cid: (round(sum(v) / len(v), 1) if len(v) >= MIN_MEASURED else None)
                 for cid, v in per_channel.items()}
    return {c.id: _label(per_campaign.get(c.id, []), baselines.get(c.channel_id))
            for c in campaigns}


# ── Autopilot config (per-channel, stored in Channel.autopilot_json) ─────────
MODES = ("off", "copilot", "autopilot")
DEFAULT_INTERVAL_HOURS = 3
DEFAULT_APPROVE_MIN = 7   # QC score (/10) at/above which a render is auto-approved (autopilot mode)
DEFAULT_REJECT_MAX = 4    # QC score at/below which a render is auto-rejected (both copilot + autopilot)


def ap_mode(channel) -> str:
    """The channel's autopilot mode ('off' | 'copilot' | 'autopilot'); 'off' if unset/invalid."""
    m = (channel.autopilot_json or {}).get("mode", "off")
    return m if m in MODES else "off"


def ap_interval_seconds(channel) -> int:
    """How often this channel's autopilot may run — operator-configurable (default 3h), clamped 1–24h."""
    h = (channel.autopilot_json or {}).get("interval_hours", DEFAULT_INTERVAL_HOURS)
    try:
        h = int(h)
    except (TypeError, ValueError):
        h = DEFAULT_INTERVAL_HOURS
    return max(1, min(h, 24)) * 3600


def review_thresholds(channel) -> tuple[int, int]:
    """(approve_min, reject_max) QC scores for this channel — how strict its auto-review is."""
    r = (channel.autopilot_json or {}).get("review") or {}
    try:
        lo = int(r.get("reject_max", DEFAULT_REJECT_MAX))
        hi = int(r.get("approve_min", DEFAULT_APPROVE_MIN))
    except (TypeError, ValueError):
        lo, hi = DEFAULT_REJECT_MAX, DEFAULT_APPROVE_MIN
    lo = max(0, min(lo, 10))
    hi = max(lo + 1, min(hi, 10))  # approve threshold always strictly above the reject threshold
    return hi, lo


def review_decision(qc: dict | None, approve_min: int, reject_max: int) -> tuple[str, str]:
    """Decide on a rendered video from its STORED QC verdict — never calls AI (reuses the pipeline's
    vision verdict). Returns (action, reason) where action ∈ 'approve' | 'reject' | 'escalate'.

    A low score or a failed/critical QC → reject (the safe action: a rejection never publishes, and
    the reason teaches the scriptwriter). A high score + passed QC → approve. Anything in between, or
    a render with no machine verdict, → escalate to the operator (a good employee asks when unsure)."""
    if not qc or qc.get("score") is None:
        return ("escalate", "no automatic QC verdict — needs a human eye")
    score = qc.get("score")
    passed = qc.get("passed", True)
    issues = qc.get("issues") or []
    if not passed or score <= reject_max:
        why = "; ".join(issues) if issues else f"low quality score ({score}/10)"
        return ("reject", why[:180])
    if score >= approve_min:
        return ("approve", f"passed auto-QC ({score}/10)")
    return ("escalate", f"borderline auto-QC ({score}/10) — needs a human eye")


def channel_baseline(db, channel_id: int) -> float | None:
    """Average retention across ALL measured episodes of one channel — the bar its campaigns are
    judged against. None until there are ≥ MIN_MEASURED measured episodes."""
    rets = [r for r in (
        _retention(s) for s in db.scalars(
            select(Task.stats_json).join(Campaign, Task.campaign_id == Campaign.id)
            .where(Campaign.channel_id == channel_id)).all())
        if r is not None]
    return round(sum(rets) / len(rets), 1) if len(rets) >= MIN_MEASURED else None
