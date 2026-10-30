"""Phase of every meter per quarter: regression of the 1-min changes of the phases on those of the meters.

Works well for meters with frequent, large changes. For meters with rare or small changes use
stepphase.py and put the result into meter_phase_fixed in config.yaml.
Result: out/meter_phase.csv (quarter, meter, beta_a/b/c, phase)
"""
import numpy as np
import pandas as pd

from common import phases, power
from config import DATA, METERS, METERS_NOT_BEHIND_MAIN, OUT

ph = phases().resample("1min").mean()
meters = [m for m in METERS if m not in METERS_NOT_BEHIND_MAIN and (DATA / "W" / f"{m}.parquet").exists()]
M = pd.DataFrame({m: power(m, rule="1min", limit="10min") for m in meters}).reindex(ph.index)
dP, dM = ph.diff(), M.diff()
rows = []
for q, idx in dP.groupby(dP.index.to_period("Q")).groups.items():
    p, m = dP.loc[idx], dM.loc[idx]
    act = [c for c in meters if m[c].notna().mean() > 0.3 and (m[c].abs() > 5).sum() > 50]
    if not act:
        continue
    X, Y = m[act].fillna(0), p
    ok = Y.notna().all(axis=1)
    X, Y = X[ok].to_numpy(), Y[ok].to_numpy()
    # robust: iteratively down-weight large unexplained steps (unmetered devices)
    w = np.ones(len(X))
    for _ in range(3):
        B = np.linalg.lstsq(X * w[:, None], Y * w[:, None], rcond=None)[0]
        w = 1 / np.maximum(1, np.abs(Y - X @ B).sum(1) / 20)
    for i, c in enumerate(act):
        rows.append(dict(q=str(q), meter=c, a=B[i, 0], b=B[i, 1], c=B[i, 2], n=int((m[c].abs() > 5).sum())))
R = pd.DataFrame(rows)
R["phase"] = R[["a", "b", "c"]].idxmax(axis=1)
R.to_csv(OUT / "meter_phase.csv", index=False)
pd.set_option("display.width", 250, "display.max_rows", 500)
piv = R.assign(v=R.phase + R[["a", "b", "c"]].max(axis=1).round(2).astype(str)).pivot(
    index="meter", columns="q", values="v")
print(piv.fillna("").to_string())
