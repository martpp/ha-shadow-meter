"""Publish activated devices to Home Assistant via MQTT discovery.

Each activated device becomes a Home Assistant device with three entities: on/off, power and energy
(total_increasing, usable in the Energy dashboard). Deactivating a device removes it again (empty
retained discovery message).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

_LOG = logging.getLogger(__name__)


def slug(device_id: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", device_id.lower())[:60]


class Client(Protocol):
    async def publish(self, topic: str, payload: str | bytes, qos: int = 0, retain: bool = False) -> None: ...


class Publisher:
    def __init__(self, client: Client, discovery_prefix: str, topic_prefix: str) -> None:
        self._c = client
        self._disc = discovery_prefix
        self._prefix = topic_prefix

    def _uid(self, device_id: str, obj: str) -> str:
        return f"{self._prefix}_{slug(device_id)}_{obj}"

    async def online(self) -> None:
        await self._c.publish(f"{self._prefix}/status", "online", qos=1, retain=True)

    async def announce(self, device_id: str, name: str) -> None:
        base_topic = f"{self._prefix}/{slug(device_id)}"
        device = {
            "identifiers": [f"{self._prefix}_{slug(device_id)}"],
            "name": name,
            "manufacturer": "Shadow Meter",
            "model": "estimated from the whole-home power",
        }
        common = {
            "device": device,
            "state_topic": f"{base_topic}/state",
            "availability_topic": f"{self._prefix}/status",
        }
        configs: dict[tuple[str, str], dict[str, Any]] = {
            ("binary_sensor", "on"): {
                "name": "Running",
                "device_class": "running",
                "value_template": "{{ value_json.on }}",
            },
            ("sensor", "power"): {
                "name": "Power",
                "device_class": "power",
                "unit_of_measurement": "W",
                "state_class": "measurement",
                "value_template": "{{ value_json.power }}",
            },
            ("sensor", "energy"): {
                "name": "Energy",
                "device_class": "energy",
                "unit_of_measurement": "kWh",
                "state_class": "total_increasing",
                "value_template": "{{ value_json.energy }}",
            },
        }
        for (component, obj), cfg in configs.items():
            uid = self._uid(device_id, obj)
            payload = {**common, **cfg, "unique_id": uid, "default_entity_id": f"{component}.{uid}"}
            await self._c.publish(
                f"{self._disc}/{component}/{uid}/config", json.dumps(payload), qos=1, retain=True
            )
        _LOG.info("announced %s as %r", device_id, name)

    async def remove(self, device_id: str) -> None:
        for component, obj in (("binary_sensor", "on"), ("sensor", "power"), ("sensor", "energy")):
            await self._c.publish(
                f"{self._disc}/{component}/{self._uid(device_id, obj)}/config", b"", qos=1, retain=True
            )
        await self._c.publish(f"{self._prefix}/{slug(device_id)}/state", b"", qos=1, retain=True)

    async def state(self, device_id: str, on: bool, power_w: float, energy_kwh: float) -> None:
        msg = {"on": "ON" if on else "OFF", "power": round(power_w, 1), "energy": round(energy_kwh, 4)}
        await self._c.publish(f"{self._prefix}/{slug(device_id)}/state", json.dumps(msg), retain=True)
