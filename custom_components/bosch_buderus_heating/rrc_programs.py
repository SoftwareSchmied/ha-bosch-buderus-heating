"""Lossless RRC schedules and single-write transactions for existing programs.

ZONE-13: a program selection and a switch point change go out exactly once
and are read back on the RRC schedule (1, 2, 4 and 8 seconds,
``rrc_write.RrcStructWrite``). A send that may have taken effect and is not
confirmed by then raises ``RrcWriteNotConfirmed``; the caller counts the
sends (``on_sent``) and a later regular reading may still confirm it
(``selection_shows`` and ``edit_shows``).

A switch point is ``{d, t, Th}``, optionally with an integer ``name``;
a day program may leave out ``d`` (its weekday comes from the path). Any other
key, or a ``name`` of another type, keeps the program read-only. A change is
sent as ``{d, t, Th}`` for every point of the day, and the preflight and the
read-back compare only ``(d, t, Th)``.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from . import rrc
from .pointt import (
    PointTClient,
    Resource,
    WriteValidationError,
)
from .pointt.models import JsonValue
from .rrc_write import RrcStructWrite
from .start_counters import value_shape
from .write_policies import RRC_READ_BACK_DELAYS

DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
DAY_PATHS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
WEEK = re.compile(rf"^/programs/pg({rrc.RRC_ID})/week$")
DAY = re.compile(rf"^/programs/pg({rrc.RRC_ID})/({'|'.join(DAY_PATHS)})$")
# The keys of a switch point. ``name`` is optional and must be an
# integer; it is not part of a change and not compared.
POINT_KEYS = frozenset({"d", "t", "Th"})
OPTIONAL_POINT_KEYS: dict[str, type] = {"name": int}


@dataclass(frozen=True, slots=True)
class SwitchPoint:
    day: str
    minute: int
    temperature: float


@dataclass(frozen=True, slots=True)
class Schedule:
    """Retain the complete value, including unrecognized fields and order."""

    path: str
    points: tuple[SwitchPoint, ...]
    raw_value: str
    editable: bool

    def encode(self) -> list[JsonValue]:
        """Round-trip the original array without silently dropping fields."""
        value: list[JsonValue] = json.loads(self.raw_value)
        return value

    def day_points(self, day: str) -> tuple[SwitchPoint, ...]:
        """The ``(d, t, Th)`` projection of one day, in reading order."""
        return tuple(point for point in self.points if point.day == day)


def _point_key_issue(item: Mapping[str, object]) -> str | None:
    """``extra_key`` for a key outside the allowed set, ``optional_key_type``
    for an allowed optional key of another type; ``None`` otherwise."""
    issue: str | None = None
    for key, value in item.items():
        if key in POINT_KEYS:
            continue
        kind = OPTIONAL_POINT_KEYS.get(key)
        if kind is None:
            return "extra_key"
        if isinstance(value, bool) or not isinstance(value, kind):
            issue = "optional_key_type"
    return issue


def parse_schedule(resource: Resource) -> Schedule | None:
    day_match = DAY.fullmatch(resource.path)
    # #113 (RRC-07): a single day is typed ``dayProgram`` with the same
    # switch points as the week; a hardware confirmation is pending. A week
    # stays ``weekProgram`` only.
    types = {"weekProgram", "dayProgram"} if day_match else {"weekProgram"}
    if (
        not (WEEK.fullmatch(resource.path) or day_match)
        or resource.metadata.resource_type not in types
        or not rrc.available(resource)
        or resource.references
        or resource.has_values
        or not resource.has_value
        or not isinstance(resource.value, list)
        or len(resource.value) > 512
    ):
        return None
    points: list[SwitchPoint] = []
    editable = resource.metadata.writable and not resource.unknown_fields
    last: dict[str, int] = {}
    path_day = DAYS[DAY_PATHS.index(day_match[2])] if day_match else None
    for item in resource.value:
        if not isinstance(item, dict):
            return None
        minute, temperature = item.get("t"), item.get("Th")
        # A day program may leave out the weekday; the path names it.
        day = item.get("d") if "d" in item or path_day is None else path_day
        if (
            not isinstance(day, str)
            or day not in DAYS
            or type(minute) is not int
            or not 0 <= minute < 1440
            or isinstance(temperature, bool)
            or not isinstance(temperature, int | float)
            or not math.isfinite(temperature)
            or not 0 <= temperature <= 30
            or (path_day is not None and day != path_day)
        ):
            return None
        if (
            _point_key_issue(item) is not None
            or minute <= last.get(day, -1)
            or minute % 5
            or not 5 <= temperature <= 30
            or not float(temperature * 2).is_integer()
        ):
            editable = False
        last[day] = minute
        points.append(SwitchPoint(day, minute, float(temperature)))
    return Schedule(
        resource.path,
        tuple(points),
        json.dumps(resource.value, ensure_ascii=True, allow_nan=False, sort_keys=True),
        editable,
    )


# R4 (T4): the reasons a program is not read or not editable, as stable
# tokens. ``parse_schedule`` decides; these only name why (diagnostics).
SHAPE_ISSUES = (
    "not_a_program",
    "type_unexpected",
    "not_available",
    "has_references",
    "has_values",
    "no_value",
    "not_a_list",
    "too_many_points",
    "point_not_object",
    "day_missing_d",
    "day_invalid",
    "day_mismatch",
    "minute_invalid",
    "temperature_invalid",
)
EDIT_ISSUES = (
    "not_writable",
    "unknown_fields",
    "extra_key",
    "optional_key_type",
    "unsorted",
    "off_grid_minute",
    "temperature_range",
    "temperature_grid",
)


def schedule_issues(resource: Resource) -> tuple[str, ...]:
    """Why ``parse_schedule`` rejects a program or marks it read-only.

    Empty for an editable program. A rejected shape names its first
    rejecting reason (the parser stops there too) after the read-only
    reasons found up to that point; an accepted one names every reason.
    Tokens only, never values (R4, T4).
    """
    day_match = DAY.fullmatch(resource.path)
    if not (WEEK.fullmatch(resource.path) or day_match):
        return ("not_a_program",)
    types = {"weekProgram", "dayProgram"} if day_match else {"weekProgram"}
    for failed, token in (
        (resource.metadata.resource_type not in types, "type_unexpected"),
        (not rrc.available(resource), "not_available"),
        (bool(resource.references), "has_references"),
        (resource.has_values, "has_values"),
        (not resource.has_value, "no_value"),
        (not isinstance(resource.value, list), "not_a_list"),
    ):
        if failed:
            return (token,)
    assert isinstance(resource.value, list)
    if len(resource.value) > 512:
        return ("too_many_points",)
    found: set[str] = set()
    if not resource.metadata.writable:
        found.add("not_writable")
    if resource.unknown_fields:
        found.add("unknown_fields")
    last: dict[str, int] = {}
    path_day = DAYS[DAY_PATHS.index(day_match[2])] if day_match else None
    for item in resource.value:
        if not isinstance(item, dict):
            return (*_ordered(found), "point_not_object")
        minute, temperature = item.get("t"), item.get("Th")
        day = item.get("d") if "d" in item or path_day is None else path_day
        shape = (
            "day_missing_d"
            if "d" not in item and path_day is None
            else "day_invalid"
            if not isinstance(day, str) or day not in DAYS
            else "day_mismatch"
            if path_day is not None and day != path_day
            else "minute_invalid"
            if type(minute) is not int or not 0 <= minute < 1440
            else "temperature_invalid"
            if isinstance(temperature, bool)
            or not isinstance(temperature, int | float)
            or not math.isfinite(temperature)
            or not 0 <= temperature <= 30
            else None
        )
        if shape is not None:
            return (*_ordered(found), shape)
        assert isinstance(day, str) and type(minute) is int
        assert isinstance(temperature, int | float)
        if (key_issue := _point_key_issue(item)) is not None:
            found.add(key_issue)
        if minute <= last.get(day, -1):
            found.add("unsorted")
        if minute % 5:
            found.add("off_grid_minute")
        if not 5 <= temperature <= 30:
            found.add("temperature_range")
        if not float(temperature * 2).is_integer():
            found.add("temperature_grid")
        last[day] = minute
    return _ordered(found)


def _ordered(found: set[str]) -> tuple[str, ...]:
    return tuple(token for token in EDIT_ISSUES if token in found)


MAX_POINT_SHAPES = 4


def point_shapes(resource: Resource) -> tuple[str, ...]:
    """The distinct forms of a program's switch points for the
    diagnostics: key names and value types only, never a value.

    At most ``MAX_POINT_SHAPES`` forms, then ``"more"``; empty when the value
    is not a list.
    """
    if not (DAY.fullmatch(resource.path) or WEEK.fullmatch(resource.path)) or not (
        isinstance(resource.value, list)
    ):
        return ()
    shapes = sorted({value_shape(item) for item in resource.value[:512]})
    if len(shapes) > MAX_POINT_SHAPES:
        return (*shapes[:MAX_POINT_SHAPES], "more")
    return tuple(shapes)


def edit_point(
    schedule: Schedule, index: int, minute: int, temperature: float
) -> list[JsonValue]:
    """Change one existing point; do not infer device limits for new points."""
    if (
        not schedule.editable
        or type(index) is not int
        or not 0 <= index < len(schedule.points)
        or type(minute) is not int
        or not 0 <= minute < 1440
        or minute % 5
        or isinstance(temperature, bool)
        or not isinstance(temperature, int | float)
        or not math.isfinite(temperature)
        or not 5 <= temperature <= 30
        or not float(temperature * 2).is_integer()
    ):
        raise WriteValidationError("Unsupported RRC schedule change")
    original = schedule.points[index]
    if any(
        point.day == original.day and point.minute == minute and position != index
        for position, point in enumerate(schedule.points)
    ):
        raise WriteValidationError("A switch point already exists at this time")
    # Every point of the day as ``{d, t, Th}``; an optional ``name``
    # is not sent. Unchanged points keep the temperature as read.
    day_points: list[JsonValue] = []
    for position, item in enumerate(schedule.encode()):
        point = schedule.points[position]
        if point.day != original.day or not isinstance(item, dict):
            continue
        reading = item.get("Th")
        assert isinstance(reading, int | float)  # checked by the parser
        day_points.append(
            {"d": point.day, "t": minute, "Th": temperature}
            if position == index
            else {"d": point.day, "t": point.minute, "Th": reading}
        )
    # All entries have already passed the integer-minute parser.
    day_points.sort(key=_minute_of)
    return day_points


def _minute_of(item: JsonValue) -> int:
    assert isinstance(item, dict) and type(item["t"]) is int
    return item["t"]


def projection(points: list[JsonValue]) -> tuple[SwitchPoint, ...]:
    """The ``(d, t, Th)`` of a change built by ``edit_point``."""
    result: list[SwitchPoint] = []
    for item in points:
        assert isinstance(item, dict)
        day, minute, temperature = item["d"], item["t"], item["Th"]
        assert isinstance(day, str) and type(minute) is int
        assert isinstance(temperature, int | float)
        result.append(SwitchPoint(day, minute, float(temperature)))
    return tuple(result)


def selection_issue(resource: Resource, programs: Resource | None) -> str | None:
    """Why a program selection is not offered for a zone, or ``None``.

    The reasons are stable tokens for diagnostics (#113, RRC-02).
    """
    if rrc.scalar_field(resource.path) != "clockProgram":
        return "not_a_program_selection"
    if rrc.scalar_value(resource) is None:
        return "reading_invalid"
    if not resource.metadata.writable:
        return "selection_not_writable"
    if programs is None or programs.path != rrc.PROGRAM_LIST:
        return "program_list_missing"
    if (issue := rrc.list_issue(programs)) is not None:
        return f"program_list_invalid_{issue}"
    if not rrc.listed_ids(programs):
        return "program_list_empty"
    return None


def selection_options(resource: Resource, programs: Resource | None) -> tuple[int, ...]:
    if selection_issue(resource, programs) is not None or programs is None:
        return ()
    return rrc.listed_ids(programs) or ()


def program_names(
    programs: Resource | None, names: Mapping[int, Resource | None] | None = None
) -> dict[int, str]:
    """#113 (RRC-05): the readable name of each listed program.

    The list entry's name comes first, then a known ``/programs/pgN/name``.
    Both are usually Base64 and are decoded like zone names. A program without
    a usable name has no entry; its ID stays the option.
    """
    if programs is None or not (identifiers := rrc.listed_ids(programs)):
        return {}
    assert isinstance(programs.value, list)  # checked by listed_ids
    listed = {
        item["id"]: item.get("name")
        for item in programs.value
        if isinstance(item, dict)
    }
    result: dict[int, str] = {}
    for identifier in identifiers:
        named = rrc.display_name(listed.get(identifier))
        if named is None and names is not None:
            resource = names.get(identifier)
            if (
                resource is not None
                and resource.path == f"/programs/pg{identifier}/name"
                and resource.metadata.resource_type == "stringValue"
                and rrc.available(resource)
                and resource.has_value
            ):
                named = rrc.display_name(resource.value)
        if named is not None:
            result[identifier] = named
    return result


def selection_shows(path: str, program: int) -> Callable[[Resource], bool]:
    """Whether a later reading of ``path`` shows the selected program."""

    def shows(resource: Resource) -> bool:
        return resource.path == path and rrc.scalar_value(resource) == program

    return shows


def _day_of(resource: Resource, day: str) -> tuple[SwitchPoint, ...] | None:
    parsed = parse_schedule(resource)
    if parsed is None:
        return None
    return parsed.day_points(day)


def edit_shows(
    expected: Resource, index: int, minute: int, temperature: float
) -> Callable[[Resource], bool] | None:
    """Whether a later reading of the week shows a switch point change;
    ``None`` for a change that is refused anyway."""
    original = parse_schedule(expected)
    if original is None:
        return None
    try:
        desired = projection(edit_point(original, index, minute, temperature))
    except WriteValidationError:
        return None
    day = original.points[index].day

    def shows(resource: Resource) -> bool:
        return resource.path == expected.path and _day_of(resource, day) == desired

    return shows


class RrcProgramService:
    """Fresh preflight, one PUT, bounded read-back, no guessed program IDs."""

    def __init__(
        self,
        client: PointTClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        delays: tuple[float, ...] = RRC_READ_BACK_DELAYS,
    ) -> None:
        self.client = client
        self.sleep = sleep
        self.delays = delays

    def _writer(self) -> RrcStructWrite:
        # ``sleep`` may be replaced after construction (tests).
        return RrcStructWrite(sleep=self.sleep, delays=self.delays)

    async def select(
        self,
        gateway_id: str,
        path: str,
        program: int,
        *,
        on_sent: Callable[[], None] | None = None,
    ) -> tuple[Resource, ...]:
        if rrc.scalar_field(path) != "clockProgram" or type(program) is not int:
            raise WriteValidationError("Invalid RRC program selection")
        programs = await self.client.get_resource(gateway_id, rrc.PROGRAM_LIST)
        current = await self.client.get_resource(gateway_id, path)
        if current.path != path or program not in selection_options(current, programs):
            raise WriteValidationError("RRC program is no longer offered")
        if rrc.scalar_value(current) == program:
            return programs, current
        shows = selection_shows(path, program)

        async def preflight() -> None:
            return None

        async def send(_state: None) -> None:
            # The program number is a float on the wire, as the controller
            # reports it (``floatValue``).
            await self.client.put_resource_value(gateway_id, path, float(program))

        async def confirm(_state: None) -> tuple[Resource, ...] | None:
            confirmed = await self.client.get_resource(gateway_id, path)
            return (programs, confirmed) if shows(confirmed) else None

        return await self._writer().async_write(
            preflight=preflight, send=send, confirm=confirm, on_sent=on_sent
        )

    async def edit(
        self,
        gateway_id: str,
        expected: Resource,
        index: int,
        minute: int,
        temperature: float,
        *,
        on_sent: Callable[[], None] | None = None,
    ) -> tuple[Resource, ...]:
        original = parse_schedule(expected)
        if original is None or WEEK.fullmatch(expected.path) is None:
            raise WriteValidationError("Unsupported RRC week")
        body = edit_point(original, index, minute, temperature)
        desired = projection(body)
        day = original.points[index].day
        day_path = expected.path.removesuffix("week") + DAY_PATHS[DAYS.index(day)]
        current_week = await self.client.get_resource(gateway_id, expected.path)
        current = parse_schedule(current_week)
        # Compared as ``(d, t, Th)``; an optional ``name`` is ignored.
        if (
            current is None
            or current_week.path != expected.path
            or not current.editable
            or current.points != original.points
        ):
            raise WriteValidationError("RRC schedule changed; reopen the schedule")
        day_resource = await self.client.get_resource(gateway_id, day_path)
        current_day = parse_schedule(day_resource)
        expected_day = original.day_points(day)
        if (
            current_day is None
            or day_resource.path != day_path
            or not current_day.editable
            or current_day.points != expected_day
        ):
            raise WriteValidationError("RRC day changed or is not writable")
        if desired == expected_day:
            return (current_week,)

        async def preflight() -> None:
            return None

        async def send(_state: None) -> None:
            await self.client.put_resource_value(gateway_id, day_path, body)

        async def confirm(_state: None) -> tuple[Resource, ...] | None:
            confirmed = await self.client.get_resource(gateway_id, expected.path)
            # Other days may legitimately have changed meanwhile.
            if confirmed.path == expected.path and _day_of(confirmed, day) == desired:
                return (confirmed,)
            return None

        return await self._writer().async_write(
            preflight=preflight, send=send, confirm=confirm, on_sent=on_sent
        )
