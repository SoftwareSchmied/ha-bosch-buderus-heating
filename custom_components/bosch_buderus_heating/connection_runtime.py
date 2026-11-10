"""Independent connection sessions and per-target source lifecycles."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .configuration import (
    CLOUD_KIND,
    LOCAL_KIND,
    ConfigurationError,
    ConfigurationSnapshot,
    JsonObject,
    StoredBinding,
    StoredConnection,
)
from .configuration_changes import ConfigurationRuns, RequestGeneration
from .configuration_management import ConfigurationManagement, section
from .gateway_identity import GatewayNamespace, IdentityContext
from .local_state import LocalCondition, LocalSourceState
from .log_helpers import log_failure
from .pointt import AuthenticationError, RateLimited
from .pointt.retry_after import (
    DEFAULT_CLOUD_PAUSE_SECONDS,
    MAX_CLOUD_PAUSE_SECONDS,
    MIN_CLOUD_PAUSE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)
CLOSE_TIMEOUT = 5.0
# R1 (P49b): the releasing key of a target view, and the pause before a failed
# target view is built again. NUL cannot occur in a target identifier.
_TARGET_KEY = "\0target:"
TARGET_RETRY_SECONDS = 300.0
# B04: failed attempts in a row of the same source or connection wait longer
# each time, from one minute up to 30 minutes; a successful start resets it.
RETRY_BASE_SECONDS = 60.0
RETRY_MAX_SECONDS = 1800.0
# B08: wording of the log lines, without identifiers.
_SOURCE_KINDS = {"pointt": "cloud", "bacon": "additional devices", LOCAL_KIND: "local"}
_CONNECTION_KINDS = {CLOUD_KIND: "cloud", LOCAL_KIND: "local"}


def _source_subject(adapter: str) -> str:
    return _SOURCE_KINDS.get(adapter, "unsupported") + " source"


class RuntimeChange(StrEnum):
    """C68: what a runtime notification carries.

    ``LOCAL_VALUES``: only new readings of a running local source; its phase,
    its condition and whether it is published stay the same. ``STATE``:
    anything else (phases, configuration, published or kept sources and
    views). Listeners that follow only phases or the configuration skip
    ``LOCAL_VALUES``.
    """

    LOCAL_VALUES = "local_values"
    STATE = "state"


class RepeatedFailure:
    """C61: the first failure of a series as one line with the error class
    only; its traceback and all repetitions at debug level.

    Never the error message: it may contain a value or a path. A series is
    kept per ``key`` (for example per source); ``succeeded`` ends it, so a
    later failure is logged again.
    """

    def __init__(
        self, logger: logging.Logger, message: str, *, level: int = logging.WARNING
    ) -> None:
        self._logger, self._message, self._level = logger, message, level
        self._failing: dict[str, set[str]] = {}

    def failed(self, error: BaseException, key: str = "") -> None:
        name = type(error).__name__
        logged = self._failing.setdefault(key, set())
        if name in logged:
            self._logger.debug("%s again (%s)", self._message, name, exc_info=error)
            return
        logged.add(name)
        self._logger.log(
            self._level,
            "%s (%s); further failures are logged at debug level",
            self._message,
            name,
        )
        self._logger.debug("Details: %s", self._message, exc_info=error)

    def succeeded(self, key: str = "") -> None:
        if self._failing:
            self._failing.pop(key, None)


class ConnectionPhase(StrEnum):
    CONNECTING = "connecting"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    AUTH_REQUIRED = "auth_required"
    DISABLED = "disabled"
    UNSUPPORTED = "unsupported"
    IDLE = "idle"
    STOPPED = "stopped"


def plant_gateway(kind: str, target: Mapping[str, Any]) -> str | None:
    """The gateway ID of a plant: its cloud identity, or for a plant set up
    locally (P49b step 7) its local one, the same string the cloud uses."""
    identities = target.get("identities")
    values = {
        item.get("namespace"): item.get("value")
        for item in (identities if isinstance(identities, list) else ())
        if isinstance(item, dict)
    }
    gateway = values.get("pointt_gateway")
    if gateway is None and kind == "local_installation":
        gateway = values.get("local_system_gateway")
    return gateway if isinstance(gateway, str) else None


class CachedConfiguration(ConfigurationManagement):
    """Parse the entry's configuration once per stored version.

    Home Assistant replaces ``data`` and ``options`` with new objects on every
    entry update, so the parsed snapshot is kept while both objects (and the
    stored revision and versions) are the same ones. ``invalidate`` drops it
    explicitly on every update notice.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        super().__init__(hass, entry)
        self._cached: tuple[object, object, object, ConfigurationSnapshot] | None = None

    def invalidate(self) -> None:
        self._cached = None

    def current(self) -> ConfigurationSnapshot:
        entry = self.entry
        data, options = entry.data, entry.options
        key = (
            entry.version,
            entry.minor_version,
            data.get("revision") if isinstance(data, Mapping) else None,
        )
        cached = self._cached
        if (
            cached is not None
            and cached[0] is data
            and cached[1] is options
            and cached[2] == key
            and self.hass.config_entries.async_get_entry(entry.entry_id) is entry
        ):
            return cached[3]
        snapshot = super().current()
        # Only read-only mappings: a plain dict could change in place.
        self._cached = (
            (data, options, key, snapshot)
            if isinstance(data, MappingProxyType)
            and isinstance(options, MappingProxyType)
            else None
        )
        return snapshot


class SourceFailure(Exception):
    """Transport adapters report a fixed category, never a private error body."""

    def __init__(
        self,
        phase: ConnectionPhase = ConnectionPhase.UNAVAILABLE,
        *,
        retry_after: float = 60,
    ) -> None:
        if phase not in (
            ConnectionPhase.UNAVAILABLE,
            ConnectionPhase.AUTH_REQUIRED,
            ConnectionPhase.UNSUPPORTED,
        ):
            raise ValueError("Invalid source failure category")
        self.phase = phase
        self.retry_after = (
            min(MAX_CLOUD_PAUSE_SECONDS, max(MIN_CLOUD_PAUSE_SECONDS, retry_after))
            if math.isfinite(retry_after)
            else DEFAULT_CLOUD_PAUSE_SECONDS
        )
        super().__init__(phase.value)


# B08: failures an adapter reports or the runtime maps itself; any other
# exception is unexpected and logged once per series as a warning.
_EXPECTED_FAILURES = (SourceFailure, AuthenticationError, RateLimited)


@dataclass(slots=True)
class FailureSeries:
    """B04/B08: failed attempts of one source or connection.

    Only counts, the exception class and fixed codes: never a message, value
    or identifier.
    """

    total: int = 0
    consecutive: int = 0
    last_error: str | None = None
    last_stage: str | None = None
    last_phase: ConnectionPhase | None = None
    retry_seconds: float | None = None
    # The current attempt started (published or connected).
    started: bool = False
    # This series was logged as a warning; its end is logged as info.
    warned: bool = False

    def failed(self, error: Exception, failure: SourceFailure, stage: str) -> float:
        """Count a failure; return the pause before the next attempt."""
        self.total += 1
        self.consecutive += 1
        self.last_error = type(error).__name__
        self.last_stage = stage
        self.last_phase = failure.phase
        growing = RETRY_BASE_SECONDS * (1 << min(self.consecutive - 1, 10))
        delay = max(failure.retry_after, min(RETRY_MAX_SECONDS, growing))
        self.retry_seconds = (
            delay if failure.phase is ConnectionPhase.UNAVAILABLE else None
        )
        return delay

    def succeeded(self) -> int:
        """A successful start ends the series; returns its failures."""
        self.started = True
        ended, self.consecutive, self.retry_seconds = self.consecutive, 0, None
        return ended

    def diagnostics(self) -> dict[str, object]:
        return {
            "failures": self.total,
            "consecutive_failures": self.consecutive,
            "last_failure": self.last_error,
            "last_failure_stage": self.last_stage,
            "last_failure_phase": None
            if self.last_phase is None
            else self.last_phase.value,
            "retry_seconds": self.retry_seconds,
        }


class SourceDriver(Protocol):
    async def run(self, context: SourceContext) -> None:
        """Use the existing adapter cadence and check context before every publish."""

    async def close(self) -> None:
        """Close only this source's resources, without closing the shared session."""


class ConnectionDriver(Protocol):
    async def connect(self) -> None:
        """Open or authenticate this connection, independently of its peers."""

    def source(self, binding_id: str, binding: StoredBinding) -> SourceDriver:
        """Create an independently stoppable source sharing this connection."""

    async def close(self) -> None:
        """Close the connection's owned resources after its sources stop."""


type DriverFactory = Callable[[str, StoredConnection], ConnectionDriver]
type RuntimeListener = Callable[[], None]


@dataclass(frozen=True, slots=True)
class SourceContext:
    """Capture provenance before starting a request or a long-running source."""

    runtime: ConnectionRuntime = field(repr=False)
    generation: RequestGeneration = field(repr=False)

    @property
    def current(self) -> bool:
        return self.runtime.current_source(self)

    @property
    def configuration_current(self) -> bool:
        """Active provenance, independent of the local source's health."""
        return self.runtime.current_source_configuration(self)

    @callback
    def publish_local(self, state: LocalSourceState) -> bool:
        return self.runtime.publish_local(self, state)

    def identity_context(self, namespace: GatewayNamespace) -> IdentityContext:
        return IdentityContext(
            namespace,
            self.runtime.snapshot.owner_id,
            self.generation.connection_id,
            self.generation.connection_generation,
        )

    @callback
    def publish(self, data: object) -> bool:
        """Drop late data before notifying entities or updating their inventory."""
        return self.runtime.publish(self, data)


@dataclass(slots=True)
class _SourceRun:
    generation: RequestGeneration
    task: asyncio.Task[None] | None = None
    driver: SourceDriver | None = None


@dataclass(slots=True)
class _ConnectionRun:
    identifier: str
    generation: RequestGeneration
    record: StoredConnection
    task: asyncio.Task[None] | None = None
    driver: ConnectionDriver | None = None
    connected: bool = False
    sources: dict[str, _SourceRun] = field(default_factory=dict)


class ConnectionRuntime:
    """Keep the owner online while connections authenticate, retry or stop.

    Factories are explicit adapter contracts. Unknown kinds stay unsupported;
    configuration data cannot select arbitrary imports or invent a transport.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        factories: Mapping[str, DriverFactory],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.management = CachedConfiguration(hass, entry)
        self.snapshot = self.management.current()
        # The decoded options of ``snapshot``, read-only (``_options``).
        self._options_cache: tuple[ConfigurationSnapshot, JsonObject] | None = None
        self._generations = ConfigurationRuns(self.snapshot)
        self._factories = dict(factories)
        self._sleep = sleep
        self._runs: dict[str, _ConnectionRun] = {}
        self._connection_phases: dict[str, ConnectionPhase] = {}
        self._source_phases: dict[str, ConnectionPhase] = {}
        self._data: dict[str, object] = {}
        # P40: data of a stopped cloud source that local readings keep showing
        # (sign-in required, cloud switched off, restarting), with its context.
        self._retained: dict[str, tuple[object, SourceContext]] = {}
        # P49: bindings whose dormant view is being built.
        self._dormant: set[str] = set()
        # P49b: releases of retained views still finishing (fault baseline),
        # so the next view of the binding never reads an older baseline.
        self._releasing: dict[str, asyncio.Task[None]] = {}
        # R1 (P49b): source tasks of removed bindings that still save their
        # fault baseline while they stop.
        self._stopping: dict[str, asyncio.Task[None]] = {}
        # R1: per-target local plant views of systems whose cloud access was
        # removed, the builds in progress, their generations and the removed
        # bindings each build waits for.
        self._target_views: dict[str, Any] = {}
        self._target_building: set[str] = set()
        self._target_generations: dict[str, int] = {}
        self._target_waits: dict[str, set[str]] = {}
        self._target_retry: asyncio.Task[None] | None = None
        self._local_states: dict[str, LocalSourceState] = {}
        # C68: each listener with whether it wants local readings alone.
        self._listeners: list[tuple[RuntimeListener, bool]] = []
        # C61: listeners whose last call failed, with the error classes
        # already logged.
        self._failing_listeners: dict[RuntimeListener, set[str]] = {}
        # C64: bindings whose view without a cloud failed to build (warned
        # once), and the pending retries of these builds.
        self._dormant_warned: set[str] = set()
        self._dormant_retries: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        # Dormant and target view builds, cancelled when the runtime closes.
        self._builds: set[asyncio.Task[None]] = set()
        self._connection_locks: dict[str, asyncio.Lock] = {}
        self._source_locks: dict[str, asyncio.Lock] = {}
        self._contexts: dict[str, SourceContext] = {}
        # B04/B08: failed attempts per binding and per connection.
        self._source_failures: dict[str, FailureSeries] = {}
        self._connection_failures: dict[str, FailureSeries] = {}
        self._closed = False
        self._started = False
        self._unsubscribe: list[Callable[[], None]] = []

    @callback
    def start(self) -> None:
        """Attach the HA listener before starting any independent network task."""
        if self._closed or self._started:
            raise RuntimeError("Runtime cannot be started twice")
        snapshot = self.management.current()
        self._started = True
        self._unsubscribe.append(self.management.subscribe(self.synchronize))

        async def updated(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
            self.management.invalidate()
            if not self._closed:
                try:
                    self.synchronize(self.management.current())
                except ConfigurationError:
                    self._reject_configuration()

        self._unsubscribe.append(self.entry.add_update_listener(updated))
        self.synchronize(snapshot)

    @callback
    def subscribe(
        self, listener: RuntimeListener, *, local_values: bool = True
    ) -> Callable[[], None]:
        """Call ``listener`` on every notification; with ``local_values``
        false not on those that carry only new local readings (C68)."""
        item = (listener, local_values)
        self._listeners.append(item)

        def unsubscribe() -> None:
            self._listeners.remove(item)
            self._failing_listeners.pop(listener, None)

        return unsubscribe

    def _notify(self, change: RuntimeChange = RuntimeChange.STATE) -> None:
        values_only = change is RuntimeChange.LOCAL_VALUES
        for listener, local_values in tuple(self._listeners):
            if values_only and not local_values:
                continue
            try:
                listener()
            except Exception as error:
                self._listener_failed(listener, error)
            else:
                if self._failing_listeners:
                    self._failing_listeners.pop(listener, None)

    def _listener_failed(self, listener: RuntimeListener, error: Exception) -> None:
        """C61: once per listener and error class an error naming both; the
        traceback and repetitions at debug level. A success ends it."""
        name = type(error).__name__
        where = getattr(listener, "__qualname__", type(listener).__qualname__)
        logged = self._failing_listeners.setdefault(listener, set())
        if name in logged:
            _LOGGER.debug(
                "A connection state listener failed again (%s in %s)",
                name,
                where,
                exc_info=error,
            )
            return
        logged.add(name)
        _LOGGER.error(
            "A connection state listener failed (%s in %s); further failures of "
            "it are logged at debug level",
            name,
            where,
        )
        _LOGGER.debug("Details of the listener failure", exc_info=error)

    def _task(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task: asyncio.Task[None] = self.hass.async_create_background_task(
            work, "home_comfort_connection", eager_start=False
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _options(self) -> JsonObject:
        """The current snapshot's options, decoded once; never modify them."""
        snapshot, cached = self.snapshot, self._options_cache
        if cached is None or cached[0] is not snapshot:
            cached = self._options_cache = (snapshot, snapshot.to_options())
        return cached[1]

    def _build(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = self._task(work)
        self._builds.add(task)
        task.add_done_callback(self._builds.discard)
        return task

    def _bindings(self, connection_id: str) -> dict[str, StoredBinding]:
        options = section(self._options(), "bindings")
        return {
            key: binding
            for key, binding in self.snapshot.bindings.items()
            if binding.connection_id == connection_id and options[key]["enabled"]
        }

    @callback
    def synchronize(self, snapshot: ConfigurationSnapshot) -> None:
        if self._closed:
            return
        changes = self._generations.update(snapshot)
        for key, binding in self.snapshot.bindings.items():
            if key not in snapshot.bindings:
                # R1: a view of the target waits until these have stopped.
                self._target_waits.setdefault(binding.target_id, set()).add(key)
        self.snapshot = snapshot
        for key in changes.connections:
            self._stop_connection(key, retain=True)
        for key in changes.bindings:
            self._release(key)
            self._data.pop(key, None)
            self._local_states.pop(key, None)
            self._source_phases.pop(key, None)
        for key in tuple(self._retained):
            if key not in snapshot.bindings:
                self._release(key)
        for key in tuple(self._connection_phases):
            if key not in snapshot.connections:
                self._connection_phases.pop(key)
        for failures, current in (
            (self._source_failures, snapshot.bindings),
            (self._connection_failures, snapshot.connections),
        ):
            for key in tuple(failures):
                if key not in current:
                    del failures[key]
        options = section(snapshot.to_options(), "connections")
        for key, connection in snapshot.connections.items():
            enabled = options[key]["enabled"]
            if not enabled or not self._bindings(key):
                # A switched-off access keeps local-backed data; a deselected
                # system does not.
                self._stop_connection(key, retain=not enabled)
                self._connection_phases[key] = (
                    ConnectionPhase.DISABLED if not enabled else ConnectionPhase.IDLE
                )
                if not enabled:
                    self._hold_without_cloud(key)
                    self._start_dormant(key, connection)
                continue
            if connection.kind not in self._factories:
                self._connection_phases[key] = ConnectionPhase.UNSUPPORTED
                continue
            run = self._runs.get(key)
            if run is None:
                run = _ConnectionRun(key, self._generations.capture(key), connection)
                self._runs[key] = run
                self._connection_phases[key] = ConnectionPhase.CONNECTING
                run.task = self._task(self._connect(run))
            elif run.connected:
                self._synchronize_sources(run)
        self._synchronize_target_views()
        self._notify()

    # ----------------------------------- R1: systems without a cloud access

    def target_view_gateways(self, *, marked: bool = False) -> dict[str, str]:
        """Targets shown from their local readings after their cloud access
        was removed (R1, P49b), with their gateway.

        Only with the explicit mark set when the access was removed, no cloud
        binding of the target and an enabled local access (``marked``: every
        marked target, with or without a local access, for its kept
        descriptions; the mark stays when the local access is removed).
        """
        from .local_selection import LocalSelection

        snapshot = self.snapshot
        options = section(snapshot.to_options(), "targets")
        result: dict[str, str] = {}
        for target_id, target in snapshot.targets.items():
            if options.get(target_id, {}).get("local_plant") is not True or any(
                binding.target_id == target_id and binding.adapter == "pointt"
                for binding in snapshot.bindings.values()
            ):
                continue
            gateway = plant_gateway(target.kind, target.to_data())
            if isinstance(gateway, str) and (
                marked or LocalSelection(self, target_id).has_local_access
            ):
                result[target_id] = gateway
        return result

    def target_views(self) -> dict[str, object]:
        return dict(self._target_views)

    def _synchronize_target_views(self) -> None:
        eligible = self.target_view_gateways()
        for target_id in tuple(self._target_views):
            if target_id not in eligible:
                self._end_target_view(target_id)
        for target_id in tuple(self._target_building):
            if target_id not in eligible:
                self._next_target_generation(target_id)  # the build is dropped
        for target_id, gateway_id in eligible.items():
            if target_id not in self._target_views:
                self._start_target_view(target_id, gateway_id)

    def _next_target_generation(self, target_id: str) -> int:
        generation = self._target_generations.get(target_id, 0) + 1
        self._target_generations[target_id] = generation
        return generation

    def _start_target_view(self, target_id: str, gateway_id: str) -> None:
        if self._closed or target_id in self._target_building:
            return
        build = getattr(self._factories.get(CLOUD_KIND), "target_plant_view", None)
        if build is None:
            return
        self._target_building.add(target_id)
        generation = self._next_target_generation(target_id)
        self._build(self._build_target_view(build, target_id, gateway_id, generation))

    def _target_current(self, target_id: str, generation: int) -> bool:
        return (
            not self._closed
            and self._target_generations.get(target_id) == generation
            and target_id not in self._target_views
            and target_id in self.target_view_gateways()
        )

    async def _build_target_view(
        self,
        build: Callable[..., Awaitable[Any]],
        target_id: str,
        gateway_id: str,
        generation: int,
    ) -> None:
        view: Any = None
        try:
            keys = self._target_waits.pop(target_id, set())
            waits = [
                task
                for task in (
                    *(self._releasing.get(key) for key in keys),
                    *(self._stopping.get(key) for key in keys),
                    # An earlier view of the target (local access off, on).
                    self._releasing.get(_TARGET_KEY + target_id),
                )
                if task is not None and not task.done()
            ]
            if waits:
                # The removed views save their fault baseline first.
                await asyncio.wait(waits, timeout=2 * CLOSE_TIMEOUT)
            if not self._target_current(target_id, generation):
                return
            view = await build(target_id, gateway_id)
            if view is None:
                return
            if self._target_current(target_id, generation):
                # Kept on its local readings before it is offered.
                await view.async_stop_source(retain=True)
            if not self._target_current(target_id, generation):
                # Changed while building: this view never writes anything.
                view.faults.discard()
                if view.local_retained:
                    await view.async_release_retention()
                else:
                    await view.async_stop_source()
                return
            self._target_views[target_id] = view
            self._notify()
        except asyncio.CancelledError:
            if view is not None and self._target_views.get(target_id) is not view:
                # Cancelled while building (unload): never writes or listens.
                view.faults.discard()
                with suppress(Exception):
                    async with asyncio.timeout(CLOSE_TIMEOUT):
                        if view.local_retained:
                            await view.async_release_retention()
                        else:
                            await view.async_stop_source()
            raise
        except Exception as err:
            log_failure(
                _LOGGER,
                logging.WARNING,
                "A system's local readings could not be shown; retrying",
                error=err,
            )
            _LOGGER.debug("Local plant view build failed", exc_info=True)
            if view is not None:
                # Never writes and never listens any more.
                view.faults.discard()
                with suppress(Exception):
                    await view.async_stop_source()
            retry = self._target_retry
            if not self._closed and (retry is None or retry.done()):
                # One pending retry at most, however many builds fail.
                self._target_retry = self._task(self._retry_target_views())
        finally:
            self._target_building.discard(target_id)
        if (
            not self._closed
            and target_id not in self._target_views
            and self._target_generations.get(target_id) != generation
            and (gateway := self.target_view_gateways().get(target_id)) is not None
        ):
            self._start_target_view(target_id, gateway)  # changed meanwhile

    async def _retry_target_views(self) -> None:
        """A failed build is tried again after a pause (or the next change).

        The current configuration decides: an invalid one is rejected, a
        changed one is applied, never the snapshot from before the pause.
        """
        await self._sleep(TARGET_RETRY_SECONDS)
        self._target_retry = None
        if self._closed:
            return
        try:
            current = self.management.current()
        except ConfigurationError:
            self._reject_configuration()
            return
        # Also with an unchanged snapshot: after a rejected configuration that
        # became valid again, this restarts the connections as well.
        self.synchronize(current)

    def _cancel_target_retry(self) -> None:
        retry, self._target_retry = self._target_retry, None
        if retry is not None and not retry.done():
            retry.cancel()

    def _end_target_view(self, target_id: str, *, move: bool = True) -> None:
        """The target is no longer shown alone: the cloud access was added
        again (the view continues as that binding's retained view until the
        cloud publishes), or the local access is gone."""
        view = self._target_views.pop(target_id)
        self._next_target_generation(target_id)
        options = section(self.snapshot.to_options(), "bindings")
        binding_id = next(
            (
                key
                for key, binding in self.snapshot.bindings.items()
                if binding.target_id == target_id
                and binding.adapter == "pointt"
                and options[key]["enabled"]
            ),
            None,
        )
        check = getattr(view, "retain_for_local", None)
        if (
            move
            and binding_id is not None
            and binding_id not in self._retained
            and check is not None
            and check()
        ):
            binding = self.snapshot.bindings[binding_id]
            # Like a dormant view: the new source takes its fault baseline
            # over and releases it with its first publication.
            self._retained[binding_id] = (
                view,
                SourceContext(
                    self, self._generations.peek(binding.connection_id, binding_id)
                ),
            )
            return
        release = getattr(view, "async_release_retention", None)
        if release is not None and not self._closed:
            task = self._task(release())
            key = _TARGET_KEY + target_id
            self._releasing[key] = task

            def done(finished: asyncio.Task[None]) -> None:
                if self._releasing.get(key) is finished:
                    del self._releasing[key]

            task.add_done_callback(done)

    def _synchronize_sources(self, run: _ConnectionRun) -> None:
        assert run.driver is not None
        bindings = self._bindings(run.identifier)
        for key, source in tuple(run.sources.items()):
            if key not in bindings or not self._generations.accepts(source.generation):
                if source.task is not None and not source.task.cancelling():
                    source.task.cancel()
                run.sources.pop(key)
                self._release(key)
                self._data.pop(key, None)
                self._local_states.pop(key, None)
                self._source_phases.pop(key, None)
        for key, binding in bindings.items():
            if key not in run.sources:
                source = _SourceRun(self._generations.capture(run.identifier, key))
                run.sources[key] = source
                self._source_phases[key] = ConnectionPhase.CONNECTING
                source.task = self._task(self._run_source(run, key, binding, source))

    def _stop_connection(self, key: str, *, retain: bool = False) -> None:
        run = self._runs.pop(key, None)
        if run is not None:
            if run.task is not None and not run.task.cancelling():
                run.task.cancel()
            for binding_id, source in run.sources.items():
                if source.task is not None and not source.task.cancelling():
                    source.task.cancel()
                if source.task is not None and not source.task.done():
                    self._track_stopping(binding_id, source.task)
                if retain:
                    self._retain(binding_id, self._contexts.get(binding_id))
                self._data.pop(binding_id, None)
                self._local_states.pop(binding_id, None)
                self._source_phases.pop(binding_id, None)

    def _track_stopping(self, binding_id: str, task: asyncio.Task[None]) -> None:
        self._stopping[binding_id] = task

        def done(finished: asyncio.Task[None]) -> None:
            if self._stopping.get(binding_id) is finished:
                del self._stopping[binding_id]

        task.add_done_callback(done)

    def _active(self, run: _ConnectionRun) -> bool:
        return (
            not self._closed
            and self._runs.get(run.identifier) is run
            and self.accepts(run.generation)
        )

    def _reject_configuration(self) -> None:
        """Invalid ownership cannot continue with the last valid target routing."""
        for key in tuple(self._runs):
            self._stop_connection(key)
        for key in tuple(self._retained):
            self._release(key)
        self._cancel_target_retry()
        for target_id in tuple(self._target_views):
            self._end_target_view(target_id, move=False)
        for target_id in tuple(self._target_building):
            self._next_target_generation(target_id)  # the build is dropped
        self._data.clear()
        self._local_states.clear()
        self._contexts.clear()
        self._connection_phases = {
            key: ConnectionPhase.UNAVAILABLE for key in self.snapshot.connections
        }
        self._notify()

    @staticmethod
    def _failure(error: Exception) -> SourceFailure:
        if isinstance(error, SourceFailure):
            return error
        if isinstance(error, AuthenticationError):
            return SourceFailure(ConnectionPhase.AUTH_REQUIRED)
        if isinstance(error, RateLimited):
            return SourceFailure(retry_after=error.retry_after or 300)
        return SourceFailure()

    async def _close_driver(self, driver: ConnectionDriver | SourceDriver) -> None:
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT):
                await driver.close()
        except Exception as err:
            log_failure(
                _LOGGER,
                logging.ERROR,
                "A connection resource could not be closed",
                error=err,
            )

    async def _connect(self, run: _ConnectionRun) -> None:
        # A replacement waits for its own predecessor, never for another access.
        async with self._connection_locks.setdefault(run.identifier, asyncio.Lock()):
            await self._connection_loop(run)

    def _failed(
        self,
        series: FailureSeries,
        error: Exception,
        failure: SourceFailure,
        *,
        subject: str,
        stage: str,
    ) -> float:
        """B04/B08: count and log a failed attempt; return the next pause.

        The first unexpected failure of a series is a warning with the error
        class only, its traceback and all further failures are debug messages.
        """
        delay = series.failed(error, failure, stage)
        retry = (
            f"the next attempt follows in {delay:.0f} seconds"
            if failure.phase is ConnectionPhase.UNAVAILABLE
            else "no automatic attempt follows"
        )
        expected = isinstance(error, _EXPECTED_FAILURES)
        if expected or series.warned:
            _LOGGER.debug(
                "A %s failed while %s (%s, %s); %s",
                subject,
                stage,
                type(error).__name__,
                failure.phase.value,
                retry,
                exc_info=None if expected else error,
            )
            return delay
        series.warned = True
        _LOGGER.warning(
            "A %s failed while %s (%s, %s); %s. Further failures in a row are "
            "logged at debug level",
            subject,
            stage,
            type(error).__name__,
            failure.phase.value,
            retry,
        )
        _LOGGER.debug("Details of the %s failure", subject, exc_info=error)
        return delay

    @staticmethod
    def _started_again(series: FailureSeries | None, subject: str) -> None:
        """B04/B08: a successful start ends a series of failed attempts."""
        if series is None:
            return
        warned, series.warned = series.warned, False
        if (ended := series.succeeded()) and warned:
            _LOGGER.info("A %s works again after %d failed attempts", subject, ended)
        elif ended:
            _LOGGER.debug("A %s works again after %d failed attempts", subject, ended)

    def source_failures(self, binding_id: str) -> dict[str, object]:
        """Failed attempts of a source, for diagnostics."""
        return self._source_failures.get(binding_id, FailureSeries()).diagnostics()

    def connection_failures(self, connection_id: str) -> dict[str, object]:
        """Failed attempts of a connection, for diagnostics."""
        series = self._connection_failures.get(connection_id, FailureSeries())
        return series.diagnostics()

    async def _connection_loop(self, run: _ConnectionRun) -> None:
        subject = _CONNECTION_KINDS.get(run.record.kind, "unsupported") + " access"
        while self._active(run):
            driver: ConnectionDriver | None = None
            delay = RETRY_BASE_SECONDS
            series = self._connection_failures.setdefault(
                run.identifier, FailureSeries()
            )
            series.started = False
            try:
                driver = self._factories[run.record.kind](run.identifier, run.record)
                run.driver = driver
                await driver.connect()
                if not self._active(run):
                    return
                run.connected = True
                self._started_again(series, subject)
                self._connection_phases[run.identifier] = ConnectionPhase.READY
                self._synchronize_sources(run)
                self._notify()
                await asyncio.Future[None]()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not self._active(run):
                    return
                failure = self._failure(error)
                delay = self._failed(
                    series,
                    error,
                    failure,
                    subject=subject,
                    stage="running" if series.started else "connecting",
                )
                self._connection_phases[run.identifier] = failure.phase
                self._notify()
                if failure.phase in (
                    ConnectionPhase.AUTH_REQUIRED,
                    ConnectionPhase.UNSUPPORTED,
                ):
                    return
            finally:
                run.connected = False
                tasks = [
                    source.task
                    for source in run.sources.values()
                    if source.task is not None
                ]
                for task in tasks:
                    if not task.cancelling():
                        task.cancel()
                if tasks:
                    await asyncio.wait(tasks, timeout=CLOSE_TIMEOUT)
                run.sources.clear()
                if driver is not None:
                    await asyncio.shield(self._task(self._close_driver(driver)))
            await self._sleep(delay)

    async def _run_source(
        self,
        run: _ConnectionRun,
        binding_id: str,
        binding: StoredBinding,
        source: _SourceRun,
    ) -> None:
        async with self._source_locks.setdefault(binding_id, asyncio.Lock()):
            await self._source_loop(run, binding_id, binding, source)

    async def _source_loop(
        self,
        run: _ConnectionRun,
        binding_id: str,
        binding: StoredBinding,
        source: _SourceRun,
    ) -> None:
        while (
            self._active(run)
            and self.accepts(source.generation)
            and self.connection_phase(run.identifier) is ConnectionPhase.READY
        ):
            context = SourceContext(self, source.generation)
            self._contexts[binding_id] = context
            driver: SourceDriver | None = None
            delay = RETRY_BASE_SECONDS
            series = self._source_failures.setdefault(binding_id, FailureSeries())
            series.started = False
            try:
                assert run.driver is not None
                driver = run.driver.source(binding_id, binding)
                source.driver = driver
                await driver.run(context)
                if context.current:
                    raise SourceFailure()
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not self._active(run) or not context.current:
                    return
                failure = self._failure(error)
                delay = self._failed(
                    series,
                    error,
                    failure,
                    subject=_source_subject(binding.adapter),
                    stage="running" if series.started else "starting",
                )
                if binding.adapter != LOCAL_KIND:
                    self._retain(binding_id, context)
                self._data.pop(binding_id, None)
                if binding.adapter == LOCAL_KIND and binding_id in self._local_states:
                    self._local_states[binding_id] = replace(
                        self._local_states[binding_id],
                        condition=LocalCondition.AUTH_REQUIRED
                        if failure.phase is ConnectionPhase.AUTH_REQUIRED
                        else LocalCondition.UNAVAILABLE,
                        identity_confirmed=False,
                    )
                self._source_phases[binding_id] = failure.phase
                if failure.phase is ConnectionPhase.AUTH_REQUIRED:
                    self._connection_phases[run.identifier] = failure.phase
                    for key, sibling in run.sources.items():
                        self._retain(key, self._contexts.get(key))
                        self._data.pop(key, None)
                        self._source_phases[key] = failure.phase
                        if (
                            sibling is not source
                            and sibling.task is not None
                            and not sibling.task.cancelling()
                        ):
                            sibling.task.cancel()
                    self._notify()
                    if run.task is not None and not run.task.cancelling():
                        run.task.cancel()
                    return
                self._notify()
                if failure.phase is ConnectionPhase.UNSUPPORTED:
                    return
            finally:
                if self._contexts.get(binding_id) is context:
                    self._contexts.pop(binding_id)
                if driver is not None:
                    await asyncio.shield(self._task(self._close_driver(driver)))
            await self._sleep(delay)

    def accepts(self, generation: RequestGeneration) -> bool:
        """Observe external HA changes before accepting a result from an await."""
        if self._closed:
            return False
        try:
            current = self.management.current()
        except ConfigurationError:
            self._reject_configuration()
            return False
        if current is not self.snapshot and current != self.snapshot:
            self.synchronize(current)
        return self._generations.accepts(generation)

    def current_source_configuration(self, context: SourceContext) -> bool:
        generation = context.generation
        return self._contexts.get(
            generation.binding_id or ""
        ) is context and self.accepts(generation)

    @callback
    def publish_local(self, context: SourceContext, state: LocalSourceState) -> bool:
        binding_id = context.generation.binding_id
        if binding_id is None or not context.configuration_current:
            return False
        binding = self.snapshot.bindings.get(binding_id)
        if (
            binding is None
            or binding.adapter != LOCAL_KIND
            or state.context != context.identity_context(GatewayNamespace.K40_LOCAL)
            or (
                state.condition is LocalCondition.READY
                and self.connection_phase(context.generation.connection_id)
                is not ConnectionPhase.READY
            )
        ):
            return False
        previous = self._local_states.get(binding_id)
        self._local_states[binding_id] = state
        phase = {
            LocalCondition.INITIALIZING: ConnectionPhase.CONNECTING,
            LocalCondition.READY: ConnectionPhase.READY,
            LocalCondition.PAUSED: ConnectionPhase.UNAVAILABLE,
            LocalCondition.UNAVAILABLE: ConnectionPhase.UNAVAILABLE,
            LocalCondition.AUTH_REQUIRED: ConnectionPhase.AUTH_REQUIRED,
            LocalCondition.CERTIFICATE_CHANGED: ConnectionPhase.AUTH_REQUIRED,
        }[state.condition]
        # C68: only new readings when neither the phase, the condition nor
        # whether the binding's data is published changes.
        values_only = (
            previous is not None
            and previous.condition is state.condition
            and self._source_phases.get(binding_id) is phase
            and (binding_id in self._data) is (phase is ConnectionPhase.READY)
        )
        self._source_phases[binding_id] = phase
        # A non-ready snapshot is available to recovery/diagnostics only.
        # It does not become ordinary fresh source data.
        if phase is ConnectionPhase.READY:
            self._data[binding_id] = state
            self._started_again(
                self._source_failures.get(binding_id), _source_subject(LOCAL_KIND)
            )
        else:
            self._data.pop(binding_id, None)
        self._notify(RuntimeChange.LOCAL_VALUES if values_only else RuntimeChange.STATE)
        return True

    def local_state(self, binding_id: str) -> LocalSourceState | None:
        context = self._contexts.get(binding_id)
        if context is None or not context.configuration_current:
            return None
        state = self._local_states.get(binding_id)
        if state is None or state.context != context.identity_context(
            GatewayNamespace.K40_LOCAL
        ):
            return None
        return state

    def current_source(self, context: SourceContext) -> bool:
        generation = context.generation
        return (
            self._contexts.get(generation.binding_id or "") is context
            and self.accepts(generation)
            and self.connection_phase(generation.connection_id) is ConnectionPhase.READY
        )

    def capture_connection(self, connection_id: str) -> RequestGeneration:
        return self._generations.capture(connection_id)

    def current_connection(self, generation: RequestGeneration) -> bool:
        return self.accepts(generation) and self.connection_phase(
            generation.connection_id
        ) in (ConnectionPhase.CONNECTING, ConnectionPhase.READY)

    @callback
    def publish(self, context: SourceContext, data: object) -> bool:
        generation = context.generation
        binding_id = generation.binding_id
        if binding_id is None or not context.current:
            return False
        retained = self._retained.get(binding_id)
        if retained is not None and retained[0] is not data:
            self._release(binding_id)
        self._data[binding_id] = data
        self._source_phases[binding_id] = ConnectionPhase.READY
        binding = self.snapshot.bindings.get(binding_id)
        self._started_again(
            self._source_failures.get(binding_id),
            _source_subject(binding.adapter if binding else ""),
        )
        self._notify()
        return True

    def source_data(self, binding_id: str) -> object | None:
        binding = self.snapshot.bindings.get(binding_id)
        if (
            binding is None
            or self._source_phases.get(binding_id) is not ConnectionPhase.READY
        ):
            return None
        run = self._runs.get(binding.connection_id)
        source = run.sources.get(binding_id) if run else None
        context = self._contexts.get(binding_id)
        if source is None or context is None or not context.current:
            return None
        return self._data.get(binding_id)

    # ------------------------------------------------- P40: local-backed data

    def _retain(self, binding_id: str, context: SourceContext | None) -> None:
        """Keep a stopping cloud source's data if local readings can show it."""
        data = self._data.get(binding_id)
        check = getattr(data, "retain_for_local", None)
        if data is None or context is None or check is None or not check():
            return
        previous = self._retained.get(binding_id)
        if previous is not None and previous[0] is not data:
            self._release(binding_id)
        self._retained[binding_id] = (data, context)

    def retain_startup(self, context: SourceContext, data: object) -> bool:
        """A source that cannot sign in starts local-backed from its inventory.

        Also called while the failed source closes (siblings of a shared
        sign-in), so only the configuration generation must still be valid.
        An existing retained view is kept.
        """
        binding_id = context.generation.binding_id
        check = getattr(data, "retain_for_local", None)
        if (
            binding_id is None
            or self._closed
            or binding_id in self._retained
            or binding_id not in self.snapshot.bindings
            or not self._generations.accepts(context.generation)
            or check is None
            or not check()
        ):
            return False
        self._retained[binding_id] = (data, context)
        self._notify()
        return True

    def view_changed(self) -> None:
        """A kept view changed on its own (A1): listeners look again."""
        if not self._closed:
            self._notify()

    def _hold_without_cloud(self, connection_id: str) -> None:
        """A1: views kept for local readings of a switched-off access.

        A running source stopped by switching the access off (or one already
        kept after a sign-in failure or an outage) continues without a cloud,
        like a dormant view: its local error list decides the faults.
        """
        for binding_id, (data, _) in tuple(self._retained.items()):
            binding = self.snapshot.bindings.get(binding_id)
            hold = getattr(data, "hold_without_cloud", None)
            if binding is None or binding.connection_id != connection_id:
                continue
            if hold is not None:
                try:
                    hold()
                except Exception as err:
                    log_failure(
                        _LOGGER,
                        logging.ERROR,
                        "A view could not continue without the cloud",
                        error=err,
                    )

    # ------------------------------------------- P49: switched off at start

    def _start_dormant(self, connection_id: str, connection: StoredConnection) -> None:
        """A switched-off access shows local-backed data also after a restart.

        Without a running source there is nothing to keep, so the factory builds
        a never-polling view from the stored inventory (P49). Bindings that
        already have a view, or whose build is running, are skipped.
        """
        factory = self._factories.get(connection.kind)
        build = getattr(factory, "dormant_view", None)
        if build is None:
            return
        options = section(self._options(), "bindings")
        for binding_id, binding in self.snapshot.bindings.items():
            if (
                binding.connection_id != connection_id
                or not options[binding_id]["enabled"]
                or binding_id in self._retained
                or binding_id in self._dormant
            ):
                continue
            context = SourceContext(
                self, self._generations.peek(connection_id, binding_id)
            )
            self._dormant.add(binding_id)
            self._build(self._build_dormant(build, binding_id, binding, context))

    async def _build_dormant(
        self,
        build: Callable[..., Awaitable[None]],
        binding_id: str,
        binding: StoredBinding,
        context: SourceContext,
        *,
        local_plant: bool = False,
    ) -> None:
        try:
            if (releasing := self._releasing.get(binding_id)) is not None:
                # The released view saves its fault baseline first (P49b).
                await asyncio.wait((releasing,), timeout=CLOSE_TIMEOUT)
            await build(binding_id, binding, context)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # C64: like a target view, a warning and a retry after a pause.
            if binding_id in self._dormant_warned:
                _LOGGER.debug(
                    "A view without the cloud could not be built again (%s)",
                    type(error).__name__,
                    exc_info=error,
                )
            else:
                self._dormant_warned.add(binding_id)
                _LOGGER.warning(
                    "A system's local readings could not be shown without the "
                    "cloud (%s); retrying in %.0f seconds. Further failures are "
                    "logged at debug level",
                    type(error).__name__,
                    TARGET_RETRY_SECONDS,
                )
                _LOGGER.debug("View build without the cloud failed", exc_info=error)
            self._retry_dormant(build, binding_id, binding, context, local_plant)
        else:
            self._dormant_warned.discard(binding_id)
        finally:
            self._dormant.discard(binding_id)
        generation = context.generation
        connection = self.snapshot.connections.get(generation.connection_id)
        if (
            not self._closed
            and connection is not None
            and binding_id not in self._retained
            and binding_id in self.snapshot.bindings
            and self.connection_phase(generation.connection_id)
            is ConnectionPhase.DISABLED
            and generation
            != self._generations.peek(generation.connection_id, binding_id)
        ):
            # Changed while building (for example switched on and off again).
            self._start_dormant(generation.connection_id, connection)

    def retain_dormant(self, context: SourceContext, data: object) -> bool:
        """Keep a dormant view of a switched-off access (P49)."""
        if not self._dormant_acceptable(context, data):
            return False
        binding_id = context.generation.binding_id
        assert binding_id is not None
        self._retained[binding_id] = (data, context)
        self._notify()
        return True

    def retain_local_plant(self, context: SourceContext, data: object) -> bool:
        """Keep the local plant view of a switched-off access (P49b).

        The same checks as a dormant view; it never replaces a retained view
        (a dormant view is replaced only by ``expire_dormant``, which releases
        it first), so two views of a binding never both create entities.
        """
        if getattr(data, "read_only_view", False) is not True:
            return False
        return self.retain_dormant(context, data)

    def expire_dormant(self, binding_id: str, data: object) -> bool:
        """End a retained dormant view and build the binding's view again.

        For example once its inventory passed its age limit (P49b): the new
        build waits until the old view saved its fault baseline.
        """
        binding = self.snapshot.bindings.get(binding_id)
        connection = (
            self.snapshot.connections.get(binding.connection_id)
            if binding is not None
            else None
        )
        if (
            self._closed
            or binding is None
            or connection is None
            or not self.is_retained(binding_id, data)
        ):
            return False
        phase = self.connection_phase(binding.connection_id)
        if phase is ConnectionPhase.DISABLED:
            self._release(binding_id)
            self._notify()
            self._start_dormant(binding.connection_id, connection)
            return True
        # 2A: a cloud that is not available (sign-in required, outage): the
        # local plant view replaces the expired view until the cloud answers.
        build = getattr(self._factories.get(connection.kind), "local_plant_view", None)
        # The source's phase: a failed source of a ready connection counts.
        if build is None or self.source_phase(binding_id) not in (
            ConnectionPhase.AUTH_REQUIRED,
            ConnectionPhase.UNAVAILABLE,
        ):
            return False
        context = self._retained[binding_id][1]
        self._release(binding_id)
        self._notify()
        self._dormant.add(binding_id)
        self._build(
            self._build_dormant(build, binding_id, binding, context, local_plant=True)
        )
        return True

    def _retry_dormant(
        self,
        build: Callable[..., Awaitable[None]],
        binding_id: str,
        binding: StoredBinding,
        context: SourceContext,
        local_plant: bool,
    ) -> None:
        """C64: one pending retry per binding after a failed build."""
        pending = self._dormant_retries.get(binding_id)
        if self._closed or (pending is not None and not pending.done()):
            return
        task = self._build(
            self._retry_dormant_after(build, binding_id, binding, context, local_plant)
        )
        self._dormant_retries[binding_id] = task

        def done(finished: asyncio.Task[None]) -> None:
            if self._dormant_retries.get(binding_id) is finished:
                del self._dormant_retries[binding_id]

        task.add_done_callback(done)

    async def _retry_dormant_after(
        self,
        build: Callable[..., Awaitable[None]],
        binding_id: str,
        binding: StoredBinding,
        context: SourceContext,
        local_plant: bool,
    ) -> None:
        """Build the view again after a pause, if it is still wanted.

        The current state decides, never the one before the pause: a
        switched-off access builds its dormant view anew (as after every
        change); a cloud still not available (2A) builds its local plant view
        again with the same provenance while that is still current.
        """
        await self._sleep(TARGET_RETRY_SECONDS)
        generation = context.generation
        connection = self.snapshot.connections.get(generation.connection_id)
        if (
            self._closed
            or connection is None
            or self.snapshot.bindings.get(binding_id) != binding
            or binding_id in self._retained
            or binding_id in self._dormant
        ):
            return
        if self.connection_phase(generation.connection_id) is ConnectionPhase.DISABLED:
            self._start_dormant(generation.connection_id, connection)
        elif (
            local_plant
            and self._generations.accepts(generation)
            and self.source_phase(binding_id)
            in (ConnectionPhase.AUTH_REQUIRED, ConnectionPhase.UNAVAILABLE)
        ):
            self._dormant.add(binding_id)
            self._build(
                self._build_dormant(
                    build, binding_id, binding, context, local_plant=True
                )
            )

    def _dormant_acceptable(self, context: SourceContext, data: object) -> bool:
        generation = context.generation
        binding_id = generation.binding_id
        binding = self.snapshot.bindings.get(binding_id or "")
        options = self._options()
        check = getattr(data, "retain_for_local", None)
        return not (
            binding_id is None
            or binding is None
            or self._closed
            or binding_id in self._retained
            or binding.connection_id != generation.connection_id
            or generation.connection_id not in self.snapshot.connections
            # Not after an invalid configuration was rejected.
            or self.connection_phase(generation.connection_id)
            is not ConnectionPhase.DISABLED
            or section(options, "connections")[generation.connection_id]["enabled"]
            or not section(options, "bindings")[binding_id]["enabled"]
            or generation
            != self._generations.peek(generation.connection_id, binding_id)
            or check is None
            or not check()
        )

    async def async_settle_faults(
        self, binding_id: str, owner: object | None = None
    ) -> object | None:
        """Before a new source of a binding loads its fault baseline (P49b).

        A view still being released saves its baseline first; a retained
        view that keeps a baseline of its own (the local plant view) hands it
        over now, while it keeps showing local readings until the new source
        publishes. Without either, nothing happens. Returns the view that
        handed its baseline over: if the new source never publishes, it
        gives the baseline back (``async_resume_faults``).
        """
        if (releasing := self._releasing.get(binding_id)) is not None:
            await asyncio.wait((releasing,), timeout=CLOSE_TIMEOUT)
        item = self._retained.get(binding_id)
        hand_over = getattr(item[0], "async_hand_over_faults", None) if item else None
        if hand_over is None:
            return None
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT):
                await hand_over(owner)
        except Exception as err:
            log_failure(
                _LOGGER,
                logging.ERROR,
                "A fault baseline could not be handed over",
                error=err,
            )
        assert item is not None
        return item[0]

    def retained_data(self, binding_id: str) -> object | None:
        """The retained view of a binding, if any (P49b)."""
        item = self._retained.get(binding_id)
        return item[0] if item is not None else None

    def is_retained(self, binding_id: str, data: object) -> bool:
        item = self._retained.get(binding_id)
        return item is not None and item[0] is data

    def _release(self, binding_id: str) -> None:
        item = self._retained.pop(binding_id, None)
        release = getattr(item[0], "async_release_retention", None) if item else None
        if release is not None and not self._closed:
            task = self._task(release())
            self._releasing[binding_id] = task

            def done(finished: asyncio.Task[None]) -> None:
                if self._releasing.get(binding_id) is finished:
                    del self._releasing[binding_id]

            task.add_done_callback(done)

    def display_data(self, binding_id: str) -> object | None:
        """Current source data, else retained data that local readings back."""
        data = self.source_data(binding_id)
        if data is not None:
            return data
        item = self._retained.get(binding_id)
        if item is None or binding_id not in self.snapshot.bindings:
            return None
        check = getattr(item[0], "retain_for_local", None)
        return item[0] if check is not None and check() else None

    def display_context(self, binding_id: str) -> SourceContext | None:
        if self.source_data(binding_id) is not None:
            return self.source_context(binding_id)
        item = self._retained.get(binding_id)
        return item[1] if item is not None and self.display_data(binding_id) else None

    def source_context(self, binding_id: str) -> SourceContext | None:
        """Return provenance only for the currently published source."""
        if self.source_data(binding_id) is None:
            return None
        return self._contexts.get(binding_id)

    def source_driver(self, binding_id: str) -> SourceDriver | None:
        """The running driver of a configured source, for owner-internal hints."""
        binding = self.snapshot.bindings.get(binding_id)
        run = self._runs.get(binding.connection_id) if binding is not None else None
        source = run.sources.get(binding_id) if run is not None else None
        context = self._contexts.get(binding_id)
        if source is None or context is None or not context.configuration_current:
            return None
        return source.driver

    def connection_driver(self, connection_id: str) -> ConnectionDriver | None:
        """Expose only an active owned driver for read-only diagnostics."""
        run = self._runs.get(connection_id)
        return run.driver if run is not None and self._active(run) else None

    @callback
    def restart_source(self, context: SourceContext) -> None:
        """Rediscover one currently published source after explicit repair."""
        key = context.generation.binding_id
        if key is None or self.source_context(key) is not context:
            raise ConfigurationError("binding_unavailable")
        run = self._runs[context.generation.connection_id]
        self._generations.invalidate_binding(key)
        self._data.pop(key, None)
        self._synchronize_sources(run)
        self._notify()

    @property
    def pending_reauthentication(self) -> tuple[str, ...]:
        return tuple(
            key
            for key in self.snapshot.connections
            if self._connection_phases.get(key) is ConnectionPhase.AUTH_REQUIRED
        )

    def connection_phase(self, connection_id: str) -> ConnectionPhase:
        return self._connection_phases.get(connection_id, ConnectionPhase.STOPPED)

    def source_phase(self, binding_id: str) -> ConnectionPhase:
        binding = self.snapshot.bindings.get(binding_id)
        if binding is None:
            return ConnectionPhase.STOPPED
        connection = self.connection_phase(binding.connection_id)
        if connection is not ConnectionPhase.READY:
            return connection
        if not section(self._options(), "bindings")[binding_id]["enabled"]:
            return ConnectionPhase.DISABLED
        return self._source_phases.get(binding_id, ConnectionPhase.STOPPED)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_target_retry()  # never waits for its pause
        # Views still being built end with the runtime; a view a build already
        # kept is released below with the others.
        builds = tuple(self._builds)
        for task in builds:
            task.cancel()
        if builds:
            await asyncio.wait(builds, timeout=CLOSE_TIMEOUT)
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        for key in tuple(self._runs):
            self._stop_connection(key)
        retained = [item[0] for item in self._retained.values()]
        retained.extend(self._target_views.values())
        self._retained.clear()
        self._target_views.clear()
        self._data.clear()
        self._local_states.clear()
        self._contexts.clear()
        self._source_phases.clear()
        self._connection_phases = {
            key: ConnectionPhase.STOPPED for key in self.snapshot.connections
        }
        self._notify()
        for data in retained:
            release = getattr(data, "async_release_retention", None)
            if release is not None:
                try:
                    async with asyncio.timeout(CLOSE_TIMEOUT):
                        await release()
                except Exception as err:
                    log_failure(
                        _LOGGER,
                        logging.ERROR,
                        "A connection resource could not be closed",
                        error=err,
                    )
        if self._tasks:
            await asyncio.wait(tuple(self._tasks), timeout=2 * CLOSE_TIMEOUT)
