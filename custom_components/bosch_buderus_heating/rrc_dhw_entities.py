"""RRC 2.0 entities: hot water disinfection time, its result and the program.

Owned by work package W3. ``rrc_platforms.track`` calls the tracker of each
platform listed in ``TRACKERS`` once per coordinator; see
``docs/dev/rrc-packages.md``. The switches, selections and numbers of the
hot water contract come from the generic platforms; this module adds what
they cannot show:

- the start time of the thermal disinfection as a time entity, written as
  integer minutes after midnight through the contract's number policy;
- the result of the last disinfection as a short code;
- the level the hot water program sets now, derived from its switch points
  and the controller's clock (read only, disabled by default).

Everything is expected from the controller's data shapes and still
untested on a real controller.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from types import MappingProxyType

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.const import EntityCategory, Platform
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import rrc_contracts, rrc_dhw
from .const import DOMAIN
from .control import async_set_control, track_rrc_entities
from .control_presentation import control_name
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource, grouped_entity_name
from .entity_resources import fresh_resource
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource
from .rrc_contract_dhw import DISINFECTION_TIME_POLICY
from .rrc_platforms import Tracker
from .writes import ControlContext, assess_control, number_policy_for_resource


def _key(path: str) -> str:
    return path.strip("/").replace("/", ":")


def _label(coordinator: BoschBuderusDataUpdateCoordinator, path: str) -> str:
    language = coordinator.hass.config.language
    label = rrc_contracts.name(path, language)
    assert label is not None  # every path of this module has a contract name
    return label


def _minute_of_day(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not float(value).is_integer()
        or not 0 <= value < rrc_dhw.MINUTES_PER_DAY
    ):
        return None
    return int(value)


# ------------------------------------------------------------------ time


@dataclass(frozen=True, kw_only=True)
class RrcDhwTimeDescription(TimeEntityDescription):
    resource_path: str


def _time_descriptions(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[RrcDhwTimeDescription, ...]:
    return tuple(
        RrcDhwTimeDescription(
            key=_key(path),
            resource_path=path,
            translation_key=rrc_contracts.entity_key(path, Platform.TIME.value),
            icon=rrc_contracts.icon(path),
            entity_category=EntityCategory.CONFIG,
            entity_registry_enabled_default=DISINFECTION_TIME_POLICY.enabled_by_default,
        )
        for path, resource in resources.items()
        if rrc_dhw.DISINFECTION_TIME.fullmatch(path)
        and number_policy_for_resource(resource, context) is DISINFECTION_TIME_POLICY
    )


class RrcDhwTime(CoordinatorEntity[BoschBuderusDataUpdateCoordinator], TimeEntity):
    """The start time of the thermal disinfection (minutes after midnight)."""

    _attr_has_entity_name = True
    entity_description: RrcDhwTimeDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: RrcDhwTimeDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self.resource_path = description.resource_path
        self._attr_name = control_name(
            coordinator,
            self.resource_path,
            _label(coordinator, self.resource_path),
            contract=True,
        )
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:{description.key}:control"
        )

    @property
    def _current(self) -> Resource | None:
        """A fresh reading the time policy still accepts (S1, S3, S11)."""
        snapshot = (self.coordinator.data or {}).get(self.resource_path)
        if (
            snapshot is None
            or not snapshot.available
            or snapshot.freshness is not Freshness.FRESH
            or number_policy_for_resource(
                snapshot.resource, self.coordinator.control_context()
            )
            is not DISINFECTION_TIME_POLICY
        ):
            return None
        return snapshot.resource

    @property
    def available(self) -> bool:
        return super().available and self._current is not None

    @property
    def native_value(self) -> time | None:
        resource = self._current
        if resource is None or not resource.has_value:
            return None
        minute = _minute_of_day(resource.value)
        return time(minute // 60, minute % 60) if minute is not None else None

    async def async_set_value(self, value: time) -> None:
        if value.second or value.microsecond or value.tzinfo is not None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="write_validation_failed"
            )
        minute = value.hour * 60 + value.minute
        resource = self._current
        if resource is not None:
            # S4: a time off the step would be refused anyway; say why.
            step = assess_control(resource, self.coordinator.control_context()).ui_step
            ticks = (minute - (resource.metadata.minimum or 0.0)) / step if step else 0
            if not math.isclose(ticks, round(ticks), abs_tol=1e-7):
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="rrc_time_of_day_step",
                    translation_placeholders={"step": f"{step:g}"},
                )
        await async_set_control(
            self.coordinator, self.resource_path, minute, DISINFECTION_TIME_POLICY
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.resource_path)


# ---------------------------------------------------------------- sensors


@dataclass(frozen=True, kw_only=True)
class RrcDhwSensorDescription(SensorEntityDescription):
    resource_path: str
    role: str


def _sensor_descriptions(
    resources: Mapping[str, Resource], _context: ControlContext
) -> tuple[RrcDhwSensorDescription, ...]:
    descriptions: list[RrcDhwSensorDescription] = []
    for path, resource in resources.items():
        if resource.references or resource.has_values or not resource.has_value:
            continue
        if (
            rrc_dhw.DISINFECTION_RESULT.fullmatch(path)
            and resource.metadata.resource_type == "stringValue"
        ):
            role = "result"
        elif (
            rrc_dhw.PROGRAM_WEEK.fullmatch(path)
            and resource.metadata.resource_type == "dhwProgram"
        ):
            role = "level"
        else:
            continue
        # The level belongs to the program (``…:programs:pgN:level``): an ID
        # below the week structure would count as one of its former field
        # entities and be removed at the next start.
        owner = path.removesuffix("/week") if role == "level" else path
        descriptions.append(
            RrcDhwSensorDescription(
                key=f"{_key(owner)}:{role}",
                resource_path=path,
                role=role,
                translation_key=rrc_contracts.entity_key(path, Platform.SENSOR.value),
                icon="mdi:bacteria-outline"
                if role == "result"
                else "mdi:calendar-clock",
                entity_category=EntityCategory.DIAGNOSTIC if role == "result" else None,
                entity_registry_enabled_default=False,
                device_class=SensorDeviceClass.ENUM if role == "level" else None,
                options=list(rrc_dhw.LEVELS) if role == "level" else None,
            )
        )
    return tuple(descriptions)


class RrcDhwSensor(CoordinatorEntity[BoschBuderusDataUpdateCoordinator], SensorEntity):
    """The last disinfection result, or the program's level now."""

    _attr_has_entity_name = True
    entity_description: RrcDhwSensorDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: RrcDhwSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self.resource_path = description.resource_path
        # O51: the level also reads the controller's clock.
        self.extra_resource_paths: tuple[str, ...] = (
            (rrc_dhw.CLOCK_PATH,) if description.role == "level" else ()
        )
        name = _label(coordinator, self.resource_path)
        match = rrc_dhw.PROGRAM_WEEK.fullmatch(self.resource_path)
        if match is not None and match[2] != "1":
            # A second program of the circuit keeps its number.
            name = f"{name} ({match[2]})"
        self._attr_name = grouped_entity_name(coordinator, self.resource_path, name)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{description.key}"

    @property
    def _resource(self) -> Resource | None:
        return fresh_resource(self.coordinator, self.resource_path)

    @property
    def available(self) -> bool:
        resource = self._resource
        if not super().available or resource is None:
            return False
        if self.entity_description.role == "level":
            return rrc_dhw.parse_program(resource) is not None
        return True

    @property
    def native_value(self) -> str | None:
        resource = self._resource
        if resource is None:
            return None
        if self.entity_description.role == "result":
            return rrc_dhw.result_token(resource)
        program = rrc_dhw.parse_program(resource)
        clock = (self.coordinator.data or {}).get(rrc_dhw.CLOCK_PATH)
        if (
            program is None
            or clock is None
            or not clock.available
            or clock.freshness is not Freshness.FRESH
        ):
            return None
        now = rrc_dhw.controller_now(
            clock.resource, clock.last_success, dt_util.utcnow()
        )
        if now is None:
            return None
        point = rrc_dhw.current_point(
            program, now.weekday(), now.hour * 60 + now.minute
        )
        return rrc_dhw.level_option(point.level) if point is not None else None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.resource_path)


def _track_time(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(entry, coordinator, add, _time_descriptions, RrcDhwTime)


def _track_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(entry, coordinator, add, _sensor_descriptions, RrcDhwSensor)


TRACKERS: Mapping[Platform, Tracker] = MappingProxyType(
    {Platform.TIME: _track_time, Platform.SENSOR: _track_sensors}
)
