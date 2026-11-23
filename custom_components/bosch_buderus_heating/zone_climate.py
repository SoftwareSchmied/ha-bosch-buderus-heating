"""One climate entity per ConnectKey zone (``/zones/zoneN``).

A zone controller (for example an MX400 with radio room thermostats) has a
mode and setpoints per zone and side: ``heating`` normally, ``cooling`` while
the zone's heating circuit reports the cooling season (``currentSuWiMode``
``cooling``). ``off`` is off, ``manual`` is heat (cool on the cooling side)
with ``manualRoomSetpoint`` as its target, and ``auto`` follows the zone's
program; a target set in ``auto`` is a temporary setpoint until the next
switch point, and the clear button ends it (the value -1).

The zone's heating circuit comes from its radio devices (``zoneId`` and
``assignedHC``); when they name none, an installation with exactly one
heating circuit uses that one, otherwise the zone has none: it stays on the
heating side and its action is unknown.

Every change reads the mode, the circuit's season and control type again on
the gateway right before its one request; a change since it was shown, a
setting the zone does not use now (``used`` false) or one it does not offer
(``available`` false) refuses it unsent. A mode with a temperature writes the
mode first and, once it is confirmed, the target. A circuit with the control
type ``constants`` is only shown. Expected, untested on a real device.

The action is calculated: off in the mode off, otherwise the activity of the
zone's circuit, and heating (cooling) only while the room is below (above)
the zone's active setpoint; idle otherwise. Without one of these inputs it
is unknown, never guessed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.components.button import ButtonEntity
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature as Feature
from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.temperature import display_temp
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import (
    TemperatureConverter,
    TemperatureDeltaConverter,
)

from .const import DOMAIN
from .control import (
    HeldCapabilities,
    async_set_control,
    cloud_down_error,
    track_control_entities,
)
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .derived_activity import (
    COOLING,
    DEFROSTING,
    HEATING,
    IDLE,
    OFF,
    circuit_activity,
    circuit_input_paths,
)
from .device import device_info_for_resource, grouped_entity_name
from .entity_resources import fresh_resource, temperature
from .ha_compat import temperature_members, temperature_unit
from .heating_climate import (
    CLEAR_VALUE,
    TemporarySetpoint,
    clear_outside_limits,
    manual_floor_reading,
    takes_clear_value,
    temporary_setpoint,
    valid_resource,
)
from .humidity_reading import plausible_humidity
from .i18n import localize
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource, WriteValidationError
from .unit_contracts import TEMPERATURE_UNITS
from .writes import (
    ZONE_OPERATION_MODE_POLICY,
    ZONE_TEMPORARY_SETPOINT_POLICY,
    ControlContext,
    assess_control,
    context_of,
    number_policy_for_resource,
    validate_control_value,
)
from .zone_controls import zone_control_issue
from .zone_write_stats import counted_write, zone_write_stats

HEAT_SIDE: Final = "heating"
COOL_SIDE: Final = "cooling"
# The side shown while it is not known now; nothing is changed then.
UNKNOWN_SIDE: Final = "unknown"
_ROOT = re.compile(r"^(/zones/zone([0-9]+))/heating/operationMode$")
_TEMPORARY = re.compile(r"^(/zones/zone[0-9]+)/heating/temporaryRoomSetpoint$")
_MODES: Final = {
    HEAT_SIDE: {"off": HVACMode.OFF, "manual": HVACMode.HEAT, "auto": HVACMode.AUTO},
    COOL_SIDE: {"off": HVACMode.OFF, "manual": HVACMode.COOL, "auto": HVACMode.AUTO},
}
_READ_ONLY_CONTROL_TYPES: Final = frozenset({"constants"})
_SEASON = re.compile(r"^/heatingCircuits/hc[0-9]+/currentSuWiMode$")
GLOBAL_SEASON: Final = "/system/globalSeasonOptimizer/currentMode"
_ZONE_ID = re.compile(r"^(/devices/[^/]+)/zoneId$")
_CIRCUIT_VALUE = re.compile(r"^(?:hc)?([0-9]+)$", re.IGNORECASE)
_CIRCUIT_MODE = re.compile(r"^(/heatingCircuits/hc[0-9]+)/operationMode$")
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
ACTIVE_SETPOINT: Final = "currentRoomSetpoint"
ROOM_TEMPERATURE: Final = "averageCurrentTemperature"
ROOM_HUMIDITY: Final = "averageActualHumidity"
CHILD_LOCK: Final = "childLock"


def _season_paths(coordinator: BoschBuderusDataUpdateCoordinator) -> tuple[str, ...]:
    """The season readings of every circuit, for a zone without a circuit."""
    circuits = sorted(
        {
            match.group(1)
            for path in coordinator.data or {}
            if (match := _CIRCUIT_MODE.fullmatch(path)) is not None
        }
    )
    return tuple(f"{circuit}/currentSuWiMode" for circuit in circuits)


def zone_number(root: str) -> int | None:
    match = re.fullmatch(r"/zones/zone([0-9]+)", root)
    return int(match.group(1)) if match is not None else None


def assigned_circuit(
    coordinator: BoschBuderusDataUpdateCoordinator, root: str
) -> str | None:
    """The heating circuit of a zone, from its radio devices.

    Every device whose ``zoneId`` names the zone must name the same circuit
    (``assignedHC`` ``hc1`` or ``1``); a device without a readable circuit,
    or two different ones, leave the zone without a circuit. When no device
    names the zone, an installation with exactly one heating circuit uses
    that one.
    """
    number = zone_number(root)
    data = coordinator.data or {}
    circuits = {
        match.group(1) for path in data if (match := _CIRCUIT_MODE.fullmatch(path))
    }
    named: set[str | None] = set()
    for path in data:
        match = _ZONE_ID.fullmatch(path)
        if match is None:
            continue
        zone = valid_resource(coordinator, path)
        if (
            zone is None
            or isinstance(zone.value, bool)
            or not isinstance(zone.value, int | float)
            or zone.value != number
        ):
            continue
        assigned = valid_resource(coordinator, f"{match.group(1)}/assignedHC")
        value = (
            _CIRCUIT_VALUE.fullmatch(assigned.value.strip())
            if assigned is not None and isinstance(assigned.value, str)
            else None
        )
        named.add(f"/heatingCircuits/hc{value.group(1)}" if value else None)
    if named:
        circuit = next(iter(named)) if len(named) == 1 else None
        return circuit if circuit in circuits else None
    return next(iter(circuits)) if len(circuits) == 1 else None


def track_zone_climates(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One climate per zone with a valid heating mode, also when found later."""
    seen: set[str] = set()

    @callback
    def discover() -> None:
        if context_of(coordinator).rrc_plant:
            return
        additions = []
        for path in coordinator.data or {}:
            match = _ROOT.fullmatch(path)
            if match is None or match.group(1) in seen:
                continue
            entity = ZoneClimate(coordinator, match.group(1))
            if not entity.available:
                if path not in coordinator.stored_resources():
                    continue
                # Started with the stored inventory (P47): add it unavailable,
                # but only if a normal start with these values would add it.
                with coordinator.stored_as_fresh():
                    if not entity.available:
                        continue
            seen.add(match.group(1))
            additions.append(entity)
        if additions:
            add_entities(additions)

    entry.async_on_unload(coordinator.async_add_listener(discover))
    discover()


@dataclass(frozen=True, slots=True)
class _Command:
    """The readings a change compares with the gateway's current ones."""

    side: str
    mode: Resource
    guards: tuple[Resource, ...]


@temperature_members
class ZoneClimate(
    HeldCapabilities,
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    ClimateEntity,
):
    """The mode, target, room temperature and humidity of one zone."""

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
            coordinator, f"{root}/{HEAT_SIDE}/operationMode", ""
        ).rstrip(" \N{EN DASH}")
        self.resource_path = f"{root}/{HEAT_SIDE}/operationMode"
        circuit = assigned_circuit(coordinator, root)
        self.extra_resource_paths = (
            *(
                f"{root}/{side}/{leaf}"
                for side in (HEAT_SIDE, COOL_SIDE)
                for leaf in (
                    "operationMode",
                    "manualRoomSetpoint",
                    "temporaryRoomSetpoint",
                )
            ),
            *(f"{root}/{leaf}" for leaf in (ACTIVE_SETPOINT, ROOM_TEMPERATURE)),
            f"{root}/{ROOM_HUMIDITY}",
            *(circuit_input_paths(coordinator, circuit) if circuit else ()),
            *((f"{circuit}/controlType",) if circuit else ()),
            *(() if circuit else _season_paths(coordinator)),
        )

    # Readings -----------------------------------------------------------

    def _reading(
        self, path: str, *, strict: bool, flags: bool = True
    ) -> Resource | None:
        if strict:
            return fresh_resource(self.coordinator, path, flags=flags)
        return valid_resource(self.coordinator, path)

    def _circuit(self) -> str | None:
        return assigned_circuit(self.coordinator, self.root)

    def _side(self, *, strict: bool = False) -> str | None:
        """``cooling`` while the zone's circuit reports the cooling season.

        A season reading that is reported but not valid now leaves the side
        unknown (``None``); without a circuit or a season it is heating.
        """
        circuit = self._circuit()
        if circuit is None:
            return None if self._cooling_possible(strict=strict) else HEAT_SIDE
        path = f"{circuit}/currentSuWiMode"
        if path not in (self.coordinator.data or {}):
            return HEAT_SIDE
        season = self._reading(path, strict=strict)
        if season is None or not isinstance(season.value, str):
            return None
        return COOL_SIDE if season.value == "cooling" else HEAT_SIDE

    def _cooling_possible(self, *, strict: bool) -> bool:
        """Whether the plant may be cooling while the zone has no circuit.

        Without a circuit the season of the zone is unknown. A circuit (or
        the global season) that reports cooling, or a season reading that is
        not valid now, could be the zone's: its heating setpoint would not
        be the one in use, so the side stays unknown and nothing is sent.
        """
        data = self.coordinator.data or {}
        paths = [path for path in data if _SEASON.fullmatch(path)]
        for path in paths:
            season = self._reading(path, strict=strict)
            if (
                season is None
                or not isinstance(season.value, str)
                or season.value == COOL_SIDE
            ):
                return True
        if GLOBAL_SEASON in data:
            season = self._reading(GLOBAL_SEASON, strict=strict)
            if season is None or (
                isinstance(season.value, str) and "cool" in season.value.lower()
            ):
                return True
        return False

    def _mode(self, side: str, *, strict: bool = False) -> Resource | None:
        """A valid mode reading of the side, writable or not."""
        resource = self._reading(f"{self.root}/{side}/operationMode", strict=strict)
        if (
            resource is None
            or zone_control_issue(resource) is not None
            or resource.value not in _MODES[side]
        ):
            return None
        return resource

    def _writable(self, resource: Resource) -> bool:
        assessment = assess_control(resource, context_of(self.coordinator))
        return (
            assessment.eligible
            and assessment.policy is ZONE_OPERATION_MODE_POLICY
            and assessment.current_value_issue is None
        )

    def _shown(self) -> tuple[str, Resource] | None:
        """The side and its valid mode reading: what a change starts from."""
        side = self._side()
        if side is None:
            return None
        mode = self._mode(side)
        return (side, mode) if mode is not None else None

    def _shown_mode(self, side: str) -> Resource | None:
        """A side's mode reading as shown: any text of a valid reading, also
        one that is not advertised or not known (it shows no mode)."""
        resource = self._reading(f"{self.root}/{side}/operationMode", strict=False)
        if (
            resource is None
            or resource.has_values
            or not isinstance(resource.value, str)
            or not resource.value.strip()
        ):
            return None
        return resource

    def _display(self) -> tuple[str, Resource | None] | None:
        """The side as shown and its mode reading; ``None``: unavailable.

        A zone stays shown while its mode or its room temperature can be read.
        A side that is not known now (a season reading that is not valid, or a
        possibly cooling plant without the zone's circuit) shows the heating
        side's mode as ``unknown``; a cooling side without a mode reading
        shows none. Changes are then locked (``_shown``, ``_command``).
        """
        side = self._side()
        mode = self._shown_mode(side or HEAT_SIDE)
        if mode is None and self.native_current_temperature is None:
            return None
        return (side or UNKNOWN_SIDE, mode)

    def _display_mode(self) -> HVACMode | None:
        display = self._display()
        if display is None:
            return None
        side, mode = display
        if mode is None:
            return None
        value = str(mode.value).strip().lower()
        return _MODES[side if side in _MODES else HEAT_SIDE].get(value)

    def _read_only_control(self, *, strict: bool = False) -> bool:
        """The circuit reports ``constants``; a stale reading keeps the lock."""
        circuit = self._circuit()
        if circuit is None:
            return False
        path = f"{circuit}/controlType"
        resource = self._reading(path, strict=strict)
        if resource is not None:
            return resource.value in _READ_ONLY_CONTROL_TYPES
        snapshot = (self.coordinator.data or {}).get(path)
        last = (
            snapshot.resource
            if snapshot is not None
            else self.coordinator.resources.get(path)
        )
        return (
            last is not None
            and last.has_value
            and last.value in (_READ_ONLY_CONTROL_TYPES)
        )

    def _guards(self) -> tuple[Resource, ...] | None:
        """The circuit's season and control type, read again before a write;
        ``None`` while one of them is reported without a current reading."""
        circuit = self._circuit()
        if circuit is None:
            return ()
        guards: list[Resource] = []
        data = self.coordinator.data or {}
        for leaf in ("currentSuWiMode", "controlType"):
            path = f"{circuit}/{leaf}"
            snapshot = data.get(path)
            if snapshot is None:
                continue
            if (
                not self.coordinator.last_update_success
                or not snapshot.available
                or snapshot.freshness is not Freshness.FRESH
                or snapshot.resource.path != path
            ):
                return None
            guards.append(snapshot.resource)
        return tuple(guards)

    def _manual(
        self, side: str, *, strict: bool = False, flags: bool = True
    ) -> Resource | None:
        resource = self._reading(
            f"{self.root}/{side}/manualRoomSetpoint", strict=strict, flags=flags
        )
        if resource is None or zone_control_issue(resource) is not None:
            return None
        return resource if temperature(resource) is not None else None

    def _manual_writable(self, resource: Resource) -> bool:
        return (
            number_policy_for_resource(resource, context_of(self.coordinator))
            is not None
        )

    def _temporary(
        self, side: str, *, strict: bool = False, flags: bool = True
    ) -> TemporarySetpoint | None:
        return temporary_setpoint(
            self.coordinator,
            f"{self.root}/{side}",
            strict=strict,
            policy=ZONE_TEMPORARY_SETPOINT_POLICY,
            flags=flags,
        )

    # State --------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._display() is not None

    @property
    def hvac_mode(self) -> HVACMode | None:
        shown = self._shown()
        if shown is None:
            return self._display_mode()
        side, mode = shown
        return _MODES[side].get(str(mode.value))

    @property
    def hvac_modes(self) -> list[HVACMode]:
        shown = self._shown()
        if shown is None:
            # Only shown: the current mode, if known, and no change.
            current = self._display_mode()
            return [current] if current is not None else []
        side, mode = shown
        if not self._writable(mode) or self._read_only_control():
            current = _MODES[side].get(str(mode.value))
            return [current] if current is not None else []
        options = assess_control(mode, context_of(self.coordinator)).options
        return [mapped for raw, mapped in _MODES[side].items() if raw in options]

    @property
    def native_current_temperature(self) -> float | None:
        return temperature(
            self._reading(f"{self.root}/{ROOM_TEMPERATURE}", strict=False)
        )

    @property
    def current_humidity(self) -> float | None:
        resource = self._reading(f"{self.root}/{ROOM_HUMIDITY}", strict=False)
        if (
            resource is None
            or resource.metadata.unit != "%"
            or not plausible_humidity(resource.value)
        ):
            return None
        return float(resource.value)

    def _active_setpoint(self) -> float | None:
        return temperature(
            self._reading(f"{self.root}/{ACTIVE_SETPOINT}", strict=False)
        )

    @property
    def native_target_temperature(self) -> float | None:
        shown = self._shown()
        if shown is None:
            # Only shown: the zone's active setpoint, none while it is off.
            if self._display() is None or self._display_mode() == HVACMode.OFF:
                return None
            return self._active_setpoint()
        side, mode = shown
        if mode.value == "manual":
            return temperature(self._manual(side))
        if mode.value == "auto":
            temporary = self._temporary(side)
            if temporary is not None and temporary.active and temporary.shown:
                return temporary.shown
            return self._active_setpoint()
        return None

    def _settable_temporary(
        self, side: str, *, strict: bool = False, flags: bool = True
    ) -> TemporarySetpoint | None:
        temporary = self._temporary(side, strict=strict, flags=flags)
        return temporary if temporary is not None and temporary.settable else None

    def _target_writable(self) -> bool:
        shown = self._shown()
        if shown is None or self._read_only_control():
            return False
        side, mode = shown
        if mode.value == "manual":
            manual = self._manual(side)
            return manual is not None and self._manual_writable(manual)
        if mode.value == "auto":
            return self._settable_temporary(side) is not None
        return False

    @property
    def supported_features(self) -> Feature:
        features = Feature(0)
        if self._target_writable():
            features |= Feature.TARGET_TEMPERATURE
        if HVACMode.OFF in self.hvac_modes and len(self.hvac_modes) > 1:
            features |= Feature.TURN_OFF
        return features

    def _limits(self) -> tuple[float, float, float | None] | None:
        """Minimum, maximum and step of the writable target, in its unit
        converted to the entity's unit."""
        shown = self._shown()
        if shown is None:
            return None
        side, mode = shown
        unit = temperature_unit(self)
        if mode.value == "auto":
            temporary = self._settable_temporary(side)
            if temporary is None:
                return None
            return (
                temporary.convert(temporary.minimum, unit),
                temporary.convert(temporary.maximum, unit),
                TemperatureDeltaConverter.convert(temporary.step, temporary.unit, unit),
            )
        manual = self._manual(side) if mode.value == "manual" else None
        if manual is None or not self._manual_writable(manual):
            return None
        metadata = manual.metadata
        source = TEMPERATURE_UNITS[metadata.unit or ""]
        if metadata.minimum is None or metadata.maximum is None:
            return None
        step = assess_control(manual, context_of(self.coordinator)).ui_step
        return (
            TemperatureConverter.convert(metadata.minimum, source, unit),
            TemperatureConverter.convert(metadata.maximum, source, unit),
            TemperatureDeltaConverter.convert(step, source, unit) if step else None,
        )

    @property
    def min_temp(self) -> float:
        limits = self._limits()
        return limits[0] if limits else 0

    @property
    def max_temp(self) -> float:
        limits = self._limits()
        return limits[1] if limits else 0

    @property
    def target_temperature_step(self) -> float | None:
        limits = self._limits()
        return limits[2] if limits else None

    def _live_capabilities(self) -> dict[str, Any]:
        attributes = dict(super()._live_capabilities() or {})
        if (step := self.target_temperature_step) is not None:
            attributes["target_temp_step"] = TemperatureDeltaConverter.convert(
                step, temperature_unit(self), self.hass.config.units.temperature_unit
            )
        return attributes

    @property
    def hvac_action(self) -> HVACAction | None:
        return self._action()[0]

    def _action(self) -> tuple[HVACAction | None, tuple[str, ...]]:
        """The calculated action and the readings it combines."""
        display = self._display()
        if display is None:
            return None, ()
        _, mode = display
        if mode is None:
            return None, ()
        mode_path = mode.path
        if str(mode.value).strip().lower() == "off":
            return HVACAction.OFF, (mode_path,)
        circuit = self._circuit()
        if circuit is None:
            return None, ()
        activity = circuit_activity(self.coordinator, circuit)
        inputs = (mode_path, *sorted(activity.derived_from))
        if activity.value in (OFF, DEFROSTING, IDLE):
            return HVACAction(activity.value), inputs
        room = self.native_current_temperature
        setpoint = self._active_setpoint()
        if activity.value not in (HEATING, COOLING) or room is None or setpoint is None:
            return None, ()
        inputs = (
            *inputs,
            f"{self.root}/{ROOM_TEMPERATURE}",
            f"{self.root}/{ACTIVE_SETPOINT}",
        )
        if activity.value == HEATING:
            return (HVACAction.HEATING if room < setpoint else HVACAction.IDLE), inputs
        return (HVACAction.COOLING if room > setpoint else HVACAction.IDLE), inputs

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        display = self._display()
        if display is None:
            return attributes
        side, mode = display
        shown = self._shown()
        attributes["zone_side"] = side
        if shown is None:
            # Only shown: why no change is offered now.
            attributes["write_lock"] = "season" if side == UNKNOWN_SIDE else "mode"
        if not self.supported_features & Feature.TARGET_TEMPERATURE and (
            self.hvac_mode == HVACMode.AUTO
            or (shown is None and self.native_target_temperature is not None)
        ):
            # The program's target, shown without a temperature command.
            attributes[ATTR_TEMPERATURE] = display_temp(
                self.hass,
                self.native_target_temperature,
                temperature_unit(self),
                self.precision,
            )
        active = self._active_setpoint()
        if active is not None:
            attributes["active_setpoint"] = display_temp(
                self.hass, active, temperature_unit(self), self.precision
            )
        temporary = self._temporary(side) if side in _MODES else None
        if temporary is not None and mode is not None:
            attributes["temporary_override"] = mode.value == "auto" and temporary.active
        lock = self._reading(f"{self.root}/{CHILD_LOCK}", strict=False)
        if lock is not None and lock.value in ("on", "off"):
            attributes["child_lock"] = lock.value
        if (circuit := self._circuit()) is not None:
            attributes["heating_circuit"] = circuit.rsplit("/", 1)[-1]
        action, inputs = self._action()
        if action is not None:
            # Calculated, like every derived value, with its inputs.
            attributes["hvac_action_derived_from"] = list(inputs)
        return attributes

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.root)

    # Commands -----------------------------------------------------------

    def _refusal(self, *paths: str) -> ServiceValidationError:
        """Why a change is refused: a setting not used now, or not possible."""
        data = self.coordinator.data or {}
        for path in paths:
            snapshot = data.get(path)
            if snapshot is not None and snapshot.resource.metadata.used is False:
                return ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="control_unused"
                )
        return ServiceValidationError(
            translation_domain=DOMAIN, translation_key="zone_climate_command"
        )

    def _command(self) -> _Command:
        """The current side, writable mode and guards, or the refusal."""
        side = self._side(strict=True)
        mode = self._mode(side, strict=True) if side is not None else None
        guards = self._guards()
        if (
            side is None
            or mode is None
            or guards is None
            or not self._writable(mode)
            or self._read_only_control(strict=True)
        ):
            raise self._refusal(
                *(
                    f"{self.root}/{each}/operationMode"
                    for each in ((side,) if side else (HEAT_SIDE, COOL_SIDE))
                )
            )
        return _Command(side, mode, guards)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if (down := cloud_down_error(self.coordinator)) is not None:
            raise down
        command = self._command()
        await self._async_write_mode(command, hvac_mode)

    async def _async_write_mode(self, command: _Command, hvac_mode: HVACMode) -> None:
        options = assess_control(command.mode, context_of(self.coordinator)).options
        raw = next(
            (
                raw
                for raw, mapped in _MODES[command.side].items()
                if mapped == hvac_mode and raw in options
            ),
            None,
        )
        with counted_write(self.coordinator, self.root, "mode"):
            if raw is None:
                raise self._refusal()
            await async_set_control(
                self.coordinator,
                command.mode.path,
                raw,
                ZONE_OPERATION_MODE_POLICY,
                expected=(command.mode, *command.guards),
            )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (down := cloud_down_error(self.coordinator)) is not None:
            raise down
        value = kwargs.get(ATTR_TEMPERATURE)
        if (
            set(kwargs) - _SERVICE_KEYS
            or isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            raise self._refusal()
        command = self._command()
        requested = kwargs.get("hvac_mode")
        current = _MODES[command.side].get(str(command.mode.value))
        if requested is not None and requested != current:
            raw = next(
                (r for r, m in _MODES[command.side].items() if m == requested), None
            )
            if raw not in ("manual", "auto"):
                raise self._refusal()  # a target needs manual or auto
            # Checked before anything is sent: the target must be possible in
            # the requested mode with the current readings. Its flags may
            # follow the mode (a manual setpoint not used in the mode auto):
            # they are checked on the reading of the target's own write.
            self._raw_target(command.side, raw, float(value), strict=True, flags=False)
            await self._async_write_mode(command, requested)
            # Only after the confirmed mode, with the readings it published.
            command = self._command()
            if command.mode.value != raw:
                raise self._refusal()
        await self._async_write_target(command, float(value))

    def _raw_target(
        self, side: str, mode: str, value: float, *, strict: bool, flags: bool = True
    ) -> tuple[Resource, float, Any]:
        """The resource, value in its unit and policy of a target in a mode.

        Without ``flags`` the target's ``used`` and ``available`` flags are
        not checked here but by its write, on the reading of its request.
        """
        if mode == "auto":
            temporary_path = f"{self.root}/{side}/temporaryRoomSetpoint"
            temporary = self._settable_temporary(side, strict=strict, flags=flags)
            if temporary is None:
                raise self._refusal(temporary_path)
            raw = TemperatureConverter.convert(
                value, temperature_unit(self), temporary.unit
            )
            if {temperature_unit(self), self.hass.config.units.temperature_unit} != {
                temporary.unit
            }:
                # C31: a converted value never lies exactly on the grid; use the
                # nearest grid value, counted from the advertised minimum.
                raw = temporary.on_grid(raw)
            if not temporary.minimum - 1e-7 <= raw <= temporary.maximum + 1e-7:
                raise self._refusal(temporary_path)
            return temporary.resource, raw, ZONE_TEMPORARY_SETPOINT_POLICY
        manual_path = f"{self.root}/{side}/manualRoomSetpoint"
        manual = (
            self._manual(side, strict=strict, flags=flags) if mode == "manual" else None
        )
        policy = (
            number_policy_for_resource(manual, context_of(self.coordinator))
            if manual is not None
            else None
        )
        if manual is None or policy is None:
            raise self._refusal(manual_path)
        native = TEMPERATURE_UNITS[manual.metadata.unit or ""]
        raw = TemperatureConverter.convert(value, temperature_unit(self), native)
        if {temperature_unit(self), self.hass.config.units.temperature_unit} != {
            native
        }:
            # C31: a converted value, such as 70 °F, never lies exactly on the
            # grid; use the nearest grid value, counted from the minimum.
            step = assess_control(manual, context_of(self.coordinator)).ui_step
            base = manual.metadata.minimum or 0.0
            if step is not None and step > 0:
                raw = round(base + round((raw - base) / step) * step, 4)
        try:
            # The rules of the write itself, before a mode is sent for it.
            validate_control_value(manual, raw, context_of(self.coordinator))
        except WriteValidationError as err:
            raise self._refusal(manual_path) from err
        return manual, raw, policy

    async def _async_write_target(self, command: _Command, value: float) -> None:
        with counted_write(self.coordinator, self.root, "target"):
            await self._async_write_target_counted(command, value)

    async def _async_write_target_counted(
        self, command: _Command, value: float
    ) -> None:
        mode = str(command.mode.value)
        if mode not in ("manual", "auto"):
            raise self._refusal()
        resource, raw, policy = self._raw_target(
            command.side, mode, value, strict=True, flags=False
        )
        # The target is read again with the mode and the guards, and its
        # limits and flags come from that reading. A change of its own value
        # does not refuse it: a mode change just sent may move the target
        # with it, and may make it used.
        await async_set_control(
            self.coordinator,
            resource.path,
            raw,
            policy,
            expected=(command.mode, *command.guards),
            fresh=(
                resource,
                *(
                    manual_floor_reading(
                        self.coordinator, f"{self.root}/{command.side}"
                    )
                    if mode == "auto"
                    else ()
                ),
            ),
        )


@dataclass(frozen=True, kw_only=True)
class ZoneClearDescription(EntityDescription):
    """The zone root is the key."""


def zone_clear_button_descriptions(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[ZoneClearDescription, ...]:
    """A zone whose writable temporary heating setpoint takes -1 gets a
    clear button; created disabled when the limits leave -1 out."""
    if context.rrc_plant:
        return ()
    descriptions = []
    for path, resource in resources.items():
        match = _TEMPORARY.fullmatch(path)
        if match is None or f"{match[1]}/heating/operationMode" not in resources:
            continue
        assessment = assess_control(resource, context)
        if (
            assessment.eligible
            and assessment.policy is ZONE_TEMPORARY_SETPOINT_POLICY
            and takes_clear_value(
                resource, assessment.ui_step, ZONE_TEMPORARY_SETPOINT_POLICY
            )
        ):
            descriptions.append(
                ZoneClearDescription(
                    key=match[1],
                    entity_registry_enabled_default=not clear_outside_limits(
                        resource, ZONE_TEMPORARY_SETPOINT_POLICY
                    ),
                )
            )
    return tuple(descriptions)


def track_zone_clear_buttons(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    track_control_entities(
        entry,
        coordinator,
        add_entities,
        zone_clear_button_descriptions,
        lambda owner, description: ZoneTemporaryClearButton(
            owner,
            description.key,
            enabled_default=description.entity_registry_enabled_default,
        ),
    )


class ZoneTemporaryClearButton(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], ButtonEntity
):
    """End the temporary setpoint of a zone (write -1) on its current side.

    Available in the mode ``auto`` while a temporary setpoint is active; the
    read-back must show a negative value. Expected, untested on a real
    device.
    """

    _attr_has_entity_name = True
    # The icon of the heating circuit's clear button (icons.json); the name
    # is built in the profile language like that button's.
    _attr_translation_key = "heating_temporary_setpoint_clear"

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        root: str,
        *,
        enabled_default: bool = True,
    ) -> None:
        super().__init__(coordinator)
        self.root = root
        self._attr_entity_registry_enabled_default = enabled_default
        self.resource_path = f"{root}/{HEAT_SIDE}/temporaryRoomSetpoint"
        self.extra_resource_paths = (
            f"{root}/{HEAT_SIDE}/operationMode",
            f"{root}/{COOL_SIDE}/operationMode",
            f"{root}/{COOL_SIDE}/temporaryRoomSetpoint",
        )
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
        self._climate = ZoneClimate(coordinator, root)

    def _state(
        self, *, strict: bool
    ) -> tuple[Resource, Resource, tuple[Resource, ...]] | None:
        climate = self._climate
        side = climate._side(strict=strict)
        if side is None:
            return None
        mode = climate._mode(side, strict=strict)
        guards = climate._guards() if strict else ()
        temporary = climate._temporary(side, strict=strict)
        if (
            mode is None
            or mode.value != "auto"
            or not climate._writable(mode)
            or guards is None
            or climate._read_only_control(strict=strict)
            or temporary is None
            or not temporary.clearable
            or not temporary.active
        ):
            return None
        return temporary.resource, mode, guards

    @property
    def available(self) -> bool:
        return self._state(strict=False) is not None

    async def async_press(self) -> None:
        if (down := cloud_down_error(self.coordinator)) is not None:
            raise down
        with counted_write(self.coordinator, self.root, "clear"):
            state = self._state(strict=True)
            if state is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="write_validation_failed"
                    if self._state(strict=False) is not None
                    else "zone_temporary_setpoint_not_active",
                )
            resource, mode, guards = state
            await async_set_control(
                self.coordinator,
                resource.path,
                CLEAR_VALUE,
                ZONE_TEMPORARY_SETPOINT_POLICY,
                expected=(mode, resource, *guards),
            )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.root)


def zone_climate_diagnostics(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> list[dict[str, object]]:
    """Per zone with a heating mode: whether its climate is shown and can
    change the mode and the target, and why not; no values or names."""
    report: list[dict[str, object]] = []
    for path in sorted(coordinator.data or {}, key=_zone_order):
        match = _ROOT.fullmatch(path)
        if match is None:
            continue
        climate = ZoneClimate(coordinator, match.group(1))
        side = climate._side()
        mode = climate._mode(side) if side is not None else None
        writable = mode is not None and climate._writable(mode)
        read_only = climate._read_only_control()
        temporary = climate._temporary(side) if side is not None else None
        reason = (
            "circuit_unknown_cooling"
            if side is None and climate._circuit() is None
            else "season_unknown"
            if side is None
            else "mode_invalid"
            if mode is None
            else "mode_not_writable"
            if not writable
            else "control_type_read_only"
            if read_only
            else None
        )
        report.append(
            {
                "zone": f"zone{match.group(2)}",
                "side": side,
                "available": climate.available,
                "mode_writable": writable,
                "target_writable": climate._target_writable(),
                "temporary_setpoint": temporary is not None,
                "temporary_clearable": temporary is not None and temporary.clearable,
                "heating_circuit_assigned": climate._circuit() is not None,
                "hvac_action_known": climate._action()[0] is not None,
                "rejection_reason": reason,
                # Changes made here: counts and the last step, no values.
                "writes": zone_write_stats(coordinator).snapshot(match.group(1)),
            }
        )
    return report


def _zone_order(path: str) -> tuple[int, str]:
    match = _ROOT.fullmatch(path)
    return (int(match.group(2)) if match else 0, path)
