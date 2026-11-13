"""The temporary setpoint of a heating circuit in the mode auto.

A target set in the mode auto goes to ``temporaryRoomSetpoint``: one PUT
after a fresh read of the mode, the temporary setpoint and the heating
context, confirmed by a read-back. A clear button writes -1 when the
advertised limits include it. The shapes follow an iCom gateway (value -1,
limits 5 to 30) and variants with -1 in the limits; every value is
constructed. Nothing here confirms behaviour on a real device.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating import button as button_platform
from custom_components.bosch_buderus_heating.controls_flow import controls
from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
from custom_components.bosch_buderus_heating.heating_climate import (
    HeatingCircuitTemporaryClearButton,
    clear_button_descriptions,
    temporary_setpoint,
)
from custom_components.bosch_buderus_heating.number import build_number_descriptions
from custom_components.bosch_buderus_heating.resource_catalog import (
    slows_when_unused,
)
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    HEATING_CIRCUIT_TEMPORARY_SETPOINT_POLICY,
    RRC_CONTEXT,
    assess_control,
)
from tests.test_heating_controls import MODE, ROOT, TARGET, scalar, setup
from tests.test_write_guards import Cloud, _Lifecycle

TEMPORARY = ROOT + "/temporaryRoomSetpoint"
CURRENT = ROOT + "/currentRoomSetpoint"
TARGET_FEATURE = ClimateEntityFeature.TARGET_TEMPERATURE


def _temporary(value=-1.0, *, minimum=5.0, maximum=30.0, **metadata):
    resource = scalar(TEMPORARY, value, minimum=minimum, maximum=maximum)
    return replace(resource, metadata=replace(resource.metadata, **metadata))


def _put(coordinator, resource):
    coordinator.data[resource.path] = ResourceSnapshot(
        resource, True, datetime.now(UTC)
    )
    coordinator.resources[resource.path] = resource


def _circuit(hass, *, mode="auto", temporary=None, manual=True):
    _entry, coordinator, entity = setup(hass, mode=mode)
    _put(coordinator, temporary if temporary is not None else _temporary())
    if not manual:
        del coordinator.data[TARGET]
        del coordinator.resources[TARGET]
    cloud = Cloud(coordinator)
    return coordinator, entity, cloud


def _button(hass, coordinator):
    button = HeatingCircuitTemporaryClearButton(coordinator, ROOT)
    button.hass = hass
    return button


# The target of the mode auto ------------------------------------------------


async def test_auto_target_writes_the_temporary_setpoint_once(hass):
    coordinator, entity, cloud = _circuit(hass)
    assert entity.hvac_mode == HVACMode.AUTO
    assert entity.supported_features & TARGET_FEATURE
    assert (entity.min_temp, entity.max_temp) == (5, 30)
    assert entity.target_temperature_step == 0.5
    # Without an active temporary setpoint the program's target is shown.
    assert entity.native_target_temperature == 21
    assert entity.extra_state_attributes == {"temporary_override": False}
    await entity.async_set_temperature(temperature=22.5)
    # The fresh read of the guards precedes the one PUT; then the read-back.
    assert cloud.bulk_calls and TEMPORARY in cloud.bulk_calls[0]
    assert MODE in cloud.bulk_calls[0]
    assert cloud.puts == [(TEMPORARY, 22.5)]
    assert cloud.gets[-1] == TEMPORARY
    assert entity.native_target_temperature == 22.5
    assert entity.extra_state_attributes == {"temporary_override": True}
    # The manual setpoint and the mode stay as they were.
    assert coordinator.data[TARGET].resource.value == 19
    assert coordinator.data[MODE].resource.value == "auto"


@pytest.mark.parametrize("value", [4.5, 30.5, -1, 0, 21.3, 21.25])
async def test_auto_target_outside_the_range_or_grid_sends_nothing(hass, value):
    _, entity, cloud = _circuit(hass)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=value)
    assert cloud.puts == []


async def test_advertised_step_sets_the_grid_from_the_minimum(hass):
    _, entity, cloud = _circuit(
        hass, temporary=_temporary(minimum=5.5, maximum=29.5, step_size=1.0)
    )
    assert entity.target_temperature_step == 1.0
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22.0)
    await entity.async_set_temperature(temperature=22.5)
    assert cloud.puts == [(TEMPORARY, 22.5)]


async def test_manual_mode_still_writes_the_manual_setpoint(hass):
    _, entity, cloud = _circuit(hass, mode="manual")
    await entity.async_set_temperature(temperature=20.5)
    assert cloud.puts == [(TARGET, 20.5)]
    assert entity.extra_state_attributes == {"temporary_override": False}


async def test_temporary_changed_on_the_gateway_refuses_the_write(hass):
    _, entity, cloud = _circuit(hass)
    cloud.state[TEMPORARY] = replace(cloud.state[TEMPORARY], value=23.0)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    assert cloud.puts == []
    # The fresh reading is shown at once.
    assert entity.native_target_temperature == 23


async def test_unconfirmed_write_is_sent_once_and_reported(hass):
    _, entity, cloud = _circuit(hass)

    async def ignored(gateway, path, value):
        cloud.puts.append((path, value))

    cloud.coordinator.client.put_resource_value.side_effect = ignored
    with pytest.raises(HomeAssistantError) as error:
        await entity.async_set_temperature(temperature=22)
    assert error.value.translation_key == "write_not_confirmed"
    assert cloud.puts == [(TEMPORARY, 22.0)]


async def test_combined_mode_and_target_is_refused_in_auto(hass):
    _, entity, cloud = _circuit(hass)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22, hvac_mode=HVACMode.AUTO)
    assert cloud.puts == []


@pytest.mark.parametrize(
    "change",
    [
        {"writable": False},
        {"minimum": None},
        {"maximum": None},
        {"unit": "%"},
        {"resource_type": "stringValue"},
        {"used": False},
        {"available": False},
    ],
)
async def test_no_auto_target_without_a_writable_temporary_setpoint(hass, change):
    _, entity, cloud = _circuit(hass, temporary=_temporary(**change))
    assert not entity.supported_features & TARGET_FEATURE
    assert ATTR_TEMPERATURE in entity.extra_state_attributes
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    assert cloud.puts == []


async def test_no_auto_target_without_the_resource(hass):
    _, coordinator, entity = setup(hass, mode="auto")
    cloud = Cloud(coordinator)
    assert not entity.supported_features & TARGET_FEATURE
    assert "temporary_override" not in entity.extra_state_attributes
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    assert cloud.puts == []


async def test_no_auto_target_on_a_room_controller(hass):
    coordinator, entity, cloud = _circuit(hass)
    coordinator.control_context = lambda: RRC_CONTEXT
    assert temporary_setpoint(coordinator, ROOT, strict=False) is None
    assert not entity.supported_features & TARGET_FEATURE
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    assert cloud.puts == []


async def test_no_auto_target_for_a_constant_control_type(hass):
    coordinator, entity, cloud = _circuit(hass)
    control = scalar(ROOT + "/controlType", "constants", options=("constants",))
    _put(coordinator, control)
    cloud.state[control.path] = control
    assert entity.supported_features == ClimateEntityFeature(0)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    assert cloud.puts == []


async def test_converted_units_use_the_nearest_grid_value(hass):
    hass.config.units = US_CUSTOMARY_SYSTEM
    _, entity, cloud = _circuit(hass)
    # Home Assistant converted 72 °F into the entity's Celsius: 22.22 °C.
    await entity.async_set_temperature(temperature=22.222222)
    assert cloud.puts == [(TEMPORARY, 22.0)]


# Limits that include the clear and off values ------------------------------


def test_lower_limit_comes_from_the_manual_setpoint(hass):
    coordinator, entity, _ = _circuit(hass, temporary=_temporary(minimum=-1.0))
    assert (entity.min_temp, entity.max_temp) == (5, 30)
    manual = coordinator.data[TARGET].resource
    _put(
        coordinator,
        replace(manual, metadata=replace(manual.metadata, minimum=5.25)),
    )
    # The lowest value on the grid counted from -1.
    assert entity.min_temp == 5.5


@pytest.mark.parametrize("value", [-1, 0, 4.5])
async def test_auto_target_never_writes_the_clear_or_off_value(hass, value):
    """The limits take -1 and 0, the climate's range starts at 5."""
    _, entity, cloud = _circuit(hass, temporary=_temporary(minimum=-1.0))
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=value)
    assert cloud.puts == []
    await entity.async_set_temperature(temperature=5)
    assert cloud.puts == [(TEMPORARY, 5.0)]


def test_no_auto_target_without_a_positive_lower_limit(hass):
    _, entity, _ = _circuit(hass, temporary=_temporary(minimum=0.0), manual=False)
    assert not entity.supported_features & TARGET_FEATURE


@pytest.mark.parametrize(("value", "active"), [(0.0, True), (-1.0, False)])
def test_off_and_clear_readings_show_the_program_target(hass, value, active):
    _, entity, _ = _circuit(hass, temporary=_temporary(value, minimum=-1.0))
    assert entity.native_target_temperature == 21
    assert entity.extra_state_attributes == {"temporary_override": active}


# The clear button -----------------------------------------------------------


def test_clear_button_only_where_the_limits_take_minus_one(hass):
    coordinator, _, _ = _circuit(hass)
    resources = {path: item.resource for path, item in coordinator.data.items()}
    assert clear_button_descriptions(resources, GENERIC_CONTEXT) == ()
    resources[TEMPORARY] = _temporary(minimum=-1.0)
    assert [
        item.key for item in clear_button_descriptions(resources, GENERIC_CONTEXT)
    ] == [ROOT]
    assert clear_button_descriptions(resources, RRC_CONTEXT) == ()
    # -1 off the advertised grid counted from the minimum.
    resources[TEMPORARY] = _temporary(minimum=-1.5, step_size=1.0)
    assert clear_button_descriptions(resources, GENERIC_CONTEXT) == ()
    resources[TEMPORARY] = _temporary(minimum=-1.0)
    del resources[MODE]
    assert clear_button_descriptions(resources, GENERIC_CONTEXT) == ()


@pytest.mark.parametrize(("minimum", "count"), [(-1.0, 1), (5.0, 0)])
async def test_button_platform_adds_the_clear_button_once(hass, minimum, count):
    coordinator, _, _ = _circuit(hass, temporary=_temporary(22.0, minimum=minimum))
    lifecycle = _Lifecycle()
    scope = SimpleNamespace(
        coordinators=(coordinator,),
        bacon=None,
        async_on_unload=lifecycle.async_on_unload,
    )
    added = []
    button_platform._setup_entities(scope, added.extend)
    coordinator.async_update_listeners()
    clear = [
        item for item in added if isinstance(item, HeatingCircuitTemporaryClearButton)
    ]
    assert len(clear) == count
    lifecycle.unload()


async def test_clear_button_writes_minus_one_once(hass):
    coordinator, _, cloud = _circuit(hass, temporary=_temporary(22.0, minimum=-1.0))
    button = _button(hass, coordinator)
    assert button.unique_id == "gateway:heatingCircuits:hc2:temporaryRoomSetpoint:clear"
    assert button.available
    await button.async_press()
    assert cloud.puts == [(TEMPORARY, -1.0)]
    assert coordinator.data[TEMPORARY].resource.value == -1.0
    assert not button.available


@pytest.mark.parametrize(
    ("mode", "value"), [("auto", -1.0), ("manual", 22.0), ("off", 22.0)]
)
async def test_clear_button_needs_an_active_setpoint_in_auto(hass, mode, value):
    coordinator, _, cloud = _circuit(
        hass, mode=mode, temporary=_temporary(value, minimum=-1.0)
    )
    button = _button(hass, coordinator)
    assert not button.available
    with pytest.raises(ServiceValidationError) as error:
        await button.async_press()
    assert error.value.translation_key == "heating_temporary_setpoint_not_active"
    assert cloud.puts == []


async def test_clear_button_refuses_without_minus_one_in_the_limits(hass):
    coordinator, _, cloud = _circuit(hass, temporary=_temporary(22.0))
    button = _button(hass, coordinator)
    assert not button.available
    with pytest.raises(ServiceValidationError):
        await button.async_press()
    assert cloud.puts == []


async def test_clear_button_refuses_a_changed_mode_on_the_gateway(hass):
    coordinator, _, cloud = _circuit(hass, temporary=_temporary(22.0, minimum=-1.0))
    cloud.state[MODE] = replace(cloud.state[MODE], value="manual")
    with pytest.raises(ServiceValidationError):
        await _button(hass, coordinator).async_press()
    assert cloud.puts == []


# No entity or dialog row of its own ----------------------------------------


def test_temporary_setpoint_has_no_number_or_dialog_row(hass):
    coordinator, _, _ = _circuit(hass, temporary=_temporary(22.0))
    resource = coordinator.data[TEMPORARY].resource
    assessment = assess_control(resource)
    assert assessment.eligible
    assert assessment.policy is HEATING_CIRCUIT_TEMPORARY_SETPOINT_POLICY
    assert TEMPORARY not in controls(coordinator)
    assert TARGET in controls(coordinator)
    assert all(
        item.resource_path != TEMPORARY
        for item in build_number_descriptions({TEMPORARY: resource})
    )


def test_temporary_setpoint_keeps_the_climate_pace():
    assert not slows_when_unused(TEMPORARY)
    assert not slows_when_unused("/heatingCircuits/hc1/temporaryRoomSetpoint")
