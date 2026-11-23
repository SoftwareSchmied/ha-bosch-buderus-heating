"""Calculated values around a start: COP start-up phase, periods before the
history is read, start of the counters' history (0.9.2 stable, s-derived)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.cop_start import (
    STARTUP_SECONDS,
    StartPhase,
)
from custom_components.bosch_buderus_heating.performance import live_cop
from custom_components.bosch_buderus_heating.performance_history import Period
from custom_components.bosch_buderus_heating.performance_sensors import (
    CurrentCopSensor,
    PerformanceFactorSensor,
)
from custom_components.bosch_buderus_heating.recordings import Period as RPeriod
from custom_components.bosch_buderus_heating.recordings.manager import (
    RecordingsManager,
    Shown,
)
from custom_components.bosch_buderus_heating.recordings.measures import CALCULATED
from custom_components.bosch_buderus_heating.recordings.sensor import (
    RecordingPeriodSensor,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_derived_values import COMPRESSOR, COMPRESSOR_ACTIVE, REFRIGERANT, air
from tests.test_performance import _at, _engine, counters, local, running
from tests.test_recordings_manager import GW, Clock, Driver, key, manager
from tests.test_recordings_manager import local as rlocal
from tests.test_start_counters import (
    recorder_runtime_annotations,  # noqa: F401 - fixture
)

Z = ZoneInfo("Europe/Berlin")


def _stop(plant: Any) -> None:
    plant.local(REFRIGERANT, "idle").flag(COMPRESSOR_ACTIVE, False)
    plant.cloud(COMPRESSOR, "off")


def _start(plant: Any) -> None:
    plant.local(REFRIGERANT, "heating").flag(COMPRESSOR_ACTIVE, True)
    plant.cloud(COMPRESSOR, "heating")


# ------------------------------------------------------------ H-B3-5 (COP)
def test_the_cop_is_unknown_while_the_compressor_starts(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer))
    sensor = CurrentCopSensor(plant.coordinator)
    _stop(plant)
    assert sensor._derive().attributes["reason"] == "compressor_off"
    _start(plant)
    # The heat meter lags: a start-up spike is not shown.
    running(plant, output=8.0, power=580.0)
    result = sensor._derive()
    assert result.value is None
    assert result.attributes["reason"] == "starting"
    assert result.attributes["electrical_power_kw"] == pytest.approx(0.58)
    freezer.tick(timedelta(seconds=STARTUP_SECONDS - 1))
    assert sensor._derive().attributes["reason"] == "starting"
    freezer.tick(timedelta(seconds=2))
    running(plant, output=3.4, power=634.0)
    assert sensor._derive().value == pytest.approx(5.36, abs=0.01)


def test_a_start_below_the_minimum_power_is_starting_not_missing_inputs(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer))
    sensor = CurrentCopSensor(plant.coordinator)
    _stop(plant)
    sensor._derive()
    _start(plant)
    running(plant, output=0.0, power=60.0)
    assert sensor._derive().attributes["reason"] == "starting"


def test_every_new_start_holds_the_cop_back_again(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer))
    sensor = CurrentCopSensor(plant.coordinator)
    _stop(plant)
    sensor._derive()
    _start(plant)
    sensor._derive()
    freezer.tick(timedelta(seconds=STARTUP_SECONDS + 1))
    assert sensor._derive().value is not None
    _stop(plant)
    assert sensor._derive().attributes["reason"] == "compressor_off"
    _start(plant)
    assert sensor._derive().attributes["reason"] == "starting"


def test_a_run_found_in_progress_is_not_held_back(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """After a restart of Home Assistant a running compressor shows its COP
    at once: its start is not known."""
    plant = running(air(hass, freezer))
    sensor = CurrentCopSensor(plant.coordinator)
    assert sensor._derive().value == pytest.approx(5.36, abs=0.01)


def test_no_zero_cop_at_the_end_of_a_run(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The compressor still draws power, no heat flows: unknown, never 0.0."""
    plant = running(air(hass, freezer))
    sensor = CurrentCopSensor(plant.coordinator)
    assert sensor._derive().value is not None
    running(plant, output=0.0, power=300.0)
    result = sensor._derive()
    assert result.value is None
    assert result.attributes["reason"] == "no_heat_output"
    running(plant, output=-0.2, power=300.0)
    assert sensor._derive().value is None


def test_without_a_phase_the_cop_is_not_held_back(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer))
    _stop(plant)
    live_cop(plant.coordinator)
    _start(plant)
    assert live_cop(plant.coordinator).value is not None


def test_the_start_phase_ignores_missing_readings() -> None:
    phase = StartPhase()
    start = datetime(2026, 11, 19, 10, tzinfo=UTC)
    assert phase.observe(False, start) is False
    assert phase.observe(True, start) is True
    # No reading changes nothing, in either direction.
    assert phase.observe(None, start + timedelta(seconds=10)) is True
    late = start + timedelta(seconds=STARTUP_SECONDS)
    assert phase.observe(None, late) is False
    assert phase.observe(True, late) is False


# ------------------------------------------------------- H-B3-3 (periods)
async def test_month_and_year_stay_unknown_until_the_history_is_read(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 18, 10, 24))
    engine = await _engine(plant)
    heat, power = 1600.0, 400.0
    counters(plant, heat, power)
    engine.sync()
    # The day started with 20 kWh heat and 3.2 kWh electricity ago.
    engine._baseline_day = engine.today()
    engine._baseline = {"total": (heat - 20.0, power - 3.2)}
    engine._baseline_method = "statistics"
    sensors = {
        period: PerformanceFactorSensor(plant.coordinator, engine, period, "total")
        for period in (
            Period.TODAY,
            Period.CURRENT_MONTH,
            Period.CURRENT_YEAR,
            Period.SINCE_COMMISSIONING,
        )
    }
    for sensor in sensors.values():
        sensor._refresh()
    # Today and since commissioning are known; month and year are not the
    # value of today.
    assert sensors[Period.TODAY].native_value == pytest.approx(6.25, abs=0.01)
    assert sensors[Period.SINCE_COMMISSIONING].native_value == pytest.approx(4.0)
    for period in (Period.CURRENT_MONTH, Period.CURRENT_YEAR):
        assert sensors[period].native_value is None
        assert sensors[period].extra_state_attributes == {"reason": "history_loading"}
    assert "history_loading" not in str(sensors[Period.TODAY].extra_state_attributes)
    assert engine.diagnostics()["history_read"] is False
    await engine.async_refresh(full=True)
    assert engine.history_read and engine.diagnostics()["history_read"] is True
    for sensor in sensors.values():
        sensor._refresh()
    # The history is read (here nothing but today): the values follow.
    assert sensors[Period.CURRENT_MONTH].native_value == pytest.approx(6.25, abs=0.01)
    assert "reason" not in (sensors[Period.CURRENT_YEAR].extra_state_attributes or {})


# ------------------------------------------------------ H-B3-4 (history)
def _counter_statistics(hass: HomeAssistant, entity_id: str, first: date) -> None:
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import async_import_statistics

    rows = []
    total = 100.0
    day = first
    while day <= date(2026, 11, 13):
        for hour in (0, 12):
            total += 5.0
            moment = datetime(day.year, day.month, day.day, hour, tzinfo=Z)
            rows.append(
                StatisticData(start=moment.astimezone(UTC), sum=total, state=total)
            )
        day += timedelta(days=1)
    async_import_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_class="energy",
            unit_of_measurement="kWh",
        ),
        rows,
    )


async def _calculated_year(hass: HomeAssistant, first: date) -> tuple[Any, Any, str]:
    from tests.test_recordings_recorder import _rows

    clock = Clock(rlocal(2026, 11, 14, 10) + timedelta(minutes=20))
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    counter = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{GW}:gateway:heating_heat_produced", config_entry=entry
    )
    _counter_statistics(hass, counter.entity_id, first)
    await _rows(hass, counter.entity_id)
    rec = await manager(hass, clock, [Driver(clock)], entry=entry)
    name = key("ch_heat", RPeriod.CURRENT_YEAR, CALCULATED)
    rec.request(name)
    await rec.async_run("start")
    spec = next(item for item in rec.specs() if item.key == name)
    return rec, spec, name


def _attributes(rec: Any, spec: Any) -> dict[str, Any]:
    sensor = SimpleNamespace(_manager=rec, _spec=spec)
    sensor._heat_pump_recorded = lambda: RecordingPeriodSensor._heat_pump_recorded(
        sensor  # type: ignore[arg-type]
    )
    return RecordingPeriodSensor.extra_state_attributes.fget(sensor)  # type: ignore[attr-defined,no-any-return]


async def test_a_calculated_year_names_where_the_counter_history_starts(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    rec, spec, name = await _calculated_year(hass, date(2026, 7, 1))
    shown = rec.shown(spec)
    assert shown is not None and shown.source == "counters"
    assert shown.history_start == date(2026, 7, 1)
    assert len(rec.published[name]) == 6
    attributes = _attributes(rec, spec)
    assert attributes["counter_history_start"] == "2026-07-01"
    assert attributes["period_start"] == "2026-01-01"
    assert "counter_history_start" in RecordingPeriodSensor._unrecorded_attributes
    # The answer is kept per period: the next run does not ask again.
    assert rec._counter_first[(spec.measure.key, date(2026, 1, 1))] == date(2026, 7, 1)


async def test_a_counter_history_from_the_start_of_the_period_adds_nothing(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    rec, spec, name = await _calculated_year(hass, date(2026, 1, 1))
    shown = rec.shown(spec)
    assert shown is not None and shown.history_start is None
    assert len(rec.published[name]) == 5
    assert "counter_history_start" not in _attributes(rec, spec)


def test_an_old_published_value_without_the_start_is_still_read() -> None:
    rec = SimpleNamespace(
        published={"x": ["2026-01-01", "2027-01-01", 3.0, "counters", "hourly"]},
        _missing_parts={},
    )
    shown = RecordingsManager.shown(rec, SimpleNamespace(key="x"))  # type: ignore[arg-type]
    assert isinstance(shown, Shown) and shown.history_start is None
