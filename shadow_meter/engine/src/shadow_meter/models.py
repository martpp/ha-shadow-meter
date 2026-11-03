"""Small immutable value types shared by the detection pipeline.

All timestamps are Unix epoch seconds (float, UTC). Power is in watts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Sample:
    """One reported value of a power sensor."""

    ts: float
    value: float


@dataclass(frozen=True, slots=True)
class Step:
    """A sudden change of the power on one phase (positive = something switched on)."""

    ts: float
    phase: str
    delta_w: float


@dataclass(frozen=True, slots=True)
class StateChange:
    """An on/off change of a Home Assistant entity (light, switch, ...)."""

    ts: float
    entity_id: str
    on: bool


@dataclass(frozen=True, slots=True)
class Cycle:
    """A switch-on step paired with its matching switch-off step on the same phase."""

    phase: str
    start_ts: float
    end_ts: float
    on_w: float  # size of the switch-on step (may include an inrush peak)
    off_w: float  # size of the switch-off step (closer to the running power)
    # reactive / real power of the switch-on step (dQ/dP): ~0 for heating elements, clearly > 0 for
    # motors; None without a power factor sensor or when the step could not be measured cleanly
    q_ratio: float | None = None

    @property
    def duration_s(self) -> float:
        return self.end_ts - self.start_ts

    @property
    def power_w(self) -> float:
        """Best estimate of the running power: the switch-off step has no inrush peak."""
        return self.off_w
