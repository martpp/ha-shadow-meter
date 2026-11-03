"""Lifecycle of the analysis: start it once meters are set up, restart it when the setup changes.

The meter setup comes from the web UI (stored in the database) or, if none was saved there, from the
`phases` option. Without a setup the app still runs and shows the setup page. When the meters change,
everything learned from the old meters is discarded: detector state, confidence history, energy
counters, and pattern devices that were added to Home Assistant (their ids belong to the old analysis).
Decisions about entity-based devices (lights, ...) are kept - they do not depend on the meters.
The same reset happens when the user asks to analyse the history again (e.g. after linking an InfluxDB).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .analyzer import Analyzer
from .mqtt.discovery import Publisher
from .settings import Options, Setup
from .sources import HistorySource
from .sources.homeassistant import HomeAssistant, HomeAssistantError
from .sources.influx import InfluxError
from .store import Store

_LOG = logging.getLogger(__name__)
RETRY_MAX_S = 300.0

SETUP_STATUS = {
    "state": "setup",
    "message": "Choose your power meters in Settings",
    "backfill": None,
    "live_since": None,
}


class Engine:
    def __init__(
        self,
        options: Options,
        store: Store,
        ha: HomeAssistant,
        *,
        influx: HistorySource | None = None,
        publisher: Publisher | None = None,
        mqtt_connected: Callable[[], bool] | None = None,
        list_states: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> None:
        self.options = options
        self.store = store
        self.ha = ha
        self.influx = influx
        self.publisher = publisher
        self.mqtt_connected = mqtt_connected
        self._list_states = list_states
        self.analyzer: Analyzer | None = None
        self.setup: Setup | None = None
        self.source: str | None = None  # "ui" or "options"
        self._task: asyncio.Task[None] | None = None

    # setup ------------------------------------------------------------------------------------------
    async def load_setup(self) -> None:
        saved = await self.store.get_json("setup")
        if saved:
            self.setup, self.source = Setup.model_validate(saved), "ui"
        elif self.options.phases:
            self.setup, self.source = Setup(phases=self.options.phases), "options"
        else:
            self.setup, self.source = None, None

    async def start(self) -> None:
        await self.load_setup()
        if self.setup:
            await self._launch(self.setup)

    async def reconfigure(self, setup: Setup) -> bool:
        """Save a new setup from the UI and restart the analysis. Returns True if the old data was reset."""
        await self.stop()
        changed = await self.store.get_json("setup_fingerprint") not in (None, setup.fingerprint())
        await self.store.set_json("setup", setup.model_dump(mode="json"))
        self.setup, self.source = setup, "ui"
        await self._launch(setup)
        return changed

    async def reanalyse(self) -> None:
        """Start the analysis over with the current setup, e.g. after linking an InfluxDB.

        The analysis only moves forward in time, so older history can only be added by starting over.
        Same reset as for new meters: pattern devices get new ids, decisions about lights are kept.
        """
        if self.setup is None:
            raise RuntimeError("no meters set up")
        await self.stop()
        await self._reset("analysing the history again")
        await self._launch(self.setup)

    async def states(self) -> list[dict[str, Any]]:
        """All entity states of Home Assistant (for the setup page)."""
        if self._list_states is not None:
            return await self._list_states()
        return await self.ha.get_states()

    # analysis lifecycle ------------------------------------------------------------------------------
    async def _launch(self, setup: Setup) -> None:
        fingerprint = setup.fingerprint()
        old = await self.store.get_json("setup_fingerprint")
        if old is not None and old != fingerprint:
            await self._reset()
        await self.store.set_json("setup_fingerprint", fingerprint)
        self.analyzer = Analyzer(
            self.options,
            self.store,
            self.ha,
            phases=setup.phases,
            influx=self.influx,
            publisher=self.publisher,
            mqtt_connected=self.mqtt_connected,
        )
        self._task = asyncio.create_task(self._run(self.analyzer))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.analyzer:
            with contextlib.suppress(Exception):
                await self.store.set_json("detector", self.analyzer.detector.to_dict())
        await self.ha.close()

    async def _reset(self, reason: str = "meter setup changed") -> None:
        _LOG.info("%s: starting the analysis over", reason)
        await self.store.delete_json("detector", "energy")
        await self.store.clear_confidence()
        for device_id, decision in (await self.store.devices()).items():
            if device_id.startswith("cluster:"):
                if decision.get("status") == "active" and self.publisher:
                    await self.publisher.remove(device_id)
                await self.store.delete_device(device_id)

    async def _run(self, analyzer: Analyzer) -> None:
        delay = 5.0
        while True:
            try:
                await self.ha.connect()
                await analyzer.setup()
                await analyzer.backfill()
                delay = 5.0
                await analyzer.live()
            except asyncio.CancelledError:
                raise
            except (
                HomeAssistantError,
                InfluxError,
                aiohttp.ClientError,
                OSError,
                TimeoutError,
                ExceptionGroup,
            ) as err:
                _LOG.warning("analysis interrupted: %s - retrying in %.0f s", err, delay)
                analyzer.set_status(
                    state="error", message=f"Connection problem, retrying: {type(err).__name__}"
                )
            await self.ha.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_S)

    # helpers for the web UI and MQTT ----------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return self.analyzer.status if self.analyzer else dict(SETUP_STATUS)

    def mqtt_state(self) -> str:
        if self.analyzer:
            return self.analyzer.mqtt_state()
        return "disabled" if self.publisher is None else "connected"

    async def republish(self) -> None:
        if self.analyzer:
            await self.analyzer.republish()
