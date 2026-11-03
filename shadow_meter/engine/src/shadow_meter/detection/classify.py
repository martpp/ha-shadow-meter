"""Rule-based device typing with a probability, and a confidence that grows with evidence.

This is deliberately simple and transparent: each device type has typical ranges for a few features
(power, run time, cycles per day, how evenly the cycles are spread over the day). A soft membership
score per type is normalised into probabilities. The confidence combines the probability of the best
type with the amount of evidence (number of cycles) and how consistent the power is.

The numbers are plausible defaults for European households, not a trained model. They are meant to be
replaced or complemented by a model trained on public datasets later.

With a power factor sensor the reactive/real power ratio of the switch-on steps (dQ/dP) is known as
well. Heating elements are purely resistive (~0), a fridge compressor is clearly above 0 (~0.2 in a test
household). A known ratio that does not fit lowers the score of a type; without one nothing changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Types the user can choose from in the UI (key -> label)
DEVICE_TYPES: dict[str, str] = {
    "refrigerator": "Fridge / freezer",
    "heating": "Heating element (kettle, hob, heater, boiler)",
    "stove": "Stove / hob",
    "oven": "Oven",
    "washing_machine": "Washing machine",
    "dishwasher": "Dishwasher",
    "dryer": "Dryer",
    "light": "Light",
    "pump": "Pump / fan",
    "electronics": "Electronics",
    "other": "Other",
}


def band(x: float, lo: float, hi: float, softness: float = 0.25) -> float:
    """1 inside [lo, hi], falling off smoothly (on a log scale) outside. softness ~ width of the edge."""
    if not math.isfinite(x) or x <= 0:
        return 0.0
    lx, llo, lhi = math.log(x), math.log(lo), math.log(hi)
    dist = llo - lx if lx < llo else lx - lhi if lx > lhi else 0.0
    return math.exp(-((dist / softness) ** 2))


@dataclass(frozen=True, slots=True)
class Classification:
    type: str
    probabilities: dict[str, float]
    confidence: float


def _scores(f: dict[str, float]) -> dict[str, float]:
    p = f.get("power_w", math.nan)
    dur = f.get("duration_min", math.nan)
    per_day = f.get("cycles_per_day", math.nan)
    uniform = f.get("hour_uniformity", 0.0)
    dur_spread = f.get("duration_spread", math.inf)
    q = f.get("q_ratio", math.nan)
    known = math.isfinite(q)
    resistive = _ramp(q, 0.25, 0.08) if known else 1.0  # 1 up to 0.08, 0 from 0.25 on
    motor = _ramp(q, 0.02, 0.1) if known else 1.0  # 0 up to 0.02, 1 from 0.1 on
    return {
        # compressor: 30-250 W, runs 8-90 min (shorter "cycles" are mostly noise) with similar run times,
        # many times a day, independent of the time of day
        "refrigerator": band(p, 30, 250)
        * band(dur, 8, 90)
        * band(per_day, 6, 60, 0.5)
        * min(1.0, max(0.0, (uniform - 0.6) / 0.3))
        # equal run times make a compressor likely; varying ones lower the score but do not rule it out
        # (a fridge runs longer in summer and after a defrost)
        * (0.1 + 0.9 * _ramp(dur_spread, 0.9, 0.4))
        * (0.05 + 0.95 * motor),
        # heating element: >= 800 W, minutes to an hour, used at certain times of the day
        "heating": band(p, 800, 6000, 0.15) * band(dur, 0.5, 60, 0.6) * (0.05 + 0.95 * resistive),
        # baseline so that weak matches end up as "unknown"
        "unknown": 0.15,
    }


def _ramp(x: float, zero: float, one: float) -> float:
    """0 at `zero`, 1 at `one`, linear in between and clamped (works in both directions)."""
    return min(1.0, max(0.0, (x - zero) / (one - zero)))


def probabilities(features: dict[str, float]) -> dict[str, float]:
    scores = _scores(features)
    total = sum(scores.values())
    return {k: v / total for k, v in scores.items()}


def classify(features: dict[str, float], smoothed: dict[str, float] | None = None) -> Classification:
    """Type and confidence; with `smoothed` probabilities (kept over time) instead of the current ones."""
    probs = smoothed if smoothed else probabilities(features)
    best = max(probs, key=lambda k: probs[k])
    n = features.get("count", 0.0)
    evidence = 1 - math.exp(-n / 20)  # ~63 % after 20 cycles, ~99 % after 100
    spread = features.get("power_spread", math.inf)
    consistency = 1 / (1 + max(0.0, spread))  # 1 for a perfectly constant power
    confidence = probs[best] * evidence * consistency if best != "unknown" else 0.0
    return Classification(type=best, probabilities=probs, confidence=round(confidence, 3))
