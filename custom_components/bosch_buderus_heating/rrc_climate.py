"""CT-C03, CT-C04: one climate entity per zone of an RRC 2.0 room controller.

``heat`` is the mode ``manual`` and ``auto`` the mode ``clock``; the
controller has no off mode. A target set in ``manual`` changes the manual
setpoint (confirmed on a TC100.2). A target set in ``clock`` is a temporary
setpoint (``clockOverride/temperatureHeating``) that lasts until the next
switch point of the program (P23, P33); that write is expected, untested.
A mode is never changed on the side: a temperature with a mode writes the
mode first and, once it is confirmed, the target.
"""

from __future__ import annotations

import math
from typing import Any, Final

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature as Feature
from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.temperature import display_temp
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import TemperatureDeltaConverter

from . import rrc, rrc_contract_zone
from .const import DOMAIN
from .control import HeldCapabilities, async_set_control
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource, grouped_entity_name
from .ha_compat import temperature_members, temperature_unit
from .pointt import Resource
from .writes import (
    RRC_OPERATION_MODE_POLICY,
    NumberWritePolicy,
    assess_control,
    context_of,
    number_policy_for_resource,
)

MODES: Final = {"manual": HVACMode.HEAT, "clock": HVACMode.AUTO}
MANUAL: Final = "manualTemperatureHeating"
OVERRIDE: Final = rrc_contract_zone.OVERRIDE_LEAF
ACTIVE: Final = "temperatureHeatingSetpoint"
ACTUAL: Final = "temperatureActual"
MODE: Final = "userMode"
# Every zone leaf the climate entity reads (O51: entity usage of the polling).
LEAVES: Final = (
    MODE,
    ACTUAL,
    ACTIVE,
    MANUAL,
    OVERRIDE,
    rrc_contract_zone.STATUS,
    rrc_contract_zone.NEXT_SETPOINT,
    rrc_contract_zone.MINUTES_TO_NEXT,
)
_ACTIONS: Final = {"heat_request": HVACAction.HEATING, "idle": HVACAction.IDLE}
_SERVICE_KEYS: Final = frozenset(
    {
        ATTR_TEMPERATURE,
        "hvac_mode",
        ATTR_ENTITY_ID,
        "device_id",
        "area_id",
        "floor_id",
        "label_id",
    }
)


def zone_reading(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str, *, strict: bool = False
) -> Resource | None:
    """A reading of a zone leaf within its validity limit (O101).

    ``strict`` for a change: a fresh reading of a successful cloud cycle,
    the reading the coordinator compares its write guards with. The
    validity rules of each leaf (flags, type, range) are the caller's.
    """
    assumed = getattr(coordinator, "assumed_fresh", None)
    if assumed is not None and (stored := assumed(path)) is not None:
        return stored if isinstance(stored, Resource) else None
    if strict:
        if not coordinator.last_update_success:
            return None
        snapshot = (coordinator.data or {}).get(path)
    else:
        snapshot = coordinator.display_snapshot(path)
    if (
        snapshot is None
        or not snapshot.available
        or snapshot.freshness is not Freshness.FRESH
        or snapshot.resource.path != path
    ):
        return None
    return snapshot.resource


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


@temperature_members
class RrcZoneClimate(
    HeldCapabilities,
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    ClimateEntity,
):
    """The mode, room temperature and target of one RRC zone."""

    _attr_has_entity_name = True
    _attr_translation_key = "rrc_zone"
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = 0.1
    _enable_turn_on_off_backwards_compatibility = False
    # They change with every program step; the recorder keeps the state.
    _unrecorded_attributes = frozenset(
        {
            "temporary_override",
            "override_until_next_switch_point",
            "next_setpoint",
            "minutes_to_next_setpoint",
            "zone_status",
        }
    )

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, root: str
    ) -> None:
        super().__init__(coordinator)
        self.root = root
        key = root.strip("/").replace("/", ":")
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{key}:climate"
        self._attr_name = grouped_entity_name(coordinator, f"{root}/{MODE}", "").rstrip(
            " \N{EN DASH}"
        )
        self.extra_resource_paths = tuple(f"{root}/{leaf}" for leaf in LEAVES)

    # ---------------------------------------------------------- readings
    def _reading(self, leaf: str, *, strict: bool = False) -> Resource | None:
        return zone_reading(self.coordinator, f"{self.root}/{leaf}", strict=strict)

    def _scalar(self, leaf: str, *, strict: bool = False) -> Resource | None:
        """A valid zone scalar (flags, type, unit, range: ``rrc.scalar_value``)."""
        resource = self._reading(leaf, strict=strict)
        return (
            resource
            if resource is not None and rrc.scalar_value(resource) is not None
            else None
        )

    def _mode(self, *, strict: bool = False) -> Resource | None:
        resource = self._scalar(MODE, strict=strict)
        return resource if resource is not None and resource.value in MODES else None

    def _value(self, leaf: str) -> float | None:
        resource = self._scalar(leaf)
        return _number(resource.value) if resource is not None else None

    def _override_active(self) -> bool:
        value = self._value(OVERRIDE)
        return value is not None and value != 0

    def _detail(self, leaf: str) -> str | float | bool | None:
        resource = self._reading(leaf)
        return rrc.detail_value(resource) if resource is not None else None

    def _target(
        self, *, strict: bool = False
    ) -> tuple[Resource, NumberWritePolicy, float, float, Resource] | None:
        """The path a target is written to in the current mode, its policy,
        limits and the mode reading; ``None`` when none can be written.

        S3/S4: the limits are the metadata of that path. A temporary setpoint
        of 0 clears it, so its lower limit is the manual setpoint's minimum.
        """
        mode = self._mode(strict=strict)
        if mode is None:
            return None
        leaf = MANUAL if mode.value == "manual" else OVERRIDE
        resource = self._scalar(leaf, strict=strict)
        if resource is None:
            return None
        policy = number_policy_for_resource(resource, context_of(self.coordinator))
        metadata = resource.metadata
        if policy is None or metadata.minimum is None or metadata.maximum is None:
            return None
        minimum, maximum = metadata.minimum, metadata.maximum
        if leaf == OVERRIDE:
            manual = self._scalar(MANUAL, strict=strict)
            floor = manual.metadata.minimum if manual is not None else None
            if floor is None:
                if minimum <= 0:
                    return None
                floor = minimum
            minimum = max(minimum, floor)
        if not math.isfinite(minimum) or not math.isfinite(maximum):
            return None
        if minimum > maximum:
            return None
        return resource, policy, minimum, maximum, mode

    # ------------------------------------------------------ entity state
    @property
    def available(self) -> bool:
        return self._mode() is not None and self._value(ACTUAL) is not None

    @property
    def hvac_mode(self) -> HVACMode | None:
        mode = self._mode()
        return MODES[str(mode.value)] if self.available and mode else None

    @property
    def hvac_modes(self) -> list[HVACMode]:
        mode = self._mode()
        if not self.available or mode is None:
            return []
        # P03: the TC100.2 advertises no options; the fallback is manual and
        # clock. P04: a locked mode keeps its modes shown; writing is refused.
        offered = [raw for raw in rrc.allowed_values(mode) if raw in MODES]
        if mode.value not in offered:
            offered.append(str(mode.value))
        return [mapped for raw, mapped in MODES.items() if raw in offered]

    @property
    def hvac_action(self) -> HVACAction | None:
        status = self._detail(rrc_contract_zone.STATUS) if self.available else None
        return _ACTIONS.get(status) if isinstance(status, str) else None

    @property
    def native_current_temperature(self) -> float | None:
        return self._value(ACTUAL) if self.available else None

    @property
    def native_target_temperature(self) -> float | None:
        mode = self._mode()
        if not self.available or mode is None:
            return None
        if mode.value == "manual":
            return self._value(MANUAL)
        # P06: a temporary setpoint of 0 means none; the program's applies.
        override = self._value(OVERRIDE)
        if override is not None and override != 0:
            return override
        return self._value(ACTIVE)

    @property
    def supported_features(self) -> Feature:
        if self.available and self._target() is not None:
            return Feature.TARGET_TEMPERATURE
        return Feature(0)

    @property
    def min_temp(self) -> float:
        target = self._target()
        return target[2] if target is not None else super().min_temp

    @property
    def max_temp(self) -> float:
        target = self._target()
        return target[3] if target is not None else super().max_temp

    @property
    def target_temperature_step(self) -> float | None:
        target = self._target()
        if target is None:
            return None
        resource, policy = target[0], target[1]
        step = assess_control(resource, context_of(self.coordinator)).ui_step
        return step if step is not None else policy.step

    def _live_capabilities(self) -> dict[str, Any]:
        attributes = dict(super()._live_capabilities() or {})
        if (step := self.target_temperature_step) is not None:
            attributes["target_temp_step"] = TemperatureDeltaConverter.convert(
                step, temperature_unit(self), self.hass.config.units.temperature_unit
            )
        return attributes

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.available:
            return {}
        attributes: dict[str, Any] = {}
        unit = temperature_unit(self)
        if not self.supported_features & Feature.TARGET_TEMPERATURE:
            # HA publishes a target only with the writable-target feature;
            # show the reading without offering a command for it.
            attributes[ATTR_TEMPERATURE] = display_temp(
                self.hass, self.native_target_temperature, unit, self.precision
            )
        override = self._override_active()
        attributes["temporary_override"] = override
        if override:
            # P33: it lasts until the next switch point, not for good.
            attributes["override_until_next_switch_point"] = True
        next_setpoint = _number(self._detail(rrc_contract_zone.NEXT_SETPOINT))
        if next_setpoint is not None:
            attributes["next_setpoint"] = display_temp(
                self.hass, next_setpoint, unit, self.precision
            )
        minutes = _number(self._detail(rrc_contract_zone.MINUTES_TO_NEXT))
        if minutes is not None:
            attributes["minutes_to_next_setpoint"] = (
                int(minutes) if minutes.is_integer() else minutes
            )
        status = self._detail(rrc_contract_zone.STATUS)
        if isinstance(status, str):
            attributes["zone_status"] = status
        return attributes

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.root)

    # ------------------------------------------------------------ writes
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        mode = self._mode(strict=True)
        if mode is None or hvac_mode not in self.hvac_modes:
            raise _invalid()
        raw = next(raw for raw, mapped in MODES.items() if mapped == hvac_mode)
        # P04: a locked mode is refused by the write service (ControlLocked).
        await async_set_control(
            self.coordinator,
            mode.path,
            raw,
            RRC_OPERATION_MODE_POLICY,
            expected=(mode,),
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        value = kwargs.get(ATTR_TEMPERATURE)
        if (
            set(kwargs) - _SERVICE_KEYS
            or isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            raise _invalid()
        if "hvac_mode" in kwargs:
            requested = kwargs["hvac_mode"]
            if requested not in self.hvac_modes:
                raise _invalid()
            if requested != self.hvac_mode:
                # The mode first; only a confirmed mode leads to the target.
                await self.async_set_hvac_mode(HVACMode(requested))
        target = self._target(strict=True)
        if target is None:
            raise _invalid()
        resource, policy, minimum, maximum, mode = target
        raw = float(value)
        step = self.target_temperature_step
        if step and {
            temperature_unit(self),
            self.hass.config.units.temperature_unit,
        } != {UnitOfTemperature.CELSIUS}:
            # C31: a converted value (70 °F) never lies on the controller's
            # grid; use the nearest grid value from the path's minimum.
            base = resource.metadata.minimum or 0.0
            raw = round(base + round((raw - base) / step) * step, 4)
        if not minimum - 1e-7 <= raw <= maximum + 1e-7:
            # Never clear a temporary setpoint through a target of 0.
            raise _invalid()
        await async_set_control(
            self.coordinator, resource.path, raw, policy, expected=(mode,)
        )


def _invalid() -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key="rrc_zone_climate_command"
    )
