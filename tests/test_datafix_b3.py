"""Findings of the beta.2 health check: standstill values, the end of a hot
water charge, the day change, and the activity sensor's pump condition.

Synthetic plants (see ``test_derived_values``); each test runs real cycles
(``Plant.later``) instead of single mocked results.
"""

from __future__ import annotations

from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.derived_activity import (
    circuit_activity,
    circuit_has_pump,
)
from custom_components.bosch_buderus_heating.derived_sensors import (
    SupplyOvershootSensor,
    track_calculated_sensors,
)
from custom_components.bosch_buderus_heating.performance_history import Period
from custom_components.bosch_buderus_heating.performance_sensors import (
    PerformanceFactorSensor,
)
from tests.test_derived_values import (
    COMPRESSOR,
    COMPRESSOR_ACTIVE,
    PUMP,
    REFRIGERANT,
    ROOT,
    VW1,
    Plant,
    _entry,
    air,
)
from tests.test_performance import _at, _engine, counters, local

SUPPLY = "/heatSources/actualSupplyTemperature"
SETPOINT = "/heatSources/currentSupplySetpoint"


def _overshoot(plant: Plant) -> SupplyOvershootSensor:
    plant.cloud(SUPPLY, 25.1, unit="°C")
    plant.local(SETPOINT, 24.0, unitOfMeasure="C")
    return SupplyOvershootSensor(plant.coordinator, SUPPLY, SETPOINT, None)


# ------------------------------------------------------------------ H1
def test_supply_above_setpoint_is_unknown_at_standstill_not_unavailable(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    sensor = _overshoot(plant)
    sensor._refresh()
    assert sensor.available and sensor.native_value == 1.1
    # The compressor stops and the refrigerant circuit goes off.
    plant.later(0.2).flag(COMPRESSOR_ACTIVE, False).local(REFRIGERANT, "off")
    plant.cloud(COMPRESSOR, "off")
    sensor._refresh()
    assert sensor.native_value is None and sensor.available
    # A hot water charge, too.
    plant.later(0.2).flag(COMPRESSOR_ACTIVE, True).local(REFRIGERANT, "dhw")
    plant.flag(VW1, True)
    sensor._refresh()
    assert sensor.native_value is None and sensor.available
    # Heating again.
    plant.later(0.2).flag(VW1, False).local(REFRIGERANT, "heating")
    plant.cloud(COMPRESSOR, "heating")
    sensor._refresh()
    assert sensor.native_value == 1.1


def test_supply_above_setpoint_is_unavailable_without_its_inputs(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    sensor = _overshoot(plant)
    sensor._refresh()
    assert sensor.available
    plant.later(0.2).forget(SETPOINT)
    sensor._refresh()
    assert sensor.native_value is None and not sensor.available
    plant.later(0.2).local(SETPOINT, 24.0, unitOfMeasure="C").stale_cloud(SUPPLY)
    sensor._refresh()
    assert not sensor.available


# ------------------------------------------------------------------ H2
def test_the_end_of_a_hot_water_charge_is_not_heating(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The valve is back on the heating side while the refrigerant circuit
    still says hot water or has stopped and the compressor signal still runs."""
    plant = air(hass, freezer)
    coordinator = plant.coordinator
    plant.later(0.2).flag(VW1, True).local(REFRIGERANT, "dhw")
    assert circuit_activity(coordinator, ROOT).value == "idle"
    # The valve is off first (out of order), the status follows.
    plant.later(0.2).flag(VW1, False)
    assert circuit_activity(coordinator, ROOT).value == "idle"
    plant.later(0.2).local(REFRIGERANT, "off")  # status off, compressor still on
    assert circuit_activity(coordinator, ROOT).value == "idle"
    plant.later(0.2).flag(COMPRESSOR_ACTIVE, False)
    assert circuit_activity(coordinator, ROOT).value == "idle"
    # Heating starts again: the status follows the valve.
    plant.later(0.2).flag(COMPRESSOR_ACTIVE, True).local(REFRIGERANT, "heating")
    assert circuit_activity(coordinator, ROOT).value == "heating"


def test_a_start_status_does_not_contradict_the_valve(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer, refrigerant="startup")
    assert circuit_activity(plant.coordinator, ROOT).value == "heating"


# ------------------------------------------------------------------ H3
async def test_the_factor_of_today_says_why_it_is_unknown_after_midnight(
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
    plant.later(2)  # 00:01, the first reading of the day
    counters(plant, 1600.4, 400.07)
    engine.sync()
    sensor = PerformanceFactorSensor(plant.coordinator, engine, Period.TODAY, "total")
    sensor._refresh()
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["reason"] == "too_little_electricity"
    # Ten minutes into the day the electricity passes the minimum.
    plant.later(10)
    counters(plant, 1601.2, 400.2)
    engine.sync()
    sensor._refresh()
    assert sensor.native_value == pytest.approx(6.0, abs=0.01)
    assert "reason" not in sensor.extra_state_attributes


# ---------------------------------------------------------------- #130
def test_the_pump_value_decides_whether_the_circuit_has_one(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    assert circuit_has_pump(plant.coordinator, ROOT)  # local pumpStatus
    plant.forget(PUMP)
    assert not circuit_has_pump(plant.coordinator, ROOT)
    plant.cloud(ROOT + "/pumpModulation", 0.0, unit="%")
    assert circuit_has_pump(plant.coordinator, ROOT)


async def test_no_activity_sensor_without_a_pump_value_but_with_one_it_is_added(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer).forget(PUMP)
    added: list[Any] = []
    entry = _entry()
    track_calculated_sensors(entry, plant.coordinator, added.extend)
    assert "calculated_activity" not in {e.translation_key for e in added}
    # The pump value shows up later (the local access finds it): added then.
    plant.local(PUMP, "on")
    plant.coordinator.async_update_listeners()
    assert [e.translation_key for e in added].count("calculated_activity") == 1
    for unsubscribe in entry.listeners:  # no refresh timer is left behind
        unsubscribe()


async def test_an_activity_sensor_already_registered_stays(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    registry = er.async_get(hass)
    unique = "gateway-one:heatingCircuits:hc1:calculated_activity"
    existing = registry.async_get_or_create("sensor", DOMAIN, unique)
    plant = air(hass, freezer).forget(PUMP)
    entry = _entry()
    track_calculated_sensors(entry, plant.coordinator, lambda _items: None)
    plant.coordinator.async_update_listeners()
    for unsubscribe in entry.listeners:  # no refresh timer is left behind
        unsubscribe()
    kept = registry.async_get(existing.entity_id)
    assert kept is not None and kept.unique_id == unique


def test_a_circuit_without_a_pump_value_has_no_activity_while_the_source_runs(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer).forget(PUMP)
    assert circuit_activity(plant.coordinator, ROOT).value is None


async def test_the_diagnostics_list_the_circuits_without_a_pump_value(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    from custom_components.bosch_buderus_heating.derived_activity import (
        activity_inputs_missing,
    )

    plant = air(hass, freezer)
    assert activity_inputs_missing(plant.coordinator) == {}
    plant.forget(PUMP)
    assert activity_inputs_missing(plant.coordinator) == {"hc1": ["pump"]}
    plant.cloud(ROOT + "/pumpModulation", 0.0, unit="%")
    assert activity_inputs_missing(plant.coordinator) == {}


async def test_the_gateway_report_carries_the_missing_activity_inputs(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    from custom_components.bosch_buderus_heating.diagnostics import (
        _gateway_diagnostics,
    )

    plant = air(hass, freezer).forget(PUMP)
    report = _gateway_diagnostics(1, plant.coordinator)
    assert report["activity_inputs_missing"] == {"hc1": ["pump"]}
