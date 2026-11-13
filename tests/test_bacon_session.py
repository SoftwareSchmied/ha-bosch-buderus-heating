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
            # The first read is the baseline; a later one sees a newer shadow.
            reads = sum(n.endswith("/get") for n, _, _ in published)
            later = reads > 1
            emit(
                account,
                "shadows/state/get/accepted",
                state(10 if later else 1, timestamp=9 if later else 1),
            )
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
        # C50: a plain read follows at once while the connection still holds
        # the device; its full response settles the uncertainty.
        clarified = outcome not in ("disconnected", "removed")
        assert account.devices["serial-A"].uncertain is not clarified
        assert sum(n.endswith("/get") for n, _, _ in published) == 1 + clarified
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


# ------------------------------------------------- 0.9 review fixes (C45-C59)


class Refusing(Mqtt):
    """A broker refusing the sign-in with an MQTT 5 CONNACK reason."""

    def __init__(self, code):
        super().__init__()
        self.code = code

    async def __aenter__(self):
        raise aiomqtt.MqttCodeError(self.code, "refused")


def broker_sequence(*clients):
    """Hand out the given clients; each working session ends at once."""
    for client in clients:
        if not isinstance(client, Refusing):
            client.queue.put_nowait(None)
    return Mock(side_effect=list(clients))


@pytest.mark.parametrize(
    ("codes", "outcome"),
    [
        ((135,), "connected"),
        ((134,), "connected"),
        ((135, 135), "authentication"),
        ((151,), "rate_limited"),
    ],
)
async def test_refused_mqtt_sign_in_retries_once_with_a_fresh_token(codes, outcome):
    """C45: as for HTTP 401, a refused sign-in first gets one fresh token."""
    account = environment()
    working = Mqtt()
    account._factory = broker_sequence(*(Refusing(c) for c in codes), working)
    tokens = account.client.tokens.get_access_token
    tokens.side_effect = [token(), token(signature="fresh")]
    if outcome == "connected":
        # The fresh token connects; the closed stream then ends the session.
        with pytest.raises(BaconError, match="connection"):
            await account.run_connection()
        assert account._factory.call_count == 2
        fresh = token(signature="fresh")
        assert account._factory.call_args.kwargs["password"] == fresh
        working.subscribe.assert_awaited_once()
    else:
        with pytest.raises(aiomqtt.MqttCodeError) as caught:
            await account.run_connection()
        assert caught.value.rc == codes[-1]
        assert account._factory.call_count == len(codes)
    refreshes = [c for c in tokens.await_args_list if c.kwargs.get("force_refresh")]
    retried = outcome != "rate_limited"
    assert len(refreshes) == int(retried)
    if retried:
        assert refreshes[0].kwargs["rejected_token"] == token()


async def test_refused_mqtt_sign_in_twice_still_requests_sign_in():
    """C45: only the second refusal ends the session as a sign-in failure."""
    account = environment()
    account._factory = broker_sequence(Refusing(135), Refusing(135))
    account.client.tokens.get_access_token.side_effect = [
        token(),
        token(signature="fresh"),
    ]
    errors = []
    account.on_error = errors.append
    with patch(MODULE + ".asyncio.sleep") as sleep:
        await account.run()
    sleep.assert_not_called()
    assert account.error == "authentication"
    assert [e.category for e in errors] == ["authentication"]


@pytest.mark.parametrize(
    ("codes", "rejected"),
    [([SimpleNamespace(value=0x87)], True), ((0x80,), True), ([1], False)],
)
async def test_rejected_subscription_is_a_connection_failure(codes, rejected):
    """C46: SUBACK reason codes of 0x80 or higher never count as connected."""
    account = environment()
    mqtt = Mqtt()
    mqtt.subscribe.return_value = codes
    mqtt.queue.put_nowait(None)
    account._factory = Mock(return_value=mqtt)
    account.connected = False
    seen = []
    account.add_listener(lambda: seen.append(account.connected))
    with pytest.raises(
        BaconError, match="subscription_rejected" if rejected else "connection"
    ):
        await account.run_connection()
    assert any(seen) is not rejected
    account.run_connection = AsyncMock(side_effect=BaconError("subscription_rejected"))
    with (
        patch(MODULE + ".asyncio.sleep", side_effect=asyncio.CancelledError) as sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        await account.run()
    assert account.error == "subscription_rejected"
    assert sleep.call_args.args[0] == 60


@pytest.mark.parametrize(
    ("desired", "reported", "busy"),
    [
        ({"temp": 22.0}, {"temp": 22}, False),
        ({"temp": 22}, {"temp": 22.0}, False),
        ({"nested": {"temp": 21.5}}, {"nested": {"temp": 21.5}}, False),
        ({"temp": 22.5}, {"temp": 22}, True),
        ({"power": True}, {"power": 1}, True),
        ({"power": 0}, {"power": False}, True),
        ({"mode": "22"}, {"mode": 22}, True),
    ],
)
async def test_pending_numbers_compare_by_value(desired, reported, busy):
    """C47: 22.0 and 22 are equal; booleans and texts never equal numbers."""
    account = environment()
    writes = []

    async def publish(name, payload, **kwargs):
        if name.endswith("/get"):
            shadow = {
                "state": {"reported": {"level": 1, **reported}, "desired": desired},
                "version": 1,
                "timestamp": 1,
            }
            emit(account, "shadows/state/get/accepted", shadow)
            return
        writes.append(json.loads(payload))
        emit(account, "shadows/state/update/accepted", state(2, "on", timestamp=2))

    account._mqtt.publish.side_effect = publish
    if busy:
        with pytest.raises(BaconError, match="busy"):
            await account.write_state("serial-A", {"mode": "on"}, lambda d, v: True)
        assert writes == []
    else:
        await account.write_state("serial-A", {"mode": "on"}, lambda d, v: True)
        assert writes == [{"state": {"desired": {"mode": "on"}}}]


async def test_failed_clarifying_read_keeps_the_uncertainty():
    """C50: the follow-up read is bounded and never turns into a command."""
    account = environment()
    reads, updates = [], []

    async def publish(name, payload, **kwargs):
        if name.endswith("/update"):
            updates.append(name)
            return
        reads.append(name)
        if len(reads) == 1:
            emit(account, "shadows/state/get/accepted", state())
            return
        # The follow-up read hangs beyond the command timeout.
        await asyncio.sleep(1)

    account._mqtt.publish.side_effect = publish
    with pytest.raises(BaconError, match="command_unconfirmed"):
        await account.write_state("serial-A", {"mode": "on"}, lambda d, v: True)
    assert account.devices["serial-A"].uncertain
    assert len(reads) == 2 and len(updates) == 1
    # A later full read settles it, as before.
    emit(account, "shadows/state/get/accepted", state(3, "on"))
    assert not account.devices["serial-A"].uncertain


async def run_connection_until(account, mqtt, condition):
    """Run one connection with short intervals until the condition holds."""
    account._factory = Mock(return_value=mqtt)
    with (
        patch(MODULE + ".MAINTENANCE_INTERVAL", 0.001),
        patch(MODULE + ".RESYNC_INTERVAL", 0.001),
    ):
        task = asyncio.create_task(account.run_connection())
        try:
            for _ in range(2000):
                if condition() or task.done():
                    break
                await asyncio.sleep(0.001)
            assert condition()
            assert not task.done()
        finally:
            mqtt.queue.put_nowait(None)
            with pytest.raises(BaconError, match="connection"):
                await task


@pytest.mark.parametrize(
    "category", ["service_unavailable", "connection", "invalid_device_list"]
)
async def test_failed_periodic_device_list_keeps_the_connection(category):
    """C48: a single failure of the claim service keeps MQTT and membership."""
    account = environment()
    mqtt = Mqtt()
    calls, during = [], []

    async def listed():
        calls.append(1)
        if len(calls) == 2:
            raise BaconError(category)
        if len(calls) == 1:
            return ("serial-A", "serial-B")
        if len(calls) == 3:
            during.append((account.connected, account.error, set(account._claimed)))
        return ("serial-A",)

    account.client.get_devices.side_effect = listed
    errors = []
    account.on_error = errors.append
    await run_connection_until(account, mqtt, lambda: len(calls) >= 3)
    assert during == [(True, category, {"serial-A", "serial-B"})]
    assert [e.category for e in errors] == [category]
    # The next successful list applies the membership and clears the error.
    assert account._claimed == {"serial-A"} and account.error is None
    gets = [c.args[0] for c in mqtt.publish.await_args_list]
    # Read after connecting and after the failed list, not after removal.
    assert gets.count(topic("account-A", "serial-B", "shadows/state/get")) == 2


@pytest.mark.parametrize("category", ["paused", "rate_limited"])
async def test_paused_periodic_device_list_skips_the_state_reads(category):
    """C48: during a shared pause, the state reads of that round wait too."""
    account = environment()
    mqtt = Mqtt()
    calls = []

    async def listed():
        calls.append(1)
        if len(calls) == 1:
            return ("serial-A",)
        raise BaconError(category, 120)

    account.client.get_devices.side_effect = listed
    await run_connection_until(account, mqtt, lambda: len(calls) >= 3)
    # Only the first round after connecting read the state.
    assert mqtt.publish.await_count == 1
    assert account.error == category and account._claimed == {"serial-A"}


@pytest.mark.parametrize(
    "category", ["authentication", "account_changed", "source_stopped"]
)
async def test_fatal_periodic_device_list_failure_still_ends_the_connection(
    category,
):
    account = environment()
    mqtt = Mqtt()
    calls = []

    async def listed():
        calls.append(1)
        if len(calls) == 1:
            return ("serial-A",)
        raise BaconError(category)

    account.client.get_devices.side_effect = listed
    account._factory = Mock(return_value=mqtt)
    with (
        patch(MODULE + ".MAINTENANCE_INTERVAL", 0.001),
        patch(MODULE + ".RESYNC_INTERVAL", 0.001),
        pytest.raises(BaconError, match=category),
    ):
        await account.run_connection()


async def test_local_pause_does_not_extend_the_shared_pause():
    """C49: a locally blocked attempt waits for the pause, never extends it."""
    import aiohttp

    from custom_components.bosch_buderus_heating.managed_bacon import (
        ManagedBaconAccount,
    )
    from custom_components.bosch_buderus_heating.pointt.transport import (
        RateLimitBackoff,
    )

    now = [1000.0]
    backoff = RateLimitBackoff(clock=lambda: now[0])
    backoff.activate(60)
    now[0] += 30

    def refuse(*args, **kwargs):
        raise aiohttp.ClientError()

    client = BaconClient(
        SimpleNamespace(get=refuse), provider(), backoff=backoff, clock=lambda: now[0]
    )
    pool = SimpleNamespace(
        updated=lambda: None,
        connection=SimpleNamespace(backoff=backoff),
        guard=lambda: None,
        auth_failed=asyncio.Event(),
        sources={},
    )
    account = ManagedBaconAccount(pool, client)
    sleeps = []

    async def sleep(delay):
        sleeps.append((now[0], backoff.remaining, delay, account.error))
        now[0] += delay
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    with (
        patch(MODULE + ".asyncio.sleep", side_effect=sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await account.run()
    # The pause still ends at t=1060, and the first wait lasts until then.
    assert sleeps[0] == (1030.0, 30.0, 30.0, "paused")
    assert sleeps[1][:2] == (1060.0, 0.0) and sleeps[1][3] == "connection"


async def test_planned_renewal_reconnects_at_once_without_a_device_list():
    """C52: a token renewal reconnects without a pause and without a claim."""
    account = environment()
    first, second = Mqtt(), Mqtt()
    account._factory = Mock(side_effect=[first, second])
    account.client.tokens.get_access_token.side_effect = [
        token(),
        token(signature="2"),
        token(signature="2"),
        token(signature="3"),
    ]
    with (
        patch(MODULE + ".MAINTENANCE_INTERVAL", 0.001),
        patch(MODULE + ".asyncio.sleep", side_effect=asyncio.CancelledError) as sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        await account.run()
    assert account._factory.call_count == 2
    assert account._factory.call_args.kwargs["password"] == token(signature="2")
    assert account.client.get_devices.await_count == 1
    assert account.renewals == 1
    # A second renewal within five minutes takes the normal backoff.
    assert account.reconnects == 1
    sleep.assert_called_once_with(5)
    diagnostics = account.diagnostics()
    assert diagnostics["renewals"] == 1 and diagnostics["reconnects"] == 1


async def test_renewal_with_an_old_device_list_asks_the_claim_service():
    account = environment()
    account._renewing = True
    account._synced_at = None
    account.client.get_devices.return_value = ()
    with pytest.raises(BaconError, match="devices_unavailable"):
        await account.run_connection()
    account.client.get_devices.assert_awaited_once()


def test_offline_device_shows_disconnected_after_restart_and_hour():
    """C51: an offline state needs no sign of life to be shown."""
    from custom_components.bosch_buderus_heating.bacon.entity import (
        BaconConnectivity,
    )

    account = environment()
    account.devices["serial-A"] = Device("serial-A")
    entity = BaconConnectivity(account, "serial-A")
    # Retained information after a restart, without any sign of life.
    emit(account, "topics/info", {"online": False, "timestamp": 7}, retained=True)
    assert entity.available and entity.is_on is False
    emit(account, "topics/info", {"online": False, "timestamp": 8})
    later = account.devices["serial-A"].alive_at + 3601
    with patch(MODULE + ".monotonic", return_value=later):
        assert entity.available and entity.is_on is False
        # Online still needs a recent sign of life.
        emit(account, "topics/info", {"online": True, "timestamp": 9}, retained=True)
        assert not entity.available
    # A new connection needs the information again.
    account._disconnected()
    account.connected, account.generation = True, 2
    assert not entity.available


def test_information_without_a_type_keeps_model_and_descriptions():
    """C53: a missing or empty appliance type is no model change."""
    account = environment()
    device = account.devices["serial-A"]
    info = {"online": True, "timestamp": 2, "appliance": {"type": "model-1"}}
    emit(account, "topics/info", info)
    emit(account, "topics/meta", {"field": {"ro": False}})
    emit(account, "shadows/state/get/accepted", state())
    for stamp, partial in enumerate(
        ({}, {"appliance": None}, {"appliance": {"type": ""}}), start=3
    ):
        emit(account, "topics/info", {"online": True, "timestamp": stamp, **partial})
        assert device.model == "model-1"
        assert device.metadata and device.state is not None
    assert not account._refresh
    emit(
        account,
        "topics/info",
        {"online": True, "timestamp": 9, "appliance": {"type": "model-2"}},
    )
    assert device.model == "model-2"
    assert not device.metadata and device.state is None
    assert account._refresh == {"serial-A"}


async def test_model_change_requests_descriptions_without_reconnecting():
    """C53: metadata and state of a replaced model arrive on the same session."""
    from unittest.mock import call

    account = environment()
    mqtt = Mqtt()
    emit(
        account,
        "topics/info",
        {"online": True, "timestamp": 2, "appliance": {"type": "model-1"}},
    )
    mqtt.queue.put_nowait(
        SimpleNamespace(
            topic=topic("account-A", "serial-A", "topics/info"),
            payload=json.dumps(
                {"online": True, "timestamp": 3, "appliance": {"type": "model-2"}}
            ).encode(),
            retain=False,
        )
    )
    meta = call(topic("account-A", "serial-A", "topics/meta"), qos=1)
    await run_connection_until(
        account, mqtt, lambda: meta in mqtt.subscribe.await_args_list
    )
    assert account._factory.call_count == 1
    gets = [c.args[0] for c in mqtt.publish.await_args_list]
    assert gets.count(topic("account-A", "serial-A", "shadows/state/get")) >= 2
    assert not account._refresh


async def test_unexpected_failure_reconnects_with_backoff_and_logs_once(caplog):
    """C59: an unexpected error neither ends the session nor floods the log."""
    account = environment()
    account.run_connection = AsyncMock(
        side_effect=[RuntimeError("private detail"), RuntimeError("private detail")]
    )
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    with (
        patch(MODULE + ".asyncio.sleep", side_effect=sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await account.run()
    assert delays == [5, 10]
    assert account.error == "internal" and account.reconnects == 2
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()
    assert "private detail" not in warnings[0].getMessage()


async def test_failing_listener_does_not_end_the_session():
    """C59: a listener failure on the receive path retries the connection."""
    account = environment()
    account._factory = Mock(return_value=Mqtt())
    failing = [True]

    def listener():
        if failing[0]:
            raise ValueError("entity")

    account.add_listener(listener)
    delays = []

    async def sleep(delay):
        delays.append(delay)
        failing[0] = False
        raise asyncio.CancelledError

    with (
        patch(MODULE + ".asyncio.sleep", side_effect=sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await account.run()
    assert delays == [5]
    assert account.error == "internal" and not account.connected


async def test_close_tolerates_a_task_that_already_failed():
    """C59: unloading never re-raises an earlier failure of the session task."""
    account = environment()

    async def failed():
        raise RuntimeError("earlier")

    task = asyncio.create_task(failed())
    await asyncio.gather(task, return_exceptions=True)
    account.attach_task(task)
    await account.close()
    assert not account.connected
