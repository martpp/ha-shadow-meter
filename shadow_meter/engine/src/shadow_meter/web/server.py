"""HTTP API and web UI.

Security model
- App mode: the UI is only reachable through Home Assistant ingress. Home Assistant authenticates the
  user; the app accepts connections *only* from the Supervisor's ingress proxy (172.30.32.2).
- Standalone mode: a bearer token (SHADOW_METER_API_TOKEN) is required for every API call unless the
  server listens on localhost only. The token is compared in constant time.
- Every response carries strict security headers; the UI loads no external resources (CSP 'self').
- Request bodies are small, JSON only, and validated with pydantic; unknown fields are rejected.
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
from ..settings import Runtime

_LOG = logging.getLogger(__name__)
INGRESS_PROXY = ipaddress.ip_address("172.30.32.2")
ANALYZER = web.AppKey("analyzer", Analyzer)
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


def build_app(analyzer: Analyzer, runtime: Runtime) -> web.Application:
    app = web.Application(middlewares=[access_middleware(runtime)], client_max_size=MAX_BODY)
    app[ANALYZER] = analyzer
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
                **analyzer.status,
                "version": __version__,
                "types": DEVICE_TYPES,
                "phases": [p.phase for p in analyzer.phase_configs],
                "mqtt": analyzer.mqtt_state(),
            },
            dumps=_dumps,
        )

    async def candidates(_: web.Request) -> web.Response:
        return web.json_response(analyzer.candidates(), dumps=_dumps)

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
            result = await analyzer.update_device(request.match_info["id"], body.status, body.name, body.type)
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
    app.router.add_get("/api/events", events)
    return app


async def events(request: web.Request) -> web.WebSocketResponse:
    """Push status and candidate updates to the UI as long as the socket is open."""
    analyzer = request.app[ANALYZER]
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
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
