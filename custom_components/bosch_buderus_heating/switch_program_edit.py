"""Z2: rules for changing an EMS/iCom weekly switch program (pure, no HA).

A program (``switchPrograms/A`` or ``/B`` of a heating circuit, ``/A`` of a
hot water circuit) is always written as the whole week, a raw JSON array of
``{"dayOfWeek", "setpoint", "time"}`` points. Every rule here runs before
anything is sent; the gateway's own reaction to a program it does not like
is unknown, so nothing is left for it to judge.

The rules follow what the gateway reports: the time raster, the points per
day and in all, and the levels of the circuit's ``temperatureLevels``. A
program that is not read as a level program (``switchProgramMode``
``level``) is never offered; the program type itself is never written.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .pointt.models import JsonValue, Resource
from .switch_programs import (
    DAYS,
    MINUTES_PER_DAY,
    SwitchPoint,
    SwitchProgramAssessment,
    assess_switch_program,
    is_dhw_switch_program_path,
    is_switch_program_path,
)

# The levels a heating circuit program may use, as the manufacturer's
# heating circuit programs do; another level only where the program already
# uses it and the circuit lists it.
HEATING_LEVELS = frozenset({"eco", "comfort2"})
# The hot water levels a program may use where the circuit lists them.
DHW_LEVELS = frozenset({"off", "low", "eco", "eco_plus", "comfort", "high"})
# The program types whose setpoints are level keys.
LEVEL_MODES = frozenset({"level", "levels"})
_LEVEL_LEAF = re.compile(
    r"^(?P<circuit>/[^/]+/[^/]+)/temperatureLevels/(?P<key>[^/]+)$"
)


def circuit_path(path: str) -> str:
    """The circuit of a program path, e.g. ``/heatingCircuits/hc1``."""
    return path.rsplit("/switchPrograms/", 1)[0]


def contract_of(path: str) -> str:
    """The counter bucket of a program write: ``dhw`` or ``heating``."""
    return "dhw" if is_dhw_switch_program_path(path) else "heating"


@dataclass(frozen=True, slots=True)
class EditableProgram:
    """A program as shown for editing: its points, limits and levels."""

    path: str
    points: tuple[SwitchPoint, ...]
    max_switch_points: int
    max_switch_points_per_day: int
    time_raster: int
    levels: tuple[str, ...]

    def day(self, day: int) -> tuple[SwitchPoint, ...]:
        return day_points(self.points, day)


def day_points(points: Iterable[SwitchPoint], day: int) -> tuple[SwitchPoint, ...]:
    """The points of one weekday (Mo = 0), sorted by time."""
    return tuple(sorted((point for point in points if point.day == day), key=_minute))


def _minute(point: SwitchPoint) -> int:
    return point.minute


def sorted_points(points: Iterable[SwitchPoint]) -> tuple[SwitchPoint, ...]:
    return tuple(sorted(points, key=lambda point: (point.day, point.minute)))


def allowed_levels(
    path: str,
    assessment: SwitchProgramAssessment,
    resources: Mapping[str, Resource],
) -> tuple[str, ...]:
    """The levels a change of this program may use (never guessed).

    ``resources`` are the readings and discovered resources of the gateway;
    a level counts as listed when ``{circuit}/temperatureLevels/<key>`` is
    among them. A heating circuit has ``eco`` and ``comfort2``; a hot water
    circuit the listed keys of the known hot water levels. A level the
    program uses already stays usable only when the circuit lists it.
    """
    circuit = circuit_path(path)
    listed = frozenset(
        match["key"]
        for item in resources
        if (match := _LEVEL_LEAF.fullmatch(item)) is not None
        and match["circuit"] == circuit
    )
    used = frozenset(assessment.levels)
    allowed: frozenset[str] = (
        (DHW_LEVELS & listed) | (used & listed)
        if is_dhw_switch_program_path(path)
        else HEATING_LEVELS | (used & listed)
    )
    ordered = [level for level in assessment.levels if level in allowed]
    ordered += sorted(allowed - set(ordered))
    return tuple(ordered)


def edit_issue(
    resource: Resource | None,
    resources: Mapping[str, Resource],
    *,
    program_mode: Resource | None = None,
) -> str | None:
    """Why a program cannot be changed here, as a stable token, or ``None``.

    ``program_mode`` is the current reading of the heating circuit's
    ``switchProgramMode``, if the circuit reports one.
    """
    if resource is None or not is_switch_program_path(resource.path):
        return "not_switch_program"
    assessment = assess_switch_program(resource)
    if not assessment.supported:
        return assessment.reason or "unsupported"
    metadata = resource.metadata
    if not metadata.writable:
        return "not_writable"
    if not metadata.availability_flags_valid:
        return "invalid_flags"
    if metadata.available is False:
        return "unavailable"
    if program_mode is not None and not is_dhw_switch_program_path(resource.path):
        value = program_mode.value if program_mode.has_value else None
        if value not in LEVEL_MODES:
            return "not_level_program"
    levels = allowed_levels(resource.path, assessment, resources)
    if not levels:
        return "levels_unknown"
    if any(level not in levels for level in assessment.levels):
        return "level_not_listed"
    return None


def editable_program(
    resource: Resource,
    resources: Mapping[str, Resource],
    *,
    program_mode: Resource | None = None,
) -> EditableProgram | None:
    """The program for the editor, or ``None`` where ``edit_issue`` refuses."""
    if edit_issue(resource, resources, program_mode=program_mode) is not None:
        return None
    assessment = assess_switch_program(resource)
    assert assessment.max_switch_points is not None
    assert assessment.max_switch_points_per_day is not None
    assert assessment.time_raster is not None
    return EditableProgram(
        resource.path,
        sorted_points(assessment.points),
        assessment.max_switch_points,
        assessment.max_switch_points_per_day,
        assessment.time_raster,
        allowed_levels(resource.path, assessment, resources),
    )


def week_issue(
    points: Sequence[SwitchPoint],
    program: EditableProgram,
) -> str | None:
    """The first rule a new week breaks, as a stable token, or ``None``.

    Times are whole minutes from 0 to 1439 on the reported raster, at most
    one point per time and day, no more points than reported per day and in
    all, only the program's levels, and at least one point in the week (an
    empty week would mean one level all week and is never sent).
    """
    if not points:
        return "empty_week"
    seen: set[tuple[int, int]] = set()
    per_day = [0] * len(DAYS)
    for point in points:
        if (
            not isinstance(point, SwitchPoint)
            or type(point.day) is not int
            or not 0 <= point.day < len(DAYS)
        ):
            return "invalid_day"
        if type(point.minute) is not int or not 0 <= point.minute < MINUTES_PER_DAY:
            return "invalid_time"
        if point.minute % program.time_raster:
            return "off_raster"
        if (point.day, point.minute) in seen:
            return "duplicate_time"
        seen.add((point.day, point.minute))
        per_day[point.day] += 1
        if per_day[point.day] > program.max_switch_points_per_day:
            return "too_many_per_day"
        if not isinstance(point.level, str) or point.level not in program.levels:
            return "level_not_allowed"
    if len(points) > program.max_switch_points:
        return "too_many_in_all"
    return None


def replace_day(
    points: Iterable[SwitchPoint], day: int, new: Iterable[SwitchPoint]
) -> tuple[SwitchPoint, ...]:
    """The week with one day's points replaced."""
    kept = [point for point in points if point.day != day]
    return sorted_points([*kept, *(SwitchPoint(day, p.minute, p.level) for p in new)])


def copy_day(
    points: Iterable[SwitchPoint], source: int, targets: Iterable[int]
) -> tuple[SwitchPoint, ...]:
    """The week with the points of ``source`` copied onto ``targets``."""
    week = tuple(points)
    template = day_points(week, source)
    for target in sorted(set(targets) - {source}):
        week = replace_day(week, target, template)
    return week


def same_points(left: Iterable[SwitchPoint], right: Iterable[SwitchPoint]) -> bool:
    """Whether two weeks hold the same points, in any order."""
    first, second = tuple(left), tuple(right)
    return len(first) == len(second) and set(first) == set(second)


def encode_week(points: Iterable[SwitchPoint]) -> list[JsonValue]:
    """The wire body: the whole week sorted by day and time, in the field
    order of the manufacturer's apps (``dayOfWeek``, ``setpoint``, ``time``)."""
    return [
        {"dayOfWeek": DAYS[point.day], "setpoint": point.level, "time": point.minute}
        for point in sorted_points(points)
    ]


def fingerprint(resource: Resource) -> tuple[object, ...] | None:
    """What must not change between showing a program and writing it.

    The points (in any order), the limits, the level reference, the write
    permission and the flags; ``None`` for a program that is not supported.
    """
    assessment = assess_switch_program(resource)
    if not assessment.supported:
        return None
    reference = resource.program_fields.get("setpointProperty")
    metadata = resource.metadata
    return (
        resource.path,
        frozenset(assessment.points),
        len(assessment.points),
        assessment.max_switch_points,
        assessment.max_switch_points_per_day,
        assessment.time_raster,
        reference.get("id") if isinstance(reference, dict) else None,
        metadata.writable,
        metadata.available,
        metadata.used,
        metadata.availability_flags_valid,
    )


def points_per_day(resource: Resource) -> list[int] | None:
    """Diagnostics: the number of points of each weekday, no times or levels."""
    assessment = assess_switch_program(resource)
    if not assessment.supported:
        return None
    counts = [0] * len(DAYS)
    for point in assessment.points:
        counts[point.day] += 1
    return counts
