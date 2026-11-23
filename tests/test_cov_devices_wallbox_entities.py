"""Wallbox entities, single-value parsing and the coordinator's control state.

Entities are driven through their service methods against a recording fake
wallbox: each action must end in exactly one request or in a translated,
value-free error.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.configuration_migration import (
    legacy_cloud_connection_id,
)
from custom_components.bosch_buderus_heating.managed_wallbox import (
    ManagedWallboxCoordinator,
)
from custom_components.bosch_buderus_heating.pointt import Gateway
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    AuthenticationError,
    RateLimited,
    ServiceUnavailable,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.wallbox_paths import (
    setting_value_valid,
)
from custom_components.bosch_buderus_heating.wallbox_control import (
    WallboxCloudDown,
    WallboxRejected,
    async_start,
)
from custom_components.bosch_buderus_heating.wallbox_entities import (
    WallboxButton,
    WallboxNumber,
    WallboxReading,
    WallboxSelect,
    WallboxSwitch,
    WallboxText,
    async_wallbox_call,
)
from custom_components.bosch_buderus_heating.wallbox_leaves import (
    LEAVES,
    parse_leaf,
)
from custom_components.bosch_buderus_heating.writes import WriteBusy
from tests.test_wallbox_controls import (
    GW,
    ROOT,
    _spec,
    config,
    fixture,
    settle,
    sleeper,
    wallbox,
)


@pytest.fixture(autouse=True)
def quick_read_back():
    """The entity services use the real delays; make them instant."""
    with patch(ROOT + ".wallbox_control.READ_BACK_DELAYS", (0.0,)):
        yield


async def _now(coordinator):
    coordinator._next.clear()
    await coordinator.async_refresh()


# Services of the entities ----------------------------------------------------------


async def test_buttons_press_one_command_and_follow_the_state(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    start = WallboxButton(coordinator, _spec("start_charging"))
    pause = WallboxButton(coordinator, _spec("pause_charging"))
    assert start.available
    assert not pause.available
    await start.async_press()
    assert fake.writes() == [("POST", "start", {"name": ""})]
    await _now(coordinator)
    assert pause.available
    assert not start.available
    coordinator.last_command = None
    await pause.async_press()
    assert [call[1] for call in fake.writes()] == ["start", "pause"]


async def test_number_services_send_one_request_each(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    await WallboxNumber(
        coordinator, _spec("charging_current_limit")
    ).async_set_native_value(10)
    coordinator.last_command = None
    await WallboxNumber(coordinator, _spec("current_limit_l2")).async_set_native_value(
        20
    )
    await WallboxNumber(coordinator, _spec("electricity_price")).async_set_native_value(
        0.25
    )
    assert fake.writes() == [
        ("POST", "limit", {"limit": 10}),
        ("PUT", "conf/overloadProtection/currentLimit", [32.0, 20.0, 32.0]),
        ("PUT", "conf/price", 0.25),
    ]


async def test_number_with_a_value_out_of_range_shows_a_translated_error(hass):
    coordinator, fake = await wallbox(hass)
    number = WallboxNumber(coordinator, _spec("charging_current_limit"))
    with pytest.raises(ServiceValidationError) as caught:
        await number.async_set_native_value(40)
    assert caught.value.translation_key == "write_validation_failed"
    assert fake.writes() == []


async def test_number_range_falls_back_to_the_app_range_when_reported_one_clashes(
    hass,
):
    data = fixture()
    data["leaves"]["conf/overloadProtection/powerLimit"]["minValue"] = 90000
    coordinator, _ = await wallbox(hass, data)
    number = WallboxNumber(coordinator, _spec("power_limit"))
    # The reported minimum is above the maximum the meter allows: the known
    # minimum is shown instead of a nonsensical range.
    assert (number.native_min_value, number.native_max_value) == (5000.0, 5000.0)
    assert number.native_step == 100.0


async def test_switch_services_and_states(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    boost = WallboxSwitch(coordinator, _spec("boost"))
    locked = WallboxSwitch(coordinator, _spec("locked"))
    assert boost.is_on is False
    assert locked.is_on is False
    await boost.async_turn_on()
    coordinator.last_command = None
    await boost.async_turn_off()
    await locked.async_turn_on()
    assert fake.writes() == [
        ("POST", "force/limit", {"limit": 16}),
        ("POST", "force/limitReset", {}),
        ("PUT", "conf/locked", "on"),
    ]


async def test_solar_supplement_switch_reads_on_and_off(hass):
    coordinator, fake = await wallbox(hass)
    switch = WallboxSwitch(coordinator, _spec("solar_supplement"))
    assert switch.is_on is False
    fake.leaf("conf/solarSurplus/supplementEnable", "on")
    coordinator.leaves.pop("conf/solarSurplus/supplementEnable")
    for _ in range(3):
        await _now(coordinator)
    assert switch.is_on is True


async def test_home_battery_switch_shows_a_custom_offset_as_unknown(hass):
    coordinator, fake = await wallbox(hass)
    switch = WallboxSwitch(coordinator, _spec("home_battery_priority"))
    assert switch.is_on is False
    fake.leaf("conf/strategy/powerOffset", -100.0)
    coordinator.leaves.pop("conf/strategy/powerOffset")
    for _ in range(3):
        await _now(coordinator)
    assert switch.is_on is True
    assert switch.available


async def test_select_and_text_services(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    select = WallboxSelect(coordinator, _spec("charging_strategy"))
    text = WallboxText(coordinator, _spec("label"))
    assert select.current_option == "manual"
    assert text.native_value == "private-label"
    assert text.native_max == 32
    await select.async_select_option("solar")
    # The conf object shows the confirmed value at once.
    assert select.current_option == "solar"
    await text.async_set_value("Garage")
    assert text.native_value == "Garage"
    assert fake.writes() == [
        ("PUT", "conf/chargingStrategy", "solar-eco"),
        ("PUT", "conf/label", "Garage"),
    ]


async def test_a_long_name_reported_by_the_wallbox_is_still_shown(hass):
    data = fixture()
    data["conf"]["values"]["label"] = "n" * 50
    coordinator, _ = await wallbox(hass, data)
    text = WallboxText(coordinator, _spec("label"))
    assert text.native_max == 50


async def test_text_refuses_a_name_the_wallbox_would_not_take(hass):
    coordinator, fake = await wallbox(hass)
    text = WallboxText(coordinator, _spec("label"))
    with pytest.raises(ServiceValidationError):
        await text.async_set_value("x" * 33)
    assert fake.writes() == []


async def test_readings_are_unknown_for_malformed_values(hass):
    coordinator, fake = await wallbox(hass)
    fake.leaf("telemetry/extmeterState", "garbage")
    coordinator.leaves.pop("telemetry/extmeterState")
    fake.telemetry(limiter="something-new", emStatus="activeCh")
    for _ in range(3):
        await _now(coordinator)
    assert WallboxReading(coordinator, _spec("external_meter")).native_value is None
    assert WallboxReading(coordinator, _spec("limiter")).native_value is None
    assert (
        WallboxReading(coordinator, _spec("energy_manager")).native_value
        == "active_heating"
    )


# Error translation -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (WriteNotConfirmed("x"), "write_not_confirmed"),
        (WriteBusy("x"), "write_busy"),
        (AuthenticationError("x"), "write_authentication_failed"),
        (RateLimited(5), "write_rate_limited"),
        (ServiceUnavailable(503), "write_failed"),
        (WriteValidationError("secret detail"), "write_validation_failed"),
        (WallboxRejected("wallbox_state_not_allowed"), "wallbox_state_not_allowed"),
    ],
)
async def test_errors_become_translated_messages_without_values(hass, error, key):
    coordinator, _ = await wallbox(hass)

    async def fail():
        raise error

    with pytest.raises(HomeAssistantError) as caught:
        await async_wallbox_call(coordinator, fail())
    assert caught.value.translation_key == key
    assert "secret" not in str(caught.value)


async def test_cloud_down_message_names_the_state_or_falls_back(hass):
    coordinator, _ = await wallbox(hass)

    async def down():
        raise WallboxCloudDown("x")

    # Nothing explains it any more: the general message.
    with pytest.raises(HomeAssistantError) as caught:
        await async_wallbox_call(coordinator, down())
    assert caught.value.translation_key == "write_failed"
    coordinator.client.backoff.remaining = 120
    with pytest.raises(HomeAssistantError) as caught:
        await async_wallbox_call(coordinator, down())
    assert caught.value.translation_key == "command_cloud_rate_limited"


# Single values ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (12, 12.0),
        ("12.5", 12.5),
        (True, None),
        ("x" * 40, None),
        ("abc", None),
        ("nan", None),
        ("inf", None),
        (None, None),
    ],
)
def test_numbers_are_read_only_when_finite(raw, expected):
    leaf = parse_leaf(LEAVES["conf/price"], {"value": raw}, 0)
    assert leaf.value == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (False, False), ("true", True), ("false", False), ("yes", None)],
)
def test_used_and_available_flags_accept_bools_and_their_text(raw, expected):
    leaf = parse_leaf(LEAVES["conf/price"], {"value": 1, "used": raw}, 0)
    assert leaf.used is expected
    leaf = parse_leaf(LEAVES["conf/price"], {"value": 1, "available": raw}, 0)
    assert leaf.available is expected


@pytest.mark.parametrize(
    "raw",
    [
        [1.0, 2.0],
        [1.0, 2.0, 3.0, 4.0],
        [1.0, -2.0, 3.0],
        [1.0, "x", 3.0],
        "1,2,3",
        None,
    ],
)
def test_a_phase_list_must_be_three_non_negative_numbers(raw):
    leaf = parse_leaf(LEAVES["conf/overloadProtection/currentLimit"], {"value": raw}, 0)
    assert leaf.value is None


def test_a_phase_list_of_numbers_in_text_is_read():
    leaf = parse_leaf(
        LEAVES["conf/overloadProtection/currentLimit"],
        {"value": [16, "20", 32.0]},
        0,
    )
    assert leaf.value == (16.0, 20.0, 32.0)


@pytest.mark.parametrize("raw", ["", "   ", "x" * 129, "bad\nname", 5, None])
def test_text_values_that_are_empty_long_or_unprintable_are_unknown(raw):
    assert parse_leaf(LEAVES["conf/label"], {"value": raw}, 0).value is None


def test_text_values_are_trimmed():
    assert parse_leaf(LEAVES["conf/label"], {"value": "  Garage "}, 0).value == "Garage"


def test_unknown_strategy_text_is_unknown():
    leaf = parse_leaf(LEAVES["conf/chargingStrategy"], {"value": "turbo"}, 0)
    assert leaf.value is None


@pytest.mark.parametrize(
    ("leaf", "value", "valid"),
    [
        ("conf/locked", "on", True),
        ("conf/price", 1, True),
        ("conf/price", 0.5, True),
        ("conf/price", True, False),
        ("conf/price", None, False),
        ("conf/price", ["x"], False),
        ("conf/auth", "on", False),
        ("conf/overloadProtection/currentLimit", [1.0, 2.0, 3.0], True),
        ("conf/overloadProtection/currentLimit", [1.0, 2.0, 3], False),
        ("conf/overloadProtection/currentLimit", "1,2,3", False),
    ],
)
def test_setting_values_allowed_on_the_wire(leaf, value, valid):
    assert setting_value_valid(leaf, value) is valid


# Coordinator --------------------------------------------------------------------------


async def test_leaves_are_served_only_while_current(hass):
    coordinator, _ = await wallbox(hass)
    path = "conf/locked"
    assert coordinator.leaf(path) is coordinator.leaves[path]
    assert coordinator.leaf("conf/unknown") is None
    assert coordinator.leaf_value(path) == "off"
    with patch(ROOT + ".wallbox.monotonic", return_value=1e9):
        assert coordinator.leaf(path) is not None
        assert coordinator.leaf_value(path) is None
    assert coordinator.leaf_value("conf/unknown") is None


async def test_two_entities_share_one_demand_until_the_last_is_removed(hass):
    coordinator, _ = await wallbox(hass)
    path = "telemetry/extmeterState"
    first = coordinator.demand_leaf(path)
    second = coordinator.demand_leaf(path)
    first()
    assert coordinator._leaf_demand[path] == 1
    second()
    assert path not in coordinator._leaf_demand


async def test_a_garbled_single_value_is_recorded_as_an_error_and_others_go_on(hass):
    data = fixture()
    data["leaves"]["conf/label"] = ["not", "an", "object"]
    coordinator, _ = await wallbox(hass, data)
    assert coordinator.leaf_errors["conf/label"] == "invalid_payload"
    assert "conf/locked" in coordinator.leaves
    assert "conf/label" not in coordinator.leaves


# Managed coordinator ------------------------------------------------------------------


class _Source:
    def __init__(self, hass, entry):
        self.connection = SimpleNamespace(hass=hass, entry=entry, connection_id="c1")
        self.live = True

    def current(self):
        return self.live

    def authentication_failed(self):
        self.live = False


async def test_a_stopped_source_takes_no_command_and_publishes_nothing(hass):
    from tests.test_wallbox_controls import FakeWallbox

    fake = FakeWallbox(fixture())
    entry = config(hass)
    source = _Source(hass, entry)
    source.connection.connection_id = legacy_cloud_connection_id(entry.entry_id)
    coordinator = ManagedWallboxCoordinator(
        source, fake, Gateway(GW, device_type="commodule")
    )
    await coordinator.async_refresh()
    assert coordinator.control_status() == (None, None)
    before = coordinator.data
    source.live = False
    assert coordinator.control_status() == ("cloud_stopped", None)
    assert not coordinator.section_fresh("telemetry")
    # A late answer of the stopped source does not replace the data.
    coordinator.async_set_updated_data({})
    assert coordinator.data is before
    fake.calls.clear()
    with pytest.raises(HomeAssistantError) as caught:
        await async_wallbox_call(coordinator, async_start(coordinator, sleep=sleeper()))
    assert caught.value.translation_key == "command_cloud_stopped"
    assert fake.calls == []
