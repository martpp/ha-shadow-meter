"""Streaming step detection on a regular grid.

A step at grid index i is median(v[i..i+2]) - median(v[i-3..i-1]): the change between the three values
after and the three values before. Medians make it robust against single spikes (e.g. an inrush peak
lasting one sample). Only local maxima of |step| are reported, so one switching event gives one step.
"""

from __future__ import annotations

import numpy as np

from ..models import Step
from .grid import FloatArray

_BEFORE = 3  # samples before the step
_AFTER = 3  # samples after the step (including the step sample)
_TAIL = _BEFORE + _AFTER + 2  # samples kept between chunks


class StepDetector:
    """Detects steps on one phase; feed consecutive grid chunks with `feed`."""

    def __init__(
        self,
        phase: str,
        step_s: float = 5.0,
        min_w: float = 12.0,
        max_w: float = 15000.0,
        min_gap_s: float = 10.0,
    ) -> None:
        self.phase = phase
        self.step_s = step_s
        self.min_w = min_w
        self.max_w = max_w
        self.min_gap_s = min_gap_s
        self._tail = np.empty(0)
        self._tail_start = 0.0
        self._last_step_ts = -np.inf

    def feed(self, values: FloatArray, start_ts: float) -> list[Step]:
        """Process the grid values starting at start_ts; returns the steps that are final now.

        Steps close to the end of the chunk are only reported with the next chunk, when the values
        after them are known.
        """
        if self._tail.size and not np.isclose(self._tail_start + self._tail.size * self.step_s, start_ts):
            self._tail = np.empty(0)  # not contiguous (gap between chunks): start over
        if not self._tail.size:
            self._tail_start = start_ts
        v = np.concatenate([self._tail, values])
        t0 = self._tail_start
        steps: list[Step] = []
        n = v.size
        if n >= _BEFORE + _AFTER + 2:
            windows = np.lib.stride_tricks.sliding_window_view(v, 3)
            med = np.median(windows, axis=1)  # med[j] = median(v[j..j+2]); NaN if any NaN
            # d[i] for i in [_BEFORE, n - _AFTER]: med[i] - med[i - 3]
            i = np.arange(_BEFORE, n - _AFTER + 1)
            d = med[i] - med[i - _BEFORE]
            a = np.abs(np.nan_to_num(d))
            # local maximum: >= left neighbour, > right neighbour (only interior positions are final);
            # vectorised pre-selection, so the Python loop only runs over real peaks
            mid = a[1:-1]
            peaks = (mid >= self.min_w) & (mid <= self.max_w) & (mid >= a[:-2]) & (mid > a[2:])
            for k in np.flatnonzero(peaks) + 1:
                # a clean step gives a plateau of equal |d| values; report its middle
                j = k
                while j > 0 and np.isclose(a[j - 1], a[k]):
                    j -= 1
                ts = float(t0 + i[(j + k) // 2] * self.step_s)
                if ts - self._last_step_ts < self.min_gap_s:
                    continue
                self._last_step_ts = ts
                steps.append(Step(ts=ts, phase=self.phase, delta_w=float(d[k])))
        keep = min(n, _TAIL)
        self._tail = v[n - keep :]
        self._tail_start = t0 + (n - keep) * self.step_s
        return steps
