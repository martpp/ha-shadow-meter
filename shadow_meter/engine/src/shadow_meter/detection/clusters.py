"""Online clustering of on/off cycles into device candidates.

Every cycle is assigned to the cluster on the same phase whose typical power is closest (within a
relative tolerance); otherwise it starts a new cluster. A cluster keeps only counters and a bounded
reservoir of recent cycles, so memory stays small even for years of history. Cluster ids are stable,
so user decisions (confirm, rename, ...) stay attached when the cluster keeps learning.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any

from ..models import Cycle

RESERVOIR = 300
RECENT_WINDOW_S = 7 * 86400


@dataclass
class Cluster:
    id: str
    phase: str
    count: int = 0
    first_ts: float = math.inf
    last_ts: float = -math.inf
    hour_counts: list[int] = field(default_factory=lambda: [0] * 24)
    recent: deque[Cycle] = field(default_factory=lambda: deque(maxlen=RESERVOIR))
    _power: float | None = field(default=None, repr=False, compare=False)

    @property
    def power_w(self) -> float:
        """Typical running power; cached because it is needed for every new cycle on every cluster."""
        if self._power is None:
            self._power = statistics.median(c.power_w for c in self.recent) if self.recent else math.nan
        return self._power

    def add(self, c: Cycle, local_hour: int) -> None:
        self._power = None
        self.count += 1
        self.first_ts = min(self.first_ts, c.start_ts)
        self.last_ts = max(self.last_ts, c.end_ts)
        self.hour_counts[local_hour] += 1
        self.recent.append(c)

    def absorb(self, other: Cluster) -> None:
        """Merge another cluster into this one (keeps this id)."""
        self._power = None
        self.count += other.count
        self.first_ts = min(self.first_ts, other.first_ts)
        self.last_ts = max(self.last_ts, other.last_ts)
        self.hour_counts = [a + b for a, b in zip(self.hour_counts, other.hour_counts, strict=True)]
        merged = sorted([*self.recent, *other.recent], key=lambda c: c.start_ts)
        self.recent = deque(merged[-RESERVOIR:], maxlen=RESERVOIR)

    def features(self) -> dict[str, float]:
        """Numbers that describe the cluster; used for classification and shown in the UI."""
        cyc = list(self.recent)
        if not cyc:
            return {}
        powers = sorted(c.power_w for c in cyc)
        q1, q3 = powers[len(powers) // 4], powers[(3 * len(powers)) // 4]
        window_start = self.last_ts - RECENT_WINDOW_S
        recent = [c for c in cyc if c.start_ts >= window_start]
        covered_s = max(3600.0, min(RECENT_WINDOW_S, self.last_ts - max(self.first_ts, cyc[0].start_ts)))
        run_s = sum(c.duration_s for c in recent)
        starts = [c.start_ts for c in recent]
        gaps = [b - a for a, b in itertools.pairwise(starts)]
        gap_cv = statistics.pstdev(gaps) / statistics.mean(gaps) if len(gaps) >= 3 else math.nan
        durations = [c.duration_s for c in cyc]
        med_duration = statistics.median(durations)
        # robust spread of the run time: a compressor runs about equally long every time, random pairs don't
        duration_spread = (
            statistics.median(abs(d - med_duration) for d in durations) / med_duration
            if med_duration
            else math.inf
        )
        total = sum(self.hour_counts)
        probs = [h / total for h in self.hour_counts if h]
        hour_uniformity = -sum(p * math.log(p) for p in probs) / math.log(24) if total else 0.0
        power = statistics.median(powers)
        duty = min(1.0, run_s / covered_s)
        return {
            "power_w": power,
            "on_w": statistics.median(c.on_w for c in cyc),
            "power_spread": (q3 - q1) / power if power > 0 else math.inf,
            "duration_min": med_duration / 60,
            "duration_spread": duration_spread,
            "cycles_per_day": len(recent) / (covered_s / 86400),
            "duty": duty,
            "gap_cv": gap_cv,
            "hour_uniformity": hour_uniformity,
            "kwh_per_day": power * duty * 24 / 1000,
            "count": float(self.count),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase": self.phase,
            "count": self.count,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "hour_counts": self.hour_counts,
            "recent": [[c.start_ts, c.end_ts, c.on_w, c.off_w] for c in self.recent],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Cluster:
        c = cls(
            id=d["id"],
            phase=d["phase"],
            count=d["count"],
            first_ts=d["first_ts"],
            last_ts=d["last_ts"],
            hour_counts=list(d["hour_counts"]),
        )
        for s, e, on, off in d["recent"]:
            c.recent.append(Cycle(phase=c.phase, start_ts=s, end_ts=e, on_w=on, off_w=off))
        return c


class ClusterSet:
    """All clusters; assigns cycles and merges clusters that have grown together."""

    def __init__(self, tolerance: float = 0.2, tz: tzinfo = UTC) -> None:
        self.tolerance = tolerance
        self.tz = tz  # local time zone for the hour-of-day profile
        self.clusters: dict[str, Cluster] = {}
        self._next = 1

    def add(self, c: Cycle) -> Cluster:
        best: Cluster | None = None
        best_mismatch = self.tolerance
        for cl in self.clusters.values():
            if cl.phase != c.phase or not cl.recent:
                continue
            mismatch = abs(cl.power_w - c.power_w) / max(cl.power_w, c.power_w)
            if mismatch <= best_mismatch:
                best, best_mismatch = cl, mismatch
        if best is None:
            best = Cluster(id=self._new_id(c.phase), phase=c.phase)
            self.clusters[best.id] = best
        best.add(c, datetime.fromtimestamp(c.start_ts, self.tz).hour)
        return best

    def merge_close(self) -> list[tuple[str, str]]:
        """Merge clusters on the same phase whose typical power has converged. Returns (kept, removed)."""
        merged: list[tuple[str, str]] = []
        ids = sorted(self.clusters, key=lambda k: self.clusters[k].first_ts)
        for i, a_id in enumerate(ids):
            a = self.clusters.get(a_id)
            if a is None or not a.recent:
                continue
            for b_id in ids[i + 1 :]:
                b = self.clusters.get(b_id)
                if b is None or b.phase != a.phase or not b.recent:
                    continue
                if abs(a.power_w - b.power_w) / max(a.power_w, b.power_w) <= self.tolerance / 2:
                    a.absorb(b)
                    del self.clusters[b_id]
                    merged.append((a_id, b_id))
        return merged

    def prune(self, now: float, min_count: int = 5, max_age_s: float = 14 * 86400) -> list[str]:
        """Forget small clusters that have not been seen for a while (noise); returns removed ids."""
        old = [k for k, c in self.clusters.items() if c.count < min_count and now - c.last_ts > max_age_s]
        for k in old:
            del self.clusters[k]
        return old

    def _new_id(self, phase: str) -> str:
        while (cid := f"{phase}-{self._next}") in self.clusters:
            self._next += 1
        self._next += 1
        return cid

    def to_dict(self) -> dict[str, Any]:
        return {"next": self._next, "clusters": [c.to_dict() for c in self.clusters.values()]}

    def load(self, d: dict[str, Any]) -> None:
        self._next = int(d.get("next", 1))
        self.clusters = {c["id"]: Cluster.from_dict(c) for c in d.get("clusters", [])}
