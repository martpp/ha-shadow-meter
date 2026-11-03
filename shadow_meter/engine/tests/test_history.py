"""Confidence history: thinned out with age, resampled evenly over time for the UI."""

from __future__ import annotations

import itertools
import math
from pathlib import Path
from typing import Any

from shadow_meter.detection.pipeline import _link_explained
from shadow_meter.history import MAX_POINTS, VIEW_POINTS, resample, thin
from shadow_meter.store import Store


def _cand(
    cid: str, power: float, count: float, conf: float, q: float = math.nan, *, kind: str = "refrigerator"
) -> Any:
    features = {"count": count, "q_ratio": q}
    return {"id": cid, "source": "pattern", "phase": "A", "type": kind, "confidence": conf, "power_w": power,
            "features": features}  # fmt: skip


def test_fragments_of_a_device_are_linked_to_it() -> None:
    """A fridge leaves a few small "fridges" next to it; they are linked to it, other devices are not."""
    cands = [
        _cand("main", 38, 5000, 0.77, 0.21),
        _cand("fragment", 30, 1200, 0.6, 0.26),  # fewer cycles, similar power and ratio
        _cand("other_motor", 46, 200, 0.55, 0.5),  # a different kind of load: a device of its own
        _cand("far", 120, 100, 0.6),  # too far in power
        _cand("kettle", 2000, 100, 0.8, kind="heating"),
        _cand("hob_plate", 1600, 30, 0.8, kind="heating"),  # another plate, not a fragment of the kettle
    ]
    _link_explained(cands)
    linked = {c["id"]: c["explained_by"] for c in cands}
    assert linked == {
        "main": None, "fragment": "main", "other_motor": None, "far": None, "kettle": None, "hob_plate": None
    }  # fmt: skip


def test_thin_keeps_recent_points_and_reaches_back_to_the_start() -> None:
    points: list[tuple[float, float]] = []
    for i in range(5000):  # e.g. 300 days of history, then weeks of live points every 15 minutes
        points = thin([*points, (float(i), i / 5000)])
    assert len(points) <= MAX_POINTS
    assert points[0][0] < 50  # the start of the analysis is still there
    assert [t for t, _ in points[-100:]] == [float(i) for i in range(4900, 5000)]  # newest complete
    assert all(a[0] < b[0] for a, b in itertools.pairwise(points))


def test_resample_spreads_points_evenly_over_time() -> None:
    days = [(i * 86400.0, 0.5) for i in range(300)]  # one point per analysed day
    live = [(300 * 86400.0 + i * 900, 0.8) for i in range(2000)]  # then dense live points
    view = resample(days + live)
    assert len(view) <= VIEW_POINTS
    assert view[0][0] == 0 and view[-1][0] == round(live[-1][0])
    # most of the view covers the long history, not the dense recent live points
    assert sum(1 for t, _ in view if t < 300 * 86400) > VIEW_POINTS / 2
    assert resample(days[:3]) == [(0, 0.5), (86400, 0.5), (172800, 0.5)]


async def test_store_thins_like_memory(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    expected: list[tuple[float, float]] = []
    for i in range(MAX_POINTS + 150):
        await store.add_confidence([("cluster:A-1", float(i), 0.5), ("cluster:A-2", float(i), 0.1)])
        expected = thin([*expected, (float(i), 0.5)])
    hist = await store.confidence_history()
    assert hist["cluster:A-1"] == expected
    assert len(hist["cluster:A-2"]) == len(expected)
    store.close()
