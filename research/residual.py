"""Residual per phase: whole-home power - metered devices (on their phase) - estimated lights.

The residual is what is left for unmetered devices (fridge, stove, ...). Computed quarter by quarter
to keep the RAM usage low. Result: data/residual/<quarter>.parquet (columns a, b, c, float32).
Usage: residual.py [out/lights.csv]
"""
import sys

import numpy as np
import pandas as pd

from common import RULE, light_state, power
from config import (DATA, LIGHTS_NOT_BEHIND_MAIN, METER_PHASE_FIXED, METERED_LIGHTS, METERS,
                    METERS_NOT_BEHIND_MAIN, OUT)

RES = DATA / "residual"


def load_meter_phases():
    p = OUT / "meter_phase.csv"
    return pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["q", "meter", "phase"])


MP = load_meter_phases()


def meter_phase(m, q):
    """Fixed phase from config, else the learned phase of that quarter (or the closest earlier one)."""
    if m in METER_PHASE_FIXED:
        return METER_PHASE_FIXED[m]
    mp = MP[MP.meter == m].set_index("q").phase.sort_index()
    if q in mp.index:
        return mp[q]
    before = mp[mp.index < q]
    return before.iloc[-1] if len(before) else (mp.iloc[0] if len(mp) else None)


def light_members(name):
    return [x if x.startswith("light.") else f"light.{x}" for x in name.split(" + ")]


if __name__ == "__main__":
    R = pd.read_csv(sys.argv[1] if len(sys.argv) > 1 else OUT / "lights.csv", index_col=0)
    RES.mkdir(exist_ok=True)
    skip_lights = LIGHTS_NOT_BEHIND_MAIN | set(METERED_LIGHTS.values())  # metered lights: the meter is used
    idx_all = pd.read_parquet(DATA / f"phases_{RULE}.parquet", columns=[]).index
    for qp in idx_all.tz_convert(None).to_period("Q").unique():
        q = str(qp)
        a, b = qp.start_time.tz_localize("UTC"), (qp.end_time + pd.Timedelta(1, "ns")).tz_localize("UTC")
        res = pd.read_parquet(DATA / f"phases_{RULE}.parquet", filters=[("time", ">=", a), ("time", "<", b)])
        res = res.astype(np.float32)
        for m in METERS:
            if m in METERS_NOT_BEHIND_MAIN or not (DATA / "W" / f"{m}.parquet").exists():
                continue
            # HA only writes on change -> carry values forward for long (otherwise fake steps at report gaps)
            s = power(m, limit="6h", start=a, end=b).reindex(res.index).fillna(0).to_numpy(np.float32)
            p = meter_phase(m, q)
            if s.any() and p:
                res[p] -= s
        for l in R.index:
            members = light_members(l)
            if set(members) & skip_lights or not all((DATA / "state" / f"{x}.parquet").exists() for x in members):
                continue
            st = sum(light_state(x, res.index) for x in members) / len(members)  # group: mean state
            for p in "abc":
                if R.loc[l, p] > 0.5:  # negative coefficients are co-switching artefacts, never subtract them
                    res[p] -= st * R.loc[l, p]
        res.to_parquet(RES / f"{q}.parquet")
        night = res.between_time("02:00", "04:00")
        print(q, "mean", res.mean().round(1).to_dict(), " night median", night.median().round(1).to_dict(),
              flush=True)
