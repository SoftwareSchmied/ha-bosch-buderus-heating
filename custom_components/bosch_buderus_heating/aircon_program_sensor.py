"""The weekly programs of an air conditioner on a PointT gateway as sensors.

One enum sensor per program the unit reports (``program1`` ... ``program9``):
the operation mode the program sets now, its switch points and limits as
attributes. It is the target of the action ``set_switch_points``; the same
program can be changed in the options dialog. Like the heating programs it
is enabled when first created if the unit reports the program as writable,
otherwise disabled; an existing entity keeps its setting. Expected, untested
on a real installation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .aircon import AirconCoordinator
from .aircon_entity import AirconEntity, option_key, track_aircon_entities
from .aircon_program_rules import MODES, OFF, Setting, parse_setting
from .platform_scope import PlatformScope
from .switch_program_edit import edit_issue
from .switch_programs import DAYS, SwitchProgramAssessment, assess_switch_program

# The enum options: ``off`` and the modes as Home Assistant options.
STATES: Final = (OFF, *(option_key(mode) for mode in MODES))


@dataclass(frozen=True, kw_only=True)
class AirconProgramDescription(SensorEntityDescription):
    """A program sensor; ``value_kind`` marks it as a target of the action."""

    resource_path: str
    value_kind: str = "switch_program"


class AirconProgramSensor(AirconEntity, SensorEntity):
    """The mode a weekly program of the unit sets now."""

    _attr_translation_key = "aircon_switch_program"
    _attr_device_class = SensorDeviceClass.ENUM
    # The program can be long; it stays out of the recorder.
    _unrecorded_attributes = frozenset({"switch_points"})
    entity_description: AirconProgramDescription

    def __init__(self, coordinator: AirconCoordinator, path: str) -> None:
        super().__init__(coordinator, path.strip("/").replace("/", ":"))
        self.entity_description = AirconProgramDescription(key=path, resource_path=path)
        self._attr_translation_placeholders = {"number": path[-1]}
        self._attr_options = list(STATES)
        # A writable program is a target of the action, as on the heating
        # circuits; only the first creation uses it.
        resource = coordinator.resource(path)
        self._attr_entity_registry_enabled_default = (
            resource is not None and resource.metadata.writable
        )

    def _assessment(self) -> SwitchProgramAssessment | None:
        resource = self.coordinator.resource(self.entity_description.resource_path)
        if resource is None:
            return None
        assessment = assess_switch_program(resource)
        return assessment if assessment.supported else None

    @property
    def available(self) -> bool:
        return super().available and self._assessment() is not None

    def _now_setting(self) -> Setting | None:
        assessment = self._assessment()
        if assessment is None:
            return None
        now = dt_util.now()
        level = assessment.active_level(now.weekday(), now.hour * 60 + now.minute)
        # Before the first point of a day the unit is off.
        return parse_setting(level if level is not None else OFF)

    @property
    def native_value(self) -> str | None:
        setting = self._now_setting()
        return None if setting is None else option_key(setting.mode)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        assessment = self._assessment()
        if assessment is None:
            return None
        schedule = assessment.schedule()
        setting = self._now_setting()
        resource = self.coordinator.resource(self.entity_description.resource_path)
        assert resource is not None
        issue = edit_issue(resource, self.coordinator.program_readings())
        return {
            "switch_points": {day: schedule[day] for day in DAYS if day in schedule},
            "program_mode": "aircon",
            "active": self.coordinator.program_active(
                self.entity_description.resource_path
            ),
            "temperature": setting.temperature if setting is not None else None,
            "fan_speed": setting.fan if setting is not None else None,
            "editable": issue is None,
        }


def _programs(
    coordinator: AirconCoordinator,
) -> Iterable[tuple[str, partial[AirconProgramSensor]]]:
    for path in coordinator.program_paths():
        resource = coordinator.resource(path)
        if resource is not None and assess_switch_program(resource).supported:
            yield path, partial(AirconProgramSensor, coordinator, path)


def track_aircon_program_sensors(
    scope: PlatformScope, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add a sensor for each readable program once."""
    track_aircon_entities(scope, async_add_entities, _programs)
