"""TP-19: changing the hot water week program of an RRC 2.0 room controller.

``/dhwCircuits/dhwN/programs/pg1/week`` holds ``{d, t, dhw}`` points. The
room controller app writes the whole week at once as
``{"value": [{d, t, dhw}, ...]}``, sorted by day and time, always program 1.
The levels are program levels, not temperatures: the app offers ``high`` and
``off`` for new points; ``low`` and ``eco`` are kept where the program
already uses them (their meaning depends on the heat source) and never
offered otherwise. A level as read is never re-coded.

Limits: six points per day (a constant of the app, not reported by the
controller), times on the five-minute grid, one point per time and day. A
day that has points keeps at least one: what an empty day means on the
controller is not known. The week is read fresh and compared with the one
shown; one PUT, read-back of the week with the RRC schedule, no retry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

from .pointt import PointTClient, Resource, WriteValidationError
from .pointt.models import JsonValue
from .rrc_dhw import PROGRAM_WEEK, DhwSwitchPoint, parse_program
from .rrc_programs import APP_MAX_POINTS_PER_DAY
from .rrc_write import RrcStructWrite
from .switch_program_edit import EditableProgram, week_issue
from .switch_programs import DAYS, SwitchPoint
from .write_policies import RRC_READ_BACK_DELAYS, ContractRejected

# The levels a new point may have (app), and those kept only where used.
NEW_LEVELS: Final = ("high", "off")
KEPT_LEVELS: Final = ("low", "eco")
# Levels as read, by the key the dialog uses ("Off" is read as "off").
_READS: Final = {"off": "off", "Off": "off", "low": "low", "high": "high", "eco": "eco"}
RASTER: Final = 5
# Seven days of at most six points.
MAX_IN_ALL: Final = APP_MAX_POINTS_PER_DAY * len(DAYS)


def edit_issue(resource: Resource | None) -> str | None:
    """Why a hot water week program cannot be changed, as a token, or ``None``."""
    if resource is None:
        return "not_a_program"
    match = PROGRAM_WEEK.fullmatch(resource.path)
    if match is None:
        return "not_a_program"
    if match[2] != "1":
        # The app writes program 1 only; another ID is never guessed.
        return "program_id_unexpected"
    program = parse_program(resource)
    if program is None:
        return "program_unreadable"
    if not resource.metadata.writable:
        return "not_writable"
    if program.unknown_fields:
        return "unknown_fields"
    if not program.ordered:
        return "unsorted"
    if any(point.level not in _READS for point in program.points):
        return "unknown_levels"
    if any(point.minute % RASTER for point in program.points):
        return "off_grid_minute"
    counts = [0] * len(DAYS)
    for point in program.points:
        counts[DAYS.index(point.day)] += 1
    if any(count > APP_MAX_POINTS_PER_DAY for count in counts):
        return "too_many_per_day"
    return None


def points_of(resource: Resource) -> tuple[SwitchPoint, ...]:
    """The program as dialog points; the level key as the dialog names it."""
    program = parse_program(resource)
    if program is None:
        return ()
    return tuple(
        SwitchPoint(
            DAYS.index(point.day), point.minute, _READS.get(point.level, point.level)
        )
        for point in program.points
    )


def editable(resource: Resource) -> EditableProgram | None:
    """The program for the dialog, or ``None`` where it cannot be changed."""
    if edit_issue(resource) is not None:
        return None
    points = points_of(resource)
    used = {point.level for point in points}
    levels = (*NEW_LEVELS, *(level for level in KEPT_LEVELS if level in used))
    return EditableProgram(
        resource.path,
        tuple(sorted(points, key=lambda point: (point.day, point.minute))),
        MAX_IN_ALL,
        APP_MAX_POINTS_PER_DAY,
        RASTER,
        levels,
        kind="rrc_dhw",
        value_type="level",
        body="value",
        keep_days=frozenset(point.day for point in points),
    )


def encode_week(resource: Resource, points: Sequence[SwitchPoint]) -> list[JsonValue]:
    """The whole week as ``{d, t, dhw}``, sorted; a point as read keeps its
    level text as read (``Off`` stays ``Off``)."""
    program = parse_program(resource)
    raw: dict[tuple[int, int, str], str] = {}
    if program is not None:
        for point in program.points:
            raw[
                (
                    DAYS.index(point.day),
                    point.minute,
                    _READS.get(point.level, point.level),
                )
            ] = point.level
    return [
        {
            "d": DAYS[point.day],
            "t": point.minute,
            "dhw": raw.get((point.day, point.minute, point.level), point.level),
        }
        for point in sorted(points, key=lambda item: (item.day, item.minute))
    ]


def _normalized(resource: Resource) -> frozenset[tuple[int, int, str]] | None:
    program = parse_program(resource)
    if program is None:
        return None
    return frozenset(
        (DAYS.index(point.day), point.minute, _READS.get(point.level, point.level))
        for point in program.points
    )


def week_shows(path: str, points: Sequence[SwitchPoint]) -> Callable[[Resource], bool]:
    """Whether a reading shows exactly these points (levels as the dialog
    names them; any order)."""
    desired = frozenset((point.day, point.minute, point.level) for point in points)

    def shows(resource: Resource) -> bool:
        return resource.path == path and _normalized(resource) == desired

    return shows


class RrcDhwProgramService:
    """Fresh preflight, one PUT of the week, read-back; never a second PUT."""

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

    async def write(
        self,
        gateway_id: str,
        expected: Resource,
        points: Sequence[SwitchPoint],
        *,
        on_sent: Callable[[], None] | None = None,
    ) -> tuple[Resource, ...]:
        shown = editable(expected)
        if shown is None:
            raise WriteValidationError("The hot water program cannot be changed")
        if (issue := week_issue(points, shown)) is not None:
            raise ContractRejected(f"program_{issue}")
        path = expected.path
        current = await self.client.get_resource(gateway_id, path)
        live = editable(current)
        if (
            current.path != path
            or live is None
            or _normalized(current) != _normalized(expected)
            or current.metadata.used is False
            or current.metadata.available is False
        ):
            raise ContractRejected("program_changed")
        shows = week_shows(path, points)
        if shows(current):
            return (current,)
        body = encode_week(current, points)

        async def preflight() -> None:
            return None

        async def send(_state: None) -> None:
            await self.client.put_resource_value(gateway_id, path, body)

        async def confirm(_state: None) -> tuple[Resource, ...] | None:
            reading = await self.client.get_resource(gateway_id, path)
            return (reading,) if shows(reading) else None

        writer = RrcStructWrite(sleep=self.sleep, delays=self.delays)
        return await writer.async_write(
            preflight=preflight, send=send, confirm=confirm, on_sent=on_sent
        )


__all__ = ["DhwSwitchPoint", "RrcDhwProgramService", "edit_issue", "editable"]
