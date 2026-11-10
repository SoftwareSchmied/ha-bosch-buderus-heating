"""Native water heaters for PointT circuits and identified Bacon DHW families."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
)
from homeassistant.const import ATTR_TEMPERATURE, Platform, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.unit_conversion import TemperatureDeltaConverter

from . import BoschBuderusConfigEntry, rrc_platforms
from .bacon import dhw
from .bacon.access import BaconAccess
from .bacon.entity import BaconEntity
from .bacon.models import BaconError, Device
from .const import DOMAIN
from .ha_compat import temperature_members, temperature_unit
from .platform_scope import PlatformScope, setup_platform
from .pointt_water_heater import track_pointt_water_heaters

if TYPE_CHECKING:
    from homeassistant.components.water_heater.const import WaterHeaterEntityFeature
else:
    from homeassistant.components.water_heater import WaterHeaterEntityFeature


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_platform(entry, Platform.WATER_HEATER, async_add_entities, _setup_entities)


def _setup_entities(
    entry: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for coordinator in entry.coordinators:
        track_pointt_water_heaters(entry, coordinator, async_add_entities)
        rrc_platforms.track(
            Platform.WATER_HEATER, entry, coordinator, async_add_entities
        )
    if (account := entry.bacon) is None:
        return
    seen: set[str] = set()

    @callback
    def discover() -> None:
        additions = []
        for serial, device in account.devices.items():
            if serial not in seen and dhw.family(device) is not None:
                seen.add(serial)
                additions.append(BaconWaterHeater(account, serial))
        if additions:
            async_add_entities(additions)

    entry.async_on_unload(account.add_listener(discover))
    discover()


@temperature_members
class BaconWaterHeater(BaconEntity, WaterHeaterEntity):
    _attr_translation_key = "bacon_dhw"
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = 1.0

    def __init__(self, account: BaconAccess, serial: str) -> None:
        super().__init__(account, serial, "water_heater")

    @property
    def available(self) -> bool:
        return (
            dhw.family(self.device) is not None
            and not self.device.uncertain
            and (self.state_current or self.sensor_current)
        )

    @property
    def current_operation(self) -> str | None:
        return dhw.operation(self.device) if self.state_current else None

    @property
    def native_current_temperature(self) -> float | None:
        if not self.sensor_current or not self.metadata_current:
            return None
        return dhw.temperature(
            self.device.sensor.get("currentTemp"),
            dhw.metadata(self.device, "sensor", "currentTemp"),
        )

    @property
    def native_target_temperature(self) -> float | None:
        return (
            dhw.target(self.device)
            if self.state_current and self.metadata_current
            else None
        )

    @property
    def operation_list(self) -> list[str]:
        if (
            not self.metadata_current
            or not self.state_current
            or self.device.state is None
            or type(self.device.state.reported.get("powerEnabled")) is not bool
        ):
            return []
        return dhw.operation_list(self.device)

    @property
    def supported_features(self) -> WaterHeaterEntityFeature:
        result = WaterHeaterEntityFeature(0)
        if self.operation_list:
            result |= WaterHeaterEntityFeature.OPERATION_MODE
        if self.native_target_temperature is not None and dhw.writable(
            dhw.metadata(self.device, "state", "tempSetpoint")
        ):
            result |= WaterHeaterEntityFeature.TARGET_TEMPERATURE
        return result

    @property
    def min_temp(self) -> float:
        limits = dhw.target_range(self.device) if self.metadata_current else None
        return limits.minimum if limits else 0

    @property
    def max_temp(self) -> float:
        limits = dhw.target_range(self.device) if self.metadata_current else None
        return limits.maximum if limits else 0

    @property
    def target_temperature_step(self) -> float | None:
        limits = dhw.target_range(self.device) if self.metadata_current else None
        return limits.step if limits else None

    @property
    def capability_attributes(self) -> dict[str, Any]:
        attributes = super().capability_attributes
        # HA converts absolute temperatures, but passes this interval through.
        if (step := self.target_temperature_step) is not None:
            attributes["target_temp_step"] = TemperatureDeltaConverter.convert(
                step, temperature_unit(self), self.hass.config.units.temperature_unit
            )
        return attributes

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if "operation_mode" in kwargs:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="bacon_separate_settings",
            )
        value = kwargs.get(ATTR_TEMPERATURE)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0 <= value <= 100
            or abs(value - round(value)) > 1e-7
        ):
            raise _invalid_command()
        await self._write({"tempSetpoint": round(value)})

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        name = dhw.family(self.device)
        if operation_mode not in self.operation_list or name is None:
            raise _invalid_command()
        desired: dict[str, Any] = {"powerEnabled": operation_mode != "off"}
        if operation_mode != "off":
            desired["opMode"] = dhw.MODES[name][operation_mode]
        await self._write(desired)

    async def _write(self, desired: dict[str, Any]) -> None:
        name = dhw.family(self.device)
        if name is None or not self.metadata_current or not self.state_current:
            raise _invalid_command()

        def validate(device: Device, patch: dict[str, Any]) -> bool:
            return (
                self.metadata_current
                and self.state_current
                and dhw.command_valid(device, patch, name)
            )

        if not validate(self.device, desired):
            raise _invalid_command()
        try:
            await self.account.write_state(self.serial, desired, validate)
        except BaconError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="bacon_command_failed",
            ) from err


def _invalid_command() -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key="bacon_unsupported_command"
    )
