# src/aiogrilla/mqtt.py
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from awscrt import auth, mqtt  # type: ignore[import-untyped]
from awsiot import mqtt_connection_builder  # type: ignore[import-untyped]

from . import const
from .auth import Credentials
from .models import GrillState, parse_grill_state

_LOGGER = logging.getLogger(__name__)
StateCb = Callable[[GrillState], None]
AvailCb = Callable[[bool], None]
ConnectionFactory = Callable[..., "mqtt.Connection"]


def _default_connection_factory(
    *,
    creds: Credentials,
    client_id: str,
    on_interrupted: Callable[..., None],
    on_resumed: Callable[..., None],
) -> mqtt.Connection:
    """Build the real awscrt MQTT-over-WSS connection. Owned seam so IotStream's
    connect/subscribe/availability logic can be tested without a live awscrt connection."""
    provider = auth.AwsCredentialsProvider.new_static(
        access_key_id=creds.access_key,
        secret_access_key=creds.secret_key,
        session_token=creds.session_token,
    )
    return mqtt_connection_builder.websockets_with_default_aws_signing(
        endpoint=const.IOT_ENDPOINT,
        region=const.REGION,
        credentials_provider=provider,
        client_id=client_id,
        clean_session=True,
        keep_alive_secs=30,
        # The vendor's IoT policy pins the client id, which we therefore share with the
        # vendor app (one connection per id broker-side). Generous reconnect backoff
        # keeps the mutual-kick contention bounded when both are online.
        reconnect_min_timeout_secs=30,
        reconnect_max_timeout_secs=300,
        on_connection_interrupted=on_interrupted,
        on_connection_resumed=on_resumed,
    )


class StateDispatcher:
    """Bridges awscrt native-thread messages onto the asyncio loop, parses, and runs a
    staleness watchdog that marks the grill UNAVAILABLE (without synthesizing a mode)
    when it goes silent while connected."""

    def __init__(
        self,
        grill_id: str,
        *,
        on_state: StateCb,
        on_availability: AvailCb,
        loop: asyncio.AbstractEventLoop,
        staleness: float = const.STALENESS_SECONDS,
    ) -> None:
        self._id = grill_id
        self._on_state = on_state
        self._on_availability = on_availability
        self._loop = loop
        self._staleness = staleness
        self._closed = False
        self._available = False  # combined availability (connection + telemetry freshness)
        self._watchdog: asyncio.TimerHandle | None = None
        self._last: GrillState | None = None

    # called from the awscrt NATIVE thread
    def handle_message_threadsafe(self, payload: bytes) -> None:
        # Guard against a loop that is shutting down: call_soon_threadsafe raises
        # RuntimeError on a closed loop. close() already guarantees no dispatch,
        # so dropping here is safe.
        try:
            self._loop.call_soon_threadsafe(self._on_loop, payload)
        except RuntimeError:
            pass

    def _on_loop(self, payload: bytes) -> None:
        if self._closed:
            return
        try:
            data = json.loads(payload)
            state = parse_grill_state(data)
        except Exception:  # total safety net; parser is total, json may not be
            _LOGGER.warning("dropping unparseable grill_state for %s", self._id, exc_info=True)
            return
        self._last = state
        self.set_available(True)  # fresh telemetry -> available (idempotent)
        self._arm_watchdog()
        self._on_state(state)

    def _arm_watchdog(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
        self._watchdog = self._loop.call_later(self._staleness, self._on_silent)

    def _on_silent(self) -> None:
        if self._closed:
            return
        # Long silence while connected: the grill has likely powered off. Mark UNAVAILABLE.
        # Do NOT synthesize a mode -- the grill reports its own state (incl. "standby")
        # whenever it is publishing; inventing "off" from silence flaps falsely.
        self.set_available(False)

    def set_callbacks(
        self,
        *,
        on_state: StateCb | None = None,
        on_availability: AvailCb | None = None,
    ) -> None:
        """Update callbacks on the live dispatcher without tearing down the connection."""
        if on_state is not None:
            self._on_state = on_state
        if on_availability is not None:
            self._on_availability = on_availability

    def set_available(self, available: bool) -> None:
        if self._closed or available == self._available:
            return
        self._available = available
        if available:
            self._arm_watchdog()  # expect a message within staleness, else go unavailable
        self._on_availability(available)

    def close(self) -> None:
        self._closed = True
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None


class IotStream:
    """One AWS IoT MQTT-over-WSS connection per account; subscribes each grill's grill_state."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: str,
        *,
        connection_factory: ConnectionFactory = _default_connection_factory,
    ) -> None:
        self._loop = loop
        self._client_id = client_id
        self._connection_factory = connection_factory
        self._conn: mqtt.Connection | None = None
        self._dispatchers: dict[str, StateDispatcher] = {}

    def add_grill(
        self,
        grill_id: str,
        *,
        on_state: StateCb,
        on_availability: AvailCb,
    ) -> StateDispatcher:
        d = StateDispatcher(
            grill_id,
            on_state=on_state,
            on_availability=on_availability,
            loop=self._loop,
        )
        self._dispatchers[grill_id] = d
        return d

    async def async_connect(self, creds: Credentials) -> None:
        # awscrt's connection builder does blocking I/O (it reads its own package metadata
        # via importlib.metadata), so build the connection off the event loop.
        self._conn = await asyncio.to_thread(
            self._connection_factory,
            creds=creds,
            client_id=self._client_id,
            on_interrupted=self._on_interrupted,
            on_resumed=self._on_resumed,
        )
        await asyncio.wrap_future(self._conn.connect())
        await self._subscribe_all()

    async def _subscribe_all(self) -> None:
        assert self._conn is not None
        for grill_id, disp in self._dispatchers.items():
            topic = const.TOPIC_GRILL_STATE.format(device_id=grill_id)
            sub_future, _ = self._conn.subscribe(
                topic=topic,
                qos=mqtt.QoS.AT_LEAST_ONCE,
                callback=lambda topic, payload, d=disp, **kw: d.handle_message_threadsafe(payload),
            )
            await asyncio.wrap_future(sub_future)
            disp.set_available(True)

    def _on_interrupted(self, connection: Any, error: Any, **kw: Any) -> None:  # native thread
        for d in list(self._dispatchers.values()):
            try:
                self._loop.call_soon_threadsafe(d.set_available, False)
            except RuntimeError:
                pass

    def _on_resumed(
        self,
        connection: Any,
        return_code: Any,
        session_present: Any,
        **kw: Any,
    ) -> None:
        # "resumed" is NOT healthy until re-subscribe resolves; do that on the loop.
        try:
            asyncio.run_coroutine_threadsafe(self._resubscribe(), self._loop)
        except RuntimeError:
            pass

    async def _resubscribe(self) -> None:
        try:
            await self._subscribe_all()
        except Exception:
            _LOGGER.warning("re-subscribe failed; staying unavailable", exc_info=True)

    def update_grill(
        self,
        grill_id: str,
        *,
        on_state: StateCb | None = None,
        on_availability: AvailCb | None = None,
    ) -> None:
        """Push updated callbacks to an already-running dispatcher, if present."""
        disp = self._dispatchers.get(grill_id)
        if disp is not None:
            disp.set_callbacks(on_state=on_state, on_availability=on_availability)

    async def async_disconnect(self) -> None:
        for d in self._dispatchers.values():
            d.close()
        if self._conn is not None:
            await asyncio.wrap_future(self._conn.disconnect())
            self._conn = None
