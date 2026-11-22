"""#138: a hybrid system's heat pump reports no electric heater part (synthetic).

The MX300 of a hybrid system reports its heat pump's own counters under
``/heatSources/hs2/emon/...`` with the parts ``outputProduced`` and
``compressor`` only; that heat pump has no heater. The compressor alone is
then its electricity. A reported heater part is still added, a burner part
still rules the collection out, and the gateway's collections
(``/heatSources/emon/...``) still need both parts.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import performance_history
from custom_components.bosch_buderus_heating.burner_share import BURNER_SHARE
from custom_components.bosch_buderus_heating.counter_selection import counter_vector
from custom_components.bosch_buderus_heating.energy_contracts import (
    compressor_is_electricity,
)
from custom_components.bosch_buderus_heating.performance import (
    HEAT_PUMP_COUNTERS,
    counter_possible,
    heat_pump_counter_possible,
    heat_pump_environmental,
    heat_pump_lifetime,
    heat_pump_root,
    lifetime,
)
from custom_components.bosch_buderus_heating.performance_history import Period
from custom_components.bosch_buderus_heating.performance_sensors import (
    PerformanceFactorSensor,
    _candidates,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from tests.test_derived_values import CH, DHW, TOTAL, Plant, air
from tests.test_perf_hybrid_helpers import HS2_CH, HS2_DHW, HS2_TOTAL, boiler_gateway
from tests.test_performance import _engine

# The heat pump's counters of the report in #138.
HEAT = 2607.64
COMPRESSOR = 866.94


@pytest.fixture(autouse=True)
async def _stop_engines(hass: HomeAssistant) -> AsyncGenerator[None]:
    yield
    for engine in hass.data.get(performance_history._ENGINES, {}).values():
        engine.stop()


def _compressor_only(
    plant: Plant, path: str, *, heat: float = HEAT, compressor: float = COMPRESSOR
) -> Plant:
    """A collection as the MX300 reports it: no heater part."""
    return plant.cloud(
        path, values=({"outputProduced": heat}, {"compressor": compressor})
    )


def _hybrid(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> Plant:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    return _compressor_only(plant, HS2_TOTAL)


async def _factor(plant: Plant, mode: str = "total") -> PerformanceFactorSensor:
    engine = await _engine(plant)
    sensor = PerformanceFactorSensor(
        plant.coordinator, engine, Period.SINCE_COMMISSIONING, mode
    )
    sensor._refresh()
    return sensor


def _sensor(plant: Plant, path: str, kind: str) -> BoschBuderusSensor | None:
    resource = plant.coordinator.resources[path]
    for description in build_sensor_descriptions({path: resource}):
        if description.value_kind == kind:
            return BoschBuderusSensor(plant.coordinator, description)  # type: ignore[arg-type]
    return None


# ------------------------------------------------------- compressor alone
async def test_the_compressor_alone_is_the_heat_pumps_electricity(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = _hybrid(hass, freezer)
    assert heat_pump_root(plant.coordinator) == "/heatSources/hs2"
    assert heat_pump_root(plant.coordinator, current=False) == "/heatSources/hs2"
    found = heat_pump_lifetime(plant.coordinator, "total")
    assert found is not None
    assert found[0].heat == pytest.approx(HEAT)
    assert found[0].electricity == pytest.approx(COMPRESSOR)
    sensor = await _factor(plant)
    # 2607.64 / 866.94 = 3.0079
    assert sensor.native_value == pytest.approx(3.01)
    attributes = sensor.extra_state_attributes or {}
    assert attributes["basis"] == HEAT_PUMP_COUNTERS
    assert "reason" not in attributes
    assert attributes["electricity_kwh"] == pytest.approx(COMPRESSOR)
    assert attributes["derived_from"] == {HS2_TOTAL: "cloud"}
    assert heat_pump_environmental(plant.coordinator, "total") == (
        pytest.approx(HEAT - COMPRESSOR),
        HS2_TOTAL,
    )
    environmental = _sensor(plant, TOTAL, "environmental_energy")
    assert environmental is not None
    assert environmental.native_value == pytest.approx(1740.70)
    assert (environmental.extra_state_attributes or {})["basis"] == HEAT_PUMP_COUNTERS


async def test_heating_and_hot_water_of_the_heat_pump(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = _hybrid(hass, freezer)
    _compressor_only(plant, HS2_CH, heat=2000.0, compressor=500.0)
    _compressor_only(plant, HS2_DHW, heat=600.0, compressor=300.0)
    assert (await _factor(plant, "ch")).native_value == pytest.approx(4.0)
    assert (await _factor(plant, "dhw")).native_value == pytest.approx(2.0)
    for mode in ("total", "ch", "dhw"):
        assert heat_pump_counter_possible(plant.coordinator, mode)
    ch = _sensor(plant, CH, "environmental_energy")
    dhw = _sensor(plant, DHW, "environmental_energy")
    assert ch is not None and ch.native_value == pytest.approx(1500.0)
    assert dhw is not None and dhw.native_value == pytest.approx(300.0)


async def test_factor_entities_exist_for_compressor_only_counters(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.cloud(TOTAL, values=({"outputProduced": 900.0}, {"burner": 1100.0}))
    for path in (CH, DHW):
        plant.coordinator.resources.pop(path)
        plant.coordinator.data.pop(path)
    assert not any(
        key.startswith("factor:") for key, _ in _candidates(plant.coordinator)
    )
    _compressor_only(plant, HS2_TOTAL)
    keys = {key for key, _ in _candidates(plant.coordinator)}
    assert {f"factor:total:{period.value}" for period in Period} <= keys


async def test_the_statistics_come_from_the_compressor_entity(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.bosch_buderus_heating.const import DOMAIN

    plant = _hybrid(hass, freezer)
    resource = plant.coordinator.resources[HS2_TOTAL]
    assert compressor_is_electricity(resource)
    vector = counter_vector(resource)
    assert vector is not None
    assert vector["=total_electricity"] == pytest.approx(COMPRESSOR)
    # No second entity with the compressor's number.
    assert _sensor(plant, HS2_TOTAL, "total_electricity") is None
    registry = er.async_get(hass)
    ids = {}
    for description in build_sensor_descriptions({HS2_TOTAL: resource}):
        if description.value_key in ("outputProduced", "compressor"):
            entity = BoschBuderusSensor(plant.coordinator, description)  # type: ignore[arg-type]
            assert entity.unique_id is not None
            ids[description.value_key] = registry.async_get_or_create(
                "sensor", DOMAIN, entity.unique_id, suggested_object_id="x"
            ).entity_id
    assert set(ids) == {"outputProduced", "compressor"}
    found = (await _engine(plant))._counter_ids()
    assert found[("hp:total", "heat")] == ids["outputProduced"]
    assert found[("hp:total", "electricity")] == ids["compressor"]


async def test_an_electricity_entity_created_before_the_reading(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Created from the collection's name before any part was read.
    plant = air(hass, freezer).cloud(HS2_TOTAL, kind="emonValue")
    electricity = _sensor(plant, HS2_TOTAL, "total_electricity")
    assert electricity is not None
    _compressor_only(plant, HS2_TOTAL)
    assert electricity.native_value == pytest.approx(COMPRESSOR)
    assert electricity.extra_state_attributes == {
        "value_source": "calculated",
        "calculation": "compressor",
    }


# ------------------------------------------------------------ other cases
async def test_a_reported_heater_part_is_still_added(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    plant.cloud(
        HS2_TOTAL,
        values=({"outputProduced": HEAT}, {"compressor": 800.0}, {"eheater": 66.94}),
    )
    resource = plant.coordinator.resources[HS2_TOTAL]
    assert not compressor_is_electricity(resource)
    found = heat_pump_lifetime(plant.coordinator, "total")
    assert found is not None and found[0].electricity == pytest.approx(866.94)
    assert (await _factor(plant)).native_value == pytest.approx(3.01)
    electricity = _sensor(plant, HS2_TOTAL, "total_electricity")
    assert electricity is not None
    assert electricity.native_value == pytest.approx(866.94)
    assert (electricity.extra_state_attributes or {})[
        "calculation"
    ] == "compressor + auxiliary_heater"
    # The heater's share is no compressor alone in the statistics either.
    vector = counter_vector(resource)
    assert vector is not None
    assert vector["=total_electricity"] == pytest.approx(866.94)


async def test_an_invalid_heater_part_is_no_compressor_alone(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    plant.cloud(
        HS2_TOTAL,
        values=({"outputProduced": HEAT}, {"compressor": COMPRESSOR}, {"eheater": "x"}),
    )
    assert heat_pump_root(plant.coordinator) is None
    sensor = await _factor(plant)
    assert sensor.native_value is None
    assert (sensor.extra_state_attributes or {})["reason"] == BURNER_SHARE


async def test_an_explicit_electricity_part_comes_first(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    plant.cloud(
        HS2_TOTAL,
        values=(
            {"outputProduced": HEAT},
            {"compressor": 800.0},
            {"electricity": COMPRESSOR},
        ),
    )
    found = heat_pump_lifetime(plant.coordinator, "total")
    assert found is not None and found[0].electricity == pytest.approx(COMPRESSOR)
    electricity = _sensor(plant, HS2_TOTAL, "total_electricity")
    assert electricity is not None
    assert electricity.native_value == pytest.approx(COMPRESSOR)


async def test_a_burner_part_still_rules_the_collection_out(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    plant.cloud(
        HS2_TOTAL,
        values=({"outputProduced": HEAT}, {"compressor": COMPRESSOR}, {"burner": 5.0}),
    )
    assert heat_pump_root(plant.coordinator) is None
    assert heat_pump_root(plant.coordinator, current=False) is None
    assert not heat_pump_counter_possible(plant.coordinator, "total")
    sensor = await _factor(plant)
    assert sensor.native_value is None
    assert (sensor.extra_state_attributes or {})["reason"] == BURNER_SHARE
    environmental = _sensor(plant, TOTAL, "environmental_energy")
    assert environmental is not None and environmental.native_value is None


async def test_the_gateways_collection_still_needs_the_heater_part(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _compressor_only(plant, TOTAL)
    resource = plant.coordinator.resources[TOTAL]
    assert not compressor_is_electricity(resource)
    assert lifetime(plant.coordinator, "total") is None
    assert not counter_possible(plant.coordinator, "total")
    vector = counter_vector(resource)
    assert vector is not None and "=total_electricity" not in vector
    assert _sensor(plant, TOTAL, "total_electricity") is None
    environmental = _sensor(plant, TOTAL, "environmental_energy")
    assert environmental is None or environmental.native_value is None
