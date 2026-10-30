"""Pair switch-on steps with matching switch-off steps into on/off cycles.

Several devices can run on the same phase at the same time, so an off-step is matched with the open
on-step of the most similar size (within a relative tolerance and a plausible duration).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Cycle, Step


@dataclass(slots=True)
class _Open:
    ts: float
    on_w: float


class CyclePairer:
    """Pairs steps of one phase. Feed steps in time order."""

    def __init__(
        self,
        phase: str,
        tolerance: float = 0.35,
        min_duration_s: float = 10.0,
        max_duration_s: float = 6 * 3600,
        max_open: int = 200,
    ) -> None:
        self.phase = phase
        self.tolerance = tolerance
        self.min_duration_s = min_duration_s
        self.max_duration_s = max_duration_s
        self.max_open = max_open
        self._open: list[_Open] = []

    @property
    def open_steps(self) -> list[tuple[float, float]]:
        """Currently unmatched switch-on steps as (ts, watts): devices that may be running now."""
        return [(o.ts, o.on_w) for o in self._open]

    def feed(self, step: Step) -> Cycle | None:
        self._expire(step.ts)
        if step.delta_w > 0:
            self._open.append(_Open(step.ts, step.delta_w))
            if len(self._open) > self.max_open:
                self._open.pop(0)
            return None
        off_w = -step.delta_w
        best: tuple[float, int] | None = None
        for k, o in enumerate(self._open):
            duration = step.ts - o.ts
            if duration < self.min_duration_s:
                continue
            mismatch = abs(o.on_w - off_w) / max(o.on_w, off_w)
            if mismatch <= self.tolerance and (best is None or mismatch < best[0]):
                best = (mismatch, k)
        if best is None:
            return None
        o = self._open.pop(best[1])
        return Cycle(phase=self.phase, start_ts=o.ts, end_ts=step.ts, on_w=o.on_w, off_w=off_w)

    def _expire(self, now: float) -> None:
        self._open = [o for o in self._open if now - o.ts <= self.max_duration_s]
