"""AI review of rule-made decisions (R38, ADR-102) — a second opinion before the rules act.

The deterministic autopilot (extend / wind down / successor) and the best-time engine (slot moves)
decide from fixed thresholds. Before Full-auto applies one, Gemini reads the same evidence pack
the council reads plus the decision itself, and answers approve or veto with a reason:

  * Full-auto: a veto stops the action (dismissed, with the AI's reason) — the same 30-day
    "no" an operator's dismissal gives. An approve lets it apply, the reason kept on the action.
  * Copilot: the opinion is shown on the proposal; the operator still decides.

The AI can only say NO to a rule; it cannot create an action. And it fails OPEN in Full-auto: no
key, the budget reserve, an outage or a reason that cites numbers not in the pack all let the rule
decision stand, recorded as such — the rules ran this factory before the review existed, and the
review must never be the thing that stalls it (ADR-076). Decisions the council itself made, and
best-time undos (a safety net), are not reviewed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from core import council

logger = logging.getLogger(__name__)

REVIEWED_KINDS = ("extend", "wind_down", "successor", "slot_change")
MAX_REVIEWS_PER_PASS = 3


class ReviewVerdict(BaseModel):
    verdict: Literal["approve", "veto"]
    reason: str = Field(min_length=20, max_length=400,
                        description="Plain language, citing the pack's numbers")
    evidence: list[str] = Field(min_length=1, max_length=4)


_SYSTEM = (
    "You review ONE decision a rule-based autopilot is about to apply to a short-form video "
    "channel. You get the evidence pack (every number in it was measured; nothing else exists) and "
    "the decision with the numbers that triggered it. Approve unless the evidence shows it would "
    "likely hurt the channel — e.g. stopping a campaign whose recent videos are recovering, "
    "extending one whose production is failing, or moving a posting time on too little data. "
    "Use ONLY numbers present in the pack. Write in the channel's language when one is set, "
    "otherwise English, in one or two plain sentences."
)


def needs_review(action) -> bool:
    ev = action.evidence or {}
    return (action.status == "proposed" and action.kind in REVIEWED_KINDS
            and ev.get("by") != "council" and not ev.get("revert") and "ai_review" not in ev)


def review(db, channel, action, *, api_key: str, model: str) -> dict:
    """One Gemini call → {"verdict": approve|veto|unreliable, "reason", "evidence", "at"}.
    'unreliable' = the reason cited a number the pack does not contain; callers treat it as no
    review (the rule stands). AI errors surface to the caller."""
    from core.ai_engine import generate_structured

    pack = council.evidence_pack(db, channel)
    pack.pop("my_recent_decisions", None)
    pack["campaigns"] = [c for c in pack["campaigns"] if c["campaign_id"] == action.campaign_id]
    decision = {"kind": action.kind, "summary": action.summary, "params": action.params or {},
                "evidence": {k: v for k, v in (action.evidence or {}).items() if k != "ai_review"}}
    pack_json = json.dumps({**pack, "decision": decision}, ensure_ascii=False, default=str)
    v = generate_structured(
        prompt="EVIDENCE PACK + DECISION:\n" + pack_json + "\n\nApprove or veto this decision.",
        schema=ReviewVerdict, api_key=api_key, system_prompt=_SYSTEM, model=model,
        temperature=0.2, max_retries=2)
    out = {"verdict": v.verdict, "reason": v.reason, "evidence": v.evidence,
           "at": datetime.utcnow().isoformat()}
    problem = council.numbers_missing(v.reason + " " + " ".join(v.evidence), pack_json)
    if problem:
        out.update(verdict="unreliable", reason=f"AI review discarded — {problem}")
    return out


def unavailable(why: str) -> dict:
    return {"verdict": "unavailable", "reason": why, "at": datetime.utcnow().isoformat()}
