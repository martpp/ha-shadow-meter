"""Meter setup in the web UI: sensor list, phase suggestion, validation, (re)configuration and reset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from shadow_meter.engine import Engine
from shadow_meter.mqtt.discovery import Publisher
from shadow_meter.settings import Options, Setup
from shadow_meter.setup import power_sensors, suggest_phases
from shadow_meter.store import Store

from .synthetic import make_house
from .test_app import FakeHA, FakeMqtt, client_for, runtime

NO_PHASES = Options.model_validate({"phases": [], "history_days": 0})
ABC = {
    "phases": [
        {"phase": "A", "mains": "sensor.phase_a", "submeters": ["sensor.dishwasher"]},
        {"phase": "B", "mains": "sensor.phase_b"},
        {"phase": "C", "mains": "sensor.phase_c"},
    ]
}


def sensors(*ids: str) -> list[dict[str, Any]]:
    return [{"entity_id": e, "name": e, "value": 1.0} for e in ids]


@pytest.mark.parametrize(
    "ids",
    [
        (
            "sensor.shellyem3_ab12_channel_a_power",
            "sensor.shellyem3_ab12_channel_b_power",
            "sensor.shellyem3_ab12_channel_c_power",
        ),
        (
            "sensor.shellypro3em_x_phase_a_active_power",
            "sensor.shellypro3em_x_phase_b_active_power",
            "sensor.shellypro3em_x_phase_c_active_power",
        ),
        ("sensor.meter_power_l1", "sensor.meter_power_l2", "sensor.meter_power_l3"),
    ],
)
def test_suggests_three_phase_meters(ids: tuple[str, str, str]) -> None:
    got = suggest_phases(sensors(*ids, "sensor.fridge_plug_power"))
    assert [(p["phase"], p["mains"]) for p in got] == list(zip("ABC", ids, strict=True))


def test_no_suggestion_when_ambiguous_or_incomplete() -> None:
    two_meters = sensors(*(f"sensor.{m}_channel_{p}_power" for m in ("em_one", "em_two") for p in "abc"))
    assert suggest_phases(two_meters) == []
    assert suggest_phases(sensors("sensor.em_channel_a_power", "sensor.em_channel_b_power")) == []


def test_power_sensors_only_watts() -> None:
    states = [
        {
            "entity_id": "sensor.b",
            "state": "12.5",
            "attributes": {"unit_of_measurement": "W", "friendly_name": "B"},
        },
        {"entity_id": "sensor.a", "state": "unavailable", "attributes": {"unit_of_measurement": "W"}},
        {"entity_id": "sensor.k", "state": "1", "attributes": {"unit_of_measurement": "kW"}},
        {"entity_id": "switch.w", "state": "on", "attributes": {"unit_of_measurement": "W"}},
    ]
    assert power_sensors(states) == [  # sorted by display name
        {"entity_id": "sensor.b", "name": "B", "value": 12.5},
        {"entity_id": "sensor.a", "name": "sensor.a", "value": None},
    ]


@pytest.mark.parametrize(
    ("phases", "error"),
    [
        ([{"phase": "A", "mains": "sensor.x"}, {"phase": "A", "mains": "sensor.y"}], "own name"),
        ([{"phase": "A", "mains": "sensor.x"}, {"phase": "B", "mains": "sensor.x"}], "mains of one phase"),
        ([{"phase": "A", "mains": "sensor.x", "submeters": ["sensor.x"]}], "cannot also be"),
        (
            [
                {"phase": "A", "mains": "sensor.x", "submeters": ["sensor.s"]},
                {"phase": "B", "mains": "sensor.y", "submeters": ["sensor.s"]},
            ],
            "one phase",
        ),
        ([], "at least 1"),
    ],
)
def test_setup_validation(phases: list[dict[str, Any]], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        Setup.model_validate({"phases": phases})


async def test_setup_flow_without_options(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    engine = Engine(NO_PHASES, store, FakeHA(make_house(days=1)))  # type: ignore[arg-type]
    await engine.start()
    assert engine.analyzer is None
    assert engine.status()["state"] == "setup"
    c = await client_for(engine, runtime("standalone"))
    try:
        assert (await (await c.get("/api/status")).json())["state"] == "setup"
        assert await (await c.get("/api/candidates")).json() == []
        setup = await (await c.get("/api/setup")).json()
        assert [s["entity_id"] for s in setup["sensors"]] == [
            "sensor.dishwasher",
            "sensor.phase_a",
            "sensor.phase_b",
            "sensor.phase_c",
        ]
        assert setup["phases"] == [] and setup["source"] is None
        bad = {"phases": [{"phase": "A", "mains": "sensor.heat_pump"}]}  # kW sensor: not offered
        r = await c.put("/api/setup", json=bad)
        assert r.status == 400 and "sensor.heat_pump" in await r.text()
        r = await c.put(
            "/api/setup",
            json={
                "phases": [
                    {"phase": "A", "mains": "sensor.phase_a"},
                    {"phase": "A", "mains": "sensor.phase_b"},
                ]
            },
        )
        assert r.status == 400 and "own name" in await r.text()
        r = await c.put("/api/setup", json=ABC)
        assert r.status == 200 and (await r.json())["reset"] is False
        assert engine.analyzer is not None and engine.source == "ui"
        assert [p.phase for p in engine.analyzer.phase_configs] == ["A", "B", "C"]
        assert (await (await c.get("/api/setup")).json())["phases"][0]["submeters"] == ["sensor.dishwasher"]
    finally:
        await c.close()
        await engine.stop()
    # the saved setup is used after a restart, even without phases in the options
    again = Engine(NO_PHASES, store, FakeHA(make_house(days=1)))  # type: ignore[arg-type]
    await again.load_setup()
    assert again.source == "ui" and again.setup is not None and len(again.setup.phases) == 3
    store.close()


async def test_reanalyse_starts_over(tmp_path: Path) -> None:
    """After linking an InfluxDB the user can analyse the history again from the start."""
    store = Store(tmp_path / "db.sqlite")
    engine = Engine(NO_PHASES, store, FakeHA(make_house(days=1)))  # type: ignore[arg-type]
    await engine.start()
    c = await client_for(engine, runtime("standalone"))
    try:
        r = await c.post("/api/reanalyse", json={"confirm": True})
        assert r.status == 409  # nothing to analyse before the meters are chosen
        assert (await (await c.get("/api/setup")).json())["influx"] is False
        assert (await c.put("/api/setup", json=ABC)).status == 200
        await engine.stop()
        await store.set_json("detector", {"processed_until": 123.0})
        await store.update_device("cluster:A-3", status="confirmed", name="Fridge")
        await store.update_device("entity:light.hallway", status="active", name="Hallway")
        assert (await c.post("/api/reanalyse", data="confirm=true")).status == 415  # no plain form posts
        assert (await c.post("/api/reanalyse", json={})).status == 400  # explicit confirmation needed
        assert await store.get_json("detector") is not None
        r = await c.post("/api/reanalyse", json={"confirm": True})
        assert r.status == 200
        assert engine.analyzer is not None and engine.setup is not None
        devices = await store.devices()
        assert "cluster:A-3" not in devices
        assert devices["entity:light.hallway"]["name"] == "Hallway"
    finally:
        await c.close()
        await engine.stop()
    saved = await store.get_json("detector")
    assert saved is None or saved.get("processed_until") != 123.0  # the old state is gone
    store.close()


async def test_changing_meters_resets_learned_data(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    mqtt = FakeMqtt()
    engine = Engine(
        NO_PHASES,
        store,
        FakeHA(make_house(days=1)),  # type: ignore[arg-type]
        publisher=Publisher(mqtt, "homeassistant", "sm"),
    )
    await engine.reconfigure(Setup.model_validate(ABC))
    await engine.stop()
    # pretend the analysis learned something and two devices were added to Home Assistant
    await store.set_json("detector", {"processed_until": 123.0})
    await store.update_device("cluster:A-3", status="active", name="Fridge")
    await store.update_device("entity:light.hallway", status="active", name="Hallway")
    same = await engine.reconfigure(Setup.model_validate(ABC))
    await engine.stop()
    assert same is False
    assert await store.get_json("detector") is not None  # same meters: nothing lost
    other = {"phases": [{"phase": "A", "mains": "sensor.phase_b"}]}
    assert await engine.reconfigure(Setup.model_validate(other)) is True
    await engine.stop()
    devices = await store.devices()
    assert "cluster:A-3" not in devices  # belonged to the old analysis
    assert devices["entity:light.hallway"]["name"] == "Hallway"  # lights do not depend on the meters
    removed = [t for t, v in mqtt.messages.items() if "cluster_a_3" in t and v == b""]
    assert removed  # and it was removed from Home Assistant
    assert json.loads(json.dumps(await store.get_json("setup")))["phases"][0]["mains"] == "sensor.phase_b"
    store.close()
