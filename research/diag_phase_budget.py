"""Diagnosis: night budget of one phase - raw power vs. everything that is subtracted on that phase.

If the remainder is clearly negative, something is subtracted that is not on this phase (wrong phase,
not behind the main meter, nested meters, a light that 'stays on' forever).
Usage: diag_phase_budget.py 2026Q1 [--phase b] [--lights out/lights.csv]
"""
import argparse

import numpy as np
import pandas as pd

from common import light_state, power
from config import DATA, LIGHTS_NOT_BEHIND_MAIN, METER_PHASE_FIXED, METERED_LIGHTS, METERS, METERS_NOT_BEHIND_MAIN, OUT
from residual import light_members, meter_phase
from stepphase import phases30

ap = argparse.ArgumentParser()
ap.add_argument("quarter")
ap.add_argument("--phase", default="b")
ap.add_argument("--lights", default=str(OUT / "lights.csv"))
a = ap.parse_args()
R = pd.read_csv(a.lights, index_col=0)
qp = pd.Period(a.quarter)
t0, t1 = qp.start_time.tz_localize("UTC"), (qp.end_time + pd.Timedelta(1, "ns")).tz_localize("UTC")
raw = phases30().loc[t0:t1, a.phase]
night = (raw.index.hour >= 2) & (raw.index.hour < 4)
print(f"{a.quarter} phase {a.phase}: raw night median {raw[night].median():.1f} W, "
      f"2% quantile {raw[night].quantile(.02):.1f} W")
total = 0.0
for m in METERS:
    if m in METERS_NOT_BEHIND_MAIN or not (DATA / "W" / f"{m}.parquet").exists() or meter_phase(m, a.quarter) != a.phase:
        continue
    v = power(m, limit="6h", start=t0, end=t1).resample("30s").mean().reindex(raw.index).fillna(0)[night].median()
    if v > 0.3:
        total += v
        print(f"  meter {m:32s} {v:6.1f} W  ({'fixed' if m in METER_PHASE_FIXED else 'learned'})")
skip = LIGHTS_NOT_BEHIND_MAIN | set(METERED_LIGHTS.values())
for l in R.index:
    members = light_members(l)
    if R.loc[l, a.phase] <= 0.5 or set(members) & skip:
        continue
    on = np.mean([light_state(x, raw.index)[night].mean() for x in members])
    v = on * R.loc[l, a.phase]
    if v > 0.3:
        total += v
        print(f"  light {l[:32]:32s} {v:6.1f} W  (on at night: {on:.0%})")
print(f"  subtracted {total:.1f} W  -> remainder {raw[night].median() - total:.1f} W")
