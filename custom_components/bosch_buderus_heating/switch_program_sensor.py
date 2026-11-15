"""The program sensors of every program kind (pure, no Home Assistant).

A level program (heating circuit, hot water, cooling, ventilation) is an enum
sensor of the level in effect; an absolute program (heating circuit with
``switchProgramMode`` ``absolute``, zones) and the WDDW2 hot water program
show the scheduled temperature. A switched-off point (``"0.0"``, ``"off"``)
shows no temperature and the attribute ``scheduled: off``.

The sensor of a program the gateway reports as writable is enabled by
default: it is the target of the action ``set_switch_points``. Its kind is
taken from the reading it is created from; a reading of another kind later
shows no state until the entities are set up again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .pointt.models import Resource
from .switch_program_edit import (
    VENTILATION_LEVELS,
    allowed_levels,
    wddw2_range,
)
from .switch_programs import (
    DAYS,
    SwitchProgramAssessment,
    assess_switch_program,
    program_kind,
)

# The enum options every level program of a kind has, before its own levels.
KNOWN_LEVELS: dict[str, tuple[str, ...]] = {
    "heating_switch_program": ("comfort1", "comfort2", "comfort3", "eco", "off"),
    "dhw_switch_program": ("low", "eco", "high", "off"),
    "cooling_switch_program": ("on", "off"),
    "ventilation_switch_program": VENTILATION_LEVELS,
}
_OFF = frozenset({"0.0", "off"})


@dataclass(frozen=True, slots=True)
class ProgramSensorKind:
    """How the sensor of one program is described."""

    translation_key: str
    numeric: bool


def sensor_kind(resource: Resource) -> ProgramSensorKind:
    """The sensor kind for the reading a program sensor is created from."""
    kind = program_kind(resource.path)
    key = kind.key if kind is not None else "heating"
    assessment = assess_switch_program(resource)
    if key == "wddw2":
        return ProgramSensorKind("wddw2_switch_program", True)
    if key == "zone" or assessment.value_type == "absolute":
        return ProgramSensorKind("absolute_switch_program", True)
    return ProgramSensorKind(f"{key}_switch_program", False)


def enabled_by_default(resource: Resource) -> bool:
    """D9: a program the gateway reports as writable is the action's target."""
    return resource.metadata.writable and assess_switch_program(resource).supported


def options(resource: Resource, translation_key: str) -> list[str] | None:
    """The enum options of a level program: known levels, then its own."""
    if translation_key not in KNOWN_LEVELS:
        return None
    assessment = assess_switch_program(resource)
    used = assessment.levels if assessment.value_type == "level" else ()
    return list(dict.fromkeys((*KNOWN_LEVELS[translation_key], *used)))


def state(
    assessment: SwitchProgramAssessment, numeric: bool, day: int, minute: int
) -> str | float | None:
    """The level, or the temperature, in effect; ``None`` without one.

    A reading of another kind than the sensor was created for has no state.
    """
    if not assessment.supported or (assessment.value_type != "level") != numeric:
        return None
    level = assessment.active_level(day, minute)
    if level is None or not numeric:
        return level
    if level in _OFF:
        return None
    return float(level)


def attributes(
    resource: Resource,
    readings: Mapping[str, Resource],
    numeric: bool,
    day: int,
    minute: int,
) -> dict[str, object] | None:
    """The weekly switch points, the reported limits and the allowed
    setpoints; never a URI."""
    assessment = assess_switch_program(resource)
    if not assessment.supported:
        return None
    schedule = assessment.schedule()
    result: dict[str, object] = {
        "switch_points": {day_: schedule[day_] for day_ in DAYS if day_ in schedule},
        "max_switch_points": assessment.max_switch_points,
        "max_switch_points_per_day": assessment.max_switch_points_per_day,
        "time_raster_minutes": assessment.time_raster,
    }
    if assessment.kind not in ("heating", "dhw") or assessment.value_type != "level":
        result["program_mode"] = assessment.value_type
    if assessment.value_type == "level":
        levels = allowed_levels(resource.path, assessment, readings)
        if levels:
            result["allowed_levels"] = list(levels)
    elif assessment.value_type == "degrees":
        bounds = wddw2_range(readings)
        if bounds is not None:
            result["min_setpoint"], result["max_setpoint"] = bounds
    else:
        result["min_setpoint"] = assessment.minimum
        result["max_setpoint"] = assessment.maximum
        result["setpoint_step"] = assessment.step
    if numeric and assessment.value_type != "level":
        level = assessment.active_level(day, minute)
        if level in _OFF:
            result["scheduled"] = "off"
    return result
