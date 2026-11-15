"""Battery and radio signal of the radio room devices of a zone controller.

A ConnectKey zone controller (for example an MX400) reports each radio room
device below ``/devices/<id>`` with ``battery`` (a state such as
``BAT_NORMAL`` or ``BAT_LOW``) and ``signal`` (a level in dB). Room
controllers (RRC) report their devices in a list of their own and keep their
own entities.

The battery is a diagnostic binary sensor, enabled by default as a warning:
on for ``BAT_LOW`` and ``BAT_TOO_LOW``, off for ``BAT_NORMAL`` (``low`` and
``ok`` as well; any case). Any other state (for example ``NO_BAT``,
``BAT_MEDIUM`` or ``BAT_FOTA_FORBIDDEN``) stays unknown and is shown as
``reported_state`` when the device offers it. The signal is a diagnostic
sensor in dB, disabled by default. Expected, untested on a real device.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any, ClassVar, Final

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS, EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import BoschBuderusDataUpdateCoordinator
from .device import device_info_for_resource, grouped_entity_name
from .heating_climate import valid_resource
from .i18n import localize
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource
from .writes import context_of

if TYPE_CHECKING:
    from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
else:
    # As in binary_sensor.py: older releases define it in the platform module.
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass

_DEVICE_LEAF: Final = re.compile(r"^/devices/(?!list$)([^/]+)/(battery|signal)$")
# The states the devices report (their allowed values), casefolded.
BATTERY_LOW: Final = frozenset({"bat_low", "bat_too_low", "low"})
BATTERY_OK: Final = frozenset({"bat_normal", "ok"})
# Plausible limits of a radio level in dB; anything else is no reading.
_SIGNAL_RANGE: Final = (-200.0, 200.0)


def battery_reading(resource: Resource | None) -> str | None:
    """The battery text of a valid reading, ``None`` otherwise."""
    if (
        resource is None
        or resource.metadata.resource_type != "stringValue"
        or not isinstance(resource.value, str)
        or not resource.value.isprintable()
    ):
        return None
    return resource.value.strip()


def signal_reading(resource: Resource | None) -> float | None:
    """The level in dB of a valid reading, ``None`` otherwise."""
    if (
        resource is None
        or resource.metadata.resource_type != "floatValue"
        or (resource.metadata.unit or "").casefold() != "db"
        or isinstance(resource.value, bool)
        or not isinstance(resource.value, int | float)
        or not math.isfinite(resource.value)
        or resource.value in resource.metadata.numeric_error_values
        or not _SIGNAL_RANGE[0] <= resource.value <= _SIGNAL_RANGE[1]
    ):
        return None
    return float(resource.value)


class ZoneRadioEntity(CoordinatorEntity[BoschBuderusDataUpdateCoordinator]):
    """One value of one radio room device of a zone controller."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    leaf: ClassVar[str]
    names: ClassVar[tuple[str, str]]

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, device: str
    ) -> None:
        super().__init__(coordinator)
        self.device = device
        self.resource_path = f"/devices/{device}/{self.leaf}"
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:devices:{device}:{self.leaf}"
        )
        self._attr_name = grouped_entity_name(
            coordinator,
            self.resource_path,
            localize(coordinator.hass.config.language, *self.names),
        )

    @staticmethod
    def wanted(resource: Resource) -> bool:
        raise NotImplementedError

    @property
    def _resource(self) -> Resource | None:
        return valid_resource(self.coordinator, self.resource_path)

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.resource_path)


class ZoneRadioBatteryBinarySensor(ZoneRadioEntity, BinarySensorEntity):
    """On when the device reports a low battery."""

    leaf = "battery"
    names = ("Batterie schwach", "Battery low")
    _attr_device_class = BinarySensorDeviceClass.BATTERY

    @staticmethod
    def wanted(resource: Resource) -> bool:
        return battery_reading(resource) is not None

    @property
    def available(self) -> bool:
        return battery_reading(self._resource) is not None

    @property
    def is_on(self) -> bool | None:
        state = battery_reading(self._resource)
        key = state.casefold() if state is not None else None
        if key in BATTERY_LOW:
            return True
        return False if key in BATTERY_OK else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        resource = self._resource
        state = battery_reading(resource)
        if resource is None or state is None:
            return {}
        # Only a text the device itself offers, never free text.
        if state not in resource.metadata.allowed_values:
            return {}
        return {"reported_state": state}


class ZoneRadioSignalSensor(ZoneRadioEntity, SensorEntity):
    """The radio level of the device in dB."""

    leaf = "signal"
    names = ("Funksignal", "Radio signal")
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    @staticmethod
    def wanted(resource: Resource) -> bool:
        return signal_reading(resource) is not None

    @property
    def available(self) -> bool:
        return signal_reading(self._resource) is not None

    @property
    def native_value(self) -> float | None:
        return signal_reading(self._resource)


def track_zone_radio_devices(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entity_class: type[ZoneRadioEntity],
) -> None:
    """Add the entity of every device that reports the value, once."""
    seen: set[str] = set()

    @callback
    def discover() -> None:
        if context_of(coordinator).rrc_plant:
            return  # a room controller's devices have entities of their own
        resources = {
            path: snapshot.resource
            for path, snapshot in (coordinator.data or {}).items()
            if snapshot.available
        }
        for path, resource in coordinator.stored_resources().items():
            resources.setdefault(path, resource)
        additions: list[Entity] = []
        for path, resource in resources.items():
            match = _DEVICE_LEAF.fullmatch(path)
            if (
                match is None
                or match.group(2) != entity_class.leaf
                or match.group(1) in seen
                or not entity_class.wanted(resource)
            ):
                continue
            seen.add(match.group(1))
            additions.append(entity_class(coordinator, match.group(1)))
        if additions:
            async_add_entities(additions)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))
