"""Validated settings.

As a Home Assistant app the options come from /data/options.json (written by the Supervisor from the
app configuration page). Standalone they come from the JSON file given with SHADOW_METER_OPTIONS.
Secrets (tokens, passwords) never appear in logs: they are `SecretStr`.

Entity ids are validated strictly. They are later used inside InfluxQL queries, so the format check is
also the first line of defence against query injection.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, field_validator

ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
PHASE_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _entity(v: str) -> str:
    if not ENTITY_RE.fullmatch(v):
        raise ValueError(f"not a valid entity id: {v!r}")
    return v


def _phase(v: str) -> str:
    if not PHASE_RE.fullmatch(v):
        raise ValueError(f"phase must be 1-16 letters/digits: {v!r}")
    return v


def _name(v: str) -> str:
    if not NAME_RE.fullmatch(v):
        raise ValueError(f"invalid name: {v!r}")
    return v


def _optional_name(v: str) -> str:
    return v if v == "" else _name(v)


EntityId = Annotated[str, AfterValidator(_entity)]
PhaseName = Annotated[str, AfterValidator(_phase)]
Identifier = Annotated[str, AfterValidator(_name)]
OptionalIdentifier = Annotated[str, AfterValidator(_optional_name)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PhaseOptions(_Model):
    phase: PhaseName
    mains: EntityId
    submeters: list[EntityId] = []


class InfluxOptions(_Model):
    enabled: bool = False
    url: str = "http://localhost:8086"
    database: Identifier = "homeassistant"
    username: str = ""
    password: SecretStr = SecretStr("")
    power_measurement: Identifier = "W"
    # older HA versions wrote all states into one measurement "state"; empty = not used
    legacy_state_measurement: OptionalIdentifier = "state"
    history_start: date | None = None  # empty = as far back as the data goes (max. history_days_max)
    verify_ssl: bool = True

    @field_validator("history_start", mode="before")
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        return None if v == "" else v


class MqttOptions(_Model):
    # empty host: use the MQTT service of the Supervisor (app) or disable publishing (standalone)
    host: str = ""
    port: int = Field(1883, ge=1, le=65535)
    username: str = ""
    password: SecretStr = SecretStr("")
    discovery_prefix: Identifier = "homeassistant"
    topic_prefix: Identifier = "shadow_meter"


class Options(_Model):
    phases: list[PhaseOptions] = Field(min_length=1, max_length=8)
    context_domains: list[Literal["light", "switch", "fan", "input_boolean"]] = ["light"]
    context_entities: list[EntityId] = []
    exclude_entities: list[EntityId] = []
    history_days: int = Field(14, ge=0, le=60)  # from the Home Assistant recorder
    history_days_max: int = Field(3 * 365, ge=0, le=20 * 365)  # from InfluxDB
    min_step_w: float = Field(12.0, ge=3, le=1000)
    influx: InfluxOptions = InfluxOptions()
    mqtt: MqttOptions = MqttOptions()
    log_level: Literal["debug", "info", "warning", "error"] = "info"


class Runtime(_Model):
    """How the engine runs; from the environment, not from user options."""

    mode: Literal["app", "standalone"]
    ha_url: str  # websocket url
    ha_token: SecretStr
    data_dir: Path
    listen_host: str
    listen_port: int
    api_token: SecretStr  # standalone only; empty = API disabled for remote clients


def load_options(path: Path | None = None) -> Options:
    p = path or Path(os.environ.get("SHADOW_METER_OPTIONS", "/data/options.json"))
    return Options.model_validate(json.loads(p.read_text(encoding="utf-8")))


def load_runtime() -> Runtime:
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if supervisor_token:
        return Runtime(
            mode="app",
            ha_url="ws://supervisor/core/websocket",
            ha_token=SecretStr(supervisor_token),
            data_dir=Path("/data"),
            listen_host="0.0.0.0",  # noqa: S104 - only the ingress proxy is allowed, see web.py
            listen_port=8099,
            api_token=SecretStr(""),
        )
    return Runtime(
        mode="standalone",
        ha_url=os.environ.get("HA_URL", "ws://homeassistant.local:8123/api/websocket"),
        ha_token=SecretStr(os.environ.get("HA_TOKEN", "")),
        data_dir=Path(os.environ.get("SHADOW_METER_DATA", "./data")),
        listen_host=os.environ.get("SHADOW_METER_HOST", "127.0.0.1"),
        listen_port=int(os.environ.get("SHADOW_METER_PORT", "8099")),
        api_token=SecretStr(os.environ.get("SHADOW_METER_API_TOKEN", "")),
    )
