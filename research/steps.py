"""Step analysis for a single switched device: estimated power from the steps in the whole-home power.

Good first test for devices that switch on their own (e.g. a motion-controlled hallway light).
If other devices switch at the same time, use joint.py instead.
Usage: steps.py light.hallway [--pre 40 10] [--post 10 40]
"""
import argparse

import numpy as np
import pandas as pd

from common import phases, transitions
from config import OUT


def deltas(ph, ev, pre=(40, 10), post=(10, 40)):
    """Power step per event and phase (median after - median before), sign normalised to 'switch on'."""
    idx = ph.index
    rows = []
    for t, on in zip(ev["time"], ev["on"]):
        a = idx.searchsorted(t - pd.Timedelta(seconds=pre[0]))
        b = idx.searchsorted(t - pd.Timedelta(seconds=pre[1]))
        c = idx.searchsorted(t + pd.Timedelta(seconds=post[0]))
        d = idx.searchsorted(t + pd.Timedelta(seconds=post[1]))
        before, after = ph.iloc[a:b], ph.iloc[c:d]
        if len(before) < 3 or len(after) < 3 or before.isna().any().any() or after.isna().any().any():
            rows.append([np.nan] * ph.shape[1])
            continue
        dlt = (after.median() - before.median()).to_numpy()
        rows.append(dlt if on else -dlt)
    return pd.DataFrame(rows, columns=ph.columns, index=ev.index)


def boot_ci(x, n=2000, seed=0):
    """95 % bootstrap confidence interval of the median."""
    x = x.dropna().to_numpy()
    if len(x) < 5:
        return np.nan, np.nan
    r = np.random.default_rng(seed)
    m = np.median(r.choice(x, (n, len(x))), axis=1)
    return np.percentile(m, [2.5, 97.5])


def summarize(d, label):
    tot = d.sum(axis=1, min_count=3)
    lo, hi = boot_ci(tot)
    ph = d.median().round(1).to_dict()
    print(f"{label:>16}: n={tot.notna().sum():5d}  median {tot.median():7.1f} W  95% CI [{lo:.1f}, {hi:.1f}]"
          f"  per phase {ph}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("entity")
    ap.add_argument("--pre", type=int, nargs=2, default=(40, 10), help="seconds before the switch event")
    ap.add_argument("--post", type=int, nargs=2, default=(10, 40), help="seconds after the switch event")
    a = ap.parse_args()
    ph = phases()
    ev = transitions(a.entity)
    d = deltas(ph, ev, a.pre, a.post)
    print(f"== {a.entity}: {len(ev)} state changes {ev.time.min():%Y-%m-%d}..{ev.time.max():%Y-%m-%d}")
    summarize(d, "all")
    summarize(d[ev.on], "switch-on only")
    summarize(d[~ev.on], "switch-off only")
    for y, g in d.groupby(ev.time.dt.year):
        summarize(g, str(y))
    if ev.brightness.notna().sum() > 20:
        on = ev.on & ev.brightness.notna()
        for q, g in d[on].groupby(pd.cut(ev.brightness[on], [0, 64, 128, 192, 255])):
            summarize(g, f"brightness {q}")
    d.assign(time=ev.time, on=ev.on).to_parquet(OUT / f"steps_{a.entity}.parquet")
