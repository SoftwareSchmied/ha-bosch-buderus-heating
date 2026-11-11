"""W5 (CT-G05): the energy history of an RRC 2.0 room controller, read in
bounded cycles (``rrc_energy``, without Home Assistant).

The records come from ``energy.json`` (synthetic, demo shapes). Everything
is expected, untested on real hardware.
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.bosch_buderus_heating import rrc_energy
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    RequestTimeout,
    ResourceNotFound,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.rrc_energy import (
    FIRST_FILL_DAYS,
    HEAT_SOURCE_TYPE_PATH,
    MAX_REQUESTS,
    HistoryRefused,
    HistoryState,
    continue_from,
    legacy_meaning,
    merge,
    parse_day,
    parse_records,
    preferred_mode,
    run_cycle,
    uses_legacy_keys,
)

FIXTURE = Path(__file__).parent / "fixtures" / "contracts" / "rrc" / "energy.json"
TODAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
NEW_FIRMWARE = "05.04.00"
OLD_FIRMWARE = "05.02.07"


def fixture() -> dict[str, object]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["provenance"]["physical_device_verified"] is False
    return data


def records() -> list[dict[str, object]]:
    return list(fixture()["records"])  # type: ignore[arg-type]


def day_record(day: date, **values: float) -> dict[str, object]:
    return {"d": day.strftime("%d-%m-%Y"), "T": 5.0, "eCh": 1.0, **values}


class Source:
    """Answers the history like the controller: ``?entry=N`` from index N
    on, ``?date=`` from that day on (``exact``: only from a day that has a
    record, else an empty list), no query from the first record on,
    ``page`` records per answer."""

    def __init__(
        self,
        items: list[dict[str, object]],
        *,
        page: int = 4,
        gas_unit: str | None = "kWh",
        refuse: frozenset[str] = frozenset(),
        heat_source: str | None = "Heatpump",
        exact: bool = False,
    ) -> None:
        self.items = items
        self.page_size = page
        self.unit = gas_unit
        self.refuse = set(refuse)
        self.heat_source = heat_source
        self.exact = exact
        self.entries_value: int | None = None
        self.calls: list[tuple[str, object]] = []
        # Call number (1-based) -> error raised instead of answering.
        self.errors: dict[int, Exception] = {}
        self.unit_error: Exception | None = None
        self.type_error: Exception | None = None

    def _call(self, name: str, detail: object = None) -> None:
        self.calls.append((name, detail))
        if (error := self.errors.get(len(self.calls))) is not None:
            raise error

    async def entries(self) -> int | None:
        self._call("entries")
        return len(self.items) if self.entries_value is None else self.entries_value

    async def page(self, query: Mapping[str, str | int]) -> list[object]:
        self._call("page", dict(query))
        form = next(iter(query), "date")
        if form in self.refuse:
            raise HistoryRefused("HTTP 404")
        if form == "entry":
            start = int(query["entry"])
            return list(self.items[start : start + self.page_size])
        if not query:
            return list(self.items[: self.page_size])
        first = date.fromisoformat(str(query["date"]))
        days = [parse_day(item.get("d")) for item in self.items]
        if self.exact and first not in days:
            return []
        later = [
            item
            for item, day in zip(self.items, days, strict=True)
            if day is not None and day >= first
        ]
        return list(later[: self.page_size])

    async def gas_unit(self) -> str | None:
        self._call("gas_unit")
        if self.unit_error is not None:
            raise self.unit_error
        return self.unit

    async def heat_source_type(self) -> str | None:
        self._call("heat_source_type")
        if self.type_error is not None:
            raise self.type_error
        return self.heat_source

    def queries(self) -> list[object]:
        return [detail for name, detail in self.calls if name == "page"]


class Sink:
    """Collects the written rows: slug -> day -> value (later rows win)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[date, float]] = {}
        self.writes = 0

    def __call__(self, rows: rrc_energy.Rows) -> None:
        self.writes += 1
        for slug, items in rows.items():
            for day, value in items:
                self.rows.setdefault(slug, {})[day] = value


async def cycle(
    state: HistoryState,
    source: Source,
    sink: Sink,
    *,
    firmware: str | None = OLD_FIRMWARE,
    today: date = TODAY,
    now: datetime = NOW,
    heat_source_type: str | None = None,
) -> rrc_energy.CycleResult:
    return await run_cycle(
        state,
        source,
        today=today,
        now=now,
        firmware=firmware,
        write=sink,
        heat_source_type=heat_source_type,
    )


def parsed() -> list[rrc_energy.DayRecord]:
    """The fixture records of a heat pump plant (older keys = electricity)."""
    return parse_records(records(), "electricity")[0]


# ----------------------------------------------------------------- records
def test_records_keep_valid_days_and_numbers_only() -> None:
    read, invalid = parse_records(records(), "electricity")
    days = [record.day for record in read]
    # "$DATE$" and 31 February are no days.
    assert invalid == 2
    assert date(2026, 10, 4) not in days  # a gap stays a gap
    by_day = {record.day: record.values for record in read}
    # Older firmware spells two keys differently; on a heat pump plant they
    # are the heat pump's electricity.
    assert by_day[date(2026, 10, 6)]["heat_pump_electricity_heating"] == 5.5
    assert by_day[date(2026, 10, 6)]["heat_pump_electricity_hot_water"] == 1.5
    # The key itself wins over the gas meaning of the older key.
    assert by_day[date(2026, 10, 6)]["gas_heating"] == 6.5
    # A number as a string counts, a negative amount never.
    assert by_day[date(2026, 10, 8)]["heat_pump_electricity_hot_water"] == 1.0
    assert "electric_heater_heating" not in by_day[date(2026, 10, 8)]
    assert by_day[date(2026, 10, 7)]["outdoor_temperature"] in (-2.0, -2.5)
    assert parse_day("07-10-2026") == date(2026, 10, 7)
    for text in ("2026-10-07", "7-10-2026", "07-13-2026", None, 7):
        assert parse_day(text) is None


@pytest.mark.parametrize(
    ("firmware", "mode"),
    [
        ("05.03.00", "date"),
        ("05.04.00", "date"),
        ("5.10.1", "date"),
        ("05.02.99", "entry"),
        ("04.09.00", "entry"),
        (None, "entry"),
        ("unknown", "entry"),
    ],
)
def test_the_query_form_follows_the_firmware(firmware, mode) -> None:
    assert preferred_mode(firmware) == mode


# --------------------------------------------------------------- merging
def test_sums_run_on_from_the_first_value_and_skip_zero_keys() -> None:
    state = HistoryState(gas_unit_confirmed=True)
    rows = merge(state, parsed(), TODAY)
    heating = dict(rows["heat_pump_electricity_heating"])
    # 3 + 3.5 + 4 + 5 + 5.5 + 6.5 (the later 7 October wins) + 4.5 + 4 + 3.
    assert heating[date(2026, 10, 10)] == pytest.approx(39.0)
    assert heating[date(2026, 10, 7)] == pytest.approx(27.5)
    assert date(2026, 10, 4) not in heating
    # Keys that are zero in every record become no statistic.
    for slug in ("heat_pump_electricity_cooling", "boiler_heat_heating"):
        assert slug not in rows
        assert slug not in state.active
    # The temperature is a daily mean, not a sum.
    temperature = dict(rows["outdoor_temperature"])
    assert temperature[date(2026, 10, 7)] == -2.5
    assert temperature[date(2026, 10, 10)] == 8.5
    # A statistic that starts later starts at zero.
    heater = dict(rows["electric_heater_heating"])
    assert heater[date(2026, 10, 1)] == 0.0
    assert heater[date(2026, 10, 2)] == pytest.approx(0.2)
    assert state.last_day == date(2026, 10, 10)
    assert state.imported_days == 9


def test_today_and_days_beyond_the_first_filling_are_left_out() -> None:
    state = HistoryState()
    old = TODAY - timedelta(days=FIRST_FILL_DAYS + 1)
    parsed, _ = parse_records(
        [day_record(old), day_record(TODAY - timedelta(days=1)), day_record(TODAY)]
    )
    rows = merge(state, parsed, TODAY)
    assert list(dict(rows["heat_pump_electricity_heating"])) == [
        TODAY - timedelta(days=1)
    ]
    assert state.last_day == TODAY - timedelta(days=1)


def test_a_changed_recent_day_recalculates_the_later_sums() -> None:
    state = HistoryState()
    merge(state, parsed(), TODAY)
    changed, _ = parse_records([fixture()["changed_record"]])
    rows = merge(state, changed, TODAY)
    heating = dict(rows["heat_pump_electricity_heating"])
    # 9 October grew from 4 to 6 kWh: it and every later day move by 2.
    assert heating == {
        date(2026, 10, 9): pytest.approx(38.0),
        date(2026, 10, 10): pytest.approx(41.0),
    }
    # An unchanged answer writes nothing.
    assert merge(state, changed, TODAY) == {}


def test_a_change_older_than_the_window_is_counted_and_left_alone() -> None:
    state = HistoryState()
    first = TODAY - timedelta(days=60)
    days = [first + timedelta(days=offset) for offset in range(59)]
    merge(state, parse_records([day_record(day) for day in days])[0], TODAY)
    assert len(state.recent) == rrc_energy.RECOMPUTE_DAYS
    assert first in state.digests
    before = dict(state.base)
    rows = merge(state, parse_records([day_record(first, eCh=9.0)])[0], TODAY)
    assert rows == {}
    assert state.old_changes == 1
    assert state.base == before
    # The same old day unchanged is no change.
    merge(state, parse_records([day_record(first)])[0], TODAY)
    assert state.old_changes == 1


def test_gas_needs_a_confirmed_unit() -> None:
    unconfirmed = HistoryState(gas_unit_confirmed=False)
    rows = merge(unconfirmed, parsed(), TODAY)
    assert "gas_heating" not in rows
    assert unconfirmed.gas_seen is True
    confirmed = HistoryState(gas_unit_confirmed=True)
    rows = merge(confirmed, parsed(), TODAY)
    assert dict(rows["gas_heating"])[date(2026, 10, 1)] == 4.0


def test_the_stored_state_survives_and_bad_data_starts_over() -> None:
    state = HistoryState(
        mode="entry",
        position=7,
        gas_unit_confirmed=True,
        cursor=date(2026, 10, 9),
        floor=date(2026, 9, 30),
        statistics_checked=True,
        heat_source="gas",
        heat_source_errors=1,
    )
    merge(state, parsed(), TODAY)
    stored = json.loads(json.dumps(state.to_dict()))
    assert HistoryState.from_dict(stored) == state
    for broken in (None, [], {"version": 99}, {"version": 1, "recent": {"x": 1}}):
        assert HistoryState.from_dict(broken) == HistoryState()
    values = {
        **stored,
        "position": -3,
        "mode": "other",
        "base": {"x": 1},
        "cursor": "x",
        "heat_source": "oil",
        "statistics_checked": "yes",
    }
    restored = HistoryState.from_dict(values)
    assert restored.position is None
    assert restored.mode is None
    assert restored.base == {}
    assert restored.cursor is None
    assert restored.heat_source is None
    assert restored.statistics_checked is False
    # A state stored before these fields existed is read as it was.
    added = ("cursor", "floor", "statistics_checked", "heat_source")
    older = {
        key: value
        for key, value in stored.items()
        if key not in added and key != "heat_source_errors"
    }
    assert HistoryState.from_dict(older).last_day == state.last_day
    # Values without their last day cannot be continued.
    assert HistoryState.from_dict({**stored, "last_day": None}) == HistoryState()


# ----------------------------------------------------------------- cycles
async def test_a_first_filling_by_entry_spends_at_most_eight_requests() -> None:
    first = TODAY - timedelta(days=40)
    items = [day_record(first + timedelta(days=n)) for n in range(40)]
    source = Source(items, page=3)
    state = HistoryState()
    sink = Sink()
    result = await cycle(state, source, sink)
    assert result.requests == len(source.calls) == MAX_REQUESTS
    assert result.reason == "request_budget_reached"
    # The gas unit, the number of entries and six pages.
    assert [name for name, _ in source.calls[:2]] == ["gas_unit", "entries"]
    assert source.queries() == [{"entry": n} for n in range(0, 18, 3)]
    assert state.position == 18
    # The next cycles go on where this one stopped, without the gas unit.
    while state.last_day != TODAY - timedelta(days=1):
        source.calls.clear()
        result = await cycle(state, source, sink)
        assert result.requests <= MAX_REQUESTS
        assert ("gas_unit", None) not in source.calls
    sums = sink.rows["heat_pump_electricity_heating"]
    assert sums[TODAY - timedelta(days=1)] == pytest.approx(40.0)
    assert state.imported_days == 40


async def test_a_first_filling_reaches_back_at_most_400_entries() -> None:
    source = Source([], page=3)
    source.entries_value = 995
    state = HistoryState(gas_checked_at=NOW)
    await cycle(state, source, Sink())
    assert source.queries()[0] == {"entry": 995 - FIRST_FILL_DAYS}


async def test_new_firmware_reads_by_date_from_the_last_known_day() -> None:
    source = Source(records(), page=4)
    state = HistoryState()
    sink = Sink()
    result = await cycle(state, source, sink, firmware=NEW_FIRMWARE)
    assert result.reason is None
    # SYSTEM-04: the first answer comes without a query, every later query
    # names the newest day of the answer before, which surely exists.
    assert source.queries() == [
        {},
        {"date": "2026-10-05"},
        {"date": "2026-10-07"},
        {"date": "2026-10-09"},
        {"date": "2026-10-10"},
    ]
    assert state.mode == "date"
    assert state.last_day == state.cursor == date(2026, 10, 10)
    assert "entries" not in [name for name, _ in source.calls]
    heating = sink.rows["heat_pump_electricity_heating"]
    assert heating[date(2026, 10, 10)] == pytest.approx(39.0)
    source.calls.clear()
    await cycle(state, source, sink, firmware=NEW_FIRMWARE)
    assert source.queries() == [{"date": "2026-10-10"}]


async def test_a_refused_form_switches_once_and_never_back() -> None:
    source = Source(records(), refuse=frozenset({"date"}))
    state = HistoryState()
    result = await cycle(state, source, Sink(), firmware=NEW_FIRMWARE)
    assert result.reason == "history_form_switched"
    assert state.mode == "entry"
    assert state.switched
    source.calls.clear()
    result = await cycle(state, source, Sink(), firmware=NEW_FIRMWARE)
    assert result.reason is None
    assert all("entry" in query for query in source.queries())
    # Both forms refused: the reads stop for a week, no ping-pong.
    source.refuse = {"date", "entry"}
    state.position = 0
    source.calls.clear()
    result = await cycle(state, source, Sink(), firmware=NEW_FIRMWARE)
    assert result.reason == "history_unsupported"
    assert result.defer == timedelta(days=7)
    assert state.mode == "entry"
    assert state.unsupported
    # A firmware update chooses again.
    source.refuse = set()
    source.calls.clear()
    result = await cycle(state, source, Sink(), firmware="05.05.00")
    assert state.mode == "date" and not state.switched and not state.unsupported


async def test_a_rate_limit_while_paging_keeps_what_was_read() -> None:
    first = TODAY - timedelta(days=20)
    items = [day_record(first + timedelta(days=n)) for n in range(20)]
    source = Source(items, page=3)
    # gas unit, entries, two pages, then 429.
    source.errors[5] = RateLimited(retry_after=900)
    state = HistoryState()
    sink = Sink()
    result = await cycle(state, source, sink)
    assert result.reason == "rate_limited"
    assert result.retry_after == 900
    assert result.requests == 5
    assert state.position == 6
    assert len(sink.rows["heat_pump_electricity_heating"]) == 6
    source.errors.clear()
    source.calls.clear()
    await cycle(state, source, sink)
    # The entry of the last known day is read again first.
    assert source.queries()[0] == {"entry": 5}
    heating = sink.rows["heat_pump_electricity_heating"]
    assert heating[TODAY - timedelta(days=1)] == pytest.approx(20.0)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RequestTimeout("timeout"), "request_timeout"),
        (ServiceUnavailable(503), "http_503"),
        (ResourceNotFound("/energy/historyEntries", 404), "http_404"),
    ],
)
async def test_a_failed_read_ends_the_cycle_and_keeps_the_state(error, reason) -> None:
    source = Source(records())
    source.errors[2] = error  # the number of entries
    state = HistoryState()
    result = await cycle(state, source, Sink())
    assert result.reason == reason
    assert state.position is None
    assert state.last_day is None


async def test_server_errors_in_three_cycles_count_as_a_refusal() -> None:
    source = Source(records())
    state = HistoryState(gas_checked_at=NOW)
    for expected in ("http_500", "http_500", "history_form_switched"):
        source.calls.clear()
        source.errors[1] = ServiceUnavailable(500)
        result = await cycle(state, source, Sink())
        assert result.reason == expected
    assert state.mode == "date"
    assert state.server_errors == 0


async def test_a_counter_that_went_back_reads_again_from_the_last_day() -> None:
    first = TODAY - timedelta(days=12)
    items = [day_record(first + timedelta(days=n)) for n in range(10)]
    source = Source(items, page=10)
    state = HistoryState()
    sink = Sink()
    await cycle(state, source, sink)
    before = sink.rows["heat_pump_electricity_heating"][state.last_day]
    # The controller starts its list again with two new days.
    source.items = [
        day_record(first + timedelta(days=9)),
        day_record(first + timedelta(days=10), eCh=2.0),
        day_record(first + timedelta(days=11), eCh=3.0),
    ]
    source.calls.clear()
    await cycle(state, source, sink)
    assert source.queries()[0] == {"entry": 0}
    sums = sink.rows["heat_pump_electricity_heating"]
    assert sums[first + timedelta(days=11)] == pytest.approx(before + 5.0)
    assert all(
        later >= earlier
        for earlier, later in zip(
            list(sums.values()), list(sums.values())[1:], strict=False
        )
    )


async def test_gas_unit_m3_leaves_gas_out_with_a_reason() -> None:
    source = Source(records(), gas_unit=fixture()["gas_units"]["unconfirmed"])
    state = HistoryState()
    sink = Sink()
    result = await cycle(state, source, sink)
    assert result.reason == "gas_unit_unconfirmed"
    assert "gas_heating" not in sink.rows
    assert "heat_pump_electricity_heating" in sink.rows


async def test_a_missing_gas_unit_does_not_stop_the_cycle() -> None:
    source = Source(records())
    source.unit_error = ServiceUnavailable(500)
    state = HistoryState()
    sink = Sink()
    result = await cycle(state, source, sink)
    assert state.gas_unit_confirmed is False
    assert result.reason == "gas_unit_unconfirmed"
    assert state.last_day == date(2026, 10, 10)
    # A rate limit there ends the cycle like everywhere else.
    source.unit_error = RateLimited(retry_after=None)
    state = HistoryState()
    result = await cycle(state, source, Sink())
    assert result.reason == "rate_limited"
    assert state.gas_checked_at is None


async def test_the_gas_unit_is_read_again_after_a_day() -> None:
    source = Source([], gas_unit="kWh")
    state = HistoryState()
    await cycle(state, source, Sink())
    await cycle(state, source, Sink(), now=NOW + timedelta(hours=23))
    assert [name for name, _ in source.calls].count("gas_unit") == 1
    await cycle(state, source, Sink(), now=NOW + timedelta(hours=24))
    assert [name for name, _ in source.calls].count("gas_unit") == 2


async def test_an_old_change_gives_its_reason() -> None:
    first = TODAY - timedelta(days=60)
    items = [day_record(first + timedelta(days=n)) for n in range(59)]
    source = Source(items, page=100)
    state = HistoryState()
    await cycle(state, source, Sink())
    source.items = [day_record(first, eCh=7.0)]
    state.position = 0
    result = await cycle(state, source, Sink())
    assert result.reason == "history_change_outside_window"


# ------------------------------------------------- deep review (SYSTEM-01..05)
def at(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=9)


def legacy_record(day: date, **values: float) -> dict[str, object]:
    """A record of older firmware: ``eCH``/``eHW`` instead of the keys."""
    return {"d": day.strftime("%d-%m-%Y"), "T": 5.0, "eCH": 3.0, "eHW": 1.0, **values}


def ascending(sums: dict[date, float]) -> bool:
    values = [sums[day] for day in sorted(sums)]
    return all(later >= earlier for earlier, later in itertools.pairwise(values))


async def test_by_entry_the_running_day_is_read_again_once_complete() -> None:
    """SYSTEM-01: the controller lists the running day as its last entry."""
    first = date(2026, 10, 15)
    items = [day_record(first + timedelta(days=n)) for n in range(4)]  # 15.-18.
    items.append(day_record(date(2026, 10, 19), eCh=0.5))  # running
    state, source, sink = HistoryState(), Source(items), Sink()
    day = date(2026, 10, 19)
    await cycle(state, source, sink, today=day, now=at(day))
    assert state.last_day == date(2026, 10, 18)
    assert state.position == 4  # the entry of the running day
    # A day later the 19th is complete and the 20th runs.
    items[-1] = day_record(date(2026, 10, 19), eCh=3.0)
    items.append(day_record(date(2026, 10, 20), eCh=0.2))
    source.calls.clear()
    day = date(2026, 10, 20)
    await cycle(state, source, sink, today=day, now=at(day))
    assert source.queries() == [{"entry": 3}]
    heating = sink.rows["heat_pump_electricity_heating"]
    assert heating[date(2026, 10, 19)] == pytest.approx(7.0)
    assert date(2026, 10, 20) not in heating
    assert state.last_day == date(2026, 10, 19)
    assert state.position == 5
    assert ascending(heating)


async def test_by_entry_a_full_history_is_followed_day_by_day() -> None:
    """SYSTEM-03: a full history keeps its number of entries and drops its
    oldest day, so the same index holds a later day each day."""
    first = date(2026, 9, 1)
    items = [day_record(first + timedelta(days=n)) for n in range(30)]
    state, source, sink = HistoryState(), Source(items, page=40), Sink()
    day = date(2026, 10, 1)
    await cycle(state, source, sink, today=day, now=at(day))
    assert state.last_day == date(2026, 9, 30)
    source.items = [*items[1:], day_record(date(2026, 10, 1), eCh=2.0)]
    assert len(source.items) == 30
    day = date(2026, 10, 2)
    result = await cycle(state, source, sink, today=day, now=at(day))
    assert result.reason is None
    heating = sink.rows["heat_pump_electricity_heating"]
    assert state.last_day == date(2026, 10, 1)
    assert heating[date(2026, 10, 1)] == pytest.approx(32.0)
    # Three days later three more dropped out; still nothing is lost.
    later = [day_record(date(2026, 10, 2) + timedelta(days=n)) for n in range(3)]
    source.items = [*source.items[3:], *later]
    assert len(source.items) == 30
    day = date(2026, 10, 5)
    await cycle(state, source, sink, today=day, now=at(day))
    assert state.last_day == date(2026, 10, 4)
    assert heating[date(2026, 10, 4)] == pytest.approx(35.0)
    assert sorted(heating)[-4:] == [date(2026, 10, n) for n in range(1, 5)]
    assert ascending(heating)


@pytest.mark.parametrize(
    ("heat_source", "meaning"),
    [
        ("Heatpump", "electricity"),
        ("Hybrid", "electricity"),
        ("Hybrid_Gas", "electricity"),
        ("Hybrid_Oil", "electricity"),
        ("Boiler", "gas"),
        ("No_Applicance", "gas"),
        ("GasBoiler", "gas"),
        ("Unknown", "gas"),
        ("", "gas"),
        (None, "gas"),
        (3, "gas"),
    ],
)
def test_older_keys_follow_the_heat_source_type(heat_source, meaning) -> None:
    assert legacy_meaning(heat_source) == meaning


def test_older_keys_on_a_boiler_are_gas_never_heat_pump_electricity() -> None:
    """SYSTEM-02: ``eCH``/``eHW`` are gas on a boiler, no appliance or an
    unknown type; without a known meaning they are left out."""
    item = {"d": "01-10-2026", "T": 5.0, "eCH": 7.0, "eHW": 2.0}
    (gas,), _ = parse_records([item], "gas")
    assert dict(gas.values) == {
        "outdoor_temperature": 5.0,
        "gas_heating": 7.0,
        "gas_hot_water": 2.0,
    }
    (electric,), _ = parse_records([item], "electricity")
    assert dict(electric.values) == {
        "outdoor_temperature": 5.0,
        "heat_pump_electricity_heating": 7.0,
        "heat_pump_electricity_hot_water": 2.0,
    }
    (unknown,), _ = parse_records([item])
    assert dict(unknown.values) == {"outdoor_temperature": 5.0}
    assert uses_legacy_keys([item])
    assert not uses_legacy_keys([day_record(TODAY), "x", {"eCH": None}])


@pytest.mark.parametrize(("unit", "imported"), [("m3", False), ("kWh", True)])
async def test_a_boiler_with_older_keys_imports_gas_only_with_its_unit(
    unit, imported
) -> None:
    first = TODAY - timedelta(days=5)
    items = [legacy_record(first + timedelta(days=n)) for n in range(5)]
    source = Source(items, heat_source="Boiler", gas_unit=unit)
    state, sink = HistoryState(), Sink()
    result = await cycle(state, source, sink)
    assert state.heat_source == "gas"
    assert not [slug for slug in sink.rows if slug.startswith("heat_pump")]
    assert ("gas_heating" in sink.rows) is imported
    if imported:
        assert result.reason is None
        assert sink.rows["gas_heating"][TODAY - timedelta(days=1)] == 15.0
    else:
        assert result.reason == "gas_unit_unconfirmed"
    assert state.last_day == TODAY - timedelta(days=1)
    # The type is read once, not in every cycle.
    await cycle(state, source, sink)
    assert [name for name, _ in source.calls].count("heat_source_type") == 1


async def test_the_heat_source_type_is_read_only_for_older_keys() -> None:
    source = Source([day_record(TODAY - timedelta(days=1))])
    await cycle(HistoryState(), source, Sink())
    assert "heat_source_type" not in [name for name, _ in source.calls]
    # A type the coordinator already read costs no request.
    source = Source([legacy_record(TODAY - timedelta(days=1))], heat_source=None)
    sink = Sink()
    await cycle(HistoryState(), source, sink, heat_source_type="Heatpump")
    assert "heat_source_type" not in [name for name, _ in source.calls]
    assert sink.rows["heat_pump_electricity_heating"] == {
        TODAY - timedelta(days=1): 3.0
    }


async def test_an_unreadable_heat_source_type_keeps_the_answer_for_later() -> None:
    first = TODAY - timedelta(days=3)
    items = [legacy_record(first + timedelta(days=n)) for n in range(3)]
    source = Source(items)
    source.type_error = ServiceUnavailable(500)
    state, sink = HistoryState(), Sink()
    for _ in range(2):
        result = await cycle(state, source, sink)
        assert result.reason == "heat_source_type_unavailable"
        # Nothing taken and nothing passed: the page is read again.
        assert sink.rows == {}
        assert state.last_day is None and state.position is None
        assert state.server_errors == 0 and not state.switched
    # VER-04: from the third 5xx in a row the answers are taken without
    # their older keys, never as gas or heat pump electricity, and no type
    # is stored; one read of the type per cycle, not per page.
    source.page_size = 1
    result = await cycle(state, source, sink)
    assert result.reason == "legacy_keys_ambiguous"
    assert state.heat_source is None
    assert set(sink.rows) == {"outdoor_temperature"}
    assert state.last_day == TODAY - timedelta(days=1)
    assert [name for name, _ in source.calls].count("heat_source_type") == 3
    # The next cycle reads the type again; once it answers, it is stored
    # and the older keys follow it from the last known day on (read again).
    source.items.append(legacy_record(TODAY))
    source.type_error = None
    await cycle(state, source, sink, today=TODAY + timedelta(days=1))
    assert [name for name, _ in source.calls].count("heat_source_type") == 4
    assert state.heat_source == "electricity" and state.heat_source_errors == 0
    assert sink.rows["heat_pump_electricity_heating"] == {
        TODAY - timedelta(days=1): 3.0,
        TODAY: 6.0,
    }
    assert "gas_heating" not in sink.rows
    # A refused path is an unknown type at once.
    source = Source(items)
    source.type_error = ResourceNotFound(HEAT_SOURCE_TYPE_PATH, 404)
    state = HistoryState()
    await cycle(state, source, Sink())
    assert state.heat_source == "gas"
    # A rate limit there ends the cycle like everywhere else.
    source.type_error = RateLimited(retry_after=60)
    state = HistoryState()
    result = await cycle(state, source, Sink())
    assert result.reason == "rate_limited"
    assert state.heat_source is None and state.last_day is None
    # A firmware update reads the type again.
    state.heat_source = "gas"
    source.type_error = None
    await cycle(state, source, Sink(), firmware="05.02.08")
    assert state.heat_source == "electricity"


async def test_by_date_a_day_without_a_record_never_stops_the_import() -> None:
    """SYSTEM-04: the controller answers a day without a record with an
    empty list; every query names a day it has."""
    offsets = (0, 1, 2, 5, 6, 9, 10, 11)
    days = [date(2026, 10, 1) + timedelta(days=n) for n in offsets]
    items = [day_record(day) for day in days]
    source = Source(items, page=3, exact=True)
    state, sink = HistoryState(), Sink()
    result = await cycle(state, source, sink, firmware=NEW_FIRMWARE)
    assert result.reason is None
    queries = source.queries()
    assert queries[0] == {}
    assert all(date.fromisoformat(str(q["date"])) in days for q in queries[1:])
    heating = sink.rows["heat_pump_electricity_heating"]
    assert sorted(heating) == days
    assert heating[days[-1]] == pytest.approx(8.0)
    # Days after another gap come with the next cycle.
    source.items = [
        *items,
        day_record(date(2026, 10, 15)),
        day_record(date(2026, 10, 16)),
    ]
    source.calls.clear()
    await cycle(state, source, sink, firmware=NEW_FIRMWARE)
    assert source.queries()[0] == {"date": "2026-10-12"}
    assert state.last_day == date(2026, 10, 16)
    assert heating[date(2026, 10, 16)] == pytest.approx(10.0)


@pytest.mark.parametrize("exact", [False, True])
async def test_by_date_a_long_history_starts_at_the_first_filling_limit(
    exact,
) -> None:
    oldest = TODAY - timedelta(days=FIRST_FILL_DAYS)
    first = oldest - timedelta(days=30)
    items = [day_record(first + timedelta(days=n)) for n in range(33)]
    if exact:
        # The limit itself has no record: the jump finds nothing.
        items = [item for item in items if parse_day(item["d"]) != oldest]
    source = Source(items, page=5, exact=exact)
    state, sink = HistoryState(gas_checked_at=NOW), Sink()
    await cycle(state, source, sink, firmware=NEW_FIRMWARE)
    assert source.queries()[:2] == [{}, {"date": oldest.isoformat()}]
    if not exact:
        assert len(source.queries()) == 3
    # The remaining cycles step on from the known day, never back.
    cursors = [state.cursor]
    while state.last_day != oldest + timedelta(days=2):
        assert len(cursors) < 6
        await cycle(state, source, sink, firmware=NEW_FIRMWARE)
        cursors.append(state.cursor)
    assert cursors == sorted(cursors)
    heating = sink.rows["heat_pump_electricity_heating"]
    assert min(heating) >= oldest
    assert ascending(heating)


async def test_by_date_a_history_that_started_again_is_read_from_its_start() -> None:
    items = [day_record(date(2026, 10, 1) + timedelta(days=n)) for n in range(5)]
    source = Source(items, page=10, exact=True)
    state, sink = HistoryState(), Sink()
    await cycle(state, source, sink, firmware=NEW_FIRMWARE)
    assert state.cursor == date(2026, 10, 5)
    # The controller lost its history and starts again on 8 October.
    source.items = [
        day_record(date(2026, 10, 8)),
        day_record(date(2026, 10, 9), eCh=2.0),
    ]
    source.calls.clear()
    await cycle(state, source, sink, firmware=NEW_FIRMWARE)
    assert source.queries() == [{"date": "2026-10-05"}, {}, {"date": "2026-10-09"}]
    heating = sink.rows["heat_pump_electricity_heating"]
    assert heating[date(2026, 10, 9)] == pytest.approx(8.0)
    assert ascending(heating)
    # The new start is the known day from now on.
    source.calls.clear()
    await cycle(state, source, sink, firmware=NEW_FIRMWARE)
    assert source.queries() == [{"date": "2026-10-09"}]


async def test_an_empty_history_by_date_asks_once_per_cycle() -> None:
    source = Source([], exact=True)
    state = HistoryState(gas_checked_at=NOW)
    result = await cycle(state, source, Sink(), firmware=NEW_FIRMWARE)
    assert result.reason is None
    assert source.queries() == [{}]
    assert state.cursor is None and state.last_day is None


def test_an_empty_state_goes_on_from_the_recorded_statistics() -> None:
    """SYSTEM-05: the sums go on from the rows recorded before."""
    state = HistoryState()
    continue_from(
        state,
        {
            "heat_pump_electricity_heating": (date(2026, 10, 5), 100.0),
            "outdoor_temperature": (date(2026, 10, 5), None),
            "pS_unknown": (date(2026, 10, 9), 1.0),
        },
    )
    assert state.statistics_checked
    assert state.floor == state.last_day == date(2026, 10, 5)
    rows = merge(state, parsed(), TODAY)
    heating = dict(rows["heat_pump_electricity_heating"])
    # Days up to 5 October are recorded: never written again.
    assert min(heating) == date(2026, 10, 6)
    assert heating[date(2026, 10, 6)] == pytest.approx(105.5)
    # 5.5 + 6.5 (the later 7 October) + 4.5 + 4 + 3 on top of 100.
    assert heating[date(2026, 10, 10)] == pytest.approx(123.5)
    assert min(dict(rows["outdoor_temperature"])) == date(2026, 10, 6)
    # A statistic not recorded before starts at zero, as without rows.
    hot_water = dict(rows["heat_pump_electricity_hot_water"])
    assert hot_water[date(2026, 10, 6)] == pytest.approx(1.5)
    # A re-read of a recorded day changes nothing.
    assert (
        merge(state, parse_records([day_record(date(2026, 10, 5), eCh=50.0)])[0], TODAY)
        == {}
    )
    # Nothing recorded: the state only notes that it looked.
    empty = HistoryState()
    continue_from(empty, {})
    assert empty == HistoryState(statistics_checked=True)
    # A broken sum never runs a statistic below zero.
    broken = HistoryState()
    continue_from(
        broken,
        {
            "heat_pump_electricity_heating": (date(2026, 10, 5), -3.0),
            "gas_heating": (date(2026, 10, 5), math.nan),
        },
    )
    assert broken.base == {"heat_pump_electricity_heating": 0.0, "gas_heating": 0.0}
