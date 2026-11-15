"""The older controls of a heating circuit follow the rules of its climate.

The manual setpoint number, the operating-mode select and their rows in the
controls dialog read the circuit's control type and the setting itself again
in one request before their one PUT: on constant values nothing is changed
(the climate's rule), a room controller (ISRC) does not lock them, and the
limits come from that fresh reading. A temporary target checks its lower
limit (the manual setpoint's minimum) on a fresh reading as well, and a
temperature number in °F takes the nearest step of its native grid.

Everything is synthetic; nothing here confirms behaviour on a real device.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating.circuit_control_writes import (
    write_guards,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.writes import (
    HEATING_CIRCUIT_TEMPORARY_SETPOINT_POLICY,
    assess_control,
)
from tests.circuit_control_helpers import (
    CONTROL_TYPE,
    ROOT,
    Cloud,
    coordinator_for,
    number_entity,
    select_entity,
    variant,
    with_flags,
)

MANUAL = ROOT + "/manualRoomSetpoint"
MODE = ROOT + "/operationMode"
MAX_FLOW = ROOT + "/maxFlowTemp"
TEMPORARY = ROOT + "/temporaryRoomSetpoint"


def _resources(control_type: str | None = "outdoorTemperature"):
    resources = variant("icom/temperature-levels.json", "V1")
    if control_type is None:
        del resources[CONTROL_TYPE]
    else:
        resources[CONTROL_TYPE] = replace(resources[CONTROL_TYPE], value=control_type)
    for item in (
        {
            "id": MANUAL,
            "type": "floatValue",
            "value": 21,
            "writeable": 1,
            "unitOfMeasure": "C",
            "minValue": 5,
            "maxValue": 30,
            "stepSize": 0.5,
        },
        {
            "id": MODE,
            "type": "stringValue",
            "value": "manual",
            "writeable": 1,
            "allowedValues": ["auto", "manual", "off"],
        },
        {
            "id": MAX_FLOW,
            "type": "floatValue",
            "value": 45,
            "writeable": 1,
            "unitOfMeasure": "C",
            "minValue": 25,
            "maxValue": 75,
        },
    ):
        resources[item["id"]] = parse_resource(item)
    return resources


def _setup(hass, control_type: str | None = "outdoorTemperature"):
    coordinator = coordinator_for(hass, _resources(control_type))
    return coordinator, Cloud(coordinator)


# The manual setpoint number and the operating-mode select -------------------


async def test_manual_setpoint_reads_control_type_and_itself_before_the_put(hass):
    coordinator, cloud = _setup(hass)
    policy = assess_control(coordinator.resources[MANUAL]).policy
    assert write_guards(coordinator, MANUAL, policy) is not None
    await number_entity(hass, coordinator, MANUAL).async_set_native_value(22)
    assert cloud.puts == [(MANUAL, 22.0)]
    assert set(cloud.bulk_calls[0]) == {MANUAL, CONTROL_TYPE}


async def test_operation_mode_reads_control_type_and_itself_before_the_put(hass):
    coordinator, cloud = _setup(hass)
    await select_entity(hass, coordinator, MODE).async_select_option("auto")
    assert cloud.puts == [(MODE, "auto")]
    assert set(cloud.bulk_calls[0]) == {MODE, CONTROL_TYPE}


async def test_constant_values_lock_the_manual_setpoint_and_the_mode(hass):
    coordinator, cloud = _setup(hass, "constants")
    with pytest.raises(ServiceValidationError) as number_error:
        await number_entity(hass, coordinator, MANUAL).async_set_native_value(22)
    assert number_error.value.translation_key == "circuit_constant_control"
    with pytest.raises(ServiceValidationError) as select_error:
        await select_entity(hass, coordinator, MODE).async_select_option("auto")
    assert select_error.value.translation_key == "circuit_constant_control"
    assert cloud.puts == []


async def test_constant_values_set_on_the_gateway_meanwhile_send_nothing(hass):
    coordinator, cloud = _setup(hass)
    cloud.change(CONTROL_TYPE, value="constants")
    with pytest.raises(ServiceValidationError):
        await number_entity(hass, coordinator, MANUAL).async_set_native_value(22)
    assert cloud.puts == []
    # The fresh reading is shown at once; the rule then refuses it as well.
    assert coordinator.data[CONTROL_TYPE].resource.value == "constants"
    with pytest.raises(ServiceValidationError) as error:
        await number_entity(hass, coordinator, MANUAL).async_set_native_value(22)
    assert error.value.translation_key == "circuit_constant_control"
    assert cloud.puts == []


async def test_a_room_controller_does_not_lock_them(hass):
    """ISRC locks the temporary target and its clearing, as in the app; the
    manual setpoint and the mode stay as on the climate entity."""
    coordinator, cloud = _setup(hass, "ISRC")
    await number_entity(hass, coordinator, MANUAL).async_set_native_value(22)
    await select_entity(hass, coordinator, MODE).async_select_option("auto")
    assert cloud.puts == [(MANUAL, 22.0), (MODE, "auto")]


async def test_limits_come_from_the_fresh_reading(hass):
    coordinator, cloud = _setup(hass)
    cloud.change(MANUAL, metadata=replace(cloud.state[MANUAL].metadata, maximum=21.5))
    entity = number_entity(hass, coordinator, MANUAL)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_native_value(22)
    assert cloud.puts == []
    assert coordinator.data[MANUAL].resource.metadata.maximum == 21.5
    # A changed value alone does not refuse a value set on the entity.
    cloud.change(MANUAL, value=20.0)
    await entity.async_set_native_value(21.5)
    assert cloud.puts == [(MANUAL, 21.5)]


async def test_without_a_control_type_the_setting_is_still_read_again(hass):
    coordinator, cloud = _setup(hass, None)
    await number_entity(hass, coordinator, MANUAL).async_set_native_value(22)
    assert cloud.puts == [(MANUAL, 22.0)]
    assert cloud.bulk_calls == [(MANUAL,)]


async def test_stale_control_type_sends_nothing(hass):
    coordinator, cloud = _setup(hass)
    snapshot = coordinator.data[CONTROL_TYPE]
    coordinator.data[CONTROL_TYPE] = replace(snapshot, available=False)
    with pytest.raises(ServiceValidationError):
        await select_entity(hass, coordinator, MODE).async_select_option("auto")
    assert cloud.puts == [] and cloud.bulk_calls == []


async def test_controls_dialog_rows_take_the_same_rules(hass):
    from custom_components.bosch_buderus_heating.controls_flow import _guards

    coordinator, _cloud = _setup(hass)
    for path in (MANUAL, MODE):
        policy = assess_control(coordinator.resources[path]).policy
        guards = _guards(coordinator, path, policy)
        assert guards is not None
        assert [item.path for item in guards] == [CONTROL_TYPE]


# The temporary target and the manual setpoint's minimum ---------------------


def _temporary_reading(manual_minimum: float):
    resources = _resources()
    resources[TEMPORARY] = parse_resource(
        {
            "id": TEMPORARY,
            "type": "floatValue",
            "value": -1,
            "writeable": 1,
            "unitOfMeasure": "C",
            "minValue": -1,
            "maxValue": 30,
            "stepSize": 0.5,
        }
    )
    resources[MANUAL] = with_flags(resources[MANUAL], minimum=manual_minimum)
    return resources


def test_temporary_floor_rule_takes_the_manual_minimum() -> None:
    check = HEATING_CIRCUIT_TEMPORARY_SETPOINT_POLICY.value_check
    assert check is not None
    resources = _temporary_reading(10.0)
    temporary = resources[TEMPORARY]
    assert check(temporary, 9.5, resources) == "write_validation_failed"
    assert check(temporary, 10.0, resources) is None
    # The clear value, a positive advertised minimum and a missing or
    # unusable manual minimum are not limited by it.
    assert check(temporary, -1.0, resources) is None
    assert check(with_flags(temporary, minimum=5.0), 6.0, resources) is None
    assert check(temporary, 6.0, {}) is None
    assert check(temporary, 6.0, _temporary_reading(0.0)) is None


async def test_temporary_target_checks_the_current_manual_minimum(hass):
    """The manual setpoint's minimum rose on the gateway after the poll: the
    temporary target below it is refused unsent."""
    from custom_components.bosch_buderus_heating.heating_climate import (
        HeatingCircuitClimate,
    )

    resources = _temporary_reading(5.0)
    resources[MODE] = replace(resources[MODE], value="auto")
    coordinator = coordinator_for(hass, resources)
    cloud = Cloud(coordinator)
    entity = HeatingCircuitClimate(coordinator, ROOT)
    entity.hass = hass
    cloud.change(MANUAL, metadata=replace(cloud.state[MANUAL].metadata, minimum=10.0))
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=8)
    assert cloud.puts == []
    assert MANUAL in cloud.bulk_calls[0]
    await entity.async_set_temperature(temperature=10)
    assert cloud.puts == [(TEMPORARY, 10.0)]


# °F: the nearest step of the native grid ------------------------------------


async def test_fahrenheit_value_takes_the_nearest_native_step(hass):
    hass.config.units = US_CUSTOMARY_SYSTEM
    coordinator, cloud = _setup(hass)
    entity = number_entity(hass, coordinator, MAX_FLOW)
    assert entity.unit_of_measurement == "°F"
    # 120 °F = 48.89 °C; the supply limit has a 1.0 grid from 25.
    native = entity.convert_to_native_value(120.0)
    assert native == 49.0
    await entity.async_set_native_value(native)
    assert cloud.puts == [(MAX_FLOW, 49.0)]
