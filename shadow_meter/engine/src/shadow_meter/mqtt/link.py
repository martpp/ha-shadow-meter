"""A self-healing MQTT connection for the publisher.

Messages published while the broker is unreachable are dropped (states are re-sent every 30 s anyway);
after every (re)connect the `on_connect` callback announces all active devices again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiomqtt

_LOG = logging.getLogger(__name__)


class MqttLink:
    def __init__(self, host: str, port: int, username: str, password: str, status_topic: str) -> None:
        self._host, self._port = host, port
        self._username, self._password = username or None, password or None
        self._status_topic = status_topic
        self._client: aiomqtt.Client | None = None
        self.connected = False

    async def publish(self, topic: str, payload: str | bytes, qos: int = 0, retain: bool = False) -> None:
        if self._client is None:
            _LOG.debug("MQTT not connected, dropping %s", topic)
            return
        try:
            await self._client.publish(topic, payload, qos=qos, retain=retain)
        except aiomqtt.MqttError as err:
            _LOG.warning("MQTT publish failed: %s", err)

    async def run(self, on_connect: Callable[[], Awaitable[None]]) -> None:
        delay = 5.0
        while True:
            try:
                will = aiomqtt.Will(self._status_topic, "offline", qos=1, retain=True)
                async with aiomqtt.Client(
                    self._host,
                    self._port,
                    username=self._username,
                    password=self._password,
                    will=will,
                    identifier="shadow-meter",
                ) as c:
                    self._client, self.connected, delay = c, True, 5.0
                    _LOG.info("connected to MQTT broker %s:%s", self._host, self._port)
                    await on_connect()
                    async for _ in c.messages:  # no subscriptions; raises when the connection drops
                        pass
            except aiomqtt.MqttError as err:
                _LOG.warning("MQTT connection lost (%s), retrying in %.0f s", err, delay)
            finally:
                self._client, self.connected = None, False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300.0)
