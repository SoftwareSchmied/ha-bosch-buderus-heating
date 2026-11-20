"""Calculated performance values: current COP, performance factors, spreads.

Synthetic plants shaped like the calculated-values tests (an air heat pump
with local refrigerant readings and cloud counters, a brine-to-water unit
with a brine circuit), a stand-in for the plant's recordings and, in
``test_performance_recorder``, Home Assistant's recorder. The last test
replays one day of a real installation: the live heat output and electrical
power, integrated over 24 h, against the counters' increments.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import (
    derived_store as derived_store_module,
)
from custom_components.bosch_buderus_heating import performance_history
from custom_components.bosch_buderus_heating.derived_store import derived_store
from custom_components.bosch_buderus_heating.performance import (
    HEAT_OUTPUT,
    MAX_RATIO,
    brine_spread,
    live_cop,
    primary_spread,
    ratio,
)
from custom_components.bosch_buderus_heating.performance_history import (
    PerformanceHistory,
    Period,
    add_months,
    bounds,
)
from custom_components.bosch_buderus_heating.performance_sensors import (
    BrineSpreadSensor,
    CurrentCopSensor,
    PerformanceFactorSensor,
    PrimarySpreadSensor,
    _candidates,
    factor_name,
)
from custom_components.bosch_buderus_heating.recordings.ledger import DailyValue
from custom_components.bosch_buderus_heating.recordings.periods import PeriodValue
from tests.ha_test_helpers import flush_store
from tests.test_derived_values import (
    BRINE_IN,
    COMPRESSOR,
    COMPRESSOR_ACTIVE,
    POWER,
    REFRIGERANT,
    TOTAL,
    Plant,
    air,
    brine,
)

Z = ZoneInfo("Europe/Berlin")
HEATER = "/heatSources/eHeater/powerElecActual"
TC1 = "/heatSources/additionalHeater/flowTemp"
TC0 = "/heatSources/returnTemperature"
PC0_RELAY = "/signals/SRC.CUHP.HP1.CHPumpRelay"
PC0_FLOW = "/heatSources/hs1/pumpVolumeFlow"
PC1_FLOW = "/signals/SRC.HC1.FlowCtrl.PumpVolumeFlow"
BRINE_OUT = BRINE_IN  # into the collector = out of the heat pump
BRINE_INLET = "/heatSources/hs1/brineCircuit/collectorOutflowTemp"
BRINE_PUMP = "/heatSources/hs1/brineCircuit/pumpSpeed"
FIXTURE = Path(__file__).parent / "fixtures" / "performance_day.json"


@pytest.fixture(autouse=True)
async def _stop_engines(hass: HomeAssistant) -> AsyncGenerator[None]:
    yield
    for engine in hass.data.get(performance_history._ENGINES, {}).values():
        engine.stop()


def local(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=Z)  # type: ignore[misc]


def running(plant: Plant, *, output: float = 3.4, power: float = 634.0) -> Plant:
    plant.local(HEAT_OUTPUT, output, unitOfMeasure="kW")
    plant.local(POWER, power, unitOfMeasure="W")
    plant.local(HEATER, 0.0, unitOfMeasure="W")
    return plant


def counters(plant: Plant, heat: float, electricity: float, mode: str = TOTAL) -> None:
    plant.cloud(
        mode,
        values=({"outputProduced": heat, "compressor": electricity, "eheater": 0.0},),
    )


# -------------------------------------------------------------- current COP


def test_current_cop_while_the_compressor_runs(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer))
    result = live_cop(plant.coordinator)
    assert result.value == pytest.approx(3.4 / 0.634, abs=0.01)
    attributes = result.attributes
    assert attributes["heat_output_kw"] == pytest.approx(3.4)
    assert attributes["compressor_power_kw"] == pytest.approx(0.634)
    assert attributes["heater_power_kw"] == 0.0
    assert attributes["side"] == "circuit"
    assert "reason" not in attributes
    derived = attributes["derived_from"]
    assert isinstance(derived, dict)
    assert derived[HEAT_OUTPUT] == "local"
    assert derived[POWER] == "local"
    assert derived[HEATER] == "local"
    # The heater's power counts in the electricity and, one to one, in the
    # heat.
    plant.local(HEATER, 2000.0, unitOfMeasure="W")
    assert live_cop(plant.coordinator).value == pytest.approx(
        (3.4 + 2.0) / (0.634 + 2.0), abs=0.01
    )


def test_current_cop_is_unknown_without_a_running_compressor(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer))
    coordinator = plant.coordinator
    plant.local(REFRIGERANT, "defrost")
    result = live_cop(coordinator)
    assert result.value is None and result.attributes["reason"] == "defrosting"
    plant.local(REFRIGERANT, "idle").flag(COMPRESSOR_ACTIVE, False)
    plant.cloud(COMPRESSOR, "off")
    result = live_cop(coordinator)
    assert result.value is None and result.attributes["reason"] == "compressor_off"
    plant.local(REFRIGERANT, "heating").flag(COMPRESSOR_ACTIVE, True)
    plant.cloud(COMPRESSOR, "heating")
    assert live_cop(coordinator).value is not None


def test_current_cop_needs_every_input_and_a_plausible_ratio(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer))
    coordinator = plant.coordinator
    plant.forget(HEAT_OUTPUT)
    assert live_cop(coordinator).attributes["reason"] == "inputs_missing"
    running(plant).forget(HEATER)
    # No heater reading offered at all: nothing to add.
    assert live_cop(coordinator).value == pytest.approx(5.36, abs=0.01)
    plant.local(HEATER, 5.0, unitOfMeasure="A")  # no power unit
    # A heater the plant offers without a current reading: no COP.
    assert live_cop(coordinator).attributes["reason"] == "inputs_missing"
    running(plant, power=60.0)  # starting up
    assert live_cop(coordinator).attributes["reason"] == "inputs_missing"
    running(plant, power=200.0)  # 3.4 / 0.2 = 17
    assert live_cop(coordinator).attributes["reason"] == "implausible"
    running(plant, output=2.9, power=200.0)  # 14.5: above 10 as well
    assert live_cop(coordinator).attributes["reason"] == "implausible"
    running(plant, output=1.9, power=200.0)  # 9.5 is still shown
    assert live_cop(coordinator).value == pytest.approx(9.5)
    # The period ratios keep their limit.
    assert MAX_RATIO == 15.0


def test_brine_unit_cop_notes_the_brine_pump(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(brine(hass, freezer))
    result = live_cop(plant.coordinator)
    assert result.value is not None
    assert result.attributes["brine_pump_included"] is False
    plant.local(REFRIGERANT, "defrost")
    assert live_cop(plant.coordinator).attributes.get("reason") != "defrosting"


def test_current_cop_sensor(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer))
    sensor = CurrentCopSensor(plant.coordinator)
    sensor._result = sensor._derive()
    assert sensor.native_value == pytest.approx(5.36, abs=0.01)
    assert sensor.unique_id == "gateway-one:heatSources:calculated_cop"
    assert sensor.state_class == "measurement"
    assert sensor.entity_registry_enabled_default
    assert HEAT_OUTPUT in sensor.extra_resource_paths
    assert "derived_from" in sensor._unrecorded_attributes


# ------------------------------------------------------------------ spreads


def spreading(plant: Plant, *, pc0: float = 1101, pc1: float = 1269.0) -> Plant:
    plant.local(TC1, 25.9, unitOfMeasure="C")
    plant.cloud(TC0, 22.7, unit="°C")
    plant.flag(PC0_RELAY, True)
    plant.local(PC0_FLOW, round(pc0), unitOfMeasure="l/h", kind="integerValue")
    plant.local(PC1_FLOW, pc1, unitOfMeasure="l/h")
    return plant


def test_primary_spread_while_the_primary_pump_runs(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = spreading(air(hass, freezer))
    result = primary_spread(plant.coordinator)
    assert result.value == pytest.approx(3.2)
    attributes = result.attributes
    assert attributes["supply_temperature"] == pytest.approx(25.9)
    assert attributes["return_temperature"] == pytest.approx(22.7)
    assert attributes["return_is_circuit_return"] is True
    derived = attributes["derived_from"]
    assert isinstance(derived, dict)
    assert derived[TC1] == "local" and derived[TC0] == "cloud"
    assert derived[PC0_RELAY] == "local"
    # PC0 moves more than PC1: TC0 mixes in the supply through the buffer.
    spreading(plant, pc0=1300.0, pc1=1269.0)
    assert (
        primary_spread(plant.coordinator).attributes["return_is_circuit_return"]
        is False
    )
    spreading(plant, pc0=1269.0, pc1=1269.0)
    assert (
        primary_spread(plant.coordinator).attributes["return_is_circuit_return"] is True
    )
    plant.forget(PC1_FLOW)
    assert (
        primary_spread(plant.coordinator).attributes["return_is_circuit_return"] is None
    )


def test_primary_spread_is_unknown_while_the_pump_stands(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = spreading(air(hass, freezer))
    plant.flag(PC0_RELAY, False)
    result = primary_spread(plant.coordinator)
    assert result.value is None and result.attributes["reason"] == "pump_off"
    # Without the relay the flow decides.
    plant.forget(PC0_RELAY).local(PC0_FLOW, 0, unitOfMeasure="l/h")
    assert primary_spread(plant.coordinator).attributes["reason"] == "pump_off"
    plant.local(PC0_FLOW, 900, unitOfMeasure="l/h")
    assert primary_spread(plant.coordinator).value == pytest.approx(3.2)
    plant.forget(PC0_FLOW)
    plant.forget(PC1_FLOW)
    result = primary_spread(plant.coordinator)
    assert result.value is None and result.attributes["reason"] == "inputs_missing"


def test_brine_spread_inlet_minus_outlet(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = brine(hass, freezer)
    plant.cloud(BRINE_INLET, 9.5, unit="°C")
    plant.local(BRINE_PUMP, 60, unitOfMeasure="%")
    result = brine_spread(plant.coordinator, "/heatSources/hs1")
    assert result.value == pytest.approx(3.0)  # 9.5 in, 6.5 out
    plant.local(BRINE_PUMP, 0, unitOfMeasure="%")
    assert brine_spread(plant.coordinator, "/heatSources/hs1").value is None
    # Without a pump reading the compressor decides.
    plant.forget(BRINE_PUMP)
    assert brine_spread(plant.coordinator, "/heatSources/hs1").value == 3.0
    plant.flag(COMPRESSOR_ACTIVE, False).local(REFRIGERANT, "idle")
    plant.cloud(COMPRESSOR, "off")
    assert brine_spread(plant.coordinator, "/heatSources/hs1").value is None


# ------------------------------------------------------------------ entities


def _created(plant: Plant) -> dict[str, Any]:
    return {key: create() for key, create in _candidates(plant.coordinator)}


async def test_the_sensors_their_ids_and_defaults(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = spreading(running(air(hass, freezer)))
    created = _created(plant)
    assert isinstance(created.pop("cop"), CurrentCopSensor)
    assert isinstance(created.pop("spread"), PrimarySpreadSensor)
    factors = {key: entity for key, entity in created.items()}
    assert len(factors) == 21  # 3 modes x 7 periods
    assert all(isinstance(item, PerformanceFactorSensor) for item in factors.values())
    enabled = sorted(
        key for key, item in factors.items() if item.entity_registry_enabled_default
    )
    # D-DEF: every period of the total, no mode.
    assert enabled == [
        "factor:total:current_month",
        "factor:total:current_year",
        "factor:total:previous_month",
        "factor:total:previous_year",
        "factor:total:since_commissioning",
        "factor:total:today",
        "factor:total:yesterday",
    ]
    today = factors["factor:total:today"]
    assert today.unique_id == (
        "gateway-one:heatSources:emon:totalConsumption"
        ":calculated_performance_factor_today"
    )
    assert today.state_class == "measurement"
    assert today.native_unit_of_measurement is None
    assert factor_name("en", Period.CURRENT_YEAR, "ch") == (
        "Heating \N{EN DASH} Performance factor this year (calculated)"
    )
    assert factor_name("de", Period.TODAY, "total") == (
        "Gesamt \N{EN DASH} Arbeitszahl heute (berechnet)"
    )
    assert factor_name("fr", Period.SINCE_COMMISSIONING, "dhw") == (
        "Eau chaude \N{EN DASH} Facteur de performance depuis la mise en service"
        " (calculé)"
    )


async def test_brine_spread_sensor_only_on_brine_units(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = brine(hass, freezer)
    plant.cloud(BRINE_INLET, 9.5, unit="°C")
    created = _created(plant)
    sensor = created["brine:/heatSources/hs1"]
    assert isinstance(sensor, BrineSpreadSensor)
    assert sensor.entity_registry_enabled_default is False
    assert sensor.unique_id == (
        "gateway-one:heatSources:hs1:brineCircuit:calculated_brine_spread"
    )
    assert not any(key.startswith("brine:") for key in _created(air(hass, freezer)))


# -------------------------------------------------------------------- engine


class View:
    """A stand-in for the plant's recordings (``RecordingsView``)."""

    loaded = True

    def __init__(
        self,
        days: dict[str, dict[date, float]],
        months: dict[str, dict[date, float]] | None = None,
        source: str = "local",
        open_days: frozenset[date] = frozenset(),
    ) -> None:
        self.days = days
        self.months = months or {}
        self.source = source
        self.open_days = open_days
        self.listeners: list[Callable[[], None]] = []
        self.acquired = 0

    def measure_keys(self) -> tuple[str, ...]:
        return tuple({*self.days, *self.months})

    def daily(self, key: str, start: date, end: date) -> tuple[DailyValue, ...]:
        return tuple(
            DailyValue(day, value, 1.0, day not in self.open_days, self.source)
            for day, value in sorted(self.days.get(key, {}).items())
            if start <= day <= end
        )

    def monthly(self, key: str, first: date, today: date) -> PeriodValue | None:
        value = self.months.get(key, {}).get(first)
        if value is None:
            return None
        return PeriodValue(
            first,
            add_months(first, 1),
            value,
            1.0,
            True,
            "monthly",
            frozenset({self.source}),
        )

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: None

    def acquire(self) -> Callable[[], None]:
        self.acquired += 1
        return lambda: None


async def _engine(plant: Plant) -> PerformanceHistory:
    await plant.hass.config.async_update(time_zone="Europe/Berlin")
    engine = PerformanceHistory(plant.hass, plant.coordinator)
    await engine.async_start(schedule=False)
    return engine


def _at(plant: Plant, moment: datetime) -> Plant:
    plant.at = moment.astimezone(UTC)
    plant.freezer.move_to(plant.at)
    return plant


def _span(first: date, last: date, value: float) -> dict[date, float]:
    return {
        first + timedelta(days=offset): value
        for offset in range((last - first).days + 1)
    }


async def test_today_starts_at_the_last_reading_before_midnight(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 14, 23, 40))
    engine = await _engine(plant)
    heat, power = 1600.0, 400.0
    for _ in range(4):  # 23:40 to 23:55, every five minutes
        counters(plant, heat, power)
        engine.sync()
        heat, power = heat + 0.3, power + 0.07
        plant.later(5)
    # 00:00 local: the first reading of the day.
    counters(plant, heat, power)
    engine.sync()
    assert engine._baseline_method == "midnight"
    assert engine._baseline["total"] == pytest.approx((1600.9, 400.21))
    for _ in range(12):  # one hour
        plant.later(5)
        heat, power = heat + 0.3, power + 0.07
        counters(plant, heat, power)
        engine.sync()
    result = engine.value(Period.TODAY, "total")
    assert result is not None
    assert result.heat == pytest.approx(0.3 * 13)
    assert result.electricity == pytest.approx(0.07 * 13)
    assert result.ratio == pytest.approx(4.29, abs=0.01)
    assert result.heat_sources == {"counters"}
    # The start of a day is no start of the next one.
    assert engine.value(Period.TODAY, "total", local(2026, 11, 16, 0, 30)) is None
    # A restart the same day keeps the start of the day.
    await flush_store(derived_store(hass)._store)
    engine.stop()
    hass.data.pop(derived_store_module._STORE)
    again = await _engine(plant)
    # The saved readings are no current counters.
    assert again.value(Period.SINCE_COMMISSIONING, "total") is None
    again.sync()
    restarted = again.value(Period.TODAY, "total")
    assert restarted is not None
    assert restarted.heat == pytest.approx(result.heat)
    assert again._baseline_method == "midnight"


async def test_a_long_gap_over_midnight_leaves_today_unknown(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Without the recorder, a first reading 40 minutes after the last one
    of the day before is no start of the day."""
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 14, 23, 40))
    engine = await _engine(plant)
    counters(plant, 1600.0, 400.0)
    engine.sync()
    plant.later(40)
    counters(plant, 1602.0, 400.5)
    engine.sync()
    assert engine._baseline_method is None
    assert engine.value(Period.TODAY, "total") is None
    month = engine.value(Period.CURRENT_MONTH, "total")
    assert month is None  # no day known at all
    # Since commissioning comes straight from the counters.
    lifetime = engine.value(Period.SINCE_COMMISSIONING, "total")
    assert lifetime is not None and lifetime.ratio == pytest.approx(4.0, abs=0.01)


async def test_the_recordings_fill_the_days_and_months(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch
) -> None:
    """The total from the recordings; a month without all its days from the
    plant's month value; days without a value count as missing."""
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 15, 12, 0))
    first = date(2026, 7, 27)
    yesterday = date(2026, 11, 14)
    days = {
        "total_heat": _span(first, yesterday, 40.0),
        "total_electricity": _span(first, yesterday, 10.0),
        "ch_heat": _span(first, yesterday, 30.0),
        "ch_electricity": _span(first, yesterday, 6.0),
    }
    # A gap of two days in October.
    for key in days:
        days[key].pop(date(2026, 10, 5))
        days[key].pop(date(2026, 10, 6))
    view = View(
        days,
        months={
            "total_heat": {date(2026, 7, 1): 900.0},
            "total_electricity": {date(2026, 7, 1): 300.0},
        },
    )
    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.recordings.recordings_view",
        lambda _hass, _gateway: view,
    )
    engine = await _engine(plant)
    await engine.async_refresh(full=True)
    assert view.acquired == 1 and view.listeners
    yesterday_value = engine.value(Period.YESTERDAY, "total")
    assert yesterday_value is not None
    assert yesterday_value.ratio == 4.0
    assert yesterday_value.heat_sources == {"local_recording"}
    year = engine.value(Period.CURRENT_YEAR, "total")
    assert year is not None
    # July from the month value; August to yesterday from the days without
    # the gap; today unknown (no start of the day).
    known = (yesterday - date(2026, 8, 1)).days + 1 - 2
    assert year.heat == pytest.approx(900.0 + known * 40.0)
    assert year.electricity == pytest.approx(300.0 + known * 10.0)
    assert year.missing_days == 2 + 1
    assert year.days == 31 + known
    assert year.first_day == date(2026, 7, 1)
    october = engine.value(Period.PREVIOUS_MONTH, "total", local(2026, 11, 3, 9))
    assert october is not None and october.missing_days == 2
    # Heating has no month value: July counts from the first recorded day.
    heating = engine.value(Period.CURRENT_YEAR, "ch")
    assert heating is not None and heating.first_day == first
    assert heating.ratio == pytest.approx(5.0)
    # Hot water: nothing recorded.
    assert engine.value(Period.CURRENT_YEAR, "dhw") is None
    previous = engine.value(Period.PREVIOUS_YEAR, "total")
    assert previous is None


async def test_a_day_still_open_in_the_recordings_is_not_used(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch
) -> None:
    """A day the recordings have not confirmed yet (a running value from
    the start) is no day's value: the counters stand in, or it is missing."""
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 15, 12, 0))
    yesterday = date(2026, 11, 14)
    days = {
        "total_heat": _span(date(2026, 11, 1), yesterday, 40.0),
        "total_electricity": _span(date(2026, 11, 1), yesterday, 10.0),
    }
    days["total_heat"][yesterday] = 12.0  # read at the start, never final
    view = View(days, open_days=frozenset({yesterday}))
    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.recordings.recordings_view",
        lambda _hass, _gateway: view,
    )
    engine = await _engine(plant)
    await engine.async_refresh(full=True)
    assert engine.value(Period.YESTERDAY, "total") is None
    month = engine.value(Period.CURRENT_MONTH, "total")
    assert month is not None
    assert month.heat == pytest.approx(13 * 40.0)
    assert month.ratio == pytest.approx(4.0)
    assert month.missing_days == 2  # yesterday and today


async def test_a_month_value_only_for_an_incomplete_past_month(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 15, 12, 0))
    full = {
        "total_heat": _span(date(2026, 10, 1), date(2026, 11, 14), 40.0),
        "total_electricity": _span(date(2026, 10, 1), date(2026, 11, 14), 10.0),
    }
    view = View(
        full,
        months={
            "total_heat": {date(2026, 10, 1): 5000.0},
            "total_electricity": {date(2026, 10, 1): 1.0},
        },
    )
    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.recordings.recordings_view",
        lambda _hass, _gateway: view,
    )
    engine = await _engine(plant)
    await engine.async_refresh(full=True)
    october = engine.value(Period.PREVIOUS_MONTH, "total", local(2026, 11, 2, 9))
    assert october is not None
    # Every day is known: the days count, not the month value.
    assert october.heat == pytest.approx(31 * 40.0)
    assert october.ratio == 4.0
    # Yesterday on the first of a month: a missing last day stays missing,
    # never the whole month value.
    del full["total_heat"][date(2026, 10, 31)]
    view.days["total_heat"][date(2026, 10, 20)] = -5.0  # no real value
    await engine.async_refresh(full=True)
    first = local(2026, 11, 1, 9)
    assert engine.value(Period.YESTERDAY, "total", first) is None
    october = engine.value(Period.PREVIOUS_MONTH, "total", first)
    assert october is not None and october.heat == pytest.approx(5000.0)
    assert date(2026, 10, 20) not in engine._recorded[("total", "heat")]


def test_ratio_limits() -> None:
    assert ratio(4.0, 1.0) == 4.0
    assert ratio(1.0, 0.09) is None
    assert ratio(1.0, 0.1) == 10.0
    assert ratio(16.0, 1.0) is None
    assert ratio(15.0, 1.0) == 15.0
    assert ratio(-1.0, 1.0) is None
    assert ratio(None, 1.0) is None


def test_period_bounds() -> None:
    today = date(2026, 1, 15)
    assert bounds(Period.PREVIOUS_MONTH, today) == (date(2025, 12, 1), date(2026, 1, 1))
    assert bounds(Period.CURRENT_MONTH, today) == (date(2026, 1, 1), date(2026, 2, 1))
    assert bounds(Period.PREVIOUS_YEAR, today) == (date(2025, 1, 1), date(2026, 1, 1))
    assert bounds(Period.YESTERDAY, today) == (date(2026, 1, 14), today)


async def test_factor_sensor_attributes_and_diagnostics(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 15, 12, 0))
    view = View(
        {
            "total_heat": {date(2026, 11, 14): 40.0},
            "total_electricity": {date(2026, 11, 14): 10.0},
        },
        source="cloud",
    )
    counters(plant, 1614.45, 400.0)
    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.recordings.recordings_view",
        lambda _hass, _gateway: view,
    )
    engine = await _engine(plant)
    await engine.async_refresh(full=True)
    sensor = PerformanceFactorSensor(
        plant.coordinator, engine, Period.YESTERDAY, "total"
    )
    sensor._refresh()
    assert sensor.native_value == 4.0
    attributes = sensor.extra_state_attributes
    assert attributes is not None
    assert attributes["period_start"] == attributes["period_end"] == "2026-11-14"
    assert attributes["heat_source"] == "cloud_recording"
    assert attributes["electricity_source"] == "cloud_recording"
    assert attributes["days"] == 1 and attributes["missing_days"] == 0
    assert attributes["derived_from"] == {
        "/recordings/heatSources/emon/total/compressor": "recording",
        "/recordings/heatSources/emon/total/eheater": "recording",
        "/recordings/heatSources/emon/total/outputProduced": "recording",
    }
    lifetime = PerformanceFactorSensor(
        plant.coordinator, engine, Period.SINCE_COMMISSIONING, "total"
    )
    lifetime._refresh()
    assert lifetime.native_value == pytest.approx(1614.45 / 400.0, abs=0.01)
    assert lifetime.extra_state_attributes == {
        "heat_kwh": 1614.45,
        "electricity_kwh": 400.0,
        "heat_source": "counters",
        "electricity_source": "counters",
        "derived_from": {TOTAL: "cloud"},
    }
    report = json.dumps(engine.diagnostics())
    assert "2026" not in report and "gateway-one" not in report
    assert engine.diagnostics()["days_recorded"] == {
        "total_electricity": 1,
        "total_heat": 1,
    }
    # The recordings' update reaches the sensors.
    calls: list[int] = []
    engine.add_listener(lambda: calls.append(1))
    view.listeners[0]()
    assert calls == [1]


async def test_counters_per_mode_since_commissioning(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    engine = await _engine(plant)
    engine.sync()
    heating = engine.value(Period.SINCE_COMMISSIONING, "ch")
    hot_water = engine.value(Period.SINCE_COMMISSIONING, "dhw")
    assert heating is not None and heating.ratio == pytest.approx(
        1234.16 / 400.0, abs=0.01
    )
    assert hot_water is not None and hot_water.ratio == pytest.approx(
        380.29 / 400.0, abs=0.01
    )


async def test_the_engine_ignores_readings_before_it_is_loaded(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    engine = PerformanceHistory(hass, plant.coordinator)
    engine.sync()
    assert engine.value(Period.SINCE_COMMISSIONING, "total") is None
    assert performance_history.performance_diagnostics(plant.coordinator) == {
        "waiting": "not_started"
    }


# ------------------------------------------------------------ 24 h replay


async def test_live_power_over_a_day_matches_the_counters(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """One day of a real installation, replayed: the heat output and the
    electrical power the current COP uses, integrated over 24 h, agree with
    the counters' increments the performance factor of the day uses (within
    3 %), and so do their ratios."""
    series = json.loads(FIXTURE.read_text(encoding="utf-8"))["series"]
    midnight = local(2026, 11, 14, 0, 0)
    plant = air(hass, freezer)
    _at(plant, midnight - timedelta(minutes=2))
    engine = await _engine(plant)
    cop = CurrentCopSensor(plant.coordinator)
    current: dict[str, float | None] = {
        name: rows[0][1] for name, rows in series.items()
    }

    def apply() -> None:
        output, power = current["heat_output_kw"], current["compressor_w"]
        heater = current["heater_w"]
        for path, value, unit in (
            (HEAT_OUTPUT, output, "kW"),
            (POWER, power, "W"),
            (HEATER, heater, "W"),
        ):
            if value is None:
                plant.forget(path)
            else:
                plant.local(path, value, unitOfMeasure=unit)
        heat, electricity = current["heat_total_kwh"], current["electricity_total_kwh"]
        if heat is not None and electricity is not None:
            counters(plant, heat, electricity)
        plant.flag(COMPRESSOR_ACTIVE, bool(power))

    apply()
    engine.sync()  # the last reading before midnight
    events: dict[int, list[tuple[str, float | None]]] = {}
    for name, rows in series.items():
        for second, value in rows:
            events.setdefault(second, []).append((name, value))
    heat_kwh = power_kwh = 0.0
    previous: tuple[int, float | None, float | None] | None = None
    for second in sorted(events):
        for name, value in events[second]:
            current[name] = value
        _at(plant, midnight + timedelta(seconds=second))
        apply()
        engine.sync()
        result = cop._derive()
        output = result.attributes["heat_output_kw"]
        compressor = result.attributes["compressor_power_kw"]
        heater = result.attributes["heater_power_kw"]
        electrical = (
            compressor + (heater or 0.0) if isinstance(compressor, float) else None
        )
        if previous is not None:
            elapsed = (second - previous[0]) / 3600
            if previous[1] is not None:
                heat_kwh += previous[1] * elapsed
            if previous[2] is not None:
                power_kwh += previous[2] * elapsed
        previous = (second, output, electrical)  # type: ignore[assignment]
        if result.value is not None:
            assert result.value == pytest.approx(
                ((output or 0.0) + (heater or 0.0)) / electrical, abs=0.01
            )
    assert previous is not None
    tail = (86400 - previous[0]) / 3600
    heat_kwh += (previous[1] or 0.0) * tail
    power_kwh += (previous[2] or 0.0) * tail
    day = engine.value(
        Period.TODAY, "total", midnight + timedelta(hours=23, minutes=59)
    )
    assert day is not None and engine._baseline_method == "midnight"
    assert day.heat == pytest.approx(49.66, abs=0.01)
    assert day.electricity == pytest.approx(11.10, abs=0.01)
    assert heat_kwh == pytest.approx(day.heat, rel=0.03)
    assert power_kwh == pytest.approx(day.electricity, rel=0.03)
    assert day.ratio is not None
    assert heat_kwh / power_kwh == pytest.approx(day.ratio, rel=0.03)


# ------------------------------------------------- the real recordings view


async def test_the_engine_reads_the_plants_recordings(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """With the recordings of a plant read locally (``RecordingsView``):
    48 kWh heat and 12 kWh electricity a day, no counters' statistics."""
    from types import SimpleNamespace

    from tests.test_recordings_core import local as recording_time
    from tests.test_recordings_manager import GW, Clock, Driver, manager

    moment = recording_time(2026, 11, 14, 10) + timedelta(minutes=20)
    freezer.move_to(moment)
    clock = Clock(moment)
    recordings = await manager(hass, clock, [Driver(clock)])
    await recordings.async_run("start")
    coordinator = SimpleNamespace(
        hass=hass, gateway=SimpleNamespace(gateway_id=GW), config_entry=None
    )
    engine = PerformanceHistory(hass, coordinator)  # type: ignore[arg-type]
    await engine.async_start(schedule=False)
    await engine.async_refresh(full=True)
    assert engine.diagnostics()["recordings_attached"] is True
    assert recordings.needed()
    # Yesterday is read once so far: not confirmed, no value yet.
    assert engine.value(Period.YESTERDAY, "total") is None
    clock.now += timedelta(hours=1)
    freezer.move_to(clock.now)
    await recordings.async_run("hourly")
    await engine.async_refresh()
    result = engine.value(Period.YESTERDAY, "total")
    assert result is not None
    assert result.heat == pytest.approx(48.0)
    assert result.electricity == pytest.approx(12.0)
    assert result.ratio == 4.0
    assert result.heat_sources == {"local_recording"}
    month = engine.value(Period.CURRENT_MONTH, "total")
    assert month is not None and month.first_day is None  # the first of the month
    assert engine.first_day("total") == date(2026, 11, 1)
    assert month.days == 13 and month.missing_days == 1  # today
    engine.stop()
    recordings.stop()


async def test_the_sensors_are_added_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    from custom_components.bosch_buderus_heating.performance_sensors import (
        track_performance_sensors,
    )

    plant = running(air(hass, freezer))
    added: list[Any] = []
    unloads: list[Callable[[], None]] = []
    entry = SimpleEntry(unloads)
    track_performance_sensors(entry, plant.coordinator, added.extend)  # type: ignore[arg-type]
    assert sum(isinstance(item, CurrentCopSensor) for item in added) == 1
    count = len(added)
    plant.coordinator.async_update_listeners()
    assert len(added) == count == 22  # the COP and 21 factors
    spreading(plant)
    plant.coordinator.async_update_listeners()
    assert len(added) == count + 1  # the spread, once its inputs exist
    for unload in unloads:
        unload()


class SimpleEntry:
    def __init__(self, unloads: list[Callable[[], None]]) -> None:
        self.unloads = unloads

    def async_on_unload(self, callback: Callable[[], None]) -> None:
        self.unloads.append(callback)
