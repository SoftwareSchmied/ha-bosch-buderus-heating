"""Air conditioners on a PointT gateway: detection, polling and write cycles."""

import json
from unittest.mock import patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.bosch_buderus_heating import aircon
from custom_components.bosch_buderus_heating.aircon import (
    DETECTION_PATHS,
    is_aircon_gateway,
    system_type,
)
from custom_components.bosch_buderus_heating.aircon_policies import (
    AC_CONTROL,
    AC_CONTROL_POLICY,
    ACTIVE_PROGRAM,
    ACTIVE_PROGRAM_POLICY,
    ADVANCED_FUNCTION_POLICIES,
    FAN_SPEED,
    FAN_SPEED_POLICY,
    FULL_POWER_MODE,
    OPERATION_MODE,
    OPERATION_MODE_POLICY,
    POLL_PATHS,
    PV_ENABLE,
    SETPOINT_POLICY,
    TEMPERATURE_SETPOINT,
    TEMPORARY_SETTING,
    TEMPORARY_SETTING_POLICY,
    TIMER_ON,
    TIMER_POLICY,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    AuthenticationError,
    RateLimited,
    RequestTimeout,
    ResourceForbidden,
    ServiceUnavailable,
    SourceStopped,
)
from custom_components.bosch_buderus_heating.writes import (
    ContractRejected,
    ControlUnused,
    WriteBusy,
    _policy_for_path,
    assess_control,
)
from tests.aircon_helpers import (  # noqa: F401
    GATEWAY,
    FakeUnit,
    aircon_cleanup,
    fixture,
    make_coordinator,
)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("rac", True),
        ("RAC", True),
        (" rac ", True),
        ("commodule", False),
        ("MX400", False),
        ("rac2", False),
        (None, False),
    ],
)
def test_only_the_reported_family_marks_an_air_conditioner(kind, expected):
    gateway = Gateway("synthetic", device_type=kind, model="rac air conditioner")
    assert is_aircon_gateway(gateway) is expected


def test_no_name_match_for_other_families():
    assert not is_aircon_gateway(Gateway("x", device_type="MX400", model="rac"))
    assert not is_aircon_gateway(None)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("midea", "midea"),
        ("midea_empty_model", "midea"),
        ("midea_gateway_connector", "midea_gateway_connector"),
        ("sharp", "sharp"),
        ("no_bus", None),
    ],
)
def test_system_type_from_bus_and_model(case, expected):
    detection = fixture()["detection"][case]
    assert (
        system_type(detection["/system/bus"], detection["/system/appliance/model"])
        == expected
    )


@pytest.mark.parametrize("bus", ["", None, "midea", "Knx", 1])
def test_unknown_bus_is_no_air_conditioner(bus):
    assert system_type(bus, "rac-midea-default") is None


async def test_one_bulk_request_per_cycle_after_detection(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    assert coordinator.system == "midea"
    assert coordinator.detection == "detected"
    assert unit.bulk_calls == [DETECTION_PATHS, POLL_PATHS]
    assert len(POLL_PATHS) <= 30
    assert unit.gets == [] and unit.puts == []
    await coordinator.async_refresh()
    assert unit.bulk_calls[2:] == [POLL_PATHS]
    assert coordinator.current()
    assert coordinator.interval == 60


async def test_cloud_friendly_profile_reads_every_two_minutes(hass):
    coordinator, _, _ = await make_coordinator(hass, friendly=True)
    assert coordinator.interval == 120


async def test_no_bus_builds_nothing_and_is_not_detected_again(hass):
    coordinator, unit, _ = await make_coordinator(hass, FakeUnit("no_bus"))
    assert coordinator.system is None
    assert coordinator.detection == "no_air_conditioner"
    assert not coordinator.current()
    await coordinator.async_refresh()
    assert unit.bulk_calls == [DETECTION_PATHS]


async def test_missing_bus_path_means_no_air_conditioner(hass):
    unit = FakeUnit()
    unit.status["/system/bus"] = 404
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert coordinator.detection == "no_air_conditioner"
    assert coordinator.system is None


async def test_unanswered_detection_is_retried(hass):
    unit = FakeUnit()
    unit.status["/system/bus"] = 503
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert coordinator.detection == "pending"
    del unit.status["/system/bus"]
    await coordinator.async_refresh()
    assert coordinator.system == "midea"


async def test_missing_path_leaves_its_function_out(hass):
    unit = FakeUnit()
    unit.status[FAN_SPEED] = 404
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert coordinator.data.missing[FAN_SPEED] == "http_404"
    assert coordinator.resource(FAN_SPEED) is None
    assert not coordinator.control_ready(FAN_SPEED)
    assert coordinator.control_ready(AC_CONTROL)


async def test_offline_unit_makes_every_reading_unavailable(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    for path in POLL_PATHS:
        unit.status[path] = 503
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert not coordinator.current()
    assert coordinator.resource(AC_CONTROL) is None
    assert coordinator.error == "no_reading"
    # Not detected again while offline.
    assert unit.bulk_calls.count(DETECTION_PATHS) == 1


async def test_sign_in_failure_asks_for_reauthentication(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.bulk_error = AuthenticationError("synthetic")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._read()
    assert coordinator.error == "authentication"


async def test_rate_limited_cycle_fails_without_details(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.bulk_error = RateLimited(30)
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.error == "rate_limited"


async def test_function_list_decides_where_present(hass):
    unit = FakeUnit()
    unit.resources[f"{aircon.ADVANCED_FUNCTIONS}"]["references"] = [
        {"id": "/airConditioning/ecoMode"}
    ]
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert not coordinator.control_ready(FULL_POWER_MODE)
    assert coordinator.control_ready("/airConditioning/ecoMode")
    # Without readable lists the readable leaf decides alone.
    unit.status[aircon.ADVANCED_FUNCTIONS] = 404
    unit.status[aircon.STANDARD_FUNCTIONS] = 404
    await coordinator.async_refresh()
    assert coordinator.control_ready(FULL_POWER_MODE)


# Write matrix


async def test_setpoint_is_sent_once_and_confirmed(hass):
    coordinator, unit, sleeps = await make_coordinator(hass)
    result = await coordinator.async_write(TEMPERATURE_SETPOINT, 24, SETPOINT_POLICY)
    assert unit.puts == [(TEMPERATURE_SETPOINT, 24.0)]
    assert result.value == 24.0
    assert coordinator.value(TEMPERATURE_SETPOINT) == 24.0
    # The fresh reading is one bulk request that is never retried.
    assert unit.bulk_calls[2] == (TEMPERATURE_SETPOINT,)
    assert unit.bulk_kwargs[2] == {"retryable": False}
    assert sleeps.delays == [1.0]
    assert coordinator.writes_sent == 1


@pytest.mark.parametrize("value", [15, 31, 22.5, float("nan"), 9, 41])
async def test_setpoint_outside_limits_or_grid_is_refused_unsent(hass, value):
    coordinator, unit, _ = await make_coordinator(hass)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write(TEMPERATURE_SETPOINT, value, SETPOINT_POLICY)
    assert unit.puts == []
    assert coordinator.writes_refused == 1


async def test_reported_half_degree_step_allows_half_degrees(hass):
    unit = FakeUnit()
    unit.set(TEMPERATURE_SETPOINT, stepSize=0.5)
    coordinator, _, _ = await make_coordinator(hass, unit)
    await coordinator.async_write(TEMPERATURE_SETPOINT, 22.5, SETPOINT_POLICY)
    assert unit.puts == [(TEMPERATURE_SETPOINT, 22.5)]


async def test_plausibility_limits_wide_metadata(hass):
    unit = FakeUnit()
    unit.set(TEMPERATURE_SETPOINT, minValue=0.0, maxValue=60.0)
    coordinator, _, _ = await make_coordinator(hass, unit)
    with pytest.raises(ContractRejected) as err:
        await coordinator.async_write(TEMPERATURE_SETPOINT, 45, SETPOINT_POLICY)
    assert err.value.reason == "aircon_value_not_allowed"
    assert unit.puts == []


async def test_option_not_advertised_is_refused(hass):
    unit = FakeUnit()
    unit.set(FAN_SPEED, allowedValues=["auto", "low", "high"])
    coordinator, _, _ = await make_coordinator(hass, unit)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write(FAN_SPEED, "turbo", FAN_SPEED_POLICY)
    assert unit.puts == []
    await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert unit.puts == [(FAN_SPEED, "high")]


async def test_internal_cleaning_is_never_offered_or_written(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    resource = coordinator.resource(OPERATION_MODE)
    assert "internalCleaning" not in assess_control(resource).options
    with pytest.raises(WriteValidationError):
        await coordinator.async_write(
            OPERATION_MODE, "internalCleaning", OPERATION_MODE_POLICY
        )
    assert unit.puts == []


@pytest.mark.parametrize("writable", [0, False])
async def test_not_writable_gives_no_control(hass, writable):
    unit = FakeUnit()
    unit.set(TEMPERATURE_SETPOINT, writeable=writable)
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert not coordinator.control_ready(TEMPERATURE_SETPOINT)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write(TEMPERATURE_SETPOINT, 23, SETPOINT_POLICY)
    assert unit.puts == []


@pytest.mark.parametrize("flag", [False, "false"])
async def test_unused_setting_refuses_unsent(hass, flag):
    unit = FakeUnit()
    unit.set(FAN_SPEED, used=flag)
    coordinator, _, _ = await make_coordinator(hass, unit)
    with pytest.raises(ControlUnused):
        await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert unit.puts == []


@pytest.mark.parametrize("flag", [False, "false"])
async def test_unavailable_setting_gives_no_control(hass, flag):
    unit = FakeUnit()
    unit.set(FAN_SPEED, available=flag)
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert not coordinator.control_ready(FAN_SPEED)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert unit.puts == []


async def test_rejected_put_is_not_sent_again(hass):
    coordinator, unit, sleeps = await make_coordinator(hass)
    unit.put_error = ResourceForbidden("/airConditioning/fanSpeed", 403)
    with pytest.raises(ResourceForbidden):
        await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert unit.puts == [(FAN_SPEED, "high")]
    assert sleeps.delays == []
    assert coordinator.value(FAN_SPEED) == "auto"


async def test_put_timeout_resolved_by_reading(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.put_error = RequestTimeout("synthetic")

    def applied(path):
        return {**unit.resources[path], "value": "high"}

    unit.read_back = applied
    await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert len(unit.puts) == 1


async def test_read_back_waits_one_two_four_eight_seconds_then_unconfirmed(hass):
    coordinator, unit, sleeps = await make_coordinator(hass)
    unit.apply_puts = False
    with pytest.raises(WriteNotConfirmed):
        await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert unit.puts == [(FAN_SPEED, "high")]
    assert sleeps.delays == [1.0, 2.0, 4.0, 8.0]
    assert unit.gets == [FAN_SPEED] * 4


@pytest.mark.parametrize(
    "error",
    [
        RateLimited(30),
        ServiceUnavailable(503),
        ResourceForbidden("/airConditioning/fanSpeed", 403),
    ],
)
async def test_failed_read_back_stays_unconfirmed_without_retry(hass, error):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.read_back = lambda path: error
    with pytest.raises(WriteNotConfirmed):
        await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert len(unit.puts) == 1


async def test_stopped_source_after_put_sends_nothing_again(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.read_back = lambda path: SourceStopped()
    with pytest.raises(WriteNotConfirmed):
        await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert len(unit.puts) == 1


async def test_path_missing_on_the_fresh_reading_sends_nothing(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.status[FAN_SPEED] = 404
    with pytest.raises(WriteValidationError):
        await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert unit.puts == []


async def test_rate_limit_on_the_fresh_reading_sends_nothing(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.status[FAN_SPEED] = 429
    with pytest.raises(RateLimited):
        await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert unit.puts == []


async def test_mode_change_on_a_switched_off_unit_is_refused(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    # The cache still says on; the fresh reading decides.
    unit.set(AC_CONTROL, value="off")
    with pytest.raises(ContractRejected) as err:
        await coordinator.async_write(OPERATION_MODE, "heat", OPERATION_MODE_POLICY)
    assert err.value.reason == "aircon_switched_off"
    assert unit.puts == []
    assert unit.bulk_calls[-1] == (OPERATION_MODE, AC_CONTROL, PV_ENABLE)
    # The refusal published the fresh reading.
    assert coordinator.value(AC_CONTROL) == "off"


async def test_mode_change_on_a_running_unit_is_one_put(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    await coordinator.async_write(OPERATION_MODE, "heat", OPERATION_MODE_POLICY)
    assert unit.puts == [(OPERATION_MODE, "heat")]


@pytest.mark.parametrize("mode", ["auto", "heat"])
async def test_solar_operation_blocks_auto_and_heat(hass, mode):
    unit = FakeUnit()
    unit.set(PV_ENABLE, value="on")
    coordinator, _, _ = await make_coordinator(hass, unit)
    with pytest.raises(ContractRejected) as err:
        await coordinator.async_write(OPERATION_MODE, mode, OPERATION_MODE_POLICY)
    assert err.value.reason == "aircon_pv_mode_blocked"
    assert unit.puts == []
    await coordinator.async_write(OPERATION_MODE, "dry", OPERATION_MODE_POLICY)
    assert unit.puts == [(OPERATION_MODE, "dry")]


async def test_unreadable_solar_state_sets_no_block(hass):
    unit = FakeUnit()
    unit.status[PV_ENABLE] = 404
    coordinator, _, _ = await make_coordinator(hass, unit)
    await coordinator.async_write(OPERATION_MODE, "heat", OPERATION_MODE_POLICY)
    assert unit.puts == [(OPERATION_MODE, "heat")]


async def test_same_value_sends_nothing(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    await coordinator.async_write(AC_CONTROL, "on", AC_CONTROL_POLICY)
    await coordinator.async_write(TEMPERATURE_SETPOINT, 22, SETPOINT_POLICY)
    assert unit.puts == []


async def test_too_short_send_window_sends_nothing(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    with (
        patch.object(aircon, "WRITE_DEADLINE_SECONDS", 10.0),
        pytest.raises(WriteBusy),
    ):
        await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert unit.puts == []


async def test_write_without_a_current_reading_is_refused(hass):
    coordinator, unit, _ = await make_coordinator(hass, refresh=False)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write(FAN_SPEED, "high", FAN_SPEED_POLICY)
    assert unit.bulk_calls == [] and unit.puts == []


async def test_timer_on_its_grid_and_counting_down(hass):
    coordinator, unit, _ = await make_coordinator(hass)

    def counted_down(path):
        payload = dict(unit.resources[path])
        if payload["value"]:
            payload["value"] = payload["value"] - 1
        return payload

    unit.read_back = counted_down
    await coordinator.async_write(TIMER_ON, 60, TIMER_POLICY)
    assert unit.puts == [(TIMER_ON, 60.0)]
    unit.read_back = None
    await coordinator.async_write(TIMER_ON, 0, TIMER_POLICY)
    assert unit.puts[-1] == (TIMER_ON, 0.0)


@pytest.mark.parametrize("value", [45, 1470, 10, -30])
async def test_timer_off_the_grid_or_range_is_refused(hass, value):
    coordinator, unit, _ = await make_coordinator(hass)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write(TIMER_ON, value, TIMER_POLICY)
    assert unit.puts == []


async def test_timer_read_back_far_below_is_not_confirmed(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.read_back = lambda path: {**unit.resources[path], "value": 30.0}
    with pytest.raises(WriteNotConfirmed):
        await coordinator.async_write(TIMER_ON, 60, TIMER_POLICY)
    assert len(unit.puts) == 1


async def test_temporary_change_can_only_be_ended(hass):
    unit = FakeUnit()
    unit.set(TEMPORARY_SETTING, value="on")
    coordinator, _, _ = await make_coordinator(hass, unit)
    with pytest.raises(ContractRejected):
        await coordinator.async_write(TEMPORARY_SETTING, "on", TEMPORARY_SETTING_POLICY)
    await coordinator.async_write(TEMPORARY_SETTING, "off", TEMPORARY_SETTING_POLICY)
    assert unit.puts == [(TEMPORARY_SETTING, "off")]


async def test_active_program_only_from_the_reported_list(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write(ACTIVE_PROGRAM, "program3", ACTIVE_PROGRAM_POLICY)
    await coordinator.async_write(ACTIVE_PROGRAM, "program2", ACTIVE_PROGRAM_POLICY)
    assert unit.puts == [(ACTIVE_PROGRAM, "program2")]


async def test_extra_function_switch_is_one_put(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    policy = ADVANCED_FUNCTION_POLICIES[FULL_POWER_MODE]
    await coordinator.async_write(FULL_POWER_MODE, "on", policy)
    assert unit.puts == [(FULL_POWER_MODE, "on")]


@pytest.mark.parametrize(
    "name", ["ionizer", "savePlus", "turboMode", "ecoMode/../acControl", "x"]
)
def test_only_the_five_extra_function_paths_have_a_policy(name):
    assert _policy_for_path(f"/airConditioning/{name}") is None


def test_heating_paths_keep_their_policies():
    assert _policy_for_path("/heatingCircuits/hc1/operationMode") is not None
    assert _policy_for_path("/pv/enable") is None


async def test_diagnostics_contain_no_values_names_or_identifiers(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.resources[ACTIVE_PROGRAM]["value"] = "program2"
    report = coordinator.diagnostics()
    text = json.dumps(report)
    assert report["system_type"] == "midea"
    assert report["confirmed_on_hardware"] is False
    assert report["function_references"] == {"standard": 10, "advanced": 5}
    assert GATEWAY not in text
    for marker in ("21.5", "22.0", "program1", "rac-midea-default", "Midea"):
        assert marker not in text
    assert unit.bulk_calls[-1] == POLL_PATHS  # nothing was requested
