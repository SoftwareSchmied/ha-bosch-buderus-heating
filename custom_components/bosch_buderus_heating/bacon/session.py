"""MQTT 5 account lifecycle and reported-state confirmation for Bacon."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import ssl
from collections.abc import Callable
from time import monotonic
from typing import Any
from uuid import uuid4

import aiomqtt

from . import dhw, dry, purifier, rac
from .client import BaconClient
from .models import (
    BROKER,
    MAX_BYTES,
    USER_AGENT,
    BaconError,
    Device,
    Shadow,
    object_payload,
    token_subject,
    topic,
)
from .updates import expire_values, ingest_sensor, ingest_state

# Reported state is refreshed by our own periodic get requests.
STATE_STALE_AFTER = 900
# Devices may publish measurements and information only on change.
SENSOR_STALE_AFTER = 3600
ALIVE_AFTER = 3600
RESYNC_INTERVAL = 300
MAINTENANCE_INTERVAL = 60
WriteValidator = Callable[[Device, dict[str, Any]], bool]


class BaconAccount:
    """One selected account, with no command replay across connections."""

    def __init__(
        self,
        client: BaconClient,
        selected: tuple[str, ...],
        *,
        notify: Callable[[], None] = lambda: None,
        mqtt_factory: Callable[..., Any] = aiomqtt.Client,
        command_timeout: float = 20,
    ) -> None:
        self.client, self.selected, self.notify = client, selected, notify
        self.devices = {serial: Device(serial) for serial in selected}
        self.connected = False
        self.error: str | None = None
        self.generation = 0
        self.reconnects = 0
        self.invalid_messages = 0
        self._factory, self._timeout = mqtt_factory, command_timeout
        self._mqtt: Any = None
        self._task: asyncio.Task[None] | None = None
        self._locks = {serial: asyncio.Lock() for serial in selected}
        self._readers: dict[str, asyncio.Future[Shadow]] = {}
        self._changed = asyncio.Event()
        self._rejected: set[str] = set()
        self._listeners: set[Callable[[], None]] = set()
        self._claimed: set[str] = set()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def _notify(self) -> None:
        for device in self.devices.values():
            self.expire_values(device)
        self._changed.set()
        self.notify()
        for listener in tuple(self._listeners):
            listener()

    def expire_values(self, device: Device) -> None:
        expire_values(device, monotonic(), STATE_STALE_AFTER, SENSOR_STALE_AFTER)

    def attach_task(self, task: asyncio.Task[None]) -> None:
        self._task = task

    def check_connection(self) -> None:
        """Allow a managed owner to reject obsolete connection work."""

    def on_error(self, error: BaconError) -> None:
        """Allow a managed owner to share authentication and quota failures."""

    async def _selection_update(self, mqtt: Any, subject: str) -> None:
        """Allow selected devices to change without reconnecting other consumers."""

    async def _wait_for_maintenance(self, receiver: asyncio.Task[None]) -> None:
        await asyncio.wait({receiver}, timeout=MAINTENANCE_INTERVAL)

    async def _publish(self, serial: str, suffix: str, payload: str | bytes) -> None:
        if self._mqtt is None or self.client.subject is None:
            raise BaconError("connection")
        await self._mqtt.publish(
            topic(self.client.subject, serial, suffix), payload, qos=1, retain=False
        )

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._disconnected()

    def _disconnected(self) -> None:
        self.connected = False
        self._mqtt = None
        for device in self.devices.values():
            # Readings keep their stream generations: a reconnect is our own
            # connection event, see _start_generation. Availability needs a
            # message on the new connection.
            device.generation = 0
        for future in tuple(self._readers.values()):
            if not future.done():
                future.set_exception(BaconError("connection"))
        self._notify()

    async def run(self) -> None:
        delay = 5.0
        try:
            while True:
                try:
                    await self.run_connection()
                    delay = 5
                except BaconError as err:
                    self.error = err.category
                    self.on_error(err)
                    delay = max(delay, err.retry_after)
                    if err.category in {"authentication", "account_changed"}:
                        return
                except aiomqtt.MqttError as err:
                    self.error = _mqtt_error(err)
                    self.on_error(BaconError(self.error))
                    if self.error == "authentication":
                        return
                    if self.error == "rate_limited":
                        delay = max(60, delay)
                finally:
                    self._disconnected()
                self.reconnects += 1
                await asyncio.sleep(delay)
                delay = min(300, delay * 2)
        finally:
            self._disconnected()

    async def run_connection(self) -> None:
        offered = await self.client.get_devices()
        self._set_membership(offered)
        for serial in self.selected:
            if serial not in offered:
                self.devices[serial].online = None
        if not set(offered).intersection(self.selected):
            raise BaconError("devices_unavailable", RESYNC_INTERVAL)
        token = await self.client.access_token()
        subject = token_subject(token)
        if subject != self.client.subject:
            raise BaconError("account_changed")
        tls = await asyncio.to_thread(ssl.create_default_context)
        self.check_connection()
        # A fresh client has no queued messages from an earlier connection.
        async with self._factory(
            BROKER,
            443,
            identifier=hashlib.sha256(uuid4().bytes).hexdigest(),
            username=subject,
            password=token,
            protocol=aiomqtt.ProtocolVersion.V5,
            transport="websockets",
            websocket_path="/mqtt",
            websocket_headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
            },
            tls_context=tls,
            clean_start=True,
            keepalive=60,
            timeout=20,
            max_queued_incoming_messages=256,
            max_queued_outgoing_messages=1,
            max_inflight_messages=1,
            max_concurrent_outgoing_calls=1,
        ) as mqtt:
            self.check_connection()
            self._mqtt = mqtt
            await mqtt.subscribe(f"users/{subject}/#", qos=1)
            self.check_connection()
            self._start_generation()
            self.connected, self.error = True, None
            self._notify()
            receiver = asyncio.create_task(self._receive(mqtt, subject))
            last_sync = 0.0
            try:
                while True:
                    now = monotonic()
                    if not last_sync or now - last_sync >= RESYNC_INTERVAL:
                        if last_sync:
                            self._set_membership(await self.client.get_devices())
                        for serial in sorted(self._claimed):
                            await self._publish(serial, "shadows/state/get", b"")
                        last_sync = now
                    await self._selection_update(mqtt, subject)
                    await self._wait_for_maintenance(receiver)
                    if receiver.done():
                        await receiver
                        raise BaconError("connection")
                    current = await self.client.access_token()
                    if current != token:
                        # Renew the authenticated WebSocket before using the new token.
                        return
                    self._notify()
            finally:
                receiver.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError, BaconError, aiomqtt.MqttError
                ):
                    await receiver

    def _start_generation(self) -> None:
        """Begin a new connection and carry readings of the previous one over.

        A reconnect, for example after a token renewal, is not device silence:
        sensor and reported state fields stay visible until their own age limit
        expires them. Membership loss or a re-added device clears the stream
        generations instead, so nothing is carried over for those.
        """
        previous = self.generation
        self.generation += 1
        if not previous:
            return
        for device in self.devices.values():
            if device.state_generation == previous:
                device.state_generation = self.generation
            if device.sensor_generation == previous:
                device.sensor_generation = self.generation
            if device.metadata_generation == previous:
                device.metadata_generation = self.generation

    def _set_membership(self, offered: tuple[str, ...]) -> None:
        self._claimed = set(offered).intersection(self.selected)
        for serial, device in self.devices.items():
            if serial not in self._claimed:
                device.generation = device.state_generation = 0
                device.sensor_generation = device.metadata_generation = 0
                device.online = None
        self._notify()

    async def _receive(self, mqtt: Any, subject: str) -> None:
        async for message in mqtt.messages:
            try:
                self.ingest(
                    str(message.topic),
                    bytes(message.payload),
                    subject,
                    retained=message.retain,
                )
            except BaconError as err:
                if err.category == "rate_limited":
                    raise
                self.invalid_messages += 1
        raise BaconError("connection")

    def ingest(
        self, name: str, payload: bytes, subject: str, *, retained: bool = False
    ) -> None:
        prefix = f"users/{subject}/devices/"
        if not name.startswith(prefix):
            return
        serial, _, suffix = name[len(prefix) :].partition("/")
        if serial not in self._claimed:
            return
        device = self.devices[serial]
        if suffix in {"shadows/state/get/rejected", "shadows/state/update/rejected"}:
            if retained:
                return
            data = object_payload(payload)
            if data.get("code") == 429:
                raise BaconError("rate_limited")
            self._rejected.add(serial)
            reader = self._readers.get(serial)
            if reader is not None and not reader.done():
                reader.set_exception(BaconError("request_rejected"))
            self._notify()
            return
        if suffix not in {
            "topics/info",
            "topics/meta",
            "topics/sensor",
            "shadows/state/get/accepted",
            "shadows/state/update/accepted",
        }:
            return
        data = object_payload(payload)
        if suffix == "topics/info":
            stamp, online = data.get("timestamp"), data.get("online")
            if type(stamp) is not int or stamp < 0 or type(online) is not bool:
                raise BaconError("invalid_information")
            # A reconnect is our own connection event, not device silence: the
            # redelivered information returns the device to this connection.
            # Availability still depends on the last real sign of life.
            device.generation = self.generation
            # A re-added device or regained membership starts without an online
            # state; the redelivered information with the same timestamp may set
            # it once. It never counts as a sign of life.
            repeated = stamp == device.info_timestamp and device.online is None
            if stamp <= device.info_timestamp and not repeated:
                return
            device.info_timestamp = stamp
            # The information timestamp's unit is not established. Use it only
            # for ordering; a retained message cannot prove a live connection.
            device.online = online
            # Preserve only presence, not private/unknown error codes or text.
            # Missing or retained information cannot clear a reported error.
            device.error_present = (
                None
                if retained or "error" not in data
                else False
                if data["error"] is None
                else True
                if isinstance(data["error"], dict)
                else None
            )
            if not retained and not repeated:
                device.info_received_at = monotonic()
                self._alive(device)
            appliance = data.get("appliance")
            model = appliance.get("type") if isinstance(appliance, dict) else None
            new_model = model if isinstance(model, str) and len(model) <= 80 else None
            if device.model is not None and new_model != device.model:
                # Never combine a replacement model with its predecessor's data.
                device.state = None
                device.sensor = {}
                device.metadata = {}
                device.sensor_fields_received_at.clear()
                device.state_fields_received_at.clear()
                device.state_fields_version.clear()
                device.state_snapshot_version = -1
                device.sensor_timestamp = -1
                device.state_generation = 0
                device.sensor_generation = device.metadata_generation = 0
            device.model = new_model
        elif suffix == "topics/meta":
            device.metadata = data
            # Retained capability metadata is valid for this subscription.
            # It does not establish device connectivity or fresh measurements.
            device.metadata_generation = self.generation
        elif suffix == "topics/sensor":
            if retained:
                return
            self.expire_values(device)
            if ingest_sensor(device, data, self.generation, monotonic()):
                self._alive(device)
        else:
            if retained:
                return
            self.expire_values(device)
            reports = device.state_reports
            current = ingest_state(
                device,
                data,
                self.generation,
                monotonic(),
                full=suffix == "shadows/state/get/accepted",
            )
            # Our own get response and desired-only acknowledgements come from
            # the cloud; only newly reported fields show that the device is live.
            if (
                suffix == "shadows/state/update/accepted"
                and device.state_reports != reports
            ):
                self._alive(device)
            reader = self._readers.get(serial)
            if current is not None:
                device.uncertain = False
                if reader and not reader.done():
                    reader.set_result(current)
        device.received_at = monotonic()
        self._notify()

    def _alive(self, device: Device) -> None:
        """Record a device-originated, non-retained message on any channel."""
        device.alive_at = monotonic()
        device.generation = self.generation

    def available(self, serial: str) -> bool:
        """Online state comes from the newest information message.

        Devices may send information only on change, so any sign of life within
        the last hour keeps that state usable, also across our own reconnects
        once the information was received on the current connection.
        """
        device = self.devices[serial]
        return (
            self.connected
            and serial in self._claimed
            and device.generation == self.generation
            and device.alive_at is not None
            and monotonic() - device.alive_at <= ALIVE_AFTER
            and device.online is not None
        )

    async def read_state(self, serial: str) -> Shadow:
        if not self.connected or self._mqtt is None or serial not in self._claimed:
            raise BaconError("connection")
        if serial in self._readers:
            raise BaconError("busy")
        assert self.client.subject is not None
        future: asyncio.Future[Shadow] = asyncio.get_running_loop().create_future()
        self._readers[serial] = future
        try:
            await self._publish(serial, "shadows/state/get", b"")
            async with asyncio.timeout(self._timeout):
                return await future
        except (TimeoutError, aiomqtt.MqttError) as err:
            raise BaconError("state_unconfirmed") from err
        finally:
            self._readers.pop(serial, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def write_state(
        self,
        serial: str,
        desired: dict[str, Any],
        validator: WriteValidator,
    ) -> Shadow:
        """Internal family adapter API; no generic HA write service is exposed."""
        if serial not in self.devices:
            raise BaconError("invalid_identifier")
        async with self._locks[serial]:
            device = self.devices[serial]
            try:
                # Freeze the caller's values before awaiting a response.
                desired = object_payload(json.dumps(desired, allow_nan=False))
            except (TypeError, ValueError, RecursionError) as err:
                raise BaconError("invalid_payload") from err
            baseline = await self.read_state(serial)
            if not self.available(serial) or device.online is not True or not desired:
                raise BaconError("device_unavailable")
            if not _contains(baseline.reported, baseline.desired):
                raise BaconError("busy")
            # Family-specific options, limits and metadata must be checked here.
            if not validator(device, desired):
                raise BaconError("unsupported_command")
            encoded = json.dumps({"state": {"desired": desired}}, allow_nan=False)
            object_payload(encoded)
            if len(encoded.encode()) > MAX_BYTES:
                raise BaconError("invalid_payload")
            if _contains(baseline.reported, desired):
                return baseline
            self._rejected.discard(serial)
            assert self.client.subject is not None
            mqtt = self._mqtt
            generation = self.generation
            if mqtt is None:
                raise BaconError("connection")
            try:
                async with asyncio.timeout(self._timeout):
                    # A lost PUBACK is ambiguous. Never publish the command twice.
                    with contextlib.suppress(aiomqtt.MqttError):
                        await self._publish(serial, "shadows/state/update", encoded)
                    while True:
                        self._changed.clear()
                        if (
                            serial in self._rejected
                            or not self.available(serial)
                            or device.online is not True
                            or self.generation != generation
                        ):
                            raise BaconError("command_unconfirmed")
                        current = device.state
                        if (
                            current is not None
                            and device.state_generation == generation
                            and current.version > baseline.version
                            and current.timestamp >= baseline.timestamp
                            and _contains(current.reported, desired)
                            and all(
                                _contains(baseline.reported, {key: value})
                                or device.state_fields_version.get(key, -1)
                                > baseline.version
                                for key, value in desired.items()
                            )
                        ):
                            return current
                        await self._changed.wait()
            except asyncio.CancelledError:
                device.uncertain = True
                self._notify()
                raise
            except (TimeoutError, BaconError) as err:
                device.uncertain = True
                self._notify()
                raise BaconError("command_unconfirmed") from err

    def diagnostics(self) -> dict[str, object]:
        return {
            "connected": self.connected,
            "selected_devices": len(self.selected),
            "online_devices": sum(
                self.available(s) and d.online is True for s, d in self.devices.items()
            ),
            "error": self.error,
            "reconnects": self.reconnects,
            "invalid_messages": self.invalid_messages,
            "states_received": sum(d.state is not None for d in self.devices.values()),
            "metadata_received": sum(bool(d.metadata) for d in self.devices.values()),
            "unconfirmed_commands": sum(d.uncertain for d in self.devices.values()),
            "air_conditioners": {
                "midea_climate": {
                    "identified": sum(rac.supported(d) for d in self.devices.values()),
                    "metadata_current": sum(
                        rac.supported(d)
                        and d.metadata_generation == self.generation
                        and self.available(s)
                        and d.online is True
                        for s, d in self.devices.items()
                    ),
                    "sensor_current": sum(
                        rac.supported(d)
                        and d.sensor_generation == self.generation
                        and monotonic() - d.sensor_received_at <= SENSOR_STALE_AFTER
                        and self.available(s)
                        and d.online is True
                        for s, d in self.devices.items()
                    ),
                },
            },
            "dehumidifiers": {
                name: {
                    "identified": sum(
                        dry.family(d) == name for d in self.devices.values()
                    ),
                    "metadata_current": sum(
                        dry.family(d) == name
                        and d.metadata_generation == self.generation
                        and self.available(s)
                        and d.online is True
                        for s, d in self.devices.items()
                    ),
                    "sensor_current": sum(
                        dry.family(d) == name
                        and d.sensor_generation == self.generation
                        and monotonic() - d.sensor_received_at <= SENSOR_STALE_AFTER
                        and self.available(s)
                        and d.online is True
                        for s, d in self.devices.items()
                    ),
                }
                for name in dry.FAMILIES
            },
            "air_purifiers": {
                name: {
                    "identified": sum(
                        purifier.family(d) == name for d in self.devices.values()
                    ),
                    "metadata_current": sum(
                        purifier.family(d) == name
                        and d.metadata_generation == self.generation
                        and self.available(s)
                        and d.online is True
                        for s, d in self.devices.items()
                    ),
                    "sensor_current": sum(
                        purifier.family(d) == name
                        and d.sensor_generation == self.generation
                        and monotonic() - d.sensor_received_at <= SENSOR_STALE_AFTER
                        and self.available(s)
                        and d.online is True
                        for s, d in self.devices.items()
                    ),
                }
                for name in purifier.FAMILIES
            },
            "water_heaters": {
                name: {
                    "identified": sum(
                        dhw.family(d) == name for d in self.devices.values()
                    ),
                    "metadata_current": sum(
                        dhw.family(d) == name
                        and d.metadata_generation == self.generation
                        and self.available(s)
                        and d.online is True
                        for s, d in self.devices.items()
                    ),
                    "sensor_current": sum(
                        dhw.family(d) == name
                        and d.sensor_generation == self.generation
                        and monotonic() - d.sensor_received_at <= SENSOR_STALE_AFTER
                        and self.available(s)
                        and d.online is True
                        for s, d in self.devices.items()
                    ),
                }
                for name in dhw.FAMILIES
            },
        }


def _contains(actual: dict[str, Any], desired: dict[str, Any]) -> bool:
    return all(
        key in actual
        and (
            isinstance(actual[key], dict) and _contains(actual[key], value)
            if isinstance(value, dict)
            else type(actual[key]) is type(value) and actual[key] == value
        )
        for key, value in desired.items()
    )


def _mqtt_error(err: aiomqtt.MqttError) -> str:
    if isinstance(err, aiomqtt.MqttCodeError):
        code = getattr(err.rc, "value", err.rc)
        if code in (134, 135):
            return "authentication"
        if code in (151, 152, 159):
            return "rate_limited"
    return "connection"
