"""Shared loaders on a 5 s grid (float32, so that everything fits into a few GB of RAM)."""
import numpy as np
import pandas as pd

from config import DATA, PHASES

RULE = "5s"


def power(ent, rule=RULE, limit="2min", start=None, end=None):
    """Power series on a regular grid.

    Home Assistant only writes a value when it changes, so the last value is carried forward
    (at most `limit`). start/end load only a slice (saves RAM); reading starts at start - limit so
    that the last value before the slice can be carried forward.
    """
    filters = None
    if start is not None:
        start, end = (pd.Timestamp(x) if pd.Timestamp(x).tzinfo else pd.Timestamp(x, tz="UTC") for x in (start, end))
        filters = [("time", ">=", start - pd.Timedelta(limit)), ("time", "<", end)]
    df = pd.read_parquet(DATA / "W" / f"{ent}.parquet", filters=filters).dropna()
    s = df.set_index("time")["value"].astype(np.float32).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    s = s.resample(rule).last().ffill(limit=int(pd.Timedelta(limit) / pd.Timedelta(rule)))
    return s if start is None else s.loc[start:]


def phases(rule=RULE):
    """Whole-home power per phase (columns a, b, c), cached."""
    cache = DATA / f"phases_{rule}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    df = pd.DataFrame({p: power(e, rule) for p, e in PHASES.items()})
    df.to_parquet(cache)
    return df


def transitions(full):
    """State changes (real changes only) of an entity: DataFrame time, on (bool), brightness."""
    df = pd.read_parquet(DATA / "state" / f"{full}.parquet").dropna(subset=["value"])
    df = df[df["value"].isin([0, 1])]
    chg = df["value"].ne(df["value"].shift())
    out = df[chg].iloc[1:].copy()
    out["on"] = out["value"] == 1
    return out[["time", "on", "brightness"]].reset_index(drop=True)


def light_state(l, idx):
    """On/off state (0/1) of a light on index idx, limited to the period in which the entity reports data
    (otherwise a lamp that was removed while 'on' would stay on forever)."""
    ev = transitions(l)
    st = pd.Series(ev.on.astype(np.float32).to_numpy(), index=ev.time)
    st = st[~st.index.duplicated(keep="last")]
    out = st.reindex(st.index.union(idx)).ffill().reindex(idx).fillna(0)
    t = pd.read_parquet(DATA / "state" / f"{l}.parquet", columns=["time"])["time"]
    out[(idx < t.min()) | (idx > t.max() + pd.Timedelta("1D"))] = 0
    return out.to_numpy(np.float32)
