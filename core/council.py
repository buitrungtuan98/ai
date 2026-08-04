"""The strategy council (ADR-081) — Gemini reads the evidence and decides; rails keep it honest.

The deterministic autopilot classifies and proposes from fixed rules; this layer adds the judgment a
channel manager brings: weighing flops against retention against the monetization goal, and writing
WHY in plain language. The architecture is the map-reduce the operator asked for, with one boundary
that is not negotiable:

    code computes every number  →  the model interprets and chooses  →  code validates the choice

  * D1 `evidence_pack` — all metrics computed deterministically (0 AI). The model never measures.
  * D2 `run_council` — ONE structured Gemini call per channel per day over the pack, returning
    ranked decisions from a CLOSED action menu, each with a reason, cited evidence and confidence.
    Cached on the pack hash: unchanged data is never re-judged.
  * D3 rails — a decision is filed as an ordinary AutopilotAction proposal only if it survives
    validation: known action, live campaign, params in bounds, and every big number in its prose
    present in the pack (an LLM that invents a statistic is cut off at this line). A decision the
    rails refuse is logged and dropped; the deterministic autopilot underneath keeps running either
    way — the council is a strategy layer, never the safety net (ADR-076).

The council's verdict text is remembered (`autopilot_json["council"]`) and its last decisions ride
into the next pack, so it stays consistent with itself instead of re-litigating daily.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select

from core import autopilot, flop, monetize
from database.models import AutopilotAction, BufferPoolItem, Campaign, Task
from database.types import CampaignStatus, TaskStatus

logger = logging.getLogger(__name__)

# The CLOSED action menu (compile = ADR-082's best-of long-form build).
ALLOWED_ACTIONS = ("extend", "wind_down", "successor", "tune", "slot_change", "compile", "hold")
MAX_DECISIONS = 4
EXTEND_CAP = 1.5                  # an extend may grow a campaign by at most +50%
SLOT_CHANGE_COOLDOWN_DAYS = 7     # one applied slot change per campaign per week
TUNE_KEYS = ("caption_theme", "music_mood", "rate_pct")
HALLUCINATION_MIN = 10            # numbers ≥ this in the prose must exist in the pack


class CouncilDecision(BaseModel):
    action: str = Field(description="one of: " + ", ".join(ALLOWED_ACTIONS))
    campaign_id: int
    reason: str = Field(min_length=20, max_length=500,
                        description="Plain language, in the channel's language, citing the numbers")
    evidence: list[str] = Field(min_length=1, max_length=6,
                                description="The specific facts from the pack this rests on")
    confidence: float = Field(ge=0.0, le=1.0)
    params: dict = Field(default_factory=dict)


class CouncilVerdict(BaseModel):
    channel_summary: str = Field(min_length=20, max_length=600,
                                 description="What is working, what is failing, what to watch")
    decisions: list[CouncilDecision] = Field(max_length=MAX_DECISIONS)
    watching: list[str] = Field(default_factory=list, max_length=5)


# ── D1: the evidence pack (deterministic, 0 AI) ──────────────────────────────
def _publish_hour_table(tasks, campaign) -> dict[str, dict]:
    """{hour: {"n": episodes, "avg_views_24h": x}} — the golden-hour evidence."""
    from services.analytics_service import _publish_hour

    by_hour: dict[int, list[int]] = {}
    for t in tasks:
        v = (t.stats_json or {}).get("views_24h")
        if v is None:
            continue
        h = _publish_hour(t, campaign)
        if h is not None:
            by_hour.setdefault(h, []).append(v)
    return {f"{h:02d}:00": {"n": len(vs), "avg_views_24h": round(sum(vs) / len(vs))}
            for h, vs in sorted(by_hour.items())}


def _variant_table(tasks) -> dict[str, dict]:
    by_v: dict[str, list[float]] = {}
    for t in tasks:
        r = (t.stats_json or {}).get("avg_pct_viewed")
        if t.ab_variant and r is not None:
            by_v.setdefault(t.ab_variant, []).append(r)
    return {v: {"n": len(rs), "avg_retention": round(sum(rs) / len(rs), 1)}
            for v, rs in sorted(by_v.items())}


def _consecutive_flops(tasks) -> int:
    n = 0
    for t in sorted(tasks, key=lambda x: x.episode_number, reverse=True):
        verdict = (t.stats_json or {}).get("flop")
        if verdict is None:
            continue
        if verdict:
            n += 1
        else:
            break
    return n


def evidence_pack(db, channel) -> dict:
    """Everything the council may reason from, computed by code. If a number is not in here, the
    council does not know it — and the rails will refuse a decision that cites it."""
    campaigns = db.scalars(select(Campaign).where(
        Campaign.channel_id == channel.id, Campaign.status == CampaignStatus.active)).all()
    cls = autopilot.classify_campaigns(db, campaigns)
    packs = []
    for c in campaigns:
        tasks = db.scalars(select(Task).where(Task.campaign_id == c.id)).all()
        done = [t for t in tasks if t.status == TaskStatus.COMPLETED]
        # QC verdicts live on the buffer rows (consumed rows persist after publish).
        qc_scores = [((b.metadata_json or {}).get("qc") or {}).get("score")
                     for b in db.scalars(select(BufferPoolItem).where(
                         BufferPoolItem.campaign_id == c.id)
                         .order_by(BufferPoolItem.id.desc()).limit(8)).all()]
        learning = c.learning_json or {}
        cfg = c.config_json or {}
        packs.append({
            "campaign_id": c.id,
            "topic": c.topic_name,
            "episodes": {"published": c.current_episode, "planned": c.total_episodes},
            "classification": cls.get(c.id),
            "flops": {"measured_24h": sum(1 for t in tasks
                                          if (t.stats_json or {}).get("views_24h") is not None),
                      "median_24h": flop.campaign_median_24h(tasks),
                      "total": sum(1 for t in tasks if (t.stats_json or {}).get("flop")),
                      "consecutive_latest": _consecutive_flops(tasks)},
            "publish_hours": _publish_hour_table(done, c),
            "current_slots": cfg.get("posting_slots") or [],
            "ab_variants": _variant_table(done),
            "recent_reject_reasons": (learning.get("reject_reasons") or [])[-3:],
            "recent_flop_notes": (learning.get("flop_notes") or [])[-3:],
            "qc_scores_recent": [s for s in qc_scores if s is not None][-5:],
        })
    profile = channel.profile_json or {}
    recent = db.scalars(select(AutopilotAction).where(
        AutopilotAction.channel_id == channel.id).order_by(AutopilotAction.id.desc()).limit(5)).all()
    return {
        "channel": {"name": channel.channel_name, "platform": channel.platform.value,
                    "language": profile.get("language"), "audience": profile.get("audience"),
                    "measures_retention": autopilot.measurable(channel)},
        "monetization": monetize.channel_progress(db, channel),
        "campaigns": packs,
        "my_recent_decisions": [{"kind": a.kind, "summary": a.summary, "status": a.status}
                                for a in recent],
    }


def pack_hash(pack: dict) -> str:
    """Hash of the EVIDENCE only. `my_recent_decisions` is deliberately excluded: it contains the
    council's own filings, so including it would mean every verdict invalidates its own cache and
    "unchanged data is never re-judged" would be true exactly once."""
    evidence = {k: v for k, v in pack.items() if k != "my_recent_decisions"}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()[:16]


# ── D3: the rails (deterministic, 0 AI) ──────────────────────────────────────
def _numbers_exist_in_pack(decision: CouncilDecision, pack_json: str) -> str | None:
    """The anti-hallucination rail: every number ≥ HALLUCINATION_MIN the model wrote in its reason
    or evidence must literally exist in the pack. The model interprets data; it does not mint it."""
    prose = decision.reason + " " + " ".join(decision.evidence)
    for num in re.findall(r"\d[\d,.]*", prose):
        cleaned = num.rstrip(".,").replace(",", "")
        if not cleaned.isdigit() or int(cleaned) < HALLUCINATION_MIN:
            continue
        if cleaned not in pack_json and num.rstrip(".,") not in pack_json:
            return f"cites a number not in the evidence: {num}"
    return None


def _slot_change_recently_applied(db, campaign_id: int) -> bool:
    cutoff = datetime.utcnow() - timedelta(days=SLOT_CHANGE_COOLDOWN_DAYS)
    row = db.scalars(select(AutopilotAction).where(
        AutopilotAction.campaign_id == campaign_id, AutopilotAction.kind == "slot_change",
        AutopilotAction.status == "applied").order_by(AutopilotAction.id.desc()).limit(1)).first()
    return bool(row and row.resolved_at and row.resolved_at >= cutoff)


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def validate_decision(db, decision: CouncilDecision, pack: dict, pack_json: str) -> str | None:
    """None = sound; else the reason it is refused. Every rule here is a bound the model cannot
    argue its way past — that is the point of having rails instead of trust."""
    if decision.action not in ALLOWED_ACTIONS:
        return f"unknown action {decision.action!r}"
    if decision.action == "hold":
        return None                      # explicitly deciding "no action" is always sound
    camp = next((c for c in pack["campaigns"] if c["campaign_id"] == decision.campaign_id), None)
    if camp is None:
        return "campaign not in the evidence pack (gone or inactive)"
    bad_number = _numbers_exist_in_pack(decision, pack_json)
    if bad_number:
        return bad_number
    p = decision.params or {}
    if decision.action == "extend":
        planned = camp["episodes"]["planned"]
        total = p.get("total_episodes")
        if not isinstance(total, int) or not planned < total <= int(planned * EXTEND_CAP + 1):
            return f"extend out of bounds (planned {planned}, asked {total!r}, cap +50%)"
    elif decision.action == "wind_down":
        # The bound the deterministic proposer also uses: stop new episodes, never delete.
        if p.get("total_episodes") != camp["episodes"]["published"]:
            return "wind_down must set total_episodes to the published count exactly"
    elif decision.action == "tune":
        if not p or not set(p) <= set(TUNE_KEYS):
            return f"tune params must be a subset of {TUNE_KEYS}"
    elif decision.action == "compile":
        from core.compilation import DEFAULT_TOP_N, MIN_EPISODES_TO_COMPILE

        if camp["flops"]["measured_24h"] < MIN_EPISODES_TO_COMPILE:
            return (f"compile needs ≥{MIN_EPISODES_TO_COMPILE} measured episodes "
                    f"(has {camp['flops']['measured_24h']})")
        top_n = p.get("top_n", DEFAULT_TOP_N)
        if not isinstance(top_n, int) or not 4 <= top_n <= 20:
            return f"compile top_n out of bounds (asked {top_n!r}, allowed 4-20)"
    elif decision.action == "slot_change":
        if not (_HHMM.match(str(p.get("from", ""))) and _HHMM.match(str(p.get("to", "")))):
            return "slot_change needs from/to as HH:MM"
        if p["from"] not in camp["current_slots"]:
            return f"slot {p['from']} is not one of the campaign's current slots"
        if p["from"] == p["to"]:
            return "slot_change changes nothing"
        if _slot_change_recently_applied(db, decision.campaign_id):
            return f"a slot change was already applied within {SLOT_CHANGE_COOLDOWN_DAYS} days"
        # Data-driven means the TARGET is measured too, not only well-formatted: the destination
        # (±1h) must be an hour this campaign has real first-day numbers for, and those numbers must
        # beat the hour being abandoned. Without this, "move to 03:00" is a perfectly valid HH:MM
        # the model could argue for — into an hour nothing has ever been measured at.
        hours = camp.get("publish_hours") or {}
        to_h = int(p["to"][:2])
        near = [v["avg_views_24h"] for k, v in hours.items()
                if abs(int(k[:2]) - to_h) <= 1 or abs(int(k[:2]) - to_h) >= 23]  # midnight wrap
        if not near:
            return f"no measured evidence for publishing near {p['to']} — the target hour is a guess"
        from_stats = hours.get(f"{int(p['from'][:2]):02d}:00")
        if from_stats and max(near) <= from_stats["avg_views_24h"]:
            return (f"the target hour ({p['to']}) does not outperform the current slot "
                    f"({p['from']}) in the measured data")
    return None


def _already_proposed(db, campaign_id: int, kind: str) -> bool:
    row = db.scalars(select(AutopilotAction).where(
        AutopilotAction.campaign_id == campaign_id, AutopilotAction.kind == kind)
        .order_by(AutopilotAction.id.desc()).limit(1)).first()
    return bool(row and row.status in ("proposed", "applied"))


# ── D2: the council call ─────────────────────────────────────────────────────
_SYSTEM = (
    "You are the channel manager of a short-form video factory. You are given an evidence pack: "
    "every number in it was measured; nothing else exists. Decide what to do next for this channel "
    "like a careful human manager: weigh flops, retention, QC and the monetization goal, stay "
    "consistent with your own recent decisions, and prefer doing nothing over acting on thin "
    "evidence. Rules you cannot break: use ONLY numbers present in the pack; choose actions ONLY "
    "from the allowed list (use 'hold' for campaigns needing none); every decision names its "
    "evidence; write reasons in the channel's language when one is set, otherwise English, in a "
    "plain, human voice — no jargon, no filler."
)


def run_council(db, channel, *, api_key: str, model: str) -> dict:
    """One judged pass for one channel. Returns {"filed", "refused", "held", "skipped_unchanged"}.
    Raises nothing on its own judgment path — an AI failure surfaces to the caller (the step
    isolation there shortens the cadence, per ADR-076)."""
    from core.ai_engine import generate_structured

    pack = evidence_pack(db, channel)
    if not pack["campaigns"]:
        return {"filed": 0, "refused": 0, "held": 0, "skipped_unchanged": False}
    h = pack_hash(pack)
    state = dict((channel.autopilot_json or {}).get("council") or {})
    if state.get("pack_hash") == h:
        return {"filed": 0, "refused": 0, "held": 0, "skipped_unchanged": True}

    pack_json = json.dumps(pack, ensure_ascii=False, default=str)
    verdict = generate_structured(
        prompt=("EVIDENCE PACK:\n" + pack_json + "\n\nAllowed actions: "
                + ", ".join(ALLOWED_ACTIONS) + ". Decide."),
        schema=CouncilVerdict, api_key=api_key, system_prompt=_SYSTEM,
        model=model, temperature=0.3)   # judgment, not creativity — keep it steady

    filed = refused = held = 0
    for d in verdict.decisions:
        if d.action == "hold":
            held += 1
            continue
        problem = validate_decision(db, d, pack, pack_json)
        if problem:
            refused += 1
            logger.warning("Council decision refused by rails (channel %s, campaign %s, %s): %s",
                           channel.id, d.campaign_id, d.action, problem)
            # The refusal is part of the thinking, and thinking must be visible (ADR-084): "the AI
            # wanted X, the rails said no because Y" was only ever in a log file — the operator
            # asked, reasonably, how they were supposed to trust a brain they cannot watch.
            db.add(AutopilotAction(
                user_id=channel.user_id, channel_id=channel.id,
                campaign_id=d.campaign_id if any(
                    c["campaign_id"] == d.campaign_id for c in pack["campaigns"]) else None,
                kind="refused", status="done", resolved_at=datetime.utcnow(),
                summary=(f"Council wanted “{d.action}” — rails refused: {problem}")[:300],
                evidence={"wanted": d.action, "refused_because": problem,
                          "council_reason": d.reason[:200], "confidence": d.confidence}))
            db.commit()
            continue
        if _already_proposed(db, d.campaign_id, d.action):
            held += 1
            continue
        db.add(AutopilotAction(
            user_id=channel.user_id, channel_id=channel.id, campaign_id=d.campaign_id,
            kind=d.action, summary=d.reason[:300],
            evidence={"cited": d.evidence, "confidence": d.confidence, "pack_hash": h,
                      "by": "council"},
            params=d.params or {}))
        filed += 1
    state.update({"pack_hash": h, "at": datetime.utcnow().isoformat(),
                  "summary": verdict.channel_summary[:600],
                  "watching": verdict.watching[:5]})
    cfg = dict(channel.autopilot_json or {})
    cfg["council"] = state
    channel.autopilot_json = cfg
    db.commit()
    return {"filed": filed, "refused": refused, "held": held, "skipped_unchanged": False}
