"""Stove search in the residual: high load blocks, phase coincidence, cooking sessions.

Usage: stove.py [--thr 600] [--phases a b]
Result: out/stove_sessions.parquet
"""
import argparse

import pandas as pd

from config import DATA, OUT, TZ

ap = argparse.ArgumentParser()
ap.add_argument("--thr", type=float, default=600, help="W above the rolling baseline")
ap.add_argument("--phases", nargs="*", default=None, help="default: the two phases with the most high load")
a = ap.parse_args()

# load quarter by quarter and average to 30 s (RAM)
res = pd.concat(pd.read_parquet(f).resample("30s").mean() for f in sorted((DATA / "residual").glob("*.parquet")))
base = res.rolling("6h", min_periods=20).quantile(0.1)  # rolling baseline per phase
ex = (res - base).clip(lower=0)
hi = ex > a.thr
print(f"share of time with residual > {a.thr:.0f} W above baseline:", hi.mean().round(4).to_dict())
hm = hi.resample("1min").max()
print("\nP(column high | row high), 1-min resolution:")
print(pd.DataFrame({j: [(hm[i] & hm[j]).sum() / max(1, hm[i].sum()) for i in "abc"] for j in "abc"},
                   index=list("abc")).round(2).to_string())
print("\nhour-of-day profile (% of minutes high):")
print((hm.groupby(hm.index.tz_convert(TZ).hour).mean() * 100).round(2).T.to_string())

ph = a.phases or list(hm.sum().sort_values(ascending=False).index[:2])
print(f"\nstove candidate: phases {ph}")
any_hi = hm[ph].any(axis=1)
# sessions: high minutes with gaps < 10 min belong together
t = any_hi[any_hi].index
sid = (pd.Series(t).diff() > pd.Timedelta("10min")).cumsum()
sess = pd.DataFrame({"t": t, "sid": sid.to_numpy()}).groupby("sid").t.agg(["min", "max"])
sess["duration_min"] = (sess["max"] - sess["min"]).dt.total_seconds() / 60 + 1
e_min = ex[ph].resample("1min").mean()
sess["kwh"] = [e_min.loc[s:e].sum().sum() / 60 / 1000 for s, e in zip(sess["min"], sess["max"])]
sess = sess[sess.duration_min >= 3]
print(f"\n{len(sess)} sessions >= 3 min; duration median {sess.duration_min.median():.0f} min, "
      f"energy median {sess.kwh.median():.2f} kWh")
print("sessions by duration (min):",
      pd.cut(sess.duration_min, [3, 10, 20, 40, 60, 120, 1e9]).value_counts().sort_index().to_dict())
y = sess.groupby(sess["min"].dt.year).agg(n=("kwh", "size"), kwh=("kwh", "sum"))
cnt = res.resample("D").count().iloc[:, 0]
y["days_with_data"] = cnt.gt(0).groupby(cnt.index.year).sum()
y["kwh_per_year_extrapolated"] = (y.kwh / y.days_with_data * 365).round(0)
print("\nper year:\n", y.round(1).to_string())
sess.to_parquet(OUT / "stove_sessions.parquet")
