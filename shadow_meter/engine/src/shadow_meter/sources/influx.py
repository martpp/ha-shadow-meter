"""Read-only InfluxDB 1.x client for long histories written by the Home Assistant InfluxDB integration.

Power sensors: measurement = unit (default "W"), tag entity_id = object id without "sensor.".
On/off states: current schema uses one measurement per entity ("light.kitchen"); older Home Assistant
versions wrote everything into one measurement ("state") with tags domain + entity_id. Both are read.

InfluxQL has no bind parameters. Everything that goes into a query is therefore checked against a strict
pattern first (the settings already validate it too): names inside double quotes may only contain
letters, digits, "_", "." and "-"; tag values inside single quotes only letters, digits and "_". Quotes,
backslashes, spaces and semicolons can never get in. Timestamps are generated, not taken from user input.
Only SELECT/SHOW queries are sent.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import aiohttp

from ..models import Sample, StateChange
from . import parse_power, to_changes

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}$")  # inside "double quotes"
_TAG = re.compile(r"^[A-Za-z0-9_]{1,128}$")  # inside 'single quotes'
# measurements are named after the unit ("W", "%"); also inside "double quotes", where % is harmless
_MEASUREMENT = re.compile(r"^[A-Za-z0-9_.%\-]{1,128}$")
_ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
MAX_POINTS = 2_000_000  # per query; a day of a 1 s sensor is 86 400


class InfluxError(Exception):
    pass


def ident(v: str) -> str:
    """A name used inside double quotes (measurement, database). Raises if it is not safe."""
    if not _NAME.fullmatch(v):
        raise InfluxError(f"unsafe name: {v!r}")
    return v


def measurement(v: str) -> str:
    """A measurement name used inside double quotes. Raises if it is not safe."""
    if not _MEASUREMENT.fullmatch(v):
        raise InfluxError(f"unsafe measurement name: {v!r}")
    return v


def tag(v: str) -> str:
    """A tag value used inside single quotes (entity object id, domain). Raises if it is not safe."""
    if not _TAG.fullmatch(v):
        raise InfluxError(f"unsafe tag value: {v!r}")
    return v


def entity(v: str) -> tuple[str, str]:
    """Split a Home Assistant entity id into (domain, object id), both safe tag values."""
    if not _ENTITY.fullmatch(v):
        raise InfluxError(f"unsafe entity id: {v!r}")
    domain, name = v.split(".", 1)
    return tag(domain), tag(name)


def _t(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Influx:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        database: str,
        *,
        username: str = "",
        password: str = "",
        power_measurement: str = "W",
        legacy_state_measurement: str = "state",
        power_factor_measurement: str = "%",
        verify_ssl: bool = True,
    ) -> None:
        self._session = session
        self._url = url.rstrip("/") + "/query"
        self._db = ident(database)
        self._auth = aiohttp.BasicAuth(username, password) if username else None
        self._power = measurement(power_measurement)
        # power factor sensors (optional, for the reactive power) are read together with the power
        names = [self._power, *([measurement(power_factor_measurement)] if power_factor_measurement else [])]
        self._sources = ", ".join(f'"{m}"' for m in dict.fromkeys(names))
        self._legacy = measurement(legacy_state_measurement) if legacy_state_measurement else ""
        self._ssl = verify_ssl

    async def query(self, q: str) -> list[dict[str, Any]]:
        """Run one SELECT/SHOW query; returns the series of the first statement."""
        if not q.lstrip().upper().startswith(("SELECT ", "SHOW ")):
            raise InfluxError("only SELECT/SHOW queries are allowed")
        params = {"db": self._db, "q": q, "epoch": "ms"}
        async with self._session.get(
            self._url, params=params, auth=self._auth, ssl=self._ssl, timeout=aiohttp.ClientTimeout(total=300)
        ) as r:
            if r.status != 200:
                raise InfluxError(f"InfluxDB returned HTTP {r.status}")
            data = await r.json()
        res = (data.get("results") or [{}])[0]
        if "error" in res:
            raise InfluxError(str(res["error"]))
        series: list[dict[str, Any]] = res.get("series") or []
        return series

    @staticmethod
    def _rows(series: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Flatten series into rows; each row also gets the series name ("_name") and its tags."""
        rows: list[dict[str, Any]] = []
        for s in series:
            cols = s.get("columns", [])
            extra = {"_name": s.get("name"), **(s.get("tags") or {})}
            rows += [{**extra, **dict(zip(cols, v, strict=False))} for v in s.get("values", [])]
        return rows

    async def ping(self) -> None:
        await self.query("SHOW MEASUREMENTS LIMIT 1")

    async def power(self, entity_ids: list[str], start: float, end: float) -> dict[str, list[Sample]]:
        """All sensors in two queries: the last value before start, and the values in [start, end)."""
        if not entity_ids:
            return {}
        by_object = {entity(e)[1]: e for e in entity_ids}
        where = f"entity_id =~ /^({'|'.join(sorted(by_object))})$/"
        before = await self.query(
            f'SELECT last("value") AS "value" FROM {self._sources} WHERE {where} '
            f"AND time < '{_t(start)}' AND time > '{_t(start - 2 * 86400)}' GROUP BY \"entity_id\""
        )
        main = await self.query(
            f'SELECT "value" FROM {self._sources} WHERE {where} '
            f"AND time >= '{_t(start)}' AND time < '{_t(end)}' GROUP BY \"entity_id\" LIMIT {MAX_POINTS}"
        )
        out: dict[str, list[Sample]] = {e: [] for e in entity_ids}
        for r in self._rows(before) + self._rows(main):
            eid = by_object.get(str(r.get("entity_id")))
            if eid is not None:
                out[eid].append(Sample(r["time"] / 1000, parse_power(r.get("value"))))
        for v in out.values():
            v.sort(key=lambda s: s.ts)
        return out

    async def changes(self, entity_ids: list[str], start: float, end: float) -> list[StateChange]:
        """On/off changes of all entities in two queries (current and legacy schema)."""
        if not entity_ids:
            return []
        parts = {e: entity(e) for e in entity_ids}
        rng = f"time >= '{_t(start - 86400)}' AND time < '{_t(end)}'"  # a day before: previous state
        names = "|".join(f"{d}\\.{n}" for d, n in sorted(parts.values()))
        rows = self._rows(await self.query(f'SELECT "state", "value" FROM /^({names})$/ WHERE {rng}'))
        states: dict[str, list[tuple[float, object]]] = {e: [] for e in entity_ids}
        for r in rows:
            if r.get("_name") in states:
                states[r["_name"]].append((r["time"] / 1000, _state(r)))
        if self._legacy:
            domains = "|".join(sorted({d for d, _ in parts.values()}))
            objects = "|".join(sorted({n for _, n in parts.values()}))
            legacy = await self.query(
                f'SELECT "state", "value" FROM "{self._legacy}" WHERE domain =~ /^({domains})$/ '
                f'AND entity_id =~ /^({objects})$/ AND {rng} GROUP BY "domain", "entity_id"'
            )
            for r in self._rows(legacy):
                eid = f"{r.get('domain')}.{r.get('entity_id')}"
                if eid in states:
                    states[eid].append((r["time"] / 1000, _state(r)))
        out: list[StateChange] = []
        for eid, st in states.items():
            st.sort(key=lambda x: x[0])
            out += [c for c in to_changes(eid, st) if start <= c.ts < end]
        return sorted(out, key=lambda c: c.ts)


def _state(row: dict[str, Any]) -> object:
    return row.get("state") if row.get("state") is not None else row.get("value")
