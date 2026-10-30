"""Shadow Meter for Home Assistant - load the configuration.

config.yaml (path can be overridden with the SHADOW_METER_CONFIG environment variable).
Everything specific to your home (entities, meter lists, exclusions, thresholds) lives in the YAML file,
credentials live in separate env files (see config.example.yaml).
"""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
CFG = yaml.safe_load(open(os.environ.get("SHADOW_METER_CONFIG", ROOT / "config.yaml"), encoding="utf-8"))

TZ = CFG.get("timezone", "UTC")  # local time for hour-of-day statistics and plots
DATA = Path(os.path.expanduser(CFG.get("data_dir", ROOT / "data")))
OUT = Path(os.path.expanduser(CFG.get("out_dir", ROOT / "out")))
OUT.mkdir(parents=True, exist_ok=True)

PHASES = CFG["total_phases"]  # {"a": entity, "b": ..., "c": ...}
METERS = CFG.get("meters", [])
METERS_NOT_BEHIND_MAIN = set(CFG.get("meters_not_behind_main", []))
METER_PHASE_FIXED = CFG.get("meter_phase_fixed", {})
LIGHTS_EXCLUDE = set(CFG.get("lights_exclude", []))
LIGHTS_NOT_BEHIND_MAIN = set(CFG.get("lights_not_behind_main", []))
METERED_LIGHTS = CFG.get("metered_lights", {})  # meter -> light (validation, and never subtracted twice)


def env_file(path):
    """Read a KEY=VALUE file; environment variables with the same name take precedence."""
    p = Path(os.path.expanduser(path))
    vals = {}
    if p.exists():
        vals = dict(l.strip().split("=", 1) for l in p.open() if "=" in l and not l.startswith("#"))
    return {**vals, **{k: v for k, v in os.environ.items() if k.startswith(("INFLUX_", "MQTT_"))}}


def influx_client(timeout=600):
    from influxdb import InfluxDBClient

    c = CFG["influx"]
    e = env_file(c.get("env_file", "~/.shadow-meter-influx.env"))
    return InfluxDBClient(c.get("host", "localhost"), c.get("port", 8086), e["INFLUX_USER"], e["INFLUX_PW"],
                          c["database"], timeout=timeout)
