"""Wallbox control: refusals, error paths and limits not covered elsewhere.

Same synthetic wallbox as ``test_wallbox_controls``: every request is
recorded, nothing is sent to a device. Each test states what the user sees
(the refusal key or the outcome) and how many requests went out.
"""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.bosch_buderus_heating.pointt.exceptions import (
    ProtocolError,
    RateLimited,
    ResourceForbidden,
    ServiceUnavailable,
    UnexpectedHttpStatus,
    WriteNotConfirmed,
    WriteRejected,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.wallbox_control import (
    PHASE_RULE,
    SETTINGS,
    SettingRule,
    WallboxCloudDown,
    WallboxRejected,
    WallboxView,
    _same,
    async_pause,
    async_set_boost,
    async_set_current_limit,
    async_set_phase_limit,
    async_set_setting,
    async_set_strategy,
    async_start,
    number_bounds,
    on_grid,
)
from custom_components.bosch_buderus_heating.wallbox_leaves import Leaf
from custom_components.bosch_buderus_heating.writes import (
    WriteBusy,
    WriteDeadlineExceeded,
)
from tests.test_wallbox_controls import (
    ROOT,
    FakeWallbox,
    fixture,
    settle,
    sleeper,
    wallbox,
)

METER = "telemetry/extmeterState"
FORCE = "telemetry/forceActive"
STRATEGY = "conf/chargingStrategy"
PHASES = "conf/overloadProtection/currentLimit"


def _data(software="5.1.0", **info):
    data = fixture()
    data["info"]["values"]["softwareVersion"] = software
    data["info"]["values"].update(info)
    return data


async def _refused(call, key, fake):
    with pytest.raises(WallboxRejected) as caught:
        await call
    assert caught.value.translation_key == key
    assert fake.writes() == []


# Rules of the single actions ---------------------------------------------------


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"conf": {"wallboxMode": "follower"}}, "wallbox_group_controlled"),
        ({"telemetry": {"wbState": "Preparing"}}, "wallbox_state_not_allowed"),
        ({"telemetry": {"wbState": "SuspendedEVSE"}}, "wallbox_state_not_allowed"),
    ],
)
async def test_pause_is_refused_for_a_follower_and_where_nothing_charges(
    hass, change, key
):
    coordinator, fake = await wallbox(hass)
    fake.telemetry(**change.get("telemetry", {}))
    fake.conf(**change.get("conf", {}))
    await _refused(async_pause(coordinator, sleep=sleeper()), key, fake)
    assert coordinator.write_counts == {"pause_refused": 1}


async def test_a_follower_takes_no_current_limit_but_a_leader_does(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    fake.conf(wallboxMode="follower")
    await _refused(
        async_set_current_limit(coordinator, 10, sleep=sleeper()),
        "wallbox_group_controlled",
        fake,
    )
    fake.conf(wallboxMode="leader")
    await async_set_current_limit(coordinator, 10, sleep=sleeper())
    assert [call[:2] for call in fake.writes()] == [("POST", "limit")]


async def test_boost_is_refused_while_an_energy_manager_controls_charging(hass):
    coordinator, fake = await wallbox(hass)
    fake.telemetry(energymanager="emma")
    await _refused(
        async_set_boost(coordinator, True, sleep=sleeper()),
        "wallbox_energy_manager",
        fake,
    )


async def test_boost_is_refused_when_its_state_cannot_be_read(hass):
    data = fixture()
    del data["leaves"][FORCE]
    coordinator, fake = await wallbox(hass, data)
    assert FORCE not in coordinator.leaves
    await _refused(
        async_set_boost(coordinator, True, sleep=sleeper()),
        "wallbox_value_unavailable",
        fake,
    )


@pytest.mark.parametrize(
    ("software", "change", "key"),
    [
        ("3.9.9", {}, "wallbox_firmware"),
        ("5.1.0", {"conf": {"wallboxMode": "follower"}}, "wallbox_group_controlled"),
        ("5.1.0", {"telemetry": {"energymanager": "emma"}}, "wallbox_energy_manager"),
        ("5.1.0", {"leaf": ("telemetry/extmeterState", "DISCONNECTED")}, "meter"),
    ],
)
async def test_strategy_refusals_send_nothing(hass, software, change, key):
    coordinator, fake = await wallbox(hass, _data(software))
    fake.conf(**change.get("conf", {}))
    fake.telemetry(**change.get("telemetry", {}))
    if "leaf" in change:
        fake.leaf(*change["leaf"])
        key = "wallbox_meter_missing"
    await _refused(async_set_strategy(coordinator, "solar", sleep=sleeper()), key, fake)


async def test_strategy_with_unknown_option_or_current_value_sends_nothing(hass):
    coordinator, fake = await wallbox(hass)
    with pytest.raises(WriteValidationError):
        await async_set_strategy(coordinator, "turbo", sleep=sleeper())
    await async_set_strategy(coordinator, "manual", sleep=sleeper())
    assert fake.writes() == []
    assert coordinator.write_counts == {"strategy_unchanged": 1}


@pytest.mark.parametrize(
    ("flag", "key"),
    [("used", "control_unused"), ("available", "wallbox_value_unavailable")],
)
async def test_strategy_flags_of_the_leaf_refuse_the_write(hass, flag, key):
    coordinator, fake = await wallbox(hass)
    fake.leaf(STRATEGY, **{flag: "false"})
    await _refused(async_set_strategy(coordinator, "solar", sleep=sleeper()), key, fake)


@pytest.mark.parametrize(
    ("software", "change", "key"),
    [
        ("4.0.0", {}, "wallbox_firmware"),
        ("5.1.0", {"telemetry": {"energymanager": "emma"}}, "wallbox_energy_manager"),
        ("5.1.0", {"leaf": (METER, "DISCONNECTED")}, "wallbox_meter_missing"),
    ],
)
async def test_setting_refusals_send_nothing(hass, software, change, key):
    coordinator, fake = await wallbox(hass, _data(software))
    fake.telemetry(**change.get("telemetry", {}))
    if "leaf" in change:
        fake.leaf(*change["leaf"])
    await _refused(
        async_set_setting(coordinator, "filter_time", 100, sleep=sleeper()), key, fake
    )


async def test_energy_manager_does_not_block_lock_and_label(hass):
    coordinator, fake = await wallbox(hass)
    fake.telemetry(energymanager="emma")
    settle(fake)
    await async_set_setting(coordinator, "locked", True, sleep=sleeper())
    await async_set_setting(coordinator, "label", "Garage", sleep=sleeper())
    assert [call[:2] for call in fake.writes()] == [
        ("PUT", "conf/locked"),
        ("PUT", "conf/label"),
    ]


@pytest.mark.parametrize("brand", ["Heidelberg Energy Control", None])
async def test_overload_protection_is_not_offered_for_that_brand_or_unknown(
    hass, brand
):
    data = fixture()
    if brand is None:
        del data["leaves"]["info/brand"]
    else:
        data["leaves"]["info/brand"]["value"] = brand
    coordinator, fake = await wallbox(hass, data)
    await _refused(
        async_set_setting(coordinator, "current_limiter", True, sleep=sleeper()),
        "wallbox_not_offered",
        fake,
    )


@pytest.mark.parametrize("value", ["on", 1, None])
async def test_switch_and_offset_settings_take_only_a_boolean(hass, value):
    coordinator, fake = await wallbox(hass)
    for key in ("locked", "home_battery_priority"):
        with pytest.raises(WriteValidationError):
            await async_set_setting(coordinator, key, value, sleep=sleeper())
    assert fake.writes() == []


async def test_a_whole_number_setting_refuses_a_fraction_on_a_fine_grid(hass):
    coordinator, fake = await wallbox(hass)
    fake.leaf("conf/strategy/filterTime", stepSize=0.5)
    # 180.5 is on the reported grid but filter time is whole seconds.
    with pytest.raises(WriteValidationError):
        await async_set_setting(coordinator, "filter_time", 180.5, sleep=sleeper())
    assert fake.writes() == []


@pytest.mark.parametrize("phase", [-1, 3])
async def test_unknown_phase_is_refused_before_any_read(hass, phase):
    coordinator, fake = await wallbox(hass)
    with pytest.raises(WriteValidationError):
        await async_set_phase_limit(coordinator, phase, 20, sleep=sleeper())
    assert fake.calls == []


async def test_phase_limit_is_refused_without_the_meter_and_for_odd_values(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    for value in (True, "20", float("inf")):
        with pytest.raises(WriteValidationError):
            await async_set_phase_limit(coordinator, 0, value, sleep=sleeper())
    # The same value as now: nothing to send.
    await async_set_phase_limit(coordinator, 0, 32, sleep=sleeper())
    assert fake.writes() == []
    assert coordinator.write_counts["current_limit_unchanged"] == 1
    fake.leaf(METER, "DISCONNECTED")
    await _refused(
        async_set_phase_limit(coordinator, 0, 20, sleep=sleeper()),
        "wallbox_meter_missing",
        fake,
    )


async def test_phase_limit_outside_the_reported_range_is_refused(hass):
    coordinator, fake = await wallbox(hass)
    fake.leaf(PHASES, minValue=20, maxValue=30)
    for value in (16, 31):
        with pytest.raises(WriteValidationError):
            await async_set_phase_limit(coordinator, 1, value, sleep=sleeper())
    assert fake.writes() == []


# Pure limits ---------------------------------------------------------------------


def _view(*leaves):
    return WallboxView({}, {}, {}, dict(leaves), (5, 1, 0))


def test_power_limit_maximum_follows_the_meter_and_has_a_default():
    rule = SETTINGS["power_limit"]
    cases = [
        (16.0, 3 * 230 * 16.0),
        (0.0, 43000.0),
        (-5.0, 43000.0),
        (16, 43000.0),  # an integer is not what the wallbox reports
    ]
    for meter, maximum in cases:
        view = _view(("info/extmeterCurrentMaximum", Leaf(meter)))
        assert number_bounds(view, rule, None) == (5000, maximum, 100)
    assert number_bounds(_view(), rule, None) == (5000, 43000.0, 100)


def test_reported_limits_narrow_and_the_reported_step_wins():
    rule = SETTINGS["supplement_limit"]
    narrow = Leaf(0.0, minimum=2000.0, maximum=9000.0, step=250.0)
    assert number_bounds(_view(), rule, narrow) == (2000.0, 9000.0, 250.0)
    wide = Leaf(0.0, minimum=-500.0, maximum=99999.0)
    assert number_bounds(_view(), rule, wide) == (0, 11000, 100)
    assert number_bounds(_view(), rule, Leaf(0.0)) == (0, 11000, 100)


def test_contradicting_reported_limits_give_no_range():
    rule = SETTINGS["supplement_limit"]
    assert number_bounds(_view(), rule, Leaf(0.0, minimum=9000, maximum=100)) is None


def test_rules_without_a_numeric_range_have_no_bounds():
    assert number_bounds(_view(), SETTINGS["locked"], None) is None
    open_rule = SettingRule("x", "number", 0, None, 1)
    assert number_bounds(_view(), open_rule, None) is None
    assert number_bounds(_view(), PHASE_RULE, None) == (16, 63, 1)


def test_grid_check_rejects_the_non_finite_and_off_step_values():
    assert on_grid(100.0, 0.0, 1000.0, 100.0)
    assert not on_grid(150.0, 0.0, 1000.0, 100.0)
    assert not on_grid(float("nan"), 0.0, 1000.0, 100.0)
    assert not on_grid(1100.0, 0.0, 1000.0, 100.0)


def test_values_are_compared_by_kind_not_by_python_equality():
    assert _same(True, True)
    assert not _same(True, 1)
    assert not _same(1, False)
    assert _same(1, 1.0)
    assert not _same((1.0, 2.0), (1.0,))
    assert _same((1.0, 2.0), (1, 2.0))
    assert not _same("1", 1)


# Transport and outcome ------------------------------------------------------------


@pytest.mark.parametrize(
    "error", [UnexpectedHttpStatus(500), UnexpectedHttpStatus(502), ProtocolError("x")]
)
async def test_server_error_or_garbled_answer_is_resolved_by_reading(hass, error):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    fake.errors["PUT conf/locked"] = error
    await async_set_setting(coordinator, "locked", True, sleep=sleeper())
    assert fake.writes() == [("PUT", "conf/locked", "on")]
    assert coordinator.write_counts == {"locked_confirmed": 1}


async def test_client_error_status_is_a_rejection_without_read_back(hass):
    coordinator, fake = await wallbox(hass)
    fake.errors["PUT conf/locked"] = UnexpectedHttpStatus(422)
    sleep = sleeper()
    with pytest.raises(WriteRejected):
        await async_set_setting(coordinator, "locked", True, sleep=sleep)
    assert not sleep.await_count
    assert coordinator.write_counts == {"locked_rejected": 1}


async def test_an_unreadable_value_before_the_write_refuses_it(hass):
    coordinator, fake = await wallbox(hass)
    fake.errors["conf/locked"] = ResourceForbidden("conf/locked", 403)
    await _refused(
        async_set_setting(coordinator, "locked", True, sleep=sleeper()),
        "wallbox_value_unavailable",
        fake,
    )
    assert coordinator.write_counts == {"locked_refused": 1}


async def test_a_read_back_hit_by_the_rate_limit_is_not_confirmed(hass):
    coordinator, fake = await wallbox(hass)
    fake.errors["telemetry"] = [None, RateLimited(60)]
    with pytest.raises(WriteNotConfirmed):
        await async_start(coordinator, sleep=sleeper())
    assert fake.writes() == [("POST", "start", {"name": ""})]
    assert coordinator.write_counts == {"start_not_confirmed": 1}


async def test_a_busy_wallbox_lock_sends_nothing_and_keeps_the_other_holder(hass):
    coordinator, fake = await wallbox(hass)
    await coordinator.poll_lock.acquire()
    try:
        with (
            patch(ROOT + ".wallbox_control.LOCK_TIMEOUT", 0.05),
            pytest.raises(WriteBusy),
        ):
            await async_start(coordinator, sleep=sleeper())
        assert coordinator.poll_lock.locked()
    finally:
        coordinator.poll_lock.release()
    assert fake.calls == []


class _Hanging(FakeWallbox):
    async def get_wallbox_section(self, gateway, section):
        await asyncio.sleep(3600)


async def test_a_transaction_that_cannot_start_in_time_sends_nothing(hass):
    coordinator, _ = await wallbox(hass)
    coordinator.client = _Hanging(fixture())
    with (
        patch(ROOT + ".wallbox_control.TRANSACTION_DEADLINE", 0.05),
        pytest.raises(WriteBusy),
    ):
        await async_start(coordinator, sleep=sleeper())
    assert coordinator.client.writes() == []
    assert coordinator.last_command is None
    assert coordinator.write_counts == {"start_busy": 1}
    assert not coordinator.poll_lock.locked()


async def test_a_transaction_running_out_of_time_after_sending_is_not_confirmed(hass):
    coordinator, fake = await wallbox(hass)

    async def endless(_delay):
        await asyncio.sleep(3600)

    with (
        patch(ROOT + ".wallbox_control.TRANSACTION_DEADLINE", 0.3),
        patch(ROOT + ".write_window.send_budget", return_value=0.0),
        pytest.raises(WriteDeadlineExceeded),
    ):
        await async_start(coordinator, sleep=endless)
    # Sent once, never again; the next command waits for the spacing.
    assert fake.writes() == [("POST", "start", {"name": ""})]
    assert coordinator.last_command is not None
    assert coordinator.write_counts == {"start_not_confirmed": 1}
    assert not coordinator.poll_lock.locked()


# Cloud state ----------------------------------------------------------------------


async def test_a_sign_in_that_expired_sends_nothing(hass):
    coordinator, fake = await wallbox(hass)
    coordinator.last_exception = ConfigEntryAuthFailed("expired")
    assert coordinator.control_status() == ("auth_required", None)
    with pytest.raises(WallboxCloudDown):
        await async_set_setting(coordinator, "locked", True, sleep=sleeper())
    assert fake.calls == []
    assert coordinator.write_counts == {"locked_refused": 1}


async def test_an_unreachable_cloud_sends_nothing(hass):
    coordinator, fake = await wallbox(hass)
    fake.errors["telemetry"] = ServiceUnavailable(503)
    coordinator._next.clear()
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.control_status() == ("cloud_unreachable", None)
    fake.calls.clear()
    with pytest.raises(WallboxCloudDown):
        await async_start(coordinator, sleep=sleeper())
    assert fake.calls == []
