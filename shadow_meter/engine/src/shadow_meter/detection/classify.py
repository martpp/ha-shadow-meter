"""Rule-based device typing with a probability, and a confidence that grows with evidence.

This is deliberately simple and transparent: each device type has typical ranges for a few features
(power, run time, cycles per day, how evenly the cycles are spread over the day). A soft membership
score per type is normalised into probabilities. The confidence combines the probability of the best
type with the amount of evidence (number of cycles) and how consistent the power is.

The numbers are plausible defaults for European households, not a trained model. They are meant to be
replaced or complemented by a model trained on public datasets later.
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
    return {
        # compressor: 30-250 W, runs 5-90 min about equally long every time, many times a day,
        # independent of the time of day
        "refrigerator": band(p, 30, 250)
        * band(dur, 5, 90)
        * band(per_day, 6, 60, 0.5)
        * min(1.0, max(0.0, (uniform - 0.6) / 0.3))
        * min(1.0, max(0.0, (0.7 - dur_spread) / 0.4)),
        # heating element: >= 800 W, minutes to an hour, used at certain times of the day
        "heating": band(p, 800, 6000, 0.15) * band(dur, 0.5, 60, 0.6),
        # baseline so that weak matches end up as "unknown"
        "unknown": 0.15,
    }


def classify(features: dict[str, float]) -> Classification:
    scores = _scores(features)
    total = sum(scores.values())
    probs = {k: v / total for k, v in scores.items()}
    best = max(probs, key=lambda k: probs[k])
    n = features.get("count", 0.0)
    evidence = 1 - math.exp(-n / 20)  # ~63 % after 20 cycles, ~99 % after 100
    spread = features.get("power_spread", math.inf)
    consistency = 1 / (1 + max(0.0, spread))  # 1 for a perfectly constant power
    confidence = probs[best] * evidence * consistency if best != "unknown" else 0.0
    return Classification(type=best, probabilities=probs, confidence=round(confidence, 3))
