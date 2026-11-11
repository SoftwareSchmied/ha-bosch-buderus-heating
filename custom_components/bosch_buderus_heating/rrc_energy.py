"""CT-G05: the daily energy history of an RRC 2.0 room controller.

Pure, without Home Assistant imports: the wire records, the stored state and
one bounded read cycle. ``rrc_energy_statistics`` runs the cycle and imports
the result as external statistics. Everything here is expected, untested on
real hardware (W5-E1): the energy values are taken as kWh, gas values only
with a confirmed unit.

The history is never a discovered resource and never the state of an entity
(P39). A cycle sends at most ``MAX_REQUESTS`` requests, and a first filling
reaches back at most ``FIRST_FILL_DAYS`` days.

Every cycle reads again from the last day the controller is known to have:
that day may have been the running one when it was read, and a full history
drops its oldest days, so neither the day after it nor a stored entry index
alone is a safe start.
"""

from __future__ import annotations

import json
import math
import re
import zlib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final, Literal, Protocol

from .pointt import (
    PointTError,
    RateLimited,
    ResourceError,
    ServiceUnavailable,
    UnexpectedHttpStatus,
)
from .pointt.models import JsonValue

HISTORY_PATH: Final = "/energy/history"
ENTRIES_PATH: Final = "/energy/historyEntries"
GAS_UNIT_PATH: Final = "/energy/gas/unit"
FIRMWARE_PATH: Final = "/gateway/versionFirmware"
HEAT_SOURCE_TYPE_PATH: Final = "/heatSources/type"
# Firmware from which the history is read by date instead of by entry.
DATE_QUERY_FIRMWARE: Final = (5, 3, 0)
MAX_REQUESTS: Final = 8
FIRST_FILL_DAYS: Final = 400
# A past day that changes is recalculated if it is this recent at most.
RECOMPUTE_DAYS: Final = 31
# The gas unit is read again after this long.
GAS_UNIT_MAX_AGE: Final = timedelta(hours=24)
STATE_VERSION: Final = 1
# P31/CT-B06: a controller answers HTTP 500 for a path it does not have.
# After this many cycles in a row ending in 5xx the query form counts as
# refused, so a plant without a history is not asked every cycle.
MAX_SERVER_ERROR_CYCLES: Final = 3
# A heat source type answered with 5xx holds the days with older keys for
# this long; only then are they taken without those keys.
HEAT_SOURCE_WAIT: Final = timedelta(hours=24)

type Mode = Literal["date", "entry"]
type Kind = Literal["energy", "gas", "temperature"]
# What the older keys ``eCH``/``eHW`` measure: the heat pump's electricity on
# a heat pump or hybrid plant, gas on any other (boiler, no appliance,
# unknown type).
type Legacy = Literal["electricity", "gas"]
_DATE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")
_NUMBER = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)")
_FIRMWARE = re.compile(r"(\d{1,3})\.(\d{1,3})(?:\.(\d{1,3}))?")
_MAX_VALUE = 1_000_000.0


@dataclass(frozen=True, slots=True)
class EnergyKey:
    """One value of a daily record and the statistic it becomes."""

    wire: str
    slug: str
    kind: Kind
    names: tuple[str, str]


_KWH = ("(kWh erwartet)", "(kWh expected)")


def _energy(wire: str, slug: str, german: str, english: str) -> EnergyKey:
    return EnergyKey(
        wire, slug, "energy", (f"{german} {_KWH[0]}", f"{english} {_KWH[1]}")
    )


KEYS: Final[tuple[EnergyKey, ...]] = (
    _energy(
        "eCh",
        "heat_pump_electricity_heating",
        "Wärmepumpe Strom Heizen",
        "Heat pump electricity heating",
    ),
    _energy(
        "eHw",
        "heat_pump_electricity_hot_water",
        "Wärmepumpe Strom Warmwasser",
        "Heat pump electricity hot water",
    ),
    _energy(
        "eCo",
        "heat_pump_electricity_cooling",
        "Wärmepumpe Strom Kühlen",
        "Heat pump electricity cooling",
    ),
    _energy(
        "eChB",
        "boiler_electricity_heating",
        "Heizgerät Strom Heizen",
        "Boiler electricity heating",
    ),
    _energy(
        "eHwB",
        "boiler_electricity_hot_water",
        "Heizgerät Strom Warmwasser",
        "Boiler electricity hot water",
    ),
    _energy(
        "aCh",
        "electric_heater_heating",
        "Elektroheizstab Heizen",
        "Electric heater heating",
    ),
    _energy(
        "aHw",
        "electric_heater_hot_water",
        "Elektroheizstab Warmwasser",
        "Electric heater hot water",
    ),
    _energy(
        "pS",
        "solar_electricity_produced",
        "Solarstrom erzeugt",
        "Solar electricity produced",
    ),
    _energy(
        "pChH",
        "heat_pump_heat_heating",
        "Wärmepumpe Wärme Heizen",
        "Heat pump heat heating",
    ),
    _energy(
        "pHwH",
        "heat_pump_heat_hot_water",
        "Wärmepumpe Wärme Warmwasser",
        "Heat pump heat hot water",
    ),
    _energy(
        "pCo",
        "heat_pump_cooling_produced",
        "Wärmepumpe Kühlenergie",
        "Heat pump cooling energy",
    ),
    _energy(
        "pChB",
        "boiler_heat_heating",
        "Heizgerät Wärme Heizen",
        "Boiler heat heating",
    ),
    _energy(
        "pHwB",
        "boiler_heat_hot_water",
        "Heizgerät Wärme Warmwasser",
        "Boiler heat hot water",
    ),
    EnergyKey(
        "gCh",
        "gas_heating",
        "gas",
        ("Gas Heizen (kWh)", "Gas heating (kWh)"),
    ),
    EnergyKey(
        "gHw",
        "gas_hot_water",
        "gas",
        ("Gas Warmwasser (kWh)", "Gas hot water (kWh)"),
    ),
    EnergyKey(
        "T",
        "outdoor_temperature",
        "temperature",
        (
            "Außentemperatur Tagesmittel (erwartet)",
            "Outdoor temperature daily mean (expected)",
        ),
    ),
)
KEY_BY_SLUG: Final = {key.slug: key for key in KEYS}
# An older key -> the key it stands for on an electric plant and on any
# other plant. It is read only when that key itself is missing.
LEGACY_KEYS: Final[Mapping[str, tuple[str, str]]] = {
    "eCH": ("eCh", "gCh"),
    "eHW": ("eHw", "gHw"),
}
_ELECTRIC_SOURCES: Final = frozenset({"heatpump", "hybrid", "hybridgas", "hybridoil"})


def statistic_names() -> tuple[tuple[str, str], ...]:
    """Every (German, English) statistic name, for the translation checks."""
    return tuple(key.names for key in KEYS)


def legacy_meaning(heat_source_type: object) -> Legacy:
    """What ``eCH``/``eHW`` measure on a plant with this heat source type.

    A heat pump or a hybrid plant: the heat pump's electricity. A boiler, no
    appliance, an unknown or a missing type: gas, which is imported only
    with a confirmed unit, so gas never becomes heat pump electricity.
    """
    if isinstance(heat_source_type, str):
        text = re.sub(r"[^a-z]", "", heat_source_type.casefold())
        if text in _ELECTRIC_SOURCES:
            return "electricity"
    return "gas"


def uses_legacy_keys(items: Iterable[object]) -> bool:
    """Whether an answer carries a value under an older key."""
    return any(
        isinstance(item, dict)
        and any(item.get(name) is not None for name in LEGACY_KEYS)
        for item in items
    )


class HistoryRefused(PointTError):
    """The controller refused the chosen form of the history query (400/404)."""


@dataclass(frozen=True, slots=True)
class DayRecord:
    """The values of one completed day, by statistic slug."""

    day: date
    values: Mapping[str, float]


def parse_day(text: object) -> date | None:
    """A record date ``dd-MM-yyyy``; placeholders and impossible dates fail."""
    if not isinstance(text, str) or (match := _DATE.fullmatch(text.strip())) is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and _NUMBER.fullmatch(value.strip()):
        value = float(value)
    if not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _wire_names(wire: str, legacy: Legacy | None) -> tuple[str, ...]:
    """The key itself, then the older key that stands for it on this plant."""
    if legacy is None:
        return (wire,)
    index = 0 if legacy == "electricity" else 1
    return (wire, *(old for old, meant in LEGACY_KEYS.items() if meant[index] == wire))


def _record(item: object, legacy: Legacy | None) -> DayRecord | None:
    if not isinstance(item, dict):
        return None
    day = parse_day(item.get("d"))
    if day is None:
        return None
    values: dict[str, float] = {}
    for key in KEYS:
        raw = next(
            (
                item[name]
                for name in _wire_names(key.wire, legacy)
                if name in item and item[name] is not None
            ),
            None,
        )
        number = _number(raw)
        if number is None:
            continue
        if key.kind == "temperature":
            if -60.0 <= number <= 70.0:
                values[key.slug] = number
        elif 0.0 <= number <= _MAX_VALUE:
            # Never a negative amount: a sum must not run backwards.
            values[key.slug] = number
    return DayRecord(day, values)


def history_items(value: JsonValue, values: Iterable[JsonValue] = ()) -> list[object]:
    """The record list of a history answer (``value`` or ``values``)."""
    if isinstance(value, list):
        return list[object](value)
    listed = list[object](values)
    if listed:
        return listed
    if isinstance(value, dict) and isinstance(inner := value.get("value"), list):
        return list[object](inner)
    return []


def parse_records(
    items: Iterable[object], legacy: Legacy | None = None
) -> tuple[list[DayRecord], int]:
    """Valid daily records in their order, and the number of invalid ones.

    The older keys ``eCH``/``eHW`` are read only with their ``legacy``
    meaning; without it they are left out, never guessed.
    """
    records: list[DayRecord] = []
    invalid = 0
    for item in items:
        record = _record(item, legacy)
        if record is None:
            invalid += 1
        else:
            records.append(record)
    return records, invalid


def firmware_version(value: object) -> tuple[int, int, int] | None:
    """``05.03.00`` as (5, 3, 0); anything else None."""
    if not isinstance(value, str):
        return None
    match = _FIRMWARE.match(value.strip().lstrip("vV"))
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def preferred_mode(firmware: object) -> Mode:
    """S10: by date from 5.3.0 on, else by entry (also without a version)."""
    version = firmware_version(firmware)
    return "date" if version is not None and version >= DATE_QUERY_FIRMWARE else "entry"


def _other(mode: Mode) -> Mode:
    return "entry" if mode == "date" else "date"


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day is not None else None


def _day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _float_map(value: object) -> dict[str, float]:
    result: dict[str, float] = {}
    if not isinstance(value, dict):
        return result
    for slug, raw in value.items():
        number = _number(raw)
        if slug not in KEY_BY_SLUG or number is None:
            continue
        if KEY_BY_SLUG[slug].kind == "temperature" or number >= 0:
            result[slug] = number
    return result


@dataclass(slots=True)
class HistoryState:
    """What one gateway's history import has reached; stored privately.

    The sum of a statistic at a day is ``base`` plus every value of
    ``recent`` up to that day, less its ``offsets`` (the sum before its first
    imported day). ``recent`` holds the last ``RECOMPUTE_DAYS`` days, so a
    changed day among them can be recalculated.
    """

    mode: Mode | None = None
    # The other form after the chosen one was refused; never switched back.
    switched: bool = False
    unsupported: bool = False
    firmware: str | None = None
    # By entry: the index of the first entry not known as a completed day.
    position: int | None = None
    entries: int | None = None
    last_day: date | None = None
    # By date: the newest completed day the controller answered; the next
    # cycle asks from this day on, which it has for sure.
    cursor: date | None = None
    # Days up to this one are in statistics recorded before this state
    # began: they are never written again, the sums go on from those rows.
    floor: date | None = None
    statistics_checked: bool = False
    # What the older keys eCH/eHW measure on this plant, once known.
    heat_source: Legacy | None = None
    # Cycles in a row whose type read ended in 5xx, and when the first did.
    heat_source_errors: int = 0
    heat_source_unreadable_since: datetime | None = None
    base: dict[str, float] = field(default_factory=dict)
    recent: dict[date, dict[str, float]] = field(default_factory=dict)
    # Statistics with at least one value above zero, and their offsets.
    active: dict[str, float] = field(default_factory=dict)
    # A short digest of every older day, to notice a change outside the
    # recalculation window (counted, never recalculated).
    digests: dict[date, str] = field(default_factory=dict)
    gas_unit_confirmed: bool | None = None
    gas_checked_at: datetime | None = None
    # A gas value above zero was read, imported or not.
    gas_seen: bool = False
    server_errors: int = 0
    next_due: datetime | None = None
    reason: str | None = None
    cycles: int = 0
    last_requests: int = 0
    imported_days: int = 0
    invalid_records: int = 0
    old_changes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "version": STATE_VERSION,
            "mode": self.mode,
            "switched": self.switched,
            "unsupported": self.unsupported,
            "firmware": self.firmware,
            "position": self.position,
            "entries": self.entries,
            "last_day": _iso(self.last_day),
            "cursor": _iso(self.cursor),
            "floor": _iso(self.floor),
            "statistics_checked": self.statistics_checked,
            "heat_source": self.heat_source,
            "heat_source_errors": self.heat_source_errors,
            "heat_source_unreadable_since": (
                self.heat_source_unreadable_since.isoformat()
                if self.heat_source_unreadable_since
                else None
            ),
            "base": dict(self.base),
            "recent": {
                day.isoformat(): dict(values)
                for day, values in sorted(self.recent.items())
            },
            "active": dict(self.active),
            "digests": {
                day.isoformat(): digest for day, digest in sorted(self.digests.items())
            },
            "gas_unit_confirmed": self.gas_unit_confirmed,
            "gas_seen": self.gas_seen,
            "server_errors": self.server_errors,
            "gas_checked_at": (
                self.gas_checked_at.isoformat() if self.gas_checked_at else None
            ),
            "next_due": self.next_due.isoformat() if self.next_due else None,
            "reason": self.reason,
            "cycles": self.cycles,
            "last_requests": self.last_requests,
            "imported_days": self.imported_days,
            "invalid_records": self.invalid_records,
            "old_changes": self.old_changes,
        }

    @classmethod
    def from_dict(cls, data: object) -> HistoryState:
        """A stored state; anything unreadable starts over (never a guess)."""
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            return cls()

        def integer(name: str) -> int | None:
            value = data.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        mode = data.get("mode")
        recent: dict[date, dict[str, float]] = {}
        raw_recent = data.get("recent")
        if isinstance(raw_recent, dict):
            for text, values in raw_recent.items():
                if (day := _day(text)) is not None:
                    recent[day] = _float_map(values)
        digests: dict[date, str] = {}
        raw_digests = data.get("digests")
        if isinstance(raw_digests, dict):
            for text, digest in raw_digests.items():
                if (day := _day(text)) is not None and isinstance(digest, str):
                    digests[day] = digest
        last_day = _day(data.get("last_day"))
        if last_day is None and recent:
            return cls()  # values without their position cannot be continued
        gas = data.get("gas_unit_confirmed")
        reason = data.get("reason")
        heat_source = data.get("heat_source")
        return cls(
            mode=mode if mode in ("date", "entry") else None,
            switched=data.get("switched") is True,
            unsupported=data.get("unsupported") is True,
            firmware=fw if isinstance(fw := data.get("firmware"), str) else None,
            position=integer("position"),
            entries=integer("entries"),
            last_day=last_day,
            cursor=_day(data.get("cursor")),
            floor=_day(data.get("floor")),
            statistics_checked=data.get("statistics_checked") is True,
            heat_source=(
                heat_source if heat_source in ("electricity", "gas") else None
            ),
            heat_source_errors=integer("heat_source_errors") or 0,
            heat_source_unreadable_since=_when(
                data.get("heat_source_unreadable_since")
            ),
            base=_float_map(data.get("base")),
            recent=recent,
            active=_float_map(data.get("active")),
            digests=digests,
            gas_unit_confirmed=gas if isinstance(gas, bool) else None,
            gas_seen=data.get("gas_seen") is True,
            server_errors=integer("server_errors") or 0,
            gas_checked_at=_when(data.get("gas_checked_at")),
            next_due=_when(data.get("next_due")),
            reason=reason if isinstance(reason, str) else None,
            cycles=integer("cycles") or 0,
            last_requests=integer("last_requests") or 0,
            imported_days=integer("imported_days") or 0,
            invalid_records=integer("invalid_records") or 0,
            old_changes=integer("old_changes") or 0,
        )


# Statistic slug -> [(day start, value)]; a sum for energy, the mean for
# the temperature.
type Rows = dict[str, list[tuple[date, float]]]


def importable(key: EnergyKey, state: HistoryState) -> bool:
    """Gas only with a confirmed unit (P10: units are never guessed)."""
    return key.kind != "gas" or state.gas_unit_confirmed is True


def _digest(values: Mapping[str, float]) -> str:
    text = json.dumps(sorted(values.items()), separators=(",", ":"))
    return f"{zlib.crc32(text.encode()):08x}"


def merge(state: HistoryState, records: Iterable[DayRecord], today: date) -> Rows:
    """Add the records of one answer and return the statistic rows to write.

    Only completed days count (never today), none older than
    ``FIRST_FILL_DAYS`` and none up to ``floor``. A new day extends the sums;
    a known day of the last ``RECOMPUTE_DAYS`` days whose values changed
    recalculates the sums from that day; an older change is counted and left
    alone. Within one answer a later record of the same day wins. A statistic
    starts with its first value above zero, a gas statistic only with a
    confirmed unit.
    """
    oldest = today - timedelta(days=FIRST_FILL_DAYS)
    window_start = (
        state.last_day - timedelta(days=RECOMPUTE_DAYS - 1)
        if state.last_day is not None
        else None
    )
    incoming: dict[date, dict[str, float]] = {}
    for record in records:
        if oldest <= record.day < today and (
            state.floor is None or record.day > state.floor
        ):
            incoming[record.day] = dict(record.values)
    changed_from: date | None = None
    for day, values in sorted(incoming.items()):
        if state.last_day is not None and day <= state.last_day:
            assert window_start is not None
            if day < window_start:
                known = state.digests.get(day)
                if known is not None and known != _digest(values):
                    state.old_changes += 1
                continue
            if state.recent.get(day) == values:
                continue
        if day not in state.recent:
            state.imported_days += 1
        state.recent[day] = values
        changed_from = day if changed_from is None else min(changed_from, day)
    if changed_from is None:
        return {}
    state.last_day = max(state.last_day or changed_from, max(state.recent))
    for day, values in sorted(state.recent.items()):
        for slug, value in values.items():
            key = KEY_BY_SLUG[slug]
            if value == 0:
                continue
            if key.kind == "gas":
                state.gas_seen = True
            if slug in state.active:
                continue
            if importable(key, state):
                state.active[slug] = _sum_before(state, slug, max(day, changed_from))
    rows = _rows(state, changed_from)
    _fold(state, today)
    return rows


def _sum_before(state: HistoryState, slug: str, day: date) -> float:
    """The sum of a statistic up to the day before ``day``."""
    return state.base.get(slug, 0.0) + sum(
        values.get(slug, 0.0) for when, values in state.recent.items() if when < day
    )


def _rows(state: HistoryState, start: date) -> Rows:
    rows: Rows = {}
    sums = {slug: _sum_before(state, slug, start) for slug in state.active}
    for day, values in sorted(state.recent.items()):
        if day < start:
            continue
        for slug, value in values.items():
            if slug not in state.active:
                continue
            if KEY_BY_SLUG[slug].kind == "temperature":
                rows.setdefault(slug, []).append((day, value))
                continue
            sums[slug] += value
            total = round(sums[slug] - state.active[slug], 6)
            if total >= 0:
                rows.setdefault(slug, []).append((day, total))
    return rows


def _fold(state: HistoryState, today: date) -> None:
    """Move days older than the recalculation window into ``base``; keep a
    digest of each for ``FIRST_FILL_DAYS``."""
    assert state.last_day is not None
    window_start = state.last_day - timedelta(days=RECOMPUTE_DAYS - 1)
    for day in sorted(state.recent):
        if day >= window_start:
            break
        values = state.recent.pop(day)
        state.digests[day] = _digest(values)
        for slug, value in values.items():
            if KEY_BY_SLUG[slug].kind != "temperature":
                state.base[slug] = state.base.get(slug, 0.0) + value
    oldest = today - timedelta(days=FIRST_FILL_DAYS)
    for day in [day for day in state.digests if day < oldest]:
        del state.digests[day]


def continue_from(
    state: HistoryState, last_rows: Mapping[str, tuple[date, float | None]]
) -> None:
    """Go on from the statistics already recorded, for an empty state.

    ``last_rows`` holds the last recorded row of each statistic: its day and
    its sum (None for the temperature). Every sum continues from its row,
    and no day up to the newest of these rows is written again, so a lost or
    new import state neither runs a sum backwards nor counts a day twice.
    """
    state.statistics_checked = True
    known = {slug: row for slug, row in last_rows.items() if slug in KEY_BY_SLUG}
    if not known:
        return
    floor = max(day for day, _total in known.values())
    state.floor = floor
    state.last_day = floor
    for slug, (_day_of_row, total) in known.items():
        if KEY_BY_SLUG[slug].kind != "temperature":
            finite = total is not None and math.isfinite(total)
            state.base[slug] = max(0.0, total) if total is not None and finite else 0.0
        state.active[slug] = 0.0


class HistorySource(Protocol):
    """The reads of a cycle; each is one request.

    ``page`` reads the history without a query for an empty mapping and
    raises ``HistoryRefused`` for HTTP 400 or 404.
    """

    async def entries(self) -> int | None: ...

    async def page(self, query: Mapping[str, str | int]) -> list[object]: ...

    async def gas_unit(self) -> str | None: ...

    async def heat_source_type(self) -> str | None: ...


@dataclass(slots=True)
class CycleResult:
    """What one cycle did, without values."""

    requests: int = 0
    reason: str | None = None
    retry_after: float | None = None
    # The next cycle may come later than the regular cadence.
    defer: timedelta | None = None


class _BudgetSpent(Exception):
    pass


class _HeatSourceUnknown(Exception):
    """An answer has older keys and the plant type could not be read yet."""


type _Call = Callable[..., Awaitable[object]]
type _Legacy = Callable[[list[object]], Awaitable[Legacy | None]]
type _Write = Callable[[Rows], Awaitable[None] | None]


async def run_cycle(
    state: HistoryState,
    source: HistorySource,
    *,
    today: date,
    now: datetime,
    firmware: str | None,
    write: _Write,
    max_requests: int = MAX_REQUESTS,
    heat_source_type: str | None = None,
) -> CycleResult:
    """Read new history days with at most ``max_requests`` requests.

    ``write`` gets the rows of every answer at once, so a failure later in
    the cycle keeps what was read before it. A refused query form switches
    once to the other form for the next cycle; a refused second form stops
    the reads until the firmware changes or seven days passed.

    ``heat_source_type`` is the plant type if already read; otherwise it is
    read once the first answer has an older key. An answer is only taken
    once the meaning of its older keys is known, so no value is lost or
    filed under the wrong statistic; a type answered with 5xx holds it for
    up to ``HEAT_SOURCE_WAIT``.
    """
    result = CycleResult()

    async def call[T](read: Callable[..., Awaitable[T]], *args: object) -> T:
        if result.requests >= max_requests:
            raise _BudgetSpent
        result.requests += 1
        return await read(*args)

    # VER-04: the plant type stayed unknown in this cycle after 5xx answers.
    unknown_type = False

    async def legacy_for(items: list[object]) -> Legacy | None:
        nonlocal unknown_type
        if not uses_legacy_keys(items):
            return None
        if heat_source_type is not None:
            state.heat_source = legacy_meaning(heat_source_type)
            state.heat_source_errors = 0
            state.heat_source_unreadable_since = None
        elif state.heat_source is None:
            if unknown_type:
                return None
            try:
                value = await call(source.heat_source_type)
            except ResourceError:
                value = None  # refused or missing: an unknown type
            except ServiceUnavailable, UnexpectedHttpStatus:
                # A 5xx may be a passing fault or a missing path (P31): the
                # answer waits on the controller, nothing of it is taken and
                # neither position nor cursor moves, and every cycle reads
                # the type again. Only after ``HEAT_SOURCE_WAIT`` (VER-04)
                # are the answers taken without their older keys, which are
                # then never guessed, and no type is stored, so the next
                # cycle still reads it again.
                state.heat_source_errors += 1
                since = state.heat_source_unreadable_since
                if since is None:
                    since = state.heat_source_unreadable_since = now
                if now - since < HEAT_SOURCE_WAIT:
                    raise _HeatSourceUnknown from None
                unknown_type = True
                return None
            state.heat_source_errors = 0
            state.heat_source_unreadable_since = None
            state.heat_source = legacy_meaning(value)
        return state.heat_source

    if firmware != state.firmware:
        # S10: another firmware may answer the other form; choose again.
        state.firmware = firmware
        state.mode = None
        state.switched = False
        state.unsupported = False
        state.heat_source = None
        state.heat_source_errors = 0
        state.heat_source_unreadable_since = None
    if state.mode is None:
        state.mode = preferred_mode(firmware)
    mode: Mode = state.mode
    state.cycles += 1
    state.reason = None
    old_changes = state.old_changes
    try:
        if (
            state.gas_checked_at is None
            or now - state.gas_checked_at >= GAS_UNIT_MAX_AGE
        ):
            try:
                unit = await call(source.gas_unit)
            except ResourceError, ServiceUnavailable, UnexpectedHttpStatus:
                unit = None  # no gas unit: gas stays out (also on HTTP 500)
            state.gas_checked_at = now
            state.gas_unit_confirmed = (
                isinstance(unit, str) and unit.strip().casefold() == "kwh"
            )
        if mode == "entry":
            await _entry_pages(state, source, call, legacy_for, today, write)
        else:
            await _date_pages(state, source, call, legacy_for, today, write)
        state.unsupported = False
        state.server_errors = 0
    except _BudgetSpent:
        state.server_errors = 0
        state.reason = "request_budget_reached"
    except _HeatSourceUnknown:
        state.server_errors = 0
        state.reason = "heat_source_type_unavailable"
    except HistoryRefused:
        state.server_errors = 0
        _refused(state, mode, result)
    except RateLimited as err:
        state.reason = "rate_limited"
        result.retry_after = err.retry_after
    except ServiceUnavailable as err:
        state.server_errors += 1
        if state.server_errors >= MAX_SERVER_ERROR_CYCLES:
            state.server_errors = 0
            _refused(state, mode, result)
        else:
            status = err.status
            state.reason = f"http_{status}" if status else "service_unavailable"
    except ResourceError as err:
        state.reason = f"http_{err.status}"
        result.defer = timedelta(hours=24)
    except PointTError as err:
        state.reason = _error_token(err)
    if state.reason is None and unknown_type:
        state.reason = "legacy_keys_ambiguous"
    if state.reason is None and state.old_changes > old_changes:
        state.reason = "history_change_outside_window"
    if state.reason is None and state.gas_unit_confirmed is not True and state.gas_seen:
        state.reason = "gas_unit_unconfirmed"
    state.last_requests = result.requests
    result.reason = state.reason
    return result


def _refused(state: HistoryState, mode: Mode, result: CycleResult) -> None:
    """The chosen form was refused: switch once, then stop for a while."""
    if state.switched:
        state.unsupported = True
        state.reason = "history_unsupported"
        result.defer = timedelta(days=7)
    else:
        state.mode = _other(mode)
        state.switched = True
        state.reason = "history_form_switched"


def _error_token(err: PointTError) -> str:
    name = type(err).__name__
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


async def _write(write: _Write, rows: Rows) -> None:
    if rows:
        pending = write(rows)
        if pending is not None:
            await pending


def _running(items: list[object], today: date) -> int | None:
    """The index of the first entry of the running day (or later), if any."""
    for index, item in enumerate(items):
        day = parse_day(item.get("d")) if isinstance(item, dict) else None
        if day is not None and day >= today:
            return index
    return None


async def _entry_pages(
    state: HistoryState,
    source: HistorySource,
    call: _Call,
    legacy_for: _Legacy,
    today: date,
    write: _Write,
) -> None:
    """By entry (firmware before 5.3.0): ``?entry=N`` from index N on.

    The entry of the last known day is read again first. It shows whether
    the indices moved: a full history keeps its number of entries and drops
    its oldest day, so the same index then holds a later day; the reads step
    back until they reach the last known day. The running day is never
    passed: its entry is read again by the next cycle.
    """
    entries = await call(source.entries)
    if not isinstance(entries, int) or isinstance(entries, bool) or entries < 0:
        state.reason = "history_entries_invalid"
        return
    position = state.position
    if position is None:
        start = max(0, entries - FIRST_FILL_DAYS)
    elif entries < position:
        # The counter went back (reset): read again from the last known day
        # on; days already imported are recognised by their date.
        behind = (today - state.last_day).days + 1 if state.last_day else 0
        start = max(0, entries - behind - 1) if state.last_day else 0
    elif state.last_day is not None and position > 0:
        start = position - 1
    else:
        start = position
    state.entries = entries
    aligning = state.last_day is not None
    while start < entries:
        items = await call(source.page, {"entry": start})
        assert isinstance(items, list)
        if not items:
            break
        records, invalid = parse_records(items, await legacy_for(items))
        first = min((record.day for record in records), default=None)
        last_day = state.last_day
        if (
            aligning
            and start > 0
            and last_day is not None
            and first is not None
            and first > last_day
        ):
            # The entries moved: step back by the days in between (with
            # gaps that reaches back further, which costs no data).
            start = max(0, start - (first - last_day).days)
            continue
        aligning = False
        state.invalid_records += invalid
        await _write(write, merge(state, records, today))
        if (running := _running(items, today)) is not None:
            state.position = start + running
            return
        start += len(items)
        state.position = start


async def _date_pages(
    state: HistoryState,
    source: HistorySource,
    call: _Call,
    legacy_for: _Legacy,
    today: date,
    write: _Write,
) -> None:
    """By date (firmware 5.3.0 and later).

    Without a known day the first answer comes without a query (the oldest
    days); after that every query names a day the controller is known to
    have, so a day without a record never stops the import. A first filling
    whose oldest days are beyond ``FIRST_FILL_DAYS`` tries once to jump to
    that limit. If the known day is gone (the history started again), the
    reads begin anew without a query; days already imported are recognised
    by their date.
    """
    oldest = today - timedelta(days=FIRST_FILL_DAYS)
    anchor = state.cursor or state.last_day
    jumped = probed = False
    while True:
        query: dict[str, str | int]
        if anchor is None:
            if probed:
                return
            probed = True
            kind, query = "start", {}
        elif anchor < oldest and not jumped:
            jumped = True
            kind, query = "jump", {"date": oldest.isoformat()}
        else:
            kind, query = "known", {"date": anchor.isoformat()}
        items = await call(source.page, query)
        assert isinstance(items, list)
        records, invalid = parse_records(items, await legacy_for(items))
        newest = max(
            (record.day for record in records if record.day < today), default=None
        )
        progressed = newest is not None and (anchor is None or newest > anchor)
        if kind == "jump" and not progressed:
            continue  # nothing from the limit on: step on from the known day
        state.invalid_records += invalid
        await _write(write, merge(state, records, today))
        if progressed:
            assert newest is not None
            anchor = state.cursor = newest
            if _running(items, today) is not None:
                return  # the answer reached the running day
            continue
        if (
            kind == "known"
            and not probed
            and all(record.day != anchor for record in records)
        ):
            anchor = None  # the known day is gone: start again
            continue
        return
