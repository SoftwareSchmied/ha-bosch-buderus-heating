"""Calculated values stay right with outliers and a source falling back.

A single counter reading far below the last one (for example a whole
collection read as 0) is held until a later reading confirms it, as for the
shown counters: the net heat counter, a defrost's energy and the start of a
day of the performance factors ignore it. A defrost source that falls back
to an older reading (the cloud's, while the local access drops out) neither
ends nor starts a defrost. Unchanged counters before midnight still start
the new day at midnight.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.counter_drop import DropHold, large_drop
from custom_components.bosch_buderus_heating.derived_sensors import (
    NetHeatCounterSensor,
)
from custom_components.bosch_buderus_heating.performance_history import Period
from tests.test_derived_values import (
    _COORDINATORS,
    CH,
    COMPRESSOR,
    DHW,
    REFRIGERANT,
    TOTAL,
    _monitor,
    air,
)
from tests.test_performance import _at, _engine, counters, local


@pytest.fixture(autouse=True)
async def _stop_coordinators() -> AsyncGenerator[None]:
    yield
    while _COORDINATORS:
        await _COORDINATORS.pop().async_shutdown()


async def _net(hass: HomeAssistant, plant) -> NetHeatCounterSensor:
    sensor = NetHeatCounterSensor(plant.coordinator, CH)
    sensor.hass = hass
    await sensor.async_added_to_hass()
    return sensor


def test_a_drop_is_held_until_a_later_reading_confirms_it() -> None:
    from datetime import UTC, datetime, timedelta

    hold = DropHold()
    at = datetime(2026, 11, 15, tzinfo=UTC)
    assert hold.held("a", True, at)
    assert hold.held("a", True, at)  # the same reading again
    assert not hold.held("a", True, at + timedelta(minutes=5))  # confirmed
    assert hold.held("a", True, at + timedelta(minutes=6))  # a new drop
    assert not hold.held("a", False, at)  # no drop ends the hold
    assert hold.held("a", True, at + timedelta(minutes=7))
    assert not hold.held("a", True, None)  # without a time: the next one
    assert large_drop(0.0, 1234.0)
    assert not large_drop(1200.0, 1234.0)  # within 10 %
    assert not large_drop(5.0, 10.0, at_least=10.0)


async def test_a_single_zero_reading_counts_no_net_heat(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    sensor = await _net(hass, plant)
    shown = []
    for value in (1234.16, 0.0, 1234.30):
        plant.later(5).energy(CH, value)
        sensor._refresh()
        shown.append(sensor.native_value)
    assert shown == [1234.16, 1234.16, pytest.approx(1234.30)]


async def test_a_replaced_counter_counts_on_after_a_second_reading(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    sensor = await _net(hass, plant)
    shown = []
    for value in (1234.0, 3.0, 3.0, 4.5):
        plant.energy(CH, value)
        sensor._refresh()  # the same reading again: still held
        plant.later(5)
        shown.append(sensor.native_value)
    # 3.0 is held, the next reading confirms it and counting goes on from it.
    assert shown == [1234.16, 1234.16, 1234.16, pytest.approx(1235.66)]


async def test_a_single_low_heat_reading_is_no_defrost_dip(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.stale_cloud(DHW)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "defrost")
    monitor.update(plant.at)
    plant.later(2).energy(CH, 0.0)  # one outlier
    monitor.update(plant.at)
    plant.later(2).energy(CH, 1233.9)
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "heating")
    monitor.update(plant.at)
    plant.later(3).energy(CH, 1234.2)
    monitor.update(plant.at)
    assert len(events) == 1
    assert events[0]["heat_withdrawn_kwh"] == pytest.approx(0.26)
    assert monitor.totals["heat_withdrawn_kwh"] == pytest.approx(0.26)


async def test_an_electricity_outlier_gives_no_defrost_electricity(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The total counter read as 0 at the start: the electricity of the
    defrost is unknown, never the whole meter."""
    plant = air(hass, freezer)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "defrost").energy(TOTAL, 0.0, 0.0)
    monitor.update(plant.at)
    plant.later(4).local(REFRIGERANT, "heating").energy(TOTAL, 1614.40, 416.32)
    monitor.update(plant.at)
    for _ in range(12):
        plant.later(1)
        monitor.update(plant.at)
    assert len(events) == 1
    assert events[0]["electricity_kwh"] is None
    assert monitor.totals["electricity_kwh"] == 0.0


async def test_an_older_cloud_reading_does_not_split_a_defrost(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """One real defrost; the local refrigerant status is missing for one
    cycle, the cloud compressor status (read before the start) still says
    heating."""
    plant = air(hass, freezer)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "defrost")
    monitor.update(plant.at)
    plant.later(1).forget(REFRIGERANT)  # local missing one cycle
    monitor.update(plant.at)
    assert monitor.active is True  # the shown state stays
    plant.later(1).local(REFRIGERANT, "defrost")  # still the same defrost
    monitor.update(plant.at)
    plant.later(3).local(REFRIGERANT, "heating")
    monitor.update(plant.at)
    for _ in range(12):
        plant.later(1)
        monitor.update(plant.at)
    assert monitor.totals["count"] == 1
    assert [event["duration_min"] for event in events] == [5.0]


async def test_an_older_cloud_reading_starts_no_phantom_defrost(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The cloud read the compressor status during the defrost; after the
    local end the local access drops for one cycle."""
    plant = air(hass, freezer)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "defrost")
    monitor.update(plant.at)
    plant.later(4).cloud(COMPRESSOR, "defrost")  # cloud poll during the defrost
    monitor.update(plant.at)
    plant.later(0.5).local(REFRIGERANT, "heating")  # the defrost ends
    monitor.update(plant.at)
    plant.later(0.5).forget(REFRIGERANT)  # local gone, cloud still "defrost"
    monitor.update(plant.at)
    assert monitor.active is False
    plant.later(1).local(REFRIGERANT, "heating")
    monitor.update(plant.at)
    for _ in range(12):
        plant.later(1)
        monitor.update(plant.at)
    assert monitor.totals["count"] == 1


async def test_unchanged_counters_before_midnight_start_the_day(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The heat pump stands still for the last 2 h of the day; its counters
    are read every 5 minutes up to midnight."""
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 14, 22, 0))
    engine = await _engine(plant)
    for _ in range(24):  # 22:00 .. 23:55, unchanged
        counters(plant, 1600.0, 400.0)
        engine.sync()
        plant.later(5)
    counters(plant, 1600.0, 400.0)  # 00:00, the first reading of the day
    engine.sync()
    assert engine._baseline_method == "midnight"
    assert engine._baseline["total"] == pytest.approx((1600.0, 400.0))


async def test_a_zero_outlier_before_midnight_is_no_start_of_the_day(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 14, 23, 45))
    engine = await _engine(plant)
    counters(plant, 1600.0, 400.0)
    engine.sync()
    plant.later(10)
    counters(plant, 0.0, 0.0)  # 23:55: one collection read as 0
    engine.sync()
    plant.later(5)
    counters(plant, 1600.3, 400.07)  # 00:00
    engine.sync()
    assert engine._baseline_method == "midnight"
    assert engine._baseline["total"] == pytest.approx((1600.0, 400.0))
    plant.later(60)
    counters(plant, 1601.6, 400.37)
    engine.sync()
    result = engine.value(Period.TODAY, "total")
    assert result is not None
    assert result.heat == pytest.approx(1.6)
    # A replaced meter: confirmed by the next reading, then counted from.
    plant.later(5)
    counters(plant, 2.0, 0.5)
    engine.sync()
    assert engine._live["total"].heat == pytest.approx(1601.6)
    plant.later(5)
    counters(plant, 2.0, 0.5)
    engine.sync()
    assert engine._live["total"].heat == pytest.approx(2.0)
