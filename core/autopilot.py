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

import logging
from datetime import datetime

from sqlalchemy import select

from database.models import AutopilotAction, Campaign, Task

logger = logging.getLogger(__name__)

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

# Retention is the metric every verdict here is built on, and only YouTube gives it away for free.
# The Graph API exposes view counts but no comparable "average percentage viewed", so a Facebook
# channel has no baseline, which silently means EVERY campaign on it grades "healthy" and the
# engine can never call a winner or a laggard (ADR-076). That is a real limit of the free data, not
# a verdict — so it is named, and the UI says it rather than showing confident green.
MEASURES_RETENTION = {"youtube"}


def measurable(channel) -> bool:
    """Can this channel's platform tell us how much of a video people actually watched?"""
    return getattr(channel.platform, "value", channel.platform) in MEASURES_RETENTION


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
    from core.compilation import COMPILATION_EPISODE_BASE

    channel_ids = {c.channel_id for c in campaigns}
    per_campaign: dict[int, list[float]] = {}
    per_channel: dict[int, list[float]] = {}
    for camp_id, chan_id, stats in db.execute(
            select(Task.campaign_id, Campaign.channel_id, Task.stats_json)
            .join(Campaign, Task.campaign_id == Campaign.id)
            # Ordinary episodes only (ADR-085): an 11-minute compilation's retention % is
            # systematically lower than a Short's — one in the baseline mislabels every campaign.
            .where(Campaign.channel_id.in_(channel_ids),
                   Task.episode_number < COMPILATION_EPISODE_BASE)).all():
        r = _retention(stats)
        if r is None:
            continue
        per_channel.setdefault(chan_id, []).append(r)
        per_campaign.setdefault(camp_id, []).append(r)
    baselines = {cid: (round(sum(v) / len(v), 1) if len(v) >= MIN_MEASURED else None)
                 for cid, v in per_channel.items()}
    return {c.id: _label(per_campaign.get(c.id, []), baselines.get(c.channel_id))
            for c in campaigns}


def log_action(db, channel, kind: str, summary: str, *, campaign_id: int | None = None,
               evidence: dict | None = None) -> None:
    """Record ONE decision as a done AutopilotAction — the operator-visible audit trail.

    Lives here, not in the scheduler, because the web layer writes to it too (ADR-089): a campaign
    edit is exactly the kind of "what changed and when" the retry sweep's log entries refer back to,
    and two writers with two definitions is how the drift this codebase keeps fixing starts. Status
    'done' keeps these out of the proposal-idempotency logic. Fail-open: a logging error never breaks
    the caller."""
    try:
        db.add(AutopilotAction(
            user_id=channel.user_id, channel_id=channel.id, campaign_id=campaign_id,
            kind=kind, summary=summary[:300], evidence=evidence or {}, params={},
            status="done", resolved_at=datetime.utcnow()))
        db.commit()
    except Exception:  # noqa: BLE001 — the audit log is a nicety, never a gate
        db.rollback()
        logger.debug("autopilot action log failed", exc_info=True)


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


# The pre-render SCRIPT judge's own ceiling (ADR-089). It used to read `review_thresholds` and use
# the channel's video-QC reject threshold as-is — "one scale, one discipline" (ADR-079 C2). One
# scale, two very different costs: a channel tightened for finished VIDEO ("Reject at QC ≤ 7")
# silently demanded 8/10 from every script, and a good draft scores 7 with two style notes, so that
# channel could not render at all — draft blocked, the single regenerate blocked, episode failed,
# every time, with the operator reading "too repetitive or generic" about a script nobody thought
# was either. Video strictness and scriptwriting strictness are not one dial. A channel LOOSER than
# the default keeps its own value; a stricter one is clamped to the default here.
SCRIPT_JUDGE_REJECT_MAX = 4


def script_judge_reject_max(channel) -> int:
    """The score at/below which a pre-render script is rejected — never stricter than the default."""
    _approve_min, reject_max = review_thresholds(channel)
    return min(reject_max, SCRIPT_JUDGE_REJECT_MAX)


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


# ── Strategy proposals (Phase III): reversible, evidence-backed, deterministic ─
# A dismissal is a DECISION, not a scheduling accident: no proposer — deterministic or council —
# re-files the same (campaign, kind) until this cooldown passes (ADR-086). Lives here because both
# the scheduler's proposer and the council import this module; two copies had already diverged
# (the council had no cooldown at all and could nag daily with a rejected idea).
REPROPOSE_AFTER_DAYS = 30

EXTEND_AT_PCT = 0.8        # a campaign this far through its run is "near its cap"
EXTEND_BY = 0.25           # extend a winner by +25% episodes
WIND_DOWN_CONSECUTIVE = 5  # this many straight measured episodes below the bar → wind down
FLOP_BREAKER_CONSECUTIVE = 3  # straight first-day flops → propose the pause days before retention can


def _trailing_low(tasks, threshold: float) -> int:
    """Count the most-recent MEASURED episodes that are below `threshold`, consecutively from newest
    (stops at the first measured episode that clears the bar)."""
    n = 0
    for t in sorted(tasks, key=lambda x: x.episode_number, reverse=True):
        r = _retention(t.stats_json)
        if r is None:
            continue
        if r < threshold:
            n += 1
        else:
            break
    return n


def propose_actions(campaign, tasks, verdict: dict) -> list[dict]:
    """Deterministic strategy proposals for ONE active campaign, from its classification + measured
    history. Pure (no DB writes, no AI). Each is a bounded, reversible change: extend a winner near
    its cap, plan a successor for a healthy campaign near its cap, or wind a laggard down (which only
    stops NEW episodes — nothing is ever deleted). Returns [{kind, summary, evidence, params}]."""
    if campaign.status.value != "active":
        return []
    # Ordinary episodes only (ADR-085). Sentinel numbers sort NEWEST, so an unfiltered compilation
    # sat permanently at the head of `_trailing_low` and the flop streak: one judged "fine" masked
    # the breaker forever; one judged "flop" inflated the streak with a video that isn't a Short.
    from core.compilation import ordinary_episodes

    tasks = ordinary_episodes(tasks)
    total = campaign.total_episodes or 0
    prog = (campaign.current_episode / total) if total else 0.0
    label, baseline, ret = verdict["label"], verdict.get("baseline"), verdict.get("retention")
    out: list[dict] = []
    if total and prog >= EXTEND_AT_PCT:
        if label == "winner":
            new_total = total + max(1, round(total * EXTEND_BY))
            out.append({"kind": "extend",
                        "summary": f"Extend “{campaign.topic_name}” to {new_total} episodes — it's a "
                                   f"winner near its cap ({ret}% vs {baseline}% channel avg).",
                        "evidence": {"retention": ret, "baseline": baseline,
                                     "progress_pct": round(prog * 100)},
                        "params": {"total_episodes": new_total}})
        elif label == "healthy":
            out.append({"kind": "successor",
                        "summary": f"Plan a successor to “{campaign.topic_name}” — it's near its cap; "
                                   f"carry its formula into a fresh campaign for review.",
                        "evidence": {"retention": ret, "baseline": baseline,
                                     "progress_pct": round(prog * 100)},
                        "params": {}})
    if label == "underperforming" and baseline is not None:
        low = _trailing_low(tasks, baseline * LAGGARD_RATIO)
        if low >= WIND_DOWN_CONSECUTIVE:
            out.append({"kind": "wind_down",
                        "summary": f"Wind down “{campaign.topic_name}” — {low} straight episodes well "
                                   f"below the channel average. Stops new episodes; nothing is deleted.",
                        "evidence": {"consecutive_low": low, "retention": ret, "baseline": baseline},
                        "params": {"total_episodes": campaign.current_episode}})
    # The flop breaker (ADR-079): first-day views fail days before retention can. Three straight
    # measured flops is the campaign telling us its current angle is dead — propose stopping NEW
    # episodes now, not after retention confirms it in a week. Same reversible action as the
    # retention path (kind idempotency keeps it to one live wind_down proposal per campaign);
    # deciding to stop stays the operator's (or full-auto's) explicit call — never automatic here.
    if not any(p["kind"] == "wind_down" for p in out):
        from core import flop as _flop

        streak = 0
        for t in sorted(tasks, key=lambda x: x.episode_number, reverse=True):
            verdict = (t.stats_json or {}).get("flop")
            if verdict is None:
                continue
            if verdict:
                streak += 1
            else:
                break
        if streak >= FLOP_BREAKER_CONSECUTIVE and _flop.campaign_median_24h(tasks) is not None:
            out.append({"kind": "wind_down",
                        "summary": f"Pause “{campaign.topic_name}” — its last {streak} measured "
                                   "episodes all flopped on first-day views. Stop new episodes and "
                                   "pivot the angle; nothing is deleted, extend re-opens it.",
                        "evidence": {"consecutive_flops": streak},
                        "params": {"total_episodes": campaign.current_episode}})
    return out


# ── Audience-geography verification (K3): are we reaching the target country? ─
# The primary viewer countries we'd expect for each profile language. Broad for English (it
# legitimately spans several countries) so a "mismatch" only fires on a clear signal problem.
LANG_COUNTRIES: dict[str, set[str]] = {
    "vi": {"VN"},
    "en": {"US", "GB", "CA", "AU", "IE", "NZ"},
    "es": {"ES", "MX", "AR", "CO", "CL", "PE", "VE"},
}
AUDIENCE_MIN_MEASURED = 3  # need this many geo-measured episodes before judging alignment


def audience_summary(tasks, profile: dict | None) -> dict | None:
    """Aggregate measured episodes' top-viewer country into one audience verdict for a campaign/
    channel: the dominant country, its average share of views, and whether it matches the profile
    language's expected countries (None if there's no profile language to judge against). Returns
    None until at least one episode has geography data."""
    from collections import Counter

    counts: Counter = Counter()
    pcts: list[int] = []
    for t in tasks:
        s = t.stats_json or {}
        c = s.get("top_country")
        if c:
            counts[c] += 1
            if s.get("top_country_pct") is not None:
                pcts.append(s["top_country_pct"])
    if not counts:
        return None
    country, _n = counts.most_common(1)[0]
    lang = (profile or {}).get("language")
    expected = LANG_COUNTRIES.get(lang)
    return {"country": country,
            "pct": round(sum(pcts) / len(pcts)) if pcts else None,
            "match": (country in expected) if expected else None,
            "measured": sum(counts.values())}


def channel_baseline(db, channel_id: int) -> float | None:
    """Average retention across a channel's measured ORDINARY episodes — the bar its campaigns are
    judged against (compilations excluded, ADR-085). None until ≥ MIN_MEASURED measured episodes."""
    from core.compilation import COMPILATION_EPISODE_BASE

    rets = [r for r in (
        _retention(s) for s in db.scalars(
            select(Task.stats_json).join(Campaign, Task.campaign_id == Campaign.id)
            .where(Campaign.channel_id == channel_id,
                   Task.episode_number < COMPILATION_EPISODE_BASE)).all())
        if r is not None]
    return round(sum(rets) / len(rets), 1) if len(rets) >= MIN_MEASURED else None
