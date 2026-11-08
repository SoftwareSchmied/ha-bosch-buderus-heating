"""Separate Dry-family observations, child-lock and display controls."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ..const import DOMAIN
from ..platform_scope import DiscoveryLifecycle
from . import dry
from .access import BaconAccess
from .entity import BaconEntity
from .metadata import metadata
from .models import BaconError, Device


def invalid_command() -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key="bacon_unsupported_command"
    )


class DryEntity(BaconEntity):
    def __init__(self, account: BaconAccess, serial: str, key: str) -> None:
        super().__init__(account, serial, "dry_" + key)
        self.key = key
        self._attr_translation_key = "bacon_dry_" + key

    @property
    def controls_current(self) -> bool:
        return (
            dry.family(self.device) is not None
            and self.state_current
            and self.metadata_current
        )

    @property
    def observations_current(self) -> bool:
        return (
            dry.family(self.device) is not None
            and self.sensor_current
            and self.metadata_current
        )

    async def write(self, desired: dict[str, Any]) -> None:
        model = self.device.model
        if model is None or not self.controls_current:
            raise invalid_command()

        def validate(device: Device, patch: dict[str, Any]) -> bool:
            return self.controls_current and dry.command_valid(device, patch, model)

        if not validate(self.device, desired):
            raise invalid_command()
        try:
            await self.account.write_state(self.serial, desired, validate)
        except BaconError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="bacon_command_failed"
            ) from err

    @property
    def operating_status(self) -> str | None:
        return (
            dry.operating_status(self.device)
            if self.controls_current and self.observations_current
            else None
        )


def track_entities(
    entry: DiscoveryLifecycle,
    account: BaconAccess,
    async_add_entities: AddConfigEntryEntitiesCallback,
    platform: str,
) -> None:
    seen: set[tuple[str, str]] = set()

    @callback
    def discover() -> None:
        additions: list[DryEntity] = []
        for serial, device in account.devices.items():
            if dry.family(device) is None:
                continue
            candidates: list[DryEntity] = []
            if platform == "sensor":
                candidates.append(BaconDrySensor(account, serial, "operating_status"))
                if device.metadata_generation == account.generation:
                    for key, field in (
                        ("humidity", "relHumidity"),
                        ("temperature", "roomTemperature"),
                    ):
                        if metadata(device, "sensor", field) is not None:
                            candidates.append(BaconDrySensor(account, serial, key))
            elif platform == "binary_sensor":
                candidates.append(BaconDryStatus(account, serial, "device_error"))
                if device.metadata_generation == account.generation:
                    for key, field in dry.FLAGS.items():
                        if metadata(device, "sensor", field) is not None:
                            candidates.append(BaconDryStatus(account, serial, key))
            elif platform == "switch":
                for key in dry.SWITCHES:
                    switch = BaconDrySwitch(account, serial, key)
                    if switch.available:
                        candidates.append(switch)
            for entity in candidates:
                identity = (serial, entity.key)
                if identity not in seen:
                    seen.add(identity)
                    additions.append(entity)
        if additions:
            async_add_entities(additions)

    entry.async_on_unload(account.add_listener(discover))
    discover()


class BaconDrySwitch(DryEntity, SwitchEntity):
    """Child lock or display, each sent as its own single-field command."""

    _attr_entity_category = EntityCategory.CONFIG

    @property
    def field(self) -> str:
        return dry.SWITCHES[self.key]

    @property
    def available(self) -> bool:
        return self.controls_current and dry.boolean_control(self.device, self.field)

    @property
    def is_on(self) -> bool | None:
        return dry.reported(self.device, self.field) if self.available else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.write({self.field: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.write({self.field: False})


class BaconDryStatus(DryEntity, BinarySensorEntity):
    @property
    def available(self) -> bool:
        if self.key == "device_error":
            return (
                dry.family(self.device) is not None
                and self.live
                and self.device.error_present is not None
            )
        return self.observations_current and dry.flag(self.device, self.key) is not None

    @property
    def is_on(self) -> bool | None:
        if not self.available:
            return None
        return (
            self.device.error_present
            if self.key == "device_error"
            else dry.flag(self.device, self.key)
        )


class BaconDrySensor(DryEntity, SensorEntity):
    def __init__(self, account: BaconAccess, serial: str, key: str) -> None:
        super().__init__(account, serial, key)
        if key == "operating_status":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(dry.STATUSES)
        else:
            self._attr_device_class = (
                SensorDeviceClass.HUMIDITY
                if key == "humidity"
                else SensorDeviceClass.TEMPERATURE
            )
            self._attr_native_unit_of_measurement = "%" if key == "humidity" else "°C"
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def available(self) -> bool:
        if self.key == "operating_status":
            return self.operating_status is not None
        return self.observations_current and self.native_value is not None

    @property
    def native_value(self) -> str | float | None:
        if self.key == "operating_status":
            return self.operating_status
        field = "relHumidity" if self.key == "humidity" else "roomTemperature"
        return (
            dry.measurement(self.device, field) if self.observations_current else None
        )
