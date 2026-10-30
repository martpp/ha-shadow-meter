"""Fridge search in the residual: compressor cycles (switch-on step + matching switch-off step).

Looks at the night (00-05 h), when few other devices switch, and prints a histogram of the step sizes
per phase. A fridge shows up as one phase with regular ~30-60 W on/off steps every hour or so.
Usage: fridge.py [phase ...] [--lo 30 --hi 300]
"""
import argparse

import numpy as np
import pandas as pd

from config import DATA, OUT, TZ


def edges(s, lo, hi):
    """Steps: median of the next 3 values - median of the previous 3 values (15 s), local maxima."""
    v = s.to_numpy()
    n = len(v)
    med = np.median(np.lib.stride_tricks.sliding_window_view(v, 3), 1)  # med[i] = median v[i..i+2]
    d = np.full(n, np.nan, np.float32)
    d[3:n - 2] = med[3:n - 2] - med[0:n - 5]  # after [i..i+2] - before [i-3..i-1]
    a = np.abs(np.nan_to_num(d))
    i = np.flatnonzero((a >= lo) & (a <= hi) & (a >= np.roll(a, 1)) & (a > np.roll(a, -1)))
    return pd.DataFrame({"time": s.index[i], "d": d[i]})


def edges_by_quarter(phase, lo, hi, hours=None):
    """Search steps quarter by quarter (RAM); hours=(from, to) restricts to a time of day."""
    out, n_days = [], 0
    for fq in sorted((DATA / "residual").glob("*.parquet")):
        s = pd.read_parquet(fq, columns=[phase])[phase]
        if hours:
            h = s.index.tz_convert(TZ).hour
            s = s[(h >= hours[0]) & (h < hours[1])]
        n_days += s.index.normalize().nunique()
        out.append(edges(s, lo, hi))
    e = pd.concat(out, ignore_index=True)
    return e[e.time.diff().dt.total_seconds().fillna(99) > 10].reset_index(drop=True), n_days


def cycles(e, tol=0.35, tmin=3, tmax=90):
    """Pair every switch-on step with the next switch-off step of similar size within tmin..tmax minutes."""
    up = e[e.d > 0].reset_index(drop=True)
    dn = e[e.d < 0].reset_index(drop=True)
    tdn = dn.time.to_numpy()
    out = []
    for t, d in zip(up.time.to_numpy(), up.d.to_numpy()):
        j0 = np.searchsorted(tdn, t + np.timedelta64(tmin, "m"))
        j1 = np.searchsorted(tdn, t + np.timedelta64(tmax, "m"))
        for j in range(j0, j1):
            if abs(-dn.d[j] - d) <= tol * d:
                out.append((t, tdn[j], d, -dn.d[j]))
                break
    c = pd.DataFrame(out, columns=["on", "off", "p_on", "p_off"])
    c = c[~c.off.duplicated()]
    c["run_min"] = (c.off - c.on).dt.total_seconds() / 60
    c["pause_min"] = (c.on.shift(-1) - c.off).dt.total_seconds() / 60
    return c


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("phases", nargs="*", default=list("abc"))
    ap.add_argument("--lo", type=float, default=30)
    ap.add_argument("--hi", type=float, default=300)
    a = ap.parse_args()
    for p in a.phases:
        e, nights = edges_by_quarter(p, a.lo, a.hi, hours=(0, 5))
        h_up = np.histogram(e.d[e.d > 0], bins=np.arange(a.lo, a.hi + 10, 10))
        h_dn = np.histogram(-e.d[e.d < 0], bins=np.arange(a.lo, a.hi + 10, 10))
        print(f"\n== phase {p}: {nights} nights, {len(e)} steps {a.lo}-{a.hi} W (night 00-05 h)")
        print("   step size   ON/night  OFF/night")
        for b, u, d in zip(h_up[1], h_up[0], h_dn[0]):
            if u + d > nights * 0.3:
                print(f"   {b:4.0f}-{b + 10:<4.0f}   {u / nights:7.2f}   {d / nights:7.2f}")
        c = cycles(e)
        if len(c) > 50:
            print(f"   cycles: {len(c)} ({len(c) / nights:.1f}/night)  P_on median {c.p_on.median():.0f} W"
                  f"  run median {c.run_min.median():.1f} min  pause median {c.pause_min.median():.1f} min")
            c.to_parquet(OUT / f"fridge_night_cycles_{p}.parquet")
