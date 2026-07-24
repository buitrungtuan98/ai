"""Retention-curve analysis — the 'where do viewers leave?' half of the learning loop.

Pure and deterministic (no AI, no IO). YouTube Analytics gives a free second-by-second retention
curve (`elapsedVideoTimeRatio` × `audienceWatchRatio`); on its own that's just a wiggly line. Paired
with the episode's scene boundaries (persisted at render time), a drop at 0:12 becomes "scene 3 — the
twist reveal", which is an actionable lesson the scriptwriter can learn from. This module does that
attribution; the analytics service feeds it real curves and the playbook distiller consumes its
summaries — no extra API calls anywhere.
"""
from __future__ import annotations


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def scene_map(durations: list[float], labels: list[str] | None = None) -> list[dict]:
    """Turn per-scene durations into absolute-timed scene spans: ``[{index, start, end, dur, label}]``.
    `labels[i]` names scene i (its caption hook); missing labels fall back to "Scene N"."""
    labels = labels or []
    out: list[dict] = []
    t = 0.0
    for i, d in enumerate(durations):
        label = (labels[i] if i < len(labels) and labels[i] else "").strip() or f"Scene {i + 1}"
        out.append({"index": i, "start": round(t, 3), "end": round(t + d, 3),
                    "dur": round(d, 3), "label": label})
        t += d
    return out


def _scene_at(scenes: list[dict], seconds: float) -> dict | None:
    for sc in scenes:
        if sc["start"] <= seconds < sc["end"]:
            return sc
    return scenes[-1] if scenes and seconds >= scenes[-1]["end"] else None


def drop_points(curve: list[tuple[float, float]], scenes: list[dict], *,
                top: int = 3, min_drop: float = 0.05) -> list[dict]:
    """Find the steepest audience drop-offs and attribute each to the scene playing there.

    `curve` is ``[(position 0..1 of the video, watch_ratio), …]`` sorted by position (YouTube's
    elapsedVideoTimeRatio × audienceWatchRatio). A drop is a fall in watch ratio between consecutive
    points ≥ `min_drop`. Returns up to `top` drops, biggest first:
    ``[{at_pct, at_seconds, scene_index, label, drop_pct}]``. Empty when nothing is significant."""
    pts = sorted((p, v) for p, v in curve if p is not None and v is not None)
    if len(pts) < 2 or not scenes:
        return []
    total = scenes[-1]["end"] or 1.0
    drops: list[dict] = []
    for (_p0, v0), (p1, v1) in zip(pts, pts[1:]):
        delta = v0 - v1
        if delta < min_drop:
            continue
        at_seconds = round(p1 * total, 1)
        sc = _scene_at(scenes, at_seconds)
        drops.append({
            "at_pct": round(p1 * 100),
            "at_seconds": at_seconds,
            "scene_index": sc["index"] if sc else None,
            "label": sc["label"] if sc else "?",
            "drop_pct": round(delta * 100),
        })
    drops.sort(key=lambda d: d["drop_pct"], reverse=True)
    return drops[:top]


def summarize_drop(curve: list[tuple[float, float]], scenes: list[dict]) -> str | None:
    """One human line naming the single biggest drop-off — for the Episode view + the playbook
    distiller. None when no drop clears the threshold (a flat/short curve teaches nothing)."""
    top = drop_points(curve, scenes, top=1)
    if not top:
        return None
    d = top[0]
    return (f"Biggest drop-off at {_mmss(d['at_seconds'])} "
            f"(scene {(d['scene_index'] or 0) + 1} — “{d['label']}”): "
            f"−{d['drop_pct']}% of viewers left there.")
