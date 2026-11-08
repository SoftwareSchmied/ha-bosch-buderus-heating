"""Shared identity and freshness for separately connected cloud devices."""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from ..const import DOMAIN
from ..device import registered_device
from .access import BaconAccess
from .models import device_key
from .session import SENSOR_STALE_AFTER, STATE_STALE_AFTER

if TYPE_CHECKING:
    from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
else:
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass


class BaconEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, account: BaconAccess, serial: str, suffix: str) -> None:
        self.account, self.serial = account, serial
        self.device = account.devices[serial]
        assert account.client.subject is not None
        self._identity = device_key(account.client.subject, serial)
        self._attr_unique_id = self._identity + ":" + suffix

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.account.add_listener(self.async_write_ha_state))

    @property
    def live(self) -> bool:
        return self.account.available(self.serial) and self.device.online is True

    @property
    def metadata_current(self) -> bool:
        return self.live and self.device.metadata_generation == self.account.generation

    @property
    def state_current(self) -> bool:
        self.account.expire_values(self.device)
        return (
            self.live
            and not self.device.uncertain
            and self.device.state_generation == self.account.generation
            and monotonic() - self.device.state_received_at <= STATE_STALE_AFTER
            and self.device.state is not None
            and bool(self.device.state.reported)
        )

    @property
    def sensor_current(self) -> bool:
        self.account.expire_values(self.device)
        return (
            self.live
            and self.device.sensor_generation == self.account.generation
            and monotonic() - self.device.sensor_received_at <= SENSOR_STALE_AFTER
        )

    @property
    def device_info(self) -> DeviceInfo:
        info = DeviceInfo(
            identifiers={(DOMAIN, self._identity)},
            manufacturer="Bosch Thermotechnik",
        )
        if self.hass is None or not registered_device(self.hass, self._identity):
            # C8: a new device is named without the brand.
            info["name"] = f"Appliance (…{self.serial[-6:]})"
        return info


class BaconConnectivity(BaconEntity, BinarySensorEntity):
    _attr_translation_key = "bacon_connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, account: BaconAccess, serial: str) -> None:
        super().__init__(account, serial, "connectivity")

    @property
    def available(self) -> bool:
        return self.account.available(self.serial)

    @property
    def is_on(self) -> bool | None:
        return self.device.online if self.available else None
