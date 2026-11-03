"""Confidence history of a candidate over its whole lifetime, in bounded space.

A point is recorded for every analysed day of the history and every 15 minutes live. Instead of keeping
only the newest points (which would show a few hours of a live analysis), old points are thinned out:
the newest `KEEP_RECENT` points stay, every second of the older ones is dropped whenever the history
grows beyond `MAX_POINTS`. The older a point, the more often it has been halved, so the history reaches
back to the start of the analysis with a resolution that gets coarser with age.

For the UI the history is resampled to a fixed number of points evenly spread over time, so the trend
always shows the whole lifetime of a candidate, whether that is a day or a year.
"""

from __future__ import annotations

Point = tuple[float, float]  # (timestamp, confidence)

MAX_POINTS = 400
KEEP_RECENT = 100
VIEW_POINTS = 60


def thin(points: list[Point], max_points: int = MAX_POINTS, keep_recent: int = KEEP_RECENT) -> list[Point]:
    """Points in time order, at most `max_points`: older points are halved until it fits."""
    while len(points) > max_points:
        old, recent = points[:-keep_recent], points[-keep_recent:]
        points = old[::2] + recent if len(old) > 1 else points[-max_points:]
    return points


def resample(points: list[Point], n: int = VIEW_POINTS) -> list[Point]:
    """At most n points evenly spread over time: the first point, then the last point of every time slot
    (rounded for the UI)."""
    if len(points) <= n:
        return [(round(t), round(v, 3)) for t, v in points]
    t0, t1 = points[0][0], points[-1][0]
    slots_n = n - 1
    width = (t1 - t0) / slots_n or 1.0
    slots: dict[int, Point] = {}
    for t, v in points[1:]:
        slots[min(int((t - t0) / width), slots_n - 1)] = (t, v)
    picked = [points[0], *(p for _, p in sorted(slots.items()))]
    return [(round(t), round(v, 3)) for t, v in picked]
