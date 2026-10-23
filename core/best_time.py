"""Best time to post (R37, ADR-101): learn WHEN a channel's audience shows up — per weekday and
time of day — and move posting slots toward it, a little at a time, undoing moves that do not pay.

Everything here is pure (no DB, no AI): the scheduler gathers the published episodes and applies
the one move this module returns, as an ordinary `slot_change` autopilot action.

How it judges:
  * Each video scores views-at-24h ÷ the channel's median views-at-24h ("1.8× normal"). Comparing
    the same age is what makes a 3-day-old and a 3-week-old video comparable, and a ratio works on
    YouTube and Facebook alike. Capped at SCORE_CAP so one viral video cannot steer the schedule.
  * Scores land in a weekday × daypart grid. Six dayparts instead of 24 hours: at ~2 posts a day,
    an hour-by-weekday grid would need months before any cell meant anything.
  * Small cells are shrunk toward their daypart's average (and dayparts toward "normal", 1.0) —
    a cell with 2 videos says little, so it says little.

How it moves (at most one slot per campaign per week — the cooldown lives with the caller):
  * Exploit: the campaign's weakest slot moves to a clearly better daypart (≥ MIN_GAIN, with
    ≥ MIN_TARGET_N measured videos behind it). A weekday-only move is allowed when that one day's
    evidence is strong on its own ("Mondays do better at 06:30").
  * Explore: one week in EXPLORE_EVERY, a campaign with ≥ 2 slots sends its weakest slot to the
    least-measured daypart — otherwise a time it never tried could never be discovered.
  * Rollback: ≥ JUDGE_AFTER_DAYS after a move, if the videos at the new time score below the old
    slot's score (by REVERT_MARGIN), the move is undone. Undos are never themselves judged.
"""
from __future__ import annotations

from dataclasses import dataclass

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# (key, label, first hour, end hour (exclusive), the slot a move into this daypart uses)
DAYPARTS = (
    ("late_night", "Late night", 0, 5, "02:00"),
    ("early_morning", "Early morning", 5, 9, "06:30"),
    ("morning", "Morning", 9, 12, "10:00"),
    ("afternoon", "Afternoon", 12, 17, "14:00"),
    ("evening", "Evening", 17, 21, "19:00"),
    ("night", "Night", 21, 24, "22:00"),
)
DAYPART_KEYS = tuple(d[0] for d in DAYPARTS)

WINDOW_DAYS = 90         # only recent videos speak for the audience
MIN_SAMPLES = 12         # measured videos on the channel before the engine moves anything
SHRINK_K = 3             # pseudo-videos of prior in every cell
SCORE_CAP = 5.0
MIN_GAIN = 1.15          # a daily move must promise ≥ 15 % more
MIN_TARGET_N = 4         # …from a daypart with this many measured videos
DAY_MIN_GAIN = 1.3       # a weekday-only move needs a bigger, better-backed gap
DAY_MIN_N = 3
EXPLORE_EVERY = 5        # one week in five tries an under-measured time (≈ 20 %)
JUDGE_AFTER_DAYS = 14
GIVE_UP_AFTER_DAYS = 28  # still too few videos at the new time → keep it, stop judging
REVERT_MARGIN = 0.9      # undo when the new time scores < 90 % of what the old slot scored
JUDGE_MIN_N = 3


def daypart_of(hour: int) -> str:
    for key, _label, lo, hi, _slot in DAYPARTS:
        if lo <= hour < hi:
            return key
    return DAYPARTS[-1][0]


def daypart_of_slot(slot: str) -> str | None:
    try:
        return daypart_of(int(slot.split(":")[0]) % 24)
    except (ValueError, AttributeError):
        return None


def default_slot(daypart: str) -> str:
    return next(d[4] for d in DAYPARTS if d[0] == daypart)


def label(daypart: str) -> str:
    return next(d[1] for d in DAYPARTS if d[0] == daypart)


@dataclass(frozen=True)
class Sample:
    """One measured video: when it went live (local weekday + hour), its first-day views, and the
    style it was made with (`render_json.style`, R37 — None for videos older than that)."""
    weekday: str
    hour: int
    views_24h: int
    style: dict | None = None


def _median(vals: list[float]) -> float:
    vals = sorted(vals)
    mid = len(vals) // 2
    return float(vals[mid]) if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _shrink(total: float, n: int, prior: float) -> float:
    return (total + SHRINK_K * prior) / (n + SHRINK_K)


def build_grid(samples: list[Sample]) -> dict:
    """The audience map: {"n", "median_24h", "dayparts": {dp: {n, score}},
    "cells": {weekday: {dp: {n, score}}}}. Scores are shrunk ratios to the channel median (1.0 =
    a normal video). Empty dayparts/cells are present with n=0 (their prior), so callers never
    special-case a time nothing was posted at."""
    measured = [s for s in samples if s.views_24h is not None]
    out: dict = {"n": len(measured), "median_24h": None, "dayparts": {}, "cells": {}}
    median = _median([float(s.views_24h) for s in measured]) if measured else 0.0
    if median <= 0:
        for dp in DAYPART_KEYS:
            out["dayparts"][dp] = {"n": 0, "score": 1.0}
        out["cells"] = {d: {dp: {"n": 0, "score": 1.0, "mean": None} for dp in DAYPART_KEYS}
                        for d in WEEKDAYS}
        return out
    out["median_24h"] = median
    by_dp: dict[str, list[float]] = {dp: [] for dp in DAYPART_KEYS}
    by_cell: dict[tuple[str, str], list[float]] = {}
    for s in measured:
        dp = daypart_of(s.hour)
        r = min(s.views_24h / median, SCORE_CAP)
        by_dp[dp].append(r)
        by_cell.setdefault((s.weekday, dp), []).append(r)
    for dp, rs in by_dp.items():
        out["dayparts"][dp] = {"n": len(rs), "score": round(_shrink(sum(rs), len(rs), 1.0), 3)}
    for d in WEEKDAYS:
        out["cells"][d] = {}
        for dp in DAYPART_KEYS:
            rs = by_cell.get((d, dp), [])
            prior = out["dayparts"][dp]["score"]
            out["cells"][d][dp] = {"n": len(rs), "score": round(_shrink(sum(rs), len(rs), prior), 3),
                                   "mean": round(sum(rs) / len(rs), 3) if rs else None}
    return out


@dataclass(frozen=True)
class Move:
    scope: str           # "all" or a weekday key
    frm: str             # "HH:MM" currently posted at
    to: str              # "HH:MM" to post at instead
    from_score: float
    to_score: float
    explore: bool
    reason: str


def _is_explore_week(iso_week: int, channel_id: int) -> bool:
    # Channels explore in different weeks, so a box with several channels never moves them all.
    return (iso_week + channel_id) % EXPLORE_EVERY == 0


def _wins_most_days(grid: dict, target: str, current: str) -> bool:
    """A move for EVERY day must be better on most days, not only on average: one strong Monday
    lifts the pooled morning score, and moving the whole week for it would be wrong six days out of
    seven (that case is a Monday-only move). Compares each weekday's RAW means — the shrunk cell
    scores lean on the pooled daypart, which is exactly the number one strong day inflates."""
    days = [grid["cells"][d] for d in WEEKDAYS
            if grid["cells"][d][target]["n"] and grid["cells"][d][current]["n"]]
    wins = sum(1 for c in days if c[target]["mean"] > c[current]["mean"])
    return not days or wins * 2 > len(days)


def plan_move(grid: dict, cfg: dict, *, iso_week: int, channel_id: int) -> Move | None:
    """The ONE slot move worth making this week for a campaign with config `cfg`, or None."""
    slots = [s for s in (cfg.get("posting_slots") or []) if daypart_of_slot(s)]
    if not slots or grid["n"] < MIN_SAMPLES:
        return None
    dps = grid["dayparts"]
    used = {daypart_of_slot(s) for s in slots}
    weakest = min(slots, key=lambda s: dps[daypart_of_slot(s)]["score"])
    weak_score = dps[daypart_of_slot(weakest)]["score"]

    if _is_explore_week(iso_week, channel_id) and len(slots) >= 2:
        untried = [dp for dp in DAYPART_KEYS if dp not in used]
        if untried:
            target = min(untried, key=lambda dp: (dps[dp]["n"], -dps[dp]["score"]))
            if dps[target]["n"] < MIN_TARGET_N:
                return Move("all", weakest, default_slot(target), weak_score, dps[target]["score"],
                            True, f"Trying {label(target)} — only {dps[target]['n']} video(s) "
                                  f"measured there, so nobody knows yet")

    best: Move | None = None
    candidates = [dp for dp in DAYPART_KEYS if dp not in used and dps[dp]["n"] >= MIN_TARGET_N
                  and _wins_most_days(grid, dp, daypart_of_slot(weakest))]
    if candidates:
        top = max(candidates, key=lambda dp: dps[dp]["score"])
        if dps[top]["score"] >= weak_score * MIN_GAIN:
            best = Move("all", weakest, default_slot(top), weak_score, dps[top]["score"], False,
                        f"{label(top)} scores {dps[top]['score']:.2f}× normal over "
                        f"{dps[top]['n']} videos vs {weak_score:.2f}× at {weakest}")

    by_day = cfg.get("slots_by_day") or {}
    days = cfg.get("posting_days") or list(WEEKDAYS)
    for d in days:
        if d not in WEEKDAYS:
            continue
        day_slots = [s for s in (by_day.get(d) or slots) if daypart_of_slot(s)]
        if not day_slots:
            continue
        cells = grid["cells"][d]
        day_used = {daypart_of_slot(s) for s in day_slots}
        day_weak = min(day_slots, key=lambda s: cells[daypart_of_slot(s)]["score"])
        day_weak_score = cells[daypart_of_slot(day_weak)]["score"]
        options = [dp for dp in DAYPART_KEYS if dp not in day_used and cells[dp]["n"] >= DAY_MIN_N]
        if not options:
            continue
        top = max(options, key=lambda dp: cells[dp]["score"])
        gain = cells[top]["score"] / day_weak_score if day_weak_score > 0 else 0
        if gain < DAY_MIN_GAIN:
            continue
        if best is not None and gain <= best.to_score / best.from_score:
            continue
        best = Move(d, day_weak, default_slot(top), day_weak_score, cells[top]["score"], False,
                    f"On {d.title()} {label(top)} scores {cells[top]['score']:.2f}× normal over "
                    f"{cells[top]['n']} videos vs {day_weak_score:.2f}× at {day_weak}")
    return best   # a target never collides: candidates exclude every daypart already posted at


def judge_move(evidence: dict, params: dict, samples_since: list[Sample], days_since: float) -> str:
    """'revert' | 'keep' | 'wait' for an applied best-time move, from the videos published at its
    new time since it was applied. Pure."""
    if days_since < JUDGE_AFTER_DAYS:
        return "wait"
    to_dp = daypart_of_slot(params.get("to", ""))
    scope = params.get("scope") or "all"
    median = evidence.get("median_24h") or 0
    rs = [min(s.views_24h / median, SCORE_CAP) for s in samples_since
          if median > 0 and daypart_of(s.hour) == to_dp and (scope == "all" or s.weekday == scope)]
    if len(rs) < JUDGE_MIN_N:
        return "keep" if days_since >= GIVE_UP_AFTER_DAYS else "wait"
    return "revert" if sum(rs) / len(rs) < evidence.get("from_score", 0) * REVERT_MARGIN else "keep"


# ── Evidence for the AI layers (R38): compact, computed here, never by the model ─────────────
STYLE_FACTORS = ("voice", "color_grade", "music_mood", "caption_theme", "rate_pct", "visual_style")
MIN_STYLE_N = 2


def audience_summary(grid: dict, top: int = 3) -> dict:
    """The audience map in a few lines: daypart scores and the strongest/weakest weekday cells
    with ≥ 2 videos — what a reader (or the council) needs, without 42 cells of noise."""
    cells = [{"day": d, "daypart": dp, "score": c["score"], "n": c["n"]}
             for d, row in grid["cells"].items() for dp, c in row.items() if c["n"] >= 2]
    cells.sort(key=lambda c: c["score"], reverse=True)
    return {"videos": grid["n"], "median_views_24h": grid["median_24h"],
            "dayparts": {dp: v for dp, v in grid["dayparts"].items() if v["n"]},
            "strongest": cells[:top], "weakest": cells[-top:][::-1] if len(cells) > top else []}


def style_table(samples: list[Sample], median: float | None) -> list[dict]:
    """How each style choice scored, per daypart and overall: [{factor, value, daypart, n, score}]
    (score = shrunk ratio to the channel median, like the grid). Only rows with ≥ MIN_STYLE_N videos;
    the overall row has daypart "all". This is the evidence "this style works better at night"
    rests on — the AI may explain it, never compute it."""
    if not median:
        return []
    acc: dict[tuple, list[float]] = {}
    for s in samples:
        if not s.style or s.views_24h is None:
            continue
        r = min(s.views_24h / median, SCORE_CAP)
        for f in STYLE_FACTORS:
            v = s.style.get(f)
            if v in (None, ""):
                continue
            for dp in ("all", daypart_of(s.hour)):
                acc.setdefault((f, str(v), dp), []).append(r)
    rows = [{"factor": f, "value": v, "daypart": dp, "n": len(rs),
             "score": round(_shrink(sum(rs), len(rs), 1.0), 3)}
            for (f, v, dp), rs in acc.items() if len(rs) >= MIN_STYLE_N]
    rows.sort(key=lambda r: (r["factor"], r["daypart"] != "all", -r["score"]))
    return rows

