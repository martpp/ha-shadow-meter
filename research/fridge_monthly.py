"""Fridge over whole days: cycles per month, duty cycle, energy per day and year.

Usage: fridge_monthly.py a [--on 38 75] [--off 28 55] [--since 2025-01-01] [--until ...]
Result: out/fridge_monthly.csv, out/fridge_cycles_<phase>.parquet
"""
import argparse

import pandas as pd

from config import OUT, TZ
from fridge import cycles, edges_by_quarter

ap = argparse.ArgumentParser()
ap.add_argument("phase")
ap.add_argument("--on", type=float, nargs=2, default=(38, 75), help="switch-on step range (W)")
ap.add_argument("--off", type=float, nargs=2, default=(28, 55), help="switch-off step range (W)")
ap.add_argument("--since", default=None)
ap.add_argument("--until", default=None)
a = ap.parse_args()
e, _ = edges_by_quarter(a.phase, min(a.on[0], a.off[0]), max(a.on[1], a.off[1]))
e = e[(e.time >= pd.Timestamp(a.since or "2000", tz="UTC")) & (e.time < pd.Timestamp(a.until or "2100", tz="UTC"))]
# compressor: switch-on step includes the inrush peak, the switch-off step is the running power
e = e[((e.d > 0) & (e.d >= a.on[0]) & (e.d <= a.on[1])) | ((e.d < 0) & (-e.d >= a.off[0]) & (-e.d <= a.off[1]))]
c = cycles(e, tol=0.45, tmin=5, tmax=70)
c = c[(c.pause_min > 5) & (c.pause_min < 150)]
c["p_run"] = c.p_off
m = c.groupby(c.on.dt.to_period("M"))
days = pd.Series(1, index=pd.date_range(e.time.min().normalize(), e.time.max(), freq="D")).groupby(
    lambda d: d.to_period("M")).sum()  # calendar days per month
out = pd.DataFrame({"cycles_per_day": m.size() / days, "run_min": m.run_min.median(),
                    "pause_min": m.pause_min.median(), "p_run_w": m.p_run.median(), "p_on_w": m.p_on.median()})
out["duty_pct"] = out.run_min / (out.run_min + out.pause_min) * 100
out["kwh_per_day"] = out.duty_pct / 100 * out.p_run_w * 24 / 1000
pd.set_option("display.width", 200, "display.max_rows", 100)
print(out.round(2).to_string())
h = c.groupby(c.on.dt.tz_convert(TZ).dt.hour).size()
print("\ncycle starts per hour of day (%):", (h / h.sum() * 100).round(1).to_dict())
y = out.groupby(out.index.year).kwh_per_day.mean()
print("\nkWh/day (yearly mean):", y.round(2).to_dict(), " -> kWh/year:", (y * 365).round(0).to_dict())
out.to_csv(OUT / "fridge_monthly.csv")
c.to_parquet(OUT / f"fridge_cycles_{a.phase}.parquet")
