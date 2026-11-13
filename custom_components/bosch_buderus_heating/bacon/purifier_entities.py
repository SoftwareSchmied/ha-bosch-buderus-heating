"""Native purifier readings and bounded controls on the shared Bacon session."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
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
from . import purifier
from .access import BaconAccess
from .entity import BaconEntity
from .models import BaconError, Device


def invalid_command() -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key="bacon_unsupported_command"
    )


class PurifierEntity(BaconEntity):
    def __init__(self, account: BaconAccess, serial: str, key: str) -> None:
        super().__init__(account, serial, "purifier_" + key)
        self.key = key
        self._attr_translation_key = "bacon_purifier_" + key

    @property
    def controls_current(self) -> bool:
        return (
            purifier.family(self.device) is not None
            and self.metadata_current
            and self.state_current
        )

    async def write(self, desired: dict[str, Any]) -> None:
        model = self.device.model
        if model is None or not self.controls_current:
            raise invalid_command()

        def validate(device: Device, patch: dict[str, Any]) -> bool:
            return self.controls_current and purifier.command_valid(
                device, patch, model
            )

        if not validate(self.device, desired):
            raise invalid_command()
        try:
            await self.account.write_state(self.serial, desired, validate)
        except BaconError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="bacon_command_failed"
            ) from err


def track_entities(
    entry: DiscoveryLifecycle,
    account: BaconAccess,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entity_type: type[PurifierEntity],
    fields: dict[str, str],
    *,
    sensor: bool = False,
) -> None:
    seen: set[tuple[str, str]] = set()

    @callback
    def discover() -> None:
        additions = []
        for serial, device in account.devices.items():
            if (
                purifier.family(device) is None
                or not account.available(serial)
                or device.online is not True
                or device.metadata_generation != account.generation
            ):
                continue
            for key, field in fields.items():
                identity = (serial, key)
                if identity in seen:
                    continue
                entity = entity_type(account, serial, key)
                if (
                    purifier.sensor_metadata(device, field) is not None
                    if sensor
                    else entity.controls_current
                    and purifier.control_available(device, field)
                ):
                    seen.add(identity)
                    additions.append(entity)
        if additions:
            async_add_entities(additions)

    entry.async_on_unload(account.add_listener(discover))
    discover()


class BaconPurifierSwitch(PurifierEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self) -> bool:
        return self.controls_current and purifier.control_available(
            self.device, purifier.SWITCHES[self.key]
        )

    @property
    def is_on(self) -> bool | None:
        return (
            purifier.reported(self.device, purifier.SWITCHES[self.key])
            if self.available
            else None
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.write({purifier.SWITCHES[self.key]: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.write({purifier.SWITCHES[self.key]: False})


class BaconPurifierNumber(PurifierEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, account: BaconAccess, serial: str, key: str) -> None:
        super().__init__(account, serial, key)
        if key == "light_intensity":
            self._attr_native_unit_of_measurement = "%"
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def limits(self) -> purifier.IntegerRange | None:
        return (
            purifier.number_range(self.device, purifier.NUMBERS[self.key])
            if self.controls_current
            else None
        )

    @property
    def available(self) -> bool:
        return self.controls_current and purifier.control_available(
            self.device, purifier.NUMBERS[self.key]
        )

    @property
    def native_value(self) -> int | None:
        return (
            purifier.number_value(self.device, purifier.NUMBERS[self.key])
            if self.available
            else None
        )

    @property
    def native_min_value(self) -> float:
        return limits.minimum if (limits := self.limits) else 0

    @property
    def native_max_value(self) -> float:
        return limits.maximum if (limits := self.limits) else 0

    @property
    def native_step(self) -> float:
        return limits.step if (limits := self.limits) else 1

    async def async_set_native_value(self, value: float) -> None:
        if (
            type(value) not in (int, float)
            or not 0 <= value <= 100
            or value != int(value)
        ):
            raise invalid_command()
        await self.write({purifier.NUMBERS[self.key]: int(value)})


class BaconPurifierColor(PurifierEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self) -> bool:
        return self.controls_current and purifier.control_available(
            self.device, "lightMode"
        )

    @property
    def options(self) -> list[str]:
        offered = purifier.options(self.device, "lightMode", purifier.COLORS)
        return (
            [key for key, wire in purifier.COLOR_OPTIONS.items() if wire in offered]
            if self.available
            else []
        )

    @property
    def current_option(self) -> str | None:
        value = purifier.reported(self.device, "lightMode")
        return next(
            (key for key in self.options if purifier.COLOR_OPTIONS[key] == value), None
        )

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise invalid_command()
        await self.write({"lightMode": purifier.COLOR_OPTIONS[option]})


class BaconPurifierSensor(PurifierEntity, SensorEntity):
    def __init__(self, account: BaconAccess, serial: str, key: str) -> None:
        super().__init__(account, serial, key)
        self._attr_device_class = {
            "temperature": SensorDeviceClass.TEMPERATURE,
            "humidity": SensorDeviceClass.HUMIDITY,
            "pm2_5": SensorDeviceClass.PM25,
            "air_quality": SensorDeviceClass.ENUM,
            "filter_status": SensorDeviceClass.ENUM,
        }.get(key)
        self._attr_native_unit_of_measurement = {
            "temperature": "°C",
            "humidity": "%",
            "pm2_5": "µg/m³",
            "filter_life": "%",
        }.get(key)
        if key in ("filter_status", "air_quality"):
            self._attr_options = (
                list(purifier.QUALITY.values())
                if key == "air_quality"
                else ["ok", "warning", "error"]
            )
        else:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if key in ("filter_life", "filter_status"):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return (
            purifier.family(self.device) is not None
            and self.metadata_current
            and self.sensor_current
            and purifier.reading(self.device, self.key) is not None
        )

    @property
    def native_value(self) -> str | int | float | None:
        result = purifier.reading(self.device, self.key) if self.available else None
        return result[0] if result is not None else None
