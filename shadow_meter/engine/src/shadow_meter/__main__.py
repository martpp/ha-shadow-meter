"""Entry point: `python -m shadow_meter` (in the app container or standalone)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

import aiohttp
from aiohttp import web
from pydantic import ValidationError

from . import __version__
from .analyzer import Analyzer
from .mqtt.discovery import Publisher
from .mqtt.link import MqttLink
from .settings import Options, Runtime, load_options, load_runtime
from .sources.homeassistant import HomeAssistant, HomeAssistantError
from .sources.influx import Influx, InfluxError
from .store import Store
from .web.server import build_app

_LOG = logging.getLogger("shadow_meter")


def _mqtt(options: Options) -> MqttLink | None:
    """MQTT from the options, else from the Supervisor's MQTT service (passed in by the run script)."""
    o = options.mqtt
    host = o.host or os.environ.get("MQTT_HOST", "")
    if not host:
        return None
    if o.host:
        port, user, pw = o.port, o.username, o.password.get_secret_value()
    else:
        port = int(os.environ.get("MQTT_PORT", "1883"))
        user, pw = os.environ.get("MQTT_USER", ""), os.environ.get("MQTT_PASSWORD", "")
    return MqttLink(host, port, user, pw, f"{o.topic_prefix}/status")


async def _analysis(analyzer: Analyzer, ha: HomeAssistant) -> None:
    delay = 5.0
    while True:
        try:
            await ha.connect()
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
            analyzer.set_status(state="error", message=f"Connection problem, retrying: {type(err).__name__}")
        await ha.close()
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300.0)


async def run(options: Options, runtime: Runtime) -> None:
    store = Store(runtime.data_dir / "shadow_meter.db")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    async with aiohttp.ClientSession() as session:
        ha = HomeAssistant(session, runtime.ha_url, runtime.ha_token.get_secret_value())
        influx = None
        if options.influx.enabled:
            o = options.influx
            influx = Influx(
                session,
                o.url,
                o.database,
                username=o.username,
                password=o.password.get_secret_value(),
                power_measurement=o.power_measurement,
                legacy_state_measurement=o.legacy_state_measurement,
                verify_ssl=o.verify_ssl,
            )
        link = _mqtt(options)
        publisher = (
            Publisher(link, options.mqtt.discovery_prefix, options.mqtt.topic_prefix) if link else None
        )
        analyzer = Analyzer(
            options,
            store,
            ha,
            influx=influx,
            publisher=publisher,
            mqtt_connected=(lambda: link.connected) if link else None,
        )
        if not link:
            _LOG.warning("no MQTT broker configured: devices cannot be added to Home Assistant")
        runner = web.AppRunner(build_app(analyzer, runtime), access_log=None)  # no URLs (tokens) in logs
        await runner.setup()
        await web.TCPSite(runner, runtime.listen_host, runtime.listen_port).start()
        _LOG.info(
            "Shadow Meter %s (%s mode) listening on %s:%s",
            __version__,
            runtime.mode,
            runtime.listen_host,
            runtime.listen_port,
        )
        tasks = [asyncio.create_task(_analysis(analyzer, ha))]
        if link:
            tasks.append(asyncio.create_task(link.run(analyzer.republish)))
        await stop.wait()
        _LOG.info("stopping")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await store.set_json("detector", analyzer.detector.to_dict())
        await ha.close()
        await runner.cleanup()
    store.close()


def read_options() -> Options:
    """Load the options or exit with a readable message (never echoing values: they may be secrets)."""
    try:
        return load_options()
    except FileNotFoundError as err:
        raise SystemExit(f"options file not found: {err.filename} (see docker-compose.example.yml)") from err
    except ValidationError as err:
        problems = "\n".join(f"  {'.'.join(map(str, e['loc']))}: {e['msg']}" for e in err.errors())
        raise SystemExit(f"invalid options:\n{problems}") from err
    except ValueError as err:  # broken JSON
        raise SystemExit(f"options file is not valid JSON: {err}") from err


def main() -> None:
    runtime = load_runtime()
    options = read_options()
    logging.basicConfig(
        level=options.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    if runtime.mode == "standalone" and not runtime.ha_token.get_secret_value():
        raise SystemExit("HA_TOKEN (a Home Assistant long-lived access token) is required in standalone mode")
    if (
        runtime.mode == "standalone"
        and runtime.listen_host not in ("127.0.0.1", "::1", "localhost")
        and not runtime.api_token.get_secret_value()
    ):
        _LOG.warning(
            "listening on %s without SHADOW_METER_API_TOKEN: all API requests will be refused",
            runtime.listen_host,
        )
    asyncio.run(run(options, runtime))


if __name__ == "__main__":
    main()
