"""A small synthetic household for tests.

Phase A: base load + fridge (37 W running, 12 W inrush in the first sample, ~20 min on / 40 min off)
         + dishwasher with its own meter (1800 W heating blocks; must NOT be detected)
Phase B: base load + kettle (2200 W, 3 min, at 07:00, 12:00 and 19:00)
Phase C: base load + hallway light (21 W), switched by light.hallway at random times

Like Home Assistant, sensors only report when their value changes: the mains sensors change all the
time (noise), the dishwasher meter reports only when it switches.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from shadow_meter.models import Sample, StateChange

T0 = 1_767_225_600.0  # 2026-01-01 00:00 UTC
DT = 3.0  # mains report interval


@dataclass
class House:
    days: int = 3
    seed: int = 1
    samples: dict[str, list[Sample]] = field(default_factory=dict)
    changes: list[StateChange] = field(default_factory=list)
    fridge_cycles: int = 0


def make_house(days: int = 3, seed: int = 1) -> House:
    rnd = random.Random(seed)
    end = T0 + days * 86400
    n = int((end - T0) / DT)
    t = [T0 + i * DT for i in range(n)]
    a = [150.0 + rnd.gauss(0, 2) for _ in t]
    b = [80.0 + rnd.gauss(0, 2) for _ in t]
    c = [60.0 + rnd.gauss(0, 2) for _ in t]
    h = House(days=days, seed=seed)

    def add(series: list[float], start: float, stop: float, watts: float, inrush: float = 0.0) -> None:
        i0, i1 = int((start - T0) / DT), int((stop - T0) / DT)
        for i in range(max(0, i0), min(n, i1)):
            series[i] += watts
        if inrush and 0 <= i0 < n:
            series[i0] += inrush

    # fridge
    ts = T0 + 600
    while ts < end:
        run = rnd.uniform(17, 23) * 60
        add(a, ts, ts + run, 37.0, inrush=12.0)
        h.fridge_cycles += 1
        ts += run + rnd.uniform(35, 45) * 60
    # dishwasher (own meter, reports only on change)
    dish: list[Sample] = [Sample(T0 - 1, 0.5)]
    for d in range(days):
        for k in range(3):
            s = T0 + d * 86400 + 20 * 3600 + k * 1800
            add(a, s, s + 900, 1800.0)
            dish += [Sample(s, 1800.5), Sample(s + 900, 0.5)]
    # kettle
    for d in range(days):
        for hour in (7, 12, 19):
            s = T0 + d * 86400 + hour * 3600 + rnd.uniform(0, 600)
            add(b, s, s + 180, 2200.0)
    # hallway light, switched alone
    for d in range(days):
        for _ in range(12):
            s = T0 + d * 86400 + rnd.uniform(6, 23) * 3600
            dur = rnd.uniform(60, 300)
            add(c, s, s + dur, 21.0)
            h.changes += [StateChange(s, "light.hallway", True), StateChange(s + dur, "light.hallway", False)]
    h.changes.sort(key=lambda x: x.ts)
    h.samples = {
        "sensor.phase_a": [Sample(x, round(v, 1)) for x, v in zip(t, a, strict=True)],
        "sensor.phase_b": [Sample(x, round(v, 1)) for x, v in zip(t, b, strict=True)],
        "sensor.phase_c": [Sample(x, round(v, 1)) for x, v in zip(t, c, strict=True)],
        "sensor.dishwasher": dish,
    }
    return h
