"""Cooling setpoint and cooling threshold of a heating circuit (0.9.2).

Only heat pumps with cooling have them; the shapes follow the model classes
of the manufacturer's app (``fixtures/contracts/icom/season-circuit.json``),
every value is constructed. Nothing here confirms behaviour on a real
installation.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.circuit_controls import (
    COOLING_SETPOINT_POLICY,
    COOLING_THRESHOLD_POLICY,
    display_minimum,
)
from custom_components.bosch_buderus_heating.controls_flow import controls
from custom_components.bosch_buderus_heating.number import build_number_descriptions
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    ResourceForbidden,
    ServiceUnavailable,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from custom_components.bosch_buderus_heating.writes import assess_control
from tests.circuit_control_helpers import (
    CONTROL_TYPE,
    COOLING_SETPOINT,
    COOLING_THRESHOLD,
    SWITCH_MODE,
    Cloud,
    coordinator_for,
    number_entity,
    number_paths,
    put_reading,
    variant,
    with_flags,
)

FIXTURE = "icom/season-circuit.json"
COMPONENT = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"


def _setup(hass, key: str = "V2"):
    coordinator = coordinator_for(hass, variant(FIXTURE, key))
    return coordinator, Cloud(coordinator)


# Cooling setpoint -----------------------------------------------------------


def test_setpoint_number_is_created_disabled():
    resources = variant(FIXTURE, "V2")
    (description,) = (
        item
        for item in build_number_descriptions(resources)
        if item.resource_path == COOLING_SETPOINT
    )
    assert description.write_policy is COOLING_SETPOINT_POLICY
    assert (description.native_min_value, description.native_max_value) == (18, 30)
    assert description.native_step == 0.5
    assert not description.entity_registry_enabled_default
    assert description.entity_category is EntityCategory.CONFIG
    assert description.translation_key == "cooling_room_setpoint"
    assert description.icon is None


async def test_setpoint_writes_once_and_confirms(hass):
    coordinator, cloud = _setup(hass)
    entity = number_entity(hass, coordinator, COOLING_SETPOINT)
    await entity.async_set_native_value(24)
    assert cloud.puts == [(COOLING_SETPOINT, 24.0)]
    assert set(cloud.bulk_calls[0]) == {COOLING_SETPOINT, CONTROL_TYPE}
    assert entity.native_value == 24


@pytest.mark.parametrize("value", [17.5, 30.5, 24.3])
async def test_setpoint_outside_limits_or_grid_sends_nothing(hass, value):
    coordinator, cloud = _setup(hass)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(
            COOLING_SETPOINT, value, COOLING_SETPOINT_POLICY
        )
    assert cloud.puts == []


async def test_zero_is_never_a_target(hass):
    """V10: the gateway's minimum is 0 ("off" in the app)."""
    coordinator, cloud = _setup(hass, "V10")
    entity = number_entity(hass, coordinator, COOLING_SETPOINT)
    assert entity.native_min_value == 0.5
    assert entity.entity_description.native_min_value == 0.5
    with pytest.raises(ServiceValidationError):
        await entity.async_set_native_value(0)
    assert cloud.puts == []
    await entity.async_set_native_value(0.5)
    assert cloud.puts == [(COOLING_SETPOINT, 0.5)]


@pytest.mark.parametrize(
    ("minimum", "step", "expected"),
    [(0.0, 0.5, 0.5), (-1.0, 0.5, 0.5), (-0.25, 0.5, 0.25), (0.0, None, 0.5)],
)
def test_lowest_offered_value_is_the_first_grid_value_above_zero(
    minimum, step, expected
):
    assert display_minimum(COOLING_SETPOINT_POLICY, minimum, step) == expected
    # Other settings keep their minimum.
    assert display_minimum(COOLING_THRESHOLD_POLICY, minimum, step) == minimum


def test_no_setpoint_number_without_limits():
    """V11: no fallback to 0 for missing limits (unlike the app)."""
    resources = variant(FIXTURE, "V11")
    assert assess_control(resources[COOLING_SETPOINT]).rejection_reason == (
        "missing_bounds"
    )
    assert COOLING_SETPOINT not in number_paths(resources)


def test_boiler_without_cooling_keeps_its_entities():
    """V12: cooling answers 403/404; nothing below it, the base unchanged."""
    resources = variant(FIXTURE, "V12")
    assert number_paths(resources) == {"/heatingCircuits/hc1/suWiThreshold"}
    sensors = {item.key for item in build_sensor_descriptions(resources)}
    assert not any("cooling" in key for key in sensors)


# Cooling threshold ------------------------------------------------------------


def test_threshold_only_while_cooling_is_offered():
    assert COOLING_THRESHOLD in number_paths(variant(FIXTURE, "V2"))
    resources = variant(FIXTURE, "V2")
    resources[SWITCH_MODE] = variant(FIXTURE, "V1")[SWITCH_MODE]
    # The threshold is reported, but the switching offers no cooling (V1).
    assert COOLING_THRESHOLD in resources
    assert COOLING_THRESHOLD not in number_paths(resources)
    del resources[SWITCH_MODE]
    assert COOLING_THRESHOLD not in number_paths(resources)


async def test_threshold_writes_once(hass):
    coordinator, cloud = _setup(hass)
    entity = number_entity(hass, coordinator, COOLING_THRESHOLD)
    assert not entity.entity_description.entity_registry_enabled_default
    assert entity.entity_description.translation_key == "cooling_outdoor_threshold"
    await entity.async_set_native_value(24.5)
    assert cloud.puts == [(COOLING_THRESHOLD, 24.5)]
    assert set(cloud.bulk_calls[0]) == {COOLING_THRESHOLD, CONTROL_TYPE, SWITCH_MODE}


@pytest.mark.parametrize("value", [14.5, 35.5, 24.2])
async def test_threshold_outside_limits_or_grid_sends_nothing(hass, value):
    coordinator, cloud = _setup(hass)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(
            COOLING_THRESHOLD, value, COOLING_THRESHOLD_POLICY
        )
    assert cloud.puts == []


async def test_threshold_unavailable_once_cooling_is_no_longer_offered(hass):
    coordinator, cloud = _setup(hass)
    entity = number_entity(hass, coordinator, COOLING_THRESHOLD)
    assert entity.available
    put_reading(coordinator, variant(FIXTURE, "V1")[SWITCH_MODE])
    assert not entity.available


async def test_threshold_refused_when_cooling_disappears_on_the_gateway(hass):
    """The switching is read again with it: cooling gone, nothing sent."""
    coordinator, cloud = _setup(hass)
    cloud.change(
        SWITCH_MODE,
        metadata=replace(
            cloud.state[SWITCH_MODE].metadata,
            allowed_values=("automatic", "forced", "off"),
        ),
    )
    with pytest.raises(ServiceValidationError):
        await number_entity(hass, coordinator, COOLING_THRESHOLD).async_set_native_value(
            25
        )
    assert cloud.puts == []


async def test_cooling_rule_also_holds_without_the_guard(hass):
    """The contract rule itself refuses, with its own message."""
    resources = variant(FIXTURE, "V2")
    resources[SWITCH_MODE] = variant(FIXTURE, "V1")[SWITCH_MODE]
    coordinator = coordinator_for(hass, resources)
    cloud = Cloud(coordinator)
    with pytest.raises(WriteValidationError) as error:
        await coordinator.async_write_control(
            COOLING_THRESHOLD, 25, COOLING_THRESHOLD_POLICY
        )
    assert getattr(error.value, "reason", None) == "cooling_not_offered"
    assert cloud.puts == []


# Room controller and flags ------------------------------------------------------


@pytest.mark.parametrize("path", [COOLING_SETPOINT, COOLING_THRESHOLD])
async def test_room_controller_or_unused_refuses(hass, path):
    coordinator, cloud = _setup(hass)
    put_reading(coordinator, replace(coordinator.resources[CONTROL_TYPE], value="ISRC"))
    cloud.state[CONTROL_TYPE] = coordinator.resources[CONTROL_TYPE]
    with pytest.raises(ServiceValidationError) as error:
        await number_entity(hass, coordinator, path).async_set_native_value(25)
    assert error.value.translation_key == "circuit_room_controller"
    put_reading(coordinator, replace(coordinator.resources[CONTROL_TYPE], value="x"))
    cloud.state[CONTROL_TYPE] = coordinator.resources[CONTROL_TYPE]
    unused = with_flags(coordinator.resources[path], used=False, available=True)
    put_reading(coordinator, unused)
    cloud.state[path] = unused
    with pytest.raises(ServiceValidationError) as error:
        await number_entity(hass, coordinator, path).async_set_native_value(25)
    assert error.value.translation_key == "control_unused"
    assert cloud.puts == []


@pytest.mark.parametrize("path", [COOLING_SETPOINT, COOLING_THRESHOLD])
def test_unavailable_gives_no_entity(path):
    resources = variant(FIXTURE, "V2")
    resources[path] = with_flags(resources[path], used=True, available=False)
    assert path not in number_paths(resources)


# Failure paths with real write cycles ---------------------------------------


@pytest.mark.parametrize(
    ("failure", "key"),
    [
        (ResourceForbidden(COOLING_SETPOINT, 403), "write_failed"),
        (ServiceUnavailable(503), "write_not_confirmed"),
        (RateLimited(60), "write_rate_limited"),
    ],
)
async def test_failed_write_is_never_repeated(hass, failure, key):
    coordinator, cloud = _setup(hass)
    cloud.put_error = failure
    with pytest.raises(HomeAssistantError) as error:
        await number_entity(hass, coordinator, COOLING_SETPOINT).async_set_native_value(
            24
        )
    assert error.value.translation_key == key
    assert cloud.puts == [(COOLING_SETPOINT, 24.0)]


async def test_read_back_with_another_value_is_not_confirmed(hass):
    coordinator, cloud = _setup(hass)
    cloud.stuck[COOLING_THRESHOLD] = 24.0
    with pytest.raises(HomeAssistantError) as error:
        await number_entity(hass, coordinator, COOLING_THRESHOLD).async_set_native_value(
            26
        )
    assert error.value.translation_key == "write_not_confirmed"
    assert cloud.puts == [(COOLING_THRESHOLD, 26.0)]


async def test_cloud_pause_refuses(hass):
    coordinator, cloud = _setup(hass)
    coordinator.control_status = lambda: ("rate_limited", None)
    coordinator._cloud_backoff_until = float("inf")
    with pytest.raises(HomeAssistantError):
        await number_entity(hass, coordinator, COOLING_SETPOINT).async_set_native_value(
            24
        )
    assert cloud.puts == []


async def test_fahrenheit_is_written_natively(hass):
    resources = variant(FIXTURE, "V2")
    resources[COOLING_SETPOINT] = replace(
        resources[COOLING_SETPOINT],
        value=77.0,
        metadata=replace(
            resources[COOLING_SETPOINT].metadata, unit="F", minimum=64.0, maximum=86.0,
            step_size=1.0,
        ),
    )
    coordinator = coordinator_for(hass, resources)
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, COOLING_SETPOINT)
    assert entity.native_unit_of_measurement == "°F"
    with pytest.raises(ServiceValidationError):
        await entity.async_set_native_value(75.5)
    await entity.async_set_native_value(75)
    assert cloud.puts == [(COOLING_SETPOINT, 75.0)]


# Dialog and translations ----------------------------------------------------------


def test_dialog_offers_the_threshold_only_with_cooling(hass):
    coordinator = coordinator_for(hass, variant(FIXTURE, "V2"))
    assert COOLING_THRESHOLD in controls(coordinator)
    put_reading(coordinator, variant(FIXTURE, "V1")[SWITCH_MODE])
    assert COOLING_THRESHOLD not in controls(coordinator)
    assert COOLING_SETPOINT in controls(coordinator)


def test_names_and_errors_in_every_language():
    for path in sorted((COMPONENT / "translations").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("cooling_room_setpoint", "cooling_outdoor_threshold"):
            assert data["entity"]["number"][key]["name"], (path.name, key)
        assert data["exceptions"]["cooling_not_offered"]["message"]
