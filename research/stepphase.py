"""Phase of a meter from its own large steps: median(phase step / meter step) for each phase.

A ratio near 1 on one phase means the device is on that phase. Ratios near 0 on all phases mean the
device is not behind the main meter at all -> add it to meters_not_behind_main in config.yaml.
Run one meter per process on small machines (large meters need ~1 GB RAM).
Usage: stepphase.py [--min 80] meter [meter ...]
"""
import argparse

import numpy as np
import pandas as pd

from common import phases
from config import DATA


def jumps(s, thr):
    """Steps in the meter: median 2 min after - median 2 min before, local extrema above thr."""
    pre = s.rolling(4).median()  # 4 * 30 s = 2 min
    d = (pre.shift(-4) - pre).dropna()
    a = d.abs()
    pk = a[(a > thr) & (a >= a.rolling(11, center=True).max())]
    pk = pk[pk.index.to_series().diff().fillna(pd.Timedelta("1h")) > pd.Timedelta("5min")]
    return d.loc[pk.index]


def power30(ent):
    """Raw points directly on a 30 s grid (value holds until the next report, max. 6 h)."""
    df = pd.read_parquet(DATA / "W" / f"{ent}.parquet").dropna()
    v = df.set_index("time")["value"].astype(np.float32).sort_index()
    v = v[~v.index.duplicated(keep="last")]
    return v.resample("30s").last().ffill(limit=720)


def phases30():
    cache = DATA / "phases_30s.parquet"
    if not cache.exists():
        phases().resample("30s").mean().astype(np.float32).to_parquet(cache)
    return pd.read_parquet(cache)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("meters", nargs="+")
    ap.add_argument("--min", type=float, default=80, help="minimum step (W)")
    a = ap.parse_args()
    ph = phases30()
    for m in a.meters:
        j = jumps(power30(m).dropna(), a.min)
        if len(j) < 10:
            print(f"{m}: only {len(j)} steps")
            continue
        t = j.index
        pre = ph.reindex(t - pd.Timedelta("150s"), method="nearest").to_numpy()
        post = ph.reindex(t + pd.Timedelta("150s"), method="nearest").to_numpy()
        ratio = (post - pre) / j.to_numpy()[:, None]
        hit = (np.abs(ratio - 1) < 0.35).mean(0)
        print(f"{m:28s} n={len(j):4d} median step {j.abs().median():5.0f} W  ratio a/b/c "
              f"{np.round(np.nanmedian(ratio, 0), 2)}  hits(±35%) a/b/c {np.round(hit, 2)}  "
              f"years {t.year.min()}-{t.year.max()}")
