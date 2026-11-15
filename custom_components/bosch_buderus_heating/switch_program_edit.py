"""Z2: rules for changing a PointT weekly switch program (pure, no HA).

A program is always written as the whole week of ``{"dayOfWeek",
"setpoint", "time"}`` points: a raw JSON array, or for the WDDW2 hot water
program the object ``{"switchPoints": [...]}`` (``switch_programs.KINDS``).
Every rule here runs before anything is sent; the gateway's own reaction to
a program it does not like is unknown, so nothing is left for it to judge.

The rules follow what the gateway reports: the time raster, the points per
day and in all, the levels of the circuit (``temperatureLevels``, the cooling
level ``on``, the ventilation levels) or the range and step of an absolute
program. Nothing is taken from the app's own fallback values: without a
reported limit, level set or range a program is shown, never changed.

A heating circuit program is changed only with a current reading of its
program type (``switchProgramMode``) that matches its points: a level
program as ``level``/``levels``, an absolute program as ``absolute``. The
write reads it again right before sending. A program the gateway reports as
not used (``used: false``) is not offered either. The program type itself is
never written.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .pointt.models import JsonValue, Resource
from .switch_programs import (
    DAYS,
    MINUTES_PER_DAY,
    SwitchPoint,
    SwitchProgramAssessment,
    ValueType,
    assess_switch_program,
    canonical_number,
    is_dhw_switch_program_path,
    is_switch_program_path,
    program_circuit,
    program_kind,
)

# The levels a heating circuit program may use; another level only where the
# program already uses it and the circuit lists it.
HEATING_LEVELS = frozenset({"eco", "comfort2"})
# The hot water levels a program may use where the circuit lists them.
DHW_LEVELS = frozenset({"off", "low", "eco", "eco_plus", "comfort", "high"})
# The cooling program: ``off`` is fixed in the app, ``on`` needs the circuit's
# cooling level ``on``.
COOLING_LEVELS = frozenset({"on", "off"})
# The ventilation levels the app knows as the endings of the references of
# ``ventilationLevels``.
VENTILATION_LEVELS = ("min", "red", "nom", "max", "dem", "off")
# The program types whose setpoints are level keys.
LEVEL_MODES = frozenset({"level", "levels"})
ABSOLUTE_MODES = frozenset({"absolute"})
# The setpoint "off" of an absolute program (only where the reading has it).
ABSOLUTE_OFF = "0.0"
_LEVEL_LEAF = re.compile(
    r"^(?P<circuit>/[^/]+/[^/]+)/temperatureLevels/(?P<key>[^/]+)$"
)
_WDDW2_RANGE = ("/dhwCircuits/dhw1/manualSetpoint", "/dhwCircuits/dhw1/manualsetpoint")


def circuit_path(path: str) -> str:
    """The circuit of a program path, e.g. ``/heatingCircuits/hc1``."""
    return program_circuit(path) or path.rsplit("/switchPrograms/", 1)[0]


def kind_key(path: str) -> str | None:
    kind = program_kind(path)
    return None if kind is None else kind.key


def program_mode_path(path: str) -> str | None:
    """The program type of a heating circuit program; ``None`` otherwise."""
    if kind_key(path) != "heating":
        return None
    return f"{circuit_path(path)}/switchProgramMode"


def program_mode_issue(
    path: str, program_mode: Resource | None, value_type: ValueType | None = "level"
) -> str | None:
    """Why the program type of a current reading refuses a change, or ``None``."""
    if program_mode is None or program_mode.path != program_mode_path(path):
        return "program_mode_unknown"
    value = program_mode.value if program_mode.has_value else None
    if value_type == "absolute":
        return None if value in ABSOLUTE_MODES else "program_mode_not_absolute"
    return None if value in LEVEL_MODES else "not_level_program"


def contract_of(path: str) -> str:
    """The counter bucket of a program write: the program kind."""
    return kind_key(path) or "heating"


@dataclass(frozen=True, slots=True)
class NumericRules:
    """What a number of a numeric program must satisfy."""

    minimum: float
    maximum: float
    step: float
    # The setpoint that switches off ("0.0", "off"), if it may be used.
    off_value: str | None
    numbers_as_text: bool
    # Whole degrees (WDDW2) instead of decimals.
    degrees: bool


@dataclass(frozen=True, slots=True)
class EditableProgram:
    """A program as shown for editing: its points, limits and setpoints."""

    path: str
    points: tuple[SwitchPoint, ...]
    max_switch_points: int
    max_switch_points_per_day: int
    time_raster: int
    levels: tuple[str, ...]
    kind: str = "heating"
    value_type: ValueType = "level"
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    # The setpoint that switches off ("0.0", "off"), if it may be used.
    off_value: str | None = None
    # Numbers are written as text (True) or as JSON numbers (False).
    numbers_as_text: bool | None = True
    body: str = "array"
    carry_over: bool = True

    def day(self, day: int) -> tuple[SwitchPoint, ...]:
        return day_points(self.points, day)

    @property
    def numeric(self) -> bool:
        return self.value_type != "level"


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
    program uses already stays usable only when the circuit lists it. A
    cooling program has ``off`` and, with a readable cooling level ``on``,
    ``on``; a ventilation program the known levels its unit lists. An
    absolute program has numbers instead of levels: none.
    """
    if assessment.value_type not in (None, "level"):
        return ()
    circuit = circuit_path(path)
    kind = kind_key(path)
    used = frozenset(assessment.levels)
    if kind == "cooling":
        allowed = (
            COOLING_LEVELS
            if _readable_number(resources.get(f"{circuit}/cooling/temperatureLevels/on"))
            else frozenset()
        )
    elif kind == "ventilation":
        allowed = frozenset(ventilation_levels(resources.get(f"{circuit}/ventilationLevels")))
    else:
        listed = frozenset(
            match["key"]
            for item in resources
            if (match := _LEVEL_LEAF.fullmatch(item)) is not None
            and match["circuit"] == circuit
        )
        allowed = (
            (DHW_LEVELS & listed) | (used & listed)
            if is_dhw_switch_program_path(path)
            else HEATING_LEVELS | (used & listed)
        )
    ordered = [level for level in assessment.levels if level in allowed]
    ordered += sorted(allowed - set(ordered))
    return tuple(ordered)


def ventilation_levels(resource: Resource | None) -> tuple[str, ...]:
    """The known ventilation levels a unit lists as references (in app order).

    An unknown ending is never offered; an empty list offers nothing.
    """
    if resource is None or not resource.references:
        return ()
    endings = {
        reference.path.rsplit("/", 1)[-1]
        for reference in resource.references
        if reference.path.startswith(f"{resource.path}/")
    }
    return tuple(level for level in VENTILATION_LEVELS if level in endings)


def _readable_number(resource: Resource | None) -> bool:
    if resource is None or not resource.has_value:
        return False
    value = resource.value
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and resource.metadata.available is not False
    )


def wddw2_range(resources: Mapping[str, Resource]) -> tuple[float, float] | None:
    """The whole-degree range of the WDDW2 program from ``manualSetpoint``."""
    for path in _WDDW2_RANGE:
        resource = resources.get(path)
        if resource is None:
            continue
        low, high = resource.metadata.minimum, resource.metadata.maximum
        if (
            low is not None
            and high is not None
            and math.isfinite(low)
            and math.isfinite(high)
            and low < high
        ):
            return float(low), float(high)
    return None


def edit_issue(
    resource: Resource | None,
    resources: Mapping[str, Resource],
    *,
    program_mode: Resource | None = None,
) -> str | None:
    """Why a program cannot be changed here, as a stable token, or ``None``.

    ``program_mode`` is the current reading of the heating circuit's
    ``switchProgramMode``. A level program of a circuit that reports one
    (its path is among ``resources``) needs it; an absolute program always
    needs it. Without a current reading the program is not offered
    (``program_mode_unknown``).
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
    if metadata.used is False:
        # Consistent with the write, which refuses such a program.
        return "not_used"
    mode_path = program_mode_path(resource.path)
    if mode_path is not None and (
        assessment.value_type == "absolute"
        or program_mode is not None
        or mode_path in resources
    ):
        issue = program_mode_issue(resource.path, program_mode, assessment.value_type)
        if issue is not None:
            return issue
    if not assessment.limits_reported:
        return "program_limits_missing"
    if "program_over_limit" in assessment.notes:
        return "program_over_limit"
    if assessment.value_type == "level":
        levels = allowed_levels(resource.path, assessment, resources)
        if not levels:
            return "levels_unknown"
        if any(level not in levels for level in assessment.levels):
            return "level_not_listed"
        return None
    rules = _numeric_rules(resource.path, assessment, resources)
    if isinstance(rules, str):
        return rules
    if any(value_issue(point.level, rules) for point in assessment.points):
        return "point_out_of_range"
    return None


def _numeric_rules(
    path: str,
    assessment: SwitchProgramAssessment,
    resources: Mapping[str, Resource],
) -> NumericRules | str:
    """Range, step, off value and number form of a numeric program, or a token."""
    if assessment.value_type == "degrees":
        bounds = wddw2_range(resources)
        if bounds is None:
            return "program_range_missing"
        if assessment.numbers_as_text is None:
            # No number read yet: the form of a number is not known.
            return "number_format_unknown"
        return NumericRules(
            bounds[0], bounds[1], 1.0, "off", assessment.numbers_as_text, True
        )
    if assessment.minimum is None or assessment.maximum is None:
        return "program_range_missing"
    if assessment.step is None:
        return "program_step_missing"
    if assessment.numbers_as_text is None:
        return "number_format_unknown"
    # "0.0" switches off only where the reading already uses it.
    off = ABSOLUTE_OFF if ABSOLUTE_OFF in assessment.levels else None
    return NumericRules(
        assessment.minimum,
        assessment.maximum,
        assessment.step,
        off,
        assessment.numbers_as_text,
        False,
    )


def numeric_rules(program: EditableProgram) -> NumericRules | None:
    """The number rules of an editable numeric program, ``None`` for levels."""
    if not program.numeric:
        return None
    assert program.minimum is not None and program.maximum is not None
    assert program.step is not None
    return NumericRules(
        program.minimum,
        program.maximum,
        program.step,
        program.off_value,
        bool(program.numbers_as_text),
        program.value_type == "degrees",
    )


def value_issue(level: object, program: EditableProgram | NumericRules) -> str | None:
    """Why one setpoint text is not allowed, as a token, or ``None``."""
    if isinstance(program, EditableProgram):
        rules = numeric_rules(program)
        if rules is None:
            return (
                None
                if isinstance(level, str) and level in program.levels
                else "level_not_allowed"
            )
    else:
        rules = program
    if not isinstance(level, str):
        return "value_not_allowed"
    if rules.off_value is not None and level == rules.off_value:
        return None
    if level in (ABSOLUTE_OFF, "off"):
        return "off_not_offered"
    number = _number(level, degrees=rules.degrees)
    if number is None:
        return "value_not_allowed"
    if not rules.minimum <= number <= rules.maximum:
        return "value_out_of_range"
    steps = (number - rules.minimum) / rules.step
    if abs(steps - round(steps)) > 1e-6:
        return "value_off_step"
    return None


def _number(level: str, *, degrees: bool) -> float | None:
    if degrees:
        return float(level) if re.fullmatch(r"-?[0-9]{1,3}", level) else None
    if not re.fullmatch(r"-?[0-9]{1,3}\.[0-9]{1,2}", level):
        return None
    number = float(level)
    return number if canonical_number(number) == level else None


def setpoint_text(program: EditableProgram, value: object) -> str | None:
    """The setpoint text for a value entered for this program, or ``None``.

    A level stays a level; a number becomes ``"21.5"`` (absolute) or
    ``"45"`` (whole degrees), never rounded: an entry off the grid is
    refused by ``value_issue``.
    """
    if not program.numeric:
        return value if isinstance(value, str) else None
    if isinstance(value, str):
        if value in (ABSOLUTE_OFF, "off"):
            return value
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value):
        return None
    if program.value_type == "degrees":
        return str(int(value)) if float(value).is_integer() else f"{value}"
    text = canonical_number(float(value))
    # An entry with more decimals than the canonical text holds is kept as
    # it is, so the step check refuses it instead of rounding it.
    return text if math.isclose(float(text), float(value), abs_tol=1e-9) else repr(value)


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
    kind = program_kind(resource.path)
    assert kind is not None and assessment.value_type is not None
    minimum = maximum = step = None
    off: str | None = None
    as_text: bool | None = True
    if assessment.value_type != "level":
        rules = _numeric_rules(resource.path, assessment, resources)
        assert isinstance(rules, NumericRules)
        minimum, maximum, step = rules.minimum, rules.maximum, rules.step
        off, as_text = rules.off_value, rules.numbers_as_text
    return EditableProgram(
        resource.path,
        sorted_points(assessment.points),
        assessment.max_switch_points,
        assessment.max_switch_points_per_day,
        assessment.time_raster,
        allowed_levels(resource.path, assessment, resources),
        kind=kind.key,
        value_type=assessment.value_type,
        minimum=minimum,
        maximum=maximum,
        step=step,
        off_value=off,
        numbers_as_text=as_text,
        body=kind.body,
        carry_over=kind.carry_over,
    )


def week_issue(
    points: Sequence[SwitchPoint],
    program: EditableProgram,
) -> str | None:
    """The first rule a new week breaks, as a stable token, or ``None``.

    Times are whole minutes from 0 to 1439 on the reported raster, at most
    one point per time and day, no more points than reported per day and in
    all, only the program's levels (or numbers in its range and on its
    step), and at least one point in the week (an empty week would mean one
    level all week and is never sent).
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
        issue = value_issue(point.level, program)
        if issue is not None:
            return "level_not_allowed" if not program.numeric else issue
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


def encode_week(
    points: Iterable[SwitchPoint], program: EditableProgram | None = None
) -> list[JsonValue]:
    """The wire points: the whole week sorted by day and time, with the
    fields in the order ``dayOfWeek``, ``setpoint``, ``time``.

    A number keeps the form the program was read in: text stays text, a
    JSON number stays a number.
    """
    return [
        {
            "dayOfWeek": DAYS[point.day],
            "setpoint": _wire_setpoint(point.level, program),
            "time": point.minute,
        }
        for point in sorted_points(points)
    ]


def _wire_setpoint(level: str, program: EditableProgram | None) -> JsonValue:
    if program is None or not program.numeric or program.numbers_as_text:
        return level
    if level == "off":
        return level
    if program.value_type == "degrees":
        return int(level)
    return float(level)


def encode_body(
    points: Iterable[SwitchPoint], program: EditableProgram
) -> list[JsonValue] | dict[str, JsonValue]:
    """The PUT body: a raw array, or ``{"switchPoints": [...]}`` for WDDW2."""
    week = encode_week(points, program)
    if program.body == "object":
        return {"switchPoints": week}
    return week


def fingerprint(resource: Resource) -> tuple[object, ...] | None:
    """What must not change between showing a program and writing it.

    The points (in any order), the limits, the level reference, the range,
    the number form, the write permission and the flags; ``None`` for a
    program that is not supported.
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
        assessment.value_type,
        assessment.minimum,
        assessment.maximum,
        assessment.step,
        assessment.numbers_as_text,
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
