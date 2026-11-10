"""C8 (CT-E02, CT-E03): read-only entities of the radio devices of an RRC
controller, each on its own device below the controller (E07)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import rrc, rrc_devices
from .coordinator import BoschBuderusDataUpdateCoordinator
from .device import device_info_for_resource, is_rrc_plant
from .i18n import localize
from .platform_scope import DiscoveryLifecycle
from .rrc_devices import RadioDevice

if TYPE_CHECKING:
    from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
else:
    # As in binary_sensor.py: older releases define it in the platform module.
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass


class RadioDeviceEntity(CoordinatorEntity[BoschBuderusDataUpdateCoordinator]):
    """One value of one listed radio device, read from the device list."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    key: ClassVar[str]
    names: ClassVar[tuple[str, str]]
    # O51: the path this entity reads, for the entity usage of the polling.
    resource_path = rrc.DEVICE_LIST

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, device_id: int
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:devices:device{device_id}:{self.key}"
        )
        self._attr_name = localize(coordinator.hass.config.language, *self.names)

    @staticmethod
    def wanted(device: RadioDevice) -> bool:
        """Whether a device reports this value at all."""
        raise NotImplementedError

    @property
    def _device(self) -> RadioDevice | None:
        snapshot = self.coordinator.display_snapshot(rrc.DEVICE_LIST)
        if snapshot is None or not snapshot.available:
            return None
        return rrc_devices.find({rrc.DEVICE_LIST: snapshot.resource}, self._device_id)

    @property
    def available(self) -> bool:
        # The displayed list decides, like for every other cloud reading;
        # a device that left the list is unavailable.
        return self._device is not None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(
            self.coordinator, f"/devices/device{self._device_id}"
        )


def track_radio_devices(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entity_class: type[RadioDeviceEntity],
) -> None:
    """Add the entities of newly listed devices once, also when found later."""
    seen: set[int] = set()

    @callback
    def discover() -> None:
        snapshot = (coordinator.data or {}).get(rrc.DEVICE_LIST)
        resource = (
            snapshot.resource
            if snapshot is not None and snapshot.available
            else coordinator.stored_resources().get(rrc.DEVICE_LIST)
        )
        devices = rrc_devices.parse_devices(resource)
        if not devices or not is_rrc_plant(coordinator):
            return
        additions: list[Entity] = []
        for device in devices:
            if (
                device.identifier in seen
                or device.is_controller
                or not entity_class.wanted(device)
            ):
                continue
            seen.add(device.identifier)
            additions.append(entity_class(coordinator, device.identifier))
        if additions:
            async_add_entities(additions)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class RadioSignalSensor(RadioDeviceEntity, SensorEntity):
    """CT-E03: the radio signal (0 to 100), a diagnostic disabled by default."""

    key = "signal"
    names = ("Funksignal", "Radio signal")
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:signal"

    @staticmethod
    def wanted(device: RadioDevice) -> bool:
        return device.signal is not None

    @property
    def native_value(self) -> float | int | None:
        device = self._device
        if device is None or device.signal is None:
            return None
        signal = device.signal
        return int(signal) if signal.is_integer() else signal


class RadioBatteryLowBinarySensor(RadioDeviceEntity, BinarySensorEntity):
    """CT-E02: on when the device reports a low battery; unknown stays
    unknown. Enabled by default, as a battery warning."""

    key = "battery"
    names = ("Batterie schwach", "Battery low")
    _attr_device_class = BinarySensorDeviceClass.BATTERY

    @staticmethod
    def wanted(device: RadioDevice) -> bool:
        return device.battery is not None

    @property
    def is_on(self) -> bool | None:
        device = self._device
        return device.battery_low if device is not None else None
