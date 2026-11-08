"""One additional-cloud session with separately guarded device access."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bacon.client import BaconClient
from .bacon.models import BaconError, Device, Shadow, topic
from .bacon.session import MAINTENANCE_INTERVAL, BaconAccount, WriteValidator
from .configuration import JsonObject, StoredBinding
from .connection_runtime import ConnectionPhase, SourceContext, SourceFailure
from .pointt.exceptions import SourceStopped

if TYPE_CHECKING:
    from .managed_cloud import CloudConnection

_operation: ContextVar[BaconSource | None] = ContextVar(
    "cloud_device_operation", default=None
)
# Failures a stop itself causes before its cancellation arrives.
_STOP_OUTCOMES = frozenset(
    {"source_stopped", "command_unconfirmed", "connection", "device_unavailable"}
)


class BaconPool:
    """Own a single MQTT session while at least one device source needs it."""

    def __init__(self, connection: CloudConnection) -> None:
        self.connection = connection
        self.account: ManagedBaconAccount | None = None
        self.sources: dict[str, BaconSource] = {}
        self.auth_failed = asyncio.Event()
        self._lock = asyncio.Lock()

    def guard(self) -> None:
        try:
            self.connection.ensure_current()
        except SourceStopped as error:
            raise BaconError("source_stopped") from error
        if self.auth_failed.is_set():
            raise BaconError("authentication")

    def updated(self) -> None:
        for source in tuple(self.sources.values()):
            source.updated()

    def request_guard(self) -> None:
        self.guard()
        if not any(source.current() for source in tuple(self.sources.values())):
            raise BaconError("source_stopped")

    async def attach(self, source: BaconSource) -> None:
        async with self._lock:
            self.guard()
            source.ensure_current()
            if self.account is None:
                expected = self.connection.configuration.cloud_data().get(
                    "bacon_account"
                )
                if not isinstance(expected, str) or not expected:
                    raise SourceFailure(ConnectionPhase.AUTH_REQUIRED)
                client = BaconClient(
                    async_get_clientsession(self.connection.hass),
                    self.connection.tokens,
                    request_guard=self.request_guard,
                    backoff=self.connection.backoff,
                    request_semaphore=self.connection.resources.semaphore,
                    expected_account=expected,
                )
                self.account = ManagedBaconAccount(self, client)
            account = self.account
            if source.serial in self.sources:
                raise BaconError("duplicate_source")
            self.sources[source.serial] = source
            source.view = BaconDeviceAccess(source, account)
            account.devices[source.serial] = source.view.device
            account._locks.setdefault(source.serial, asyncio.Lock())
            account.selected = tuple(self.sources)
            if account._task is None:
                task = self.connection.hass.async_create_background_task(
                    account.run(), "home_comfort_additional_cloud", eager_start=False
                )
                account.attach_task(task)
            else:
                account.selection_changed.set()
            self.updated()

    async def detach(self, source: BaconSource) -> None:
        async with self._lock:
            if self.sources.get(source.serial) is not source:
                return
            del self.sources[source.serial]
            assert self.account is not None
            self.account._claimed.discard(source.serial)
            self.account.selected = tuple(self.sources)
            self.account.selection_changed.set()
            if not self.sources:
                await self.account.close()
                self.account = None

    async def close(self) -> None:
        async with self._lock:
            if self.account is not None:
                await self.account.close()
                self.account = None


class ManagedBaconAccount(BaconAccount):
    """Reuse ordering and write confirmation while guarding all MQTT sends."""

    def __init__(self, pool: BaconPool, client: BaconClient) -> None:
        self.pool = pool
        self.selection_changed = asyncio.Event()
        self._publish_lock = asyncio.Lock()
        self._subscribed: dict[str, BaconSource] = {}
        super().__init__(client, (), notify=pool.updated)

    def check_connection(self) -> None:
        self.pool.guard()
        self.client.check_request()

    def on_error(self, error: BaconError) -> None:
        if error.category == "rate_limited":
            self.pool.connection.backoff.activate(error.retry_after)
        elif error.category in {"authentication", "account_changed"}:
            self.pool.auth_failed.set()

    async def run_connection(self) -> None:
        self.selection_changed.clear()
        self._subscribed = dict(self.pool.sources)
        await super().run_connection()

    def _set_membership(self, offered: tuple[str, ...]) -> None:
        super()._set_membership(tuple(s for s in offered if self._current(s)))

    def _current(self, serial: str) -> bool:
        source = self.pool.sources.get(serial)
        return source is not None and source.current()

    def available(self, serial: str) -> bool:
        return self._current(serial) and super().available(serial)

    def ingest(
        self, name: str, payload: bytes, subject: str, *, retained: bool = False
    ) -> None:
        prefix = f"users/{subject}/devices/"
        if name.startswith(prefix) and self._current(
            name[len(prefix) :].partition("/")[0]
        ):
            super().ingest(name, payload, subject, retained=retained)

    async def _publish(self, serial: str, suffix: str, payload: str | bytes) -> None:
        operation = _operation.get()
        source = operation or self.pool.sources.get(serial)
        if source is None or not source.current():
            if operation is not None:
                raise BaconError("source_stopped")
            return
        mqtt, generation = self._mqtt, self.generation
        async with self._publish_lock:
            if not source.current():
                if operation is not None:
                    raise BaconError("source_stopped")
                return
            if (
                source.serial != serial
                or self._mqtt is not mqtt
                or self.generation != generation
            ):
                raise BaconError("connection")
            self.check_connection()
            await super()._publish(serial, suffix, payload)
            if operation is not None:
                source.ensure_current()

    async def _selection_update(self, mqtt: Any, subject: str) -> None:
        if not self.selection_changed.is_set():
            return
        self.selection_changed.clear()
        selected = {
            serial: source
            for serial, source in self.pool.sources.items()
            if source.current()
        }
        added = {
            serial: source
            for serial, source in selected.items()
            if self._subscribed.get(serial) is not source
        }
        if added:
            self._set_membership(await self.client.get_devices())
        for serial in sorted(self._subscribed.keys() - selected.keys()):
            self.check_connection()
            await mqtt.unsubscribe(topic(subject, serial, "#"))
            self._subscribed.pop(serial, None)
        for serial, source in sorted(added.items()):
            self.check_connection()
            if not source.current():
                continue
            # Redeliver this device's retained metadata without reconnecting peers.
            await mqtt.subscribe(topic(subject, serial, "#"), qos=1)
            self._subscribed[serial] = source
            if not source.current():
                continue
            if serial in self._claimed:
                await self._publish(serial, "shadows/state/get", b"")

    async def _wait_for_maintenance(self, receiver: asyncio.Task[None]) -> None:
        changed = asyncio.create_task(self.selection_changed.wait())
        try:
            await asyncio.wait(
                {receiver, changed},
                timeout=MAINTENANCE_INTERVAL,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            changed.cancel()
            await asyncio.gather(changed, return_exceptions=True)


class BaconSource:
    """Retain an exact device binding independently of the shared MQTT session."""

    def __init__(
        self, pool: BaconPool, binding_id: str, binding: StoredBinding
    ) -> None:
        self.pool, self.binding_id = pool, binding_id
        self.serial = cast(str, cast(JsonObject, binding.to_data()["locator"])["value"])
        self.context: SourceContext | None = None
        self.view: BaconDeviceAccess | None = None
        self._closed = False
        self._operations: set[asyncio.Task[Any]] = set()
        self._listeners: set[Callable[[], None]] = set()

    def current(self) -> bool:
        return (
            not self._closed
            and not self.pool.auth_failed.is_set()
            and self.context is not None
            and self.context.current
        )

    def ensure_current(self) -> None:
        if not self.current():
            raise BaconError("source_stopped")

    def updated(self) -> None:
        if (
            self.current()
            and self.view is not None
            and self.view.client.subject is not None
        ):
            assert self.context is not None
            self.context.publish(self.view)
            for listener in tuple(self._listeners):
                listener()

    async def run(self, context: SourceContext) -> None:
        self.context = context
        await self.pool.attach(self)
        await self.pool.auth_failed.wait()
        raise SourceFailure(ConnectionPhase.AUTH_REQUIRED)

    async def operation[T](self, action: Callable[[], Awaitable[T]]) -> T:
        self.ensure_current()

        async def perform() -> T:
            token = _operation.set(self)
            try:
                self.ensure_current()
                result = await action()
                self.ensure_current()
                return result
            finally:
                _operation.reset(token)

        task = asyncio.create_task(perform())
        self._operations.add(task)
        try:
            return await task
        except asyncio.CancelledError as error:
            if not self.current():
                raise BaconError("source_stopped") from error
            raise
        except BaconError as error:
            # Stopped before the cancellation arrived (for example right after
            # a publish): the same outcome as a cancelled operation.
            if not self.current() and error.category in _STOP_OUTCOMES:
                raise BaconError("source_stopped") from error
            raise
        finally:
            self._operations.discard(task)

    async def close(self) -> None:
        self._closed = True
        for task in tuple(self._operations):
            task.cancel()
        if self._operations:
            await asyncio.gather(*self._operations, return_exceptions=True)
        await self.pool.detach(self)
        for listener in tuple(self._listeners):
            listener()
        self._listeners.clear()


class BaconDeviceAccess:
    """Expose the existing family contract for exactly one current source."""

    def __init__(self, source: BaconSource, account: ManagedBaconAccount) -> None:
        self.source, self.account = source, account
        self.device = Device(source.serial)
        previous = account.devices.get(source.serial)
        if previous is not None:
            # Keep ordering fences, never the previous source's measurements or grants.
            self.device.info_timestamp = previous.info_timestamp
            self.device.sensor_timestamp = previous.sensor_timestamp
            if previous.state is not None:
                self.device.state = Shadow(
                    previous.state.version, previous.state.timestamp, {}, {}
                )
                self.device.state_snapshot_version = previous.state.version
        self.client = account.client
        self.devices: Mapping[str, Device] = MappingProxyType(
            {source.serial: self.device}
        )
        self.selected = (source.serial,)

    @property
    def generation(self) -> int:
        return self.account.generation

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.source._listeners.add(listener)
        return lambda: self.source._listeners.discard(listener)

    def expire_values(self, device: Device) -> None:
        self.account.expire_values(device)

    def available(self, serial: str) -> bool:
        return (
            serial == self.source.serial
            and self.source.current()
            and self.account.available(serial)
        )

    def _check(self, serial: str) -> None:
        self.source.ensure_current()
        if serial != self.source.serial:
            raise BaconError("invalid_identifier")

    async def read_state(self, serial: str) -> Shadow:
        self._check(serial)
        return await self.source.operation(lambda: self.account.read_state(serial))

    async def write_state(
        self, serial: str, desired: dict[str, Any], validator: WriteValidator
    ) -> Shadow:
        self._check(serial)
        return await self.source.operation(
            lambda: self.account.write_state(serial, desired, validator)
        )
