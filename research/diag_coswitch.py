"""Diagnosis: how often do other entities switch within ±15 s of a given entity? (co-switching)

If a light almost always switches together with others (scenes), its power can only be estimated
as part of a group, see joint.py.
Usage: diag_coswitch.py light.kitchen_table
"""
import sys

import numpy as np
import pandas as pd

from common import transitions
from config import DATA

target = sys.argv[1]
ev = transitions(target).time.to_numpy()
res = {}
for f in (DATA / "state").glob("*.parquet"):
    if f.stem == target:
        continue
    t = transitions(f.stem).time.to_numpy()
    if len(t) == 0:
        continue
    i = np.searchsorted(t, ev)
    dl = np.abs(np.stack([t[np.clip(i - 1, 0, len(t) - 1)] - ev, t[np.clip(i, 0, len(t) - 1)] - ev]))
    res[f.stem] = (dl.min(axis=0) <= np.timedelta64(15, "s")).mean()
s = pd.Series(res).sort_values(ascending=False)
print(f"{target}: {len(ev)} state changes; share with another change within 15 s:")
print(s[s > 0.03].round(2).to_string())
