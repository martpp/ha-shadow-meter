"""Diagnosis plot: raw phase power vs. residual for a few hours (e.g. to check whether a 'device' found
in the residual really exists in the raw data or is an artefact of the subtraction).

Usage: diag_plot_night.py b "2025-01-21 23:00" "2025-01-22 03:00" [--out out/night.png]
"""
import argparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from config import DATA, OUT, PHASES, TZ

ap = argparse.ArgumentParser()
ap.add_argument("phase")
ap.add_argument("start")
ap.add_argument("end")
ap.add_argument("--tz", default=TZ)
ap.add_argument("--out", default=str(OUT / "night.png"))
a = ap.parse_args()
s, e = pd.Timestamp(a.start, tz=a.tz).tz_convert("UTC"), pd.Timestamp(a.end, tz=a.tz).tz_convert("UTC")
raw = pd.read_parquet(DATA / "W" / f"{PHASES[a.phase]}.parquet",
                      filters=[("time", ">=", s), ("time", "<", e)]).set_index("time")["value"].sort_index()
res = pd.read_parquet(DATA / "residual" / f"{s.tz_convert(None).to_period('Q')}.parquet", columns=[a.phase])
res = res.loc[s:e, a.phase]
fig, ax = plt.subplots(2, 1, figsize=(12, 6), sharex=True)
ax[0].step(raw.index.tz_convert(a.tz), raw.values, where="post", lw=0.8)
ax[0].set_title(f"phase {a.phase}, raw")
ax[1].plot(res.index.tz_convert(a.tz), res.values, lw=0.7, color="C3")
ax[1].set_title(f"phase {a.phase}, residual (metered devices and estimated lights subtracted)")
for x in ax:
    x.set_ylabel("W")
    x.grid(alpha=0.3)
fig.tight_layout()
fig.savefig(a.out, dpi=90)
print("saved", a.out)
