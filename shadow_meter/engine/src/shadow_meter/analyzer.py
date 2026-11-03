"""Orchestration: analyse the history, then follow the live data; keep the UI and Home Assistant updated.

1. setup:    time zone and context entities (lights, ...) from Home Assistant, saved state from the store
2. backfill: history day by day (InfluxDB if configured, else the Home Assistant recorder); the state is
             saved after every day, so a restart continues where it stopped
3. live:     subscribe to the sensors, process every few seconds, publish activated devices via MQTT

Progress and candidate updates are pushed to listeners (the web UI) as small events.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .detection.classify import DEVICE_TYPES
from .detection.pipeline import Detector, PhaseConfig
from .models import Sample, StateChange
from .mqtt.discovery import Publisher
from .settings import Options, PhaseOptions
from .sources import HistorySource, parse_on, parse_power
from .sources.homeassistant import HomeAssistant, HomeAssistantError
from .store import Status, Store

_LOG = logging.getLogger(__name__)

DAY = 86400.0
LIVE_TICK_S = 10.0
LIVE_LAG_S = 15.0  # late sensor reports are still accepted
REFRESH_S = 60.0
HISTORY_EVERY_S = 900.0
PUBLISH_EVERY_S = 30.0
MATCH_TOLERANCE = 0.35


class PublishUnavailable(Exception):
    """A device cannot be added to Home Assistant because no MQTT broker is configured."""


class Analyzer:
    def __init__(
        self,
        options: Options,
        store: Store,
        ha: HomeAssistant,
        *,
        phases: Sequence[PhaseOptions] | None = None,
        influx: HistorySource | None = None,
        publisher: Publisher | None = None,
        mqtt_connected: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.options = options
        self.store = store
        self.ha = ha
        self.influx = influx
        self.publisher = publisher
        self._mqtt_connected = mqtt_connected
        self.clock = clock
        self.tz: tzinfo = UTC
        chosen = options.phases if phases is None else phases
        self.phase_configs = [
            PhaseConfig(p.phase, p.mains, tuple(p.submeters), p.power_factor) for p in chosen
        ]
        self.detector = Detector(self.phase_configs, min_step_w=options.min_step_w)
        self.context: list[str] = []
        self.names: dict[str, str] = {}
        self.status: dict[str, Any] = {
            "state": "starting",
            "message": "",
            "backfill": None,
            "live_since": None,
        }
        self._listeners: set[asyncio.Queue[dict[str, Any]]] = set()
        self._candidates: list[dict[str, Any]] = []
        self._decisions: dict[str, dict[str, Any]] = {}
        self._history: dict[str, list[tuple[float, float]]] = {}
        self._energy: dict[str, float] = {}
        self._entity_on: dict[str, bool] = {}
        self._running: dict[str, tuple[bool, float]] = {}
        self._loaded = False

    # events for the UI ------------------------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._listeners.discard(q)

    def _emit(self, kind: str, data: Any) -> None:
        for q in list(self._listeners):
            with contextlib.suppress(asyncio.QueueFull):  # a slow client misses updates, it reloads anyway
                q.put_nowait({"kind": kind, "data": data})

    def set_status(self, **kw: Any) -> None:
        self.status.update(
            kw, updated=self.clock(), steps=self.detector.step_count, cycles=self.detector.cycle_count
        )
        self._emit("status", self.status)

    # setup ------------------------------------------------------------------------------------------
    async def setup(self) -> None:
        cfg = await self.ha.get_config()
        try:
            self.tz = ZoneInfo(str(cfg.get("time_zone") or "UTC"))
        except ZoneInfoNotFoundError:
            self.tz = UTC
        self.detector.clusters.tz = self.tz
        states = await self.ha.get_states()
        self.names = {
            s["entity_id"]: str(s.get("attributes", {}).get("friendly_name") or s["entity_id"])
            for s in states
            if "entity_id" in s
        }
        power_entities = set(self.detector.entities)
        wanted = set(self.options.context_entities) | {
            e for e in self.names if e.split(".", 1)[0] in self.options.context_domains
        }
        self.context = sorted(wanted - set(self.options.exclude_entities) - power_entities)
        missing = [e for e in power_entities if e not in self.names]
        if missing:
            _LOG.warning("power sensors not found in Home Assistant: %s", ", ".join(sorted(missing)))
        if not self._loaded:  # saved state only once; after a reconnect the in-memory state is newer
            saved = await self.store.get_json("detector")
            if saved:
                self.detector.load(saved)
            self._energy = await self.store.get_json("energy", {}) or {}
            self._decisions = await self.store.devices()
            self._history = await self.store.confidence_history()
            self._loaded = True
        _LOG.info("watching %d power sensors and %d context entities", len(power_entities), len(self.context))
        await self.refresh(record=False)

    # backfill ---------------------------------------------------------------------------------------
    async def backfill(self) -> None:
        """Analyse the long history, then catch up with the Home Assistant recorder until (almost) now."""
        days = self.options.history_days_max if self.influx else self.options.history_days
        first = self.clock() - days * DAY
        start_date = self.options.influx.history_start
        if self.influx and start_date:
            first = max(first, datetime.combine(start_date, datetime.min.time(), self.tz).timestamp())
        if self.influx:
            await self._backfill(self.influx, "InfluxDB", first)
        # the long analysis may take hours: fill the time since then from the recorder
        while self.clock() - self.detector.processed_until > 2 * LIVE_TICK_S + LIVE_LAG_S:
            await self._backfill(self.ha, "Home Assistant history", first)

    async def _backfill(self, source: HistorySource, name: str, first: float) -> None:
        end = math.floor((self.clock() - LIVE_LAG_S) / 5) * 5
        t = max(first, self.detector.processed_until)
        while t < end:
            e = min(t + DAY, end)
            self.set_status(
                state="backfill",
                message=f"Analysing {name}",
                backfill={
                    "from": first,
                    "to": end,
                    "current": t,
                    "percent": round(100 * (t - first) / max(1.0, end - first), 1),
                },
            )
            power = await source.power(self.detector.entities, t, e)
            changes = await source.changes(self.context, t, e) if self.context else []
            self.detector.process(power, changes, t, e)
            self.detector.maintain(keep=self._decisions)
            await self.store.set_json("detector", self.detector.to_dict())
            await self.refresh(record=True, ts=e)
            t = e
        self.set_status(backfill={"from": first, "to": end, "current": end, "percent": 100.0})

    # live -------------------------------------------------------------------------------------------
    async def live(self) -> None:
        entities = [*self.detector.entities, *self.context]
        buffer: dict[str, list[Sample]] = {e: [] for e in self.detector.entities}
        changes: list[StateChange] = []
        self.set_status(state="live", message="Following live data", live_since=self.clock())

        async def consume() -> None:
            async for eid, ts, state in self.ha.subscribe(entities):
                if eid in buffer:
                    buffer[eid].append(Sample(ts, parse_power(state)))
                elif (on := parse_on(state)) is not None:
                    if eid in self._entity_on and self._entity_on[eid] != on:
                        changes.append(StateChange(ts, eid, on))
                    self._entity_on[eid] = on
            raise HomeAssistantError("subscription ended")

        async def tick() -> None:
            last_refresh = last_history = last_publish = self.clock()
            while True:
                await asyncio.sleep(LIVE_TICK_S)
                now = self.clock()
                end = math.floor((now - LIVE_LAG_S) / 5) * 5
                chunk = {e: sorted(v, key=lambda s: s.ts) for e, v in buffer.items()}
                for v in buffer.values():
                    v.clear()
                batch = sorted(changes, key=lambda c: c.ts)
                changes.clear()
                for e, v in chunk.items():  # keep samples newer than `end` for the next tick
                    buffer[e].extend(s for s in v if s.ts >= end)
                changes.extend(c for c in batch if c.ts >= end)
                self.detector.process(
                    chunk, [c for c in batch if c.ts < end], self.detector.processed_until, end
                )
                await self._update_running(now)
                if now - last_publish >= PUBLISH_EVERY_S:
                    last_publish = now
                    await self._publish_states()
                if now - last_refresh >= REFRESH_S:
                    last_refresh = now
                    self.detector.maintain(keep=self._decisions)
                    record = now - last_history >= HISTORY_EVERY_S
                    if record:
                        last_history = now
                    await self.refresh(record=record, ts=now)
                    await self.store.set_json("detector", self.detector.to_dict())
                    await self.store.set_json("energy", self._energy)
                    self.set_status()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(consume())
            tg.create_task(tick())

    async def _update_running(self, now: float) -> None:
        """Estimate on/off and power of activated devices and integrate their energy."""
        open_steps = self.detector.running()
        for c in self._candidates:
            if c["status"] != "active":
                continue
            if c["source"] == "entity":
                on = bool(self._entity_on.get(c["entity_id"] or ""))
            else:
                on_w = c["features"].get("on_w", c["power_w"])
                on = any(
                    abs(w - on_w) <= MATCH_TOLERANCE * max(w, on_w) for _, w in open_steps.get(c["phase"], [])
                )
            power = float(c["power_w"]) if on and math.isfinite(c["power_w"]) else 0.0
            prev_on, prev_ts = self._running.get(c["id"], (False, now))
            prev_power = float(c["power_w"]) if prev_on and math.isfinite(c["power_w"]) else 0.0
            self._energy[c["id"]] = (
                self._energy.get(c["id"], 0.0) + prev_power * (now - prev_ts) / 3600 / 1000
            )
            self._running[c["id"]] = (on, now)
            c["running"], c["live_power_w"] = on, power

    async def _publish_states(self) -> None:
        if not self.publisher:
            return
        for c in self._candidates:
            if c["status"] == "active":
                await self.publisher.state(
                    c["id"],
                    bool(c.get("running")),
                    float(c.get("live_power_w", 0.0)),
                    self._energy.get(c["id"], 0.0),
                )

    async def republish(self) -> None:
        """After an MQTT (re)connect: announce all active devices again."""
        if not self.publisher:
            return
        await self.publisher.online()
        for c in self._candidates:
            if c["status"] == "active":
                await self.publisher.announce(c["id"], c["name"])
        await self._publish_states()

    # candidates and user decisions ------------------------------------------------------------------
    async def refresh(self, record: bool = False, ts: float | None = None) -> None:
        cands = self.detector.candidates()
        if record:
            when = ts if ts is not None else self.clock()
            points = [(c["id"], when, float(c["confidence"])) for c in cands]
            await self.store.add_confidence(points)
            for i, _, v in points:
                self._history.setdefault(i, []).append((when, v))
                del self._history[i][:-60]
        known = {c["id"] for c in self._candidates}
        self._candidates = [self._view(c) for c in cands]
        new = [c for c in self._candidates if c["id"] not in known]
        if new and known:
            self._emit("new", [{"id": c["id"], "name": c["name"]} for c in new])
        self._emit("candidates", self._candidates)

    def _view(self, c: dict[str, Any]) -> dict[str, Any]:
        d = self._decisions.get(c["id"], {})
        suggested = self._suggest_name(c)
        prev = next((p for p in self._candidates if p["id"] == c["id"]), {})
        return {
            **c,
            "detected_type": c["type"],
            "type": d.get("type") or c["type"],
            "status": d.get("status", "new"),
            "name": d.get("name") or suggested,
            "suggested_name": suggested,
            "history": self._history.get(c["id"], [])[-30:],
            "energy_kwh": self._energy.get(c["id"], 0.0),
            "running": prev.get("running", False),
            "live_power_w": prev.get("live_power_w", 0.0),
        }

    def _suggest_name(self, c: dict[str, Any]) -> str:
        if c["source"] == "entity" and c["entity_id"]:
            return str(self.names.get(c["entity_id"], c["entity_id"]))
        label = DEVICE_TYPES.get(c["type"], "Unknown device").split(" (")[0].split(" /")[0]
        p = float(c["power_w"])
        power = f"{p / 1000:.1f} kW" if p >= 1000 else f"{p:.0f} W"
        return f"{label} ~{power} (phase {c['phase']})"

    def candidates(self) -> list[dict[str, Any]]:
        return self._candidates

    def mqtt_state(self) -> str:
        """'disabled' (no broker configured), 'connected' or 'disconnected'."""
        if self.publisher is None:
            return "disabled"
        if self._mqtt_connected is None or self._mqtt_connected():
            return "connected"
        return "disconnected"

    async def update_device(
        self, device_id: str, status: Status | None = None, name: str | None = None, type_: str | None = None
    ) -> dict[str, Any]:
        cand = next((c for c in self._candidates if c["id"] == device_id), None)
        if cand is None:
            raise KeyError(device_id)
        if status == "active" and self.publisher is None:
            raise PublishUnavailable("no MQTT broker configured")
        was_active = cand["status"] == "active"
        self._decisions[device_id] = await self.store.update_device(device_id, status, name, type_)
        updated = self._view(
            {k: v for k, v in cand.items() if k in _DETECTOR_KEYS} | {"type": cand["detected_type"]}
        )
        self._candidates = [updated if c["id"] == device_id else c for c in self._candidates]
        if self.publisher:
            if updated["status"] == "active":
                await self.publisher.announce(device_id, updated["name"])
            elif was_active:
                await self.publisher.remove(device_id)
        self._emit("candidates", self._candidates)
        return updated


_DETECTOR_KEYS = {
    "id",
    "source",
    "phase",
    "type",
    "probabilities",
    "confidence",
    "power_w",
    "features",
    "first_seen",
    "last_seen",
    "entity_id",
    "explained_by",
}
