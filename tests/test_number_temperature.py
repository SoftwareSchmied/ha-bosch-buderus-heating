"""Temperature number entities carry a temperature device class (0.9.2).

Home Assistant then shows them in the unit of the user's unit system. It
converts value and limits but not the step; the entities convert the step
as a temperature difference and put a value set in the other unit back on
the native grid when it misses it only by the conversion's rounding. Unique
IDs, names and enablement stay as they were.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.components.number import NumberDeviceClass
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating import rrc_absence_entities
from custom_components.bosch_buderus_heating.number import (
    build_number_descriptions,
    policy_device_class,
)
from custom_components.bosch_buderus_heating.number_temperature import (
    snap_to_grid,
    temperature_device_class,
)
from custom_components.bosch_buderus_heating.writes import NumberWritePolicy
from tests.circuit_control_helpers import (
    COMFORT,
    COOLING_SETPOINT,
    THRESHOLD,
    Cloud,
    coordinator_for,
    number_entity,
    variant,
)

SEASON = "icom/season-circuit.json"
LEVELS = "icom/temperature-levels.json"


@pytest.mark.parametrize(
    ("unit", "delta", "expected"),
    [
        ("°C", False, NumberDeviceClass.TEMPERATURE),
        ("°F", False, NumberDeviceClass.TEMPERATURE),
        ("K", False, NumberDeviceClass.TEMPERATURE),
        ("°C", True, NumberDeviceClass.TEMPERATURE_DELTA),
        ("min", False, None),
        ("%", False, None),
        (None, False, None),
    ],
)
def test_device_class_follows_the_unit(unit, delta, expected) -> None:
    assert temperature_device_class(unit, delta=delta) is expected


def test_only_temperature_policies_get_the_class() -> None:
    temperature = NumberWritePolicy(r"^/x$", "C", 0.5)
    duration = NumberWritePolicy(r"^/y$", "mins", 15)
    dimensionless = NumberWritePolicy(r"^/z$", "", 1.0)
    assert policy_device_class(temperature, "°C") is NumberDeviceClass.TEMPERATURE
    # A temperature policy shown without a unit gets no class (no HA warning).
    assert policy_device_class(temperature, None) is None
    assert policy_device_class(duration, "min") is None
    assert policy_device_class(dimensionless, None) is None


@pytest.mark.parametrize(("fixture", "key"), [(SEASON, "V2"), (LEVELS, "V1")])
def test_every_temperature_number_has_the_class(fixture, key) -> None:
    descriptions = build_number_descriptions(variant(fixture, key))
    assert descriptions
    for item in descriptions:
        if item.native_unit_of_measurement in {"°C", "°F", "K"}:
            assert item.device_class is NumberDeviceClass.TEMPERATURE, item.key
        else:
            assert item.device_class is None, item.key


def test_boost_temperature_is_a_temperature_and_its_duration_not() -> None:
    temperature, duration = rrc_absence_entities._NUMBERS
    assert temperature.device_class is NumberDeviceClass.TEMPERATURE
    assert duration.device_class is None


def test_snap_takes_the_nearest_step() -> None:
    # 70.7 °F is 21.5 °C up to the float error of the conversion.
    assert snap_to_grid((70.7 - 32) * 5 / 9, 5.0, 0.5) == 21.5
    # 21.47 °C (shown 70.65 °F rounded) still is the step 21.5.
    assert snap_to_grid(21.47, 5.0, 0.5) == 21.5
    # 70.5 °F = 21.39 °C lies between two steps: the nearest one, as the
    # climate entities do with a converted value.
    assert snap_to_grid((70.5 - 32) * 5 / 9, 5.0, 0.5) == 21.5
    # 120 °F = 48.89 °C on a 1.0 grid: 49.
    assert snap_to_grid((120 - 32) * 5 / 9, 25.0, 1.0) == 49.0
    # Counted from the minimum, not from 0.
    assert snap_to_grid(10.29, 10.3, 1.0) == 10.3
    assert snap_to_grid(10.71, 10.3, 1.0) == 10.3
    assert snap_to_grid(21.5, 5.0, None) == 21.5
    assert snap_to_grid(21.3, 5.0, 0.0) == 21.3


async def test_metric_installation_shows_and_writes_natively(hass) -> None:
    hass.config.units = METRIC_SYSTEM
    coordinator = coordinator_for(hass, variant(SEASON, "V2"))
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, COOLING_SETPOINT)
    assert entity.device_class is NumberDeviceClass.TEMPERATURE
    assert entity.unit_of_measurement == "°C"
    assert (entity.min_value, entity.max_value, entity.step) == (18, 30, 0.5)
    native = entity.convert_to_native_value(24.5)
    assert native == 24.5
    await entity.async_set_native_value(native)
    assert cloud.puts == [(COOLING_SETPOINT, 24.5)]


async def test_imperial_installation_converts_value_limits_and_step(hass) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    coordinator = coordinator_for(hass, variant(SEASON, "V2"))
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, COOLING_SETPOINT)
    assert entity.unit_of_measurement == "°F"
    assert entity.native_unit_of_measurement == "°C"
    # 18 to 30 °C in 0.5 K steps: 64.4 to 86 °F in 0.9 °F steps.
    assert entity.min_value == pytest.approx(64.4)
    assert entity.max_value == pytest.approx(86.0)
    assert entity.step == pytest.approx(0.9)
    assert entity.capability_attributes["step"] == pytest.approx(0.9)
    # 64.4 + 7 * 0.9 = 70.7 °F is 21.5 °C exactly on the wire.
    native = entity.convert_to_native_value(70.7)
    assert native == 21.5
    await entity.async_set_native_value(native)
    assert cloud.puts == [(COOLING_SETPOINT, 21.5)]


async def test_imperial_value_between_two_steps_takes_the_nearest_step(hass) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    coordinator = coordinator_for(hass, variant(SEASON, "V2"))
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, COOLING_SETPOINT)
    # 70.5 °F = 21.39 °C: the nearest step 21.5 °C is written.
    native = entity.convert_to_native_value(70.5)
    assert native == 21.5
    await entity.async_set_native_value(native)
    assert cloud.puts == [(COOLING_SETPOINT, 21.5)]


async def test_metric_value_between_two_steps_is_still_refused(hass) -> None:
    hass.config.units = METRIC_SYSTEM
    coordinator = coordinator_for(hass, variant(SEASON, "V2"))
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, COOLING_SETPOINT)
    native = entity.convert_to_native_value(21.3)
    assert native == 21.3
    with pytest.raises(ServiceValidationError):
        await entity.async_set_native_value(native)
    assert cloud.puts == []


async def test_outdoor_threshold_is_an_absolute_temperature(hass) -> None:
    """The heating limit is an outdoor temperature: 15 °C is 59 °F, not 27 °F."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    resources = variant(SEASON, "V2")
    coordinator = coordinator_for(hass, resources)
    entity = number_entity(hass, coordinator, THRESHOLD)
    native = resources[THRESHOLD].value
    assert isinstance(native, int | float)
    assert entity.value == pytest.approx(native * 9 / 5 + 32, abs=0.1)
    assert entity.value != pytest.approx(native * 9 / 5, abs=0.1)


async def test_fahrenheit_gateway_on_a_metric_installation(hass) -> None:
    hass.config.units = METRIC_SYSTEM
    resources = variant(SEASON, "V2")
    resources[COOLING_SETPOINT] = replace(
        resources[COOLING_SETPOINT],
        value=77.0,
        metadata=replace(
            resources[COOLING_SETPOINT].metadata,
            unit="F",
            minimum=64.0,
            maximum=86.0,
            step_size=1.0,
        ),
    )
    coordinator = coordinator_for(hass, resources)
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, COOLING_SETPOINT)
    assert entity.unit_of_measurement == "°C"
    assert entity.value == pytest.approx(25.0)
    assert entity.step == pytest.approx(0.5556, abs=1e-4)
    # 23.9 °C shown (75 °F) is written as 75 °F.
    native = entity.convert_to_native_value(23.9)
    assert native == 75.0
    await entity.async_set_native_value(native)
    assert cloud.puts == [(COOLING_SETPOINT, 75.0)]


async def test_unique_id_and_name_unchanged(hass) -> None:
    coordinator = coordinator_for(hass, variant(LEVELS, "V1"))
    entity = number_entity(hass, coordinator, COMFORT)
    assert entity.unique_id == (
        "gateway:heatingCircuits:hc1:temperatureLevels:comfort2:control"
    )
    assert entity.device_class is NumberDeviceClass.TEMPERATURE
    assert entity.entity_description.entity_registry_enabled_default
