"""Reactive power of switch-on steps, from the power factor of the whole-home meter (optional).

With a power factor sensor on a phase, the reactive power is Q = |P| * sqrt(1/pf^2 - 1) of the mains.
For every switch-on step the plateaus before ([-100 s, -15 s]) and after ([+45 s, +150 s]) are compared:
dQ/dP is ~0 for heating elements (kettle, hob, heater) and clearly above 0 for motors (a fridge
compressor ~0.2). The plateau after the step starts late because Home Assistant records the power
factor less often than the power (only when it changes).

A step is only measured when both plateaus are steady and the change of the real power between them
matches the step: another device switching inside the windows (or the device switching off again, as
the short pulses of a hob do) makes the value unreliable, so it is left out (None). Sub-meters have no
power factor, so their reactive power cannot be subtracted; their switching is already removed from the
real power and does not produce steps.
"""

from __future__ import annotations

import math

import numpy as np

from ..models import Step
from .grid import FloatArray

PRE_S = (-100.0, -15.0)  # ends a little before the step: its reported time can be a sample late
POST_S = (45.0, 150.0)
KEEP_S = 600.0  # values kept between chunks: enough for the windows of steps that wait for more data
MAX_WAIT_S = 6 * 3600.0  # forget measured steps that never became a cycle


def _spread(x: FloatArray) -> float:
    """Range of the middle 80 % of the values (robust against single spikes)."""
    lo, hi = np.percentile(x, [10, 90])
    return float(hi - lo)


def reactive_power(p_mains: FloatArray, pf: FloatArray) -> FloatArray:
    """Q of the mains from P and the power factor (in % or as a fraction; the sign is ignored)."""
    f = np.abs(pf)
    f = np.where(f > 1.0, f / 100.0, f)  # Home Assistant usually reports %, some meters a fraction
    f = np.clip(f, 0.05, 1.0)
    return np.abs(p_mains) * np.sqrt(1.0 / f**2 - 1.0)


class ReactiveTracker:
    """dQ/dP of the switch-on steps of one phase. Feed contiguous chunks with `add`, steps with `watch`."""

    def __init__(self, step_s: float = 5.0) -> None:
        self.step_s = step_s
        self._t0 = 0.0
        self._p = np.empty(0)
        self._q = np.empty(0)
        self._pending: list[Step] = []
        self._ratios: dict[float, float | None] = {}

    def add(self, start: float, residual: FloatArray, q: FloatArray) -> None:
        """Append the grid values of a chunk (residual real power and mains reactive power)."""
        if self._p.size and not math.isclose(self._t0 + self._p.size * self.step_s, start):
            self._p, self._q = np.empty(0), np.empty(0)  # not contiguous: the old values do not fit
        if not self._p.size:
            self._t0 = start
        self._p = np.concatenate([self._p, residual])
        self._q = np.concatenate([self._q, q])

    def watch(self, step: Step) -> None:
        if step.delta_w > 0:
            self._pending.append(step)

    def resolve(self) -> None:
        """Measure all watched steps whose windows are complete; then drop values no longer needed."""
        end = self._t0 + self._p.size * self.step_s
        waiting = []
        for s in self._pending:
            if s.ts + POST_S[1] <= end:
                self._ratios[s.ts] = self._measure(s)
            else:
                waiting.append(s)
        self._pending = waiting
        oldest = min([end - KEEP_S, *(s.ts + PRE_S[0] for s in waiting)])
        drop = int(max(0.0, (oldest - self._t0) // self.step_s))
        if drop:
            self._p, self._q = self._p[drop:], self._q[drop:]
            self._t0 += drop * self.step_s

    def ratio(self, on_ts: float) -> float | None:
        """dQ/dP of the switch-on step at on_ts (once), or None if it was not measured."""
        return self._ratios.pop(on_ts, None)

    def forget(self, now: float) -> None:
        """Drop measured steps that can no longer become a cycle. Call after the steps up to `now` were
        paired: a chunk can cover a whole day, and its early steps are paired only at the end of it."""
        for ts in [t for t in self._ratios if t < now - MAX_WAIT_S]:
            del self._ratios[ts]

    def _window(self, ts: float, rel: tuple[float, float]) -> slice:
        a = math.ceil((ts + rel[0] - self._t0) / self.step_s)
        b = math.floor((ts + rel[1] - self._t0) / self.step_s) + 1
        return slice(max(a, 0), max(b, 0))

    def _measure(self, s: Step) -> float | None:
        pre, post = self._window(s.ts, PRE_S), self._window(s.ts, POST_S)
        p0, p1, q0, q1 = self._p[pre], self._p[post], self._q[pre], self._q[post]
        if p0.size < 10 or p1.size < 10:
            return None
        if not (
            np.isfinite(p0).all()
            and np.isfinite(p1).all()
            and np.isfinite(q0).all()
            and np.isfinite(q1).all()
        ):
            return None
        # medians and the 10-90 % range: sub-meters report at other moments than the mains, which leaves
        # short spikes in the residual that must not make a plateau look unsteady
        dp = float(np.median(p1) - np.median(p0))
        steady = max(12.0, 0.25 * abs(dp))
        if _spread(p0) > steady or _spread(p1) > steady:
            return None
        # the running power may be well below the step (inrush of a motor), but not above it
        if not 0.5 * s.delta_w <= dp <= 1.25 * s.delta_w:
            return None
        return float(np.median(q1) - np.median(q0)) / dp
