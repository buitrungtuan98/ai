"""Early-flop detection + autopsy (ADR-079) — the fast half of the feedback loop.

Retention (the king metric) arrives ~2 days late; by then two more episodes may have shipped into
the same hole. Views in the first 24 hours are available almost immediately (the early-stats pass
refreshes young episodes hourly), so a flop can be named while it still changes what renders next.

Definitions live here, once, shared by the stats collector (flags), the alert feed (says it), the
learning loop (writes the autopsy note) and the council pack (reads the flags):

  * `views_24h` — a one-shot snapshot of views when the episode crosses 24h of age.
  * flop — views_24h < FLOP_RATIO × the campaign's median views_24h, judged only when the campaign
    has ≥ MIN_MEASURED_24H measured episodes (below that the gate says "not enough data", never a
    guess).
  * autopsy — a structured note appended to campaign.learning_json["flop_notes"], which the script
    generator already consumes through its avoid-notes plumbing. The loop closes by itself.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

FLOP_RATIO = 0.3          # under 30% of the campaign's own median first-day views = a flop
MIN_MEASURED_24H = 5      # judgments need this many measured episodes; below → silent
MAX_FLOP_NOTES = 10       # learning_json cap, matching reject_reasons
SNAPSHOT_AGE_H = 24       # when views_24h is stamped (first early-stats refresh past this age)


def campaign_median_24h(tasks) -> float | None:
    """Median first-day views across this campaign's measured ORDINARY episodes, or None below the
    floor. Compilations are excluded here, at the definition (ADR-085): a long-form video's
    first-day views are a different distribution, and one in the median skews every flop verdict."""
    from core.compilation import ordinary_episodes

    vals = sorted(t.stats_json["views_24h"] for t in ordinary_episodes(tasks)
                  if (t.stats_json or {}).get("views_24h") is not None)
    if len(vals) < MIN_MEASURED_24H:
        return None
    mid = len(vals) // 2
    return float(vals[mid]) if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def is_flop(views_24h: int | None, median: float | None) -> bool | None:
    """True flop · False fine · None cannot judge (no snapshot yet / not enough history)."""
    if views_24h is None or median is None or median <= 0:
        return None
    return views_24h < FLOP_RATIO * median


def autopsy_note(task, campaign, *, median: float, tz_hour: int | None) -> str:
    """One structured, prompt-ready sentence about WHY this episode may have flopped — every fact
    from data we hold, no speculation dressed as knowledge. Fed into the next scripts' avoid-notes,
    so it must read as an instruction, not a log line."""
    v = (task.stats_json or {}).get("views_24h", 0)
    bits = [f"episode {task.episode_number} flopped ({v} first-day views vs a typical "
            f"{round(median)})"]
    if task.ab_variant:
        bits.append(f"metadata variant {task.ab_variant}")
    if tz_hour is not None:
        bits.append(f"published around {tz_hour:02d}:00")
    if task.synopsis:
        bits.append(f"premise: “{task.synopsis[:80]}”")
    return "; ".join(bits) + " — avoid repeating this angle without a fresh hook"


def record_flop(db, task, campaign, *, median: float, tz_hour: int | None) -> bool:
    """Flag the episode + append the autopsy note. Idempotent: an episode is autopsied once.
    Returns True when a NEW flop was recorded."""
    stats = dict(task.stats_json or {})
    if stats.get("flop") is not None:      # already judged (either way) — never re-litigate
        return False
    stats["flop"] = True
    task.stats_json = stats
    learning = dict(campaign.learning_json or {})
    notes = (learning.get("flop_notes") or [])[-(MAX_FLOP_NOTES - 1):]
    learning["flop_notes"] = notes + [autopsy_note(task, campaign, median=median, tz_hour=tz_hour)]
    campaign.learning_json = learning
    db.commit()
    logger.info("Flop recorded: campaign %s episode %s", campaign.id, task.episode_number)
    return True


def mark_fine(db, task) -> None:
    """The episode cleared the bar — remember the verdict so it is never re-judged."""
    stats = dict(task.stats_json or {})
    if stats.get("flop") is None:
        stats["flop"] = False
        task.stats_json = stats
        db.commit()


def late_autopsy_hook_note(task, drop_summary: str) -> str | None:
    """When the retention curve finally arrives (~2 days) and blames the first scene, upgrade the
    autopsy: the hook lost them, not the topic. Returns the note or None otherwise.
    `summarize_drop` writes "(scene N — “label”)", so match "scene 1 " with its delimiter —
    a bare "scene 1" would also match scenes 10-19."""
    if not drop_summary or "scene 1 " not in str(drop_summary).lower():
        return None
    return (f"episode {task.episode_number}'s retention curve blames the OPENING "
            f"({drop_summary}) — the hook failed; open with the most concrete, surprising fact")
