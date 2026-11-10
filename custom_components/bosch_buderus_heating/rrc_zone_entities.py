"""RRC 2.0 entities: zone climate, temporary setpoint, zone details.

Owned by work package W1. ``rrc_platforms.track`` calls the tracker of each
platform listed in ``TRACKERS`` once per coordinator; see
``docs/dev/rrc-packages.md``. Every tracker waits for an RRC controller
(S11). The open window switches and numbers are generic contract controls
(``rrc_contract_zone``); this module adds:

- climate: one ``RrcZoneClimate`` per zone with a mode and a room temperature;
- button: "clear temporary setpoint" per zone with a temporary setpoint;
- sensor: next setpoint, minutes to it, zone status, valve opening and the
  optimum start (CT-C06, CT-G07);
- binary sensor: window open.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    Platform,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import rrc, rrc_contract_zone, rrc_contracts
from .const import DOMAIN
from .control import async_set_control, track_rrc_entities
from .coordinator import BoschBuderusDataUpdateCoordinator
from .device import device_info_for_resource, grouped_entity_name
from .i18n import localize
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource
from .rrc_climate import RrcZoneClimate, zone_reading
from .rrc_platforms import Tracker
from .writes import ControlContext, context_of, number_policy_for_resource

if TYPE_CHECKING:
    from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
else:
    # As in binary_sensor.py: older releases define it in the platform module.
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass

Z = rrc_contract_zone


@dataclass(frozen=True, kw_only=True)
class ZoneEntityDescription(EntityDescription):
    """One zone (``key`` is its root) or one zone leaf (``key`` is its path)."""

    root: str


@dataclass(frozen=True, kw_only=True)
class ZoneSensorDescription(SensorEntityDescription):
    """A read-only zone detail; ``key`` is its resource path."""

    field: str


# Field -> translation key, unit, device class, state class, enabled,
# diagnostic. Activation and names are proposals for the entity review.
@dataclass(frozen=True, slots=True)
class _Detail:
    translation_key: str
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    enabled: bool = True
    diagnostic: bool = False
    icon: str | None = None


_SENSORS: Final[Mapping[str, _Detail]] = MappingProxyType(
    {
        Z.NEXT_SETPOINT: _Detail(
            "rrc_next_setpoint",
            UnitOfTemperature.CELSIUS,
            SensorDeviceClass.TEMPERATURE,
            SensorStateClass.MEASUREMENT,
        ),
        Z.MINUTES_TO_NEXT: _Detail(
            "rrc_minutes_to_next_setpoint",
            UnitOfTime.MINUTES,
            SensorDeviceClass.DURATION,
            enabled=False,
        ),
        Z.STATUS: _Detail(
            "rrc_zone_status",
            device_class=SensorDeviceClass.ENUM,
            icon="mdi:home-thermometer-outline",
        ),
        Z.VALVE_POSITION: _Detail(
            "rrc_zone_valve_position",
            PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            enabled=False,
            icon="mdi:valve",
        ),
        Z.OPTIMUM_STATE: _Detail(
            "rrc_optimum_start_state", enabled=False, diagnostic=True
        ),
        Z.OPTIMUM_RATE: _Detail(
            "rrc_optimum_start_heatup_rate",
            state_class=SensorStateClass.MEASUREMENT,
            enabled=False,
            diagnostic=True,
        ),
    }
)
STATUS_OPTIONS: Final = tuple(dict.fromkeys(Z.ZONE_STATUS.values()))


def _leaf_name(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str, key: str
) -> str:
    return grouped_entity_name(
        coordinator, path, localize(coordinator.hass.config.language, *Z.NAMES[key])
    )


def _key(path: str) -> str:
    return path.strip("/").replace("/", ":")


class _ZoneLeafEntity(CoordinatorEntity[BoschBuderusDataUpdateCoordinator]):
    """One read-only zone leaf, shown while its reading is valid (O101)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, path: str, field: str
    ) -> None:
        super().__init__(coordinator)
        # O51: the path this entity reads, for the entity usage of the polling.
        self.resource_path = path
        self._field = field
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{_key(path)}"
        self._attr_name = _leaf_name(coordinator, path, field)

    @property
    def _resource(self) -> Resource | None:
        resource = zone_reading(self.coordinator, self.resource_path)
        return resource if resource is not None and rrc.detail_shape(resource) else None

    @property
    def available(self) -> bool:
        return self._resource is not None

    @property
    def _value(self) -> str | float | bool | None:
        resource = self._resource
        return rrc.detail_value(resource) if resource is not None else None

    @property
    def device_info(self) -> DeviceInfo:
        root = Z.zone_root(self.resource_path) or self.resource_path
        return device_info_for_resource(self.coordinator, root)


class RrcZoneSensor(_ZoneLeafEntity, SensorEntity):
    """Next setpoint (P07: 999 is unknown), minutes to it, zone status
    (unknown values stay unknown), valve opening, optimum start."""

    entity_description: ZoneSensorDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: ZoneSensorDescription,
    ) -> None:
        super().__init__(coordinator, description.key, description.field)
        self.entity_description = description

    @property
    def native_value(self) -> str | float | None:
        value = self._value
        if isinstance(value, bool):
            return None
        return value

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self._field != Z.OPTIMUM_RATE:
            return self.entity_description.native_unit_of_measurement
        # The heat-up rate keeps the controller's own unit (expected "s/K").
        resource = zone_reading(self.coordinator, self.resource_path)
        unit = resource.metadata.unit if resource is not None else None
        return unit if isinstance(unit, str) and 0 < len(unit) <= 8 else None


class RrcWindowOpenSensor(_ZoneLeafEntity, BinarySensorEntity):
    """On while the zone reports an open window; other values are unknown."""

    _attr_device_class = BinarySensorDeviceClass.WINDOW
    _attr_translation_key = "rrc_window_open"

    @property
    def is_on(self) -> bool | None:
        value = self._value
        return value if isinstance(value, bool) else None


class RrcOverrideClearButton(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], ButtonEntity
):
    """CT-C04: clear the temporary setpoint of a zone (write 0.0).

    Offered only in the mode ``clock`` while a temporary setpoint is active;
    the read-back must show 0. Expected, untested.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "rrc_override_clear"
    _attr_icon = "mdi:timer-off-outline"

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, root: str
    ) -> None:
        super().__init__(coordinator)
        self.root = root
        self.resource_path = f"{root}/{Z.OVERRIDE_LEAF}"
        self.extra_resource_paths = (f"{root}/userMode",)
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:{_key(self.resource_path)}:clear"
        )
        self._attr_name = _leaf_name(coordinator, self.resource_path, "clear_override")

    def _state(self, *, strict: bool) -> tuple[Resource, Resource] | None:
        """The active temporary setpoint and the mode ``clock``, if both are."""
        override = zone_reading(self.coordinator, self.resource_path, strict=strict)
        mode = zone_reading(self.coordinator, f"{self.root}/userMode", strict=strict)
        if (
            override is None
            or mode is None
            or rrc.scalar_value(mode) != "clock"
            or rrc.scalar_value(override) in (None, 0)
            or number_policy_for_resource(override, context_of(self.coordinator))
            is not Z.OVERRIDE_POLICY
        ):
            return None
        return override, mode

    @property
    def available(self) -> bool:
        return self._state(strict=False) is not None

    async def async_press(self) -> None:
        state = self._state(strict=True)
        if state is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="rrc_override_not_active"
            )
        override, mode = state
        await async_set_control(
            self.coordinator, override.path, 0.0, Z.OVERRIDE_POLICY, expected=(mode,)
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.root)


# ------------------------------------------------------------- builders
def climate_descriptions(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[ZoneEntityDescription, ...]:
    """A zone with a mode and a room temperature gets a climate entity."""
    del context
    return tuple(
        ZoneEntityDescription(key=root, root=root)
        for root in rrc.climate_roots(resources)
    )


def button_descriptions(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[ZoneEntityDescription, ...]:
    """A zone with a writable temporary setpoint gets the clear button."""
    descriptions = []
    for root in rrc.zone_roots(resources):
        override = resources.get(f"{root}/{Z.OVERRIDE_LEAF}")
        if (
            override is not None
            and override.metadata.writable
            and override.metadata.resource_type == "floatValue"
            and rrc_contracts.policy_for(override, context) is Z.OVERRIDE_POLICY
        ):
            descriptions.append(ZoneEntityDescription(key=root, root=root))
    return tuple(descriptions)


def sensor_descriptions(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[ZoneSensorDescription, ...]:
    del context
    descriptions = []
    for path, resource in resources.items():
        field = rrc.detail_field(path)
        detail = _SENSORS.get(field or "")
        if field is None or detail is None or not rrc.detail_shape(resource):
            continue
        descriptions.append(
            ZoneSensorDescription(
                key=path,
                field=field,
                translation_key=detail.translation_key,
                native_unit_of_measurement=detail.unit,
                device_class=detail.device_class,
                state_class=detail.state_class,
                options=list(STATUS_OPTIONS) if field == Z.STATUS else None,
                entity_registry_enabled_default=detail.enabled,
                entity_category=EntityCategory.DIAGNOSTIC
                if detail.diagnostic
                else None,
                icon=detail.icon,
            )
        )
    return tuple(descriptions)


def window_descriptions(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[ZoneEntityDescription, ...]:
    del context
    return tuple(
        ZoneEntityDescription(key=path, root=Z.zone_root(path) or path)
        for path, resource in resources.items()
        if rrc.detail_field(path) == Z.WINDOW_STATUS and rrc.detail_shape(resource)
    )


# -------------------------------------------------------------- trackers
def _climate(
    coordinator: BoschBuderusDataUpdateCoordinator, description: ZoneEntityDescription
) -> Entity:
    return RrcZoneClimate(coordinator, description.root)


def _button(
    coordinator: BoschBuderusDataUpdateCoordinator, description: ZoneEntityDescription
) -> Entity:
    return RrcOverrideClearButton(coordinator, description.root)


def _sensor(
    coordinator: BoschBuderusDataUpdateCoordinator, description: ZoneSensorDescription
) -> Entity:
    return RrcZoneSensor(coordinator, description)


def _window(
    coordinator: BoschBuderusDataUpdateCoordinator, description: ZoneEntityDescription
) -> Entity:
    return RrcWindowOpenSensor(coordinator, description.key, Z.WINDOW_STATUS)


def track_climates(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(entry, coordinator, add, climate_descriptions, _climate)


def track_buttons(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(entry, coordinator, add, button_descriptions, _button)


def track_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(entry, coordinator, add, sensor_descriptions, _sensor)


def track_windows(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(entry, coordinator, add, window_descriptions, _window)


TRACKERS: Mapping[Platform, Tracker] = MappingProxyType(
    {
        Platform.CLIMATE: track_climates,
        Platform.BUTTON: track_buttons,
        Platform.SENSOR: track_sensors,
        Platform.BINARY_SENSOR: track_windows,
    }
)
