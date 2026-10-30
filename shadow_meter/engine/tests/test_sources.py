"""Tests for the Home Assistant and InfluxDB clients against small fake servers."""

from __future__ import annotations

import math
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from shadow_meter.sources import parse_on, parse_power, to_changes
from shadow_meter.sources.homeassistant import AuthError, HomeAssistant
from shadow_meter.sources.influx import Influx, InfluxError, entity, ident

TOKEN = "secret-token"


def test_parsers() -> None:
    assert parse_power("12.5") == 12.5
    assert math.isnan(parse_power("unavailable"))
    assert math.isnan(parse_power("inf"))
    assert parse_on("on") is True
    assert parse_on("0") is False
    assert parse_on("unavailable") is None


def test_to_changes_ignores_repeats_and_unavailable() -> None:
    states = [(1, "off"), (2, "on"), (3, "on"), (4, "unavailable"), (5, "off"), (6, "on")]
    assert [(c.ts, c.on) for c in to_changes("light.x", states)] == [(2, True), (6, True)]


# --- fake Home Assistant ---------------------------------------------------------------------------
async def fake_ha(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_json({"type": "auth_required"})
    auth = await ws.receive_json()
    if auth.get("access_token") != TOKEN:
        await ws.send_json({"type": "auth_invalid"})
        await ws.close()
        return ws
    await ws.send_json({"type": "auth_ok", "ha_version": "test"})
    async for raw in ws:
        msg: dict[str, Any] = raw.json()
        mid, typ = msg["id"], msg["type"]
        if typ == "get_config":
            await ws.send_json(
                {"id": mid, "type": "result", "success": True, "result": {"time_zone": "Europe/Vienna"}}
            )
        elif typ == "history/history_during_period":
            assert msg["minimal_response"] and msg["no_attributes"]
            result = {
                "sensor.phase_a": [{"s": "100.0", "lu": 1000.0}, {"s": "137.0", "lu": 1010.0}],
                "light.hall": [
                    {"s": "off", "lc": 990.0},
                    {"s": "on", "lu": 1005.0},
                    {"s": "off", "lu": 1100.0},
                ],
            }
            await ws.send_json(
                {
                    "id": mid,
                    "type": "result",
                    "success": True,
                    "result": {k: v for k, v in result.items() if k in msg["entity_ids"]},
                }
            )
        elif typ == "subscribe_entities":
            await ws.send_json({"id": mid, "type": "result", "success": True, "result": None})
            await ws.send_json(
                {"id": mid, "type": "event", "event": {"a": {"sensor.phase_a": {"s": "100", "lc": 1.0}}}}
            )
            await ws.send_json(
                {
                    "id": mid,
                    "type": "event",
                    "event": {"c": {"sensor.phase_a": {"+": {"s": "140", "lu": 2.0}}}},
                }
            )
        else:
            await ws.send_json(
                {
                    "id": mid,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unknown_command", "message": typ},
                }
            )
    return ws


@pytest.fixture
async def ha_url() -> Any:
    app = web.Application()
    app.router.add_get("/api/websocket", fake_ha)
    server = TestServer(app)
    await server.start_server()
    yield str(server.make_url("/api/websocket"))
    await server.close()


async def test_ha_history_and_live(ha_url: str) -> None:
    async with aiohttp.ClientSession() as session:
        ha = HomeAssistant(session, ha_url, TOKEN)
        await ha.connect()
        assert (await ha.get_config())["time_zone"] == "Europe/Vienna"
        power = await ha.power(["sensor.phase_a"], 1000, 2000)
        assert [(s.ts, s.value) for s in power["sensor.phase_a"]] == [(1000.0, 100.0), (1010.0, 137.0)]
        changes = await ha.changes(["light.hall"], 1000, 2000)
        assert [(c.ts, c.on) for c in changes] == [(1005.0, True), (1100.0, False)]
        got = []
        async for item in ha.subscribe(["sensor.phase_a"]):
            got.append(item)
            if len(got) == 2:
                break
        assert got == [("sensor.phase_a", 1.0, "100"), ("sensor.phase_a", 2.0, "140")]
        await ha.close()


async def test_ha_bad_token(ha_url: str) -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AuthError):
            await HomeAssistant(session, ha_url, "wrong").connect()


# --- fake InfluxDB ---------------------------------------------------------------------------------
QUERIES: list[str] = []


async def fake_influx(request: web.Request) -> web.Response:
    """Answers the batched queries of the client like InfluxDB 1.x does (one series per tag set)."""
    q = request.query["q"]
    QUERIES.append(q)
    if request.query.get("db") != "ha":
        return web.json_response({"results": [{"error": "database not found"}]})
    series: list[dict[str, Any]] = []
    if 'FROM "W"' in q and 'last("value")' in q:
        series = [
            {
                "name": "W",
                "tags": {"entity_id": "phase_a"},
                "columns": ["time", "value"],
                "values": [[500_000, 99.0]],
            }
        ]
    elif 'FROM "W"' in q:
        series = [
            {
                "name": "W",
                "tags": {"entity_id": "phase_a"},
                "columns": ["time", "value"],
                "values": [[1_000_000, 100.0], [1_010_000, 137.0]],
            },
            {
                "name": "W",
                "tags": {"entity_id": "phase_b"},
                "columns": ["time", "value"],
                "values": [[1_000_000, 50.0]],
            },
        ]
    elif "FROM /^(" in q:
        series = [
            {
                "name": "light.hall",
                "columns": ["time", "state", "value"],
                "values": [[1_005_000, "on", 1], [1_100_000, "off", 0]],
            }
        ]
    elif 'FROM "state"' in q:
        series = [
            {
                "name": "state",
                "tags": {"domain": "light", "entity_id": "hall"},
                "columns": ["time", "state", "value"],
                "values": [[990_000, None, 0]],
            }
        ]
    return web.json_response({"results": [{"series": series}]})


@pytest.fixture
async def influx_url() -> Any:
    app = web.Application()
    app.router.add_get("/query", fake_influx)
    server = TestServer(app)
    await server.start_server()
    yield str(server.make_url(""))
    await server.close()


async def test_influx_power_and_changes(influx_url: str) -> None:
    QUERIES.clear()
    async with aiohttp.ClientSession() as session:
        db = Influx(session, influx_url, "ha")
        power = await db.power(["sensor.phase_a"], 1000, 2000)
        assert [(s.ts, s.value) for s in power["sensor.phase_a"]] == [
            (500.0, 99.0),
            (1000.0, 100.0),
            (1010.0, 137.0),
        ]
        assert len(QUERIES) == 2  # one query for the values before start, one for the range
        assert all("entity_id =~ /^(phase_a)$/" in q for q in QUERIES)
        both = await db.power(["sensor.phase_a", "sensor.phase_b"], 1000, 2000)
        assert [s.value for s in both["sensor.phase_b"]] == [50.0]
        QUERIES.clear()
        changes = await db.changes(["light.hall"], 1000, 2000)
        assert [(c.ts, c.on) for c in changes] == [(1005.0, True), (1100.0, False)]
        assert "FROM /^(light\\.hall)$/" in QUERIES[0]
        assert await db.power([], 0, 1) == {}
        assert await db.changes([], 0, 1) == []


async def test_influx_error_is_raised(influx_url: str) -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(InfluxError, match="database not found"):
            await Influx(session, influx_url, "other").ping()


@pytest.mark.parametrize("bad", ["x' OR 1=1 --", 'a"; DROP', "", "a b", "sensor.x;", "a\\b", '"', ".hidden"])
def test_unsafe_names_rejected(bad: str) -> None:
    with pytest.raises(InfluxError):
        ident(bad)


@pytest.mark.parametrize("ok", ["W", "home-assistant", "home_assistant", "light.kitchen", "kWh"])
def test_safe_names_accepted(ok: str) -> None:
    assert ident(ok) == ok


@pytest.mark.parametrize(
    "bad", ["sensor.x' OR 1=1 --", "light.a.b", "Sensor.X", "light", "light.a-b", "light.a'"]
)
def test_unsafe_entities_rejected(bad: str) -> None:
    with pytest.raises(InfluxError):
        entity(bad)


async def test_influx_refuses_non_select(influx_url: str) -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(InfluxError, match="only SELECT"):
            await Influx(session, influx_url, "ha").query("DROP DATABASE ha")
