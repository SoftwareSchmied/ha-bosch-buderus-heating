"""Devices that send nothing on their own (issue 136): availability, token
renewal grace window, the active-client report and the diagnostics."""

import asyncio
import json
from pathlib import Path
from time import monotonic as real_monotonic
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.bosch_buderus_heating.bacon.entity import BaconConnectivity
from custom_components.bosch_buderus_heating.bacon.models import (
    BaconError,
    Device,
    topic,
)
from custom_components.bosch_buderus_heating.climate import BaconClimate
from custom_components.bosch_buderus_heating.fan import BaconPurifierFan
from custom_components.bosch_buderus_heating.humidifier import BaconDehumidifier
from custom_components.bosch_buderus_heating.pointt import AuthTokens
from custom_components.bosch_buderus_heating.water_heater import BaconWaterHeater
from tests.test_bacon_contracts import token
from tests.test_bacon_session import MODULE, emit, environment
from tests.test_bacon_wire import packet, string
from tests.test_managed_bacon import (
    ROOT,
    A,
    B,
    Broker,
    connected,
    ready,
    view,
)
from tests.test_managed_pointt import eventually

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
FIXTURES = Path(__file__).parent / "fixtures/contracts"
# One second more than the interval of the state reads: no float rounding at 300.
STEP = 301


# -- unit level: all families, silent devices ---------------------------------


class Clock:
    """A controllable monotonic clock for session, entities and managed code."""

    def __init__(self) -> None:
        self.now = real_monotonic()

    def __call__(self) -> float:
        return self.now

    def patches(self):
        return (
            patch(MODULE + ".monotonic", self),
            patch(
                "custom_components.bosch_buderus_heating.bacon.entity.monotonic", self
            ),
            patch(ROOT + ".managed_bacon.monotonic", self),
            # The maintenance loop looks at the clock every 50 ms of real time.
            patch(ROOT + ".managed_bacon.MAINTENANCE_INTERVAL", 0.05),
        )


FAMILY_ENTITIES = {
    "rac/cl_7000i": BaconClimate,
    "dhw/midea": BaconWaterHeater,
    "dry/dry_8000i": BaconDehumidifier,
    "purifier/air_6001i": BaconPurifierFan,
}


def bare_environment():
    """Two claimed devices without any message yet."""
    account = environment()
    account.devices = {serial: Device(serial) for serial in account.selected}
    account._set_membership(("serial-A", "serial-B"))
    return account


def silent_unit(case):
    """A unit that only has stored information, metadata and our own reads."""
    group, name = case.split("/")
    data = json.loads((FIXTURES / f"bacon-{group}/{name}.json").read_text())
    account = bare_environment()
    for suffix, key in (
        ("topics/info", "info"),
        ("topics/meta", "metadata"),
        ("topics/sensor", "sensor"),
    ):
        emit(account, suffix, data[key], retained=True)
    emit(account, "shadows/state/get/accepted", data["shadow"])
    return account, FAMILY_ENTITIES[case](account, "serial-A"), data


@pytest.mark.parametrize("case", sorted(FAMILY_ENTITIES))
def test_silent_unit_stays_available_for_hours_on_our_own_reads(case):
    """Issue 136 for every family: the stored information and our reads suffice."""
    account, entity, data = silent_unit(case)
    device = account.devices["serial-A"]
    assert device.alive_at is None and entity.available
    connectivity = BaconConnectivity(account, "serial-A")
    clock = Clock()
    version = data["shadow"]["version"]
    with (
        patch(MODULE + ".monotonic", clock),
        patch("custom_components.bosch_buderus_heating.bacon.entity.monotonic", clock),
    ):
        for _ in range(36):
            clock.now += 300
            version += 1
            reply = json.loads(json.dumps(data["shadow"]))
            reply["version"] = version
            emit(account, "shadows/state/get/accepted", reply)
            assert entity.available and connectivity.available
            assert connectivity.is_on is True
        assert account.available("serial-A") and device.alive_at is None
        assert account.diagnostics()["online_devices"] == 1


def test_climate_without_answers_to_our_reads_expires_after_15_minutes():
    account, entity, _data = silent_unit("rac/cl_7000i")
    clock = Clock()
    with (
        patch(MODULE + ".monotonic", clock),
        patch("custom_components.bosch_buderus_heating.bacon.entity.monotonic", clock),
    ):
        clock.now += 899
        assert entity.available
        clock.now += 2
        assert not entity.available
        # Still online according to the information, so the connection entity is.
        assert BaconConnectivity(account, "serial-A").available


def test_fresh_measurements_alone_keep_the_climate_available_without_controls():
    account, entity, _data = silent_unit("rac/cl_7000i")
    clock = Clock()
    with (
        patch(MODULE + ".monotonic", clock),
        patch("custom_components.bosch_buderus_heating.bacon.entity.monotonic", clock),
    ):
        clock.now += 1000
        sample = {"items": [{"timestamp": 50, "roomTemperature": 70.0}]}
        emit(account, "topics/sensor", sample)
        assert entity.available
        assert entity.native_current_temperature is not None
        # The state expired: no operation, no modes, no target.
        assert entity.hvac_mode is None and entity.hvac_modes == []
        assert entity.native_target_temperature is None


def test_no_measurement_for_an_hour_gives_an_unknown_temperature_but_stays_available():
    account, entity, data = silent_unit("rac/cl_7000i")
    emit(account, "topics/sensor", data["sensor"])
    clock = Clock()
    version = data["shadow"]["version"]
    with (
        patch(MODULE + ".monotonic", clock),
        patch("custom_components.bosch_buderus_heating.bacon.entity.monotonic", clock),
    ):
        clock.now += 60
        emit(
            account,
            "topics/sensor",
            {"items": [{"timestamp": 9, "roomTemperature": 72.0}]},
        )
        assert entity.native_current_temperature is not None
        for _ in range(14):
            clock.now += 300
            version += 1
            reply = json.loads(json.dumps(data["shadow"]))
            reply["version"] = version
            emit(account, "shadows/state/get/accepted", reply)
        assert entity.native_current_temperature is None
        assert entity.available and entity.hvac_mode is not None


def test_offline_information_and_membership_loss_still_make_the_device_unavailable():
    account, entity, data = silent_unit("rac/cl_7000i")
    connectivity = BaconConnectivity(account, "serial-A")
    emit(account, "topics/info", {**data["info"], "timestamp": 99, "online": False})
    assert connectivity.available and connectivity.is_on is False
    assert not entity.available
    emit(account, "topics/info", {**data["info"], "timestamp": 100, "online": True})
    assert entity.available
    account._set_membership(("serial-B",))
    assert not entity.available and not connectivity.available


# -- unit level: the renewal grace window ---------------------------------------


def renewing_account():
    account, entity, _data = silent_unit("rac/cl_7000i")
    account._mqtt = None
    return account, entity


async def test_renewal_keeps_devices_for_the_window_without_notifying():
    account, entity = renewing_account()
    seen = []
    account.add_listener(lambda: seen.append(entity.available))
    account._disconnected(renewal=True)
    assert seen == [] and not account.connected
    assert account.available("serial-A") and entity.available
    assert account.carried("serial-A")
    account._end_grace()


async def test_renewal_window_ends_with_a_notification_when_nothing_arrives():
    account, entity = renewing_account()
    seen = []
    account.add_listener(lambda: seen.append(entity.available))
    with patch(MODULE + ".RENEWAL_GRACE", 0.05):
        account._disconnected(renewal=True)
        assert account.available("serial-A")
        await asyncio.sleep(0.15)
    assert not account.available("serial-A") and seen[-1] is False
    assert account.grace_expired == 1 and account._grace_timer is None
    assert not account.diagnostics()["renewal_grace"]["active"]


async def test_any_other_disconnect_is_immediate_and_ends_the_window():
    account, entity = renewing_account()
    seen = []
    account._disconnected(renewal=True)
    account.add_listener(lambda: seen.append(entity.available))
    account._disconnected()
    assert seen == [False] and not account.available("serial-A")
    assert account._grace_timer is None and account._grace_until is None


async def test_information_on_the_new_connection_ends_the_carry_over():
    account, _entity = renewing_account()
    account._disconnected(renewal=True)
    account.connected = True
    account.generation += 1
    assert account.available("serial-A") and account.carried("serial-A")
    emit(account, "topics/info", {"online": True, "timestamp": 1}, retained=True)
    assert account.available("serial-A") and not account.carried("serial-A")
    account._end_grace()
    assert account.available("serial-A")


async def test_a_membership_loss_inside_the_window_is_not_carried_over():
    account, _entity = renewing_account()
    account._disconnected(renewal=True)
    assert account.available("serial-A")
    account._set_membership(("serial-B",))
    assert not account.available("serial-A")
    account._end_grace()


async def test_writing_is_blocked_while_a_device_is_only_carried_over():
    account, _entity = renewing_account()
    account._disconnected(renewal=True)
    account.connected = True
    account.generation += 1
    account._mqtt = Mock(publish=AsyncMock())
    device = account.devices["serial-A"]

    async def reply():
        await asyncio.sleep(0)
        emit(
            account,
            "shadows/state/get/accepted",
            {
                "state": {"reported": {"powerEnabled": True}, "desired": {}},
                "version": 99,
                "timestamp": 99,
            },
        )

    validated = Mock(return_value=True)
    account._timeout = 0.5
    task = asyncio.create_task(reply())
    with pytest.raises(BaconError, match="device_unavailable"):
        await account.write_state("serial-A", {"powerEnabled": False}, validated)
    await task
    validated.assert_not_called()
    # Only the read, never the command.
    assert [c.args[1] for c in account._mqtt.publish.await_args_list] == [b""]
    assert device.online is True
    account._end_grace()


# -- diagnostics ---------------------------------------------------------------


def test_diagnostics_hold_age_classes_per_family_and_no_identifiers():
    account, _entity, _data = silent_unit("rac/cl_7000i")
    report = account.diagnostics()
    text = json.dumps(report)
    assert "serial-A" not in text and "serial-B" not in text
    row = report["availability"]["midea_climate"]
    assert row["devices"] == 1 and row["online_true"] == 1 and row["available"] == 1
    # Stored information only: no sign of life and no sensor message yet.
    assert row["last_sign_of_life"] == {"never": 1}
    assert row["last_sensor_message"] == {"never": 1}
    assert row["last_state_read_answer"] == {"under_5_min": 1}
    # The second device has no message and no model yet.
    other = report["availability"]["other"]
    assert (other["devices"], other["online_unknown"], other["available"]) == (1, 1, 0)
    assert report["renewal_grace"] == {"window_s": 90.0, "active": False, "expired": 0}
    live = report["live_report"]
    assert live["reports_sent"] == 0 and live["last_report_age"] == "never"
    clock = Clock()
    with patch(MODULE + ".monotonic", clock):
        clock.now += 4000
        aged = account.diagnostics()["availability"]["midea_climate"]
    assert aged["last_state_read_answer"] == {"under_6_h": 1}


def test_diagnostics_count_unknown_and_offline_devices_separately():
    account = bare_environment()
    emit(account, "topics/info", {"online": False, "timestamp": 2})
    row = account.diagnostics()["availability"]["other"]
    assert (row["online_true"], row["online_false"], row["online_unknown"]) == (0, 1, 1)
    assert row["devices"] == 2 and row["available"] == 1


# -- integration: real HTTP, MQTT 5 codec, token renewals ------------------------


class SilentBroker(Broker):
    """Stored information, metadata and measurements on every subscription,
    then nothing on its own: only answers to state reads."""

    def __init__(self, name="cl_7000i"):
        super().__init__(name)
        self.mute = False

    async def descriptions(self, ws, serial):
        if self.mute:
            return
        data = self.data[serial]
        info = {**data["info"], "timestamp": 10}
        for suffix, value in (
            ("topics/info", info),
            ("topics/meta", data["metadata"]),
            ("topics/sensor", data["sensor"]),
        ):
            name = topic("account-A", serial, suffix)
            await ws.send_bytes(
                packet(0x31, string(name) + b"\0" + json.dumps(value).encode())
            )

    def reads(self, serial):
        return sum(
            1
            for name in self.sent
            if name.endswith("/shadows/state/get") and f"/{serial}/" in name
        )


def rotate(access, signature):
    renewed = AuthTokens(
        token(signature=signature), "refresh-2", 10**12, scope=("bacon",)
    )
    return patch.object(
        access.client.tokens._oauth_client, "refresh", AsyncMock(return_value=renewed)
    )


async def test_silent_climates_stay_available_for_three_hours_with_three_renewals(hass):
    broker = SilentBroker()
    clock = Clock()
    patches = clock.patches()
    for item in patches:
        item.start()
    try:
        async with connected(hass, broker) as (_, _, owner, _):
            await eventually(lambda: ready(owner, A) and ready(owner, B))
            access, peer = view(owner, A), view(owner, B)
            account = access.account
            climates = [BaconClimate(access, A), BaconClimate(peer, B)]
            connectivities = [BaconConnectivity(access, A), BaconConnectivity(peer, B)]
            history = []

            def record():
                history.append(
                    (
                        *(c.available for c in climates),
                        *(c.available for c in connectivities),
                        account.available(A),
                        account.available(B),
                    )
                )

            access.add_listener(record)
            peer.add_listener(record)
            await eventually(lambda: len(broker.lives) == 1)
            assert account.diagnostics()["online_devices"] == 2
            renewals = 0
            for step in range(1, 37):
                clock.now += STEP
                reads, lives = broker.reads(A), len(broker.lives)
                if step % 12 == 0:
                    renewals += 1
                    with rotate(access, f"rotation{step}"):
                        await access.client.tokens.get_access_token(force_refresh=True)
                    await eventually(
                        lambda n=renewals: account.renewals == n, timeout=20
                    )
                await eventually(
                    lambda reads=reads, lives=lives: (
                        broker.reads(A) > reads
                        and broker.reads(B) > 0
                        and len(broker.lives) > lives
                        and ready(owner, A)
                        and ready(owner, B)
                    ),
                    timeout=20,
                )
            assert history, "no notification seen"
            # No unavailable state was ever written: nothing was shown as lost.
            assert all(all(row) for row in history), [r for r in history if not all(r)]
            assert account.renewals == 3 and account.reconnects == 0
            assert len(broker.connections) == 4
            # One report right after each subscription, then one per 300 seconds.
            assert len(broker.lives) == 4 + 33
            diagnostics = account.diagnostics()
            assert diagnostics["online_devices"] == 2
            assert diagnostics["air_conditioners"]["midea_climate"]["identified"] == 2
            assert (
                diagnostics["air_conditioners"]["midea_climate"]["metadata_current"]
                == 2
            )
            # The last answer may still be on its way back to the client.
            await eventually(lambda: account._live.sent == 37)
            assert diagnostics["renewal_grace"]["expired"] == 0
            assert diagnostics["availability"]["midea_climate"]["available"] == 2
            assert not broker.writes
    finally:
        for item in reversed(patches):
            item.stop()


async def test_a_failed_reconnect_after_the_renewal_ends_the_window_at_once(hass):
    broker = SilentBroker()
    async with connected(hass, broker) as (_, _, owner, _):
        await eventually(lambda: ready(owner, A))
        access = view(owner, A)
        account = access.account
        climate = BaconClimate(access, A)
        history = []
        access.add_listener(lambda: history.append(climate.available))
        broker.suback = 0x80
        with rotate(access, "failing"):
            await access.client.tokens.get_access_token(force_refresh=True)
        account.selection_changed.set()
        # Far inside the 90 seconds of the window.
        await eventually(lambda: history and history[-1] is False, timeout=10)
        assert not account.available(A) and account.error == "subscription_rejected"
        assert account.grace_expired == 0 and account._grace_timer is None


async def test_without_information_the_window_ends_by_itself_and_blocks_writes(hass):
    broker = SilentBroker()
    with patch(ROOT + ".bacon.session.RENEWAL_GRACE", 1.5):
        async with connected(hass, broker) as (_, _, owner, _):
            await eventually(lambda: ready(owner, A))
            access = view(owner, A)
            account = access.account
            climate = BaconClimate(access, A)
            history = []
            access.add_listener(lambda: history.append(climate.available))
            broker.mute = True
            generation = account.generation
            with rotate(access, "quiet"):
                await access.client.tokens.get_access_token(force_refresh=True)
            account.selection_changed.set()
            await eventually(
                lambda: account.generation > generation and account.connected,
                timeout=10,
            )
            # Connected again, no information yet: still shown, but no writing.
            assert account.available(A) and account.carried(A) and climate.available
            with pytest.raises(BaconError, match="device_unavailable"):
                await access.write_state(A, {"powerEnabled": False}, lambda d, x: True)
            assert broker.writes == []
            await eventually(lambda: history and history[-1] is False, timeout=10)
            assert not account.available(A)
            assert account.grace_expired == 1 and all(history[:-1])


async def test_a_rejected_report_stops_for_the_connection_and_keeps_it(hass):
    broker = SilentBroker()
    broker.live_status = 404
    clock = Clock()
    patches = clock.patches()
    for item in patches:
        item.start()
    try:
        async with connected(hass, broker) as (_, _, owner, _):
            await eventually(lambda: ready(owner, A) and len(broker.lives) == 1)
            access = view(owner, A)
            account = access.account
            await eventually(lambda: account._live.disabled)
            for _ in range(3):
                clock.now += STEP
                reads = broker.reads(A)
                await eventually(lambda r=reads: broker.reads(A) > r, timeout=20)
            assert len(broker.lives) == 1 and account.connected
            assert len(broker.connections) == 1 and ready(owner, A)
            live = account.diagnostics()["live_report"]
            assert live["disabled_for_connection"] and live["errors"]["4xx"] == 1
    finally:
        for item in reversed(patches):
            item.stop()


async def test_a_429_of_the_report_pauses_everything_without_ending_the_connection(
    hass,
):
    broker = SilentBroker()
    broker.live_status = 429
    async with connected(hass, broker) as (_, _cloud, owner, _):
        await eventually(lambda: ready(owner, A) and len(broker.lives) == 1)
        access = view(owner, A)
        account = access.account
        await eventually(lambda: account._live.errors["4xx"] == 1)
        assert account.connected and ready(owner, A)
        assert account.client.retry_at > 0
        assert len(broker.connections) == 1
