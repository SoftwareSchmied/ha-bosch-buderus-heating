"""Wallbox commands, settings and additional readings with synthetic data.

Every test runs without a device or network. A fake client records each
request; the read-back delays are injected. These tests show that the rules
hold for the shapes taken from the vendor app's behaviour, not that a
real wallbox behaves like this.
"""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.pointt import Gateway, PointTClient
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    ProtocolError,
    RateLimited,
    RequestTimeout,
    ResourceForbidden,
    ResourceNotFound,
    ServiceUnavailable,
    UnexpectedHttpStatus,
    WriteNotConfirmed,
    WriteRejected,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.wallbox_paths import (
    COMMANDS,
    READ_LEAVES,
    SETTING_LEAVES,
    command_body_valid,
)
from custom_components.bosch_buderus_heating.wallbox import (
    MAX_LEAF_READS,
    WallboxCoordinator,
    parse_section,
)
from custom_components.bosch_buderus_heating.wallbox_control import (
    COMMAND_SPACING,
    OPTION_AUTHENTICATE,
    OPTION_AUTHENTICATION_NAME,
    READ_BACK_DELAYS,
    WallboxCloudDown,
    WallboxRejected,
    async_pause,
    async_set_boost,
    async_set_current_limit,
    async_set_phase_limit,
    async_set_setting,
    async_set_strategy,
    async_start,
)
from custom_components.bosch_buderus_heating.wallbox_entities import (
    SPECS,
    WallboxButton,
    WallboxFlag,
    WallboxNumber,
    WallboxReading,
    WallboxSelect,
    WallboxSwitch,
    WallboxText,
    async_wallbox_call,
    cached_view,
)
from custom_components.bosch_buderus_heating.wallbox_leaves import (
    LEAVES,
    firmware_version,
    parse_leaf,
)
from custom_components.bosch_buderus_heating.writes import WriteBusy
from tests.ha_test_helpers import MockConfigEntry

ROOT = "custom_components.bosch_buderus_heating"
FIXTURE = Path(__file__).parent / "fixtures/contracts/pointt-wallbox/cp0-control.json"
GW = "wallbox-A"


def fixture():
    return json.loads(FIXTURE.read_text())


class FakeWallbox:
    """Records every request; ``react`` lets a test play the wallbox."""

    def __init__(self, data):
        self.data = data
        self.calls = []
        self.errors = {}
        self.backoff = SimpleNamespace(remaining=0.0)
        self.react = None

    def _fail(self, key):
        error = self.errors.get(key)
        if isinstance(error, list):
            error = error.pop(0) if error else None
        if error is not None:
            raise error

    async def get_wallbox_section(self, gateway, section):
        self.calls.append(("GET", section))
        self._fail(section)
        return copy.deepcopy(self.data[section])

    async def get_wallbox_leaf(self, gateway, leaf):
        assert leaf in READ_LEAVES
        self.calls.append(("GET", leaf))
        self._fail(leaf)
        if leaf not in self.data["leaves"]:
            raise ResourceNotFound(leaf, 404)
        return copy.deepcopy(self.data["leaves"][leaf])

    async def post_wallbox_command(self, gateway, command, body):
        assert command_body_valid(command, body)
        self.calls.append(("POST", command, body))
        # A reaction first: a lost answer may follow an applied command.
        if self.react is not None:
            self.react("POST", command, body)
        self._fail("POST " + command)

    async def put_wallbox_setting(self, gateway, leaf, value):
        assert leaf in SETTING_LEAVES
        self.calls.append(("PUT", leaf, value))
        if self.react is not None:
            self.react("PUT", leaf, value)
        self._fail("PUT " + leaf)

    def writes(self):
        return [call for call in self.calls if call[0] in ("POST", "PUT")]

    def telemetry(self, **values):
        self.data["telemetry"]["values"].update(values)

    def conf(self, **values):
        self.data["conf"]["values"].update(values)

    def info(self, **values):
        self.data["info"]["values"].update(values)

    def leaf(self, path, value=None, **flags):
        if value is not None:
            self.data["leaves"][path]["value"] = value
        self.data["leaves"][path].update(flags)


def settle(fake):
    """A wallbox that applies every change at once."""

    def react(method, target, body):
        if method == "PUT":
            fake.leaf(target, body)
        elif target in ("start", "authenticate"):
            fake.telemetry(wbState="Charging")
        elif target == "pause":
            fake.telemetry(wbState="SuspendedEVSE")
        elif target == "limit":
            fake.telemetry(limit=str(body["limit"]))
        elif target == "force/limit":
            fake.leaf("telemetry/forceActive", "on")
        elif target == "force/limitReset":
            fake.leaf("telemetry/forceActive", "off")

    fake.react = react


def config(hass, **options):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={"brand": "buderus", "gateway_ids": [GW]},
        options={"polling_profile": "standard", **options},
    )
    entry.add_to_hass(hass)
    return entry


async def wallbox(hass, data=None, *, options=None, discover=True):
    fake = FakeWallbox(data or fixture())
    coordinator = WallboxCoordinator(
        hass,
        fake,
        Gateway(GW, device_type="commodule"),
        config(hass, **(options or {})),
    )
    await coordinator.async_refresh()
    if discover:
        coordinator.want_leaves()
        for _ in range(len(LEAVES) // MAX_LEAF_READS + 2):
            await coordinator.async_refresh()
    fake.calls.clear()
    return coordinator, fake


def sleeper():
    return AsyncMock()


# Client allowlists -----------------------------------------------------------


@pytest.mark.parametrize(
    "leaf",
    [
        "conf/auth",
        "conf/rfid",
        "conf/rfid/secure",
        "chargelog",
        "energyhistory",
        "conf/../conf/auth",
        "conf/locked?x=1",
        "/conf/locked",
    ],
)
async def test_client_never_reads_access_control_logs_or_free_paths(leaf):
    async with aiohttp.ClientSession() as session:
        client = PointTClient(session, "synthetic")
        client._request = AsyncMock()
        with pytest.raises(ValueError):
            await client.get_wallbox_leaf(GW, leaf)
        assert not client._request.await_count


@pytest.mark.parametrize(
    ("leaf", "value"),
    [
        ("conf/auth", "off"),
        ("conf/rfid/secure", "off"),
        ("conf/rfid", []),
        ("conf/currency", "EUR"),
        ("conf/locked", True),
        ("conf/overloadProtection/currentLimit", [32.0, 32.0]),
        ("conf/overloadProtection/currentLimit", [32, 32, 32]),
    ],
)
async def test_client_never_writes_access_control_or_malformed_settings(leaf, value):
    async with aiohttp.ClientSession() as session:
        client = PointTClient(session, "synthetic")
        client._request = AsyncMock()
        with pytest.raises(ValueError):
            await client.put_wallbox_setting(GW, leaf, value)
        assert not client._request.await_count


@pytest.mark.parametrize(
    ("command", "body"),
    [
        ("rfid/teachIn", {}),
        ("start/../rfid/teachIn", {"name": ""}),
        ("start", {"name": 1}),
        ("start", {"name": "", "extra": 1}),
        ("start", {"name": "\nx"}),
        ("limit", {"limit": 5}),
        ("limit", {"limit": 64}),
        ("limit", {"limit": 10.0}),
        ("limit", {"limit": True}),
        ("force/limitReset", {"limit": 6}),
        ("pause", []),
    ],
)
async def test_client_commands_are_a_fixed_list_with_exact_bodies(command, body):
    async with aiohttp.ClientSession() as session:
        client = PointTClient(session, "synthetic")
        client._request = AsyncMock()
        with pytest.raises(ValueError):
            await client.post_wallbox_command(GW, command, body)
        assert not client._request.await_count


async def test_client_sends_each_request_once_with_its_body_and_path():
    async with aiohttp.ClientSession() as session:
        client = PointTClient(session, "synthetic")
        client._request = AsyncMock(return_value=None)
        await client.post_wallbox_command(GW, "start", {"name": ""})
        await client.post_wallbox_command(GW, "force/limit", {"limit": 16})
        await client.post_wallbox_command(GW, "force/limitReset", {})
        await client.put_wallbox_setting(
            GW, "conf/overloadProtection/currentLimit", [32.0, 25.0, 32.0]
        )
        await client.get_wallbox_leaf(GW, "conf/locked")
        calls = client._request.await_args_list
        assert [call.args for call in calls] == [
            ("POST", f"gateways/{GW}/resource/rest/v1/cp0/cmd/start"),
            ("POST", f"gateways/{GW}/resource/rest/v1/cp0/cmd/force/limit"),
            ("POST", f"gateways/{GW}/resource/rest/v1/cp0/cmd/force/limitReset"),
            (
                "PUT",
                f"gateways/{GW}/resource/rest/v1/cp0/conf/overloadProtection/currentLimit",
            ),
            ("GET", f"gateways/{GW}/resource/rest/v1/cp0/conf/locked"),
        ]
        assert [call.kwargs.get("json_body") for call in calls] == [
            {"name": ""},
            {"limit": 16},
            {},
            {"value": [32.0, 25.0, 32.0]},
            None,
        ]
        # Commands and settings are never retried by the transport.
        assert [call.kwargs["retryable"] for call in calls] == [
            False,
            False,
            False,
            False,
            True,
        ]


def test_lists_hold_no_access_control_and_every_value_is_cataloged():
    forbidden = {"conf/auth", "conf/rfid", "conf/rfid/secure", "chargelog"}
    assert not forbidden & (READ_LEAVES | SETTING_LEAVES)
    assert "rfid/teachIn" not in COMMANDS
    assert set(LEAVES) == READ_LEAVES
    assert SETTING_LEAVES <= READ_LEAVES


# Start and pause ---------------------------------------------------------------


async def test_start_sends_one_command_and_confirms_the_state(hass):
    data = fixture()
    data["info"]["values"]["softwareVersion"] = "5.0.0"
    coordinator, fake = await wallbox(hass, data)
    settle(fake)
    sleep = sleeper()
    await async_start(coordinator, sleep=sleep)
    assert fake.writes() == [("POST", "start", {"name": ""})]
    assert sleep.await_count == 1
    assert coordinator.reading("telemetry", "state") == "charging"
    assert coordinator.write_counts == {"start_confirmed": 1}


async def test_start_authenticates_first_only_when_allowed(hass):
    coordinator, fake = await wallbox(
        hass,
        options={OPTION_AUTHENTICATE: True, OPTION_AUTHENTICATION_NAME: "Garage"},
    )
    fake.conf(auth="on")
    settle(fake)
    await async_start(coordinator, sleep=sleeper())
    assert fake.writes() == [
        ("POST", "authenticate", {"name": "Garage"}),
        ("POST", "start", {"name": "Garage"}),
    ]


async def test_refused_authentication_sends_no_start(hass):
    coordinator, fake = await wallbox(hass, options={OPTION_AUTHENTICATE: True})
    fake.conf(auth="on")
    fake.errors["POST authenticate"] = ResourceForbidden("cmd", 403)
    with pytest.raises(WriteRejected):
        await async_start(coordinator, sleep=sleeper())
    assert fake.writes() == [("POST", "authenticate", {"name": ""})]


async def test_unclear_authentication_sends_no_start(hass):
    coordinator, fake = await wallbox(hass, options={OPTION_AUTHENTICATE: True})
    fake.conf(auth="on")
    fake.errors["POST authenticate"] = RequestTimeout()
    with pytest.raises(WriteNotConfirmed):
        await async_start(coordinator, sleep=sleeper())
    assert fake.writes() == [("POST", "authenticate", {"name": ""})]


@pytest.mark.parametrize(
    ("software", "allowed"), [("5.1.0", False), ("5.0.0", False), (None, True)]
)
async def test_authentication_needs_the_option_and_firmware(hass, software, allowed):
    """Without the option, or (with it) without a known software, a station
    that requires authentication gets no command."""
    data = fixture()
    if software is None:
        del data["info"]["values"]["softwareVersion"]
    else:
        data["info"]["values"]["softwareVersion"] = software
    coordinator, fake = await wallbox(
        hass, data, options={OPTION_AUTHENTICATE: allowed}
    )
    fake.conf(auth="on")
    await coordinator.async_refresh()
    coordinator._next.clear()
    await coordinator.async_refresh()
    button = WallboxButton(coordinator, _spec("start_charging"))
    assert not button.available
    fake.calls.clear()
    with pytest.raises(WallboxRejected) as caught:
        await async_start(coordinator, sleep=sleeper())
    assert caught.value.translation_key == "wallbox_authentication_required"
    assert fake.writes() == []


async def test_start_below_software_5_1_sends_the_start_alone(hass):
    """Below software 5.1.0 the manufacturer's app sends ``start`` without
    ``authenticate`` to a station that requires authentication; so does the
    integration, when the option allows starts with authentication."""
    data = fixture()
    data["info"]["values"]["softwareVersion"] = "5.0.0"
    coordinator, fake = await wallbox(
        hass,
        data,
        options={OPTION_AUTHENTICATE: True, OPTION_AUTHENTICATION_NAME: "Garage"},
    )
    fake.conf(auth="on")
    await coordinator.async_refresh()
    coordinator._next.clear()
    await coordinator.async_refresh()
    button = WallboxButton(coordinator, _spec("start_charging"))
    assert button.available
    settle(fake)
    fake.calls.clear()
    await async_start(coordinator, sleep=sleeper())
    assert fake.writes() == [("POST", "start", {"name": "Garage"})]


async def test_pause_sends_one_command_and_confirms_the_station_pause(hass):
    coordinator, fake = await wallbox(hass)
    fake.telemetry(wbState="Charging")
    settle(fake)
    await async_pause(coordinator, sleep=sleeper())
    assert fake.writes() == [("POST", "pause", {"name": ""})]
    assert coordinator.reading("telemetry", "state") == "suspended_evse"


@pytest.mark.parametrize(
    "state", ["Available", "Init", "Faulted", "Locked", "Charging"]
)
async def test_start_is_refused_in_states_the_app_does_not_offer(hass, state):
    coordinator, fake = await wallbox(hass)
    fake.telemetry(wbState=state)
    with pytest.raises(WallboxRejected) as caught:
        await async_start(coordinator, sleep=sleeper())
    assert caught.value.translation_key == "wallbox_state_not_allowed"
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("limiter", "allowed"),
    [
        ("derating", False),
        ("extlimit", False),
        ("ocpp", False),
        ("", False),
        ("app", True),
        ("pv", True),
        ("future-token", True),
    ],
)
async def test_station_pause_ends_only_for_the_app_limiters(hass, limiter, allowed):
    coordinator, fake = await wallbox(hass)
    fake.telemetry(wbState="SuspendedEVSE", limiter=limiter)
    settle(fake)
    if allowed:
        await async_start(coordinator, sleep=sleeper())
        assert fake.writes() == [("POST", "start", {"name": ""})]
    else:
        with pytest.raises(WallboxRejected):
            await async_start(coordinator, sleep=sleeper())
        assert fake.writes() == []


async def test_start_below_six_amperes_is_refused_without_a_hidden_limit(hass):
    coordinator, fake = await wallbox(hass)
    fake.telemetry(wbState="SuspendedEVSE", limiter="app", limit="5")
    with pytest.raises(WallboxRejected) as caught:
        await async_start(coordinator, sleep=sleeper())
    assert caught.value.translation_key == "wallbox_current_below_minimum"
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("role", "start", "boost"),
    [("follower", False, False), ("leader", True, False), ("standalone", True, True)],
)
async def test_group_roles(hass, role, start, boost):
    coordinator, fake = await wallbox(hass)
    fake.conf(wallboxMode=role)
    settle(fake)
    for action, allowed in (
        (lambda: async_start(coordinator, sleep=sleeper()), start),
        (lambda: async_set_boost(coordinator, True, sleep=sleeper()), boost),
    ):
        coordinator.last_command = None
        if allowed:
            await action()
        else:
            with pytest.raises(WallboxRejected) as caught:
                await action()
            assert caught.value.translation_key == "wallbox_group_controlled"
    assert len(fake.writes()) == int(start) + int(boost)


async def test_energy_manager_blocks_current_but_not_start(hass):
    coordinator, fake = await wallbox(hass)
    fake.telemetry(energymanager="emma")
    settle(fake)
    with pytest.raises(WallboxRejected) as caught:
        await async_set_current_limit(coordinator, 10, sleep=sleeper())
    assert caught.value.translation_key == "wallbox_energy_manager"
    await async_start(coordinator, sleep=sleeper())
    assert fake.writes() == [("POST", "start", {"name": ""})]


async def test_refused_command_is_not_read_back_or_repeated(hass):
    coordinator, fake = await wallbox(hass)
    fake.errors["POST start"] = UnexpectedHttpStatus(400)
    sleep = sleeper()
    with pytest.raises(WriteRejected):
        await async_start(coordinator, sleep=sleep)
    assert fake.writes() == [("POST", "start", {"name": ""})]
    assert not sleep.await_count
    assert coordinator.write_counts == {"start_rejected": 1}


@pytest.mark.parametrize("error", [RequestTimeout(), ServiceUnavailable(503)])
async def test_lost_command_is_resolved_by_reading_never_by_sending(hass, error):
    coordinator, fake = await wallbox(hass)
    # The command was applied, its answer lost.
    settle(fake)
    fake.errors["POST start"] = error
    await async_start(coordinator, sleep=sleeper())
    assert fake.writes() == [("POST", "start", {"name": ""})]
    assert coordinator.write_counts == {"start_confirmed": 1}


async def test_unchanged_state_is_not_confirmed_and_never_resent(hass):
    coordinator, fake = await wallbox(hass)
    sleep = sleeper()
    with pytest.raises(WriteNotConfirmed):
        await async_start(coordinator, sleep=sleep)
    assert fake.writes() == [("POST", "start", {"name": ""})]
    assert sleep.await_count == len(READ_BACK_DELAYS)
    assert sum(call == ("GET", "telemetry") for call in fake.calls) == 1 + len(
        READ_BACK_DELAYS
    )
    assert coordinator.write_counts == {"start_not_confirmed": 1}


async def test_failed_read_back_leaves_the_command_unconfirmed(hass):
    coordinator, fake = await wallbox(hass)
    fake.errors["telemetry"] = [None, ResourceForbidden("telemetry", 403)]
    with pytest.raises(WriteNotConfirmed):
        await async_start(coordinator, sleep=sleeper())
    assert fake.writes() == [("POST", "start", {"name": ""})]


@pytest.mark.parametrize(
    "error", [ResourceNotFound("telemetry", 404), ResourceForbidden("telemetry", 403)]
)
async def test_unreadable_state_refuses_the_command(hass, error):
    coordinator, fake = await wallbox(hass)
    fake.errors["telemetry"] = error
    with pytest.raises(WallboxRejected) as caught:
        await async_start(coordinator, sleep=sleeper())
    assert caught.value.translation_key == "wallbox_value_unavailable"
    assert fake.writes() == []


async def test_cloud_pause_sends_nothing_at_all(hass):
    coordinator, fake = await wallbox(hass)
    fake.backoff.remaining = 300
    with pytest.raises(WallboxCloudDown):
        await async_start(coordinator, sleep=sleeper())
    assert fake.calls == []
    with pytest.raises(HomeAssistantError) as caught:
        await async_wallbox_call(coordinator, async_start(coordinator))
    assert caught.value.translation_key == "command_cloud_rate_limited"


async def test_rate_limited_command_is_not_read_back(hass):
    coordinator, fake = await wallbox(hass)
    fake.errors["POST start"] = RateLimited(60)
    sleep = sleeper()
    with pytest.raises(RateLimited):
        await async_start(coordinator, sleep=sleep)
    assert not sleep.await_count


async def test_commands_keep_five_seconds_apart(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    with patch(ROOT + ".wallbox_control.monotonic", return_value=1000.0) as now:
        await async_start(coordinator, sleep=sleeper())
        now.return_value += COMMAND_SPACING - 0.1
        with pytest.raises(WriteBusy):
            await async_pause(coordinator, sleep=sleeper())
        assert len(fake.writes()) == 1
        now.return_value += 0.2
        await async_pause(coordinator, sleep=sleeper())
    assert [call[1] for call in fake.writes()] == ["start", "pause"]


async def test_restart_during_read_back_sends_nothing_again(hass):
    coordinator, fake = await wallbox(hass)
    waiting = asyncio.Event()

    async def sleep(_delay):
        waiting.set()
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(async_start(coordinator, sleep=sleep))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.writes() == [("POST", "start", {"name": ""})]
    # A new coordinator after the restart has nothing pending to send.
    again, other = await wallbox(hass, fake.data)
    await again.async_refresh()
    assert other.writes() == []
    assert not coordinator.poll_lock.locked()


def _spec(key):
    return next(spec for spec in SPECS if spec.key == key)


# Charging current ------------------------------------------------------------


async def test_current_limit_is_sent_once_and_confirmed(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    await async_set_current_limit(coordinator, 10, sleep=sleeper())
    assert fake.writes() == [("POST", "limit", {"limit": 10})]
    assert coordinator.reading("telemetry", "current_limit") == 10


@pytest.mark.parametrize("value", [5, 17, 10.5, float("nan"), True])
async def test_current_limit_outside_six_to_hardware_is_refused(hass, value):
    coordinator, fake = await wallbox(hass)
    with pytest.raises(WriteValidationError):
        await async_set_current_limit(coordinator, value, sleep=sleeper())
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"conf": {"chargingStrategy": "solar-eco"}}, "wallbox_not_manual"),
        ({"leaf": "on"}, "wallbox_boost_active"),
        (
            {
                "conf": {"wallboxMode": "leader"},
                "telemetry": {"limiter": "loadbalancer"},
            },
            "wallbox_group_controlled",
        ),
        ({"telemetry": {"wbState": "SuspendedEV"}}, "wallbox_state_not_allowed"),
        ({"info": {"hwCurrentLimit": "64"}}, "wallbox_value_unavailable"),
    ],
)
async def test_current_limit_follows_the_app_conditions(hass, change, key):
    coordinator, fake = await wallbox(hass)
    fake.conf(**change.get("conf", {}))
    fake.telemetry(**change.get("telemetry", {}))
    fake.info(**change.get("info", {}))
    if "leaf" in change:
        fake.leaf("telemetry/forceActive", change["leaf"])
    with pytest.raises(WallboxRejected) as caught:
        await async_set_current_limit(coordinator, 10, sleep=sleeper())
    assert caught.value.translation_key == key
    assert fake.writes() == []


async def test_same_current_sends_nothing(hass):
    coordinator, fake = await wallbox(hass)
    await async_set_current_limit(coordinator, 16, sleep=sleeper())
    assert fake.writes() == []
    assert coordinator.write_counts == {"limit_unchanged": 1}


async def test_current_limited_elsewhere_is_not_confirmed(hass):
    coordinator, fake = await wallbox(hass)
    fake.react = lambda *_: fake.telemetry(limit="8", limiter="pv")
    with pytest.raises(WriteNotConfirmed):
        await async_set_current_limit(coordinator, 10, sleep=sleeper())
    assert fake.writes() == [("POST", "limit", {"limit": 10})]


async def test_without_hardware_limit_no_current_control_exists(hass):
    data = fixture()
    del data["info"]["values"]["hwCurrentLimit"]
    coordinator, _ = await wallbox(hass, data)
    view = cached_view(coordinator)
    assert not _spec("charging_current_limit").creatable(view)
    assert not _spec("boost").creatable(view)


async def test_limits_sent_as_json_numbers_still_create_the_controls(hass):
    # A real wallbox reports "limit": 6.3 and "hwCurrentLimit": 16 as numbers.
    data = fixture()
    data["telemetry"]["values"]["limit"] = 6.3
    data["info"]["values"]["hwCurrentLimit"] = 16
    coordinator, _ = await wallbox(hass, data)
    view = cached_view(coordinator)
    assert coordinator.reading("telemetry", "current_limit") == 6.3
    assert coordinator.reading("info", "hardware_current_limit") == 16
    assert _spec("charging_current_limit").creatable(view)
    assert _spec("boost").creatable(view)


# Boost -------------------------------------------------------------------------


async def test_boost_uses_the_hardware_maximum_and_resets_with_an_empty_body(hass):
    data = fixture()
    data["info"]["values"]["softwareVersion"] = "4.0.0"
    coordinator, fake = await wallbox(hass, data)
    settle(fake)
    await async_set_boost(coordinator, True, sleep=sleeper())
    coordinator.last_command = None
    await async_set_boost(coordinator, False, sleep=sleeper())
    assert fake.writes() == [
        ("POST", "force/limit", {"limit": 16}),
        ("POST", "force/limitReset", {}),
    ]


async def test_boost_needs_firmware_four(hass):
    data = fixture()
    data["info"]["values"]["softwareVersion"] = "3.9.9"
    coordinator, fake = await wallbox(hass, data)
    assert "telemetry/forceActive" not in coordinator.leaves
    assert not _spec("boost").creatable(cached_view(coordinator))
    with pytest.raises(WallboxRejected) as caught:
        await async_set_boost(coordinator, True, sleep=sleeper())
    assert caught.value.translation_key == "wallbox_firmware"
    assert fake.writes() == []


async def test_boost_already_on_sends_nothing_and_unconfirmed_is_reported(hass):
    coordinator, fake = await wallbox(hass)
    fake.leaf("telemetry/forceActive", "on")
    await async_set_boost(coordinator, True, sleep=sleeper())
    assert fake.writes() == []
    fake.leaf("telemetry/forceActive", "off")
    with pytest.raises(WriteNotConfirmed):
        await async_set_boost(coordinator, True, sleep=sleeper())
    assert fake.writes() == [("POST", "force/limit", {"limit": 16})]


# Charging mode -----------------------------------------------------------------


async def test_solar_mode_needs_a_connected_meter(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    await async_set_strategy(coordinator, "solar", sleep=sleeper())
    assert fake.writes() == [("PUT", "conf/chargingStrategy", "solar-eco")]
    fake.calls.clear()
    fake.leaf("telemetry/extmeterState", "ERROR")
    fake.leaf("conf/chargingStrategy", "default")
    with pytest.raises(WallboxRejected) as caught:
        await async_set_strategy(coordinator, "solar", sleep=sleeper())
    assert caught.value.translation_key == "wallbox_meter_missing"
    assert fake.writes() == []


async def test_back_to_normal_mode_waits_for_boost_to_end(hass):
    coordinator, fake = await wallbox(hass)
    fake.leaf("conf/chargingStrategy", "solar-eco")
    fake.leaf("telemetry/forceActive", "on")
    with pytest.raises(WallboxRejected) as caught:
        await async_set_strategy(coordinator, "manual", sleep=sleeper())
    assert caught.value.translation_key == "wallbox_boost_active"
    assert fake.writes() == []


async def test_setting_reported_not_writeable_is_never_written(hass):
    data = fixture()
    data["leaves"]["conf/chargingStrategy"]["writeable"] = 0
    coordinator, fake = await wallbox(hass, data)
    assert not _spec("charging_strategy").creatable(cached_view(coordinator))
    with pytest.raises(WallboxRejected) as caught:
        await async_set_strategy(coordinator, "solar", sleep=sleeper())
    assert caught.value.translation_key == "wallbox_not_writeable"
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("flag", "key"),
    [("used", "control_unused"), ("available", "wallbox_value_unavailable")],
)
async def test_unused_or_unavailable_setting_is_not_written(hass, flag, key):
    coordinator, fake = await wallbox(hass)
    fake.leaf("conf/locked", **{flag: "false"})
    with pytest.raises(WallboxRejected) as caught:
        await async_set_setting(coordinator, "locked", True, sleep=sleeper())
    assert caught.value.translation_key == key
    assert fake.writes() == []


# Settings ----------------------------------------------------------------------


async def test_solar_settings_apply_in_solar_mode_with_their_ranges(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    with pytest.raises(WallboxRejected) as caught:
        await async_set_setting(coordinator, "supplement_limit", 5000, sleep=sleeper())
    assert caught.value.translation_key == "wallbox_not_solar"
    fake.leaf("conf/chargingStrategy", "solar-eco")
    await async_set_setting(coordinator, "supplement_limit", 5000, sleep=sleeper())
    for key, value in (
        ("supplement_limit", 12000),
        ("supplement_limit", 5050),
        ("supplement_threshold", -100),
    ):
        with pytest.raises(WriteValidationError):
            await async_set_setting(coordinator, key, value, sleep=sleeper())
    await async_set_setting(coordinator, "solar_supplement", True, sleep=sleeper())
    assert fake.writes() == [
        ("PUT", "conf/solarSurplus/supplementLimit", 5000.0),
        ("PUT", "conf/solarSurplus/supplementEnable", "on"),
    ]


async def test_reported_range_narrows_the_app_range(hass):
    coordinator, fake = await wallbox(hass)
    fake.leaf("conf/chargingStrategy", "solar-eco")
    fake.leaf("conf/solarSurplus/supplementLimit", maxValue=7400, stepSize=200)
    with pytest.raises(WriteValidationError):
        await async_set_setting(coordinator, "supplement_limit", 8000, sleep=sleeper())
    with pytest.raises(WriteValidationError):
        await async_set_setting(coordinator, "supplement_limit", 5100, sleep=sleeper())
    assert fake.writes() == []


async def test_home_battery_switch_writes_minus_hundred_or_zero(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    await async_set_setting(coordinator, "home_battery_priority", True, sleep=sleeper())
    await async_set_setting(
        coordinator, "home_battery_priority", False, sleep=sleeper()
    )
    assert fake.writes() == [
        ("PUT", "conf/strategy/powerOffset", -100.0),
        ("PUT", "conf/strategy/powerOffset", 0.0),
    ]
    fake.leaf("conf/strategy/powerOffset", -37.0)
    coordinator.leaves.clear()
    for _ in range(6):
        await coordinator.async_refresh()
    switch = WallboxSwitch(coordinator, _spec("home_battery_priority"))
    assert switch.is_on is None


@pytest.mark.parametrize(
    ("value", "valid"), [(4, False), (601, False), (180.5, False), (181, True)]
)
async def test_filter_time_is_whole_seconds_from_five_to_six_hundred(
    hass, value, valid
):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    if valid:
        await async_set_setting(coordinator, "filter_time", value, sleep=sleeper())
        assert fake.writes() == [("PUT", "conf/strategy/filterTime", 181)]
        assert type(fake.writes()[0][2]) is int
    else:
        with pytest.raises(WriteValidationError):
            await async_set_setting(coordinator, "filter_time", value, sleep=sleeper())
        assert fake.writes() == []


@pytest.mark.parametrize(
    ("software", "present", "absent"),
    [
        ("5.1.0", {"supplement_limit", "filter_time", "power_limit"}, set()),
        (
            "5.0.0",
            {"filter_time", "home_battery_priority", "power_limit"},
            {"supplement_limit", "solar_supplement"},
        ),
        (
            "4.0.0",
            {"charging_strategy", "boost"},
            {"filter_time", "power_limit", "feed_in_price"},
        ),
        (
            "3.9.9",
            {"locked", "label", "electricity_price"},
            {"charging_strategy", "boost"},
        ),
        ("unknown", set(), {"start_charging", "locked"}),
    ],
)
async def test_entities_follow_the_firmware_thresholds(hass, software, present, absent):
    data = fixture()
    data["info"]["values"]["softwareVersion"] = software
    coordinator, _ = await wallbox(hass, data)
    view = cached_view(coordinator)
    creatable = {spec.key for spec in SPECS if spec.creatable(view)}
    assert present <= creatable
    assert not absent & creatable


async def test_phase_list_is_read_fresh_and_sent_whole(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    await async_set_phase_limit(coordinator, 1, 25, sleep=sleeper())
    assert fake.writes() == [
        ("PUT", "conf/overloadProtection/currentLimit", [32.0, 25.0, 32.0])
    ]
    for value in (15, 64, 20.5):
        with pytest.raises(WriteValidationError):
            await async_set_phase_limit(coordinator, 0, value, sleep=sleeper())
    fake.leaf("conf/overloadProtection/currentLimit", [32.0, 32.0])
    with pytest.raises(WallboxRejected):
        await async_set_phase_limit(coordinator, 0, 20, sleep=sleeper())
    assert len(fake.writes()) == 1
    number = WallboxNumber(coordinator, _spec("current_limit_l1"))
    assert number.native_value is None
    assert not number._usable(cached_view(coordinator))


async def test_power_limit_range_follows_the_meter_and_is_never_raised_alone(hass):
    data = fixture()
    data["leaves"]["conf/overloadProtection/powerLimit"]["value"] = 3000.0
    coordinator, fake = await wallbox(hass, data)
    # Reading a value below 5 kW writes nothing.
    assert fake.writes() == []
    number = WallboxNumber(coordinator, _spec("power_limit"))
    assert number.native_value == 3000
    assert number.native_max_value == 3 * 230 * 63
    settle(fake)
    for value in (4999, 43500):
        with pytest.raises(WriteValidationError):
            await async_set_setting(coordinator, "power_limit", value, sleep=sleeper())
    await async_set_setting(coordinator, "power_limit", 5000, sleep=sleeper())
    assert fake.writes() == [("PUT", "conf/overloadProtection/powerLimit", 5000.0)]


@pytest.mark.parametrize(
    ("brand", "meter", "created"),
    [
        ("Synthetic brand", "CONNECTED", True),
        ("HEIDELBERG Wallbox", "CONNECTED", False),
        ("Synthetic brand", "DISCONNECTED", False),
        (None, "CONNECTED", False),
    ],
)
async def test_overload_protection_needs_meter_brand_and_all_values(
    hass, brand, meter, created
):
    data = fixture()
    data["leaves"]["telemetry/extmeterState"]["value"] = meter
    if brand is None:
        del data["leaves"]["info/brand"]
    else:
        data["leaves"]["info/brand"]["value"] = brand
    coordinator, _ = await wallbox(hass, data)
    view = cached_view(coordinator)
    keys = ("current_limiter", "power_limiter", "power_limit", "current_limit_l1")
    assert all(_spec(key).creatable(view) is created for key in keys)


async def test_overload_needs_all_four_values_writeable(hass):
    data = fixture()
    data["leaves"]["conf/overloadProtection/powerLimiter"]["writeable"] = 0
    coordinator, _ = await wallbox(hass, data)
    view = cached_view(coordinator)
    assert not _spec("current_limit_l2").creatable(view)
    assert not _spec("current_limiter").creatable(view)


async def test_lock_and_display_name(hass):
    coordinator, fake = await wallbox(hass)
    settle(fake)
    await async_set_setting(coordinator, "locked", True, sleep=sleeper())
    await async_set_setting(coordinator, "locked", True, sleep=sleeper())
    for label in ("x" * 33, "bad\nname", "", " padded"):
        with pytest.raises(WriteValidationError):
            await async_set_setting(coordinator, "label", label, sleep=sleeper())
    await async_set_setting(coordinator, "label", "y" * 32, sleep=sleeper())
    assert fake.writes() == [
        ("PUT", "conf/locked", "on"),
        ("PUT", "conf/label", "y" * 32),
    ]


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (0.3, True),
        (0, True),
        (-1, False),
        (101, False),
        (float("nan"), False),
        (0.305, False),
    ],
)
async def test_electricity_price(hass, value, valid):
    coordinator, fake = await wallbox(hass)
    fake.leaf("conf/price", 0.25)
    settle(fake)
    if valid:
        await async_set_setting(
            coordinator, "electricity_price", value, sleep=sleeper()
        )
        assert fake.writes() == [("PUT", "conf/price", float(value))]
    else:
        with pytest.raises(WriteValidationError):
            await async_set_setting(
                coordinator, "electricity_price", value, sleep=sleeper()
            )
        assert fake.writes() == []


async def test_setting_read_back_that_never_answers_is_unconfirmed(hass):
    coordinator, fake = await wallbox(hass)
    fake.errors["conf/locked"] = [None] + [ServiceUnavailable(503)] * len(
        READ_BACK_DELAYS
    )
    with pytest.raises(WriteNotConfirmed):
        await async_set_setting(coordinator, "locked", True, sleep=sleeper())
    assert fake.writes() == [("PUT", "conf/locked", "on")]


async def test_group_member_changes_no_setting(hass):
    coordinator, fake = await wallbox(hass)
    fake.conf(wallboxMode="leader")
    for key, value in (("locked", True), ("electricity_price", 0.2)):
        with pytest.raises(WallboxRejected):
            await async_set_setting(coordinator, key, value, sleep=sleeper())
    assert fake.writes() == []


# Additional readings --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CONNECTED", "connected"),
        ("disconnected", "disconnected"),
        ("Error", "error"),
        ("BROKEN", None),
        (1, None),
    ],
)
def test_meter_state_is_parsed_tolerantly(raw, expected):
    leaf = parse_leaf(LEAVES["telemetry/extmeterState"], {"value": raw}, 0)
    assert leaf.value == expected
    assert not leaf.writeable


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "on",
        {"id": "/rest/v1/cp0/conf/auth", "value": "on"},
        {"id": 5, "value": "on"},
    ],
)
def test_foreign_or_malformed_single_values_are_refused(payload):
    with pytest.raises(ProtocolError):
        parse_leaf(LEAVES["conf/locked"], payload, 0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("none", "none"),
        ("nocar", "no_car"),
        ("derating", "derating"),
        ("key", "key"),
        ("paragraph14a", "paragraph_14a"),
        ("app", "app"),
        ("web", "web"),
        ("emma", "energy_manager"),
        ("modbustcp", "modbus_tcp"),
        ("ocpp", "ocpp"),
        ("loadbalancer", "load_balancer"),
        ("watchdog", "watchdog"),
        ("pv", "solar"),
        ("dynLmm", "dynamic_load_management"),
        ("staticLmm", "static_load_management"),
        ("powermeterFailSafe", "meter_fail_safe"),
        ("extlimit", "external_limit"),
        ("future", None),
        ("", None),
        (None, None),
    ],
)
def test_all_limiter_tokens_and_unknown_ones(raw, expected):
    values = parse_section("telemetry", {"values": {"limiter": raw}}).values
    assert values["limiter"] == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("inactive", "inactive"),
        ("activeCh", "active_heating"),
        ("activeDhw", "active_hot_water"),
        ("activeEm", "active_energy_manager"),
        ("notConnected", None),
        (None, None),
    ],
)
def test_energy_manager_states(raw, expected):
    assert (
        parse_section("telemetry", {"values": {"emStatus": raw}}).values[
            "energy_manager"
        ]
        == expected
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("on", True), ("off", False), ("ON", True), ("yes", None), (None, None)],
)
async def test_flags(hass, raw, expected):
    data = fixture()
    data["info"]["values"]["relaisAvailable"] = raw
    data["leaves"]["telemetry/extInputState"]["value"] = raw
    coordinator, _ = await wallbox(hass, data)
    assert WallboxFlag(coordinator, _spec("relays_available")).is_on is expected
    assert WallboxFlag(coordinator, _spec("grid_friendly_input")).is_on is expected


async def test_readings_show_values_and_unknown_never_defaults(hass):
    coordinator, fake = await wallbox(hass)
    meter = WallboxReading(coordinator, _spec("external_meter"))
    maximum = WallboxReading(coordinator, _spec("external_meter_maximum_current"))
    limiter = WallboxReading(coordinator, _spec("limiter"))
    assert (meter.native_value, maximum.native_value, limiter.native_value) == (
        "connected",
        63.0,
        "none",
    )
    fake.leaf("info/extmeterCurrentMaximum", -3)
    coordinator.leaves.pop("info/extmeterCurrentMaximum")
    for _ in range(2):
        await coordinator.async_refresh()
    assert maximum.native_value is None


async def test_default_enablement(hass):
    enabled = {spec.key for spec in SPECS if spec.enabled}
    assert enabled == {
        "external_meter",
        "limiter",
        "energy_manager",
        "grid_friendly_configured",
        "grid_friendly_active",
        "relays_available",
        "authentication_required",
    }
    from homeassistant.const import Platform

    assert all(
        not spec.enabled
        for spec in SPECS
        if spec.platform not in (Platform.SENSOR, Platform.BINARY_SENSOR)
    )


# Polling budget and diagnostics ------------------------------------------------------


async def test_one_cycle_reads_at_most_three_objects_and_four_values(hass):
    fake = FakeWallbox(fixture())
    coordinator = WallboxCoordinator(
        hass, fake, Gateway(GW, device_type="commodule"), config(hass)
    )
    coordinator.want_leaves()
    with patch(ROOT + ".wallbox.monotonic", return_value=100) as now:
        await coordinator.async_refresh()
        # The first cycle learns the firmware; values follow.
        assert len(fake.calls) <= 3 + 4
        cycles = 1
        while len(coordinator.leaves) + len(coordinator.leaf_errors) < len(LEAVES):
            before = len(fake.calls)
            now.return_value += 60
            await coordinator.async_refresh()
            assert len(fake.calls) - before <= 3 + 4
            cycles += 1
        assert cycles <= len(LEAVES) // MAX_LEAF_READS + 2
        # Without an enabled entity showing them, values are not read again.
        fake.calls.clear()
        now.return_value += 600
        await coordinator.async_refresh()
        assert all(call[1] in ("telemetry", "conf", "info") for call in fake.calls)
        release = coordinator.demand_leaf("telemetry/extmeterState")
        now.return_value += 60
        fake.calls.clear()
        await coordinator.async_refresh()
        assert ("GET", "telemetry/extmeterState") in fake.calls
        fake.calls.clear()
        now.return_value += 60
        await coordinator.async_refresh()
        assert ("GET", "telemetry/extmeterState") not in fake.calls
        release()
        now.return_value += 900
        fake.calls.clear()
        await coordinator.async_refresh()
        assert ("GET", "telemetry/extmeterState") not in fake.calls


async def test_refused_values_wait_for_new_firmware(hass):
    data = fixture()
    del data["leaves"]["conf/price"]
    fake = FakeWallbox(data)
    coordinator = WallboxCoordinator(
        hass, fake, Gateway(GW, device_type="commodule"), config(hass)
    )
    coordinator.want_leaves()
    with patch(ROOT + ".wallbox.monotonic", return_value=100) as now:
        for _ in range(8):
            now.return_value += 60
            await coordinator.async_refresh()
        assert coordinator.leaf_errors == {"conf/price": "http_404"}
        # Also an entity showing it does not bring it back.
        coordinator.demand_leaf("conf/price")
        for _ in range(3):
            now.return_value += 600
            await coordinator.async_refresh()
        assert sum(call == ("GET", "conf/price") for call in fake.calls) == 1
        fake.info(softwareVersion="5.2.0")
        coordinator._next.clear()
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        assert sum(call == ("GET", "conf/price") for call in fake.calls) == 2


async def test_single_value_transport_errors_keep_the_objects(hass):
    coordinator, fake = await wallbox(hass, discover=False)
    coordinator.want_leaves()
    fake.errors["telemetry/extmeterState"] = ServiceUnavailable(503)
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.reading("telemetry", "state") == "preparing"
    assert "telemetry/extmeterState" not in coordinator.leaf_errors


async def test_diagnostics_hold_no_names_labels_brands_or_prices(hass):
    coordinator, fake = await wallbox(
        hass, options={OPTION_AUTHENTICATION_NAME: "private-auth"}
    )
    settle(fake)
    await async_set_setting(coordinator, "label", "private-new-label", sleep=sleeper())
    report = json.dumps(coordinator.diagnostics())
    for private in (
        "private-label",
        "private-new-label",
        "private-auth",
        "Synthetic brand",
        "private-meter-serial",
        "EUR",
        "0.3",
        GW,
    ):
        assert private not in report
    assert coordinator.diagnostics()["write_counts"] == {"label_confirmed": 1}
    assert "conf/label" in coordinator.diagnostics()["values"]["writeable"]


def test_firmware_versions():
    assert firmware_version("5.1.0") == (5, 1, 0)
    assert firmware_version("v4.0.12-rc1") == (4, 0, 12)
    assert firmware_version("5.1") is None
    assert firmware_version(None) is None
    assert firmware_version("x" * 65) is None


# Entities -----------------------------------------------------------------------------


async def test_entities_map_rejections_to_translated_errors(hass):
    coordinator, fake = await wallbox(hass)
    fake.telemetry(wbState="Available")
    await coordinator.async_refresh()
    coordinator._next.clear()
    await coordinator.async_refresh()
    button = WallboxButton(coordinator, _spec("start_charging"))
    assert not button.available
    with pytest.raises(ServiceValidationError) as caught:
        await button.async_press()
    assert caught.value.translation_key == "wallbox_state_not_allowed"
    number = WallboxNumber(coordinator, _spec("charging_current_limit"))
    with pytest.raises(ServiceValidationError) as caught:
        await number.async_set_native_value(10.5)
    assert caught.value.translation_key == "write_validation_failed"


async def test_entities_show_current_values(hass):
    coordinator, _ = await wallbox(hass)
    select = WallboxSelect(coordinator, _spec("charging_strategy"))
    text = WallboxText(coordinator, _spec("label"))
    boost = WallboxSwitch(coordinator, _spec("boost"))
    current = WallboxNumber(coordinator, _spec("charging_current_limit"))
    locked = WallboxSwitch(coordinator, _spec("locked"))
    assert select.current_option == "manual"
    assert text.native_value == "private-label"
    assert boost.is_on is False
    assert locked.is_on is False
    assert (
        current.native_min_value,
        current.native_max_value,
        current.native_step,
    ) == (6, 16, 1)
    assert current.native_value == 16
    assert all(
        entity.unique_id.startswith(GW + ":wallbox:cp0:")
        for entity in (select, text, boost, current, locked)
    )
    assert all(
        entity.entity_registry_enabled_default is False
        for entity in (select, text, boost, current, locked)
    )


def test_controls_poll_every_value_their_rules_need():
    from custom_components.bosch_buderus_heating.wallbox_control import (
        METER_PATH,
        PHASE_RULE,
        SETTINGS,
        STRATEGY_PATH,
    )

    for spec in SPECS:
        rule = SETTINGS.get(spec.key)
        if spec.key.startswith("current_limit_l"):
            rule = PHASE_RULE
        if rule is None:
            continue
        if rule.meter != "none":
            assert METER_PATH in spec.shows, spec.key
        if rule.solar_only:
            assert STRATEGY_PATH in spec.shows, spec.key
        if spec.key not in ("locked", "label"):
            assert rule.path in spec.shows, spec.key


async def test_installation_facts_do_not_expire_with_the_readings(hass):
    with patch(ROOT + ".wallbox.monotonic", return_value=100) as now:
        coordinator, fake = await wallbox(hass)
        now.return_value += 5000
        coordinator._next.clear()
        await coordinator.async_refresh()
        view = cached_view(coordinator)
        assert view.leaf_value("info/brand") == "Synthetic brand"
        assert view.leaf_value("info/extmeterCurrentMaximum") == 63.0
        assert view.leaf_value("telemetry/extmeterState") is None
        assert fake.writes() == []
