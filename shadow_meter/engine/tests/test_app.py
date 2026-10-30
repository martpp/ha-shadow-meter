"""Analyzer (with a fake Home Assistant) and web API tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from shadow_meter import analyzer as analyzer_module
from shadow_meter.__main__ import read_options
from shadow_meter.analyzer import Analyzer
from shadow_meter.engine import Engine
from shadow_meter.models import Sample, StateChange
from shadow_meter.mqtt.discovery import Publisher
from shadow_meter.settings import Options, Runtime, Setup
from shadow_meter.sources.homeassistant import HomeAssistantError
from shadow_meter.store import Store
from shadow_meter.web.server import build_app

from .synthetic import T0, House, make_house

OPTIONS = Options.model_validate(
    {
        "phases": [
            {"phase": "a", "mains": "sensor.phase_a", "submeters": ["sensor.dishwasher"]},
            {"phase": "b", "mains": "sensor.phase_b"},
            {"phase": "c", "mains": "sensor.phase_c"},
        ],
        "history_days": 3,
    }
)


class FakeHA:
    connected = True

    def __init__(self, house: House) -> None:
        self.house = house

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_config(self) -> dict[str, Any]:
        return {"time_zone": "Europe/Vienna"}

    async def get_states(self) -> list[dict[str, Any]]:
        def st(eid: str, name: str, state: str, unit: str | None = None) -> dict[str, Any]:
            attrs = {"friendly_name": name} | ({"unit_of_measurement": unit} if unit else {})
            return {"entity_id": eid, "state": state, "attributes": attrs}

        return [
            st("light.hallway", "Hallway light", "off"),
            st("sensor.phase_a", "Phase A", "152.3", "W"),
            st("sensor.phase_b", "Phase B", "80.1", "W"),
            st("sensor.phase_c", "Phase C", "61.0", "W"),
            st("sensor.dishwasher", "Dishwasher power", "0.5", "W"),
            st("sensor.heat_pump", "Heat pump", "1.2", "kW"),  # not in W: not offered
            st("sensor.temperature", "Temperature", "21.5", "°C"),
        ]

    async def power(self, entity_ids: list[str], start: float, end: float) -> dict[str, list[Sample]]:
        out = {}
        for e in entity_ids:
            v = self.house.samples.get(e, [])
            before = [s for s in v if s.ts < start][-1:]
            out[e] = before + [s for s in v if start <= s.ts < end]
        return out

    async def changes(self, entity_ids: list[str], start: float, end: float) -> list[StateChange]:
        return [c for c in self.house.changes if c.entity_id in entity_ids and start <= c.ts < end]

    async def subscribe(self, entity_ids: list[str]) -> AsyncIterator[tuple[str, float, object]]:
        if False:  # pragma: no cover - live mode is not part of these tests
            yield ("", 0.0, "")


class FakeMqtt:
    def __init__(self) -> None:
        self.messages: dict[str, Any] = {}

    async def publish(self, topic: str, payload: str | bytes, qos: int = 0, retain: bool = False) -> None:
        self.messages[topic] = payload


@pytest.fixture(scope="module")
def house() -> House:
    return make_house(days=3)


@pytest.fixture
async def analyzer(house: House, tmp_path: Path) -> AsyncIterator[tuple[Analyzer, FakeMqtt, Store]]:
    store = Store(tmp_path / "db.sqlite")
    mqtt = FakeMqtt()
    clock_now = T0 + 3 * 86400 + 60
    a = Analyzer(
        OPTIONS,
        store,
        FakeHA(house),
        publisher=Publisher(mqtt, "homeassistant", "shadow_meter"),  # type: ignore[arg-type]
        clock=lambda: clock_now,
    )
    await a.setup()
    await a.backfill()
    yield a, mqtt, store
    store.close()


async def test_backfill_finds_devices_and_reports_progress(
    analyzer: tuple[Analyzer, FakeMqtt, Store],
) -> None:
    a, _, _ = analyzer
    assert a.status["backfill"]["percent"] == 100.0
    types = {c["type"] for c in a.candidates()}
    assert {"refrigerator", "heating", "light"} <= types
    light = next(c for c in a.candidates() if c["entity_id"] == "light.hallway")
    assert light["name"] == "Hallway light"
    fridge = next(c for c in a.candidates() if c["type"] == "refrigerator")
    assert fridge["name"].startswith("Fridge ~")
    assert len(fridge["history"]) >= 3  # one confidence point per analysed day


async def test_restart_resumes_without_reprocessing(
    analyzer: tuple[Analyzer, FakeMqtt, Store], house: House
) -> None:
    a, _, store = analyzer
    b = Analyzer(OPTIONS, store, FakeHA(house), clock=a.clock)  # type: ignore[arg-type]
    await b.setup()
    steps = b.detector.step_count
    await b.backfill()
    assert b.detector.step_count == steps
    assert {c["id"] for c in b.candidates()} == {c["id"] for c in a.candidates()}


async def test_activate_publishes_discovery_and_remove(analyzer: tuple[Analyzer, FakeMqtt, Store]) -> None:
    a, mqtt, store = analyzer
    fridge = next(c for c in a.candidates() if c["type"] == "refrigerator")
    updated = await a.update_device(fridge["id"], status="active", name="Kitchen fridge")
    assert updated["status"] == "active" and updated["name"] == "Kitchen fridge"
    configs = [t for t in mqtt.messages if t.startswith("homeassistant/") and t.endswith("/config")]
    assert len(configs) == 3
    cfg = json.loads(mqtt.messages[configs[0]])
    assert cfg["device"]["name"] == "Kitchen fridge"
    assert (await store.devices())[fridge["id"]]["name"] == "Kitchen fridge"
    await a.update_device(fridge["id"], status="confirmed")
    assert all(mqtt.messages[t] == b"" for t in configs)
    with pytest.raises(KeyError):
        await a.update_device("cluster:nope", status="active")


# --- web -------------------------------------------------------------------------------------------
def runtime(mode: str, host: str = "127.0.0.1", token: str = "") -> Runtime:
    return Runtime(
        mode=mode,
        ha_url="ws://x",
        ha_token=SecretStr("t"),
        data_dir=Path("."),
        listen_host=host,
        listen_port=0,
        api_token=SecretStr(token),
    )


def engine_for(a: Analyzer) -> Engine:
    """An engine that already runs the given analyzer (set up from the options)."""
    e = Engine(OPTIONS, a.store, a.ha)
    e.analyzer, e.setup, e.source = a, Setup(phases=OPTIONS.phases), "options"
    return e


async def client_for(a: Analyzer | Engine, rt: Runtime) -> TestClient[Any, Any]:
    engine = a if isinstance(a, Engine) else engine_for(a)
    c = TestClient(TestServer(build_app(engine, rt)))
    await c.start_server()
    return c


async def test_app_mode_only_accepts_ingress_proxy(analyzer: tuple[Analyzer, FakeMqtt, Store]) -> None:
    c = await client_for(analyzer[0], runtime("app"))
    try:
        r = await c.get("/api/status")  # the test client connects from 127.0.0.1, not 172.30.32.2
        assert r.status == 403
    finally:
        await c.close()


async def test_standalone_token(analyzer: tuple[Analyzer, FakeMqtt, Store]) -> None:
    c = await client_for(analyzer[0], runtime("standalone", host="0.0.0.0", token="s3cret"))  # noqa: S104
    try:
        assert (await c.get("/api/status")).status == 401
        assert (await c.get("/api/status", headers={"Authorization": "Bearer wrong"})).status == 401
        r = await c.get("/api/status", headers={"Authorization": "Bearer s3cret"})
        assert r.status == 200
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"
    finally:
        await c.close()

    c = await client_for(analyzer[0], runtime("standalone", host="0.0.0.0"))  # noqa: S104
    try:
        assert (await c.get("/api/status")).status == 403  # remote without a token configured: refused
    finally:
        await c.close()


async def test_api_and_validation(analyzer: tuple[Analyzer, FakeMqtt, Store]) -> None:
    a = analyzer[0]
    c = await client_for(a, runtime("standalone"))
    try:
        status = await (await c.get("/api/status")).json()
        assert status["phases"] == ["a", "b", "c"]
        cands = await (await c.get("/api/candidates")).json()
        cid = cands[0]["id"]
        r = await c.patch(f"/api/candidates/{cid}", json={"name": "Thing", "type": "oven"})
        assert r.status == 200 and (await r.json())["type"] == "oven"
        assert (await c.patch(f"/api/candidates/{cid}", json={"type": "rocket"})).status == 400
        assert (await c.patch(f"/api/candidates/{cid}", json={"status": "hacked"})).status == 400
        assert (await c.patch(f"/api/candidates/{cid}", json={"evil": 1})).status == 400
        assert (await c.patch(f"/api/candidates/{cid}", data="name=x")).status == 415
        assert (await c.patch("/api/candidates/none", json={"name": "x"})).status == 404
        assert (await c.patch(f"/api/candidates/{cid}", json={"name": "x" * 81})).status == 400
        assert (await c.get("/static/../settings.py")).status == 404
        assert (await c.get("/static/app.js")).status == 200
        assert "Shadow Meter" in await (await c.get("/")).text()
        big = {"name": "x" * 20000}
        assert (await c.patch(f"/api/candidates/{cid}", json=big)).status in (400, 413)
    finally:
        await c.close()


async def test_activation_needs_mqtt(house: House, tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    a = Analyzer(OPTIONS, store, FakeHA(house), clock=lambda: T0 + 3 * 86400 + 60)  # type: ignore[arg-type]
    await a.setup()
    await a.backfill()
    assert a.mqtt_state() == "disabled"
    cid = a.candidates()[0]["id"]
    c = await client_for(a, runtime("standalone"))
    try:
        assert (await (await c.get("/api/status")).json())["mqtt"] == "disabled"
        r = await c.patch(f"/api/candidates/{cid}", json={"status": "active"})
        assert r.status == 409
        assert (await c.patch(f"/api/candidates/{cid}", json={"status": "confirmed"})).status == 200
    finally:
        await c.close()
        store.close()


async def test_events_websocket(analyzer: tuple[Analyzer, FakeMqtt, Store]) -> None:
    a = analyzer[0]
    c = await client_for(a, runtime("standalone"))
    try:
        ws = await c.ws_connect("/api/events")
        first = json.loads((await ws.receive()).data)
        second = json.loads((await ws.receive()).data)
        assert first["kind"] == "status" and second["kind"] == "candidates"
        a.set_status(message="hello")
        third = json.loads((await ws.receive()).data)
        assert third["data"]["message"] == "hello"
        await ws.close()
    finally:
        await c.close()


# --- live ------------------------------------------------------------------------------------------
class LiveHA(FakeHA):
    """Replays the last day of the house as a live stream and moves the fake clock along."""

    def __init__(self, house: House, live_from: float, clock: list[float]) -> None:
        super().__init__(house)
        self.live_from = live_from
        self.clock = clock

    async def subscribe(self, entity_ids: list[str]) -> AsyncIterator[tuple[str, float, object]]:
        events: list[tuple[float, str, object]] = [
            (s.ts, e, str(s.value))
            for e, v in self.house.samples.items()
            if e in entity_ids
            for s in v
            if s.ts >= self.live_from
        ]
        events += [
            (c.ts, c.entity_id, "on" if c.on else "off")
            for c in self.house.changes
            if c.entity_id in entity_ids and c.ts >= self.live_from
        ]
        events.sort(key=lambda x: x[0])
        for i, (ts, e, st) in enumerate(events):
            self.clock[0] = ts
            yield e, ts, st
            if i % 2000 == 0:
                await asyncio.sleep(0.002)  # let the tick task run
        # time moves on after the last report, so the tick task processes everything before the stream ends
        self.clock[0] += 60
        for _ in range(50):
            await asyncio.sleep(0.01)


async def test_live_mode_processes_stream_and_publishes(
    house: House, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analyzer_module, "LIVE_TICK_S", 0.001)
    monkeypatch.setattr(analyzer_module, "PUBLISH_EVERY_S", 0.0)
    monkeypatch.setattr(analyzer_module, "REFRESH_S", 0.0)
    live_from = T0 + 2 * 86400
    clock = [live_from]
    store = Store(tmp_path / "db.sqlite")
    mqtt = FakeMqtt()
    opts = OPTIONS.model_copy(update={"history_days": 2})
    ha = LiveHA(house, live_from, clock)
    a = Analyzer(opts, store, ha, publisher=Publisher(mqtt, "homeassistant", "sm"), clock=lambda: clock[0])  # type: ignore[arg-type]
    await a.setup()
    await a.backfill()
    fridge = next(c for c in a.candidates() if c["type"] == "refrigerator")
    await a.update_device(fridge["id"], status="active")
    with pytest.raises(ExceptionGroup) as info:
        await a.live()
    assert info.group_contains(HomeAssistantError)
    assert a.detector.processed_until >= live_from + 86400 - 60
    states = [json.loads(v) for t, v in mqtt.messages.items() if t.startswith("sm/") and t.endswith("/state")]
    assert states
    assert states[0]["energy"] > 0.05  # ~0.3 kWh/day for a 37 W fridge running a third of the time
    store.close()


def test_readable_option_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOW_METER_OPTIONS", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit, match="options file not found"):
        read_options()
    bad = tmp_path / "options.json"
    bad.write_text(
        json.dumps(
            {"phases": [{"phase": "A", "mains": "not an entity"}], "influx": {"password": "s3cret-value"}}
        )
    )
    monkeypatch.setenv("SHADOW_METER_OPTIONS", str(bad))
    with pytest.raises(SystemExit) as info:
        read_options()
    msg = str(info.value)
    assert "phases.0.mains" in msg
    assert "s3cret-value" not in msg  # values are never echoed
    bad.write_text("{not json")
    with pytest.raises(SystemExit, match="not valid JSON"):
        read_options()
