"""Home Assistant WebSocket API client: entity list, recorder history and live state changes.

As an app, Home Assistant is reached through the Supervisor proxy (ws://supervisor/core/websocket) with
the SUPERVISOR_TOKEN; standalone with a long-lived access token. Only read-only commands are used.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import aiohttp

from ..models import Sample, StateChange
from . import parse_power, to_changes

_LOG = logging.getLogger(__name__)
TIMEOUT_S = 60
MAX_MSG = 64 * 1024 * 1024  # history responses can be large


class HomeAssistantError(Exception):
    pass


class AuthError(HomeAssistantError):
    pass


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _state_ts(item: dict[str, Any]) -> float | None:
    """Timestamp of a compressed state: last_updated ('lu'), else last_changed ('lc')."""
    ts = item.get("lu", item.get("lc"))
    return float(ts) if isinstance(ts, (int, float)) else None


class HomeAssistant:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = url
        self._token = token
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscriptions: dict[int, asyncio.Queue[dict[str, Any] | None]] = {}
        self._reader: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        self._ws = await self._session.ws_connect(self._url, max_msg_size=MAX_MSG, heartbeat=30)
        msg = await self._ws.receive_json(timeout=TIMEOUT_S)
        if msg.get("type") != "auth_required":
            raise HomeAssistantError(f"unexpected greeting: {msg.get('type')}")
        await self._ws.send_json({"type": "auth", "access_token": self._token})
        msg = await self._ws.receive_json(timeout=TIMEOUT_S)
        if msg.get("type") != "auth_ok":
            raise AuthError("authentication with Home Assistant failed")
        _LOG.info("connected to Home Assistant %s", msg.get("ha_version", ""))
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        if self._ws:
            await self._ws.close()

    @property
    def connected(self) -> bool:
        return (
            self._ws is not None
            and not self._ws.closed
            and self._reader is not None
            and not self._reader.done()
        )

    async def _read(self) -> None:
        if self._ws is None:
            return
        try:
            async for raw in self._ws:
                if raw.type != aiohttp.WSMsgType.TEXT:
                    continue
                msg = raw.json()
                mid = msg.get("id")
                if msg.get("type") == "result" and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        if msg.get("success"):
                            fut.set_result(msg.get("result"))
                        else:
                            err = msg.get("error", {})
                            fut.set_exception(HomeAssistantError(f"{err.get('code')}: {err.get('message')}"))
                elif msg.get("type") == "event" and mid in self._subscriptions:
                    await self._subscriptions[mid].put(msg.get("event", {}))
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(HomeAssistantError("connection closed"))
            self._pending.clear()
            for q in self._subscriptions.values():
                q.put_nowait(None)

    async def _call(self, msg: dict[str, Any]) -> Any:
        if self._ws is None:
            raise HomeAssistantError("not connected")
        mid = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send_json({**msg, "id": mid})
        try:
            return await asyncio.wait_for(fut, TIMEOUT_S)
        finally:
            self._pending.pop(mid, None)

    async def get_config(self) -> dict[str, Any]:
        result: dict[str, Any] = await self._call({"type": "get_config"})
        return result

    async def get_states(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._call({"type": "get_states"})
        return result

    async def _history(
        self, entity_ids: list[str], start: float, end: float
    ) -> dict[str, list[dict[str, Any]]]:
        if not entity_ids:
            return {}
        result: dict[str, list[dict[str, Any]]] = await self._call(
            {
                "type": "history/history_during_period",
                "start_time": _iso(start),
                "end_time": _iso(end),
                "entity_ids": entity_ids,
                "include_start_time_state": True,
                "significant_changes_only": False,
                "minimal_response": True,
                "no_attributes": True,
            }
        )
        return result or {}

    # HistorySource -----------------------------------------------------------------------------
    async def power(self, entity_ids: list[str], start: float, end: float) -> dict[str, list[Sample]]:
        raw = await self._history(entity_ids, start, end)
        out: dict[str, list[Sample]] = {}
        for eid, items in raw.items():
            samples = [
                Sample(ts, parse_power(it.get("s"))) for it in items if (ts := _state_ts(it)) is not None
            ]
            out[eid] = sorted(samples, key=lambda s: s.ts)
        return out

    async def changes(self, entity_ids: list[str], start: float, end: float) -> list[StateChange]:
        raw = await self._history(entity_ids, start, end)
        out: list[StateChange] = []
        for eid, items in raw.items():
            states = sorted(
                ((ts, it.get("s")) for it in items if (ts := _state_ts(it)) is not None), key=lambda x: x[0]
            )
            out += [c for c in to_changes(eid, states) if start <= c.ts < end]
        return sorted(out, key=lambda c: c.ts)

    # live --------------------------------------------------------------------------------------
    async def subscribe(self, entity_ids: list[str]) -> AsyncIterator[tuple[str, float, object]]:
        """Yields (entity_id, ts, state) for every state update of the given entities."""
        if self._ws is None:
            raise HomeAssistantError("not connected")
        mid = next(self._ids)
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._subscriptions[mid] = queue
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send_json({"id": mid, "type": "subscribe_entities", "entity_ids": entity_ids})
        await asyncio.wait_for(fut, TIMEOUT_S)
        try:
            while (event := await queue.get()) is not None:
                for eid, st in (event.get("a") or {}).items():  # initial states
                    if (ts := _state_ts(st)) is not None:
                        yield eid, ts, st.get("s")
                for eid, diff in (event.get("c") or {}).items():  # changes
                    plus = diff.get("+") or {}
                    if "s" in plus and (ts := _state_ts(plus)) is not None:
                        yield eid, ts, plus["s"]
        finally:
            self._subscriptions.pop(mid, None)
