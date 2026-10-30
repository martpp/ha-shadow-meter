"""Diagnosis: inventory of all exported power sensors (period, points, median, distinct values).

Few distinct values (e.g. always 1.4 W) usually mean an estimate (Powercalc, self-reported) rather than
a real measurement. Sums (PoE totals, UPS output) must not be listed as meters next to their parts.
"""
import pandas as pd

from config import DATA

rows = []
for f in sorted((DATA / "W").glob("*.parquet")):
    df = pd.read_parquet(f).dropna()
    if df.empty:
        continue
    s = df["value"]
    rows.append(dict(entity=f.stem, start=f"{df.time.min():%Y-%m}", end=f"{df.time.max():%Y-%m}", n=len(s),
                     median=round(s.median(), 1), p95=round(s.quantile(.95), 1), distinct=s.nunique()))
pd.set_option("display.width", 200, "display.max_rows", 300)
print(pd.DataFrame(rows).to_string(index=False))
