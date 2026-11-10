"""Lossless RRC schedules and single-write transactions for existing programs."""

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
    RateLimited,
    RequestTimeout,
    Resource,
    ServiceUnavailable,
    TransportError,
    WriteNotConfirmed,
    WriteValidationError,
)
from .pointt.models import JsonValue

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


def parse_schedule(resource: Resource) -> Schedule | None:
    day_match = DAY.fullmatch(resource.path)
    # #113 (RRC-07): the vendor app types a single day as ``dayProgram`` with
    # the same switch points as the week. This follows the vendor app; a
    # hardware confirmation is pending. A week stays ``weekProgram`` only.
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
    for item in resource.value:
        if not isinstance(item, dict):
            return None
        day, minute, temperature = item.get("d"), item.get("t"), item.get("Th")
        if (
            not isinstance(day, str)
            or day not in DAYS
            or type(minute) is not int
            or not 0 <= minute < 1440
            or isinstance(temperature, bool)
            or not isinstance(temperature, int | float)
            or not math.isfinite(temperature)
            or not 0 <= temperature <= 30
            or (day_match is not None and day != DAYS[DAY_PATHS.index(day_match[2])])
        ):
            return None
        if (
            set(item) != {"d", "t", "Th"}
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
    day_points: list[dict[str, JsonValue]] = []
    for position, item in enumerate(schedule.encode()):
        if isinstance(item, dict) and item.get("d") == original.day:
            if position == index:
                item["t"], item["Th"] = minute, temperature
            day_points.append(item)
    # All entries have already passed the integer-minute parser.
    day_points.sort(key=lambda item: int(str(item["t"])))
    return list(day_points)


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


class RrcProgramService:
    """Fresh preflight, one PUT, bounded read-back, no guessed program IDs."""

    def __init__(
        self,
        client: PointTClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.sleep = sleep

    async def select(
        self, gateway_id: str, path: str, program: int
    ) -> tuple[Resource, ...]:
        if rrc.scalar_field(path) != "clockProgram" or type(program) is not int:
            raise WriteValidationError("Invalid RRC program selection")
        programs = await self.client.get_resource(gateway_id, rrc.PROGRAM_LIST)
        current = await self.client.get_resource(gateway_id, path)
        if current.path != path or program not in selection_options(current, programs):
            raise WriteValidationError("RRC program is no longer offered")
        if rrc.scalar_value(current) == program:
            return programs, current
        try:
            await self.client.put_resource_value(gateway_id, path, program)
        except RateLimited:
            raise
        except TransportError:
            # A transport failure can follow an applied write. Never repeat it.
            pass
        for attempt in range(3):
            await self.sleep(0.5 * 2**attempt)
            try:
                confirmed = await self.client.get_resource(gateway_id, path)
            except RequestTimeout, ServiceUnavailable:
                continue
            if confirmed.path == path and rrc.scalar_value(confirmed) == program:
                return programs, confirmed
        raise WriteNotConfirmed("RRC program selection was not confirmed")

    async def edit(
        self,
        gateway_id: str,
        expected: Resource,
        index: int,
        minute: int,
        temperature: float,
    ) -> tuple[Resource, ...]:
        original = parse_schedule(expected)
        if original is None or WEEK.fullmatch(expected.path) is None:
            raise WriteValidationError("Unsupported RRC week")
        desired = edit_point(original, index, minute, temperature)
        day = original.points[index].day
        day_path = expected.path.removesuffix("week") + DAY_PATHS[DAYS.index(day)]
        current_week = await self.client.get_resource(gateway_id, expected.path)
        current = parse_schedule(current_week)
        if (
            current is None
            or current_week.path != expected.path
            or not current.editable
            or current.raw_value != original.raw_value
        ):
            raise WriteValidationError("RRC schedule changed; reopen the schedule")
        day_resource = await self.client.get_resource(gateway_id, day_path)
        current_day = parse_schedule(day_resource)
        expected_day = [
            item
            for item in original.encode()
            if isinstance(item, dict) and item.get("d") == day
        ]
        if (
            current_day is None
            or day_resource.path != day_path
            or not current_day.editable
            or current_day.encode() != expected_day
        ):
            raise WriteValidationError("RRC day changed or is not writable")
        if desired == expected_day:
            return (current_week,)
        try:
            await self.client.put_resource_value(gateway_id, day_path, desired)
        except RateLimited:
            raise
        except TransportError:
            pass
        for attempt in range(3):
            await self.sleep(0.5 * 2**attempt)
            try:
                confirmed = await self.client.get_resource(gateway_id, expected.path)
            except RequestTimeout, ServiceUnavailable:
                continue
            parsed = parse_schedule(confirmed)
            if parsed is None or confirmed.path != expected.path:
                continue
            read_day = [
                item
                for item in parsed.encode()
                if isinstance(item, dict) and item.get("d") == day
            ]
            if read_day == desired:
                # Other days may legitimately have changed in the app meanwhile.
                return (confirmed,)
        raise WriteNotConfirmed("RRC schedule change was not confirmed")
