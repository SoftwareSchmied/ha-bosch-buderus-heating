"""Series kept as samples of the mean (the energy monitoring of iCom
gateways, ``minute_sums``).

Every slot of such a series is the mean of its samples times the hours of the
slot: an hour, a day and also a month of the year interval (earlier versions
kept the months with the plant's sum, up to about 60 times the energy). A slot
still running counts the hours elapsed. The energy units the gateways write
are taken in any case, a unit of power (kW) only for such a series. Months an
earlier version kept are corrected when the recordings are loaded, and an
imported row made from such a month is imported again. Every other series
reads exactly as before.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.bosch_buderus_heating.recordings.ledger import CLOUD, Ledger
from custom_components.bosch_buderus_heating.recordings.parse import (
    Rate,
    add_months,
    count_fits,
    local_midnight,
    parse_cloud,
    slot_hours,
)
from custom_components.bosch_buderus_heating.recordings.series import (
    ALL_SERIES,
    BY_KEY,
    UNITS,
    Quantity,
    Series,
    series_unit_factor,
    unit_factor,
)

Z = ZoneInfo("Europe/Berlin")
CONSUMED = BY_KEY["total_consumed"]
MONITORING = BY_KEY["monitoring_total_heat"]
HEAT = BY_KEY["total_heat"]
# 2026-11-22 10:20 on the plant's clock.
NOW = datetime(2026, 11, 22, 10, 20, tzinfo=Z)


def _cloud(rows: list[dict[str, float]], unit: str = "kWh") -> dict[str, Any]:
    return {"type": "yRecording", "unitOfMeasure": unit, "recording": rows}


def _minutes(first: date, mean: float) -> list[dict[str, float]]:
    """A year of minute samples with the same mean power, month by month."""
    rows = []
    for index in range(12):
        month = add_months(first, index)
        count = slot_hours(Rate.MONTH, month, Z) * 60
        rows.append({"y": mean * count, "c": count})
    return rows


# ------------------------------------------------------------------- months


@pytest.mark.parametrize("series", [CONSUMED, MONITORING])
def test_the_months_of_a_year_are_the_mean_times_their_hours(series: Series) -> None:
    parsed = parse_cloud(_cloud(_minutes(date(2025, 1, 1), 0.3)), series, "2025", Z)
    assert parsed is not None and len(parsed.samples) == 12
    assert not parsed.issues
    for sample in parsed.samples:
        hours = slot_hours(Rate.MONTH, sample.day, Z)
        assert sample.rate is Rate.MONTH
        assert sample.c == hours
        assert sample.y == pytest.approx(0.3 * hours)
    # Summer time starts in March and ends in October.
    assert parsed.samples[2].c == 743 and parsed.samples[9].c == 745
    # Not the sum of the samples (60 times as much).
    assert parsed.samples[0].y == pytest.approx(0.3 * 744)


def test_a_month_without_data_stays_a_gap() -> None:
    rows = _minutes(date(2025, 1, 1), 0.3)
    rows[4] = {"y": 1.0, "c": 0}
    parsed = parse_cloud(_cloud(rows), CONSUMED, "2025", Z)
    assert parsed is not None
    assert (parsed.samples[4].y, parsed.samples[4].c) == (1.0, 0)


def test_slots_still_running_count_the_hours_elapsed() -> None:
    now = NOW.astimezone(UTC)
    elapsed_month = 21 * 24 + 10 + 1 / 3
    year = _minutes(date(2026, 1, 1), 0.5)
    year[10] = {"y": 0.5 * elapsed_month * 60, "c": elapsed_month * 60}
    year[11] = {"y": 1.0, "c": 0}
    parsed = parse_cloud(_cloud(year), CONSUMED, "2026", Z, now)
    assert parsed is not None
    november = parsed.samples[10]
    assert november.y == pytest.approx(0.5 * elapsed_month)
    assert november.c == 720
    assert parsed.samples[9].y == pytest.approx(0.5 * 745)
    assert parsed.samples[11].c == 0

    days = [{"y": 0.5 * 1440, "c": 1440}] * 21 + [{"y": 0.5 * 620, "c": 620}]
    days += [{"y": 1.0, "c": 0}] * 8
    parsed = parse_cloud(_cloud(days), CONSUMED, "2026-11", Z, now)
    assert parsed is not None
    assert parsed.samples[0].y == pytest.approx(12.0)
    assert parsed.samples[21].y == pytest.approx(0.5 * (10 + 1 / 3))
    assert parsed.samples[21].c == 24

    hours = [{"y": 30.0, "c": 60}] * 10 + [{"y": 10.0, "c": 20}]
    hours += [{"y": 1.0, "c": 0}] * 13
    parsed = parse_cloud(_cloud(hours), CONSUMED, "2026-11-22", Z, now)
    assert parsed is not None
    assert parsed.samples[0].y == pytest.approx(0.5)
    assert parsed.samples[10].y == pytest.approx(0.5 / 3)
    assert parsed.samples[10].c == 1.0
    # Read without a time, every slot counts its full hours.
    full = parse_cloud(_cloud(hours), CONSUMED, "2026-11-22", Z)
    assert full is not None and full.samples[10].y == pytest.approx(0.5)


def test_a_month_of_one_value_per_hour_is_the_mean_times_its_hours() -> None:
    rows = [{"y": 30.0 * 24, "c": 720}] * 12
    parsed = parse_cloud(_cloud(rows), CONSUMED, "2025", Z)
    assert parsed is not None
    assert parsed.samples[0].y == pytest.approx(720.0 / 720 * 744)
    assert parsed.samples[3].y == pytest.approx(720.0)


# ----------------------------------------------- every other series as before


def _unit(series: Series) -> str | None:
    return next(iter(UNITS[series.quantity]))


def _before(
    payload: dict[str, Any], series: Series, interval: str
) -> list[tuple[Any, ...]]:
    """What the earlier version took from a cloud answer of a series that is
    not kept as samples of the mean: ``y`` times the unit's factor and the
    count as they are, hours and days judged by their count."""
    factor = UNITS[series.quantity][payload["unitOfMeasure"]]
    rows = payload["recording"]
    found = []
    if len(interval) == 4:
        first = date(int(interval), 1, 1)
        for index, row in enumerate(rows):
            found.append(
                (Rate.MONTH, add_months(first, index), row["y"] * factor, row["c"])
            )
        return found
    rate = Rate.HOUR if len(interval) == 10 else Rate.DAY
    first = date.fromisoformat(interval if rate is Rate.HOUR else f"{interval}-01")
    for index, row in enumerate(rows):
        day = first if rate is Rate.HOUR else first + timedelta(days=index)
        if count_fits(series.quantity, rate, row["c"], day, Z):
            found.append((rate, day, row["y"] * factor, row["c"]))
    return found


OTHER = [item for item in ALL_SERIES if not item.minute_sums]
SHAPES = {
    "2025": [{"y": 12.5 * (i + 1), "c": c} for i, c in enumerate([744, 0] * 6)],
    "2026": [{"y": 7.25 * (i + 1), "c": 44640 if i % 2 else 31} for i in range(12)],
    "2026-11": [{"y": 1.5 * (i + 1), "c": (24, 1440, 17, 0)[i % 4]} for i in range(30)],
    "2026-11-21": [{"y": 0.25 * (i + 1), "c": (1, 60, 0)[i % 3]} for i in range(24)],
}


@pytest.mark.parametrize("interval", sorted(SHAPES))
def test_every_other_series_reads_exactly_as_before(interval: str) -> None:
    assert OTHER and CONSUMED not in OTHER and MONITORING not in OTHER
    for series in OTHER:
        payload = _cloud(SHAPES[interval], _unit(series))  # type: ignore[arg-type]
        for now in (None, NOW.astimezone(UTC)):
            parsed = parse_cloud(payload, series, interval, Z, now)
            assert parsed is not None, series.key
            taken = [(s.rate, s.day, s.y, s.c) for s in parsed.samples]
            assert taken == _before(payload, series, interval), series.key


# -------------------------------------------------------------------- units


@pytest.mark.parametrize(
    ("unit", "factor"),
    [
        ("kWh", 1.0),
        ("KWh", 1.0),
        ("kwh", 1.0),
        ("KWH", 1.0),
        (" kWh ", 1.0),
        ("Wh", 0.001),
        ("wh", 0.001),
        ("WH", 0.001),
        ("MWh", 1000.0),
        ("mWh", None),
        ("mwh", None),
        ("W", None),
        ("unit", None),
        ("", None),
        (None, None),
    ],
)
def test_the_energy_units_of_every_series(
    unit: str | None, factor: float | None
) -> None:
    for series in (HEAT, CONSUMED, MONITORING):
        assert series_unit_factor(series, unit) == factor


@pytest.mark.parametrize("unit", ["kW", "KW", "kw", " kW"])
def test_a_unit_of_power_only_for_a_series_of_mean_samples(unit: str) -> None:
    assert series_unit_factor(CONSUMED, unit) == 1.0
    assert series_unit_factor(MONITORING, unit) == 1.0
    assert series_unit_factor(HEAT, unit) is None
    assert unit_factor(Quantity.ENERGY, unit) is None
    rows = [{"y": 18.0, "c": 60}] * 24
    assert parse_cloud(_cloud(rows, unit), HEAT, "2026-11-21", Z) is None
    parsed = parse_cloud(_cloud(rows, unit), MONITORING, "2026-11-21", Z)
    assert parsed is not None
    assert {(s.y, s.c) for s in parsed.samples} == {(0.3, 1.0)}


def test_other_quantities_keep_their_units_as_written() -> None:
    assert unit_factor(Quantity.TEMPERATURE, "c") is None
    assert unit_factor(Quantity.DURATION, "H") is None
    assert unit_factor(Quantity.VOLUME, "KWh") is None
    assert unit_factor(Quantity.COUNT, "kW") is None


# ------------------------------------------------- months of earlier versions


def _cell(y: float, c: float, final: bool = True) -> list[Any]:
    return [y, c, CLOUD, 2, final]


def test_months_kept_by_an_earlier_version_are_corrected() -> None:
    september, november = "2026-09-01", "2026-11-01"
    ledger = Ledger(
        CONSUMED,
        {
            "months": {
                september: _cell(0.3 * 720 * 60, 720 * 60),
                "2026-10-01": _cell(0.3 * 745, 745),
                november: _cell(0.5 * 30000, 30000, final=False),
                "2026-12-01": _cell(1.0, 0, final=False),
            }
        },
    )
    assert ledger.correct_mean_months(NOW, Z) == 2
    assert ledger.months[september] == [pytest.approx(0.3 * 720), 720.0, CLOUD, 2, True]
    assert ledger.months["2026-10-01"] == _cell(0.3 * 745, 745)
    assert november not in ledger.months
    assert ledger.months["2026-12-01"] == _cell(0.0, 0, final=False)
    assert ledger.stats["months_corrected"] == 2
    # In this form already: nothing changes any more.
    assert ledger.correct_mean_months(NOW, Z) == 0
    assert ledger.stats["months_corrected"] == 2


def test_the_months_of_every_other_series_stay() -> None:
    cells = {"2026-09-01": _cell(43200.0, 43200)}
    ledger = Ledger(HEAT, {"months": cells})
    assert ledger.correct_mean_months(NOW, Z) == 0
    assert ledger.months == {"2026-09-01": _cell(43200.0, 43200)}
    assert not ledger.stats


def test_a_row_imported_from_a_corrected_month_is_imported_again() -> None:
    ledger = Ledger(CONSUMED, {"months": {"2026-09-01": _cell(12960.0, 43200)}})
    assert ledger.start_import(0.0, Z)
    start = int(local_midnight(date(2026, 9, 1), Z).timestamp())
    first = ledger.plan(NOW, Z)
    assert first.rows == [(start, 12960.0, 43200, 12960.0)]
    assert ledger.correct_mean_months(NOW, Z) == 1
    assert ledger.imp is not None and ledger.imp["redo"] == start
    again = ledger.plan(NOW, Z)
    assert again.clear == (start, start + 3600)
    assert again.rows == [(start, pytest.approx(216.0), 720.0, pytest.approx(216.0))]
    assert ledger.stats["imported_again"] == 1
    assert "redo" not in ledger.imp
    # Nothing more to do on the next run.
    assert ledger.plan(NOW, Z).rows == []


def test_a_correction_without_an_imported_month_row_clears_nothing() -> None:
    ledger = Ledger(CONSUMED, {"months": {"2026-09-01": _cell(12960.0, 43200)}})
    assert ledger.correct_mean_months(NOW, Z) == 1
    assert ledger.imp is None
    ledger.start_import(0.0, Z)
    plan = ledger.plan(NOW, Z)
    assert plan.clear is None
    assert plan.rows == [
        (
            int(local_midnight(date(2026, 9, 1), Z).timestamp()),
            pytest.approx(216.0),
            720.0,
            pytest.approx(216.0),
        )
    ]


def test_a_sample_of_a_series_of_mean_samples_is_merged_as_read() -> None:
    ledger = Ledger(CONSUMED)
    parsed = parse_cloud(_cloud(_minutes(date(2025, 1, 1), 0.3)), CONSUMED, "2025", Z)
    assert parsed is not None
    ledger.merge(parsed, CLOUD, read_at=NOW, zone=Z)
    assert ledger.months["2025-01-01"][:2] == [pytest.approx(0.3 * 744), 744.0]
    assert ledger.correct_mean_months(NOW, Z) == 0


# Every unit taken before, with its factor exactly as before.
UNITS_BEFORE: dict[Quantity, dict[str | None, float]] = {
    Quantity.ENERGY: {"kWh": 1.0, "Wh": 0.001, "wh": 0.001, "MWh": 1000.0},
    Quantity.COUNT: {None: 1.0, "": 1.0, "count": 1.0},
    Quantity.DURATION: {"s": 1 / 3600, "min": 1 / 60, "mins": 1 / 60, "h": 1.0},
    Quantity.TEMPERATURE: {"C": 1.0, "°C": 1.0},
    Quantity.VOLUME: {"l": 1.0, "L": 1.0, "m3": 1000.0, "m³": 1000.0},
}


def test_every_unit_taken_before_keeps_its_factor() -> None:
    for quantity, table in UNITS_BEFORE.items():
        for unit, factor in table.items():
            assert unit_factor(quantity, unit) == factor, (quantity, unit)


@pytest.mark.parametrize(
    ("quantity", "unit", "factor"),
    [
        (Quantity.ENERGY, "KWh", 1.0),
        (Quantity.ENERGY, "kBtu", 0.29307107),
        (Quantity.DURATION, "hours", 1.0),
        (Quantity.DURATION, "hour", 1.0),
        (Quantity.DURATION, "minutes", 1 / 60),
        (Quantity.DURATION, "seconds", 1 / 3600),
        (Quantity.DURATION, "days", 24.0),
        (Quantity.TEMPERATURE, "degC", 1.0),
        (Quantity.TEMPERATURE, "ºC", 1.0),
        (Quantity.TEMPERATURE, "°F", None),
        (Quantity.TEMPERATURE, "K", None),
        (Quantity.ENERGY, "kW", None),
    ],
)
def test_the_spellings_of_the_shared_unit_table(
    quantity: Quantity, unit: str, factor: float | None
) -> None:
    assert unit_factor(quantity, unit) == factor
