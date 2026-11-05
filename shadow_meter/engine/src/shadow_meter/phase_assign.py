"""Find the phase of devices with their own meter (smart plugs) from the history.

Few people know on which phase a socket is. But every time a plug's device switches, the power of one
phase of the whole-home meter changes by the same amount at the same moment. For every clear step of a
plug (>= MIN_STEP_W) the change of each phase across the step is compared with the plug's change: on
the right phase the ratio is ~1. A device whose steps never show up on any phase is not behind the
whole-home meter at all (e.g. behind a UPS or on another circuit) and must not be subtracted.

The history is processed day by day and only counters are kept, so a week of data for dozens of plugs
needs little memory (it also runs on a Raspberry Pi).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from .detection.grid import CarryForwardGrid, FloatArray
from .models import Sample

STEP_S = 5.0
MIN_STEP_W = 30.0
WINDOW = (2, 12)  # samples before/after the step that form the plateaus: 10-60 s
MIN_GAP = 24  # samples between two steps of one plug (2 min)
HIT = 0.35  # |phase change / plug change - 1| below this: the step shows up on that phase
MIN_STEPS = 6


@dataclass
class Evidence:
    """Counters for one plug: its steps and on which phases they showed up."""

    steps: int = 0
    hits: dict[str, int] = field(default_factory=dict)

    def result(self) -> dict[str, object]:
        shares = {p: (h / self.steps if self.steps else 0.0) for p, h in self.hits.items()}
        ranked = sorted(shares, key=lambda p: -shares[p])
        best = ranked[0] if ranked else None
        second = shares[ranked[1]] if len(ranked) > 1 else 0.0
        # Other devices switching at the same moment spoil many single comparisons, so a phase does not
        # need most of the steps - it needs clearly more than the other phases (which see ~none).
        if self.steps < MIN_STEPS or best is None:
            verdict, phase = "unsure", None  # too few switching events to tell
        elif shares[best] >= 0.2 and shares[best] >= 3 * second + 0.02:
            verdict, phase = "phase", best
        elif shares[best] < 0.15 and self.steps >= 10:
            verdict, phase = "not_behind", None  # its steps never show up on the whole-home meter
        else:
            verdict, phase = "unsure", None
        return {"verdict": verdict, "phase": phase, "steps": self.steps, "shares": shares}


def _plateau(v: FloatArray, idx: np.ndarray, sign: int) -> FloatArray:
    """Median of the samples WINDOW after (sign=1) or before (sign=-1) each index."""
    a, b = WINDOW
    offsets = np.arange(a, b + 1) * sign
    pos = np.clip(idx[:, None] + offsets[None, :], 0, len(v) - 1)
    return np.median(v[pos], axis=1)


def _steps(v: FloatArray) -> np.ndarray:
    """Indices of clear steps of a plug: plateau change >= MIN_STEP_W, the largest within MIN_GAP."""
    n = len(v)
    b = WINDOW[1]
    if n < 2 * b + 2:
        return np.empty(0, dtype=np.int64)
    idx = np.arange(b, n - b)
    d = _plateau(v, idx, 1) - _plateau(v, idx, -1)
    mag = np.abs(np.nan_to_num(d))
    out: list[int] = []
    for k in np.flatnonzero(mag >= MIN_STEP_W):
        lo, hi = max(0, k - MIN_GAP), min(len(mag), k + MIN_GAP + 1)
        if mag[k] == mag[lo:hi].max() and (not out or idx[k] - out[-1] >= MIN_GAP):
            out.append(int(idx[k]))
    return np.array(out, dtype=np.int64)


class PhaseFinder:
    """Feed the history day by day with `add`; `results` gives the phase of every plug."""

    def __init__(self, phases: Mapping[str, str], plugs: Sequence[str]) -> None:
        self.phases = dict(phases)  # phase name -> whole-home power sensor
        self.plugs = list(plugs)
        self.evidence = {p: Evidence(hits=dict.fromkeys(self.phases, 0)) for p in self.plugs}

    @property
    def entities(self) -> list[str]:
        return [*self.phases.values(), *self.plugs]

    def add(self, samples: Mapping[str, Sequence[Sample]], start: float, end: float) -> None:
        grid = {e: CarryForwardGrid(STEP_S).fill(samples.get(e, ()), start, end) for e in self.entities}
        mains = {p: grid[e] for p, e in self.phases.items()}
        for plug in self.plugs:
            v = grid[plug]
            idx = _steps(v)
            if not idx.size:
                continue
            dm = _plateau(v, idx, 1) - _plateau(v, idx, -1)
            dp = {phase: _plateau(m, idx, 1) - _plateau(m, idx, -1) for phase, m in mains.items()}
            ok = np.isfinite(dm) & np.logical_and.reduce([np.isfinite(x) for x in dp.values()])
            ev = self.evidence[plug]
            ev.steps += int(ok.sum())
            for phase, x in dp.items():
                ev.hits[phase] += int((np.abs(x[ok] / dm[ok] - 1) <= HIT).sum())

    def results(self) -> dict[str, dict[str, object]]:
        return {p: ev.result() for p, ev in self.evidence.items()}
