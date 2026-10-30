"""Data sources: Home Assistant (history of the last days + live) and optionally InfluxDB (long history)."""

from __future__ import annotations

import math
from typing import Protocol

from ..models import Sample, StateChange


class HistorySource(Protocol):
    async def power(self, entity_ids: list[str], start: float, end: float) -> dict[str, list[Sample]]:
        """Power samples per entity in [start, end), plus the last value before start if known."""
        ...

    async def changes(self, entity_ids: list[str], start: float, end: float) -> list[StateChange]:
        """On/off changes of the given entities in [start, end), sorted by time."""
        ...


ON_STATES = {"on", "open", "playing", "home", "true", "1", "1.0"}
OFF_STATES = {"off", "closed", "idle", "paused", "standby", "not_home", "false", "0", "0.0"}


def parse_power(state: object) -> float:
    """HA state string -> watts; NaN for unavailable/unknown/garbage."""
    try:
        v = float(str(state))
    except ValueError:
        return math.nan
    return v if math.isfinite(v) else math.nan


def parse_on(state: object) -> bool | None:
    s = str(state).lower()
    if s in ON_STATES:
        return True
    if s in OFF_STATES:
        return False
    return None  # unavailable, unknown, ...


def to_changes(entity_id: str, states: list[tuple[float, object]]) -> list[StateChange]:
    """Collapse a state history into real on/off changes (repeated 'on' reports are not changes)."""
    out: list[StateChange] = []
    last: bool | None = None
    for ts, st in states:
        on = parse_on(st)
        if on is None:
            last = None  # after 'unavailable' the next known state is not a real switching event
            continue
        if last is not None and on != last:
            out.append(StateChange(ts, entity_id, on))
        last = on
    return out
