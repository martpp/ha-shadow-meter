"""The detection pipeline: raw samples -> residual per phase -> steps -> cycles -> candidates.

For every phase the residual is the mains power minus all sub-metered devices on that phase (their
power is already known, so it must not be "detected" again). Steps in the residual are paired into
cycles, cycles are clustered into device candidates, and steps are matched with entity changes.
The detector is fed chunk by chunk (history day by day, live every few seconds); all state can be
serialised with `to_dict` so that an analysis survives restarts.
With an optional power factor sensor per phase, every cycle also gets the reactive/real power ratio of
its switch-on step (see `reactive.py`); without one everything works on the real power alone.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, tzinfo
from typing import Any

import numpy as np

from ..models import Sample, StateChange, Step
from .classify import classify
from .clusters import ClusterSet
from .context import ContextMatcher
from .cycles import CyclePairer
from .grid import CarryForwardGrid
from .reactive import ReactiveTracker, reactive_power
from .steps import StepDetector

STEP_S = 5.0


@dataclass(frozen=True, slots=True)
class PhaseConfig:
    """Whole-home sensor of one phase and the sub-meters on it (all entity ids of power sensors)."""

    phase: str
    mains: str
    submeters: tuple[str, ...] = ()
    power_factor: str | None = None  # optional power factor sensor of the mains (reactive power)


class Detector:
    def __init__(self, phases: Sequence[PhaseConfig], tz: tzinfo = UTC, min_step_w: float = 12.0) -> None:
        if not phases:
            raise ValueError("at least one phase is required")
        self.phases = {p.phase: p for p in phases}
        sensors = [e for p in phases for e in (p.mains, *p.submeters, *_optional(p.power_factor))]
        self.grids = {e: CarryForwardGrid(STEP_S) for e in sensors}
        self.steps = {p.phase: StepDetector(p.phase, STEP_S, min_w=min_step_w) for p in phases}
        self.pairers = {p.phase: CyclePairer(p.phase) for p in phases}
        self.reactive = {p.phase: ReactiveTracker(STEP_S) for p in phases if p.power_factor}
        self.clusters = ClusterSet(tz=tz)
        self.context = ContextMatcher()
        self.processed_until = -math.inf
        self.step_count = 0
        self.cycle_count = 0

    @property
    def entities(self) -> list[str]:
        """All power sensors the detector needs."""
        return list(self.grids)

    def process(
        self,
        samples: Mapping[str, Sequence[Sample]],
        changes: Sequence[StateChange],
        start: float,
        end: float,
    ) -> list[Step]:
        """Process [start, end). `samples` per entity (sorted), may include the last value before start."""
        start = max(start, self.processed_until)
        if end <= start:
            return []
        start = math.floor(start / STEP_S) * STEP_S
        new_steps: list[Step] = []
        for c in sorted(changes, key=lambda c: c.ts):
            if start <= c.ts < end:
                self.context.add_change(c)
        for phase, cfg in self.phases.items():
            mains = self.grids[cfg.mains].fill(samples.get(cfg.mains, ()), start, end)
            residual = mains
            for sub in cfg.submeters:
                sub_values = self.grids[sub].fill(samples.get(sub, ()), start, end)
                residual = residual - np.nan_to_num(sub_values)  # unknown sub-meter -> assume 0 W
            phase_steps = self.steps[phase].feed(residual, start)
            tracker = self.reactive.get(phase)
            if tracker is not None and cfg.power_factor:
                pf = self.grids[cfg.power_factor].fill(samples.get(cfg.power_factor, ()), start, end)
                tracker.add(math.floor(start / STEP_S) * STEP_S, residual, reactive_power(mains, pf))
                for step in phase_steps:
                    tracker.watch(step)
                tracker.resolve()  # before pairing: a cycle takes the value of its switch-on step
            new_steps.extend(phase_steps)
        new_steps.sort(key=lambda s: s.ts)
        for step in new_steps:
            self.context.add_step(step)
            cycle = self.pairers[step.phase].feed(step)
            if cycle is not None:
                tracker = self.reactive.get(cycle.phase)
                if tracker is not None:
                    cycle = replace(cycle, q_ratio=tracker.ratio(cycle.start_ts))
                self.clusters.add(cycle)
                self.cycle_count += 1
        for tracker in self.reactive.values():
            tracker.forget(end)
        self.step_count += len(new_steps)
        self.context.flush(end)
        self.processed_until = end
        return new_steps

    def maintain(self, keep: Collection[str] = ()) -> list[tuple[str, str]]:
        """Housekeeping: merge clusters of one device, forget noise. Returns merged ids (kept, removed).

        Clusters in `keep` (the user decided on them) are never merged away."""
        merged = self.clusters.merge_close(keep)
        if math.isfinite(self.processed_until):
            self.clusters.prune(self.processed_until)
        return merged

    def candidates(self, min_cycles: int = 5, min_entity_changes: int = 5) -> list[dict[str, Any]]:
        """All current device candidates, best first. Plain dicts for the store and the API."""
        out: list[dict[str, Any]] = []
        for cl in self.clusters.clusters.values():
            if cl.count < min_cycles:
                continue
            f = cl.features()
            c = classify(f)
            out.append(
                {
                    "id": f"cluster:{cl.id}",
                    "source": "pattern",
                    "phase": cl.phase,
                    "type": c.type,
                    "probabilities": c.probabilities,
                    "confidence": c.confidence,
                    "power_w": f["power_w"],
                    "features": f,
                    "first_seen": cl.first_ts,
                    "last_seen": cl.last_ts,
                    "entity_id": None,
                }
            )
        for st in self.context.stats.values():
            s = st.summary()
            if s["n"] < min_entity_changes:
                continue
            out.append(
                {
                    "id": f"entity:{st.entity_id}",
                    "source": "entity",
                    "phase": s["phase"],
                    "type": "light" if st.entity_id.startswith("light.") else "other",
                    "probabilities": {},
                    "confidence": st.confidence(),
                    "power_w": s["power_w"],
                    "features": {
                        "match_rate": s["match_rate"],
                        "chance_rate": s["chance_rate"],
                        "lift": s["lift"],
                        "power_spread": s["spread"],
                        "count": s["n"],
                    },
                    "first_seen": None,
                    "last_seen": None,
                    "entity_id": st.entity_id,
                }
            )
        _link_explained(out)
        out.sort(key=lambda c: c["confidence"], reverse=True)
        return out

    def running(self) -> dict[str, list[tuple[float, float]]]:
        """Open switch-on steps per phase (ts, watts): what may be running right now."""
        return {p: pr.open_steps for p, pr in self.pairers.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "clusters": self.clusters.to_dict(),
            "context": self.context.to_dict(),
            "processed_until": self.processed_until,
            "step_count": self.step_count,
            "cycle_count": self.cycle_count,
        }

    def load(self, d: dict[str, Any]) -> None:
        self.clusters.load(d.get("clusters", {}))
        self.context.load(d.get("context", {}))
        self.processed_until = float(d.get("processed_until", -math.inf))
        self.step_count = int(d.get("step_count", 0))
        self.cycle_count = int(d.get("cycle_count", 0))


def _optional(entity_id: str | None) -> tuple[str, ...]:
    return (entity_id,) if entity_id else ()


def _link_explained(cands: list[dict[str, Any]]) -> None:
    """Mark pattern candidates that are probably the same device as a confident entity candidate."""
    entities = [c for c in cands if c["source"] == "entity" and c["confidence"] >= 0.5]
    for c in cands:
        c["explained_by"] = None
        if c["source"] != "pattern":
            continue
        for e in entities:
            similar = abs(e["power_w"] - c["power_w"]) <= 0.2 * max(e["power_w"], c["power_w"])
            if e["phase"] == c["phase"] and similar:
                c["explained_by"] = e["id"]
                break
