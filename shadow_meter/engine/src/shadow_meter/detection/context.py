"""Match on/off changes of Home Assistant entities (lights, switches, ...) with power steps.

Home Assistant knows *when* a light was switched. If the whole-home power steps by a similar amount at
almost the same moment, again and again, that step size is the power of the light. This is the main
advantage of doing NILM inside Home Assistant.

Lights often switch together (scenes). A step that coincides with several entities changing is the sum
of all of them, so only "solo" changes - no other watched entity changed within the window - are used.

In a busy home there is a step every minute or so, so a window of +-20 s often contains a step by pure
chance. For every change the same window is therefore also checked at a control time (90 s earlier),
where nothing was switched. The confidence only counts matches *above* this chance level.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..models import StateChange, Step

WINDOW_S = 20.0  # HA timestamps and meter reports are not perfectly aligned
CONTROL_OFFSET_S = 90.0  # control window, far enough from the change itself
MAX_DELTAS = 500


@dataclass
class EntityStats:
    entity_id: str
    changes: int = 0  # solo changes evaluated
    matched: int = 0  # ... with a matching step
    control_hits: int = 0  # ... with a matching step at the control time (chance)
    deltas: dict[str, deque[float]] = field(default_factory=dict)  # phase -> |step| of matched changes

    def summary(self) -> dict[str, Any]:
        chance = self.control_hits / self.changes if self.changes else 0.0
        if not self.deltas:
            return {
                "phase": None,
                "power_w": math.nan,
                "match_rate": 0.0,
                "chance_rate": chance,
                "lift": 0.0,
                "spread": math.inf,
                "n": 0,
            }
        phase = max(self.deltas, key=lambda p: len(self.deltas[p]))
        d = sorted(self.deltas[phase])
        med = _shorth_median(d)
        q1, q3 = d[len(d) // 4], d[(3 * len(d)) // 4]
        rate = self.matched / self.changes if self.changes else 0.0
        return {
            "phase": phase,
            "power_w": med,
            "match_rate": rate,
            "chance_rate": chance,
            "lift": max(0.0, (rate - chance) / (1 - chance)) if chance < 1 else 0.0,
            "spread": (q3 - q1) / med if med > 0 else math.inf,
            "n": len(d),
        }

    def confidence(self) -> float:
        s = self.summary()
        evidence = 1 - math.exp(-float(s["n"]) / 15)
        return round(float(s["lift"]) * evidence / (1 + float(s["spread"])), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "changes": self.changes,
            "matched": self.matched,
            "control_hits": self.control_hits,
            "deltas": {p: list(v) for p, v in self.deltas.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EntityStats:
        return cls(
            entity_id=d["entity_id"],
            changes=d["changes"],
            matched=d["matched"],
            control_hits=d.get("control_hits", 0),
            deltas={p: deque(v, maxlen=MAX_DELTAS) for p, v in d["deltas"].items()},
        )


def _shorth_median(values: list[float]) -> float:
    """Median of the densest half of the sorted values (the "shorth").

    Steps that happen to coincide with a switching event by chance are spread over all sizes, while the
    real ones are close together; the densest half ignores the stray ones better than the plain median.
    """
    n = len(values)
    if n < 4:
        return statistics.median(values)
    h = n // 2 + 1
    i = min(range(n - h + 1), key=lambda k: values[k + h - 1] - values[k])
    return statistics.median(values[i : i + h])


class ContextMatcher:
    """Feed steps and state changes (each in time order), then call `flush(now)` regularly."""

    def __init__(self, window_s: float = WINDOW_S, min_w: float = 5.0) -> None:
        self.window_s = window_s
        self.min_w = min_w
        self.stats: dict[str, EntityStats] = {}
        self._steps: deque[Step] = deque()
        self._pending: deque[StateChange] = deque()  # not evaluated yet
        self._evaluated: deque[StateChange] = deque()  # evaluated, still close enough to matter

    def add_step(self, s: Step) -> None:
        self._steps.append(s)

    def add_change(self, c: StateChange) -> None:
        self._pending.append(c)

    def flush(self, now: float) -> None:
        """Evaluate all changes whose window has fully passed (ts + window <= now)."""
        while self._pending and self._pending[0].ts + self.window_s <= now:
            c = self._pending.popleft()
            if not self._is_solo(c):
                self._evaluated.append(c)
                continue
            st = self.stats.setdefault(c.entity_id, EntityStats(c.entity_id))
            st.changes += 1
            sign = 1 if c.on else -1
            near = self._near(c.ts, sign)
            if self._near(c.ts - CONTROL_OFFSET_S, sign):
                st.control_hits += 1
            if near:
                best = min(near, key=lambda s: abs(s.ts - c.ts))
                st.matched += 1
                st.deltas.setdefault(best.phase, deque(maxlen=MAX_DELTAS)).append(abs(best.delta_w))
            self._evaluated.append(c)
        cutoff = now - 3 * self.window_s - CONTROL_OFFSET_S
        while self._steps and self._steps[0].ts < cutoff:
            self._steps.popleft()
        while self._evaluated and self._evaluated[0].ts < cutoff:
            self._evaluated.popleft()

    def _near(self, ts: float, sign: int) -> list[Step]:
        """Steps of the given direction within the window around ts."""
        return [s for s in self._steps if abs(s.ts - ts) <= self.window_s and s.delta_w * sign >= self.min_w]

    def _is_solo(self, c: StateChange) -> bool:
        """True if no other entity changed within the window before or after c."""
        return not any(
            o.entity_id != c.entity_id and abs(o.ts - c.ts) <= self.window_s
            for o in (*self._evaluated, *self._pending)
        )

    def to_dict(self) -> dict[str, Any]:
        return {"stats": [s.to_dict() for s in self.stats.values()]}

    def load(self, d: dict[str, Any]) -> None:
        self.stats = {s["entity_id"]: EntityStats.from_dict(s) for s in d.get("stats", [])}
