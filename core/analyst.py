"""The AI analyst (R38, ADR-102) — a weekly, plain-language read of each channel.

Same boundary as the council (ADR-081): code computes every number, the model interprets, code
checks the interpretation. The analyst reads the council's evidence pack (campaigns, flops,
operations, the best-time audience map, style × time of day) plus a this-week-vs-last-week block,
and writes what a human analyst would: what happened, why it probably happened, what to try next,
and fresh topic ideas grounded in what already works.

It DECIDES nothing. Its report is shown on the Channels page and logged on the Autopilot page; its
topic ideas feed the successor design when the factory creates a new campaign (ADR-102). An insight
that cites a number the pack does not contain is dropped by the shared rail (`numbers_missing`) —
the reader sees how many were dropped, so the filter is visible too.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select

from core import council
from database.models import Campaign, Task
from database.types import TaskStatus

logger = logging.getLogger(__name__)

ANALYST_EVERY_DAYS = 7


class Insight(BaseModel):
    title: str = Field(min_length=3, max_length=90)
    detail: str = Field(min_length=20, max_length=450,
                        description="What happened and the likely why, citing the pack's numbers")
    evidence: list[str] = Field(min_length=1, max_length=4,
                                description="The specific facts from the pack this rests on")


class TopicIdea(BaseModel):
    topic: str = Field(min_length=3, max_length=120)
    why: str = Field(min_length=10, max_length=300,
                     description="Which proven campaign, style or time this builds on")


class AnalystReport(BaseModel):
    headline: str = Field(min_length=10, max_length=200,
                          description="The one sentence an owner should read this week")
    insights: list[Insight] = Field(min_length=1, max_length=5)
    recommendations: list[str] = Field(default_factory=list, max_length=4)
    topic_ideas: list[TopicIdea] = Field(default_factory=list, max_length=3)


_SYSTEM = (
    "You are the data analyst of a short-form video channel. You get an evidence pack: every "
    "number in it was measured; nothing else exists. Write the weekly analysis an owner reads in "
    "two minutes: a headline, up to five insights (what happened, the most likely why, citing the "
    "numbers), up to four concrete recommendations, and up to three fresh topic ideas that build on "
    "what already works on THIS channel. Read `audience_map` (when the audience shows up; score 1.0 "
    "= a normal video at 24h; n = videos behind it), `style_by_daypart` (which style choices score "
    "better overall and per time of day) and `last_14_days`. Small n is a hint, never a fact — say "
    "so. Use ONLY numbers present in the pack. Write in the channel's language when one is set, "
    "otherwise English, in a plain, human voice — no jargon, no filler."
)


def _window(db, channel, start: datetime, end: datetime) -> dict:
    from services.analytics_service import published_time

    rows = db.execute(select(Task).join(Campaign, Task.campaign_id == Campaign.id).where(
        Campaign.channel_id == channel.id, Task.status == TaskStatus.COMPLETED,
        Task.published_video_id.isnot(None))).scalars().all()
    inside = [t for t in rows if (p := published_time(t)) is not None and start <= p < end]
    views = sorted(int((t.stats_json or {})["views_24h"]) for t in inside
                   if (t.stats_json or {}).get("views_24h") is not None)
    median = None
    if views:
        mid = len(views) // 2
        median = views[mid] if len(views) % 2 else round((views[mid - 1] + views[mid]) / 2)
    return {"published": len(inside), "measured_24h": len(views), "median_views_24h": median}


def evidence_pack(db, channel, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    pack = council.evidence_pack(db, channel)
    pack.pop("my_recent_decisions", None)
    pack["last_14_days"] = {"this_week": _window(db, channel, now - timedelta(days=7), now),
                            "last_week": _window(db, channel, now - timedelta(days=14),
                                                 now - timedelta(days=7))}
    return pack


def _clean(report: AnalystReport, pack_json: str) -> tuple[dict, int]:
    """Drop every piece that cites a number the pack does not contain. Returns (report, dropped)."""
    dropped = 0
    insights = []
    for i in report.insights:
        if council.numbers_missing(" ".join([i.title, i.detail, *i.evidence]), pack_json):
            dropped += 1
            continue
        insights.append(i.model_dump())
    recs = []
    for r in report.recommendations:
        if council.numbers_missing(r, pack_json):
            dropped += 1
            continue
        recs.append(r[:200])
    ideas = []
    for t in report.topic_ideas:
        if council.numbers_missing(t.why, pack_json):
            dropped += 1
            continue
        ideas.append(t.model_dump())
    headline = report.headline
    if council.numbers_missing(headline, pack_json):
        dropped += 1
        headline = insights[0]["title"] if insights else "Weekly analysis"
    return ({"headline": headline, "insights": insights, "recommendations": recs,
             "topic_ideas": ideas}, dropped)


def has_data(pack: dict) -> bool:
    return bool(pack["campaigns"]) or bool((pack.get("audience_map") or {}).get("videos"))


def run_analyst(db, channel, *, api_key: str, model: str, now: datetime | None = None) -> dict:
    """One weekly analysis for one channel. Stores it on `autopilot_json["analyst"]` and returns
    it; returns {} when there is nothing to analyse yet. AI errors surface to the caller."""
    from core.ai_engine import generate_structured

    now = now or datetime.utcnow()
    pack = evidence_pack(db, channel, now)
    if not has_data(pack):
        return {}
    pack_json = json.dumps(pack, ensure_ascii=False, default=str)
    report = generate_structured(
        prompt="EVIDENCE PACK:\n" + pack_json + "\n\nWrite this week's analysis.",
        schema=AnalystReport, api_key=api_key, system_prompt=_SYSTEM, model=model,
        temperature=0.4, max_retries=2)   # optional — bounded inside the pass's AI budget
    cleaned, dropped = _clean(report, pack_json)
    state = {**cleaned, "dropped": dropped, "at": now.isoformat()}
    cfg = dict(channel.autopilot_json or {})
    cfg["analyst"] = state
    channel.autopilot_json = cfg
    db.commit()
    return state


def due(channel, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    at = ((channel.autopilot_json or {}).get("analyst") or {}).get("at")
    try:
        return not at or datetime.fromisoformat(at) <= now - timedelta(days=ANALYST_EVERY_DAYS)
    except ValueError:
        return True


def topic_ideas(channel) -> list[dict]:
    """The latest analysis's topic ideas — the successor design reads these (ADR-102)."""
    return list(((channel.autopilot_json or {}).get("analyst") or {}).get("topic_ideas") or [])
