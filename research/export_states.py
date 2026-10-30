"""Export on/off histories (value 0/1 + brightness) of HA entities to Parquet.

Merges both InfluxDB schemas of the Home Assistant integration:
  legacy: one measurement "state" with tags domain + entity_id (without domain prefix)
  current: one measurement per entity, e.g. "light.kitchen"
Usage:
  export_states.py --all-lights
  export_states.py light.hallway switch.desk_outlet ...
Result: data/state/<domain.entity>.parquet
"""
import argparse

import pandas as pd

from config import CFG, DATA, influx_client

LEGACY = CFG["influx"].get("legacy_state_measurement")


def fetch(c, q):
    pts = list(c.query(q, epoch="ms").get_points()) if q else []
    if not pts:
        return pd.DataFrame(columns=["time", "value", "brightness"])
    df = pd.DataFrame(pts).reindex(columns=["time", "value", "brightness"])
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    return df


def all_lights(c):
    names = set()
    if LEGACY:
        r = c.query(f'SHOW TAG VALUES FROM "{LEGACY}" WITH KEY = entity_id WHERE domain = \'light\'')
        names |= {f"light.{p['value']}" for p in r.get_points()}
    names |= {p["name"] for p in c.query("SHOW MEASUREMENTS").get_points() if p["name"].startswith("light.")}
    return sorted(names)


def export(c, full):
    dom, name = full.split(".", 1)
    old = fetch(c, LEGACY and f"SELECT value, brightness FROM \"{LEGACY}\" WHERE domain='{dom}' AND entity_id='{name}'")
    new = fetch(c, f'SELECT value, brightness FROM "{full}"')
    df = pd.concat([old, new]).sort_values("time").drop_duplicates("time")
    df[["value", "brightness"]] = df[["value", "brightness"]].apply(pd.to_numeric, errors="coerce")
    out = DATA / "state" / f"{full}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    rng = f"{df.time.min():%Y-%m-%d}..{df.time.max():%Y-%m-%d}" if len(df) else "-"
    print(f"{full}: legacy {len(old)} current {len(new)} {rng}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("entities", nargs="*")
    ap.add_argument("--all-lights", action="store_true")
    a = ap.parse_args()
    c = influx_client()
    for e in (all_lights(c) if a.all_lights else []) + a.entities:
        export(c, e)
