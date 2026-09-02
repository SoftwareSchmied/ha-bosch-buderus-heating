"""Tests for locally derived heating values."""

from __future__ import annotations

from math import inf, nan

import pytest

from custom_components.bosch_buderus_heating.derived import calculate_dew_point


@pytest.mark.parametrize(
    ("temperature", "humidity", "expected"),
    (
        (25.0, 65.0, 17.96),
        (20.0, 50.0, 9.26),
        (20.0, 100.0, 20.0),
    ),
)
def test_calculate_dew_point(
    temperature: float, humidity: float, expected: float
) -> None:
    calculation = calculate_dew_point(temperature, humidity)

    assert calculation is not None
    assert calculation.room_temperature_celsius == temperature
    assert calculation.relative_humidity_percent == humidity
    assert calculation.dew_point_celsius == pytest.approx(expected)


@pytest.mark.parametrize(
    ("temperature", "humidity"),
    (
        (True, 50.0),
        (20.0, False),
        ("20", 50.0),
        (20.0, "50"),
        (nan, 50.0),
        (20.0, inf),
        (-45.1, 50.0),
        (60.1, 50.0),
        (20.0, 0.0),
        (20.0, 100.1),
        (32767.0, 50.0),
        (20.0, -32768.0),
    ),
)
def test_calculate_dew_point_rejects_invalid_inputs(
    temperature: object, humidity: object
) -> None:
    assert calculate_dew_point(temperature, humidity) is None
