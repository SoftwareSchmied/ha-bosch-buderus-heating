"""Climate entity of an air conditioner on a PointT gateway.

On/off writes ``acControl``, the mode ``operationMode`` (only while the
unit is on: one path per command), the target ``temperatureSetpoint``, the
fan ``fanSpeed``, the louvres ``airFlowVertical``/``airFlowHorizontal`` and
the eco preset ``ecoMode``. Options come from the reported
``allowedValues`` alone. No ``hvac_action``: a mode does not show whether
the compressor runs.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature as Feature
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.unit_conversion import TemperatureDeltaConverter

from .aircon import AirconCoordinator
from .aircon_entity import (
    AirconEntity,
    invalid_command,
    options,
    remove_heating_leftovers,
    track_aircon_entities,
)
from .aircon_policies import (
    AC_CONTROL,
    AC_CONTROL_POLICY,
    ADVANCED_FUNCTION_POLICIES,
    AIR_FLOW_HORIZONTAL,
    AIR_FLOW_HORIZONTAL_POLICY,
    AIR_FLOW_VERTICAL,
    AIR_FLOW_VERTICAL_POLICY,
    ECO_MODE,
    FAN_SPEED,
    FAN_SPEED_POLICY,
    OPERATION_MODE,
    OPERATION_MODE_POLICY,
    ROOM_TEMPERATURE,
    SETPOINT_POLICY,
    TEMPERATURE_SETPOINT,
)
from .const import DOMAIN
from .ha_compat import temperature_members, temperature_unit
from .platform_scope import PlatformScope
from .unit_contracts import TEMPERATURE_UNITS
from .writes import assess_control

HVAC_MODES = {
    "auto": HVACMode.HEAT_COOL,
    "cool": HVACMode.COOL,
    "heat": HVACMode.HEAT,
    "dry": HVACMode.DRY,
    "fanOnly": HVACMode.FAN_ONLY,
}
FAN_ORDER = ("auto", "quiet", "low", "mid", "high", "turbo")
SWING_ORDER = (
    "off",
    "on",
    "auto",
    "swing",
    *(f"angle{index}" for index in range(1, 6)),
    "left",
    "center",
    "right",
    "wide",
    "ultraWide",
    "fixed",
)
PRESET_NONE, PRESET_ECO = "none", "eco"
# Service fields that are not settings.
_TARGET_FIELDS = frozenset(
    {"entity_id", "device_id", "area_id", "floor_id", "label_id"}
)


def track_aircon_climates(
    scope: PlatformScope, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    def build(
        coordinator: AirconCoordinator,
    ) -> Iterable[tuple[str, Any]]:
        if coordinator.data.resources:
            yield "climate", lambda: AirconClimate(coordinator)

    # Once per setup, before the backend's own entities are added.
    remove_heating_leftovers(scope)
    track_aircon_entities(scope, async_add_entities, build)


def _celsius(unit: object) -> bool:
    return isinstance(unit, str) and TEMPERATURE_UNITS.get(unit) == "°C"


@temperature_members
class AirconClimate(AirconEntity, ClimateEntity):
    """The main control of the unit: enabled, named after the device."""

    _attr_name = None
    _attr_translation_key = "pointt_air_conditioner"
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = 0.5
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coordinator: AirconCoordinator) -> None:
        super().__init__(coordinator, "airConditioning:climate")

    # Readings

    @property
    def hvac_mode(self) -> HVACMode | None:
        power = self.coordinator.value(AC_CONTROL)
        mode = self.coordinator.value(OPERATION_MODE)
        if power == "off" or mode == "off":
            # "off" as a mode is only ever read, never written.
            return HVACMode.OFF
        if power not in (None, "on"):
            return None
        return HVAC_MODES.get(mode) if isinstance(mode, str) else None

    @property
    def hvac_modes(self) -> list[HVACMode]:
        modes: list[HVACMode] = []
        if self.coordinator.control_ready(AC_CONTROL):
            modes.append(HVACMode.OFF)
        offered = options(self.coordinator, OPERATION_MODE, HVAC_MODES)
        modes.extend(HVAC_MODES[token] for token in offered.values())
        current = self.hvac_mode
        if current is not None and current not in modes:
            # Shown although it cannot be chosen here.
            modes.append(current)
        return modes

    @property
    def native_current_temperature(self) -> float | None:
        resource = self.coordinator.resource(ROOM_TEMPERATURE)
        if resource is None or not _celsius(resource.metadata.unit):
            return None
        value = resource.value
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value in resource.metadata.numeric_error_values
        ):
            return None
        return float(value)

    def _setpoint(self) -> tuple[float, float, float, float] | None:
        """Value, minimum, maximum and step of a writable valid setpoint."""
        if not self.coordinator.control_ready(TEMPERATURE_SETPOINT):
            return None
        resource = self.coordinator.resource(TEMPERATURE_SETPOINT)
        if resource is None or not _celsius(resource.metadata.unit):
            return None
        value = resource.value
        minimum, maximum = resource.metadata.minimum, resource.metadata.maximum
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or minimum is None
            or maximum is None
            or not minimum < maximum
            or not minimum <= value <= maximum
        ):
            return None
        step = assess_control(resource).ui_step or SETPOINT_POLICY.step
        return float(value), minimum, maximum, step

    @property
    def native_target_temperature(self) -> float | None:
        setpoint = self._setpoint()
        return setpoint[0] if setpoint else None

    @property
    def min_temp(self) -> float:
        setpoint = self._setpoint()
        return setpoint[1] if setpoint else super().min_temp

    @property
    def max_temp(self) -> float:
        setpoint = self._setpoint()
        return setpoint[2] if setpoint else super().max_temp

    @property
    def target_temperature_step(self) -> float | None:
        setpoint = self._setpoint()
        return setpoint[3] if setpoint else None

    @property
    def capability_attributes(self) -> dict[str, Any]:
        attributes = super().capability_attributes or {}
        if (step := self.target_temperature_step) is not None and self.hass:
            attributes["target_temp_step"] = TemperatureDeltaConverter.convert(
                step, temperature_unit(self), self.hass.config.units.temperature_unit
            )
        return attributes

    def _modes(self, path: str, order: Iterable[str]) -> dict[str, str]:
        offered = options(self.coordinator, path, order)
        return offered if len(offered) >= 2 else {}

    def _current(self, path: str, offered: dict[str, str]) -> str | None:
        value = self.coordinator.value(path)
        return next((key for key, token in offered.items() if token == value), None)

    @property
    def fan_modes(self) -> list[str]:
        return list(self._modes(FAN_SPEED, FAN_ORDER))

    @property
    def fan_mode(self) -> str | None:
        return self._current(FAN_SPEED, self._modes(FAN_SPEED, FAN_ORDER))

    @property
    def swing_modes(self) -> list[str]:
        return list(self._modes(AIR_FLOW_VERTICAL, SWING_ORDER))

    @property
    def swing_mode(self) -> str | None:
        return self._current(
            AIR_FLOW_VERTICAL, self._modes(AIR_FLOW_VERTICAL, SWING_ORDER)
        )

    @property
    def swing_horizontal_modes(self) -> list[str]:
        return list(self._modes(AIR_FLOW_HORIZONTAL, SWING_ORDER))

    @property
    def swing_horizontal_mode(self) -> str | None:
        return self._current(
            AIR_FLOW_HORIZONTAL, self._modes(AIR_FLOW_HORIZONTAL, SWING_ORDER)
        )

    def _eco_offered(self) -> bool:
        resource = self.coordinator.resource(ECO_MODE)
        return (
            resource is not None
            and self.coordinator.control_ready(ECO_MODE)
            and assess_control(resource).supports_switch
        )

    @property
    def preset_modes(self) -> list[str] | None:
        return [PRESET_NONE, PRESET_ECO] if self._eco_offered() else None

    @property
    def preset_mode(self) -> str | None:
        if not self._eco_offered():
            return None
        value = self.coordinator.value(ECO_MODE)
        return {"on": PRESET_ECO, "off": PRESET_NONE}.get(str(value))

    @property
    def supported_features(self) -> Feature:
        features = Feature(0)
        if self.coordinator.control_ready(AC_CONTROL):
            features |= Feature.TURN_ON | Feature.TURN_OFF
        if self._setpoint() is not None:
            features |= Feature.TARGET_TEMPERATURE
        if self.fan_modes:
            features |= Feature.FAN_MODE
        if self.swing_modes:
            features |= Feature.SWING_MODE
        if self.swing_horizontal_modes:
            features |= Feature.SWING_HORIZONTAL_MODE
        if self.preset_modes:
            features |= Feature.PRESET_MODE
        return features

    # Commands: each writes exactly one path.

    async def async_turn_on(self) -> None:
        await self._power("on")

    async def async_turn_off(self) -> None:
        await self._power("off")

    async def _power(self, value: str) -> None:
        if not self.coordinator.control_ready(AC_CONTROL):
            raise invalid_command()
        await self._write(AC_CONTROL, value, AC_CONTROL_POLICY)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self._power("off")
            return
        offered = {
            HVAC_MODES[token]: token
            for token in options(self.coordinator, OPERATION_MODE, HVAC_MODES).values()
        }
        token = offered.get(HVACMode(hvac_mode))
        if token is None:
            raise invalid_command()
        # A switched-off unit is refused on the fresh reading (switch it on
        # first); the mode is never sent together with a power change.
        await self._write(OPERATION_MODE, token, OPERATION_MODE_POLICY)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if "hvac_mode" in kwargs:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="bacon_separate_settings"
            )
        value = kwargs.get(ATTR_TEMPERATURE)
        if (
            set(kwargs) - {ATTR_TEMPERATURE, *_TARGET_FIELDS}
            or isinstance(value, bool)
            or not isinstance(value, int | float)
            or self._setpoint() is None
        ):
            raise invalid_command()
        await self._write(TEMPERATURE_SETPOINT, float(value), SETPOINT_POLICY)

    async def _choose(
        self, path: str, offered: dict[str, str], choice: str, policy: Any
    ) -> None:
        token = offered.get(choice)
        if token is None:
            raise invalid_command()
        await self._write(path, token, policy)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._choose(
            FAN_SPEED, self._modes(FAN_SPEED, FAN_ORDER), fan_mode, FAN_SPEED_POLICY
        )

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        await self._choose(
            AIR_FLOW_VERTICAL,
            self._modes(AIR_FLOW_VERTICAL, SWING_ORDER),
            swing_mode,
            AIR_FLOW_VERTICAL_POLICY,
        )

    async def async_set_swing_horizontal_mode(self, swing_horizontal_mode: str) -> None:
        await self._choose(
            AIR_FLOW_HORIZONTAL,
            self._modes(AIR_FLOW_HORIZONTAL, SWING_ORDER),
            swing_horizontal_mode,
            AIR_FLOW_HORIZONTAL_POLICY,
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if not self._eco_offered() or preset_mode not in (PRESET_NONE, PRESET_ECO):
            raise invalid_command()
        await self._write(
            ECO_MODE,
            "on" if preset_mode == PRESET_ECO else "off",
            ADVANCED_FUNCTION_POLICIES[ECO_MODE],
        )
