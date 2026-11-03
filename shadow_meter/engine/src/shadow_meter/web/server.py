"""HTTP API and web UI.

Security model
- App mode: the UI is only reachable through Home Assistant ingress. Home Assistant authenticates the
  user; the app accepts connections *only* from the Supervisor's ingress proxy (172.30.32.2).
- Standalone mode: a bearer token (SHADOW_METER_API_TOKEN) is required for every API call unless the
  server listens on localhost only. The token is compared in constant time.
- Every response carries strict security headers; the UI loads no external resources (CSP 'self').
- Request bodies are small, JSON only, and validated with pydantic; unknown fields are rejected.
- The meter setup can only use power sensors that exist in Home Assistant (checked on save).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import math
from collections.abc import Awaitable, Callable
from importlib import resources
from typing import Any, Literal

from aiohttp import WSMsgType, web
from aiohttp.typedefs import Middleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import __version__
from ..analyzer import Analyzer, PublishUnavailable
from ..detection.classify import DEVICE_TYPES
from ..engine import Engine
from ..settings import Runtime, Setup
from ..setup import power_factor_sensors, power_sensors, suggest_phases, suggest_power_factors

_LOG = logging.getLogger(__name__)
INGRESS_PROXY = ipaddress.ip_address("172.30.32.2")
ENGINE = web.AppKey("engine", Engine)
MAX_BODY = 16 * 1024

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class DeviceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["new", "confirmed", "rejected", "active"] | None = None
    name: str | None = Field(None, max_length=80)
    type: str | None = Field(None, max_length=40)


def _is_local(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def access_middleware(runtime: Runtime) -> Middleware:
    token = runtime.api_token.get_secret_value()
    local_only = _is_local(runtime.listen_host)

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        if runtime.mode == "app":
            try:
                remote = ipaddress.ip_address(request.remote or "")
            except ValueError:
                remote = None
            if remote != INGRESS_PROXY:
                raise web.HTTPForbidden(text="only reachable through Home Assistant ingress")
        elif not local_only:
            if not token:
                raise web.HTTPForbidden(text="set SHADOW_METER_API_TOKEN to allow remote access")
            sent = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            # browsers cannot set headers on WebSockets: accept the token as a query parameter there
            sent = sent or request.query.get("token", "")
            if not hmac.compare_digest(sent.encode(), token.encode()):
                raise web.HTTPUnauthorized(headers={"WWW-Authenticate": "Bearer"})
        response = await handler(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    return middleware


def _analyzer(engine: Engine) -> Analyzer:
    if engine.analyzer is None:
        raise web.HTTPConflict(text="Choose your power meters in Settings first")
    return engine.analyzer


def build_app(engine: Engine, runtime: Runtime) -> web.Application:
    app = web.Application(middlewares=[access_middleware(runtime)], client_max_size=MAX_BODY)
    app[ENGINE] = engine
    static = resources.files("shadow_meter.web") / "static"

    async def index(_: web.Request) -> web.Response:
        return web.Response(
            text=(static / "index.html").read_text(encoding="utf-8"), content_type="text/html"
        )

    async def asset(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        types = {"app.js": "text/javascript", "style.css": "text/css", "icon.svg": "image/svg+xml"}
        if name not in types:  # fixed allow-list, no file system paths from the request
            raise web.HTTPNotFound()
        return web.Response(text=(static / name).read_text(encoding="utf-8"), content_type=types[name])

    async def status(_: web.Request) -> web.Response:
        return web.json_response(
            {
                **engine.status(),
                "version": __version__,
                "types": DEVICE_TYPES,
                "phases": [p.phase for p in engine.setup.phases] if engine.setup else [],
                "setup_source": engine.source,
                "mqtt": engine.mqtt_state(),
            },
            dumps=_dumps,
        )

    async def candidates(_: web.Request) -> web.Response:
        return web.json_response(engine.analyzer.candidates() if engine.analyzer else [], dumps=_dumps)

    async def update(request: web.Request) -> web.Response:
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        try:
            body = DeviceUpdate.model_validate(await request.json())
        except (ValidationError, json.JSONDecodeError) as err:
            raise web.HTTPBadRequest(text="invalid request") from err
        if body.type is not None and body.type and body.type not in DEVICE_TYPES:
            raise web.HTTPBadRequest(text="unknown device type")
        try:
            result = await _analyzer(engine).update_device(
                request.match_info["id"], body.status, body.name, body.type
            )
        except KeyError as err:
            raise web.HTTPNotFound() from err
        except PublishUnavailable as err:
            raise web.HTTPConflict(
                text="No MQTT broker: install the Mosquitto app or configure mqtt"
            ) from err
        return web.json_response(result, dumps=_dumps)

    app.router.add_get("/", index)
    app.router.add_get("/static/{name}", asset)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/candidates", candidates)
    app.router.add_patch("/api/candidates/{id}", update)
    app.router.add_get("/api/setup", get_setup)
    app.router.add_put("/api/setup", put_setup)
    app.router.add_post("/api/reanalyse", reanalyse)
    app.router.add_get("/api/events", events)
    return app


async def get_setup(request: web.Request) -> web.Response:
    """Power sensors of Home Assistant, the current setup and a suggestion for the phases."""
    engine = request.app[ENGINE]
    try:
        states = await engine.states()
    except Exception as err:  # Home Assistant unreachable: tell the UI instead of failing silently
        _LOG.warning("could not read the entities of Home Assistant: %s", err)
        raise web.HTTPBadGateway(text="Home Assistant is not reachable, try again in a moment") from err
    sensors, pf_sensors = power_sensors(states), power_factor_sensors(states)
    pf_for = suggest_power_factors([s["entity_id"] for s in sensors], [s["entity_id"] for s in pf_sensors])
    current = engine.setup.model_dump(mode="json")["phases"] if engine.setup else []
    suggestion = [{**p, "power_factor": pf_for.get(p["mains"])} for p in suggest_phases(sensors)]
    body = {
        "source": engine.source,
        "phases": current,
        "sensors": sensors,
        "suggestion": suggestion,
        "pf_sensors": pf_sensors,
        "pf_suggestion": pf_for,  # power sensor -> its power factor sensor
        "influx": engine.influx is not None,
        "history_days": engine.options.history_days,
    }
    return web.json_response(body, dumps=_dumps)


async def put_setup(request: web.Request) -> web.Response:
    """Save the meter setup chosen in the UI and restart the analysis."""
    engine = request.app[ENGINE]
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        setup = Setup.model_validate(await request.json())
    except ValidationError as err:
        reasons = "; ".join(e["msg"].removeprefix("Value error, ") for e in err.errors())
        raise web.HTTPBadRequest(text=reasons) from err
    except json.JSONDecodeError as err:
        raise web.HTTPBadRequest(text="invalid request") from err
    states = await engine.states()
    known = {s["entity_id"] for s in power_sensors(states)}
    used = {p.mains for p in setup.phases} | {s for p in setup.phases for s in p.submeters}
    unknown = sorted(used - known)
    if unknown:
        raise web.HTTPBadRequest(text=f"not a power sensor in Home Assistant: {', '.join(unknown)}")
    known_pf = {s["entity_id"] for s in power_factor_sensors(states)}
    unknown = sorted({p.power_factor for p in setup.phases if p.power_factor} - known_pf)
    if unknown:
        raise web.HTTPBadRequest(text=f"not a power factor sensor in Home Assistant: {', '.join(unknown)}")
    reset = await engine.reconfigure(setup)
    return web.json_response({"ok": True, "reset": reset})


async def reanalyse(request: web.Request) -> web.Response:
    """Start the analysis over, e.g. after linking an InfluxDB. Discards the learned pattern devices.

    Needs a JSON body {"confirm": true}: a JSON request cannot be sent by a plain HTML form of another
    site, and the explicit flag guards against accidental calls."""
    engine = request.app[ENGINE]
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        body = await request.json()
    except json.JSONDecodeError as err:
        raise web.HTTPBadRequest(text="invalid request") from err
    if not isinstance(body, dict) or body.get("confirm") is not True:
        raise web.HTTPBadRequest(text='send {"confirm": true} to start over')
    if engine.setup is None:
        raise web.HTTPConflict(text="Choose your power meters in Settings first")
    await engine.reanalyse()
    return web.json_response({"ok": True})


async def events(request: web.Request) -> web.WebSocketResponse:
    """Push status and candidate updates to the UI as long as the socket is open."""
    engine = request.app[ENGINE]
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    analyzer = engine.analyzer
    if analyzer is None:  # nothing to follow until the meters are set up; the UI reloads after saving
        await ws.send_str(_dumps({"kind": "status", "data": engine.status()}))
        await _drain(ws)
        return ws
    queue = analyzer.subscribe()
    try:
        await ws.send_str(_dumps({"kind": "status", "data": analyzer.status}))
        await ws.send_str(_dumps({"kind": "candidates", "data": analyzer.candidates()}))
        reader = asyncio.create_task(_drain(ws))
        while not ws.closed and not reader.done():
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                await ws.send_str(_dumps(getter.result()))
            else:
                getter.cancel()
        reader.cancel()
    finally:
        analyzer.unsubscribe(queue)
    return ws


async def _drain(ws: web.WebSocketResponse) -> None:
    """Read (and ignore) client messages until the socket closes."""
    with contextlib.suppress(Exception):
        async for msg in ws:
            if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break


def _dumps(obj: Any) -> str:
    """JSON without NaN/Infinity (not valid JSON for browsers)."""
    return json.dumps(_clean(obj), separators=(",", ":"))


def _clean(obj: Any) -> Any:
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj
