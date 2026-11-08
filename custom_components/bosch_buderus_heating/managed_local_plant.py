"""The entities of a plant from its local readings, without a cloud (P49b).

Without a working cloud a plant keeps its full local entities: every readable
local reading becomes an entity (``local_plant_view``). Readings of the pair
catalog and of paths the cloud offered keep the cloud's form and unique IDs,
so their history continues; all others are local-only sensors under the P45
rules. The view is read-only: it never writes, never requests anything from
the cloud and offers no controls.

``LocalPlantCoordinator`` reuses the lifecycle of a dormant cloud view (P49):
a dormant connection, the retained slot of the cloud binding and the managed
platforms. Its data is rebuilt from the local readings on every publication:

- The entity set only grows: an entity once offered stays, unavailable while
  its reading is not current, so a missing reading never removes it.
- Values are shown regardless of the ``local_values`` option.
- Counters follow the counter rule: without stored history a counter stays
  unavailable, and it never shows a number below the last one shown.
- The local error list alone decides the fault state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Iterable, Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import PollingProfile
from .coordinator import (
    COUNTER_HELD_CATEGORY,
    Freshness,
    ResourceSnapshot,
    SnapshotSource,
)
from .local_only_sensor import UNIQUE_ID_PART
from .local_plant_view import (
    build_local_plant_view,
    cloud_form,
    from_template,
    local_reading,
)
from .managed_pointt import ManagedPointTCoordinator
from .observations import canonical_unit
from .pointt import Gateway, PointTClient
from .pointt.exceptions import SourceStopped
from .pointt.metrics import RequestMetrics
from .pointt.models import Resource
from .pointt.transport import RateLimitBackoff
from .startup_data import StartupSession

if TYPE_CHECKING:
    from .connection_runtime import ConnectionRuntime
    from .discovery_inventory import DiscoveryInventory
    from .local_api.models import LocalObservation
    from .local_only import LocalOnlyDefinition
    from .managed_pointt import PointTSource
    from .observations import Observation

_LOGGER = logging.getLogger(__name__)

LOCAL_PLANT_MODE = "local_plant"
# A reading of the view that the local access does not supply right now.
LOCAL_NOT_CURRENT_CATEGORY = "local_not_current"
_COUNTER_CLASSES = frozenset(
    {SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING}
)


def registered_local_only_paths(
    hass: HomeAssistant, entry_id: str, gateway_id: str
) -> frozenset[str]:
    """Paths of the registered local-only sensors (P45) of one gateway."""
    prefix = f"{gateway_id}{UNIQUE_ID_PART}"
    registry = er.async_get(hass)
    return frozenset(
        "/" + entity.unique_id.removeprefix(prefix).replace(":", "/")
        for entity in er.async_entries_for_config_entry(registry, entry_id)
        if entity.domain == "sensor" and entity.unique_id.startswith(prefix)
    )


class _DefaultProfile:
    def polling_profile(self) -> PollingProfile:
        return PollingProfile.STANDARD


class TargetHost:
    """R1 (P49b): what a local plant view needs from a cloud source, for a
    system whose cloud access was removed. Detached from any connection: it
    never requests, never signs in and registers no request resources."""

    binding_id: str | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        runtime: ConnectionRuntime,
        target_id: str,
        gateway_id: str,
    ) -> None:
        from .managed_cloud import _DormantTokens

        self.connection = SimpleNamespace(
            hass=hass,
            entry=entry,
            runtime=runtime,
            connection_id=None,
            configuration=_DefaultProfile(),
            startup=StartupSession((gateway_id,)),
            tokens=_DormantTokens(),
            backoff=RateLimitBackoff(),
            resources=SimpleNamespace(
                metrics=RequestMetrics(), semaphore=asyncio.Semaphore(1)
            ),
        )
        self.binding = SimpleNamespace(target_id=target_id)

    def current(self) -> bool:
        return False

    def ensure_current(self) -> None:
        raise SourceStopped()

    def authentication_failed(self) -> None:
        """There is no sign-in to renew."""


async def async_build_local_plant(
    host: Any,
    gateway_id: str,
    inventory: DiscoveryInventory,
    *,
    model: str | None = None,
    brand: str | None = None,
) -> LocalPlantCoordinator | None:
    """Build and load a local plant view of the host's target, not kept yet.

    ``None`` when nothing could show the readings (no enabled local access)
    or for a wallbox; then nothing is built, loaded or written.
    """
    from .local_selection import LocalSelection
    from .wallbox import is_wallbox

    connection = host.connection
    target_id = host.binding.target_id
    if not LocalSelection(connection.runtime, target_id).has_local_access:
        return None
    stored = inventory.gateway(gateway_id)
    # The stored gateway at any age: its name and model, never a request.
    gateway = stored.gateway if stored is not None else Gateway(gateway_id, model=model)
    if is_wallbox(gateway):
        return None
    # D1/1A: descriptions kept beyond the age limit, and the resources within
    # it (with their shape); used only where they fit a reading.
    templates = {
        **inventory.templates(gateway_id),
        **inventory.resources(gateway_id),
    }
    owner = getattr(connection.entry, "runtime_data", None)
    records = getattr(getattr(owner, "inventory", None), "entities", ())
    # Every recorded cloud entity of the target, also of a removed access.
    cloud_paths = {
        item.resource_path
        for item in records
        if item.target_id == target_id
        and item.adapter == "pointt"
        and item.resource_path is not None
    } | templates.keys()
    client = PointTClient(
        async_get_clientsession(connection.hass),
        connection.tokens,
        metrics=connection.resources.metrics,
        backoff=connection.backoff,
        request_semaphore=connection.resources.semaphore,
        request_guard=host.ensure_current,
        gateway_id=gateway_id,
    )
    coordinator = LocalPlantCoordinator(
        host,
        client,
        gateway,
        cloud_paths=cloud_paths,
        local_only_paths=registered_local_only_paths(
            connection.hass, connection.entry.entry_id, gateway_id
        ),
        templates=templates,
    )
    coordinator.brand_hint = brand
    try:
        await coordinator.async_load_fault_state()
        coordinator.refresh_view()
    except Exception:
        # A half-built view never writes and never listens.
        coordinator.faults.discard()
        await coordinator.async_stop_source()
        raise
    return coordinator


def _signature_part(observation: LocalObservation) -> tuple[object, ...]:
    """What decides an entity's shape: never the value itself."""
    document = observation.document
    data = observation.data
    value = getattr(data, "value", None)
    parts = getattr(data, "values", None)
    names: tuple[str, ...] = ()
    if isinstance(parts, tuple | list):
        names = tuple(
            sorted(
                str(name)
                for item in parts
                if isinstance(item, Mapping)
                for name in item
            )
        )
    allowed = document.get("allowedValues")
    return (
        observation.request.path,
        observation.type,
        str(document.get("unitOfMeasure", document.get("unit"))),
        tuple(str(item) for item in allowed) if isinstance(allowed, list) else None,
        type(data).__name__,
        type(value).__name__,
        names,
    )


def _same_current(
    before: Mapping[str, LocalObservation], after: Mapping[str, LocalObservation]
) -> bool:
    """The same current readings with the same answers, read anew or not."""
    from .local_selection import same_answer

    return before.keys() == after.keys() and all(
        same_answer(before[path], item) for path, item in after.items()
    )


def _part_names(resource: Resource) -> set[str] | None:
    names: set[str] = set()
    for item in resource.values:
        if not isinstance(item, Mapping):
            return None
        names.update(item)
    return names


def _fits(current: Resource, shape: Resource) -> bool:
    """A new reading fits the entity created from the first one."""
    ours, theirs = current.metadata, shape.metadata
    if (
        ours.resource_type != theirs.resource_type
        or canonical_unit(ours.unit) != canonical_unit(theirs.unit)
        or isinstance(current.value, Mapping) != isinstance(shape.value, Mapping)
    ):
        return False
    if current.values and shape.values:
        # An array may be empty now; parts of a collection must stay the same.
        return _part_names(current) == _part_names(shape)
    return True


class LocalPlantCoordinator(ManagedPointTCoordinator):
    """A read-only plant view whose data comes from the local readings."""

    read_only_view = True

    def __init__(
        self,
        source: PointTSource | TargetHost,
        client: PointTClient,
        gateway: Gateway,
        *,
        cloud_paths: Collection[str] = (),
        local_only_paths: Collection[str] = (),
        templates: Mapping[str, Resource] | None = None,
    ) -> None:
        # A target host offers what the view uses of a source (R1).
        super().__init__(cast("PointTSource", source), client, gateway)
        # Never polls and never requests: the cloud is not used.
        self.update_interval = None
        self.startup_mode = LOCAL_PLANT_MODE
        self.faults.local_authority = True
        self._cloud_paths = frozenset(cloud_paths)
        self._local_only_paths = frozenset(local_only_paths)
        # D1: cloud resources of the stored inventory (within its age rules)
        # as templates; used only when compatible.
        self._templates = dict(templates or {})
        self._sensors: dict[str, LocalOnlyDefinition] = {}
        self._current: dict[str, LocalObservation] = {}
        self._shape_signature: dict[str, tuple[object, ...]] | None = None
        # Per path: the reading whose shape was worked out, and that shape.
        self._shape_parts: dict[str, tuple[LocalObservation, tuple[object, ...]]] = {}
        self._value_marker: dict[str, LocalObservation] | None = None
        # Per path: the reading last interpreted for a local-only sensor.
        self._readings: dict[str, tuple[LocalObservation, Observation | None]] = {}
        self._counter_paths: frozenset[str] = frozenset()
        self.data = {}
        self.last_update_success = False

    # ------------------------------------------------------------ view

    def local_plant_sensors(self) -> dict[str, LocalOnlyDefinition]:
        """Local-only sensors of the view by path; only grows."""
        return dict(self._sensors)

    def local_only_reading(self, path: str) -> Observation | None:
        """The current reading of one local-only sensor of the view.

        Interpreted once per reading: the sensor asks for it several times
        in every update pass.
        """
        observation = self._current.get(path) if path in self._sensors else None
        if observation is None:
            return None
        cached = self._readings.get(path)
        if cached is not None and cached[0] is observation:
            return cached[1]
        reading = local_reading(observation)
        self._readings[path] = (observation, reading)
        return reading

    @property
    def counter_paths(self) -> frozenset[str]:
        return self._counter_paths

    def refresh_view(self) -> bool:
        """Rebuild the view from the current local states; True if changed."""
        selection = self.local_selection
        states = selection.local_states() if selection is not None else ()
        shape: dict[str, LocalObservation] = {}
        current: dict[str, LocalObservation] = {}
        for state in states:
            if state is None:
                continue
            for observation in getattr(state, "last_observations", ()) or ():
                shape.setdefault(observation.request.path, observation)
            for observation in state.current_observations:
                current[observation.request.path] = observation
        shape.update(current)
        signature = {path: self._shape_part(item) for path, item in shape.items()}
        # The parts of readings no longer offered are not kept.
        for path in self._shape_parts.keys() - shape.keys():
            del self._shape_parts[path]
        grown = False
        if signature != self._shape_signature:
            self._shape_signature = signature
            grown = self._grow(shape.values())
        self._current = current
        for path in self._readings.keys() - current.keys():
            del self._readings[path]
        # A new read with the same answer changes nothing shown (G1).
        unchanged = self._value_marker is not None and _same_current(
            self._value_marker, current
        )
        self._value_marker = current
        if not grown and unchanged:
            return False
        self.data = self._snapshots(current)
        self.last_update_success = bool(current)
        return True

    def _shape_part(self, observation: LocalObservation) -> tuple[object, ...]:
        """The shape of one reading, worked out once per reading."""
        path = observation.request.path
        cached = self._shape_parts.get(path)
        if cached is not None and cached[0] is observation:
            return cached[1]
        part = _signature_part(observation)
        self._shape_parts[path] = (observation, part)
        return part

    def _registered_form(self, path: str) -> str | None:
        from .local_only_sensor import registered_form

        return registered_form(self.hass, self.gateway.gateway_id, path)

    def _grow(self, observations: Iterable[LocalObservation]) -> bool:
        view = build_local_plant_view(
            observations,
            templates=self._templates,
            cloud_paths=self._cloud_paths | self.resources.keys(),
            # A registered or already offered local-only sensor keeps its ID.
            local_only_paths=self._local_only_paths | self._sensors.keys(),
            registered=self._registered_form,
        )
        added = False
        for path, resource in view.resources.items():
            if path not in self.resources and path not in self._sensors:
                self.resources[path] = resource
                added = True
        for sensor in view.local_sensors:
            if sensor.path not in self._sensors and sensor.path not in self.resources:
                self._sensors[sensor.path] = sensor.definition
                added = True
        if added:
            self._counter_paths = self._find_counters()
        return added

    def _find_counters(self) -> frozenset[str]:
        from .local_selection import counter_path
        from .sensor import build_sensor_descriptions

        paths = {path for path in self.resources if counter_path(path)}
        paths.update(
            item.resource_path
            for item in build_sensor_descriptions(self.resources)
            if item.state_class in _COUNTER_CLASSES
        )
        return frozenset(paths)

    def _snapshots(
        self, current: Mapping[str, LocalObservation]
    ) -> dict[str, ResourceSnapshot]:
        now = dt_util.utcnow()
        previous = self.data or {}
        data: dict[str, ResourceSnapshot] = {}
        for path, shape in self.resources.items():
            observation = current.get(path)
            resource = (
                self._current_resource(observation, shape)
                if observation is not None
                else None
            )
            if observation is not None and resource is not None:
                data[path] = ResourceSnapshot(
                    resource,
                    True,
                    observation.observed_at,
                    source=SnapshotSource.LOCAL,
                    freshness=Freshness.FRESH,
                )
                continue
            # Kept, but unavailable: never a last value shown as current.
            last = previous.get(path)
            data[path] = ResourceSnapshot(
                last.resource if last is not None else shape,
                False,
                last.last_success if last is not None else now,
                source=SnapshotSource.LOCAL,
                freshness=Freshness.STALE,
                last_error_category=LOCAL_NOT_CURRENT_CATEGORY,
                ended=True,
            )
        return data

    @staticmethod
    def _current_resource(
        observation: LocalObservation, shape: Resource
    ) -> Resource | None:
        resource, _ = cloud_form(observation, cloud_path=True)
        if resource is None or not _fits(resource, shape):
            return None
        return from_template(resource, shape)

    # ------------------------------------------------------ publication

    @callback
    def _local_updated(self) -> None:
        selection = self.local_selection
        if selection is None or not (self.source.current() or self.local_retained):
            return
        changed = False
        try:
            changed = self.refresh_view()
        except Exception:
            # A broken reading must not stop the fault list or later readings.
            _LOGGER.exception("The local plant view could not be updated")
        faults_changed = False
        try:
            faults_changed = self._local_faults_updated(selection)
        except Exception:
            _LOGGER.exception("The local error list could not be applied")
        if changed or faults_changed:
            self.async_update_listeners()

    def display_snapshot(self, path: str) -> ResourceSnapshot | None:
        if path in self._counter_paths:
            # Decided once per update; never shown before the rule decided.
            return self._counter_views.get(path)
        return (self.data or {}).get(path)

    def displayed_cloud_snapshot(self, path: str) -> ResourceSnapshot | None:
        return self.display_snapshot(path)

    def source_snapshot(self, path: str, source: str) -> ResourceSnapshot | None:
        if source == "cloud":
            return None  # there is no cloud reading (P18 per-source entities)
        return super().source_snapshot(path, source)

    def _refresh_counter_views(self) -> None:
        """Local counters under the counter rule: the cloud set the baseline."""
        guard = self.counter_guard
        if guard is None:
            return
        views: dict[str, ResourceSnapshot | None] = {}
        for path in self._counter_paths:
            snapshot = (self.data or {}).get(path)
            if snapshot is None:
                continue
            # The local access is the sole source: a reading lacking some
            # parts (for example the consumption parts) may continue.
            chosen = guard.decide(path, (("local", snapshot),), partial=True)
            views[path] = (
                chosen
                if chosen is not None
                else replace(
                    snapshot,
                    available=False,
                    freshness=Freshness.STALE,
                    last_error_category=COUNTER_HELD_CATEGORY,
                )
            )
        self._counter_views = views

    @callback
    def _schedule_validity_refresh(self) -> None:
        """Local readings carry their own currency: no expiry timer."""

    async def _async_update_data(self) -> dict[str, ResourceSnapshot]:
        return dict(self.data or {})

    async def async_refresh(self) -> None:
        """Nothing to request: local readings arrive by themselves."""

    async def async_request_refresh(self) -> None:
        """Nothing to request: local readings arrive by themselves."""

    async def async_stop_source(self, *, retain: bool = False) -> None:
        """``retain`` keeps the view on its local readings (no cloud to stop)."""
        if not retain:
            self._end_view()
            await super().async_stop_source()
            return
        self.local_retained = True
        await self.async_shutdown()
        if not self.local_retained:
            return  # released while stopping; the release closed everything
        self.refresh_view()
        self.async_update_listeners()

    async def async_release_retention(self) -> None:
        if self.local_retained:
            self._end_view()
        await super().async_release_retention()

    async def async_hand_over_faults(self, owner: object | None = None) -> None:
        """The cloud starts again: it loads the fault baseline this view kept
        (review P49b). Local values stay until the cloud publishes."""
        await self.faults.async_hand_over(owner)

    async def async_resume_faults(self, owner: object | None = None) -> None:
        """The cloud did not take over: this view keeps the baseline again.

        Until then this view shows the handed-over faults unchanged and only
        the starting cloud processes the local list, so a change in that
        short window is announced once (A1).
        """
        await self.faults.async_resume(owner)

    def _end_view(self) -> None:
        """The view ends: nothing it showed stays available."""
        self.data = {
            path: replace(
                item,
                available=False,
                freshness=Freshness.STALE,
                last_error_category=LOCAL_NOT_CURRENT_CATEGORY,
                ended=True,
            )
            for path, item in (self.data or {}).items()
        }
        self._current = {}
        self._value_marker = None
        self._counter_views = dict.fromkeys(self._counter_paths)
        if self.counter_guard is not None:
            self.counter_guard.forget_shown()
        self.last_update_success = False
