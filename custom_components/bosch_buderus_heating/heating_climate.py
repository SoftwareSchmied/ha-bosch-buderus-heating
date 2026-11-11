"""Heating-circuit climate entities using the existing PointT write contracts."""

from __future__ import annotations

import math
import re
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature as Feature
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.temperature import display_temp
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import (
    TemperatureConverter,
    TemperatureDeltaConverter,
)

from . import rrc
from .const import DOMAIN
from .control import HeldCapabilities, async_set_control
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource, grouped_entity_name
from .entity_resources import fresh_resource, temperature
from .ha_compat import temperature_members, temperature_unit
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource
from .unit_contracts import TEMPERATURE_UNITS
from .writes import (
    HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    assess_control,
    context_of,
    number_policy_for_resource,
)

_MODE_PATH = re.compile(r"^(/heatingCircuits/[^/]+)/operationMode$")
_MODES = {"off": HVACMode.OFF, "manual": HVACMode.HEAT, "auto": HVACMode.AUTO}
# APP-15: with this control type the manufacturer's app offers no control of
# the circuit; the climate then only shows it (expected, untested).
_READ_ONLY_CONTROL_TYPES = frozenset({"constants"})


def track_heating_climates(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    seen: set[str] = set()

    @callback
    def discover() -> None:
        additions = []
        # W1: an RRC controller with zone climates is controlled per zone;
        # its heating circuit gets no second climate entity.
        rrc_zones = context_of(coordinator).rrc_plant and bool(
            rrc.climate_roots(coordinator.data or {})
        )
        for path in coordinator.data or {}:
            match = _MODE_PATH.fullmatch(path)
            if match is None or match[1] in seen or rrc_zones:
                continue
            entity = HeatingCircuitClimate(coordinator, match[1])
            if not entity.available and not _registered(coordinator, entity):
                if path not in coordinator.stored_resources():
                    continue
                # Started with the stored inventory (P47): add it unavailable,
                # but only if a normal start with these values would add it.
                with coordinator.stored_as_fresh():
                    if not entity.available:
                        continue
            seen.add(match[1])
            additions.append(entity)
        if additions:
            add_entities(additions)

    entry.async_on_unload(coordinator.async_add_listener(discover))
    discover()


def _registered(
    coordinator: BoschBuderusDataUpdateCoordinator, entity: HeatingCircuitClimate
) -> bool:
    """C70: whether this climate already has a registry entry of this entry.

    In a cooling context the climate is unavailable. A registered one is
    still added, unavailable, so that it does not appear as no longer
    provided after a restart in summer. A new installation is unchanged.
    """
    registry = er.async_get(coordinator.hass)
    entity_id = registry.async_get_entity_id("climate", DOMAIN, entity.unique_id or "")
    registered = registry.async_get(entity_id) if entity_id is not None else None
    config_entry = getattr(coordinator, "config_entry", None)
    return (
        registered is not None
        and config_entry is not None
        and registered.config_entry_id == config_entry.entry_id
    )


@temperature_members
class HeatingCircuitClimate(
    HeldCapabilities,
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    ClimateEntity,
):
    """Combine the circuit's own mode, target and optional room measurement.

    O101: shown like the sensors, while each reading is within its validity
    limit; changes still need current readings of a ready cloud.
    """

    _attr_has_entity_name = True
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = 0.1
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, root: str
    ) -> None:
        super().__init__(coordinator)
        self.root = root
        key = root.strip("/").replace("/", ":")
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{key}:climate"
        self._attr_name = grouped_entity_name(
            coordinator, root + "/operationMode", ""
        ).rstrip(" \N{EN DASH}")

    def _resource(self, suffix: str, *, strict: bool = False) -> Resource | None:
        """A reading within its validity limit; ``strict`` for a change.

        A change compares its readings with the cloud's current ones, so it
        keeps the strict rule: a fresh reading of a successful cloud cycle.
        """
        path = self.root + "/" + suffix
        if strict:
            return fresh_resource(self.coordinator, path)
        return valid_resource(self.coordinator, path)

    @property
    def _mode(self) -> Resource | None:
        return self._checked_mode(strict=False)

    def _checked_mode(self, *, strict: bool) -> Resource | None:
        resource = self._resource("operationMode", strict=strict)
        if resource is None:
            return None
        assessment = assess_control(resource, context_of(self.coordinator))
        return (
            resource
            if (
                assessment.eligible
                and assessment.policy is HEATING_CIRCUIT_OPERATION_MODE_POLICY
                and assessment.current_value_issue is None
                and resource.value in _MODES
            )
            else None
        )

    @property
    def _heating_context(self) -> tuple[Resource, ...] | None:
        return self._checked_context(strict=False)

    def _checked_context(self, *, strict: bool) -> tuple[Resource, ...] | None:
        conditions = []
        for path, allowed in (
            (self.root + "/heatCoolMode", {"heat", "heatCool"}),
            (self.root + "/currentSuWiMode", {"off", "forced"}),
            ("/system/seasonOptimizer/mode", {"off", "automatic", "forcedHeat"}),
            (
                "/system/globalSeasonOptimizer/currentMode",
                {"off", "heating", "automatic", "forcedHeat"},
            ),
        ):
            if path not in (self.coordinator.data or {}):
                continue
            resource = (
                fresh_resource(self.coordinator, path)
                if strict
                else valid_resource(self.coordinator, path)
            )
            if (
                resource is None
                or resource.metadata.resource_type != "stringValue"
                or resource.metadata.unit not in (None, "")
                or resource.references
                or not isinstance(resource.value, str)
                or resource.value not in allowed
            ):
                return None
            conditions.append(resource)
        return tuple(conditions)

    @property
    def available(self) -> bool:
        return self._mode is not None and self._heating_context is not None

    @property
    def hvac_mode(self) -> HVACMode | None:
        mode = self._mode
        return _MODES.get(str(mode.value)) if self.available and mode else None

    def _read_only_control(self, *, strict: bool = False) -> bool:
        """APP-15: the circuit reports a control type without control."""
        resource = self._resource("controlType", strict=strict)
        return resource is not None and resource.value in _READ_ONLY_CONTROL_TYPES

    @property
    def hvac_modes(self) -> list[HVACMode]:
        mode = self._mode
        if self._read_only_control():
            current = self.hvac_mode
            return [current] if current is not None else []
        return (
            [
                mapped
                for raw, mapped in _MODES.items()
                if mode
                and raw in assess_control(mode, context_of(self.coordinator)).options
            ]
            if self.available
            else []
        )

    @property
    def native_current_temperature(self) -> float | None:
        return temperature(self._resource("roomtemperature"))

    @property
    def native_target_temperature(self) -> float | None:
        if self.hvac_mode == HVACMode.HEAT:
            return temperature(self._resource("manualRoomSetpoint"))
        if self.hvac_mode == HVACMode.AUTO:
            return temperature(self._resource("currentRoomSetpoint"))
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        if self.hvac_mode == HVACMode.AUTO:
            # HA normally publishes a target only with the writable-target
            # feature. Expose the schedule's reading without advertising a
            # temperature command that this mode cannot safely implement.
            attributes[ATTR_TEMPERATURE] = display_temp(
                self.hass,
                self.native_target_temperature,
                temperature_unit(self),
                self.precision,
            )
        return attributes

    @property
    def _target(self) -> Resource | None:
        return self._checked_target(strict=False)

    def _checked_target(self, *, strict: bool) -> Resource | None:
        resource = self._resource("manualRoomSetpoint", strict=strict)
        return (
            resource
            if (
                resource
                and number_policy_for_resource(resource, context_of(self.coordinator))
                and temperature(resource) is not None
            )
            else None
        )

    @property
    def supported_features(self) -> Feature:
        if self._read_only_control():
            return Feature(0)
        features = (
            Feature.TARGET_TEMPERATURE
            if (self.hvac_mode == HVACMode.HEAT and self._target is not None)
            else Feature(0)
        )
        if HVACMode.OFF in self.hvac_modes:
            features |= Feature.TURN_OFF
        return features

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    @property
    def min_temp(self) -> float:
        resource = self._target
        if resource is None or resource.metadata.minimum is None:
            return 0
        return TemperatureConverter.convert(
            resource.metadata.minimum,
            TEMPERATURE_UNITS[resource.metadata.unit or ""],
            temperature_unit(self),
        )

    @property
    def max_temp(self) -> float:
        resource = self._target
        if resource is None or resource.metadata.maximum is None:
            return 0
        return TemperatureConverter.convert(
            resource.metadata.maximum,
            TEMPERATURE_UNITS[resource.metadata.unit or ""],
            temperature_unit(self),
        )

    @property
    def target_temperature_step(self) -> float | None:
        resource = self._target
        step = (
            assess_control(resource, context_of(self.coordinator)).ui_step
            if resource
            else None
        )
        if resource is None or step is None:
            return None
        source = TEMPERATURE_UNITS[resource.metadata.unit or ""]
        return TemperatureDeltaConverter.convert(step, source, temperature_unit(self))

    def _live_capabilities(self) -> dict[str, Any]:
        attributes = dict(super()._live_capabilities() or {})
        if (step := self.target_temperature_step) is not None:
            attributes["target_temp_step"] = TemperatureDeltaConverter.convert(
                step, temperature_unit(self), self.hass.config.units.temperature_unit
            )
        return attributes

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.root)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        mode = self._checked_mode(strict=True)
        context = self._checked_context(strict=True)
        if (
            mode is None
            or context is None
            or hvac_mode not in self.hvac_modes
            or self._read_only_control(strict=True)
        ):
            raise _invalid()
        raw = next(raw for raw, mapped in _MODES.items() if mapped == hvac_mode)
        await async_set_control(
            self.coordinator,
            mode.path,
            raw,
            HEATING_CIRCUIT_OPERATION_MODE_POLICY,
            expected=(mode, *context),
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        value = kwargs.get(ATTR_TEMPERATURE)
        mode = self._checked_mode(strict=True)
        target = self._checked_target(strict=True)
        context = self._checked_context(strict=True)
        # A combined mode/temperature command must never partly change a mode.
        if (
            set(kwargs)
            - {
                ATTR_TEMPERATURE,
                ATTR_ENTITY_ID,
                "device_id",
                "area_id",
                "floor_id",
                "label_id",
            }
            or mode is None
            or mode.value != "manual"
            or target is None
            or context is None
            or self._read_only_control(strict=True)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise _invalid()
        policy = number_policy_for_resource(target, context_of(self.coordinator))
        assert policy is not None
        native_unit = TEMPERATURE_UNITS[target.metadata.unit or ""]
        raw = TemperatureConverter.convert(value, temperature_unit(self), native_unit)
        if policy.write_grid and {
            temperature_unit(self),
            self.hass.config.units.temperature_unit,
        } != {native_unit}:
            # C31: a converted value, such as 70 °F, never lies exactly on the
            # gateway's grid. Use the nearest grid value instead of rejecting.
            raw = round(raw / policy.step) * policy.step
        await async_set_control(
            self.coordinator,
            target.path,
            raw,
            policy,
            expected=(mode, target, *context),
        )


def valid_resource(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> Resource | None:
    """O101: a reading within its validity limit, like a sensor shows it.

    The stored-inventory decision at creation and the reading checks stay
    those of ``fresh_resource``; only a failed or older cloud cycle no
    longer hides a reading that is still valid.
    """
    assumed = getattr(coordinator, "assumed_fresh", None)
    if assumed is not None and assumed(path) is not None:
        return fresh_resource(coordinator, path)
    shown = coordinator.display_snapshot(path)
    if shown is None or not shown.available or shown.freshness is not Freshness.FRESH:
        return None
    resource = shown.resource
    metadata = resource.metadata
    if (
        resource.path != path
        or not resource.has_value
        or resource.has_values
        or not metadata.availability_flags_valid
        or metadata.available is False
        or metadata.used is False
    ):
        return None
    return resource


def _invalid() -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key="heating_climate_command"
    )
