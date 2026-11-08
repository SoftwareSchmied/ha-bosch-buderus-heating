"""Ordering, connection recovery and confirmation without command replay."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiomqtt
import pytest

from custom_components.bosch_buderus_heating.bacon.client import BaconClient
from custom_components.bosch_buderus_heating.bacon.models import (
    BaconError,
    Device,
    topic,
)
from custom_components.bosch_buderus_heating.bacon.session import BaconAccount
from tests.test_bacon_contracts import provider, token

MODULE = "custom_components.bosch_buderus_heating.bacon.session"


def environment():
    client = BaconClient(None, provider())
    client.subject = "account-A"
    client.get_devices = AsyncMock(return_value=("serial-A", "serial-B"))
    account = BaconAccount(client, ("serial-A", "serial-B"), command_timeout=0.025)
    account._set_membership(("serial-A", "serial-B"))
    account.connected = True
    account.generation = 1
    account._mqtt = SimpleNamespace(publish=AsyncMock())
    for serial in account.selected:
        emit(account, "topics/info", {"online": True, "timestamp": 1}, serial)
    return account


def emit(account, suffix, payload, serial="serial-A", **kwargs):
    account.ingest(
        topic("account-A", serial, suffix),
        json.dumps(payload).encode(),
        "account-A",
        **kwargs,
    )


def state(version=1, value="off", desired=None, timestamp=1):
    return {
        "state": {"reported": {"mode": value}, "desired": desired or {}},
        "version": version,
        "timestamp": timestamp,
    }


def test_message_routing_order_and_freshness():
    account = environment()
    listener = Mock()
    unsubscribe = account.add_listener(listener)
    emit(account, "shadows/state/get/accepted", state(3))
    emit(account, "shadows/state/update/accepted", state(2, "on"))
    emit(account, "topics/info", {"online": False, "timestamp": 0})
    assert account.devices["serial-A"].state.version == 3
    assert account.available("serial-A")
    # Another account, removed device and unknown topics cannot mutate this state.
    account.ingest(topic("account-B", "serial-A", "topics/info"), b"{}", "account-A")
    emit(account, "topics/info", {}, "not-selected")
    emit(account, "firmware/get/rejected", {})
    assert "serial-A" not in account._rejected
    with pytest.raises(BaconError, match="conflicting_version"):
        emit(account, "shadows/state/get/accepted", state(3, "on"))
    emit(account, "shadows/state/get/accepted", state(3, timestamp=2))
    assert account.devices["serial-A"].state.timestamp == 2
    emit(account, "topics/meta", {"private": "metadata"})
    emit(account, "topics/sensor", {"items": [{"timestamp": 1, "private": "sensor"}]})
    assert "private" not in json.dumps(account.diagnostics())
    assert "serial-A" not in json.dumps(account.diagnostics())
    unsubscribe()
    listener.reset_mock()
    account._set_membership(("serial-B",))
    emit(account, "topics/info", {"online": True, "timestamp": 99})
    assert not account.available("serial-A")
    listener.assert_not_called()
    account._disconnected()
    assert not account.available("serial-B")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"online": 1, "timestamp": 1},
        {"online": True, "timestamp": -1},
        {"online": True, "timestamp": True},
    ],
)
def test_information_contract(payload):
    with pytest.raises(BaconError, match="invalid_information"):
        emit(environment(), "topics/info", payload)


def test_retained_information_does_not_prove_live_connection():
    account = environment()
    # More than an hour of silence before our reconnect.
    account.devices["serial-A"].alive_at -= 3601
    account._disconnected()
    account.connected = True
    account.generation = 2
    emit(account, "topics/info", {"online": True, "timestamp": 2}, retained=True)
    emit(account, "shadows/state/get/accepted", state(10), retained=True)
    emit(account, "shadows/state/update/rejected", {}, retained=True)
    assert not account.available("serial-A")
    assert account.devices["serial-A"].state is None
    assert not account._rejected
    emit(account, "topics/info", {"online": True, "timestamp": 3})
    assert account.available("serial-A")
    account.devices["serial-A"].alive_at -= 3601
    assert not account.available("serial-A")


@pytest.mark.parametrize("stamp", [1, 2], ids=["unchanged", "newer"])
def test_reconnect_within_the_hour_keeps_the_device(stamp):
    account = environment()
    device = account.devices["serial-A"]
    alive = device.alive_at
    account._disconnected()
    assert not account.available("serial-A")
    account.connected = True
    account.generation = 2
    # Not yet seen on the new connection.
    assert not account.available("serial-A")
    with patch(MODULE + ".monotonic", return_value=alive + 3000):
        emit(
            account, "topics/info", {"online": True, "timestamp": stamp}, retained=True
        )
        assert account.available("serial-A")
        assert device.alive_at == alive
    with patch(MODULE + ".monotonic", return_value=alive + 3601):
        assert not account.available("serial-A")


def test_reconnect_after_an_hour_of_silence_stays_unavailable():
    account = environment()
    alive = account.devices["serial-A"].alive_at
    account._disconnected()
    account.connected = True
    account.generation = 2
    with patch(MODULE + ".monotonic", return_value=alive + 3601):
        emit(account, "topics/info", {"online": True, "timestamp": 1}, retained=True)
        assert not account.available("serial-A")
        emit(account, "topics/sensor", {"items": [{"timestamp": 5, "t": 1}]})
        assert account.available("serial-A")


def test_device_without_sign_of_life_is_unavailable_shortly_after_boot():
    account = environment()
    account.devices["serial-A"] = Device("serial-A")
    with patch(MODULE + ".monotonic", return_value=100.0):
        emit(account, "topics/info", {"online": True, "timestamp": 5}, retained=True)
        assert account.devices["serial-A"].online is True
        assert account.devices["serial-A"].alive_at is None
        assert not account.available("serial-A")


def test_regained_membership_takes_online_state_from_unchanged_information():
    account = environment()
    device = account.devices["serial-A"]
    account._set_membership(("serial-B",))
    assert device.online is None
    account._set_membership(("serial-A", "serial-B"))
    before = device.alive_at
    with patch(MODULE + ".monotonic", return_value=before + 10):
        emit(account, "topics/info", {"online": True, "timestamp": 1}, retained=True)
    assert device.online is True and device.alive_at == before
    # Once set, an equal timestamp is a duplicate again.
    emit(account, "topics/info", {"online": False, "timestamp": 1}, retained=True)
    assert device.online is True


async def test_fake_broker_redelivery_restores_online_state():
    account = environment()
    device = account.devices["serial-A"]
    account._set_membership(("serial-B",))
    account._set_membership(("serial-A", "serial-B"))
    mqtt = Mqtt()
    for _ in range(2):
        mqtt.queue.put_nowait(
            SimpleNamespace(
                topic=topic("account-A", "serial-A", "topics/info"),
                payload=json.dumps({"online": True, "timestamp": 1}).encode(),
                retain=True,
            )
        )
    mqtt.queue.put_nowait(None)
    with pytest.raises(BaconError, match="connection"):
        await account._receive(mqtt, "account-A")
    assert device.online is True and account.invalid_messages == 0
    assert account.available("serial-A")


def reconnected():
    """A fresh connection after more than an hour of device silence."""
    account = environment()
    account.devices["serial-A"].alive_at -= 3601
    account._disconnected()
    account.connected = True
    account.generation = 2
    emit(account, "topics/info", {"online": True, "timestamp": 2}, retained=True)
    assert not account.available("serial-A")
    return account, account.devices["serial-A"]


def test_sensor_only_device_stays_available():
    account, device = reconnected()
    start = device.alive_at
    for step, stamp in enumerate((3, 4, 5), start=1):
        with patch(MODULE + ".monotonic", return_value=start + step * 1200):
            emit(account, "topics/sensor", {"items": [{"timestamp": stamp, "t": 1}]})
            assert account.available("serial-A")
            assert device.online is True
    # Just inside and just outside: the start time is an arbitrary float, so an
    # exact boundary would depend on floating-point rounding.
    with patch(MODULE + ".monotonic", return_value=start + 3 * 1200 + 3599.5):
        assert account.available("serial-A")
    with patch(MODULE + ".monotonic", return_value=start + 3 * 1200 + 3601):
        assert not account.available("serial-A")


def test_information_only_on_change_stays_available_up_to_limit():
    account = environment()
    device = account.devices["serial-A"]
    start = device.alive_at
    for offset, expected in ((900, True), (901, True), (3599.5, True), (3601, False)):
        with patch(MODULE + ".monotonic", return_value=start + offset):
            assert account.available("serial-A") is expected


def test_device_reported_state_update_is_sign_of_life():
    account, device = reconnected()
    with patch(MODULE + ".monotonic", return_value=device.alive_at + 1000):
        emit(account, "shadows/state/update/accepted", state(5, "on"))
        assert account.available("serial-A")
        assert device.alive_at == pytest.approx(device.state_received_at)


@pytest.mark.parametrize(
    "suffix,payload",
    [
        ("shadows/state/get/accepted", state(5, "on")),
        (
            "shadows/state/update/accepted",
            {"state": {"desired": {"mode": "on"}}, "version": 5, "timestamp": 1},
        ),
        ("topics/meta", {"ecoEnabled": {"ro": False}}),
    ],
    ids=["own-get-response", "desired-only-ack", "metadata"],
)
def test_cloud_responses_are_not_signs_of_life(suffix, payload):
    account, device = reconnected()
    before = device.alive_at
    with patch(MODULE + ".monotonic", return_value=before + 10):
        emit(account, suffix, payload)
        assert device.alive_at == before
    assert not account.available("serial-A")


@pytest.mark.parametrize(
    "suffix,payload",
    [
        ("topics/info", {"online": True, "timestamp": 9}),
        ("topics/sensor", {"items": [{"timestamp": 9, "t": 1}]}),
        ("shadows/state/update/accepted", state(9, "on")),
    ],
)
def test_retained_messages_never_count_as_sign_of_life(suffix, payload):
    account, device = reconnected()
    before = device.alive_at
    with patch(MODULE + ".monotonic", return_value=before + 10):
        emit(account, suffix, payload, retained=True)
        assert device.alive_at == before
    assert not account.available("serial-A")


def test_replayed_sensor_sample_is_not_a_sign_of_life():
    account, device = reconnected()
    emit(account, "topics/sensor", {"items": [{"timestamp": 3, "t": 1}]})
    first = device.alive_at
    with patch(MODULE + ".monotonic", return_value=first + 3000):
        emit(account, "topics/sensor", {"items": [{"timestamp": 3, "t": 1}]})
    assert device.alive_at == first


def test_explicit_offline_information_takes_effect_immediately():
    account, device = reconnected()
    emit(account, "topics/sensor", {"items": [{"timestamp": 3, "t": 1}]})
    assert account.available("serial-A") and device.online is True
    emit(account, "topics/info", {"online": False, "timestamp": 3})
    assert device.online is False
    assert account.available("serial-A")  # connectivity reports "off"
    # Later measurements cannot override the newest information message.
    emit(account, "topics/sensor", {"items": [{"timestamp": 4, "t": 1}]})
    assert device.online is False
    # An older information message cannot bring the device back online.
    emit(account, "topics/info", {"online": True, "timestamp": 2})
    assert device.online is False
    emit(account, "topics/info", {"online": True, "timestamp": 4})
    assert device.online is True


def test_sensor_fields_expire_individually_after_an_hour():
    account = environment()
    device = account.devices["serial-A"]
    emit(account, "shadows/state/get/accepted", state())
    emit(account, "topics/sensor", {"items": [{"timestamp": 1, "a": 1, "b": 2}]})
    start = device.sensor_fields_received_at["a"]
    with patch(MODULE + ".monotonic", return_value=start + 1800):
        emit(account, "topics/sensor", {"items": [{"timestamp": 2, "b": 3}]})
    with patch(MODULE + ".monotonic", return_value=start + 3599.5):
        account.expire_values(device)
        assert device.sensor["a"] == 1
    with patch(MODULE + ".monotonic", return_value=start + 3601):
        account.expire_values(device)
        assert "a" not in device.sensor and device.sensor["b"] == 3
        assert "mode" not in device.state.reported


def test_state_fields_still_expire_after_fifteen_minutes():
    account = environment()
    device = account.devices["serial-A"]
    emit(account, "shadows/state/get/accepted", state())
    start = device.state_fields_received_at["mode"]
    with patch(MODULE + ".monotonic", return_value=start + 899.5):
        account.expire_values(device)
        assert device.state.reported["mode"] == "off"
    with patch(MODULE + ".monotonic", return_value=start + 901):
        account.expire_values(device)
        assert "mode" not in device.state.reported


@pytest.mark.parametrize(
    "outcome",
    [
        "reported",
        "lost_puback",
        "desired_only",
        "rejected",
        "disconnected",
        "offline",
        "removed",
        "new_connection",
        "stale_version",
        "retained",
        "cancel",
    ],
)
async def test_command_needs_reported_confirmation(outcome):
    account = environment()
    published = []

    async def publish(name, payload, **kwargs):
        published.append((name, payload, kwargs))
        if name.endswith("/get"):
            emit(account, "shadows/state/get/accepted", state())
            return
        assert json.loads(payload) == {"state": {"desired": {"mode": "on"}}}
        assert kwargs == {"qos": 1, "retain": False}
        if outcome in ("reported", "lost_puback"):
            emit(account, "shadows/state/update/accepted", state(2, "on", timestamp=2))
            if outcome == "lost_puback":
                raise aiomqtt.MqttError("PUBACK lost")
        elif outcome == "desired_only":
            emit(
                account,
                "shadows/state/update/accepted",
                state(2, desired={"mode": "on"}),
            )
        elif outcome == "rejected":
            emit(account, "shadows/state/update/rejected", {"code": 400})
        elif outcome == "disconnected":
            account._disconnected()
        elif outcome == "offline":
            emit(account, "topics/info", {"online": False, "timestamp": 2})
        elif outcome == "removed":
            account._set_membership(())
        elif outcome == "new_connection":
            account.generation += 1
            emit(account, "shadows/state/update/accepted", state(2, "on", timestamp=2))
        elif outcome == "stale_version":
            emit(account, "shadows/state/update/accepted", state())
        elif outcome == "retained":
            emit(
                account, "shadows/state/update/accepted", state(2, "on"), retained=True
            )
        else:
            raise asyncio.CancelledError

    account._mqtt.publish.side_effect = publish
    if outcome in ("reported", "lost_puback"):
        assert (
            await account.write_state("serial-A", {"mode": "on"}, lambda d, v: True)
        ).version == 2
    elif outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await account.write_state("serial-A", {"mode": "on"}, lambda d, v: True)
        assert account.devices["serial-A"].uncertain
    else:
        with pytest.raises(BaconError, match="command_unconfirmed"):
            await account.write_state("serial-A", {"mode": "on"}, lambda d, v: True)
        assert account.devices["serial-A"].uncertain
    assert sum(n.endswith("/update") for n, _, _ in published) == 1


@pytest.mark.parametrize(
    "case,category",
    [
        ("unsupported", "unsupported_command"),
        ("pending", "busy"),
        ("offline", "device_unavailable"),
        ("empty", "device_unavailable"),
        ("invalid", "invalid_payload"),
        ("unknown", "invalid_identifier"),
    ],
)
async def test_command_preconditions_never_publish_update(case, category):
    account = environment()

    async def publish(*args, **kwargs):
        emit(
            account,
            "shadows/state/get/accepted",
            state(desired={"mode": "on"} if case == "pending" else {}),
        )

    account._mqtt.publish.side_effect = publish
    if case == "offline":
        emit(account, "topics/info", {"online": False, "timestamp": 2})
    desired = (
        {}
        if case == "empty"
        else {"mode": float("nan")}
        if case == "invalid"
        else {"mode": "on"}
    )
    with pytest.raises(BaconError, match=category):
        await account.write_state(
            "other" if case == "unknown" else "serial-A",
            desired,
            lambda d, v: case != "unsupported",
        )
    assert all(
        not c.args[0].endswith("/update") for c in account._mqtt.publish.await_args_list
    )


async def test_noop_and_parallel_reads_are_safe():
    account = environment()

    async def publish(*args, **kwargs):
        with pytest.raises(BaconError, match="busy"):
            await account.read_state("serial-A")
        emit(account, "shadows/state/get/accepted", state())

    account._mqtt.publish.side_effect = publish
    assert (
        await account.write_state("serial-A", {"mode": "off"}, lambda d, v: True)
    ).version == 1
    assert account._mqtt.publish.await_count == 1
    account._disconnected()
    with pytest.raises(BaconError, match="connection"):
        await account.read_state("serial-A")


@pytest.mark.parametrize("case", ["timeout", "error", "rejected", "disconnected"])
async def test_read_failure_cleans_waiter(case):
    account = environment()

    async def publish(*args, **kwargs):
        if case == "error":
            raise aiomqtt.MqttError("private")
        if case == "rejected":
            emit(account, "shadows/state/get/rejected", {"code": 400})
        if case == "disconnected":
            account._disconnected()

    account._mqtt.publish.side_effect = publish
    with pytest.raises(BaconError):
        await account.read_state("serial-A")
    assert not account._readers


async def test_writes_are_serialized_per_device_and_freeze_input():
    account = environment()
    versions = {"serial-A": 1, "serial-B": 1}
    values = {"serial-A": "off", "serial-B": "off"}
    active = set()

    async def publish(name, payload, **kwargs):
        serial = name.split("/")[3]
        if name.endswith("/get"):
            assert serial not in active
            active.add(serial)
            await asyncio.sleep(0)
            emit(
                account,
                "shadows/state/get/accepted",
                state(versions[serial], values[serial]),
                serial,
            )
        else:
            versions[serial] += 1
            values[serial] = json.loads(payload)["state"]["desired"]["mode"]
            active.remove(serial)
            emit(
                account,
                "shadows/state/update/accepted",
                state(versions[serial], values[serial]),
                serial,
            )

    account._mqtt.publish.side_effect = publish
    await asyncio.gather(
        account.write_state("serial-A", {"mode": "on"}, lambda d, v: True),
        account.write_state("serial-A", {"mode": "off"}, lambda d, v: True),
        account.write_state("serial-B", {"mode": "on"}, lambda d, v: True),
    )
    assert values == {"serial-A": "off", "serial-B": "on"}


@pytest.mark.parametrize(
    "error,category,retry",
    [
        (BaconError("authentication"), "authentication", False),
        (BaconError("account_changed"), "account_changed", False),
        (BaconError("rate_limited", 120), "rate_limited", True),
        (aiomqtt.MqttCodeError(135), "authentication", False),
        (aiomqtt.MqttCodeError(151), "rate_limited", True),
        (aiomqtt.MqttError("secret"), "connection", True),
    ],
)
async def test_lifecycle_classifies_errors_and_backs_off(error, category, retry):
    account = environment()
    account.run_connection = AsyncMock(side_effect=error)
    with patch(MODULE + ".asyncio.sleep", side_effect=asyncio.CancelledError) as sleep:
        if retry:
            with pytest.raises(asyncio.CancelledError):
                await account.run()
            assert sleep.call_args.args[0] >= 5
        else:
            await account.run()
            sleep.assert_not_called()
    assert account.error == category
    assert not account.connected
    assert category in json.dumps(account.diagnostics())


class Mqtt:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.publish = AsyncMock()
        self.subscribe = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    @property
    def messages(self):
        return self.stream()

    async def stream(self):
        while True:
            value = await self.queue.get()
            if value is None:
                return
            yield value


async def test_connection_uses_exact_transport_and_renews_token():
    account = environment()
    mqtt = Mqtt()
    factory = Mock(return_value=mqtt)
    account._factory = factory
    account.client.tokens.get_access_token.side_effect = [
        token(),
        token(signature="new"),
    ]
    with patch(MODULE + ".MAINTENANCE_INTERVAL", 0.001):
        await account.run_connection()
    kwargs = factory.call_args.kwargs
    assert kwargs["protocol"] == aiomqtt.ProtocolVersion.V5
    assert kwargs["username"] == "account-A"
    assert kwargs["password"] == token()
    assert kwargs["websocket_headers"]["Authorization"] == "Bearer " + token()
    assert kwargs["websocket_path"] == "/mqtt"
    assert kwargs["clean_start"] is True
    mqtt.subscribe.assert_awaited_once_with("users/account-A/#", qos=1)
    assert mqtt.publish.await_count == 2
    assert all(c.args[1] == b"" for c in mqtt.publish.await_args_list)


async def test_removed_devices_and_malformed_messages_during_stream():
    account = environment()
    mqtt = Mqtt()
    account._factory = Mock(return_value=mqtt)
    removed = asyncio.Event()

    async def listed():
        if account.client.get_devices.await_count == 1:
            return ("serial-A",)
        removed.set()
        return ()

    account.client.get_devices.side_effect = listed
    mqtt.queue.put_nowait(
        SimpleNamespace(
            topic=topic("account-A", "serial-A", "topics/info"),
            payload=b"bad",
            retain=False,
        )
    )
    with (
        patch(MODULE + ".MAINTENANCE_INTERVAL", 0.001),
        patch(MODULE + ".RESYNC_INTERVAL", 0.001),
    ):
        task = asyncio.create_task(account.run_connection())
        await removed.wait()
        mqtt.queue.put_nowait(None)
        with pytest.raises(BaconError, match="connection"):
            await task
    assert account.invalid_messages == 1
    assert not account.available("serial-A")


async def test_stream_rate_limit_and_empty_account():
    account = environment()
    account.client.get_devices.return_value = ()
    with pytest.raises(BaconError, match="devices_unavailable"):
        await account.run_connection()
    account._set_membership(("serial-A",))
    mqtt = Mqtt()
    mqtt.queue.put_nowait(
        SimpleNamespace(
            topic=topic("account-A", "serial-A", "shadows/state/get/rejected"),
            payload=b'{"code":429}',
            retain=False,
        )
    )
    with pytest.raises(BaconError, match="rate_limited"):
        await account._receive(mqtt, "account-A")


async def test_close_cancels_background_task():
    account = environment()
    task = asyncio.create_task(asyncio.sleep(60))
    account.attach_task(task)
    await account.close()
    assert task.cancelled()
    assert not account.connected
