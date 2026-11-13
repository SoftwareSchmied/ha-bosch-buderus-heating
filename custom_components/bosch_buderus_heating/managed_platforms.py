"""Attach native entity objects to independently replaceable source attempts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import EntityPlatform

from .bacon.models import device_key
from .configuration import ConfigurationError
from .configuration_migration import legacy_cloud_connection_id
from .connection_inventory import RetainedEntity
from .connection_runtime import RepeatedFailure, SourceContext
from .const import DOMAIN
from .coordinator import BoschBuderusDataUpdateCoordinator
from .entity_use import entity_use
from .managed_bacon import BaconDeviceAccess
from .platform_scope import EntityBuilder, Listener, PlatformScope
from .wallbox import WallboxCoordinator

if TYPE_CHECKING:
    from .managed_entry import ManagedEntry
    from .pointt.metrics import RequestMetrics

_LOGGER = logging.getLogger(__name__)
# P49b: a read-only view (local readings alone) offers no controls.
# R1: the longest wait for a replaced source's entities to be removed.
_PREFIX_WAIT_SECONDS = 30.0
# C57: the longest wait for running attachments before the platforms unload.
_SUSPEND_WAIT_SECONDS = 5.0
_READ_ONLY_PLATFORMS = frozenset(
    {Platform.SENSOR, Platform.BINARY_SENSOR, Platform.EVENT}
)


@dataclass(slots=True)
class _Source:
    value: object
    scope: PlatformScope
    prefix: str
    context: SourceContext | None = None


@dataclass(slots=True)
class _Attachment:
    source: _Source
    listeners: list[Listener] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    pending: list[Entity] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    active: bool = True

    def invalidate(self) -> None:
        self.active = False
        self.pending.clear()
        for remove in self.listeners:
            remove()
        self.listeners.clear()


class ManagedPlatforms:
    """Coordinate platforms without changing the owner or restarting peers."""

    def __init__(self, owner: ManagedEntry) -> None:
        self.owner = owner
        self._platforms: dict[Platform, _Platform] = {}
        self._closed = False

    def register(self, name: Platform, build: EntityBuilder) -> None:
        if self._closed or name in self._platforms:
            raise RuntimeError("Platform lifecycle is not ready for registration")
        platform = entity_platform.async_get_current_platform()
        controller = _Platform(self.owner, name, platform, build)
        self._platforms[name] = controller
        self.owner.entry.async_on_unload(controller.stop)
        controller.changed()

    async def suspend(self) -> None:
        """C57: before Home Assistant unloads the platforms, no source adds an
        entity any more; running attachments end first (bounded).

        The entities stay: unloading the platforms removes them.
        """
        for controller in self._platforms.values():
            controller.suspend()
        jobs = [
            job
            for controller in self._platforms.values()
            for job in controller.jobs
            if not job.done()
        ]
        if jobs:
            await asyncio.wait(jobs, timeout=_SUSPEND_WAIT_SECONDS)

    def resume(self, unloaded: Collection[Platform] = ()) -> None:
        """The platforms were not all unloaded: attach the sources again where
        the platform is still set up; one already unloaded stays stopped.

        ``unloaded`` names the platforms whose unload succeeded; Home
        Assistant before 2026.10 still lists them afterwards."""
        loaded = entity_platform.async_get_platforms(self.owner.hass, DOMAIN)
        for name, controller in self._platforms.items():
            if name not in unloaded and any(
                platform is controller.platform for platform in loaded
            ):
                controller.resume()
            else:
                controller.stop()

    async def close(self) -> None:
        self._closed = True
        for controller in self._platforms.values():
            controller.stop()
        await asyncio.gather(*(p.close() for p in self._platforms.values()))


class _Platform:
    """Serialize replacement per binding while other bindings keep running."""

    def __init__(
        self,
        owner: ManagedEntry,
        name: Platform,
        platform: EntityPlatform,
        build: EntityBuilder,
    ) -> None:
        self.owner, self.name, self.platform, self.build = owner, name, platform, build
        self._active: dict[str, _Attachment] = {}
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        # C57: while Home Assistant unloads the platform, nothing is added.
        self._suspended = False
        # C60: the inventory and registry problems already logged.
        self._record_logged: set[str] = set()
        self._attach_failure = RepeatedFailure(
            _LOGGER,
            f"A source's native entities could not be attached to the {name} platform",
            level=logging.ERROR,
        )
        # C68: the attached sources follow phases and the configuration;
        # local readings reach the entities through their coordinators.
        self._unsubscribe = owner.connections.subscribe(
            self.changed, local_values=False
        )

    @property
    def jobs(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(self._jobs.values())

    def _sources(self) -> dict[str, _Source]:
        if self._closed:
            return {}
        runtime = self.owner.connections
        sources: dict[str, _Source] = {}
        for key, binding in tuple(runtime.snapshot.bindings.items()):
            # P40: a stopped cloud source stays attached while local readings
            # back it, so its entities keep showing local values.
            value = runtime.display_data(key)
            context = runtime.display_context(key)
            if value is None or context is None:
                continue

            def current(key: str = key, value: object = value) -> bool:
                return not self._closed and runtime.display_data(key) is value

            scope = PlatformScope(
                self.owner.entry.entry_id, lambda _: None, current=current
            )
            if isinstance(value, BoschBuderusDataUpdateCoordinator):
                if (
                    getattr(value, "read_only_view", False) is True
                    and self.name not in _READ_ONLY_PLATFORMS
                ):
                    continue
                scope.coordinators = (value,)
                prefix = value.gateway.gateway_id + ":"
                if self.name is Platform.SENSOR:
                    connection = binding.connection_id
                    namespace = self.owner.entry.entry_id
                    if connection != legacy_cloud_connection_id(namespace):
                        namespace += ":connection:" + connection
                    metrics = value.client.metrics
                    metric_scope = PlatformScope(
                        namespace,
                        lambda _: None,
                        metrics=metrics,
                        metric_updates=runtime.subscribe,
                        current=partial(self._metrics_current, connection, metrics),
                    )
                    # NUL cannot occur in a binding identifier.
                    sources["\0metrics:" + connection] = _Source(
                        metrics, metric_scope, namespace + ":request_metrics:"
                    )
            elif isinstance(value, WallboxCoordinator):
                scope.wallboxes = (value,)
                prefix = value.gateway.gateway_id + ":"
            elif isinstance(value, BaconDeviceAccess):
                if value.client.subject is None:
                    continue
                scope.bacon = value
                prefix = device_key(value.client.subject, value.source.serial) + ":"
            else:
                continue
            sources[key] = _Source(value, scope, prefix, context)
        # R1 (P49b): systems whose cloud access was removed, shown from their
        # local readings. No context: nothing is recorded for a binding.
        for target_id, view in runtime.target_views().items():
            if not isinstance(view, BoschBuderusDataUpdateCoordinator) or (
                self.name not in _READ_ONLY_PLATFORMS
            ):
                continue

            def shown(target_id: str = target_id, value: object = view) -> bool:
                return (
                    not self._closed and runtime.target_views().get(target_id) is value
                )

            scope = PlatformScope(
                self.owner.entry.entry_id, lambda _: None, current=shown
            )
            scope.coordinators = (view,)
            # NUL cannot occur in a target identifier.
            sources["\0target:" + target_id] = _Source(
                view, scope, view.gateway.gateway_id + ":"
            )
        return sources

    def _wake_after(self, key: str, _finished: asyncio.Task[None]) -> None:
        if not self._closed:
            self._wake(key)

    async def _others_removed(self, key: str, attached: _Attachment) -> bool:
        """Wait until no other attachment that is going away holds entities
        of the same unique-ID prefix (R1: binding view and target view).

        False when this attachment is no longer wanted, or when the other one
        is still not removed after a bounded time: then nothing is added now;
        the entities stay pending and are added once the other one is gone.
        """
        prefix = attached.source.prefix
        deadline = asyncio.get_running_loop().time() + _PREFIX_WAIT_SECONDS
        while attached.active:
            desired = self._sources()
            waiting = []
            for other, previous in tuple(self._active.items()):
                if other == key or previous.source.prefix != prefix:
                    continue
                wanted = desired.get(other)
                if (
                    previous.active
                    and wanted is not None
                    and wanted.value is previous.source.value
                ):
                    continue  # still shown: not one to wait for
                self._wake(other)
                job = self._jobs.get(other)
                if job is not None and job is not asyncio.current_task():
                    waiting.append(job)
            if not waiting:
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                _LOGGER.debug("Entities of a replaced source are still attached")
                for job in waiting:
                    # Try again once the other source is gone.
                    job.add_done_callback(partial(self._wake_after, key))
                return False
            await asyncio.wait(waiting, timeout=remaining)
        return False

    def _metrics_current(self, connection: str, metrics: RequestMetrics) -> bool:
        if self._closed:
            return False
        runtime = self.owner.connections
        return any(
            binding.connection_id == connection
            and isinstance(
                value := runtime.source_data(key), BoschBuderusDataUpdateCoordinator
            )
            and value.client.metrics is metrics
            for key, binding in tuple(runtime.snapshot.bindings.items())
        )

    def changed(self) -> None:
        if self._suspended:
            return
        sources = self._sources()
        for key in self._active.keys() | sources.keys():
            attached, desired = self._active.get(key), sources.get(key)
            if attached is not None and (
                desired is None or attached.source.value is not desired.value
            ):
                attached.invalidate()
            if (
                attached is None
                or desired is None
                or not attached.active
                or attached.source.value is not desired.value
                or attached.pending
            ):
                self._wake(key)

    def suspend(self) -> None:
        """C57: no attachment adds entities any more; they stay attached
        until the platform removes them."""
        self._suspended = True
        for attached in self._active.values():
            attached.invalidate()

    def resume(self) -> None:
        """The platform stays loaded: replace the suspended attachments."""
        if self._closed or not self._suspended:
            return
        self._suspended = False
        self.changed()

    def _wake(self, key: str) -> None:
        if self._suspended:
            return
        old = self._jobs.get(key)
        if old is not None and not old.done():
            return
        task = self.owner.hass.async_create_background_task(
            self._run(key), "home_comfort_entity_source", eager_start=False
        )
        self._jobs[key] = task

        def done(finished: asyncio.Task[None]) -> None:
            if self._jobs.get(key) is finished:
                self._jobs.pop(key)
            if finished.cancelled():
                return
            if (error := finished.exception()) is not None:
                # C61: once per series with the error class, details at debug.
                self._attach_failure.failed(error, key)
            else:
                self._attach_failure.succeeded(key)

        task.add_done_callback(done)

    async def _run(self, key: str) -> None:
        try:
            await self._reconcile(key)
        except BaseException:
            # A partial attachment must not retain listeners or prevent a later
            # source update from retrying through the same registry identity.
            if attached := self._active.get(key):
                await self._remove(attached)
                self._active.pop(key, None)
            raise

    async def _remove(self, attached: _Attachment) -> None:
        attached.invalidate()
        entities = [
            item
            for item in attached.entities
            if item.entity_id and self.platform.entities.get(item.entity_id) is item
        ]
        await asyncio.gather(*(item.async_remove() for item in entities))
        attached.entities.clear()

    async def _reconcile(self, key: str) -> None:
        while True:
            if self._suspended:
                return  # the platform unloads; it removes the entities
            desired = self._sources().get(key)
            attached = self._active.get(key)
            if attached is not None and (
                not attached.active
                or desired is None
                or attached.source.value is not desired.value
            ):
                await self._remove(attached)
                self._active.pop(key, None)
                # Removing entity listeners can yield; re-read current ownership.
                continue
            if desired is None:
                return
            if attached is None:
                attached = _Attachment(desired)
                desired.scope.async_on_unload = attached.listeners.append
                self._active[key] = attached

                def add(
                    new_entities: Iterable[Entity],
                    update_before_add: bool = False,
                    *,
                    config_subentry_id: str | None = None,
                    attached: _Attachment = attached,
                ) -> None:
                    if update_before_add or config_subentry_id is not None:
                        raise ValueError(
                            "Managed entities use their source's current data"
                        )
                    if not attached.active or not attached.source.scope.current():
                        return
                    use = entity_use(self.owner.hass, self.owner.entry)
                    for entity in new_entities:
                        identity = entity.unique_id
                        if not identity or not identity.startswith(
                            attached.source.prefix
                        ):
                            raise ConfigurationError("entity_source_identity_conflict")
                        # G3/O51: also an entity the registry keeps disabled.
                        use.note(self.name, entity)
                        if identity not in attached.seen:
                            attached.seen.add(identity)
                            attached.pending.append(entity)
                    self._wake(key)

                try:
                    self.build(desired.scope, add)
                except Exception:
                    attached.invalidate()
                    self._active.pop(key, None)
                    raise
            if not attached.pending:
                return
            if not await self._others_removed(key, attached):
                if attached.active:
                    return  # pending; added once the other source is gone
                continue
            entities, attached.pending = attached.pending, []
            if not attached.active or not attached.source.scope.current():
                continue
            # C60: a problem of one entity never blocks the others.
            entities = [
                entity for entity in entities if self._record(entity, attached.source)
            ]
            if not entities:
                continue
            attached.entities.extend(entities)
            await self.platform.async_add_entities(entities)
            await self.owner.inventory.save()

    def _record(self, entity: Entity, source: _Source) -> bool:
        """Record an entity before it is added; False: it is not added.

        An entity whose registry entry belongs to another entry is skipped,
        never claimed. The inventory is optional: an entity it cannot record
        (for example at its limit) is added anyway. Each problem is logged
        once per platform with its code, then at debug level.
        """
        assert entity.unique_id is not None
        registry = er.async_get(self.owner.hass)
        if identity := registry.async_get_entity_id(
            self.name, DOMAIN, entity.unique_id
        ):
            existing = registry.async_get(identity)
            assert existing is not None
            if existing.config_entry_id != self.owner.entry.entry_id:
                self._record_problem(
                    "entity_registry_owner_conflict",
                    "An entity registered for another entry is skipped",
                )
                return False
        context = source.context
        if context is None:
            # Account statistics belong to a connection, never to an appliance target.
            return True
        binding_id = context.generation.binding_id
        assert binding_id is not None
        binding = context.runtime.snapshot.bindings[binding_id]
        description = getattr(entity, "entity_description", None)
        path = getattr(description, "resource_path", None)
        try:
            self.owner.inventory.record(
                RetainedEntity(
                    self.name,
                    entity.unique_id,
                    binding.target_id,
                    binding_id,
                    binding.adapter,
                    entity.unique_id,
                    path,
                ),
                context,
            )
        except ConfigurationError as error:
            self._record_problem(
                str(error),  # a static code
                "An entity could not be recorded in the entity inventory; it is "
                "added anyway",
            )
        return True

    def _record_problem(self, code: str, message: str) -> None:
        if code in self._record_logged:
            _LOGGER.debug("%s (%s, %s)", message, code, self.name)
            return
        self._record_logged.add(code)
        _LOGGER.warning(
            "%s (%s, %s); further cases are logged at debug level",
            message,
            code,
            self.name,
        )

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unsubscribe()
        for attached in self._active.values():
            attached.invalidate()
        self.changed()

    async def close(self) -> None:
        self.stop()
        while self._jobs:
            await asyncio.gather(*tuple(self._jobs.values()), return_exceptions=True)
