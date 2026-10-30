"""Meter setup helpers for the web UI: list the power sensors of Home Assistant, suggest phases.

Home Assistant app options cannot offer an entity picker, so the meters are chosen in the app's own UI.
"""

from __future__ import annotations

import re
from typing import Any

from .settings import ENTITY_RE

# Common names of per-phase power sensors: Shelly 3EM "channel_a", Shelly Pro 3EM "phase_a",
# smart meter readers "l1" / "power_l1", ...
_PHASE_RE = re.compile(r"^(?P<prefix>.*?)(?:channel|phase|_l)_?(?P<p>[abc123])(?=_|$)(?P<rest>.*)$")
_PHASE_NAME = {"a": "A", "b": "B", "c": "C", "1": "A", "2": "B", "3": "C"}


def power_sensors(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sensors in watts, sorted by name: {entity_id, name, value}."""
    out = []
    for st in states:
        eid = st.get("entity_id", "")
        attrs = st.get("attributes") or {}
        if not eid.startswith("sensor.") or not ENTITY_RE.fullmatch(eid):
            continue
        if attrs.get("unit_of_measurement") != "W":
            continue
        try:
            value: float | None = float(st.get("state"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value = None
        out.append({"entity_id": eid, "name": str(attrs.get("friendly_name") or eid), "value": value})
    return sorted(out, key=lambda s: s["name"].lower())


def suggest_phases(sensors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Three sensors that look like the phases of one meter (A, B, C), or [] if there is no clear match."""
    groups: dict[tuple[str, str], dict[str, str]] = {}
    for s in sensors:
        m = _PHASE_RE.match(s["entity_id"])
        if m and "power" in s["entity_id"]:
            key = (m["prefix"], m["rest"])
            groups.setdefault(key, {})[_PHASE_NAME[m["p"]]] = s["entity_id"]
    complete = [g for g in groups.values() if set(g) == {"A", "B", "C"}]
    if len(complete) != 1:
        return []
    return [{"phase": ph, "mains": complete[0][ph], "submeters": []} for ph in ("A", "B", "C")]
