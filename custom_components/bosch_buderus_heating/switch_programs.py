"""Read-only PointT heating and hot-water switch programs (weekly time programs)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .pointt.models import Resource

DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
MINUTES_PER_DAY = 24 * 60
MAX_SWITCH_POINTS = 168
MAX_SWITCH_POINTS_PER_DAY = 48
_PROGRAM_PATH = re.compile(
    r"^(?P<circuit>/(?:heatingCircuits/hc|dhwCircuits/dhw)[1-9][0-9]?)"
    r"/switchPrograms/(?P<program>[AB])$"
)
_LEVEL_KEY = re.compile(r"^[a-z][a-z0-9]{0,31}$")
_POINT_FIELDS = frozenset({"dayOfWeek", "time", "setpoint"})


def is_switch_program_path(path: str) -> bool:
    """Recognize exact program leaves without inventing circuit or program IDs."""
    return _PROGRAM_PATH.fullmatch(path) is not None


def is_dhw_switch_program_path(path: str) -> bool:
    """Return whether a program belongs to a hot-water circuit."""
    return is_switch_program_path(path) and path.startswith("/dhwCircuits/")


@dataclass(frozen=True, slots=True)
class SwitchPoint:
    """One recurring local wall-clock switch point."""

    day: int
    minute: int
    level: str

    @property
    def label(self) -> str:
        """Return `HH:MM level` without a calendar date or time zone."""
        hours, minutes = divmod(self.minute, 60)
        return f"{hours:02d}:{minutes:02d} {self.level}"


@dataclass(frozen=True, slots=True)
class SwitchProgramAssessment:
    """One shared decision for entity creation, state and attributes."""

    supported: bool
    reason: str | None = None
    points: tuple[SwitchPoint, ...] = ()
    max_switch_points: int | None = None
    max_switch_points_per_day: int | None = None
    time_raster: int | None = None

    @property
    def levels(self) -> tuple[str, ...]:
        """Return the distinct levels in first-use order."""
        return tuple(dict.fromkeys(point.level for point in self.points))

    def active_level(self, day: int, minute: int) -> str | None:
        """Return the level in effect at a weekday (Mo=0) and minute of day.

        The program repeats weekly, so the last point of the previous days
        stays in effect until the first point of the requested day.
        """
        point = self._last_point_at_or_before(day * MINUTES_PER_DAY + minute)
        return None if point is None else point.level

    def next_switch_in_minutes(self, day: int, minute: int) -> int | None:
        """Return the minutes until the next switch point, at most one week."""
        if not self.points:
            return None
        now = day * MINUTES_PER_DAY + minute
        week = 7 * MINUTES_PER_DAY
        return min(
            (point.day * MINUTES_PER_DAY + point.minute - now - 1) % week + 1
            for point in self.points
        )

    def schedule(self) -> dict[str, list[str]]:
        """Return the points per weekday, sorted, for state attributes."""
        days: dict[str, list[str]] = {}
        for point in sorted(self.points, key=lambda item: (item.day, item.minute)):
            days.setdefault(DAYS[point.day], []).append(point.label)
        return days

    def _last_point_at_or_before(self, week_minute: int) -> SwitchPoint | None:
        if not self.points:
            return None
        week = 7 * MINUTES_PER_DAY
        return min(
            self.points,
            key=lambda item: (
                (week_minute - item.day * MINUTES_PER_DAY - item.minute) % week
            ),
        )


def assess_switch_program(resource: Resource) -> SwitchProgramAssessment:
    """Validate one reviewed `switchProgram` shape strictly and read only."""
    match = _PROGRAM_PATH.fullmatch(resource.path)
    if match is None:
        return SwitchProgramAssessment(False, "not_switch_program")
    fields = resource.program_fields
    if (
        resource.metadata.resource_type != "switchProgram"
        or resource.references
        or resource.has_value
        or resource.has_values
        or resource.unknown_fields
    ):
        return SwitchProgramAssessment(False, "unsupported_schema")
    reference = fields.get("setpointProperty")
    expected_levels = f"{match['circuit']}/temperatureLevels"
    if not isinstance(reference, dict) or reference.get("id") != expected_levels:
        return SwitchProgramAssessment(False, "unsupported_setpoint_property")
    max_points = _bounded_int(fields.get("maxNbOfSwitchPoints"), MAX_SWITCH_POINTS)
    max_per_day = _bounded_int(
        fields.get("maxNbOfSwitchPointsPerDay"), MAX_SWITCH_POINTS_PER_DAY
    )
    raster = _bounded_int(fields.get("switchPointTimeRaster"), 60)
    if (
        max_points is None
        or max_per_day is None
        or raster is None
        or MINUTES_PER_DAY % raster
    ):
        return SwitchProgramAssessment(False, "invalid_limits")
    raw_points = fields.get("switchPoints")
    if not isinstance(raw_points, list):
        return SwitchProgramAssessment(False, "missing_switch_points")
    if not raw_points:
        return SwitchProgramAssessment(False, "no_switch_points")
    if len(raw_points) > max_points:
        return SwitchProgramAssessment(False, "too_many_switch_points")
    points: list[SwitchPoint] = []
    seen: set[tuple[int, int]] = set()
    per_day = [0] * len(DAYS)
    for item in raw_points:
        point = _switch_point(item, raster)
        if point is None:
            return SwitchProgramAssessment(False, "invalid_switch_point")
        if (point.day, point.minute) in seen:
            return SwitchProgramAssessment(False, "duplicate_switch_point")
        seen.add((point.day, point.minute))
        per_day[point.day] += 1
        if per_day[point.day] > max_per_day:
            return SwitchProgramAssessment(False, "too_many_switch_points")
        points.append(point)
    return SwitchProgramAssessment(
        True,
        points=tuple(points),
        max_switch_points=max_points,
        max_switch_points_per_day=max_per_day,
        time_raster=raster,
    )


def _switch_point(item: object, raster: int) -> SwitchPoint | None:
    if not isinstance(item, dict) or set(item) != _POINT_FIELDS:
        return None
    day, minute, level = item["dayOfWeek"], item["time"], item["setpoint"]
    if (
        not isinstance(day, str)
        or day not in DAYS
        or type(minute) is not int
        or not 0 <= minute < MINUTES_PER_DAY
        or minute % raster
        or not isinstance(level, str)
        or _LEVEL_KEY.fullmatch(level) is None
    ):
        return None
    return SwitchPoint(DAYS.index(day), minute, level)


def _bounded_int(value: object, maximum: int) -> int | None:
    if type(value) is not int or not 1 <= value <= maximum:
        return None
    return value
