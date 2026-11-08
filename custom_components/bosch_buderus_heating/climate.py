"""Native climate controls for identified Bacon Midea Climate Series devices."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature as Feature
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    Platform,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.unit_conversion import TemperatureDeltaConverter

from . import BoschBuderusConfigEntry
from .bacon import rac
from .bacon.access import BaconAccess
from .bacon.entity import BaconEntity
from .bacon.metadata import metadata, writable
from .bacon.models import BaconError, Device
from .const import DOMAIN
from .ha_compat import temperature_members, temperature_unit
from .heating_climate import track_heating_climates
from .platform_scope import PlatformScope, setup_platform


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_platform(entry, Platform.CLIMATE, async_add_entities, _setup_entities)


def _setup_entities(
    entry: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for coordinator in entry.coordinators:
        track_heating_climates(entry, coordinator, async_add_entities)
    if (account := entry.bacon) is None:
        return
    seen: set[str] = set()

    @callback
    def discover() -> None:
        additions = []
        for serial, device in account.devices.items():
            if serial not in seen and rac.supported(device):
                seen.add(serial)
                additions.append(BaconClimate(account, serial))
        if additions:
            async_add_entities(additions)

    entry.async_on_unload(account.add_listener(discover))
    discover()


@temperature_members
class BaconClimate(BaconEntity, ClimateEntity):
    _attr_translation_key = "bacon_rac"
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = 0.1
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, account: BaconAccess, serial: str) -> None:
        super().__init__(account, serial, "climate")

    @property
    def available(self) -> bool:
        return (
            rac.supported(self.device)
            and not self.device.uncertain
            and (self.state_current or self.sensor_current)
        )

    @property
    def controls_current(self) -> bool:
        return (
            rac.supported(self.device) and self.metadata_current and self.state_current
        )

    @property
    def hvac_mode(self) -> HVACMode | None:
        return (
            rac.mode(self.device)
            if rac.supported(self.device) and self.state_current
            else None
        )

    @property
    def hvac_modes(self) -> list[HVACMode]:
        return rac.modes(self.device) if self.controls_current else []

    @property
    def native_current_temperature(self) -> float | None:
        return (
            rac.current_temperature(self.device)
            if rac.supported(self.device)
            and self.sensor_current
            and self.metadata_current
            else None
        )

    @property
    def native_target_temperature(self) -> float | None:
        return rac.target(self.device) if self.controls_current else None

    @property
    def min_temp(self) -> float:
        limits = rac.target_range(self.device) if self.controls_current else None
        return limits.minimum if limits else 0

    @property
    def max_temp(self) -> float:
        limits = rac.target_range(self.device) if self.controls_current else None
        return limits.maximum if limits else 0

    @property
    def target_temperature_step(self) -> float | None:
        limits = rac.target_range(self.device) if self.controls_current else None
        return limits.step if limits else None

    @property
    def capability_attributes(self) -> dict[str, Any]:
        attributes = super().capability_attributes or {}
        if (step := self.target_temperature_step) is not None:
            attributes["target_temp_step"] = TemperatureDeltaConverter.convert(
                step, temperature_unit(self), self.hass.config.units.temperature_unit
            )
        return attributes

    @property
    def fan_modes(self) -> list[str]:
        if (
            not self.controls_current
            or not writable(metadata(self.device, "state", "fanSpeed"))
            or not isinstance(rac.reported(self.device, "fanSpeed"), str)
        ):
            return []
        return rac.options(self.device, "fanSpeed", rac.FANS)

    @property
    def fan_mode(self) -> str | None:
        value = rac.reported(self.device, "fanSpeed")
        return (
            value
            if self.controls_current and isinstance(value, str) and value in rac.FANS
            else None
        )

    def _swing_modes(self, field: str) -> list[str]:
        return (
            ["off", "on"]
            if self.controls_current and rac.boolean_control(self.device, field)
            else []
        )

    def _swing(self, field: str) -> str | None:
        value = rac.reported(self.device, field)
        return (
            ("on" if value else "off")
            if self.controls_current and type(value) is bool
            else None
        )

    @property
    def swing_modes(self) -> list[str]:
        return self._swing_modes("vSwingEnabled")

    @property
    def swing_mode(self) -> str | None:
        return self._swing("vSwingEnabled")

    @property
    def swing_horizontal_modes(self) -> list[str]:
        return self._swing_modes("hSwingEnabled")

    @property
    def swing_horizontal_mode(self) -> str | None:
        return self._swing("hSwingEnabled")

    @property
    def preset_modes(self) -> list[str] | None:
        # Offered only when the device declares Eco writable and reports it.
        if self.controls_current and rac.boolean_control(self.device, rac.ECO):
            return [rac.PRESET_NONE, rac.PRESET_ECO]
        return None

    @property
    def preset_mode(self) -> str | None:
        value = rac.reported(self.device, rac.ECO)
        if not self.preset_modes or type(value) is not bool:
            return None
        return rac.PRESET_ECO if value else rac.PRESET_NONE

    @property
    def supported_features(self) -> Feature:
        features = Feature(0)
        if not self.controls_current:
            return features
        if self.preset_modes:
            features |= Feature.PRESET_MODE
        if (
            self.native_target_temperature is not None
            and writable(metadata(self.device, "state", "tempSetpoint"))
            and rac.reported(self.device, "opMode") in rac.TEMPERATURE_MODES
        ):
            features |= Feature.TARGET_TEMPERATURE
        if self.fan_modes:
            features |= Feature.FAN_MODE
        if self.swing_modes:
            features |= Feature.SWING_MODE
        if self.swing_horizontal_modes:
            features |= Feature.SWING_HORIZONTAL_MODE
        if rac.boolean_control(self.device, "powerEnabled"):
            features |= Feature.TURN_OFF
            if rac.reported(self.device, "opMode") in rac.options(
                self.device, "opMode", tuple(rac.MODES.values())
            ):
                features |= Feature.TURN_ON
        return features

    async def async_turn_on(self) -> None:
        await self._write({"powerEnabled": True})

    async def async_turn_off(self) -> None:
        await self._write({"powerEnabled": False})

    def _mode_patch(self, hvac_mode: str) -> dict[str, Any]:
        if hvac_mode not in self.hvac_modes:
            raise _invalid_command()
        if hvac_mode == HVACMode.OFF:
            return {"powerEnabled": False}
        return {"powerEnabled": True, "opMode": rac.MODES[HVACMode(hvac_mode)]}

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._write(self._mode_patch(hvac_mode))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        value = kwargs.get(ATTR_TEMPERATURE)
        if (
            set(kwargs)
            - {
                ATTR_TEMPERATURE,
                "hvac_mode",
                ATTR_ENTITY_ID,
                "device_id",
                "area_id",
                "floor_id",
                "label_id",
            }
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0 <= value <= 50
        ):
            raise _invalid_command()
        # Tolerate only floating-point conversion noise, never round a setting.
        integer = round(value)
        if not math.isclose(value, integer, rel_tol=0, abs_tol=1e-7):
            raise _invalid_command()
        desired = self._mode_patch(kwargs["hvac_mode"]) if "hvac_mode" in kwargs else {}
        desired["tempSetpoint"] = integer
        await self._write(desired)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._write({"fanSpeed": fan_mode})

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        if swing_mode not in ("off", "on"):
            raise _invalid_command()
        await self._write({"vSwingEnabled": swing_mode == "on"})

    async def async_set_swing_horizontal_mode(self, swing_horizontal_mode: str) -> None:
        if swing_horizontal_mode not in ("off", "on"):
            raise _invalid_command()
        await self._write({"hSwingEnabled": swing_horizontal_mode == "on"})

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if not self.preset_modes or preset_mode not in self.preset_modes:
            raise _invalid_command()
        await self._write({rac.ECO: preset_mode == rac.PRESET_ECO})

    async def _write(self, desired: dict[str, Any]) -> None:
        model = self.device.model
        if model is None or not self.controls_current:
            raise _invalid_command()

        def validate(device: Device, patch: dict[str, Any]) -> bool:
            return self.controls_current and rac.command_valid(device, patch, model)

        if not validate(self.device, desired):
            raise _invalid_command()
        try:
            await self.account.write_state(self.serial, desired, validate)
        except BaconError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="bacon_command_failed"
            ) from err


def _invalid_command() -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key="bacon_unsupported_command"
    )
