"""Heating-circuit climate entities using the existing PointT write contracts.

``heat`` is the mode ``manual``, whose target is ``manualRoomSetpoint``. In
the mode ``auto`` the program sets the target; a circuit that offers a
writable ``temporaryRoomSetpoint`` takes a target there for now, and a clear
button ends it again (the value -1, offered only when the advertised limits
include it). A reading of 0 or more is an active temporary setpoint, a
negative one none. A room controller's zones have climates of their own
(``rrc_climate``); their circuits get none here. The temporary setpoint is
expected, untested on a real device.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature as Feature
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.temperature import display_temp
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import (
    TemperatureConverter,
    TemperatureDeltaConverter,
)

from . import rrc
from .const import DOMAIN
from .control import HeldCapabilities, async_set_control, track_control_entities
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource, grouped_entity_name
from .entity_resources import fresh_resource, temperature
from .ha_compat import temperature_members, temperature_unit
from .i18n import localize
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource
from .unit_contracts import TEMPERATURE_UNITS
from .writes import (
    HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    HEATING_CIRCUIT_TEMPORARY_SETPOINT_POLICY,
    ControlContext,
    assess_control,
    context_of,
    number_policy_for_resource,
)

_MODE_PATH = re.compile(r"^(/heatingCircuits/[^/]+)/operationMode$")
_TEMPORARY_PATH = re.compile(r"^(/heatingCircuits/[^/]+)/temporaryRoomSetpoint$")
TEMPORARY = "temporaryRoomSetpoint"
# The value that ends a temporary setpoint; written only when the advertised
# limits include it.
CLEAR_VALUE = -1.0
_SERVICE_KEYS = frozenset(
    {ATTR_TEMPERATURE, ATTR_ENTITY_ID, "device_id", "area_id", "floor_id", "label_id"}
)
_MODES = {"off": HVACMode.OFF, "manual": HVACMode.HEAT, "auto": HVACMode.AUTO}
# A circuit with this control type is not controlled here; the climate then
# only shows it (expected, untested on a real device).
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
        """The circuit reports a control type without control.

        Without a current reading the last known value decides, so the lock
        never ends without a reading that ends it (a stale or failed reading
        of ``controlType`` keeps a circuit read-only).
        """
        resource = self._resource("controlType", strict=strict)
        if resource is not None:
            return resource.value in _READ_ONLY_CONTROL_TYPES
        path = self.root + "/controlType"
        snapshot = (self.coordinator.data or {}).get(path)
        last = (
            snapshot.resource
            if snapshot is not None
            else self.coordinator.resources.get(path)
        )
        return (
            last is not None
            and last.has_value
            and last.value in _READ_ONLY_CONTROL_TYPES
        )

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
            temporary = temporary_setpoint(self.coordinator, self.root, strict=False)
            if temporary is not None and temporary.shown is not None:
                # The written target, before the circuit's own target follows.
                return temporary.shown
            return temperature(self._resource("currentRoomSetpoint"))
        return None

    def _temporary_target(self, *, strict: bool = False) -> TemporarySetpoint | None:
        """The temporary setpoint as the target of the mode ``auto``."""
        mode = self._checked_mode(strict=strict)
        if mode is None or mode.value != "auto":
            return None
        temporary = temporary_setpoint(self.coordinator, self.root, strict=strict)
        return temporary if temporary is not None and temporary.settable else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        if (
            self.hvac_mode == HVACMode.AUTO
            and not self.supported_features & Feature.TARGET_TEMPERATURE
        ):
            # HA normally publishes a target only with the writable-target
            # feature. Expose the schedule's reading without advertising a
            # temperature command that this mode cannot safely implement.
            attributes[ATTR_TEMPERATURE] = display_temp(
                self.hass,
                self.native_target_temperature,
                temperature_unit(self),
                self.precision,
            )
        temporary = temporary_setpoint(self.coordinator, self.root, strict=False)
        if self.available and temporary is not None:
            # It acts only in the mode auto.
            attributes["temporary_override"] = (
                self.hvac_mode == HVACMode.AUTO and temporary.active
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
            or (
                self.hvac_mode == HVACMode.AUTO and self._temporary_target() is not None
            )
            else Feature(0)
        )
        if HVACMode.OFF in self.hvac_modes:
            features |= Feature.TURN_OFF
        return features

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    @property
    def min_temp(self) -> float:
        if (temporary := self._temporary_target()) is not None:
            return temporary.convert(temporary.minimum, temperature_unit(self))
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
        if (temporary := self._temporary_target()) is not None:
            return temporary.convert(temporary.maximum, temperature_unit(self))
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
        if (temporary := self._temporary_target()) is not None:
            return TemperatureDeltaConverter.convert(
                temporary.step, temporary.unit, temperature_unit(self)
            )
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
        context = self._checked_context(strict=True)
        # A combined mode/temperature command must never partly change a mode.
        if (
            set(kwargs) - _SERVICE_KEYS
            or mode is None
            or context is None
            or self._read_only_control(strict=True)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise _invalid()
        if mode.value == "auto":
            await self._async_set_temporary(float(value), mode, context)
            return
        target = self._checked_target(strict=True)
        if mode.value != "manual" or target is None:
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

    async def _async_set_temporary(
        self, value: float, mode: Resource, context: tuple[Resource, ...]
    ) -> None:
        """Write a target of the mode ``auto`` to the temporary setpoint.

        Never a value outside the offered range: -1 and 0 end or switch off
        a temporary setpoint and are no targets.
        """
        temporary = self._temporary_target(strict=True)
        if temporary is None:
            raise _invalid()
        raw = TemperatureConverter.convert(
            value, temperature_unit(self), temporary.unit
        )
        if {
            temperature_unit(self),
            self.hass.config.units.temperature_unit,
        } != {temporary.unit}:
            # C31: a converted value never lies exactly on the grid; use the
            # nearest grid value, counted from the advertised minimum.
            raw = temporary.on_grid(raw)
        if not temporary.minimum - 1e-7 <= raw <= temporary.maximum + 1e-7:
            raise _invalid()
        await async_set_control(
            self.coordinator,
            temporary.resource.path,
            raw,
            HEATING_CIRCUIT_TEMPORARY_SETPOINT_POLICY,
            expected=(mode, temporary.resource, *context),
        )


@dataclass(frozen=True, slots=True)
class TemporarySetpoint:
    """A heating circuit's writable temporary setpoint, in its own unit.

    ``minimum`` to ``maximum`` is the range a target may take: the advertised
    one, from the lowest grid value above 0. The advertised minimum may be -1
    or 0, which end or switch off a temporary setpoint; the manual setpoint's
    minimum then is the lower limit, and without one no target is offered
    (``settable``). ``clearable``: the advertised limits include -1.
    """

    resource: Resource
    unit: str
    minimum: float
    maximum: float
    step: float
    settable: bool
    clearable: bool

    @property
    def value(self) -> float | None:
        value = self.resource.value
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            return None
        return float(value)

    @property
    def active(self) -> bool:
        """A reading of 0 or more; a negative one (-1) means none."""
        value = self.value
        return value is not None and value >= 0

    @property
    def shown(self) -> float | None:
        """The temporary target in °C, if it lies in the offered range."""
        value = self.value
        if (
            not self.settable
            or value is None
            or not self.minimum - 1e-7 <= value <= self.maximum + 1e-7
        ):
            return None
        return TemperatureConverter.convert(value, self.unit, UnitOfTemperature.CELSIUS)

    def convert(self, value: float, unit: str) -> float:
        return TemperatureConverter.convert(value, self.unit, unit)

    def on_grid(self, value: float) -> float:
        base = self.resource.metadata.minimum or 0.0
        return round(base + round((value - base) / self.step) * self.step, 4)


def temporary_setpoint(
    coordinator: BoschBuderusDataUpdateCoordinator, root: str, *, strict: bool
) -> TemporarySetpoint | None:
    """The circuit's temporary setpoint, if it can be written.

    Never on a room controller, whose zones have temporary setpoints of their
    own. ``strict`` for a change: a fresh reading of a successful cloud
    cycle, as every write needs.
    """
    context = context_of(coordinator)
    if context.rrc_plant:
        return None
    path = f"{root}/{TEMPORARY}"
    resource = (
        fresh_resource(coordinator, path)
        if strict
        else valid_resource(coordinator, path)
    )
    if resource is None:
        return None
    assessment = assess_control(resource, context)
    metadata = resource.metadata
    unit = TEMPERATURE_UNITS.get(metadata.unit or "")
    step = assessment.ui_step
    advertised, maximum = metadata.minimum, metadata.maximum
    if (
        not assessment.eligible
        or assessment.policy is not HEATING_CIRCUIT_TEMPORARY_SETPOINT_POLICY
        or unit is None
        or advertised is None
        or maximum is None
        or step is None
        or step <= 0
    ):
        return None
    floor = (
        advertised
        if advertised > 0
        else _manual_minimum(coordinator, root, metadata.unit)
    )
    minimum = advertised
    if floor is not None:
        # The lowest grid value at or above the floor.
        ticks = math.ceil((floor - advertised) / step - 1e-9)
        minimum = round(advertised + ticks * step, 4)
    temporary = TemporarySetpoint(
        resource,
        unit,
        minimum,
        maximum,
        step,
        settable=floor is not None and 0 < minimum <= maximum,
        clearable=takes_clear_value(resource, step),
    )
    return temporary if temporary.value is not None else None


def _manual_minimum(
    coordinator: BoschBuderusDataUpdateCoordinator, root: str, unit: str | None
) -> float | None:
    """The positive minimum of the circuit's manual setpoint in the same unit,
    whatever its flags say (it may be reported as not used in the mode
    auto)."""
    snapshot = (coordinator.data or {}).get(f"{root}/manualRoomSetpoint")
    if (
        snapshot is None
        or not snapshot.available
        or snapshot.freshness is not Freshness.FRESH
    ):
        return None
    metadata = snapshot.resource.metadata
    minimum = metadata.minimum
    if (
        minimum is None
        or not math.isfinite(minimum)
        or minimum <= 0
        or metadata.unit != unit
    ):
        return None
    return minimum


def takes_clear_value(resource: Resource, step: float | None) -> bool:
    """Whether -1 lies within the advertised limits and on the grid."""
    minimum, maximum = resource.metadata.minimum, resource.metadata.maximum
    if minimum is None or maximum is None or not step or step <= 0:
        return False
    ticks = (CLEAR_VALUE - minimum) / step
    return minimum <= CLEAR_VALUE <= maximum and math.isclose(
        ticks, round(ticks), abs_tol=1e-7
    )


@dataclass(frozen=True, kw_only=True)
class TemporaryClearDescription(EntityDescription):
    """The heating circuit root is the key."""


def clear_button_descriptions(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[TemporaryClearDescription, ...]:
    """A circuit whose writable temporary setpoint takes -1 gets a clear
    button; never on a room controller."""
    if context.rrc_plant:
        return ()
    descriptions = []
    for path, resource in resources.items():
        match = _TEMPORARY_PATH.fullmatch(path)
        if match is None or f"{match[1]}/operationMode" not in resources:
            continue
        assessment = assess_control(resource, context)
        if (
            assessment.eligible
            and assessment.policy is HEATING_CIRCUIT_TEMPORARY_SETPOINT_POLICY
            and takes_clear_value(resource, assessment.ui_step)
        ):
            descriptions.append(TemporaryClearDescription(key=match[1]))
    return tuple(descriptions)


def track_temporary_clear_buttons(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    track_control_entities(
        entry,
        coordinator,
        add_entities,
        clear_button_descriptions,
        lambda owner, description: HeatingCircuitTemporaryClearButton(
            owner, description.key
        ),
    )


class HeatingCircuitTemporaryClearButton(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], ButtonEntity
):
    """End the temporary setpoint of a heating circuit (write -1).

    Available in the mode ``auto`` while a temporary setpoint is active; the
    read-back must show -1. Expected, untested on a real device.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "heating_temporary_setpoint_clear"
    _attr_icon = "mdi:timer-off-outline"

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, root: str
    ) -> None:
        super().__init__(coordinator)
        self.root = root
        self.resource_path = f"{root}/{TEMPORARY}"
        self.extra_resource_paths = (f"{root}/operationMode",)
        key = self.resource_path.strip("/").replace("/", ":")
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{key}:clear"
        self._attr_name = grouped_entity_name(
            coordinator,
            self.resource_path,
            localize(
                coordinator.hass.config.language,
                "Vorübergehenden Sollwert aufheben",
                "Clear temporary setpoint",
            ),
        )
        # The climate's own checks of the mode and the heating context.
        self._climate = HeatingCircuitClimate(coordinator, root)

    def _state(
        self, *, strict: bool
    ) -> tuple[Resource, Resource, tuple[Resource, ...]] | None:
        """The active temporary setpoint, the mode ``auto`` and the heating
        context, while -1 lies within the limits."""
        climate = self._climate
        mode = climate._checked_mode(strict=strict)
        context = climate._checked_context(strict=strict)
        temporary = temporary_setpoint(self.coordinator, self.root, strict=strict)
        if (
            mode is None
            or mode.value != "auto"
            or context is None
            or climate._read_only_control(strict=strict)
            or temporary is None
            or not temporary.clearable
            or not temporary.active
        ):
            return None
        return temporary.resource, mode, context

    @property
    def available(self) -> bool:
        return self._state(strict=False) is not None

    async def async_press(self) -> None:
        state = self._state(strict=True)
        if state is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="heating_temporary_setpoint_not_active",
            )
        resource, mode, context = state
        await async_set_control(
            self.coordinator,
            resource.path,
            CLEAR_VALUE,
            HEATING_CIRCUIT_TEMPORARY_SETPOINT_POLICY,
            expected=(mode, resource, *context),
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.root)


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
