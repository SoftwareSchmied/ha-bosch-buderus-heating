"""Sensor entities for readings only the local access provides (P45)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING, Protocol, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .control import HeldCapabilities
from .device import device_info_for_resource, grouped_entity_name
from .local_only import (
    BELOW_MEASURING_RANGE,
    CONCENTRATION,
    ENERGY_MANAGER_STATUS_PATH,
    ENUM_KINDS,
    FLAG,
    FLOW,
    GENERIC,
    HUMIDITY,
    MAX_STATES,
    MEASURING_RANGE,
    PERCENT,
    POWER,
    ROTATION,
    TEMPERATURE,
    LocalOnlyDefinition,
    below_measuring_range,
    definition_for,
    diagnostic,
    energy_manager_not_connected,
    energy_manager_setpoint,
    entity_name,
    native_value,
    state_class,
    state_options,
    static_unit,
    unit_and_class,
)
from .observations import ValueStatus

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .local_api.models import LocalObservation
    from .local_selection import LocalSelection
    from .observations import Observation
    from .platform_scope import DiscoveryLifecycle

UNIQUE_ID_PART = ":local_only:"
# Integration-owned entity registry options of a local-only sensor (8C-c).
OPTIONS_KEY = DOMAIN
NATIVE_UNIT = "native_unit"
# Readings offered as generic local-only sensors because of their type (3A).
NUMBER_TYPES = frozenset({"floatValue", "integerValue"})
# Running hand-overs (8C-c while running) per entry and gateway, by path.
_HAND_OVERS: HassKey[dict[tuple[str | None, str], dict[str, asyncio.Task[None]]]] = (
    HassKey(f"{DOMAIN}.local_only_hand_overs")
)
# Local-only entities handed to Home Assistant but not added yet, per entry
# and gateway: path and when. A cloud sensor of such a path waits (bounded).
_PENDING_ADDS: HassKey[dict[tuple[str | None, str], dict[str, float]]] = HassKey(
    f"{DOMAIN}.local_only_pending_adds"
)
PENDING_ADD_SECONDS = 60.0
_PRECISION = {
    TEMPERATURE: 1,
    POWER: 0,
    FLOW: 0,
    PERCENT: 0,
    HUMIDITY: 0,
    ROTATION: 0,
    CONCENTRATION: 0,
}
# #73: translates the measuring range attribute of the volume flows.
FLOW_TRANSLATION_KEY = "volume_flow"


def local_only_suffix(path: str) -> str:
    """The unique ID of a local-only reading after ``<gateway>:``."""
    return f"{UNIQUE_ID_PART[1:]}{path.strip('/').replace('/', ':')}"


def local_only_unique_id(gateway_id: str, path: str) -> str:
    return f"{gateway_id}:{local_only_suffix(path)}"


class BoschBuderusLocalOnlySensor(
    HeldCapabilities,
    CoordinatorEntity["BoschBuderusDataUpdateCoordinator"],
    SensorEntity,
):
    """One reading that only the local access of this plant provides."""

    _attr_has_entity_name = True
    # O30: the native unit of the last reading, kept while there is none.
    _held_unit: str | None = None

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        path: str,
        definition: LocalOnlyDefinition,
    ) -> None:
        super().__init__(coordinator)
        self._path = path
        self._definition = definition
        self._options_seen: tuple[str, ...] = definition.options
        language = coordinator.hass.config.language
        self._attr_name = grouped_entity_name(
            coordinator, path, entity_name(definition, path, language)
        )
        self._attr_unique_id = local_only_unique_id(
            coordinator.gateway.gateway_id, path
        )
        self._attr_entity_registry_enabled_default = definition.enabled
        self._attr_translation_key = definition.translation_key or (
            FLOW_TRANSLATION_KEY if definition.kind == FLOW else None
        )
        self._attr_state_class = state_class(definition)
        # Fixed, so the shown precision does not depend on whether a reading
        # had arrived when Home Assistant registered the entity.
        self._attr_suggested_display_precision = _PRECISION.get(definition.kind)
        if definition.suggested_unit is not None and static_unit(definition)[0]:
            # C7-4: applies when Home Assistant registers the entity (an
            # existing entry keeps the unit it shows); only with a fixed
            # unit, so it is valid also before the first reading.
            self._attr_suggested_unit_of_measurement = definition.suggested_unit
        if diagnostic(definition):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if definition.kind in ENUM_KINDS:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(definition.options)

    @property
    def resource_path(self) -> str:
        return self._path

    def _reading(self) -> Observation | None:
        if (plant := _plant_view(self.coordinator)) is not None:
            # P49b: the local plant view also offers documented readings
            # without a reviewed entry.
            return plant.local_only_reading(self._path)
        selection = self.coordinator.local_selection
        return selection.local_only_reading(self._path) if selection else None

    @property
    def options(self) -> list[str] | None:
        """C5: the reviewed states and every state the device declared since
        Home Assistant started; options only grow while it runs."""
        if self._definition.kind not in ENUM_KINDS:
            return None
        current = state_options(self._definition, self._reading())
        grown = tuple(dict.fromkeys((*self._options_seen, *current)))
        self._options_seen = grown[:MAX_STATES]
        return list(self._options_seen)

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self._path)

    def _shape(
        self,
    ) -> tuple[int | float | str | None, str | None, SensorDeviceClass | None] | None:
        """Value, unit and device class of the current reading; None without one."""
        if self._path in self.coordinator.resources:
            return None  # the cloud now offers it: that entity takes over
        reading = self._reading()
        if reading is None:
            return None
        definition = self._definition
        if definition.kind in ENUM_KINDS:
            return native_value(definition, reading), None, SensorDeviceClass.ENUM
        value = native_value(definition, reading)
        if value is not None and self._energy_manager_placeholder():
            value = None  # #76: no energy manager, so no setpoint of it
        if reading.status is ValueStatus.UNKNOWN_UNIT:
            return value, None, None  # C7: the number, without an unknown unit
        mapped = unit_and_class(definition, reading.unit)
        if mapped is None:
            return None, None, None  # another quantity: never shown
        unit, device_class = mapped
        return value, unit, device_class

    def _energy_manager_placeholder(self) -> bool:
        """#76: a setpoint of an energy manager the system reports as not
        connected; its 0 °C is a placeholder, not a setpoint.

        The status as its own entity shows it: the local reading first, else
        the cloud reading within its validity limit. The stored cloud copy
        alone would lag: it is read only every 15 minutes while the local
        reading is shown (O45).
        """
        if not energy_manager_setpoint(self._path):
            return False
        status = self.coordinator.display_snapshot(ENERGY_MANAGER_STATUS_PATH)
        return (
            status is not None
            and status.available
            and energy_manager_not_connected(status.resource.value)
        )

    @property
    def available(self) -> bool:
        """Only a current local reading; there is no cloud value to fall back to."""
        return self._shape() is not None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """#73: a flow below the sensor's measuring range is unknown; the
        attribute tells it apart from a missing reading."""
        if self._definition.kind in ENUM_KINDS or self._shape() is None:
            return None
        reading = self._reading()
        if reading is not None and below_measuring_range(reading):
            return {MEASURING_RANGE: BELOW_MEASURING_RANGE}
        return None

    @property
    def native_value(self) -> int | float | str | None:
        shape = self._shape()
        value = shape[0] if shape is not None else None
        if self._definition.kind in ENUM_KINDS and value not in (self.options or ()):
            return None  # never a state outside the options
        return value

    @property
    def native_unit_of_measurement(self) -> str | None:
        shape = self._shape()
        if shape is not None and shape[1] is not None:
            self._held_unit = shape[1]
            return shape[1]
        if (unit := static_unit(self._definition)[0]) is not None:
            return unit
        if self._held_unit is None and (entry := self.registry_entry) is not None:
            # The unit stored with the last reading before a restart (8C-c).
            stored = entry.options.get(OPTIONS_KEY)
            unit = stored.get(NATIVE_UNIT) if stored is not None else None
            self._held_unit = unit if isinstance(unit, str) else None
        return self._held_unit

    @property
    def state_class(self) -> SensorStateClass | None:
        """Generic signals: a measurement when their unit is one (#80)."""
        if self._definition.kind != GENERIC:
            return self._attr_state_class
        return state_class(self._definition, self.native_unit_of_measurement)

    @property
    def device_class(self) -> SensorDeviceClass | None:
        if self._definition.kind in ENUM_KINDS:
            return SensorDeviceClass.ENUM
        shape = self._shape()
        if shape is not None and shape[2] is not None:
            return shape[2]
        return static_unit(self._definition)[1]

    async def async_update(self) -> None:
        """Local readings arrive by themselves; never start a cloud poll (P40)."""

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self._remember_unit()

    @callback
    def _remember_unit(self) -> None:
        """Keep the native unit of the last reading, for a later hand-over
        to the cloud's sensor (8C-c, P49b).

        Home Assistant registers only the displayed unit, and only when the
        entity is added, possibly before its first reading.
        """
        entry = self.registry_entry
        if entry is None or self.hass is None or self._shape() is None:
            return
        unit = _text(self.native_unit_of_measurement)
        if unit is None and self._definition.kind not in ENUM_KINDS:
            return  # another quantity or no unit now: keep the last real one
        stored = dict(entry.options.get(OPTIONS_KEY) or {})
        if NATIVE_UNIT not in stored or stored[NATIVE_UNIT] != unit:
            er.async_get(self.hass).async_update_entity_options(
                self.entity_id, OPTIONS_KEY, {**stored, NATIVE_UNIT: unit}
            )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        selection = self.coordinator.local_selection
        if selection is not None:
            selection.track_local_only(self._path)
            self.async_on_remove(partial_untrack(selection, self._path))
        self._remember_unit()


def installation_date_unique_id(gateway_id: str) -> str:
    """C7-3: the derived installation date of a gateway."""
    return f"{gateway_id}:installation_date"


INSTALLATION_DATE_PATHS = frozenset(
    f"/signals/SC.InstallationDate.{part}" for part in ("Day", "Month", "Year")
)


class BoschBuderusInstallationDateSensor(
    CoordinatorEntity["BoschBuderusDataUpdateCoordinator"],
    SensorEntity,
):
    """C7-3: the installation date, from its three local signals.

    The year counts from 2000. Unknown while a part is missing, reports a
    declared marker (for example INVALID) or the parts give no real date.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.DATE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        german = coordinator.hass.config.language.casefold().startswith("de")
        self._attr_name = grouped_entity_name(
            coordinator,
            "/system",
            "Installationsdatum" if german else "Installation date",
        )
        self._attr_unique_id = installation_date_unique_id(
            coordinator.gateway.gateway_id
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/system")

    def _part(self, name: str) -> int | None:
        path = f"/signals/SC.InstallationDate.{name}"
        reading: Observation | None
        if (plant := _plant_view(self.coordinator)) is not None:
            reading = plant.local_only_reading(path)
        else:
            selection = self.coordinator.local_selection
            reading = selection.local_only_reading(path) if selection else None
        if reading is None or reading.status not in (
            ValueStatus.VALID,
            ValueStatus.UNKNOWN_UNIT,
        ):
            return None  # missing, or a declared marker (STATE_MARKER)
        value = reading.value
        if isinstance(value, bool) or not isinstance(value, int | Decimal):
            return None
        return int(value) if value == int(value) else None

    @property
    def native_value(self) -> date | None:
        day, month, year = (self._part(name) for name in ("Day", "Month", "Year"))
        if day is None or month is None or year is None or not 0 <= year <= 99:
            return None
        try:
            return date(2000 + year, month, day)
        except ValueError:
            return None

    @property
    def available(self) -> bool:
        return self.native_value is not None

    async def async_update(self) -> None:
        """Local readings arrive by themselves; never start a cloud poll."""


class _LocalPlantView(Protocol):
    def local_plant_sensors(self) -> Mapping[str, LocalOnlyDefinition]: ...

    def local_only_reading(self, path: str) -> Observation | None: ...


def _plant_view(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> _LocalPlantView | None:
    """The coordinator as a local plant view (P49b), else None."""
    if getattr(coordinator, "read_only_view", False) is not True:
        return None
    return cast("_LocalPlantView", coordinator)


def partial_untrack(selection: LocalSelection, path: str) -> Callable[[], None]:
    @callback
    def untrack() -> None:
        selection.untrack_local_only(path)

    return untrack


def track_local_only_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an entity for each local-only path the local access could read.

    At setup, entries of a removed local access and of paths the cloud now
    offers are removed. While running, a removed access only makes the
    entities unavailable (a reload cleans up), so adding it again restores
    them with their settings.
    """
    seen: set[str] = set()
    superseded: set[str] = set()
    date_added: list[bool] = []
    if (plant := _plant_view(coordinator)) is not None:
        _track_plant_sensors(entry, coordinator, plant, async_add_entities)
        return
    selection = coordinator.local_selection
    if selection is not None:
        clean_registered_local_only(coordinator)
        # 4A (P49b): entities the local plant view created for documented
        # readings without a reviewed entry stay served; none are added.
        selection.serve_registered_local_only(_registered_extended(coordinator))

    @callback
    def discover() -> None:
        selection = coordinator.local_selection
        if selection is None or not selection.has_configured_local_access:
            return
        with selection.update_pass():
            additions: list[SensorEntity] = []
            for path in sorted(selection.local_only_observations()):
                if path in seen or path in coordinator.resources:
                    continue  # a cloud resource keeps its own entity
                observation = selection.local_only_observations()[path]
                from .local_plant_view import local_sensor

                # C7: the same rules as the local plant view; C10-b: the
                # registered form, undecided ones are asked again later.
                sensor, _ = local_sensor(
                    observation,
                    registered=partial(
                        registered_form,
                        coordinator.hass,
                        coordinator.gateway.gateway_id,
                    ),
                )
                if sensor is None:
                    continue
                seen.add(path)
                additions.append(
                    BoschBuderusLocalOnlySensor(coordinator, path, sensor.definition)
                )
            if not date_added and seen >= INSTALLATION_DATE_PATHS:
                date_added.append(True)
                additions.append(BoschBuderusInstallationDateSensor(coordinator))
        # A path the cloud offers later (for example after a new discovery):
        # its cloud entity takes over; the entry is handed over when that is
        # unambiguous (8C-c), otherwise removed as before.
        if taken := (seen & set(coordinator.resources)) - superseded:
            superseded.update(taken)
            hand_over_while_running(coordinator, taken)
        if additions:
            _note_pending_adds(
                coordinator,
                [
                    item.resource_path
                    for item in additions
                    if isinstance(item, BoschBuderusLocalOnlySensor)
                ],
            )
            async_add_entities(additions)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


def clean_registered_local_only(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> None:
    """At setup, before any entity of the platform is added: hand over or
    remove the entries of paths the cloud offers, and remove those of a
    removed local access. Repeating it changes nothing."""
    if _plant_view(coordinator) is not None:
        return
    selection = coordinator.local_selection
    if selection is not None:
        _remove_registered(
            coordinator, everything=not selection.has_configured_local_access
        )


def _track_plant_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    plant: _LocalPlantView,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """P49b: the local-only sensors of the local plant view.

    The view decides which readings are sensors and only adds to that set.
    Registered entries are never removed here: the view is read-only and
    keeps the registered IDs.
    """
    seen: set[str] = set()

    @callback
    def discover() -> None:
        additions: list[SensorEntity] = [
            BoschBuderusLocalOnlySensor(coordinator, path, definition)
            for path, definition in sorted(plant.local_plant_sensors().items())
            if path not in seen and path not in coordinator.resources
        ]
        seen.update(path for path in plant.local_plant_sensors() if path not in seen)
        if "date" not in seen and seen >= INSTALLATION_DATE_PATHS:
            seen.add("date")
            additions.append(BoschBuderusInstallationDateSensor(coordinator))
        if additions:
            async_add_entities(additions)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


def _registered_extended(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> frozenset[str]:
    """Registered local-only paths without a reviewed entry (4A)."""
    from .local_plant_view import private_path

    entry = coordinator.config_entry
    if entry is None:
        return frozenset()
    prefix = f"{coordinator.gateway.gateway_id}{UNIQUE_ID_PART}"
    registry = er.async_get(coordinator.hass)
    paths = {
        "/" + entity.unique_id.removeprefix(prefix).replace(":", "/")
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        if entity.domain == "sensor" and entity.unique_id.startswith(prefix)
    }
    return frozenset(
        path
        for path in paths
        if definition_for(path) is None and not private_path(path)
    )


def _registered_definition(
    observation: LocalObservation,
) -> LocalOnlyDefinition | None:
    """The definition the local plant view gave a registered reading."""
    from .local_plant_view import local_sensor

    sensor, _ = local_sensor(observation)
    return sensor.definition if sensor is not None else None


def registered_form(hass: HomeAssistant, gateway_id: str, path: str) -> str | None:
    """C10-b: the form a registered entity of a two-form signal has.

    Enum options mean the on/off form, a temperature class or unit the
    temperature; so a restart never switches the form of the same ID.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, local_only_unique_id(gateway_id, path)
    )
    entry = registry.async_get(entity_id) if entity_id is not None else None
    if entry is None:
        return None
    if (entry.capabilities or {}).get("options"):
        return FLAG
    if entry.original_device_class == SensorDeviceClass.TEMPERATURE or (
        entry.unit_of_measurement
        in (UnitOfTemperature.CELSIUS, UnitOfTemperature.KELVIN)
    ):
        return TEMPERATURE
    return None


def _remove_registered(
    coordinator: BoschBuderusDataUpdateCoordinator,
    *,
    everything: bool = False,
) -> None:
    """At setup: remove the entries of a removed local access, or of paths
    the cloud offers.

    8C-c (P49b): the entry of a path the cloud now offers becomes the entry
    of the cloud's sensor when that is unambiguous, so its entity ID, name,
    area and recorded history continue. Otherwise it is removed as before.
    While running, ``hand_over_while_running`` applies the same rules.
    """
    entry = coordinator.config_entry
    if entry is None:
        return
    gateway_id = coordinator.gateway.gateway_id
    prefix = f"{gateway_id}{UNIQUE_ID_PART}"
    cloud = {
        local_only_unique_id(gateway_id, path): path for path in coordinator.resources
    }
    registry = er.async_get(coordinator.hass)
    successors: dict[str, list[_CloudSensor]] | None = None
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            everything
            and entity.domain == "sensor"
            and entity.unique_id == installation_date_unique_id(gateway_id)
        ):
            # C7-3: derived from local readings alone.
            registry.async_remove(entity.entity_id)
            continue
        if not (
            entity.domain == "sensor"
            and entity.unique_id.startswith(prefix)
            and (everything or entity.unique_id in cloud)
        ):
            continue
        path = cloud.get(entity.unique_id)
        if path is not None:
            if successors is None:
                successors = _cloud_sensors(coordinator)
            successor = _successor(registry, entity, successors, path)
            if successor is not None:
                registry.async_update_entity(entity.entity_id, new_unique_id=successor)
                # The stored local unit served only this comparison.
                registry.async_update_entity_options(
                    entity.entity_id, OPTIONS_KEY, None
                )
                continue
        registry.async_remove(entity.entity_id)


@callback
def hand_over_while_running(
    coordinator: BoschBuderusDataUpdateCoordinator, paths: Collection[str]
) -> tuple[frozenset[str], asyncio.Future[list[BaseException | None]]] | None:
    """8C-c while running: the cloud now offers these paths.

    Each registered local-only entry of them is handed over to the cloud's
    sensor by the same rules as at setup, so entity ID, name, area and
    recorded history continue; otherwise it is removed as before. The
    running local-only entity leaves first, so the cloud's sensor can take
    its entity ID. Returns the affected paths and what to wait for before
    their cloud sensors are added; ``None`` when no path is affected.
    """
    entry = getattr(coordinator, "config_entry", None)
    if entry is None:
        return None
    hass = coordinator.hass
    gateway_id = coordinator.gateway.gateway_id
    registry = er.async_get(hass)
    running = hass.data.setdefault(_HAND_OVERS, {}).setdefault(
        (entry.entry_id, gateway_id), {}
    )
    waiting = {path: running[path] for path in paths if path in running}
    # A local-only entity still being added counts as present: its cloud
    # sensor waits for a later update instead of taking another entity ID.
    pending = _pending_adds(coordinator, paths)
    fresh = frozenset(
        path
        for path in paths
        if path not in running
        and path not in pending
        and registry.async_get_entity_id(
            "sensor", DOMAIN, local_only_unique_id(gateway_id, path)
        )
        is not None
    )
    if fresh:
        task = hass.async_create_task(
            _async_hand_over(coordinator, fresh),
            "home_comfort_local_only_hand_over",
            eager_start=False,
        )
        for path in fresh:
            running[path] = task
            waiting[path] = task

        @callback
        def finished(done: asyncio.Task[None]) -> None:
            for path in fresh:
                if running.get(path) is done:
                    del running[path]

        task.add_done_callback(finished)
    if not waiting and not pending:
        return None
    return frozenset(waiting) | pending, asyncio.gather(
        *set(waiting.values()), return_exceptions=True
    )


@callback
def hand_over_open(coordinator: BoschBuderusDataUpdateCoordinator, path: str) -> bool:
    """The local-only entity of a path is still added, being added or kept.

    After a hand-over its entry is gone (continued by the cloud sensor or
    removed); if it is still there, the hand-over failed or was skipped and
    the cloud sensor must not take another entity ID now.
    """
    registry = er.async_get(coordinator.hass)
    unique_id = local_only_unique_id(coordinator.gateway.gateway_id, path)
    return registry.async_get_entity_id(
        "sensor", DOMAIN, unique_id
    ) is not None or path in _pending_adds(coordinator, (path,))


def _adds_key(coordinator: BoschBuderusDataUpdateCoordinator) -> tuple[str | None, str]:
    entry = getattr(coordinator, "config_entry", None)
    return (
        entry.entry_id if entry is not None else None,
        coordinator.gateway.gateway_id,
    )


@callback
def _note_pending_adds(
    coordinator: BoschBuderusDataUpdateCoordinator, paths: Collection[str]
) -> None:
    pending = coordinator.hass.data.setdefault(_PENDING_ADDS, {}).setdefault(
        _adds_key(coordinator), {}
    )
    now = monotonic()
    for path in paths:
        pending[path] = now


@callback
def _pending_adds(
    coordinator: BoschBuderusDataUpdateCoordinator, paths: Collection[str]
) -> frozenset[str]:
    """Paths whose local-only entity was handed to Home Assistant recently
    and is neither added nor registered as disabled yet."""
    from homeassistant.helpers.entity_platform import async_get_platforms

    pending = coordinator.hass.data.get(_PENDING_ADDS, {}).get(_adds_key(coordinator))
    if not pending:
        return frozenset()
    registry = er.async_get(coordinator.hass)
    now = monotonic()
    result = set()
    for path in paths:
        since = pending.get(path)
        if since is None:
            continue
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, local_only_unique_id(coordinator.gateway.gateway_id, path)
        )
        entry = registry.async_get(entity_id) if entity_id is not None else None
        added = entity_id is not None and any(
            platform.domain == "sensor" and entity_id in platform.entities
            for platform in async_get_platforms(coordinator.hass, DOMAIN)
        )
        if (
            not 0 <= now - since <= PENDING_ADD_SECONDS
            or added
            or (entry is not None and entry.disabled_by is not None)
        ):
            del pending[path]  # added, never added (disabled) or given up
            continue
        result.add(path)
    return frozenset(result)


async def _async_hand_over(
    coordinator: BoschBuderusDataUpdateCoordinator, paths: frozenset[str]
) -> None:
    from homeassistant.helpers.entity_platform import async_get_platforms

    hass = coordinator.hass
    registry = er.async_get(hass)
    gateway_id = coordinator.gateway.gateway_id
    successors = _cloud_sensors(coordinator)
    for path in sorted(paths):
        unique_id = local_only_unique_id(gateway_id, path)
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        entity = registry.async_get(entity_id) if entity_id is not None else None
        if entity_id is None or entity is None:
            continue
        successor = _successor(registry, entity, successors, path)
        for platform in async_get_platforms(hass, DOMAIN):
            if platform.domain == "sensor" and (
                current := platform.entities.get(entity_id)
            ):
                # Leaves the registry entry untouched.
                await current.async_remove(force_remove=True)
        if registry.async_get_entity_id("sensor", DOMAIN, unique_id) != entity_id:
            continue  # changed meanwhile (for example removed by the user)
        if (
            successor is not None
            and registry.async_get_entity_id("sensor", DOMAIN, successor) is None
        ):
            registry.async_update_entity(entity_id, new_unique_id=successor)
            # The stored local unit served only this comparison.
            registry.async_update_entity_options(entity_id, OPTIONS_KEY, None)
        else:
            registry.async_remove(entity_id)


@dataclass(frozen=True, slots=True)
class _CloudSensor:
    unique_id: str
    native_unit: str | None
    state_class: str | None


def _cloud_sensors(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> dict[str, list[_CloudSensor]]:
    """Unique ID, native unit and state class of the cloud's sensors, by path."""
    from .sensor import build_sensor_descriptions, sensor_unique_suffix

    result: dict[str, list[_CloudSensor]] = {}
    for description in build_sensor_descriptions(coordinator.resources):
        result.setdefault(description.resource_path, []).append(
            _CloudSensor(
                f"{coordinator.gateway.gateway_id}:{sensor_unique_suffix(description)}",
                description.native_unit_of_measurement,
                _text(description.state_class),
            )
        )
    return result


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _unit(unit: str | None) -> str | None:
    from .unit_contracts import TEMPERATURE_UNITS, canonical_unit

    return TEMPERATURE_UNITS.get(unit or "", canonical_unit(unit))


def _successor(
    registry: er.EntityRegistry,
    entity: er.RegistryEntry,
    successors: Mapping[str, list[_CloudSensor]],
    path: str,
) -> str | None:
    """The cloud sensor's unique ID if it may continue this entry (8C-c).

    Native units are compared, never displayed ones: a unit chosen in Home
    Assistant or by the unit system does not matter. Without a stored native
    unit the entry is not handed over. A state class must stay the same, so
    long-term statistics are never orphaned; a reading without one (and so
    without statistics) may gain the cloud's.
    """
    candidates = successors.get(path, [])
    if len(candidates) != 1:
        return None  # none or several sensors for this reading: ambiguous
    cloud = candidates[0]
    stored: Mapping[str, object] = entity.options.get(OPTIONS_KEY) or {}
    statistics = _text((entity.capabilities or {}).get("state_class"))
    if (
        registry.async_get_entity_id("sensor", DOMAIN, cloud.unique_id) is not None
        or entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        or NATIVE_UNIT not in stored
        or _unit(_text(stored[NATIVE_UNIT])) != _unit(cloud.native_unit)
        or (statistics is not None and statistics != cloud.state_class)
    ):
        return None
    return cloud.unique_id
