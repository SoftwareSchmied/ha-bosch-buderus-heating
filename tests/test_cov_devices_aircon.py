"""Air conditioner: failure paths of the coordinator, the entities and the rules.

Uses the synthetic unit of ``tests/aircon_helpers``. Each test asserts the
state the user sees or the number of requests that left.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating import aircon
from custom_components.bosch_buderus_heating.aircon import DETECTION_PATHS
from custom_components.bosch_buderus_heating.aircon_climate import (
    AirconClimate,
    track_aircon_climates,
)
from custom_components.bosch_buderus_heating.aircon_entities import (
    AirconSelect,
    AirconSolarOperation,
    AirconSwitch,
    AirconTimer,
    _selects,
)
from custom_components.bosch_buderus_heating.aircon_entity import async_write
from custom_components.bosch_buderus_heating.aircon_policies import (
    AC_CONTROL,
    ADVANCED_FUNCTION_POLICIES,
    ECO_MODE,
    FAN_SPEED,
    FAN_SPEED_POLICY,
    OPERATION_MODE,
    PV_ENABLE,
    ROOM_TEMPERATURE,
    SETPOINT_POLICY,
    SLEEP_MODE,
    TEMPERATURE_SETPOINT,
    TIMER_ON,
    VALUE_NOT_ALLOWED,
    timer_check,
    timer_confirmed,
)
from custom_components.bosch_buderus_heating.aircon_program_rules import (
    CONFIGURATION,
    bounded_int,
    canonical_setting,
    parse_point,
    rules,
    setting_issue,
    wire_point,
)
from custom_components.bosch_buderus_heating.aircon_program_sensor import (
    AirconProgramSensor,
)
from custom_components.bosch_buderus_heating.platform_scope import PlatformScope
from custom_components.bosch_buderus_heating.pointt import (
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    AuthenticationError,
    RateLimited,
    ServiceUnavailable,
    SourceStopped,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.writes import (
    ControlUnused,
    WriteBusy,
    WriteDeadlineExceeded,
)
from tests.aircon_helpers import (  # noqa: F401
    FakeUnit,
    aircon_cleanup,
    make_coordinator,
)
from tests.test_aircon_programs import (
    P1,
    P2,
    program_coordinator,
    readings,
)

ROOT = "custom_components.bosch_buderus_heating"


# Polling ------------------------------------------------------------------------------


async def test_a_stopped_source_ends_the_cycle_without_an_error_text(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.bulk_error = SourceStopped()
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.error is None


async def test_a_rate_limited_item_in_the_bulk_answer_pauses_the_cycle(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.status[FAN_SPEED] = 429
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.error == "rate_limited"
    assert not coordinator.current()


async def test_a_rate_limited_detection_is_reported_and_retried(hass):
    unit = FakeUnit()
    for path in DETECTION_PATHS:
        unit.status[path] = 429
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert coordinator.error == "rate_limited"
    assert coordinator.detection == "pending"
    unit.status.clear()
    await coordinator.async_refresh()
    assert coordinator.detection == "detected"
    assert coordinator.last_update_success


async def test_switch_points_are_unknown_for_an_unlisted_active_program(hass):
    unit = FakeUnit()
    unit.set(aircon.ACTIVE_PROGRAM, value="weekend")
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert coordinator.switch_points() is None
    assert (
        coordinator.program_active("/airConditioning/switchPrograms/program1") is None
    )


# Cloud state and the write transaction ------------------------------------------------


async def test_control_status_names_why_nothing_can_be_sent(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    assert coordinator.control_status() == (None, None)
    with patch.object(coordinator, "_source_stopped", return_value=True):
        assert coordinator.control_status() == ("cloud_stopped", None)
    unit.backoff.remaining = 30
    reason, until = coordinator.control_status()
    assert reason == "rate_limited"
    assert until is not None
    unit.backoff.remaining = 0
    coordinator.last_update_success = False
    assert coordinator.control_status() == ("cloud_unreachable", None)


async def test_a_reading_that_holds_the_unit_makes_the_write_wait_and_fail(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    await coordinator._lock.acquire()
    try:
        with (
            patch.object(aircon, "WRITE_LOCK_TIMEOUT", 0.05),
            pytest.raises(WriteBusy),
        ):
            await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
        assert coordinator._lock.locked()
    finally:
        coordinator._lock.release()
    assert unit.puts == []


async def test_a_write_that_cannot_start_in_time_sends_nothing(hass):
    coordinator, unit, _ = await make_coordinator(hass)

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(3600)

    unit.get_resources_bulk = hang
    with (
        patch.object(aircon, "WRITE_DEADLINE_SECONDS", 0.2),
        pytest.raises(WriteBusy),
    ):
        await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert unit.puts == []
    assert not coordinator._lock.locked()


async def test_a_put_that_never_answers_is_not_confirmed_and_not_repeated(hass):
    coordinator, unit, _ = await make_coordinator(hass)

    async def hang(_gateway, path, value):
        unit.puts.append((path, value))
        await asyncio.sleep(3600)

    unit.put_resource_value = hang
    with (
        patch.object(aircon, "WRITE_DEADLINE_SECONDS", 0.3),
        patch(ROOT + ".write_window.send_budget", return_value=0.0),
        pytest.raises(WriteDeadlineExceeded),
    ):
        await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert unit.puts == [(FAN_SPEED, "high")]
    assert not coordinator._lock.locked()


@pytest.mark.parametrize(
    ("path", "value", "policy"),
    [
        (TEMPERATURE_SETPOINT, "warm", SETPOINT_POLICY),
        (TEMPERATURE_SETPOINT, True, SETPOINT_POLICY),
        (FAN_SPEED, 5, FAN_SPEED_POLICY),
    ],
)
async def test_a_value_of_the_wrong_kind_is_refused_and_counted(
    hass, path, value, policy
):
    coordinator, unit, _ = await make_coordinator(hass)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write(path, value, policy)
    assert unit.puts == []
    assert coordinator.writes_refused == 1
    assert coordinator.writes_sent == 0


# Weekly program writes ----------------------------------------------------------------


async def test_a_program_write_needs_a_program_path_and_a_current_reading(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    with pytest.raises(WriteValidationError):
        await coordinator.async_plan_switch_program(FAN_SPEED, lambda *_: None)
    del coordinator.data.resources[P2]
    with pytest.raises(WriteValidationError):
        await coordinator.async_plan_switch_program(P2, lambda *_: None)
    assert unit.object_puts == []


async def test_a_program_that_cannot_be_read_now_is_not_written(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    unit.status[P1] = 404
    planned = []
    with pytest.raises(WriteValidationError):
        await coordinator.async_plan_switch_program(
            P1, lambda *args: planned.append(args)
        )
    assert planned == []
    assert unit.object_puts == []
    assert coordinator.program_writes == {"refused": 1}
    assert coordinator.writes_refused == 1


# Program rules ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "point",
    [
        {"dayOfWeek": "Mo", "time": "07:00", "parameters": {"operationMode": "off"}},
        {"dayOfWeek": "Mo", "time": 420.0, "parameters": {"operationMode": "off"}},
        {"dayOfWeek": 1, "time": 420, "parameters": {"operationMode": "off"}},
        {"dayOfWeek": "Mo", "time": 420},
        "Mo 420 off",
    ],
)
def test_a_malformed_program_point_is_not_read(point):
    assert parse_point(point) is None


def test_decimals_beyond_the_canonical_text_are_kept_for_the_step_check():
    assert canonical_setting("cool 24.123 low") == "cool 24.123 low"
    assert canonical_setting("cool 24.123") == "cool 24.123"


def test_broken_configuration_entries_leave_modes_out_not_the_whole_list():
    program = readings()[P1]
    configuration = readings()[CONFIGURATION]
    valid = {
        "operationMode": {"allowedValues": ["heat"]},
        "temperatureSetpoint": {
            "minValue": 16,
            "maxValue": 30,
            "unitOfMeasure": "C",
            "stepSize": 0.5,
        },
    }
    entries = [
        "not an object",
        {
            "operationMode": {"allowedValues": ["cool"]},
            "temperatureSetpoint": "17",
        },
        {
            "operationMode": {"allowedValues": ["dry"]},
            "temperatureSetpoint": {
                "minValue": -50,
                "maxValue": -40,
                "unitOfMeasure": "C",
            },
        },
        valid,
    ]
    broken = replace(
        configuration,
        program_fields={**configuration.program_fields, "allowedValues": entries},
    )
    found = rules(program, {**readings(), CONFIGURATION: broken})
    assert list(found.modes) == ["heat"]
    assert setting_issue("heat 21.5", found) is None
    assert setting_issue("heat 21.3", found) == "value_off_step"
    assert setting_issue("cool 21.5", found) == "setting_not_allowed"


def test_a_configuration_without_a_list_of_modes_gives_no_rules():
    configuration = readings()[CONFIGURATION]
    broken = replace(
        configuration,
        program_fields={**configuration.program_fields, "allowedValues": "cool"},
    )
    assert (
        rules(readings()[P1], {**readings(), CONFIGURATION: broken})
        == "program_configuration_missing"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, None), (15.0, 15), (15.5, None), ("15", None), (0, None), (61, None)],
)
def test_limits_of_a_program_are_whole_positive_numbers(value, expected):
    assert bounded_int(value, 60) == expected


def test_a_point_is_only_built_from_a_valid_setting():
    with pytest.raises(ValueError):
        wire_point("Mo", 420, "cool warm")
    assert wire_point("Mo", 420, "cool 24 low") == {
        "dayOfWeek": "Mo",
        "time": 420,
        "parameters": {
            "operationMode": "cool",
            "temperatureSetpoint": 24.0,
            "fanSpeed": "low",
        },
    }


# Timer rules --------------------------------------------------------------------------


def test_timer_values_must_be_numbers_on_the_unit_s_grid():
    unit = FakeUnit()
    resource = parse_resource(unit.resources[TIMER_ON], path=TIMER_ON)
    assert timer_check(resource, "soon", {}) == VALUE_NOT_ALLOWED
    assert timer_check(resource, True, {}) == VALUE_NOT_ALLOWED
    assert timer_check(resource, 0, {}) is None
    assert timer_check(resource, 90, {}) is None
    assert timer_check(resource, 100, {}) == VALUE_NOT_ALLOWED
    no_minimum = replace(resource, metadata=replace(resource.metadata, minimum=None))
    assert timer_check(no_minimum, 90, {}) == VALUE_NOT_ALLOWED


@pytest.mark.parametrize(
    ("read", "requested", "confirmed"),
    [
        (0, 0, True),
        (30, 0, False),
        (89, 90, True),
        (88, 90, True),
        (87.5, 90, False),
        (20, 90, False),
        (95, 90, False),
        ("90", 90, False),
        (True, 1, False),
        (90, "90", False),
        (90, True, False),
    ],
)
def test_a_running_timer_confirms_within_its_countdown(read, requested, confirmed):
    assert timer_confirmed(read, requested) is confirmed


# Entities -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (WriteNotConfirmed("x"), "aircon_write_not_confirmed"),
        (WriteBusy("x"), "write_busy"),
        (AuthenticationError("x"), "write_authentication_failed"),
        (RateLimited(5), "write_rate_limited"),
        (ServiceUnavailable(503), "write_failed"),
        (ControlUnused("detail", contract=False), "control_unused"),
        (WriteValidationError("detail"), "write_validation_failed"),
    ],
)
async def test_write_errors_become_translated_messages(hass, error, key):
    coordinator, _, _ = await make_coordinator(hass)
    with (
        patch.object(coordinator, "async_write", AsyncMock(side_effect=error)),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await async_write(coordinator, FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert caught.value.translation_key == key
    assert "detail" not in str(caught.value)


async def test_a_refusal_while_the_cloud_went_down_names_the_cloud(hass):
    coordinator, unit, _ = await make_coordinator(hass)

    async def refused(*_args):
        unit.backoff.remaining = 60
        raise WriteValidationError("The control cannot be read now")

    with (
        patch.object(coordinator, "async_write", refused),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await async_write(coordinator, FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert caught.value.translation_key == "command_cloud_rate_limited"


async def test_a_switch_turns_off_and_follows_the_unit_s_writeability(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    switch = AirconSwitch(
        coordinator, SLEEP_MODE, ADVANCED_FUNCTION_POLICIES[SLEEP_MODE]
    )
    switch.hass = hass
    unit.set(SLEEP_MODE, value="on")
    await coordinator.async_refresh()
    assert switch.available
    await switch.async_turn_off()
    assert unit.puts == [(SLEEP_MODE, "off")]
    unit.set(SLEEP_MODE, writeable=0)
    await coordinator.async_refresh()
    assert not switch.available


async def test_only_the_active_program_select_shows_switch_points(hass):
    coordinator, _, _ = await make_coordinator(hass)
    quick = dict(_selects(coordinator))["airConditioning:quickAirFlows"]()
    assert isinstance(quick, AirconSelect)
    assert quick.extra_state_attributes is None


@pytest.mark.parametrize(
    ("minimum", "step"), [(15.0, 15.0), (45.0, 15.0), (30.5, 1.0), (30.0, 30.0)]
)
async def test_timer_step_reaches_zero_and_every_value_of_the_unit(hass, minimum, step):
    unit = FakeUnit()
    unit.set(TIMER_ON, minValue=minimum)
    coordinator, _, _ = await make_coordinator(hass, unit)
    timer = AirconTimer(coordinator, TIMER_ON)
    assert timer.available
    assert timer.native_step == step


async def test_timer_without_a_reading_has_the_default_step_and_no_value(hass):
    coordinator, _, _ = await make_coordinator(hass)
    timer = AirconTimer(coordinator, TIMER_ON)
    coordinator.last_update_success = False
    assert not timer.available
    assert timer.native_step == 30.0
    assert timer.native_max_value == 30.0
    with patch.object(coordinator, "value", return_value="soon"):
        assert timer.native_value is None


async def test_an_unusable_timer_takes_no_value(hass):
    unit = FakeUnit()
    unit.set(TIMER_ON, minValue=0.0)
    coordinator, _, _ = await make_coordinator(hass, unit)
    timer = AirconTimer(coordinator, TIMER_ON)
    timer.hass = hass
    assert not timer.available
    with pytest.raises(ServiceValidationError) as caught:
        await timer.async_set_native_value(60)
    assert caught.value.translation_key == "write_validation_failed"
    assert unit.puts == []


async def test_solar_operation_is_unavailable_without_its_reading(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    sensor = AirconSolarOperation(coordinator)
    assert sensor.available
    unit.status[PV_ENABLE] = 404
    await coordinator.async_refresh()
    assert not sensor.available


# Climate ------------------------------------------------------------------------------


def _climate(hass, coordinator):
    entity = AirconClimate(coordinator)
    entity.hass = hass
    return entity


async def test_an_unknown_power_state_gives_no_mode(hass):
    unit = FakeUnit()
    unit.set(AC_CONTROL, value="standby")
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert _climate(hass, coordinator).hvac_mode is None


async def test_a_mode_that_cannot_be_chosen_is_still_shown(hass):
    unit = FakeUnit()
    unit.set(OPERATION_MODE, value="dry", allowedValues=["cool", "heat"])
    coordinator, _, _ = await make_coordinator(hass, unit)
    entity = _climate(hass, coordinator)
    assert entity.hvac_mode == HVACMode.DRY
    assert HVACMode.DRY in entity.hvac_modes
    assert HVACMode.HEAT in entity.hvac_modes


@pytest.mark.parametrize("value", [True, float("nan"), "warm"])
async def test_a_room_temperature_that_is_not_a_number_is_unknown(hass, value):
    coordinator, _, _ = await make_coordinator(hass)
    entity = _climate(hass, coordinator)
    resource = coordinator.data.resources[ROOM_TEMPERATURE]
    coordinator.data.resources[ROOM_TEMPERATURE] = replace(resource, value=value)
    assert entity.native_current_temperature is None


async def test_without_the_eco_function_there_is_no_preset(hass):
    unit = FakeUnit()
    unit.status[ECO_MODE] = 404
    coordinator, _, _ = await make_coordinator(hass, unit)
    entity = _climate(hass, coordinator)
    assert entity.preset_modes is None
    assert entity.preset_mode is None


# Discovery ----------------------------------------------------------------------------


async def test_climates_appear_once_when_the_scope_and_the_unit_are_current(hass):
    coordinator, _, _ = await make_coordinator(hass)
    live = {"scope": False}
    unload = []
    added = []
    scope = PlatformScope(
        "entry",
        unload.append,
        aircons=(coordinator,),
        current=lambda: live["scope"],
    )
    track_aircon_climates(scope, added.extend)
    assert added == []
    live["scope"] = True
    coordinator.async_update_listeners()
    coordinator.async_update_listeners()
    assert [type(entity) for entity in added] == [AirconClimate]
    # A unit that is not current adds nothing new.
    other, _, _ = await make_coordinator(hass)
    other.last_update_success = False
    extra = []
    track_aircon_climates(
        PlatformScope("entry", unload.append, aircons=(other,)), extra.extend
    )
    assert extra == []
    for callback in unload:
        callback()


# Program sensor -----------------------------------------------------------------------


async def test_a_program_sensor_of_an_outdated_reading_shows_nothing(hass):
    coordinator, _, _ = await program_coordinator(hass)
    sensor = AirconProgramSensor(coordinator, P1)
    assert sensor.available
    coordinator.last_update_success = False
    assert not sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None
