"""Strictness package A5: recordings read slot by slot and as tolerantly as
their meaning allows.

- S2: behind an iCom gateway every energy series is samples of the mean
  (mean times hours), the boiler's as well; K30, K40 and other gateways keep
  their sums as they are.
- S3: an electric heater part without any data in the whole period counts as
  0 for the electricity of the energy monitoring (no counter to fall back
  on), with the attribute ``parts_missing``.
- S4: the bound of one hour's energy is higher for a series that counts a
  boiler and never below 1.5 times a known nominal output.
- S5: a faulty slot of a local recording drops that slot only.
- S6: start counters take words of a plain count and whole numbers written
  with a rounding error.

Tested with constructed answers; not yet confirmed on a real plant.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.recordings.ledger import CLOUD, Ledger
from custom_components.bosch_buderus_heating.recordings.parse import (
    MAX_KWH_PER_HOUR,
    MAX_KWH_PER_HOUR_BOILER,
    Rate,
    Sample,
    hourly_energy_limit,
    parse_cloud,
    parse_local,
    rejection,
)
from custom_components.bosch_buderus_heating.recordings.periods import Period
from custom_components.bosch_buderus_heating.recordings.sensor import (
    RecordingPeriodSensor,
)
from custom_components.bosch_buderus_heating.recordings.series import (
    BY_KEY,
    HEAT_SOURCE_SERIES,
    mean_samples,
)
from custom_components.bosch_buderus_heating.start_counters import (
    assess_start_counter,
)
from tests.test_icom_heat_pump_recordings import (
    MONITORING,
    NOW,
    heat_pump_answers,
    plant,
    recordings,
)
from tests.test_local_api import read
from tests.test_recordings_manager import Clock
from tests.test_start_counters import global_resource

Z = ZoneInfo("Europe/Berlin")
CH = BY_KEY["ch_actual_power"]
HEAT = BY_KEY["total_heat"]
STARTS = BY_KEY["total_starts"]


def _cloud(rows: list[dict[str, float]], unit: str = "kWh") -> dict[str, Any]:
    return {"type": "yRecording", "unitOfMeasure": unit, "recording": rows}


# ----------------------------------------------------- S2: iCom mean samples


def test_every_energy_series_behind_an_icom_gateway_is_mean_samples() -> None:
    assert mean_samples(CH, icom=True) and mean_samples(HEAT, icom=True)
    assert not mean_samples(CH) and not mean_samples(HEAT)
    assert not mean_samples(STARTS, icom=True)
    # Minute samples of a mean of 2 kW: 2 kWh per hour behind iCom ...
    rows = [{"y": 120.0, "c": 60}] * 24
    parsed = parse_cloud(_cloud(rows, "kW"), CH, "2026-11-21", Z, icom=True)
    assert parsed is not None
    assert {(sample.y, sample.c) for sample in parsed.samples} == {(2.0, 1.0)}
    # ... while a K30 or K40 neither takes kW nor this form of a sum.
    assert parse_cloud(_cloud(rows, "kW"), CH, "2026-11-21", Z) is None
    parsed = parse_cloud(_cloud(rows), CH, "2026-11-21", Z)
    assert parsed is not None and parsed.samples == []
    assert parsed.issues["count_form"] == 24
    # A day and a month of the year interval as the mean times their hours.
    days = [{"y": 2.0 * 1440, "c": 1440}] * 30
    parsed = parse_cloud(_cloud(days), CH, "2026-11", Z, icom=True)
    assert parsed is not None
    assert {sample.y for sample in parsed.samples} == {48.0}


async def test_stored_months_of_an_icom_boiler_are_corrected_in_a_run(
    hass: HomeAssistant,
) -> None:
    """A month an earlier version kept as a plain sum of minute samples is
    the mean times its hours once the run knows the iCom gateway; a K40's
    month stays as it is."""
    september = "2026-09-01"
    hours = 720
    cell = [2.0 * hours * 60, hours * 60, CLOUD, 2, True]
    for case, key, corrected in (
        ("issue_104_icom_boiler", "ch_actual_power", True),
        ("k40_own", "total_heat", False),
    ):
        coordinator = await plant(hass, case)
        manager, _calls = await recordings(hass, coordinator, Clock(NOW), {})
        manager._load({"series": {key: {"months": {september: list(cell)}}}})
        manager.acquire()
        await manager.async_run("start")
        month = manager.ledgers[key].months[september]
        if corrected:
            assert month[:2] == [pytest.approx(2.0 * hours), float(hours)]
            assert manager.ledgers[key].stats["months_corrected"] == 1
        else:
            assert month[:2] == cell[:2]
        await coordinator.async_shutdown()


# ------------------------------------------------- S3: heater part without data


def _gaps(interval: str) -> dict[str, Any]:
    count = 12 if len(interval) == 4 else 30 if len(interval) == 7 else 24
    return _cloud([{"y": 0.0, "c": 0}] * count)


async def test_a_heater_part_without_data_counts_as_zero(hass: HomeAssistant) -> None:
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    answers = heat_pump_answers()
    answers[MONITORING["monitoring_total_eheater"]] = _gaps
    clock = Clock(NOW)
    manager, _calls = await recordings(hass, coordinator, clock, answers)
    await manager.async_run("start")
    spec = next(
        spec
        for spec in manager.specs()
        if spec.measure.key == "monitoring_total_electricity"
        and spec.period is Period.YESTERDAY
    )
    manager.request(spec.key)
    clock.now += timedelta(hours=1)
    await manager.async_run("hourly")
    shown = manager.shown(spec)
    assert shown is not None
    # The compressor alone: 0.75 kWh an hour.
    assert shown.value == pytest.approx(18.0)
    assert shown.parts_missing == ("eheater",)
    sensor = RecordingPeriodSensor(coordinator, manager, spec)
    assert sensor.extra_state_attributes["parts_missing"] == ["eheater"]
    await coordinator.async_shutdown()


async def test_a_compressor_gap_stays_a_gap(hass: HomeAssistant) -> None:
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    answers = heat_pump_answers()
    answers[MONITORING["monitoring_total_eheater"]] = _gaps
    answers[MONITORING["monitoring_total_compressor"]] = _gaps
    clock = Clock(NOW)
    manager, _calls = await recordings(hass, coordinator, clock, answers)
    await manager.async_run("start")
    spec = next(
        spec
        for spec in manager.specs()
        if spec.measure.key == "monitoring_total_electricity"
        and spec.period is Period.YESTERDAY
    )
    manager.request(spec.key)
    clock.now += timedelta(hours=1)
    await manager.async_run("hourly")
    assert manager.shown(spec) is None
    await coordinator.async_shutdown()


async def test_both_parts_with_data_are_summed_without_a_mark(
    hass: HomeAssistant,
) -> None:
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    clock = Clock(NOW)
    manager, _calls = await recordings(hass, coordinator, clock, heat_pump_answers())
    await manager.async_run("start")
    spec = next(
        spec
        for spec in manager.specs()
        if spec.measure.key == "monitoring_total_electricity"
        and spec.period is Period.YESTERDAY
    )
    manager.request(spec.key)
    clock.now += timedelta(hours=1)
    await manager.async_run("hourly")
    shown = manager.shown(spec)
    assert shown is not None and shown.value == pytest.approx(24.0)
    assert shown.parts_missing == ()
    sensor = RecordingPeriodSensor(coordinator, manager, spec)
    assert "parts_missing" not in sensor.extra_state_attributes
    await coordinator.async_shutdown()


async def test_a_k40_electricity_keeps_falling_back_on_its_counters(
    hass: HomeAssistant,
) -> None:
    """With a counter to fall back on, a heater gap stays a gap of the
    recording (the counters then give the value): nothing counted as 0."""
    coordinator = await plant(hass, "k40_own")
    answers = {
        BY_KEY["total_compressor"].path: lambda interval: _sums(interval, 0.75),
        BY_KEY["total_eheater"].path: _gaps,
    }
    clock = Clock(NOW)
    manager, _calls = await recordings(hass, coordinator, clock, answers)
    manager.acquire()
    await manager.async_run("start")
    measure = next(
        item for item in manager.measures() if item.key == "total_electricity"
    )
    assert measure.counter is not None
    today = NOW.date()
    value = manager.recorded(measure, Period.YESTERDAY, today)
    assert value is None or value.gap
    assert value is None or not value.missing
    await coordinator.async_shutdown()


def _sums(interval: str, hourly: float) -> dict[str, Any]:
    if len(interval) == 10:
        return _cloud([{"y": hourly, "c": 1}] * 24)
    if len(interval) == 7:
        return _cloud([{"y": hourly * 24, "c": 24}] * 30)
    return _cloud([{"y": hourly * 700, "c": 700}] * 12)


# ----------------------------------------------------------- S4: the bound


def test_the_bound_of_an_hour_follows_the_series_and_the_plant() -> None:
    assert MAX_KWH_PER_HOUR == 90.0
    assert hourly_energy_limit(HEAT) == MAX_KWH_PER_HOUR
    assert hourly_energy_limit(CH) == MAX_KWH_PER_HOUR_BOILER
    assert hourly_energy_limit(BY_KEY["total_consumed"]) == MAX_KWH_PER_HOUR_BOILER
    assert hourly_energy_limit(BY_KEY["total_burner"]) == MAX_KWH_PER_HOUR_BOILER
    # The gateway's series of a plant with a boiler count the boiler.
    assert hourly_energy_limit(HEAT, boiler_plant=True) == MAX_KWH_PER_HOUR_BOILER
    # A hybrid system's heat pump keeps the bound of a heat pump.
    own = HEAT_SOURCE_SERIES[0]
    assert hourly_energy_limit(own, boiler_plant=True) == MAX_KWH_PER_HOUR
    # Never below 1.5 times a known nominal output, never lower than before.
    assert hourly_energy_limit(HEAT, rated_kw=10.0) == MAX_KWH_PER_HOUR
    assert hourly_energy_limit(HEAT, rated_kw=100.0) == 150.0
    assert hourly_energy_limit(CH, rated_kw=400.0) == 600.0
    assert hourly_energy_limit(HEAT, rated_kw=float("nan")) == MAX_KWH_PER_HOUR
    assert hourly_energy_limit(STARTS) == MAX_KWH_PER_HOUR


def test_a_full_load_hour_of_a_boiler_is_kept() -> None:
    now = datetime(2026, 11, 22, 12, tzinfo=UTC)
    sample = Sample(Rate.HOUR, date(2026, 11, 21), None, 120.0, 1.0)
    kwargs: dict[str, Any] = {"now": now, "zone": Z, "floor": None}
    assert rejection(sample, HEAT, **kwargs) == "implausible"
    limit = hourly_energy_limit(CH)
    assert rejection(sample, CH, **kwargs, energy_limit=limit) is None
    ledger = Ledger(CH, {})
    from custom_components.bosch_buderus_heating.recordings.parse import Parsed

    ledger.merge(
        Parsed(samples=[sample]), CLOUD, read_at=now, zone=Z, energy_limit=limit
    )
    assert not ledger.stats.get("discarded_implausible")
    ledger = Ledger(HEAT, {})
    ledger.merge(Parsed(samples=[sample]), CLOUD, read_at=now, zone=Z)
    assert ledger.stats["discarded_implausible"] == 1


# ------------------------------------------------------- S5: local slot by slot


SUPPLY = BY_KEY["supply_temperature"]


def _local_recording(*slots: object, **group: object) -> dict[str, Any]:
    """A local daily recording of the supply temperature, 1 to 3 November."""
    return {
        "id": "/recordings/heatSources/actualSupplyTemperature?sampleRate=all",
        "type": "yRecordingExtended",
        "writeable": 0,
        "recordingType": "actual",
        "unitOfMeasure": "C",
        "recordedResource": {
            "id": "/heatSources/actualSupplyTemperature",
            "uri": "http://192.0.2.10/heatSources/actualSupplyTemperature",
        },
        "values": [
            {
                "sampleRate": "P1D",
                "startTime": "2026-11-01T00:00:00+01:00",
                "endTime": "2026-11-03T00:00:00+01:00",
                "recording": list(slots)
                or [{"y": 55, "c": 24}, {"y": 54, "c": 24}, {"y": 56, "c": 24}],
                **group,
            }
        ],
    }


@pytest.mark.parametrize(
    "faulty",
    [{"y": 1, "c": -1}, {"y": True, "c": 1}, {"y": 1}, 5, {"y": "x", "c": 1}],
)
def test_a_faulty_local_slot_drops_that_slot_only(faulty: object) -> None:
    raw = _local_recording({"y": 55 * 24, "c": 24}, faulty, {"y": 56 * 24, "c": 24})
    recording = read(raw).data
    assert len(recording.groups[0].samples) == 3
    parsed = parse_local(recording, SUPPLY, Z)
    assert parsed is not None
    assert parsed.issues["invalid_value"] == 1
    assert [sample.day.day for sample in parsed.samples] == [1, 3]


def test_a_reversed_group_drops_that_group_only() -> None:
    raw = _local_recording()
    raw["values"].append(
        {
            "sampleRate": "P1D",
            "startTime": "2026-11-05T00:00:00+01:00",
            "endTime": "2026-11-04T00:00:00+01:00",
            "recording": [{"y": 1, "c": 24}],
        }
    )
    recording = read(raw).data
    assert len(recording.groups) == 1
    parsed = parse_local(recording, SUPPLY, Z)
    assert parsed is not None and parsed.issues["reversed_group"] == 1
    assert len(parsed.samples) == 3


def test_another_recording_type_and_rate_do_not_drop_the_recording() -> None:
    raw = _local_recording()
    raw["recordingType"] = "estimated"
    raw["values"].append({**raw["values"][0], "sampleRate": "PT15M"})
    recording = read(raw).data
    parsed = parse_local(recording, SUPPLY, Z)
    assert parsed is not None and parsed.issues["unknown_rate"] == 1
    assert len(parsed.samples) == 3


# ------------------------------------------------------------ S6: start counts


@pytest.mark.parametrize(
    ("value", "expected", "reason"),
    [
        (1234.0000001, 1234, None),
        (1233.9999999, 1234, None),
        (1234.4, None, "not_integer"),
        (1234.001, None, "not_integer"),
    ],
)
def test_a_whole_count_with_a_rounding_error(
    value: float, expected: int | None, reason: str | None
) -> None:
    reading = assess_start_counter(global_resource(value)).reading(None)
    assert reading.value == expected
    assert reading.reason == reason
