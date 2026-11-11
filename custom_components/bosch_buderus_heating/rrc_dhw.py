"""Hot water of RRC 2.0 room controllers (W3): program, clock and results.

Pure, without Home Assistant imports, so that the hot water contract
(``rrc_contract_dhw``) can report on its resources. The hot water program is
read losslessly and never written; its current level is derived from the
switch points and the controller's own clock. Everything here is expected
from the controller's data shapes and still untested on a real controller.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from .pointt import Resource
from .pointt.models import JsonValue
from .rrc_contract_base import RRC_ID

CIRCUIT: Final = rf"/dhwCircuits/dhw{RRC_ID}"
OPERATION_MODE = re.compile(rf"^{CIRCUIT}/operationMode$")
PROGRAM_WEEK = re.compile(rf"^/dhwCircuits/dhw({RRC_ID})/programs/pg({RRC_ID})/week$")
EXTRA_DHW = re.compile(rf"^{CIRCUIT}/extraDhw$")
DISINFECTION_TIME = re.compile(rf"^{CIRCUIT}/thermalDisinfect/time$")
DISINFECTION_RESULT = re.compile(rf"^{CIRCUIT}/thermalDisinfect/lastResult$")
# The controller's clock, such as "2026-11-10T06:30:00+01:00 Tu".
CLOCK_PATH: Final = "/gateway/DateTime"

DAYS: Final = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
# The levels of a switch point, as shown by the level sensor. The controller
# writes "Off" and may read it back as "off".
LEVELS: Final = ("off", "low", "high", "eco")
_LEVEL_READS: Final[Mapping[str, str]] = {
    "off": "off",
    "Off": "off",
    "low": "low",
    "high": "high",
    "eco": "eco",
}
MAX_POINTS: Final = 512
MINUTES_PER_DAY: Final = 1440
# The clock is read every few minutes; an older reading is not carried on.
MAX_CLOCK_AGE: Final = timedelta(hours=1)
_CLOCK = re.compile(
    r"(?P<time>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)"
    r"(?: (?P<day>Mo|Tu|We|Th|Fr|Sa|Su))?"
)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
_LEVEL = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")


@dataclass(frozen=True, slots=True)
class DhwSwitchPoint:
    day: str
    minute: int
    # The wire level, such as "high" or "off".
    level: str


@dataclass(frozen=True, slots=True)
class DhwProgram:
    """A hot water week program, kept complete with unknown fields and order."""

    path: str
    points: tuple[DhwSwitchPoint, ...]
    raw_value: str
    # Some switch point carries more than ``d``, ``t`` and ``dhw``.
    unknown_fields: bool
    # The points of each day are in strictly rising time order.
    ordered: bool

    def encode(self) -> list[JsonValue]:
        """The original array, without dropping a field."""
        value: list[JsonValue] = json.loads(self.raw_value)
        return value


def _usable(resource: Resource) -> bool:
    """Missing flags are allowed; false or malformed flags are not."""
    metadata = resource.metadata
    return (
        metadata.availability_flags_valid
        and metadata.used is not False
        and metadata.available is not False
    )


def parse_program(resource: Resource) -> DhwProgram | None:
    """CT-D06: a hot water week program (``dhwProgram``), or ``None``.

    Each switch point is ``{"d": day, "t": minute of the day, "dhw": level}``.
    Further fields are kept and reported; a malformed point rejects the whole
    program, so that no level is ever guessed.
    """
    if (
        PROGRAM_WEEK.fullmatch(resource.path) is None
        or resource.metadata.resource_type != "dhwProgram"
        or not _usable(resource)
        or resource.references
        or resource.has_values
        or not resource.has_value
        or not isinstance(resource.value, list)
        or len(resource.value) > MAX_POINTS
    ):
        return None
    points: list[DhwSwitchPoint] = []
    unknown_fields = False
    ordered = True
    last: dict[str, int] = {}
    for item in resource.value:
        if not isinstance(item, dict):
            return None
        day, minute, level = item.get("d"), item.get("t"), item.get("dhw")
        if (
            not isinstance(day, str)
            or day not in DAYS
            or type(minute) is not int
            or not 0 <= minute < MINUTES_PER_DAY
            or not isinstance(level, str)
            or _LEVEL.fullmatch(level) is None
        ):
            return None
        unknown_fields = unknown_fields or set(item) != {"d", "t", "dhw"}
        ordered = ordered and minute > last.get(day, -1)
        last[day] = minute
        points.append(DhwSwitchPoint(day, minute, level))
    return DhwProgram(
        resource.path,
        tuple(points),
        json.dumps(resource.value, ensure_ascii=True, allow_nan=False, sort_keys=True),
        unknown_fields,
        ordered,
    )


def level_option(raw: str) -> str | None:
    """The sensor option of a wire level; ``None`` for an unknown one."""
    return _LEVEL_READS.get(raw)


def current_point(
    program: DhwProgram, weekday: int, minute: int
) -> DhwSwitchPoint | None:
    """The switch point in effect at ``minute`` of ``weekday`` (0 = Monday).

    The last point at or before that time, looking back over the week; none
    for an empty program or one whose points are out of order.
    """
    if not program.points or not program.ordered:
        return None
    if not 0 <= weekday < len(DAYS) or not 0 <= minute < MINUTES_PER_DAY:
        return None
    for back in range(len(DAYS) + 1):
        day = DAYS[(weekday - back) % len(DAYS)]
        candidates = [
            point
            for point in program.points
            if point.day == day and (back != 0 or point.minute <= minute)
        ]
        if candidates:
            return max(candidates, key=lambda point: point.minute)
    return None


def controller_now(
    clock: Resource, read_at: datetime, now: datetime
) -> datetime | None:
    """The controller's local time now, from a reading of its clock.

    ``read_at`` is when the reading arrived and ``now`` the current time
    (both aware). The time passed since then is added; an older reading,
    one from the future or a clock whose weekday contradicts its date gives
    ``None``.
    """
    if (
        clock.path != CLOCK_PATH
        or clock.metadata.resource_type != "stringValue"
        or not clock.has_value
        or not isinstance(clock.value, str)
        or (match := _CLOCK.fullmatch(clock.value.strip())) is None
    ):
        return None
    try:
        reading = datetime.fromisoformat(match["time"])
    except ValueError:
        return None
    if match["day"] is not None and match["day"] != DAYS[reading.weekday()]:
        return None
    elapsed = now - read_at
    if elapsed < timedelta(0) or elapsed > MAX_CLOCK_AGE:
        return None
    return reading + elapsed


def result_token(resource: Resource) -> str | None:
    """The last disinfection result as a short code, never free text."""
    if (
        DISINFECTION_RESULT.fullmatch(resource.path) is None
        or resource.metadata.resource_type != "stringValue"
        or not _usable(resource)
        or not resource.has_value
        or not isinstance(resource.value, str)
        or _TOKEN.fullmatch(resource.value) is None
    ):
        return None
    return resource.value


def diagnostics(resources: Mapping[str, Resource]) -> dict[str, object]:
    """S12: counts and resource types of the hot water resources, no values."""
    circuits = 0
    advertised = 0
    # An advertised empty list leaves the mode a reading; a missing key
    # gives the fallback modes.
    empty = 0
    extra: dict[str, int] = {}
    time_types: dict[str, int] = {}
    programs = {"count": 0, "valid": 0, "points": 0, "unknown_fields": 0}
    unknown_levels = 0
    for path, resource in resources.items():
        if OPERATION_MODE.fullmatch(path):
            circuits += 1
            advertised += not resource.metadata.allowed_values_missing
            empty += (
                not resource.metadata.allowed_values_missing
                and not resource.metadata.allowed_values
            )
        elif EXTRA_DHW.fullmatch(path):
            metadata = resource.metadata
            state = (
                "malformed"
                if not metadata.availability_flags_valid
                else "locked"
                if metadata.used is True and metadata.available is False
                else "unavailable"
                if metadata.available is False
                else "available"
            )
            extra[state] = extra.get(state, 0) + 1
        elif DISINFECTION_TIME.fullmatch(path):
            kind = str(resource.metadata.resource_type or "none")
            kind = kind if _TOKEN.fullmatch(kind) else "other"
            time_types[kind] = time_types.get(kind, 0) + 1
        elif PROGRAM_WEEK.fullmatch(path):
            programs["count"] += 1
            if (program := parse_program(resource)) is not None:
                programs["valid"] += 1
                programs["points"] += len(program.points)
                programs["unknown_fields"] += program.unknown_fields
                unknown_levels += sum(
                    level_option(point.level) is None for point in program.points
                )
    return {
        "circuits": circuits,
        "operation_mode_options_advertised": advertised,
        "operation_mode_options_empty": empty,
        "extra_dhw": dict(sorted(extra.items())),
        "thermal_disinfection_time_types": dict(sorted(time_types.items())),
        "programs": {**programs, "unknown_levels": unknown_levels},
    }
