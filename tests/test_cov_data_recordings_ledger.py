"""Edge cases of the recordings ledger and the series catalogue: damaged
stored cells, live values of a lower source, rounded months around known
days, corrections of the first imported row and what the period entity
shows."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.recordings import series as catalogue
from custom_components.bosch_buderus_heating.recordings.ledger import (
    CLOUD,
    DAY_KEEP,
    LOCAL,
    Ledger,
)
from custom_components.bosch_buderus_heating.recordings.parse import (
    Parsed,
    Rate,
    Sample,
)
from custom_components.bosch_buderus_heating.recordings.periods import Period
from custom_components.bosch_buderus_heating.recordings.sensor import (
    RecordingPeriodSensor,
)
from custom_components.bosch_buderus_heating.recordings.series import (
    BY_KEY,
    SERIES,
    Quantity,
    electricity_parts,
    unit_factor,
)
from tests.test_recordings_core import hours, local, read
from tests.test_recordings_manager import Clock, Driver, key, manager

Z = ZoneInfo("Europe/Berlin")
HEAT = BY_KEY["total_heat"]
OUTDOOR = BY_KEY["outdoor_temperature"]
NOW = local(2026, 11, 14, 10) + timedelta(minutes=20)


def _utc(moment: datetime) -> int:
    return int(moment.timestamp())


def _final(y: float, c: float = 1.0) -> list[Any]:
    return [y, c, LOCAL, 2, True]


# ----------------------------------------------------------- damaged store
def test_damaged_cells_and_tables_of_a_stored_ledger_are_dropped() -> None:
    good = [1.0, 1.0, LOCAL, 2, True]
    ledger = Ledger(
        HEAT,
        {
            "days": {
                "2026-11-01": good,
                "2026-11-02": [1, 2, 3],
                "2026-11-03": ["x", 1.0, LOCAL, 1, True],
                "2026-11-04": [1.0, 1.0, "elsewhere", 1, True],
                "2026-11-05": "cell",
                7: good,
            },
            "hours": "nothing",
            "months": None,
            "floor": 5,
            "unplaced": ["2026-03-28", 4],
            "stats": {"late_corrections": 2, "bad": 1.5, 3: 1, "flag": True},
            "import": {"next": 1, "sum": 0.0, "rows": [], "parts": [0, 0, 0]},
        },
    )
    assert list(ledger.days) == ["2026-11-01"]
    assert ledger.hours == {} and ledger.months == {}
    assert ledger.floor is None and ledger.unplaced == {"2026-03-28"}
    assert dict(ledger.stats) == {"late_corrections": 2}
    assert ledger.imp is None
    assert Ledger(HEAT, "garbage").days == {}  # type: ignore[arg-type]


def test_a_stored_ledger_round_trips_through_json() -> None:
    ledger = Ledger(HEAT)
    at = local(2026, 11, 20, 12)
    read(ledger, at, hours(local(2026, 11, 13), [(1.0, 1)] * 24))
    again = Ledger(HEAT, json.loads(json.dumps(ledger.to_dict())))
    assert again.hours == ledger.hours and again.days == ledger.days


def test_days_older_than_the_kept_range_are_pruned() -> None:
    ledger = Ledger(HEAT)
    now = datetime(2026, 11, 14, tzinfo=UTC)
    old = (now - DAY_KEEP - timedelta(days=1)).date().isoformat()
    new = (now - timedelta(days=3)).date().isoformat()
    ledger.days[old] = _final(1.0)
    ledger.days[new] = _final(1.0)
    ledger.unplaced = {old, new}
    ledger.prune(now)
    assert list(ledger.days) == [new]
    assert ledger.unplaced == {new}


# ----------------------------------------------------------- merging
def test_a_live_value_of_the_cloud_never_replaces_a_running_local_one() -> None:
    ledger = Ledger(HEAT)
    at = local(2026, 11, 14, 10) + timedelta(minutes=5)
    start = _utc(local(2026, 11, 14, 10))
    parsed = Parsed(samples=[Sample(Rate.HOUR, date(2026, 11, 14), start, 1.0, 1.0)])
    ledger.merge(parsed, LOCAL, read_at=at.astimezone(UTC), zone=Z)
    cloud = Parsed(samples=[Sample(Rate.HOUR, date(2026, 11, 14), start, 9.0, 1.0)])
    ledger.merge(cloud, CLOUD, read_at=at.astimezone(UTC), zone=Z)
    assert ledger.hours[str(start)][:3] == [1.0, 1.0, LOCAL]
    # The same source replaces its own live value.
    newer = Parsed(samples=[Sample(Rate.HOUR, date(2026, 11, 14), start, 1.5, 1.0)])
    ledger.merge(newer, LOCAL, read_at=at.astimezone(UTC), zone=Z)
    assert ledger.hours[str(start)][0] == 1.5


def test_a_final_cell_is_not_overwritten_by_a_live_read() -> None:
    ledger = Ledger(HEAT)
    start = _utc(local(2026, 11, 14, 10))
    ledger.hours[str(start)] = _final(4.0)
    at = local(2026, 11, 14, 10) + timedelta(minutes=5)
    live = Parsed(samples=[Sample(Rate.HOUR, date(2026, 11, 14), start, 8.0, 1.0)])
    ledger.merge(live, LOCAL, read_at=at.astimezone(UTC), zone=Z)
    assert ledger.hours[str(start)][0] == 4.0


def test_without_values_a_full_read_sets_no_floor() -> None:
    ledger = Ledger(HEAT)
    ledger.merge(
        Parsed(samples=[Sample(Rate.DAY, date(2026, 11, 1), None, 0.0, 0.0)]),
        LOCAL,
        read_at=NOW.astimezone(UTC),
        zone=Z,
        full_history=True,
    )
    assert ledger.floor is None


def test_a_gap_day_reads_as_no_value_and_a_temperature_as_its_mean() -> None:
    ledger = Ledger(OUTDOOR)
    ledger.days["2026-11-01"] = _final(240.0, 24.0)
    ledger.days["2026-11-02"] = _final(0.0, 0.0)
    first, gap = ledger.daily(date(2026, 11, 1), date(2026, 11, 2))
    assert first.value == pytest.approx(10.0)
    assert gap.value is None
    energy = Ledger(HEAT)
    energy.days["2026-11-01"] = _final(12.0, 24.0)
    assert energy.daily(date(2026, 11, 1), date(2026, 11, 1))[0].value == 12.0
    assert energy.daily(date(2026, 12, 1), date(2026, 12, 2)) == ()


# ----------------------------------------------------------- the import
def test_without_any_recorded_slot_no_import_can_start() -> None:
    ledger = Ledger(HEAT)
    assert ledger.start_import(0.0, Z) is False
    assert not ledger.importing
    assert ledger.import_start(Z) is None
    plan = ledger.plan(NOW.astimezone(UTC), Z)
    assert plan.rows == [] and plan.clear is None and plan.state is None
    ledger.commit(plan)  # nothing to take over, nothing breaks


def test_a_plan_is_taken_over_once() -> None:
    ledger = Ledger(HEAT)
    at = local(2026, 11, 20, 12)
    read(ledger, at, hours(local(2026, 11, 13), [(1.0, 1)] * 24))
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(at.astimezone(UTC), Z, commit=False)
    assert plan.rows and ledger.imp is not None and ledger.imp["rows"] == []
    ledger.commit(plan)
    assert len(ledger.imp["rows"]) == 24 and plan.state is None
    before = dict(ledger.stats)
    ledger.commit(plan)
    assert dict(ledger.stats) == before and len(ledger.imp["rows"]) == 24


def test_a_rounded_month_is_split_around_days_that_are_known_by_their_hours() -> None:
    """The rest of a month is its value less what later days (known by hours
    only) already hold; the hours then follow as hours."""
    ledger = Ledger(HEAT)
    ledger.months["2026-10-01"] = _final(300.0)
    for day in (20, 21):
        for hour in range(24):
            moment = _utc(local(2026, 10, day, hour))
            ledger.hours[str(moment)] = [1.0, 1.0, LOCAL, 2, True]
    now = local(2026, 12, 1).astimezone(UTC)
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(now, Z)
    first = plan.rows[0]
    assert first[0] == _utc(local(2026, 10, 1))
    assert first[1] == pytest.approx(300.0 - 48.0)
    hours_after = [row for row in plan.rows[1:]]
    assert len(hours_after) == 48
    assert plan.rows[-1][3] == pytest.approx(300.0)
    assert ledger.stats["imported_m"] == 1 and ledger.stats["imported_h"] == 48


def test_a_rounded_month_of_a_temperature_needs_a_sample_count() -> None:
    ledger = Ledger(OUTDOOR)
    ledger.months["2026-10-01"] = _final(0.0, 0.0)
    ledger.days["2026-10-20"] = _final(240.0, 24.0)
    now = local(2026, 12, 1).astimezone(UTC)
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(now, Z)
    # No mean for the days before the 20th: nothing is made up for them.
    assert [row[0] for row in plan.rows] == [_utc(local(2026, 10, 20))]


def test_a_corrected_first_row_is_imported_again_from_the_base() -> None:
    ledger = Ledger(HEAT)
    at = local(2026, 11, 20, 12)
    read(ledger, at, hours(local(2026, 11, 13), [(1.0, 1)] * 24))
    assert ledger.start_import(500.0, Z)
    assert ledger.plan(at.astimezone(UTC), Z).rows[0][3] == 501.0
    first = str(_utc(local(2026, 11, 13)))
    ledger.hours[first] = [4.0, 1.0, LOCAL, 2, True]
    plan = ledger.plan(at.astimezone(UTC), Z)
    assert plan.clear == (_utc(local(2026, 11, 13)), _utc(local(2026, 11, 14)))
    assert plan.rows[0][0] == _utc(local(2026, 11, 13))
    # The sum continues from the base before the first row.
    assert plan.rows[0][3] == pytest.approx(504.0)
    assert plan.rows[-1][3] == pytest.approx(527.0)
    assert ledger.stats["imported_again"] == 1


def test_where_a_range_cannot_be_cleared_the_rows_are_remembered_as_stale() -> None:
    ledger = Ledger(HEAT)
    at = local(2026, 11, 20, 12)
    read(ledger, at, hours(local(2026, 11, 13), [(1.0, 1)] * 24))
    assert ledger.start_import(0.0, Z)
    ledger.plan(at.astimezone(UTC), Z)
    ledger.hours[str(_utc(local(2026, 11, 13, 5)))] = [9.0, 1.0, LOCAL, 2, True]
    plan = ledger.plan(at.astimezone(UTC), Z, can_clear=False)
    assert plan.clear is not None
    # The rows after the changed one are written again, so none is left over.
    assert ledger.imp is not None and ledger.imp["stale"] == []
    assert plan.rows[0][0] == _utc(local(2026, 11, 13, 5))


# ----------------------------------------------------------- the catalogue
@pytest.mark.parametrize(
    "path",
    [
        "/recordings/unknown/path",
        "/system/info",
        "/recordings/heatSources/emon/bogus/outputProduced",
        "/recordings/heatSources/emon/total/bogus",
        "/recordings/heatSources/emon/total",
    ],
)
def test_a_path_that_is_no_recorded_series_has_no_series(path: str) -> None:
    assert catalogue._series(path) is None


def test_solar_and_ventilation_totals_keep_their_plain_keys() -> None:
    solar = catalogue._series("/recordings/heatSources/emon/total/solar")
    assert solar is not None and solar.key == "solar"
    assert solar.quantity is Quantity.ENERGY
    modal = catalogue._series("/recordings/heatSources/emon/ch/outputProduced")
    assert modal is not None and modal.key == "ch_heat" and modal.unit == "kWh"
    assert not modal.mean and BY_KEY["outdoor_temperature"].mean


def test_the_catalogue_has_one_series_per_key_and_the_cloud_ones_are_energy() -> None:
    assert len({item.key for item in SERIES}) == len(SERIES)
    assert [item.path for item in SERIES] == sorted(item.path for item in SERIES)
    assert {item.key for item in SERIES if item.cloud} == {
        f"{mode}_{kind}"
        for mode in ("total", "ch", "dhw")
        for kind in ("heat", "compressor", "eheater")
    }
    assert electricity_parts("ch") == ("ch_compressor", "ch_eheater")


@pytest.mark.parametrize(
    ("quantity", "unit", "factor"),
    [
        (Quantity.ENERGY, "Wh", 0.001),
        (Quantity.ENERGY, "MWh", 1000.0),
        (Quantity.ENERGY, "BTU", None),
        (Quantity.DURATION, "s", 1 / 3600),
        (Quantity.DURATION, "min", 1 / 60),
        (Quantity.COUNT, None, 1.0),
        (Quantity.COUNT, "kWh", None),
        (Quantity.TEMPERATURE, "F", None),
        (Quantity.TEMPERATURE, None, None),
    ],
)
def test_units_are_converted_or_refused(
    quantity: Quantity, unit: str | None, factor: float | None
) -> None:
    assert unit_factor(quantity, unit) == factor


# ----------------------------------------------------------- the entity
async def test_the_entity_shows_its_value_with_the_counter_it_came_from(
    hass: HomeAssistant,
) -> None:
    clock = Clock(NOW)
    rec = await manager(hass, clock, [Driver(clock)])
    name = key("ch_heat", Period.CURRENT_MONTH)
    rec.request(name)
    await rec.async_run("start")
    spec = next(s for s in rec.specs() if s.key == name)

    entity = RecordingPeriodSensor.__new__(RecordingPeriodSensor)
    entity._manager, entity._spec, entity._release = rec, spec, None
    attributes = entity.extra_state_attributes
    assert attributes["source"] in ("local_recording", "counters")
    assert attributes["period_start"] == "2026-11-01"
    assert attributes["period_end"] == "2026-11-30"
    assert entity.available and entity.native_value is not None

    # A counter stands in: the attribute names its entity, or none.
    rec.published[name] = ["2026-11-01", "2026-12-01", 5.0, "counters", "hourly"]
    rec.counter_entities["ch_heat"] = "sensor.counter"
    assert entity.extra_state_attributes["derived_from"] == ["sensor.counter"]
    del rec.counter_entities["ch_heat"]
    assert entity.extra_state_attributes["derived_from"] == []

    # H-B3-7: no value yet while the source is attached: unknown, not
    # unavailable; only the method is named.
    del rec.published[name]
    assert entity.available and entity.native_value is None
    assert entity.extra_state_attributes == {"method": "recording"}
    # Unavailable only when the source cannot be reached (nothing attached).
    rec.detach(rec._test_coordinator)
    assert rec.coordinator() is None
    assert not entity.available and entity.native_value is None
    # A value that is still shown stays available without a source attempt.
    rec.published[name] = ["2026-11-01", "2026-12-01", 5.0, "counters", "hourly"]
    assert entity.available and entity.native_value == 5.0
    await entity.async_will_remove_from_hass()  # no release: nothing to undo


def test_a_recent_day_is_imported_as_a_day_only_while_no_hour_of_it_is_known() -> None:
    now = local(2026, 11, 14, 1).astimezone(UTC)
    day = date(2026, 11, 13)
    ledger = Ledger(HEAT)
    ledger.days[day.isoformat()] = _final(24.0, 24.0)
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(now, Z, commit=False)
    assert [row[0] for row in plan.rows] == [_utc(local(2026, 11, 13))]
    assert plan.rows[0][1] == pytest.approx(24.0)
    # An hour of that day is known (not yet confirmed): the hours may still
    # come, so the day waits and nothing is imported yet.
    ledger.hours[str(_utc(local(2026, 11, 13, 5)))] = [1.0, 1.0, LOCAL, 1, False]
    waiting = ledger.plan(now, Z, commit=False)
    assert waiting.rows == []
