"""A gateway that answers minute values (c 60 per hour, y the sum of the
minutes) must not be read 60 times too high: the count of a sum of energy,
starts or working time has to fit one value per hour."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.bosch_buderus_heating.recordings.ledger import CLOUD, Ledger
from custom_components.bosch_buderus_heating.recordings.parse import (
    Rate,
    count_fits,
    parse_cloud,
    parse_local,
)
from custom_components.bosch_buderus_heating.recordings.series import BY_KEY, Quantity
from tests.test_recordings_core import days, hours, recording

Z = ZoneInfo("Europe/Berlin")
HEAT = BY_KEY["total_heat"]
OUTDOOR = BY_KEY["outdoor_temperature"]
DAY = "2026-11-13"
MONTH = "2026-11"


def _cloud(rows: list[dict[str, float]]) -> dict[str, object]:
    return {"type": "yRecording", "unitOfMeasure": "kWh", "recording": rows}


def test_the_hours_of_a_gateway_with_minute_values_are_not_taken() -> None:
    parsed = parse_cloud(_cloud([{"y": 60.0, "c": 60}] * 24), HEAT, DAY, Z)
    assert parsed is not None
    assert parsed.samples == []
    assert parsed.issues["count_form"] == 24


def test_the_days_of_a_gateway_with_minute_values_are_not_taken() -> None:
    parsed = parse_cloud(_cloud([{"y": 1800.0, "c": 1440}] * 30), HEAT, MONTH, Z)
    assert parsed is not None
    assert parsed.samples == []
    assert parsed.issues["count_form"] == 30


def test_the_usual_form_is_taken_and_gaps_stay_gaps() -> None:
    hours = parse_cloud(
        _cloud([{"y": 1.5, "c": 1}] * 23 + [{"y": 1.0, "c": 0}]), HEAT, DAY, Z
    )
    assert hours is not None and len(hours.samples) == 24
    assert not hours.issues["count_form"]
    days = parse_cloud(
        _cloud([{"y": 30.0, "c": 24}] * 29 + [{"y": 1.0, "c": 0}]), HEAT, MONTH, Z
    )
    assert days is not None and len(days.samples) == 30
    assert not days.issues["count_form"]


def test_the_days_of_a_change_of_summer_time_fit() -> None:
    # 2026-10-25 has 25 hours in Europe/Berlin; a plant may report 24 or 25.
    for hours in (24, 25):
        rows = [{"y": 1.0, "c": 24}] * 31
        rows[24] = {"y": 1.0, "c": hours}
        parsed = parse_cloud(_cloud(rows), HEAT, "2026-10", Z)
        assert parsed is not None and len(parsed.samples) == 31


def test_a_temperature_mean_keeps_any_count() -> None:
    payload = {**_cloud([{"y": 600.0, "c": 60}] * 24), "unitOfMeasure": "C"}
    parsed = parse_cloud(payload, OUTDOOR, DAY, Z)
    assert parsed is not None and len(parsed.samples) == 24
    assert not parsed.issues["count_form"]


@pytest.mark.parametrize(
    ("rate", "c", "fits"),
    [
        (Rate.HOUR, 1, True),
        (Rate.HOUR, 0, True),
        (Rate.HOUR, 60, False),
        (Rate.HOUR, 2, False),
        (Rate.DAY, 24, True),
        (Rate.DAY, 1440, False),
        (Rate.DAY, 1, False),
        (Rate.MONTH, 720, True),
    ],
)
def test_the_form_of_the_count(rate: Rate, c: float, fits: bool) -> None:
    assert count_fits(Quantity.ENERGY, rate, c, date(2026, 11, 13), Z) is fits


def test_nothing_of_such_an_answer_reaches_the_statistics_and_it_is_counted() -> None:
    ledger = Ledger(HEAT)
    parsed = parse_cloud(_cloud([{"y": 60.0, "c": 60}] * 24), HEAT, DAY, Z)
    assert parsed is not None
    at = datetime(2026, 11, 14, 12, tzinfo=Z)
    ledger.merge(parsed, CLOUD, read_at=at, zone=Z)
    assert not ledger.hours and not ledger.days
    assert ledger.stats["answer_count_form"] == 24


def test_a_local_answer_with_minute_values_is_not_taken_either() -> None:
    start = datetime(2026, 11, 13, 0, tzinfo=Z)
    group = hours(start, [(60.0, 60)] * 24)
    parsed = parse_local(recording(group), HEAT, Z)
    assert parsed is not None and parsed.samples == []
    assert parsed.issues["count_form"] == 24
    group = days(date(2026, 11, 1), [(1800.0, 1440)] * 5)
    parsed = parse_local(recording(group), HEAT, Z)
    assert parsed is not None and parsed.samples == []
    assert parsed.issues["count_form"] == 5
    usual = parse_local(recording(hours(start, [(1.0, 1)] * 24)), HEAT, Z)
    assert usual is not None and len(usual.samples) == 24


CONSUMED = BY_KEY["total_consumed"]


def test_energy_monitoring_minute_sums_are_divided_by_sixty() -> None:
    """The energy monitoring of a plant without the local data port answers
    the sum of minute values (a reporter compared a day of 420 with the app:
    7 kWh); the form is recognised by its count."""
    day = parse_cloud(_cloud([{"y": 420.0, "c": 1439}] * 30), CONSUMED, MONTH, Z)
    assert day is not None and len(day.samples) == 30
    assert {(s.y, s.c) for s in day.samples} == {(7.0, 24.0)}
    hours = parse_cloud(
        _cloud([{"y": 30.0, "c": 60}] * 23 + [{"y": 1.0, "c": 0}]), CONSUMED, DAY, Z
    )
    assert hours is not None and len(hours.samples) == 24
    assert [(s.y, s.c) for s in hours.samples[:2]] == [(0.5, 1.0), (0.5, 1.0)]
    assert not day.issues["count_form"] and not hours.issues["count_form"]


def test_energy_monitoring_in_the_usual_form_is_unchanged() -> None:
    hours = parse_cloud(_cloud([{"y": 0.5, "c": 1}] * 24), CONSUMED, DAY, Z)
    assert hours is not None
    assert {(s.y, s.c) for s in hours.samples} == {(0.5, 1.0)}
    days = parse_cloud(_cloud([{"y": 12.0, "c": 24}] * 30), CONSUMED, MONTH, Z)
    assert days is not None
    assert {(s.y, s.c) for s in days.samples} == {(12.0, 24.0)}


def test_an_odd_count_of_energy_monitoring_is_still_discarded() -> None:
    days = parse_cloud(_cloud([{"y": 420.0, "c": 5000}] * 30), CONSUMED, MONTH, Z)
    assert days is not None and days.samples == []
    assert days.issues["count_form"] == 30


def test_other_series_are_never_divided() -> None:
    days = parse_cloud(_cloud([{"y": 420.0, "c": 1439}] * 30), HEAT, MONTH, Z)
    assert days is not None and days.samples == []
    assert days.issues["count_form"] == 30
