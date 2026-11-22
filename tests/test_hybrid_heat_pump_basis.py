"""Hybrid systems: the heat pump's own counters are the basis (synthetic).

The gateway's collections count the boiler's heat; the heat pump reports its
own heat and electricity under its heat source. Factors and the environmental
energy then come from those counters; without them the value stays unknown
with the reason ``burner_share``. A plant with a heat pump only is unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import (
    derived_store as derived_store_module,
)
from custom_components.bosch_buderus_heating import performance_history
from custom_components.bosch_buderus_heating.burner_share import BURNER_SHARE
from custom_components.bosch_buderus_heating.derived_store import derived_store
from custom_components.bosch_buderus_heating.performance import (
    HEAT_PUMP_COUNTERS,
    heat_pump_environmental,
    heat_pump_root,
)
from custom_components.bosch_buderus_heating.performance_history import (
    HEAT,
    Period,
)
from custom_components.bosch_buderus_heating.performance_sensors import (
    PerformanceFactorSensor,
    _candidates,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from tests.ha_test_helpers import flush_store
from tests.test_derived_values import CH, DHW, TOTAL, Plant, air
from tests.test_perf_hybrid_helpers import (
    HS1_TOTAL,
    HS2_CH,
    HS2_DHW,
    HS2_TOTAL,
    boiler_gateway,
    heat_pump,
)
from tests.test_performance import _at, _engine, local


@pytest.fixture(autouse=True)
async def _stop_engines(hass: HomeAssistant) -> AsyncGenerator[None]:
    yield
    for engine in hass.data.get(performance_history._ENGINES, {}).values():
        engine.stop()


async def _factor(
    plant: Plant, period: Period = Period.SINCE_COMMISSIONING, mode: str = "total"
) -> PerformanceFactorSensor:
    engine = await _engine(plant)
    sensor = PerformanceFactorSensor(plant.coordinator, engine, period, mode)
    sensor._refresh()
    return sensor


def _environmental(plant: Plant, path: str = TOTAL) -> BoschBuderusSensor:
    resource = plant.coordinator.resources[path]
    description = next(
        item
        for item in build_sensor_descriptions({path: resource})
        if item.value_kind == "environmental_energy"
    )
    return BoschBuderusSensor(plant.coordinator, description)  # type: ignore[arg-type]


# ------------------------------------------------------------ both counters
async def test_both_running_use_the_heat_pump_counters_only(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    heat_pump(plant, HS2_TOTAL, heat=1500.0, electricity=400.0)
    sensor = await _factor(plant)
    # 1500 / 400, never the boiler's 900 kWh.
    assert sensor.native_value == pytest.approx(3.75)
    attributes = sensor.extra_state_attributes
    assert attributes is not None
    assert attributes["basis"] == HEAT_PUMP_COUNTERS
    assert "reason" not in attributes
    assert attributes["heat_kwh"] == pytest.approx(1500.0)
    assert attributes["electricity_kwh"] == pytest.approx(400.0)
    assert attributes["derived_from"] == {HS2_TOTAL: "cloud"}
    environmental = _environmental(plant)
    assert environmental.native_value == pytest.approx(1100.0)
    attributes = environmental.extra_state_attributes or {}
    assert attributes["basis"] == HEAT_PUMP_COUNTERS and "reason" not in attributes


async def test_the_modes_follow_their_own_heat_pump_collections(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    heat_pump(plant, HS2_TOTAL, heat=1500.0, electricity=400.0)
    heat_pump(plant, HS2_CH, heat=1100.0, electricity=250.0)
    heat_pump(plant, HS2_DHW, heat=400.0, electricity=150.0)
    heating = await _factor(plant, mode="ch")
    assert heating.native_value == pytest.approx(4.4)
    hot_water = await _factor(plant, mode="dhw")
    assert hot_water.native_value == pytest.approx(400.0 / 150.0, abs=0.01)
    assert _environmental(plant, CH).native_value == pytest.approx(850.0)
    assert _environmental(plant, DHW).native_value == pytest.approx(250.0)


async def test_a_heat_pump_that_reports_one_electricity_value(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    heat_pump(plant, HS2_TOTAL, heat=1500.0, electricity=400.0, split=False)
    sensor = await _factor(plant)
    assert sensor.native_value == pytest.approx(3.75)
    assert _environmental(plant).native_value == pytest.approx(1100.0)


async def test_a_negative_difference_is_no_environmental_energy(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    heat_pump(plant, HS2_TOTAL, heat=300.0, electricity=400.0)
    assert heat_pump_environmental(plant.coordinator, "total") == (None, HS2_TOTAL)
    environmental = _environmental(plant)
    assert environmental.native_value is None
    attributes = environmental.extra_state_attributes or {}
    assert attributes["basis"] == HEAT_PUMP_COUNTERS and "reason" not in attributes


# ------------------------------------------------------------ missing parts
async def test_only_the_boiler_counters_stay_unknown(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    sensor = await _factor(plant)
    assert sensor.native_value is None
    assert (sensor.extra_state_attributes or {})["reason"] == BURNER_SHARE
    assert "basis" not in (sensor.extra_state_attributes or {})
    environmental = _environmental(plant)
    assert environmental.native_value is None
    attributes = environmental.extra_state_attributes or {}
    assert attributes["reason"] == BURNER_SHARE and "basis" not in attributes


async def test_a_missing_collection_of_a_mode_leaves_that_mode_unknown(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    heat_pump(plant, HS2_TOTAL, heat=1500.0, electricity=400.0)
    heating = await _factor(plant, mode="ch")
    assert heating.native_value is None
    assert (heating.extra_state_attributes or {})["reason"] == BURNER_SHARE
    assert _environmental(plant, CH).native_value is None


async def test_heat_pump_heat_without_electricity_is_no_basis(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    plant.cloud(HS2_TOTAL, values=({"outputProduced": 1500.0},))
    assert heat_pump_root(plant.coordinator) is None
    sensor = await _factor(plant)
    assert sensor.native_value is None
    assert (sensor.extra_state_attributes or {})["reason"] == BURNER_SHARE
    assert _environmental(plant).native_value is None


async def test_a_boiler_collection_is_never_taken_for_the_heat_pump(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    plant.cloud(
        HS1_TOTAL,
        values=({"outputProduced": 900.0, "electricity": 12.0, "burner": 1100.0},),
    )
    assert heat_pump_root(plant.coordinator) is None
    sensor = await _factor(plant)
    assert sensor.native_value is None


async def test_a_stale_heat_pump_reading_falls_back_to_unknown(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    heat_pump(plant, HS2_TOTAL, heat=1500.0, electricity=400.0)
    sensor = await _factor(plant)
    assert sensor.native_value == pytest.approx(3.75)
    plant.stale_cloud(HS2_TOTAL)
    sensor._refresh()
    assert sensor.native_value is None
    assert (sensor.extra_state_attributes or {})["reason"] == BURNER_SHARE


async def test_entities_exist_when_only_the_heat_pump_has_counters(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    # The gateway's collection has the boiler's heat and no electricity.
    plant.cloud(TOTAL, values=({"outputProduced": 900.0}, {"burner": 1100.0}))
    plant.coordinator.resources.pop(CH)
    plant.coordinator.resources.pop(DHW)
    plant.coordinator.data.pop(CH)
    plant.coordinator.data.pop(DHW)
    assert not any(
        key.startswith("factor:") for key, _ in _candidates(plant.coordinator)
    )
    heat_pump(plant, HS2_TOTAL, heat=1500.0, electricity=400.0)
    keys = [key for key, _ in _candidates(plant.coordinator)]
    assert {f"factor:total:{period.value}" for period in Period} <= set(keys)


async def test_a_heat_pump_only_plant_is_not_changed_by_its_heat_source_counters(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.cloud(
        HS1_TOTAL,
        values=({"outputProduced": 5.0, "compressor": 1.0, "eheater": 0.0},),
    )
    engine = await _engine(plant)
    sensor = PerformanceFactorSensor(
        plant.coordinator, engine, Period.SINCE_COMMISSIONING, "total"
    )
    sensor._refresh()
    assert sensor.native_value == pytest.approx(1614.45 / 400.0, abs=0.01)
    attributes = sensor.extra_state_attributes or {}
    assert "basis" not in attributes and "reason" not in attributes
    assert attributes["derived_from"] == {TOTAL: "cloud"}
    assert not any(mode.startswith("hp:") for mode in engine._live)
    assert _environmental(plant).native_value == pytest.approx(1614.45 - 400.0)
    assert "basis" not in (_environmental(plant).extra_state_attributes or {})


# --------------------------------------------------------- periods, restart
async def test_today_and_earlier_days_use_the_heat_pump_counters(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 14, 23, 50))
    boiler_gateway(plant, heat=900.0, burner=1100.0)
    heat_pump(plant, HS2_TOTAL, heat=1500.0, electricity=400.0)
    engine = await _engine(plant)
    sensor = PerformanceFactorSensor(plant.coordinator, engine, Period.TODAY, "total")
    engine.sync()
    plant.later(10)  # midnight
    boiler_gateway(plant, heat=900.5, burner=1100.4)
    heat_pump(plant, HS2_TOTAL, heat=1500.0, electricity=400.0)
    engine.sync()
    assert engine._baseline_method == "midnight"
    plant.later(60)
    boiler_gateway(plant, heat=950.0, burner=1130.0)  # the boiler heats a lot
    heat_pump(plant, HS2_TOTAL, heat=1503.0, electricity=401.0)
    sensor._refresh()
    assert sensor.native_value == pytest.approx(3.0)
    attributes = sensor.extra_state_attributes or {}
    assert attributes["heat_kwh"] == pytest.approx(3.0)
    assert attributes["electricity_kwh"] == pytest.approx(1.0)
    assert attributes["basis"] == HEAT_PUMP_COUNTERS
    assert attributes["heat_source"] == "counters"
    # Earlier days: the statistics of the heat pump's counters.
    engine.history_read = True
    engine._first.clear()
    for quantity, value in ((HEAT, 40.0), ("electricity", 10.0)):
        engine._counted[("hp:total", quantity)] = {
            date(2026, 11, day): value for day in (13, 14)
        }
    yesterday = PerformanceFactorSensor(
        plant.coordinator, engine, Period.YESTERDAY, "total"
    )
    yesterday._refresh()
    assert yesterday.native_value == pytest.approx(4.0)
    month = PerformanceFactorSensor(
        plant.coordinator, engine, Period.CURRENT_MONTH, "total"
    )
    month._refresh()
    # X42: from the counters alone, 3 of the 15 days so far (20 %) are too
    # few for a month: unknown, with the coverage as an attribute.
    assert month.native_value is None
    attributes = month.extra_state_attributes or {}
    assert attributes["reason"] == "partial_period"
    assert attributes["coverage_percent"] == pytest.approx(20.0)
    assert attributes["heat_kwh"] == pytest.approx(83.0)
    assert "environmental_energy_kwh" not in attributes
    for quantity, value in ((HEAT, 40.0), ("electricity", 10.0)):
        engine._counted[("hp:total", quantity)] = {
            date(2026, 11, day): value for day in range(1, 15)
        }
    engine._first.clear()
    month._refresh()
    assert month.native_value == pytest.approx(
        (14 * 40.0 + 3.0) / (14 * 10.0 + 1.0), abs=0.01
    )
    attributes = month.extra_state_attributes or {}
    assert attributes["coverage_percent"] == pytest.approx(100.0)
    assert attributes["environmental_energy_kwh"] == pytest.approx(563.0 - 141.0)
    # The gateway's own history is not touched.
    assert ("total", HEAT) not in engine._counted


async def test_a_restart_keeps_today_of_the_heat_pump(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 14, 23, 55))
    boiler_gateway(plant, heat=900.0, burner=1100.0)
    heat_pump(plant, HS2_TOTAL, heat=1500.0, electricity=400.0)
    engine = await _engine(plant)
    engine.sync()
    plant.later(5)
    boiler_gateway(plant, heat=900.0, burner=1100.0)
    heat_pump(plant, HS2_TOTAL, heat=1501.0, electricity=400.2)
    engine.sync()
    plant.later(60)
    boiler_gateway(plant, heat=900.0, burner=1100.0)
    heat_pump(plant, HS2_TOTAL, heat=1504.0, electricity=401.2)
    engine.sync()
    before = engine.value(Period.TODAY, "hp:total")
    assert before is not None and before.heat == pytest.approx(4.0)
    assert before.electricity == pytest.approx(1.2)
    await flush_store(derived_store(hass)._store)
    engine.stop()
    hass.data.pop(derived_store_module._STORE)
    again = await _engine(plant)
    # Saved readings are no current counters.
    assert again.value(Period.SINCE_COMMISSIONING, "hp:total") is None
    again.sync()
    assert again.value(Period.TODAY, "hp:total") is not None
    after = again.value(Period.TODAY, "hp:total")
    assert after is not None
    assert after.heat == pytest.approx(before.heat)
    assert after.electricity == pytest.approx(before.electricity)
    assert again.value(Period.SINCE_COMMISSIONING, "hp:total") is not None


async def test_the_statistics_of_the_heat_pump_counters_are_found(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.bosch_buderus_heating.const import DOMAIN

    plant = air(hass, freezer)
    boiler_gateway(plant, heat=900.0, burner=1100.0)
    heat_pump(plant, HS2_TOTAL, heat=1500.0, electricity=400.0)
    # The ids of the integration's own counter entities of the heat source.
    resource = plant.coordinator.resources[HS2_TOTAL]
    unique_ids = {}
    for description in build_sensor_descriptions({HS2_TOTAL: resource}):
        if description.value_key in ("outputProduced", "total_electricity"):
            sensor = BoschBuderusSensor(plant.coordinator, description)  # type: ignore[arg-type]
            unique_ids[description.value_key] = sensor.unique_id
    assert set(unique_ids) == {"outputProduced", "total_electricity"}
    registry = er.async_get(hass)
    for key, unique_id in unique_ids.items():
        registry.async_get_or_create(
            "sensor", DOMAIN, unique_id, suggested_object_id=key
        )
    engine = await _engine(plant)
    found = engine._counter_ids()
    assert ("hp:total", "heat") in found and ("hp:total", "electricity") in found
    assert ("total", "heat") not in found
