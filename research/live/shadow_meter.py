"""Shadow Meter for Home Assistant - live service.

Live estimation of fridge and stove from the whole-home meter -> Home Assistant (MQTT discovery).

Every poll_seconds the new raw values are read from InfluxDB (read-only). The last `window` is kept in
memory, the residual per phase (phase - metered devices on that phase) is computed and
  fridge: compressor cycles are detected from on/off steps (state machine)
  stove:  load above a rolling baseline, with hysteresis
Energy is integrated and saved to state.json (survives restarts).

Configuration: section `live` of config.yaml (path: SHADOW_METER_CONFIG, default ../config.yaml).
Usage: shadow_meter.py [--dry-run]
"""
import argparse
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import paho.mqtt.client as mqtt
import yaml
from influxdb import InfluxDBClient

HERE = Path(__file__).parent
CFG = yaml.safe_load(open(os.environ.get("SHADOW_METER_CONFIG", HERE.parent / "config.yaml"), encoding="utf-8"))
LIVE = CFG["live"]
POLL = LIVE.get("poll_seconds", 10)
WINDOW = pd.Timedelta(LIVE.get("window", "3h"))
RULE = "5s"
STATE_FILE = Path(os.environ.get("SHADOW_METER_STATE", HERE / "state.json"))
FR, ST = LIVE["fridge"], LIVE["stove"]
USED_PHASES = sorted({FR["phase"], *ST["phases"]})
PHASES = {p: CFG["total_phases"][p] for p in USED_PHASES}
METERS = {p: LIVE.get("meters", {}).get(p, []) for p in USED_PHASES}

log = logging.getLogger("shadow-meter")


def env_file(path):
    p = Path(os.path.expanduser(path))
    vals = {}
    if p.exists():
        vals = dict(l.strip().split("=", 1) for l in p.open() if "=" in l and not l.startswith("#"))
    return {**vals, **{k: v for k, v in os.environ.items() if k.startswith(("INFLUX_", "MQTT_"))}}


class Source:
    """Raw values from InfluxDB, fetched incrementally; per entity the last WINDOW (+ the last value before)."""

    def __init__(self):
        c = CFG["influx"]
        e = env_file(c.get("env_file", "~/.shadow-meter-influx.env"))
        self.c = InfluxDBClient(c.get("host", "localhost"), c.get("port", 8086), e["INFLUX_USER"], e["INFLUX_PW"],
                                c["database"], timeout=30)
        self.meas = c.get("power_measurement", "W")
        self.ents = list(PHASES.values()) + [m for ms in METERS.values() for m in ms]
        self.data = {x: pd.Series(dtype=np.float32) for x in self.ents}
        self.last = pd.Timestamp.now(tz="UTC") - WINDOW
        for x in self.ents:  # last value before the window as start value (HA only writes on change)
            q = (f"SELECT value FROM \"{self.meas}\" WHERE entity_id='{x}' AND time < '{self.last.isoformat()}' "
                 f"AND time > '{(self.last - pd.Timedelta('2D')).isoformat()}' ORDER BY time DESC LIMIT 1")
            pts = list(self.c.query(q, epoch="ms").get_points())
            if pts:
                self.data[x] = pd.Series([pts[0]["value"]], index=[self.last], dtype=np.float32)

    def update(self):
        since = self.last - pd.Timedelta("60s")  # overlap: points that were written late
        rx = "|".join(self.ents)
        q = (f"SELECT value FROM \"{self.meas}\" WHERE entity_id =~ /^({rx})$/ AND time > '{since.isoformat()}' "
             "GROUP BY entity_id")
        now = pd.Timestamp.now(tz="UTC")
        for (_, tags), pts in self.c.query(q, epoch="ms").items():
            pts = list(pts)
            if not pts:
                continue
            s = pd.Series([p["value"] for p in pts], dtype=np.float32,
                          index=pd.to_datetime([p["time"] for p in pts], unit="ms", utc=True))
            x = tags["entity_id"]
            d = pd.concat([self.data[x], s])
            self.data[x] = d[~d.index.duplicated(keep="last")].sort_index()
        cut = now - WINDOW
        for x, d in self.data.items():  # trim, but keep the last value before the window
            if len(d) and d.index[0] < cut:
                keep = d.index >= cut
                i = max(0, keep.argmax() - 1) if keep.any() else len(d) - 1
                self.data[x] = d.iloc[i:]
        self.last = now

    def grid(self, x, idx):
        d = self.data[x].dropna()
        if d.empty:
            return np.zeros(len(idx), np.float32)
        g = d.resample(RULE).last()
        return g.reindex(g.index.union(idx)).ffill().reindex(idx).fillna(0).to_numpy(np.float32)

    def residual(self):
        now = pd.Timestamp.now(tz="UTC").floor(RULE)
        idx = pd.date_range(now - WINDOW, now, freq=RULE)
        out = {}
        for p, ph in PHASES.items():
            r = self.grid(ph, idx)
            for m in METERS[p]:
                r = r - self.grid(m, idx)
            out[p] = r
        return pd.DataFrame(out, index=idx)


def edges(s, lo=25, hi=300):
    """Steps: median of the next 3 values - median of the previous 3 values (same as fridge.py)."""
    v = s.to_numpy()
    n = len(v)
    if n < 10:
        return pd.DataFrame({"time": pd.Series(dtype="datetime64[ns, UTC]"), "d": pd.Series(dtype=np.float32)})
    med = np.median(np.lib.stride_tricks.sliding_window_view(v, 3), 1)
    d = np.full(n, np.nan, np.float32)
    d[3:n - 2] = med[3:n - 2] - med[0:n - 5]
    a = np.abs(np.nan_to_num(d))
    i = np.flatnonzero((a >= lo) & (a <= hi) & (a >= np.roll(a, 1)) & (a > np.roll(a, -1)))
    e = pd.DataFrame({"time": s.index[i], "d": d[i]})
    return e[e.time.diff().dt.total_seconds().fillna(99) > 10]


def fridge_state(res, now):
    """State machine over the steps in the window -> (running, since)."""
    on_lo, on_hi = FR["on_step"]
    off_lo, off_hi = FR["off_step"]
    min_run, max_run = pd.Timedelta(FR["min_run"]), pd.Timedelta(FR["max_run"])
    on, t_on, p_on = False, None, 0.0
    e = edges(res)
    for t, d in zip(e.time, e.d):
        if on and t - t_on > max_run:
            on = False
        if not on and on_lo <= d <= on_hi:
            on, t_on, p_on = True, t, d
        elif on and off_lo <= -d <= off_hi and abs(-d - p_on) <= 0.6 * p_on and t - t_on >= min_run:
            on = False
    if on and now - t_on > max_run:
        on = False
    return on, t_on


class Stove:
    def __init__(self):
        self.on, self.below_since = False, None
        self.off_delay = pd.Timedelta(ST["off_delay"])

    def update(self, res, fridge_on, now):
        r = res[ST["phases"]]
        base = r.resample("30s").mean().quantile(0.1)  # rolling baseline per phase
        ex = (r.iloc[-6:].mean() - base).clip(lower=0)  # last 30 s above baseline
        if fridge_on and FR["phase"] in ex.index:
            ex[FR["phase"]] = max(0.0, ex[FR["phase"]] - FR["power"])
        p = float(ex.sum())
        if p > ST["on_w"]:
            self.on, self.below_since = True, None
        elif self.on and p < ST["off_w"]:
            self.below_since = self.below_since or now
            if now - self.below_since > self.off_delay:
                self.on, self.below_since = False, None
        else:
            self.below_since = None
        return self.on, (p if self.on else 0.0)


class Publisher:
    # key: (device name, name of the binary sensor, device_class)
    DEVICES = {"fridge": ("Fridge (estimated)", "Running", "running"),
               "stove": ("Stove (estimated)", "Active", "power")}

    def __init__(self, dry):
        self.dry = dry
        if dry:
            return
        m = LIVE.get("mqtt", {})
        self.prefix = m.get("discovery_prefix", "homeassistant")
        self.id = m.get("id_prefix", "shadow_meter")  # used for MQTT topics and unique_ids
        e = env_file(m.get("env_file", "~/.shadow-meter-mqtt.env"))
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self.id}-live")
        self.c.username_pw_set(e.get("MQTT_USER"), e.get("MQTT_PW"))
        self.c.will_set(f"{self.id}/status", "offline", retain=True)
        self.c.on_connect = lambda *a: self.discovery()
        self.c.connect(e["MQTT_HOST"], int(e.get("MQTT_PORT", 1883)), 60)
        self.c.loop_start()

    def discovery(self):
        for key, (dev_name, bin_name, bin_class) in self.DEVICES.items():
            dev = {"identifiers": [f"{self.id}_{key}"], "name": dev_name, "manufacturer": "Shadow Meter",
                   "model": "estimated from the whole-home meter"}
            base = {"device": dev, "state_topic": f"{self.id}/{key}/state", "availability_topic": f"{self.id}/status"}
            cfgs = {
                ("binary_sensor", "on"): dict(base, name=bin_name, device_class=bin_class,
                                              value_template="{{ value_json.on }}"),
                ("sensor", "power"): dict(base, name="Power", device_class="power", unit_of_measurement="W",
                                          state_class="measurement", value_template="{{ value_json.power }}"),
                ("sensor", "energy"): dict(base, name="Energy", device_class="energy", unit_of_measurement="kWh",
                                           state_class="total_increasing", value_template="{{ value_json.energy }}"),
            }
            for (comp, obj), cfg in cfgs.items():
                uid = f"{self.id}_{key}_{obj}"
                cfg["unique_id"] = cfg["object_id"] = uid
                self.c.publish(f"{self.prefix}/{comp}/{uid}/config", json.dumps(cfg), retain=True)
        self.c.publish(f"{self.id}/status", "online", retain=True)
        log.info("MQTT connected, discovery sent")

    def state(self, key, on, power, energy):
        msg = {"on": "ON" if on else "OFF", "power": round(power, 1), "energy": round(energy, 4)}
        if self.dry:
            log.info("%s %s", key, msg)
        else:
            self.c.publish(f"{self.id}/{key}/state", json.dumps(msg), retain=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="only log, do not publish to MQTT")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    st = {"fridge": 0.0, "stove": 0.0}
    if STATE_FILE.exists():
        st.update(json.loads(STATE_FILE.read_text()))
    src, pub, stove = Source(), Publisher(a.dry_run), Stove()
    t_prev = saved = time.monotonic()
    while True:
        try:
            src.update()
            res = src.residual()
            now = res.index[-1]
            f_on, _ = fridge_state(res[FR["phase"]], now)
            s_on, s_p = stove.update(res, f_on, now)
            dt_h = (time.monotonic() - t_prev) / 3600
            t_prev = time.monotonic()
            f_p = float(FR["power"]) if f_on else 0.0
            st["fridge"] += f_p * dt_h / 1000
            st["stove"] += s_p * dt_h / 1000
            pub.state("fridge", f_on, f_p, st["fridge"])
            pub.state("stove", s_on, s_p, st["stove"])
            if not a.dry_run and time.monotonic() - saved > 60:
                STATE_FILE.write_text(json.dumps(st))
                saved = time.monotonic()
        except Exception:
            log.exception("error in cycle")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
