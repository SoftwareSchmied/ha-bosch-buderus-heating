"""Read-only PointT weekly time programs (switch programs).

Every program kind the integration reads is listed once in ``KINDS``: its
path, the kind of its setpoints, the shape of the body it is written with
and whether the last point of a day carries over into the next one. A
program is read tolerantly but changed only in its clean form: a point that
cannot be read at all hides the program, while further fields or keys,
another number or time form, repeated points or limits above the ones a
change keeps to leave it shown and not changeable (``read_only``). Nothing
about a program is guessed (no program IDs, no levels).

The heating circuit programs ``A``/``B`` and the hot water programs keep the
form reviewed since 0.9.0: the limits must be reported. A heating program
whose points all carry numbers is an absolute program; without a reported
range or step it takes 5 to 30 °C in steps of 0.5
(``HEATING_RANGE_DEFAULT``), and the read-back of a change confirms what the
circuit keeps. The further kinds (cooling, zones, ventilation and the WDDW2
hot water program) have no confirmed reading yet: a program whose points can be
read is shown, also without limits; whether it may be changed is decided by
``switch_program_edit`` from the reported metadata alone. The programs of an
air conditioner on a PointT gateway carry a mode, a temperature and a fan
speed per point (``aircon_program_rules``); their setting is kept as one
text and a day starts switched off until its first point.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Final, Literal

from .aircon_program_rules import bounded_int, parse_point
from .pointt.models import JsonValue, Resource

DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
MINUTES_PER_DAY = 24 * 60
MAX_SWITCH_POINTS = 168
MAX_SWITCH_POINTS_PER_DAY = 48
_LEVEL_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# A reported limit above this is not believed (a count of switch points).
_LIMIT_SANITY = 10_000
# A number as text, as the app writes it (``String.valueOf(float)``).
_DECIMAL_TEXT = re.compile(r"^-?[0-9]{1,3}\.[0-9]{1,2}$")
_INTEGER_TEXT = re.compile(r"^-?[0-9]{1,3}$")
_POINT_FIELDS = frozenset({"dayOfWeek", "time", "setpoint"})
# Range fields an absolute program may report with its limits.
RANGE_FIELDS: Final = ("switchPointMin", "switchPointMax", "switchPointResolution")
# A heating circuit's absolute program without a reported range or step:
# 5 to 30 °C in steps of 0.5. The read-back of a change confirms it.
HEATING_RANGE_DEFAULT: Final = (5.0, 30.0, 0.5)

ValueType = Literal["level", "absolute", "degrees", "aircon"]


@dataclass(frozen=True, slots=True)
class ProgramKind:
    """One program kind: where it lives and how it is read and written."""

    key: str
    pattern: re.Pattern[str]
    # "level": level keys; "level_or_absolute": level keys or, in all points,
    # numbers; "absolute": numbers; "degrees": ``off`` or whole degrees;
    # "aircon": mode, temperature and fan speed of an air conditioner.
    values: Literal["level", "level_or_absolute", "absolute", "degrees", "aircon"]
    # The PUT body: a raw array of points, or ``{"switchPoints": [...]}``.
    body: Literal["array", "object"]
    # The form reviewed since 0.9.0: limits and level reference required.
    legacy: bool
    # The last point of the previous days stays in effect (weekly repeat);
    # without it a day starts without a setpoint until its first point.
    carry_over: bool = True


def _kind(
    key: str, pattern: str, values: str, body: str, legacy: bool, carry: bool = True
) -> ProgramKind:
    return ProgramKind(key, re.compile(pattern), values, body, legacy, carry)  # type: ignore[arg-type]


KINDS: Final[tuple[ProgramKind, ...]] = (
    _kind(
        "heating",
        r"^(?P<circuit>/heatingCircuits/hc[1-9][0-9]?)/switchPrograms/(?P<program>[AB])$",
        "level_or_absolute",
        "array",
        True,
    ),
    _kind(
        "dhw",
        r"^(?P<circuit>/dhwCircuits/dhw[1-9][0-9]?)/switchPrograms/(?P<program>[AB])$",
        "level",
        "array",
        True,
    ),
    # The app writes the cooling program as ``A`` only.
    _kind(
        "cooling",
        r"^(?P<circuit>/heatingCircuits/hc[1-9][0-9]?)/cooling/switchPrograms/(?P<program>A)$",
        "level",
        "array",
        False,
    ),
    _kind(
        "zone",
        r"^(?P<circuit>/zones/zone[1-9][0-9]?)/switchPrograms/(?P<program>heating|cooling)$",
        "absolute",
        "array",
        False,
    ),
    _kind(
        "ventilation",
        r"^(?P<circuit>/ventilation/zone1)/switchPrograms/(?P<program>cp)$",
        "level",
        "array",
        False,
    ),
    # The only program written as an object; the only one without carry-over.
    _kind(
        "wddw2",
        r"^(?P<circuit>/dhwCircuits/dhw1)/switchPrograms/(?P<program>program1)$",
        "degrees",
        "object",
        False,
        False,
    ),
    # An air conditioner's programs (``program1`` ... ``program9``, the
    # number is the last character of the ID in the app): an object body,
    # no carry-over (the unit is off before the first point of a day).
    _kind(
        "aircon",
        r"^(?P<circuit>/airConditioning)/switchPrograms/(?P<program>program[1-9])$",
        "aircon",
        "object",
        False,
        False,
    ),
)


def program_kind(path: str) -> ProgramKind | None:
    """The kind of an exact program path, never a guessed one."""
    return next((kind for kind in KINDS if kind.pattern.fullmatch(path)), None)


def is_switch_program_path(path: str) -> bool:
    """Recognize exact program leaves without inventing circuit or program IDs."""
    return program_kind(path) is not None


def is_dhw_switch_program_path(path: str) -> bool:
    """Return whether a program is a level program of a hot-water circuit."""
    kind = program_kind(path)
    return kind is not None and kind.key == "dhw"


def program_circuit(path: str) -> str | None:
    """The circuit, zone or unit a program belongs to, e.g. ``/zones/zone1``."""
    kind = program_kind(path)
    match = kind.pattern.fullmatch(path) if kind is not None else None
    return None if match is None else match["circuit"]


@dataclass(frozen=True, slots=True)
class SwitchPoint:
    """One recurring local wall-clock switch point.

    ``level`` is the setpoint as text: a level key, or a number in its
    canonical form (``"21.5"``, whole degrees ``"45"``).
    """

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
    kind: str | None = None
    value_type: ValueType | None = None
    # Numbers: read as text (``"21.5"``) or as JSON numbers; ``None`` when
    # the program holds no number.
    numbers_as_text: bool | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    carry_over: bool = True
    # Diagnostic tokens about the form, never values.
    notes: tuple[str, ...] = ()
    # A program shown in a form that is not written back: why it cannot be
    # changed here (a token), ``None`` for a clean form.
    read_only: str | None = None

    @property
    def levels(self) -> tuple[str, ...]:
        """Return the distinct levels in first-use order."""
        return tuple(dict.fromkeys(point.level for point in self.points))

    @property
    def limits_reported(self) -> bool:
        return None not in (
            self.max_switch_points,
            self.max_switch_points_per_day,
            self.time_raster,
        )

    def active_level(self, day: int, minute: int) -> str | None:
        """Return the level in effect at a weekday (Mo=0) and minute of day.

        The program repeats weekly, so the last point of the previous days
        stays in effect until the first point of the requested day; a kind
        without carry-over has no level before the day's first point.
        """
        if not self.carry_over:
            earlier = [
                point
                for point in self.points
                if point.day == day and point.minute <= minute
            ]
            return max(earlier, key=_minute).level if earlier else None
        point = self._last_point_at_or_before(day * MINUTES_PER_DAY + minute)
        return None if point is None else point.level

    def next_switch_in_minutes(self, day: int, minute: int) -> int | None:
        """Return wall-clock minutes, not elapsed time across clock changes."""
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

    def structured(self) -> dict[str, list[dict[str, str]]]:
        """The points per lower-case weekday as ``{"time", "level"}``."""
        days: dict[str, list[dict[str, str]]] = {}
        for point in sorted(self.points, key=lambda item: (item.day, item.minute)):
            hours, minutes = divmod(point.minute, 60)
            days.setdefault(DAYS[point.day].lower(), []).append(
                {"time": f"{hours:02d}:{minutes:02d}", "level": point.level}
            )
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


def _minute(point: SwitchPoint) -> int:
    return point.minute


def assess_switch_program(resource: Resource) -> SwitchProgramAssessment:
    """Validate one program shape strictly and read only."""
    kind = program_kind(resource.path)
    if kind is None:
        return SwitchProgramAssessment(False, "not_switch_program")
    if kind.values == "aircon":
        return _assess_aircon(resource, kind)
    if (
        resource.metadata.resource_type != "switchProgram"
        or resource.references
        or resource.has_value
        or resource.has_values
    ):
        return SwitchProgramAssessment(False, "unsupported_schema")
    assessment = (
        _assess_legacy(resource, kind)
        if kind.legacy
        else _assess_further(resource, kind)
    )
    if (
        assessment.supported
        and assessment.read_only is None
        and resource.unknown_fields
    ):
        # Fields not known here would be lost by a change: shown only.
        return replace(
            assessment,
            read_only="unknown_fields",
            notes=(*assessment.notes, "unknown_fields"),
        )
    return assessment


def _assess_legacy(resource: Resource, kind: ProgramKind) -> SwitchProgramAssessment:
    """Heating ``A``/``B`` and hot water: the form reviewed since 0.9.0.

    Read tolerantly, changed only in the clean form: another level reference,
    a range on a level program, points with further keys or another number
    form, duplicate points or more points than are changed here leave the
    program shown with ``read_only`` set. An empty program is shown empty.
    """
    fields = resource.program_fields
    match = kind.pattern.fullmatch(resource.path)
    assert match is not None
    reference = fields.get("setpointProperty")
    expected_levels = f"{match['circuit']}/temperatureLevels"
    reference_ok = (
        isinstance(reference, dict) and reference.get("id") == expected_levels
    )
    device_points = _reported_count(fields.get("maxNbOfSwitchPoints"))
    device_per_day = _reported_count(fields.get("maxNbOfSwitchPointsPerDay"))
    raster = _bounded_int(fields.get("switchPointTimeRaster"), 60)
    raw_points = fields.get("switchPoints")
    absolute = (
        kind.values == "level_or_absolute"
        and isinstance(raw_points, list)
        and bool(raw_points)
        and all(_numeric_setpoint(item) for item in raw_points)
    )
    notes: list[str] = []
    read_only: list[str] = []
    if absolute:
        # TP-13: the level reference of an absolute program is not known; it
        # may name the levels or be missing. Only reported, never required.
        if "setpointProperty" not in fields:
            notes.append("setpoint_property_missing")
        elif reference_ok:
            notes.append("setpoint_property_levels")
        else:
            notes.append("setpoint_property_other")
    elif not reference_ok:
        read_only.append("unsupported_setpoint_property")
    elif any(key in fields for key in RANGE_FIELDS):
        # A level program carries no range; such a form is only shown.
        read_only.append("unsupported_schema")
    if (
        device_points is None
        or device_per_day is None
        or raster is None
        or MINUTES_PER_DAY % raster
    ):
        return SwitchProgramAssessment(False, "invalid_limits")
    if not isinstance(raw_points, list):
        return SwitchProgramAssessment(False, "missing_switch_points")
    if len(raw_points) > device_points:
        return SwitchProgramAssessment(False, "too_many_switch_points")
    value_type: ValueType = "absolute" if absolute else "level"
    parsed = _parse_points(raw_points, raster, value_type, legacy=True)
    if isinstance(parsed, str):
        return SwitchProgramAssessment(False, parsed)
    points, as_text, irregular = parsed
    read_only.extend(irregular)
    if any(count > device_per_day for count in _per_day(points)):
        return SwitchProgramAssessment(False, "too_many_switch_points")
    max_points = min(device_points, MAX_SWITCH_POINTS)
    max_per_day = min(device_per_day, MAX_SWITCH_POINTS_PER_DAY)
    if (max_points, max_per_day) != (device_points, device_per_day):
        # Larger limits of the device: changes keep to these.
        notes.append("program_limits_capped")
    if len(points) > max_points or any(
        count > max_per_day for count in _per_day(points)
    ):
        read_only.append("program_over_limit")
    if not points:
        notes.append("program_empty")
    minimum, maximum, step, range_notes = (
        _range(fields, HEATING_RANGE_DEFAULT if kind.key == "heating" else None)
        if absolute
        else (None, None, None, ())
    )
    return SwitchProgramAssessment(
        True,
        points=points,
        max_switch_points=max_points,
        max_switch_points_per_day=max_per_day,
        time_raster=raster,
        kind=kind.key,
        value_type=value_type,
        numbers_as_text=as_text,
        minimum=minimum,
        maximum=maximum,
        step=step,
        carry_over=kind.carry_over,
        notes=tuple(dict.fromkeys((*notes, *read_only, *range_notes))),
        read_only=read_only[0] if read_only else None,
    )


def _assess_further(resource: Resource, kind: ProgramKind) -> SwitchProgramAssessment:
    """Cooling, zones, ventilation and WDDW2: readable without limits."""
    fields = resource.program_fields
    raw_points = fields.get("switchPoints")
    if not isinstance(raw_points, list):
        return SwitchProgramAssessment(False, "missing_switch_points")
    if len(raw_points) > MAX_SWITCH_POINTS * 3:
        return SwitchProgramAssessment(False, "too_many_switch_points")
    notes: list[str] = []
    max_points = _bounded_int(fields.get("maxNbOfSwitchPoints"), MAX_SWITCH_POINTS)
    max_per_day = _bounded_int(
        fields.get("maxNbOfSwitchPointsPerDay"), MAX_SWITCH_POINTS_PER_DAY
    )
    raster = _bounded_int(fields.get("switchPointTimeRaster"), 60)
    if raster is not None and MINUTES_PER_DAY % raster:
        raster = None
    if None in (max_points, max_per_day, raster):
        notes.append("program_limits_missing")
    if "setpointProperty" in fields:
        notes.append("setpoint_property_reported")
    value_type: ValueType = (
        "absolute"
        if kind.values == "absolute"
        else "degrees"
        if kind.values == "degrees"
        else "level"
    )
    parsed = _parse_points(raw_points, raster or 1, value_type, legacy=False)
    if isinstance(parsed, str):
        return SwitchProgramAssessment(False, parsed)
    points, as_text, irregular = parsed
    notes.extend(irregular)
    if not points:
        notes.append("program_empty")
    if max_points is not None and len(points) > max_points:
        notes.append("program_over_limit")
    if max_per_day is not None and any(
        count > max_per_day for count in _per_day(points)
    ):
        notes.append("program_over_limit")
    minimum, maximum, step, range_notes = (
        _range(fields) if value_type == "absolute" else (None, None, None, ())
    )
    return SwitchProgramAssessment(
        True,
        points=points,
        max_switch_points=max_points,
        max_switch_points_per_day=max_per_day,
        time_raster=raster,
        kind=kind.key,
        value_type=value_type,
        numbers_as_text=as_text,
        minimum=minimum,
        maximum=maximum,
        step=step,
        carry_over=kind.carry_over,
        notes=tuple(dict.fromkeys((*notes, *range_notes))),
        read_only=irregular[0] if irregular else None,
    )


def _assess_aircon(resource: Resource, kind: ProgramKind) -> SwitchProgramAssessment:
    """An air conditioner's program: readable without limits or configuration.

    The ``type`` is not checked (the app does not either); every point must
    have the known form, otherwise the program is not shown at all.
    """
    if (
        resource.references
        or resource.has_value
        or resource.has_values
        or resource.unknown_fields
    ):
        return SwitchProgramAssessment(False, "unsupported_schema")
    fields = resource.program_fields
    raw_points = fields.get("switchPoints")
    if not isinstance(raw_points, list):
        return SwitchProgramAssessment(False, "missing_switch_points")
    if len(raw_points) > MAX_SWITCH_POINTS * 3:
        return SwitchProgramAssessment(False, "too_many_switch_points")
    notes: list[str] = []
    if resource.metadata.resource_type != "switchProgram":
        notes.append("program_type_other")
    max_points = bounded_int(fields.get("maxNbOfSwitchPoints"), 7 * 48)
    max_per_day = bounded_int(
        fields.get("maxNbOfSwitchPointsPerDay"), MAX_SWITCH_POINTS_PER_DAY
    )
    raster = bounded_int(fields.get("switchPointTimeRaster"), 60)
    if raster is not None and MINUTES_PER_DAY % raster:
        raster = None
    if None in (max_points, max_per_day, raster):
        # The configuration may report them (``switch_program_edit``).
        notes.append("program_limits_missing")
    points: list[SwitchPoint] = []
    seen: set[tuple[int, int]] = set()
    for item in raw_points:
        parsed = parse_point(item)
        if parsed is None:
            return SwitchProgramAssessment(False, "invalid_switch_point")
        day, minute, setting = parsed
        if day not in DAYS or not 0 <= minute < MINUTES_PER_DAY:
            return SwitchProgramAssessment(False, "invalid_switch_point")
        if raster is not None and minute % raster:
            return SwitchProgramAssessment(False, "invalid_switch_point")
        key = (DAYS.index(day), minute)
        if key in seen:
            return SwitchProgramAssessment(False, "duplicate_switch_point")
        seen.add(key)
        points.append(SwitchPoint(key[0], minute, setting.text))
    if not points:
        notes.append("program_empty")
    return SwitchProgramAssessment(
        True,
        points=tuple(points),
        max_switch_points=max_points,
        max_switch_points_per_day=max_per_day,
        time_raster=raster,
        kind=kind.key,
        value_type="aircon",
        numbers_as_text=False,
        carry_over=kind.carry_over,
        notes=tuple(notes),
    )


def _parse_points(
    raw_points: list[JsonValue], raster: int, value_type: ValueType, *, legacy: bool
) -> tuple[tuple[SwitchPoint, ...], bool | None, tuple[str, ...]] | str:
    """The points, whether numbers were text and the forms that are only
    shown (tokens); a reason token when a point cannot be read at all.

    Read tolerantly: further keys in a point, a time such as ``360.0``, a
    number in another form (``"21"``, ``"21.50"``, a JSON number) and a
    repeated point are read; such a program is only shown. A repeated point
    with the same setpoint counts once; with another one the first is kept.
    """
    points: list[SwitchPoint] = []
    seen: dict[tuple[int, int], str] = {}
    text_forms: set[bool] = set()
    irregular: list[str] = []
    for item in raw_points:
        if not isinstance(item, dict) or not set(item) >= _POINT_FIELDS:
            return "invalid_switch_point"
        if set(item) != _POINT_FIELDS:
            irregular.append("point_extra_keys")
        day, minute, setpoint = item["dayOfWeek"], item["time"], item["setpoint"]
        if isinstance(minute, float) and not isinstance(minute, bool):
            if not math.isfinite(minute) or not minute.is_integer():
                return "invalid_switch_point"
            irregular.append("time_as_number")
            minute = int(minute)
        if (
            not isinstance(day, str)
            or day not in DAYS
            or type(minute) is not int
            or not 0 <= minute < MINUTES_PER_DAY
            or minute % raster
        ):
            return "invalid_switch_point"
        level = _setpoint(setpoint, value_type, legacy=legacy)
        if level is None and value_type == "absolute":
            level = _tolerant_number(setpoint)
            if level is not None:
                irregular.append("setpoint_form_other")
        if level is None:
            return (
                "mixed_setpoints"
                if not legacy and _known_other(setpoint)
                else "invalid_switch_point"
            )
        if value_type != "level" and level != "off":
            # The form numbers are read in (text or JSON number).
            text_forms.add(isinstance(setpoint, str))
        key = (DAYS.index(day), minute)
        if key in seen:
            if seen[key] != level:
                irregular.append("duplicate_switch_point")
            else:
                irregular.append("duplicate_switch_point_same")
            continue
        seen[key] = level
        points.append(SwitchPoint(key[0], minute, level))
    if len(text_forms) > 1:
        # Numbers as text and as JSON numbers in one program: only shown.
        irregular.append("mixed_setpoints")
    as_text = next(iter(text_forms)) if len(text_forms) == 1 else None
    return tuple(points), as_text, tuple(dict.fromkeys(irregular))


def _setpoint(value: object, value_type: ValueType, *, legacy: bool) -> str | None:
    """The canonical text of one setpoint of this type, or ``None``."""
    if value_type == "level":
        return value if isinstance(value, str) and _LEVEL_KEY.fullmatch(value) else None
    if value_type == "degrees":
        if value == "off":
            return "off"
        if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value):
            number: object = int(value)
        elif type(value) is int:
            number = value
        else:
            return None
        assert isinstance(number, int)
        text = str(number)
        if isinstance(value, str) and value != text:
            return None  # e.g. "045": not written back in another form
        return text
    # absolute
    if isinstance(value, str):
        if not _DECIMAL_TEXT.fullmatch(value):
            return None
        canonical = canonical_number(float(value))
        # Only the written form (``21.5``, ``20.0``); "21.50" would not be
        # written back as read.
        return canonical if canonical == value else None
    if legacy:
        # A heating circuit's absolute program carries text; a JSON number
        # there is only read (``_tolerant_number``).
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value):
        return None
    return canonical_number(float(value))


def _tolerant_number(value: object) -> str | None:
    """A number setpoint in another form (``"21"``, ``"21.50"``, ``21.5``),
    as its canonical text; only for reading."""
    if isinstance(value, str) and (
        _DECIMAL_TEXT.fullmatch(value) or _INTEGER_TEXT.fullmatch(value)
    ):
        return canonical_number(float(value))
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return canonical_number(float(value)) if math.isfinite(value) else None


def _numeric_setpoint(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    setpoint = item.get("setpoint")
    return (
        _setpoint(setpoint, "absolute", legacy=True) is not None
        or _tolerant_number(setpoint) is not None
    )


def _known_other(value: object) -> bool:
    """A setpoint of another known type (a level key or a number)."""
    return (isinstance(value, str) and _LEVEL_KEY.fullmatch(value) is not None) or (
        _setpoint(value, "absolute", legacy=False) is not None
    )


def canonical_number(value: float) -> str:
    """The shortest decimal text with at least one decimal, e.g. ``21.5``."""
    text = repr(round(value, 2))
    return text if "." in text else f"{text}.0"


def _range(
    fields: dict[str, JsonValue],
    default: tuple[float, float, float] | None = None,
) -> tuple[float | None, float | None, float | None, tuple[str, ...]]:
    """Range and step of an absolute program; ``default`` (minimum,
    maximum, step) stands in for each one that is not reported as a number."""
    values: list[float | None] = []
    for key in RANGE_FIELDS:
        raw = fields.get(key)
        values.append(
            float(raw)
            if not isinstance(raw, bool)
            and isinstance(raw, int | float)
            and math.isfinite(raw)
            else None
        )
    minimum, maximum, step = values
    notes: list[str] = []
    if default is not None and (minimum is None or maximum is None):
        notes.append("program_range_default")
        minimum = default[0] if minimum is None else minimum
        maximum = default[1] if maximum is None else maximum
    if minimum is None or maximum is None or minimum >= maximum:
        notes.append("program_range_missing")
        minimum = maximum = None
    if default is not None and (step is None or step <= 0):
        notes.append("program_step_default")
        step = default[2]
    if step is None or step <= 0:
        notes.append("program_step_missing")
        step = None
    return minimum, maximum, step, tuple(notes)


def _per_day(points: tuple[SwitchPoint, ...]) -> list[int]:
    counts = [0] * len(DAYS)
    for point in points:
        counts[point.day] += 1
    return counts


def _bounded_int(value: object, maximum: int) -> int | None:
    """A positive whole number up to ``maximum``; ``15.0`` counts as 15."""
    return bounded_int(value, maximum)


def _reported_count(value: object) -> int | None:
    """A reported number of switch points: a positive whole number (``48.0``
    counts as 48), also above the limits changes keep to."""
    return bounded_int(value, _LIMIT_SANITY)
