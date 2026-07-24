"""Unit tests for the pure retention-curve analysis (core/retention.py)."""
from core import retention


def test_scene_map_cumulative_and_labels():
    scenes = retention.scene_map([3.0, 5.0, 2.0], ["hook", "", "the twist"])
    assert [s["start"] for s in scenes] == [0.0, 3.0, 8.0]
    assert [s["end"] for s in scenes] == [3.0, 8.0, 10.0]
    assert scenes[0]["label"] == "hook"
    assert scenes[1]["label"] == "Scene 2"      # blank label → fallback
    assert scenes[2]["label"] == "the twist"


def test_drop_points_attributes_to_scene():
    scenes = retention.scene_map([4.0, 4.0, 2.0], ["intro", "middle", "ending"])  # total 10s
    # Watch ratio holds, then falls hard at 40% (=4.0s → start of scene 2 'middle'), then drifts.
    curve = [(0.0, 1.0), (0.2, 0.98), (0.4, 0.70), (0.6, 0.66), (0.8, 0.60), (1.0, 0.55)]
    drops = retention.drop_points(curve, scenes, min_drop=0.05)
    assert drops, "a 28-point fall must be detected"
    top = drops[0]
    assert top["at_pct"] == 40 and top["at_seconds"] == 4.0
    assert top["scene_index"] == 1 and top["label"] == "middle"   # 4.0s lands in scene 2
    assert top["drop_pct"] == 28


def test_drop_points_ignores_shallow_and_respects_top():
    scenes = retention.scene_map([5.0, 5.0], ["a", "b"])
    curve = [(0.0, 1.0), (0.5, 0.97), (1.0, 0.94)]  # only ~3% steps
    assert retention.drop_points(curve, scenes, min_drop=0.05) == []
    # Multiple real drops → capped at `top`, ordered biggest first.
    curve2 = [(0.0, 1.0), (0.3, 0.8), (0.6, 0.5), (0.9, 0.45)]
    d = retention.drop_points(curve2, scenes, top=1, min_drop=0.05)
    assert len(d) == 1 and d[0]["drop_pct"] == 30


def test_summarize_drop_human_line_and_none():
    scenes = retention.scene_map([4.0, 4.0, 2.0], ["intro", "the reveal", "outro"])
    curve = [(0.0, 1.0), (0.4, 0.72), (1.0, 0.6)]
    line = retention.summarize_drop(curve, scenes)
    assert line and "0:04" in line and "the reveal" in line and "−28%" in line
    # A flat curve teaches nothing → no summary.
    assert retention.summarize_drop([(0.0, 1.0), (1.0, 0.99)], scenes) is None
