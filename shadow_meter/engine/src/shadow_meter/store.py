"""Persistent state in a small SQLite database (in the app's /data folder).

- meta: JSON blobs (detector state, energy counters, schema version)
- devices: the user's decisions per candidate (status, name, type)
- confidence: a short confidence history per candidate, for the trend shown in the UI

All queries use bind parameters. SQLite calls run on one dedicated worker thread (see Store).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, TypeVar

SCHEMA_VERSION = 1
T = TypeVar("T")
HISTORY_POINTS = 60

Status = Literal["new", "confirmed", "rejected", "active"]
STATUSES: tuple[Status, ...] = ("new", "confirmed", "rejected", "active")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('new', 'confirmed', 'rejected', 'active')),
    name TEXT,
    type TEXT,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS confidence (id TEXT NOT NULL, ts REAL NOT NULL, value REAL NOT NULL);
CREATE INDEX IF NOT EXISTS confidence_id ON confidence (id, ts);
"""


class Store:
    """All database work runs on one dedicated thread: calls are serialised, the event loop never blocks,
    and a cancelled caller can never leave a half-finished statement racing with the next one."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow-meter-db")
        self._db: sqlite3.Connection | None = None
        self._pool.submit(self._open, path).result()

    def _open(self, path: Path) -> None:
        db = sqlite3.connect(path, isolation_level=None)  # used only from the worker thread
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(_SCHEMA)
        row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        elif int(row[0]) > SCHEMA_VERSION:
            db.close()
            raise RuntimeError("database was written by a newer version of Shadow Meter")
        self._db = db

    def close(self) -> None:
        """Wait for pending work, then close the database."""

        def _close() -> None:
            if self._db is not None:
                self._db.close()
                self._db = None

        self._pool.submit(_close).result()
        self._pool.shutdown(wait=True)

    async def _call(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def job() -> T:
            if self._db is None:
                raise RuntimeError("database is closed")
            return fn(self._db)

        return await asyncio.get_running_loop().run_in_executor(self._pool, job)

    async def _run(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return await self._call(lambda db: db.execute(sql, params).fetchall())

    # meta -----------------------------------------------------------------------------------------
    async def get_json(self, key: str, default: Any = None) -> Any:
        rows = await self._run("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(rows[0][0]) if rows else default

    async def set_json(self, key: str, value: Any) -> None:
        await self._run("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))

    # user decisions -------------------------------------------------------------------------------
    async def devices(self) -> dict[str, dict[str, Any]]:
        rows = await self._run("SELECT id, status, name, type FROM devices")
        return {r[0]: {"status": r[1], "name": r[2], "type": r[3]} for r in rows}

    async def update_device(
        self, device_id: str, status: Status | None = None, name: str | None = None, type_: str | None = None
    ) -> dict[str, Any]:
        cur = (await self.devices()).get(device_id, {"status": "new", "name": None, "type": None})
        new = {
            "status": status or cur["status"],
            "name": cur["name"] if name is None else (name or None),
            "type": cur["type"] if type_ is None else (type_ or None),
        }
        await self._run(
            "INSERT OR REPLACE INTO devices VALUES (?, ?, ?, ?, ?)",
            (device_id, new["status"], new["name"], new["type"], time.time()),
        )
        return new

    # confidence history ---------------------------------------------------------------------------
    async def add_confidence(self, points: list[tuple[str, float, float]]) -> None:
        if not points:
            return

        def work(db: sqlite3.Connection) -> None:
            db.executemany("INSERT INTO confidence VALUES (?, ?, ?)", points)
            db.execute(
                "DELETE FROM confidence WHERE rowid IN (SELECT rowid FROM (SELECT rowid, ROW_NUMBER() "
                "OVER (PARTITION BY id ORDER BY ts DESC) AS n FROM confidence) WHERE n > ?)",
                (HISTORY_POINTS,),
            )

        await self._call(work)

    async def confidence_history(self) -> dict[str, list[tuple[float, float]]]:
        rows = await self._run("SELECT id, ts, value FROM confidence ORDER BY ts")
        out: dict[str, list[tuple[float, float]]] = {}
        for i, ts, v in rows:
            out.setdefault(i, []).append((ts, v))
        return out
