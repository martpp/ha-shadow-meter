"""Export power series from InfluxDB to Parquet, month by month (raw points, read-only).

Usage:
  export.py --all                 # whole-home phases + all meters from config.yaml
  export.py <entity_id> [...]     # single power entities
Result: data/W/<entity_id>.parquet  (columns: time UTC, value)
"""
import argparse

import pandas as pd

from config import CFG, DATA, METERS, PHASES, influx_client

MEAS = CFG["influx"].get("power_measurement", "W")


def export(c, ent, field="value"):
    out = DATA / "W" / f"{ent}.parquet"
    if out.exists():
        print("skip", out)
        return
    start = pd.Timestamp(CFG["influx"].get("export_start", "2020-01-01"), tz="UTC")
    parts = []
    for a in pd.date_range(start, pd.Timestamp.now(tz="UTC") + pd.offsets.MonthBegin(), freq="MS"):
        b = a + pd.offsets.MonthBegin()
        q = (f'SELECT "{field}" FROM "{MEAS}" WHERE entity_id=\'{ent}\' '
             f"AND time >= '{a.isoformat()}' AND time < '{b.isoformat()}'")
        pts = list(c.query(q, epoch="ms").get_points())
        if pts:
            df = pd.DataFrame(pts)
            parts.append(pd.DataFrame({"time": pd.to_datetime(df["time"], unit="ms", utc=True),
                                       "value": pd.to_numeric(df[field], errors="coerce")}))
        print(f"{ent} {a:%Y-%m}: {len(pts)}", flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    df = pd.concat(parts) if parts else pd.DataFrame(columns=["time", "value"])
    df.to_parquet(out, index=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("entities", nargs="*")
    ap.add_argument("--all", action="store_true", help="phases + all meters from config.yaml")
    a = ap.parse_args()
    ents = (list(PHASES.values()) + METERS if a.all else []) + a.entities
    c = influx_client()
    for e in ents:
        export(c, e)
