"""Online clustering of on/off cycles into device candidates.

Every cycle is assigned to the cluster on the same phase whose typical power is closest (within a
relative tolerance); otherwise it starts a new cluster. A cluster keeps only counters and a bounded
reservoir of recent cycles, so memory stays small even for years of history. Cluster ids are stable,
so user decisions (confirm, rename, ...) stay attached when the cluster keeps learning.
"""

from __future__ import annotations

import bisect
import itertools
import math
import statistics
from collections import deque
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any

from ..models import Cycle

RESERVOIR = 300
RECENT_WINDOW_S = 7 * 86400
# reactive/real power ratio (dQ/dP, only with a power factor sensor): a cluster's typical value counts
# once it has MIN_Q measured cycles. A cycle does not join a cluster whose ratio differs by more than
# Q_ASSIGN_TOL (heating ~0, fridge compressor ~0.2, other motors and power supplies higher), and two
# clusters whose ratios differ by more than Q_MERGE_TOL are never merged.
MIN_Q = 5
Q_ASSIGN_TOL = 0.25
Q_MERGE_TOL = 0.15


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
    _q: tuple[float | None, int] | None = field(default=None, repr=False, compare=False)
    # type probabilities smoothed over time (see `smooth`); None until the first update
    probs: dict[str, float] | None = None
    probs_ts: float = -math.inf
    probs_n: int = 0  # number of cycles at the last update

    @property
    def power_w(self) -> float:
        """Typical running power; cached because it is needed for every new cycle on every cluster."""
        if self._power is None:
            self._power = statistics.median(c.power_w for c in self.recent) if self.recent else math.nan
        return self._power

    @property
    def q_ratio(self) -> float | None:
        """Typical dQ/dP of the cycles, or None without enough measured cycles (cached like power_w)."""
        return self._q_stats()[0]

    def _q_stats(self) -> tuple[float | None, int]:
        if self._q is None:
            values = [c.q_ratio for c in self.recent if c.q_ratio is not None]
            self._q = (statistics.median(values) if len(values) >= MIN_Q else None, len(values))
        return self._q

    def add(self, c: Cycle, local_hour: int) -> None:
        self._power = self._q = None
        self.count += 1
        self.first_ts = min(self.first_ts, c.start_ts)
        self.last_ts = max(self.last_ts, c.end_ts)
        self.hour_counts[local_hour] += 1
        self.recent.append(c)

    def smooth(self, probs: dict[str, float], now: float, tau_s: float) -> None:
        """Blend new type probabilities into the smoothed ones (time constant tau_s, in data time).

        The features describe the last few hundred cycles, about a week of a fridge. A noisy week (other
        devices, a hot spell, a defrost) can make it look like something else for a while; the smoothed
        probabilities keep the type of a device that has been consistent for weeks.

        The weight of the new probabilities grows with the time passed and with the new evidence: a
        young cluster that doubled its cycles takes at least half of the new view, so a first guess made
        from a handful of cycles does not stick."""
        if self.probs is None:
            a = 1.0
        else:
            by_time = 1 - math.exp(-max(0.0, now - self.probs_ts) / tau_s)
            by_evidence = 1 - self.probs_n / self.count if self.count > self.probs_n else 0.0
            a = max(by_time, by_evidence)
        old = self.probs or {}
        self.probs = {k: (1 - a) * old.get(k, v) + a * v for k, v in probs.items()}
        self.probs_ts = max(now, self.probs_ts)
        self.probs_n = self.count

    def absorb(self, other: Cluster) -> None:
        """Merge another cluster into this one (keeps this id)."""
        if other.probs is not None:  # weighted by the number of cycles
            mine, w = self.probs or other.probs, self.count / max(1, self.count + other.count)
            self.probs = {k: w * mine.get(k, 0.0) + (1 - w) * v for k, v in other.probs.items()}
            self.probs_ts = max(self.probs_ts, other.probs_ts)
        self._power = self._q = None
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
        starts = [c.start_ts for c in recent]
        gaps = [b - a for a, b in itertools.pairwise(starts)]
        gap_cv = statistics.pstdev(gaps) / statistics.mean(gaps) if len(gaps) >= 3 else math.nan
        durations = [c.duration_s for c in cyc]
        med_duration = statistics.median(durations)
        # robust spread of the run time: a compressor runs about equally long every time, random pairs don't.
        # Only plausible cycles count: very short ones (< 2 min) and very long ones (> 3x the median) are
        # mostly mis-paired steps of other devices or missed switch-off steps, and they must not decide it.
        plausible = [d for d in durations if 120 <= d <= 3 * med_duration] or durations
        med_plausible = statistics.median(plausible)
        duration_spread = (
            statistics.median(abs(d - med_plausible) for d in plausible) / med_plausible
            if med_plausible
            else math.inf
        )
        total = sum(self.hour_counts)
        probs = [h / total for h in self.hour_counts if h]
        hour_uniformity = -sum(p * math.log(p) for p in probs) / math.log(24) if total else 0.0
        power = statistics.median(powers)
        # run time per cycle capped at 3x the median: a single on step paired with a much later off step
        # (a missed off step) must not turn a 15-minute compressor into one that runs all day
        cap = 3 * statistics.median(c.duration_s for c in recent) if recent else 0.0
        run_s = sum(min(c.duration_s, cap) for c in recent)
        duty = min(1.0, run_s / covered_s)
        q_ratio, q_count = self._q_stats()
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
            "q_ratio": q_ratio if q_ratio is not None else math.nan,
            "q_count": float(q_count),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase": self.phase,
            "count": self.count,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "hour_counts": self.hour_counts,
            "probs": self.probs,
            "probs_ts": self.probs_ts if math.isfinite(self.probs_ts) else None,
            "probs_n": self.probs_n,
            # [start, end, on_w, off_w] plus dQ/dP when measured (older states have no fifth value)
            "recent": [
                [c.start_ts, c.end_ts, c.on_w, c.off_w, *([c.q_ratio] if c.q_ratio is not None else [])]
                for c in self.recent
            ],
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
            probs=d.get("probs"),
            probs_n=int(d.get("probs_n", 0)),
            probs_ts=float(d["probs_ts"]) if d.get("probs_ts") is not None else -math.inf,
        )
        for row in d["recent"]:
            s, e, on, off = row[:4]
            q = float(row[4]) if len(row) > 4 else None
            c.recent.append(Cycle(phase=c.phase, start_ts=s, end_ts=e, on_w=on, off_w=off, q_ratio=q))
        return c


def _clean(c: Cluster) -> list[Cycle]:
    """Cycles with a plausible run time (<= 1.5 x median), in time order; drops mis-paired long cycles."""
    med = statistics.median(x.duration_s for x in c.recent)
    return sorted((x for x in c.recent if x.duration_s <= 1.5 * med), key=lambda x: x.start_ts)


def _overlap_share(ref: list[Cycle], cycles: list[Cycle]) -> float:
    """Share of `cycles` that overlap a cycle of `ref` (other than themselves); both in time order."""
    starts = [x.start_ts for x in ref]
    longest = max(x.duration_s for x in ref)
    hits = 0
    for c in cycles:
        i = bisect.bisect_left(starts, c.end_ts) - 1  # cycles of ref that start before c ends, backwards
        while i >= 0 and ref[i].start_ts >= c.start_ts - longest:
            if ref[i] is not c and ref[i].end_ts > c.start_ts:
                hits += 1
                break
            i -= 1
    return hits / len(cycles)


def takes_turns(a: Cluster, b: Cluster, min_cycles: int = 10, min_expected: float = 5.0) -> bool:
    """True if two clusters behave like one device: they run in the same period but take turns.

    One device whose power drifts (a compressor draws 34 W on some runs and 39 W on others) splits into
    two clusters. Its cycles cannot overlap, except through detection noise (mis-paired steps), which
    shows up as overlaps of a cluster with itself. Two independent devices additionally overlap by
    chance: a cycle of `b` meets a cycle of `a` with probability p ~ (run time of a + number of a cycles
    x run time of b) / period. One device if the overlaps between the clusters stay at the noise level:
    below the midpoint between "noise" and "noise + chance", and clearly (2 standard errors) below the
    level two independent devices would reach.
    """
    if not a.recent or not b.recent:
        return False
    ca, cb = _clean(a), _clean(b)
    start = max(ca[0].start_ts, cb[0].start_ts)
    end = min(ca[-1].end_ts, cb[-1].end_ts)
    ca = [c for c in ca if start <= c.start_ts < end]
    cb = [c for c in cb if start <= c.start_ts < end]
    if end <= start or len(ca) < min_cycles or len(cb) < min_cycles:
        return False
    dur_b = statistics.median(c.duration_s for c in cb)
    p = min(1.0, (sum(c.duration_s for c in ca) + len(ca) * dur_b) / (end - start))
    if len(cb) * p < min_expected:
        return False  # too little evidence (short or rare runs): leave them apart
    noise = (_overlap_share(ca, ca) + _overlap_share(cb, cb)) / 2
    cross = _overlap_share(ca, cb)
    independent = noise + (1 - noise) * p
    stderr = math.sqrt(max(cross * (1 - cross), 0.01) / len(cb))
    return cross < noise + (1 - noise) * p / 2 and cross + 2 * stderr < independent


def _q_compatible(a: float | None, b: float | None, tolerance: float) -> bool:
    """False only if both reactive power ratios are known and clearly differ."""
    return a is None or b is None or abs(a - b) <= tolerance


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
            if cl.phase != c.phase or not cl.recent or not _q_compatible(c.q_ratio, cl.q_ratio, Q_ASSIGN_TOL):
                continue
            mismatch = abs(cl.power_w - c.power_w) / max(cl.power_w, c.power_w)
            if mismatch <= best_mismatch:
                best, best_mismatch = cl, mismatch
        if best is None:
            best = Cluster(id=self._new_id(c.phase), phase=c.phase)
            self.clusters[best.id] = best
        best.add(c, datetime.fromtimestamp(c.start_ts, self.tz).hour)
        return best

    def merge_close(self, keep: Collection[str] = ()) -> list[tuple[str, str]]:
        """Merge clusters on the same phase that belong to one device. Returns (kept, removed) pairs.

        Two clusters are merged when their typical power has converged, or when they are close in power
        and take turns: they run in the same weeks but (almost) never at the same time, which two
        independent devices could not avoid (see `takes_turns`). Ids in `keep` (clusters the user has
        decided on) are never removed: such a cluster absorbs the other one, and two of them stay apart.
        """
        merged: list[tuple[str, str]] = []
        ids = sorted(self.clusters, key=lambda k: self.clusters[k].first_ts)
        for i, a_id in enumerate(ids):
            for b_id in ids[i + 1 :]:
                a, b = self.clusters.get(a_id), self.clusters.get(b_id)
                if a is None or not a.recent:
                    break
                if b is None or b.phase != a.phase or not b.recent:
                    continue
                if a_id in keep and b_id in keep:
                    continue
                if not _q_compatible(a.q_ratio, b.q_ratio, Q_MERGE_TOL):
                    continue  # same power, different kind of load (e.g. heating vs. motor)
                mismatch = abs(a.power_w - b.power_w) / max(a.power_w, b.power_w)
                if mismatch > self.tolerance / 2 and not (mismatch <= self.tolerance and takes_turns(a, b)):
                    continue
                if b_id in keep:
                    kept, gone = b, a
                else:
                    kept, gone = a, b
                kept.absorb(gone)
                del self.clusters[gone.id]
                merged.append((kept.id, gone.id))
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
