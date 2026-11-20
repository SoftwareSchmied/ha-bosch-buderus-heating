"""A performance factor with very little heat in its period carries a note."""

from __future__ import annotations

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.performance_history import Period
from custom_components.bosch_buderus_heating.performance_note import (
    DAY_LIMIT_KWH,
    LITTLE_HEAT,
    LONG_LIMIT_KWH,
    little_heat_attributes,
)
from custom_components.bosch_buderus_heating.performance_sensors import (
    PerformanceFactorSensor,
)
from tests.test_derived_values import air
from tests.test_performance import _at, _engine, counters, local


@pytest.mark.parametrize(
    ("period", "limit"),
    [
        (Period.TODAY, DAY_LIMIT_KWH),
        (Period.YESTERDAY, DAY_LIMIT_KWH),
        (Period.CURRENT_MONTH, LONG_LIMIT_KWH),
        (Period.PREVIOUS_MONTH, LONG_LIMIT_KWH),
        (Period.CURRENT_YEAR, LONG_LIMIT_KWH),
        (Period.PREVIOUS_YEAR, LONG_LIMIT_KWH),
        (Period.SINCE_COMMISSIONING, LONG_LIMIT_KWH),
    ],
)
def test_the_limit_depends_on_the_period(period: Period, limit: float) -> None:
    assert (DAY_LIMIT_KWH, LONG_LIMIT_KWH) == (5.0, 30.0)
    assert little_heat_attributes(period, limit - 0.01) == {
        "note": LITTLE_HEAT,
        "little_heat_below_kwh": limit,
    }
    assert little_heat_attributes(period, limit) == {}
    assert little_heat_attributes(period, limit + 100) == {}


async def test_the_value_stays_and_the_note_goes_with_more_heat(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 16, 23, 50))
    engine = await _engine(plant)
    counters(plant, 1600.0, 400.0)
    engine.sync()
    plant.later(9)
    counters(plant, 1600.0, 400.0)
    engine.sync()
    plant.later(12)  # 00:11 of the next day
    counters(plant, 1601.2, 400.2)
    engine.sync()
    sensor = PerformanceFactorSensor(plant.coordinator, engine, Period.TODAY, "total")
    sensor._refresh()
    # The factor is shown as before; only an attribute says it is a weak figure.
    assert sensor.native_value == pytest.approx(6.0, abs=0.01)
    attributes = sensor.extra_state_attributes
    assert attributes["note"] == LITTLE_HEAT
    assert attributes["little_heat_below_kwh"] == 5.0
    plant.later(30)
    counters(plant, 1611.2, 402.2)
    engine.sync()
    sensor._refresh()
    assert sensor.native_value == pytest.approx(5.09, abs=0.01)
    assert "note" not in sensor.extra_state_attributes
    assert "little_heat_below_kwh" not in sensor.extra_state_attributes


async def test_an_unknown_factor_gives_its_reason_and_no_note(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 16, 23, 50))
    engine = await _engine(plant)
    counters(plant, 1600.0, 400.0)
    engine.sync()
    plant.later(9)
    counters(plant, 1600.0, 400.0)
    engine.sync()
    plant.later(2)
    counters(plant, 1600.4, 400.07)
    engine.sync()
    sensor = PerformanceFactorSensor(plant.coordinator, engine, Period.TODAY, "total")
    sensor._refresh()
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["reason"] == "too_little_electricity"
    assert "note" not in sensor.extra_state_attributes
