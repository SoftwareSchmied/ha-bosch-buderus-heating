"""CT-C03, CT-C04: one climate entity per zone of an RRC 2.0 room controller.

``heat`` is the mode ``manual`` and ``auto`` the mode ``clock``; the
controller has no off mode. A target set in ``manual`` changes the manual
setpoint (confirmed on a TC100.2). A target set in ``clock`` is a temporary
setpoint (``clockOverride/temperatureHeating``) that lasts until the next
switch point of the program (P23, P33); that write and the mode with a
target are confirmed on a TC100.2 as well (``rrc_confirmations``). A mode is
never changed on the side: a temperature with a mode writes the mode first
and, once it is confirmed, the target.

The entity is available while the mode is valid; the room temperature may be
unknown (ZONE-07). A zone the controller reports with a zone or
communication error, or in standby, takes no change of the mode or the
target; the active away mode locks the target (ZONE-02, ZONE-05). In these
states, with an open window and with a boost, the target shown is the
controller's active setpoint (ZONE-08). All of these locks are expected,
untested.
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
from .coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    SnapshotSource,
)
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
    rrc_contract_zone.WINDOW_STATUS,
)
# The system and list readings the locks and the shown target depend on.
CONTEXT_PATHS: Final = (rrc_contract_zone.ZONE_LIST, rrc_contract_zone.AWAY_ENABLED)
_ACTIONS: Final = {"heat_request": HVACAction.HEATING, "idle": HVACAction.IDLE}
# A special function of the zone after which the active setpoint is shown.
_FOLLOWS_ACTIVE: Final = frozenset(
    {
        rrc_contract_zone.FUNCTION_BOOST,
        rrc_contract_zone.FUNCTION_WINDOW_OPEN,
        rrc_contract_zone.FUNCTION_STANDBY,
    }
)
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


def zone_lock_now(
    coordinator: BoschBuderusDataUpdateCoordinator, root: str, *, setpoint: bool
) -> str | None:
    """The lock of a zone's room control from the current readings
    (``rrc_contract_zone.zone_lock``): its status, the zone list and the
    away mode."""
    paths = (f"{root}/{rrc_contract_zone.STATUS}", *CONTEXT_PATHS)
    readings = {path: zone_reading(coordinator, path) for path in paths}
    return rrc_contract_zone.zone_lock(
        root,
        {path: resource for path, resource in readings.items() if resource},
        setpoint=setpoint,
    )


def refuse_locked(
    coordinator: BoschBuderusDataUpdateCoordinator, root: str, *, setpoint: bool
) -> None:
    """ZONE-02, ZONE-05: refuse a change of a locked zone, nothing sent."""
    lock = zone_lock_now(coordinator, root, setpoint=setpoint)
    if lock is not None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=rrc_contract_zone.LOCK_KEYS[lock],
        )


def away_guard(coordinator: BoschBuderusDataUpdateCoordinator) -> tuple[Resource, ...]:
    """S5: the away reading, read again with the mode before a setpoint is
    written; a change on the controller refuses the write."""
    away = zone_reading(coordinator, rrc_contract_zone.AWAY_ENABLED, strict=True)
    return (away,) if away is not None else ()


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
            "active_setpoint",
            "next_setpoint_advanced",
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
        self.extra_resource_paths = (
            *(f"{root}/{leaf}" for leaf in LEAVES),
            *CONTEXT_PATHS,
        )

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
        """A temporary setpoint acts only in the mode ``clock`` (ZONE-10)."""
        mode = self._mode()
        value = self._value(OVERRIDE)
        return (
            mode is not None
            and mode.value == "clock"
            and value is not None
            and value != 0
        )

    def _held(self, leaf: str) -> Resource | None:
        """ZONE-03: the last reading of a target path that an unconfirmed
        write made stale, until it is read again (within a minute). It keeps
        the limits and the target control shown; a change still needs a
        fresh reading."""
        path = f"{self.root}/{leaf}"
        snapshot = (self.coordinator.data or {}).get(path)
        if (
            snapshot is None
            or snapshot.source is not SnapshotSource.WRITE
            or snapshot.freshness is not Freshness.STALE
            or snapshot.resource.path != path
            or rrc.scalar_value(snapshot.resource) is None
        ):
            return None
        return snapshot.resource

    def _lock(self, *, setpoint: bool) -> str | None:
        return zone_lock_now(self.coordinator, self.root, setpoint=setpoint)

    def _function(self) -> str | None:
        """The special function the zone list shows (open window, boost,
        standby); ``None`` when unknown."""
        return rrc_contract_zone.function_icon(
            zone_reading(self.coordinator, rrc_contract_zone.ZONE_LIST), self.root
        )

    def _follows_active(self) -> bool:
        """ZONE-08: whether the active setpoint differs from the written
        target for a known reason (a lock, an open window, a boost)."""
        if self._lock(setpoint=True) is not None:
            return True
        icon = self._function()
        if icon in _FOLLOWS_ACTIVE:
            return True
        leaf = self._detail(rrc_contract_zone.WINDOW_STATUS)
        return (
            rrc_contract_zone.window_open(
                icon, leaf if isinstance(leaf, bool) else None
            )
            is True
        )

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
        if resource is None and not strict:
            resource = self._held(leaf)
        if resource is None:
            return None
        policy = number_policy_for_resource(resource, context_of(self.coordinator))
        metadata = resource.metadata
        if policy is None or metadata.minimum is None or metadata.maximum is None:
            return None
        minimum, maximum = metadata.minimum, metadata.maximum
        if leaf == OVERRIDE:
            # ZONE-12: the manual setpoint's limits count whatever its flags
            # say; in the mode clock it may be reported as not used.
            manual = self._reading(MANUAL, strict=strict)
            if manual is None and not strict:
                manual = self._held(MANUAL)
            floor = manual.metadata.minimum if manual is not None else None
            if floor is None or not math.isfinite(floor):
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
        # ZONE-07: the room temperature may be unknown (``used`` false).
        return self._mode() is not None

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
        if not self.available:
            return None
        if self._lock(setpoint=False) == "standby":
            return HVACAction.OFF
        status = self._detail(rrc_contract_zone.STATUS)
        return _ACTIONS.get(status) if isinstance(status, str) else None

    @property
    def native_current_temperature(self) -> float | None:
        return self._value(ACTUAL) if self.available else None

    @property
    def native_target_temperature(self) -> float | None:
        mode = self._mode()
        if not self.available or mode is None:
            return None
        active = self._value(ACTIVE)
        if active is not None and self._follows_active():
            return active
        if mode.value == "manual":
            manual = self._value(MANUAL)
            # ZONE-03: while an unconfirmed write is read again, the active
            # setpoint rather than none.
            return manual if manual is not None else active
        # P06: a temporary setpoint of 0 means none; the program's applies.
        override = self._value(OVERRIDE)
        if override is not None and override != 0:
            return override
        return active

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
        if override and next_setpoint is not None:
            # ZONE-06: the next switch point is advanced.
            attributes["next_setpoint_advanced"] = rrc_contract_zone.advanced(
                self._value(OVERRIDE), next_setpoint
            )
        minutes = _number(self._detail(rrc_contract_zone.MINUTES_TO_NEXT))
        if minutes is not None:
            attributes["minutes_to_next_setpoint"] = (
                int(minutes) if minutes.is_integer() else minutes
            )
        status = self._detail(rrc_contract_zone.STATUS)
        if isinstance(status, str):
            attributes["zone_status"] = status
        active = self._value(ACTIVE)
        if active is not None:
            attributes["active_setpoint"] = display_temp(
                self.hass, active, unit, self.precision
            )
        away = rrc_contract_zone.away_active(
            zone_reading(self.coordinator, rrc_contract_zone.AWAY_ENABLED)
        )
        if away is not None:
            attributes["away_mode"] = away
        if (lock := self._lock(setpoint=True)) is not None:
            attributes["control_lock"] = lock
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
        if rrc.mode_locked(mode):
            # P04, ZONE-11: a locked mode is refused with its own message,
            # also when the controller marks it as not writable.
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="rrc_mode_locked"
            )
        refuse_locked(self.coordinator, self.root, setpoint=False)
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
        refuse_locked(self.coordinator, self.root, setpoint=True)
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
            self.coordinator,
            resource.path,
            raw,
            policy,
            expected=(mode, *away_guard(self.coordinator)),
        )


def _invalid() -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key="rrc_zone_climate_command"
    )
