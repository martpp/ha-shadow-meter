"""Turn irregular sensor reports into a regular time grid.

Home Assistant only records a value when it *changes*. A device drawing a constant 70 W may report only
every few minutes. The value therefore has to be carried forward until the next report - treating a
missing report as 0 W creates fake on/off "devices" in everything that is computed from the grid.
Only after `max_hold_s` without any report the value becomes unknown (NaN), e.g. sensor offline.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np
import numpy.typing as npt

from ..models import Sample

FloatArray = npt.NDArray[np.float64]


class CarryForwardGrid:
    """Grid for one sensor; keeps the last value between calls so chunks can be processed one by one."""

    def __init__(self, step_s: float = 5.0, max_hold_s: float = 6 * 3600) -> None:
        if step_s <= 0:
            raise ValueError("step_s must be positive")
        self.step_s = step_s
        self.max_hold_s = max_hold_s
        self._last_ts: float | None = None
        self._last_value = math.nan

    def align(self, ts: float) -> float:
        """Start of the grid cell that contains ts."""
        return math.floor(ts / self.step_s) * self.step_s

    def fill(self, samples: Iterable[Sample], start: float, end: float) -> FloatArray:
        """Values at grid points start, start+step, ... < end.

        Samples before `start` only update the carried value; samples at or after `end` and non-finite
        values (unavailable) are ignored. Vectorised: this runs for every sensor and every chunk.
        """
        start = self.align(start)
        n = max(0, math.ceil((end - start) / self.step_s))
        pairs = [(s.ts, s.value) for s in samples]
        ts = np.array([p[0] for p in pairs], dtype=np.float64)
        vals = np.array([p[1] for p in pairs], dtype=np.float64)
        keep = (ts < end) & np.isfinite(vals)
        ts, vals = ts[keep], vals[keep]
        if self._last_ts is not None:  # the carried value from the previous chunk comes first
            ts = np.concatenate(([self._last_ts], ts))
            vals = np.concatenate(([self._last_value], vals))
        out = np.full(n, np.nan)
        if not ts.size:
            return out
        order = np.argsort(ts, kind="stable")
        ts, vals = ts[order], vals[order]
        # first grid index at or after each sample; the sample is valid from there on
        pos = np.where(ts > start, np.ceil((ts - start) / self.step_s), 0).astype(np.int64)
        idx = np.arange(n)
        k = np.searchsorted(pos, idx, side="right") - 1  # last sample at or before each grid point
        has = k >= 0
        kk = k[has]
        held = start + idx[has] * self.step_s - ts[kk] <= self.max_hold_s
        out[has] = np.where(held, vals[kk], np.nan)
        self._last_ts, self._last_value = float(ts[-1]), float(vals[-1])
        return out
