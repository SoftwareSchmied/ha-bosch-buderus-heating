"""Managed cloud devices over actual HTTP and the MQTT 5 WebSocket codec."""

import asyncio
import json
import ssl
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import aiomqtt
import pytest
from aiohttp import web
from homeassistant.config_entries import SOURCE_REAUTH

from custom_components.bosch_buderus_heating.bacon.client import BaconClient
from custom_components.bosch_buderus_heating.bacon.entity import BaconConnectivity
from custom_components.bosch_buderus_heating.bacon.models import (
    BaconError,
    account_key,
    topic,
)
from custom_components.bosch_buderus_heating.climate import BaconClimate
from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.configuration_migration import (
    convert_legacy_configuration,
)
from custom_components.bosch_buderus_heating.connection_runtime import ConnectionPhase
from custom_components.bosch_buderus_heating.fan import BaconPurifierFan
from custom_components.bosch_buderus_heating.humidifier import BaconDehumidifier
from custom_components.bosch_buderus_heating.managed_bacon import ManagedBaconAccount
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    PointTClient,
    RefreshTokenRejected,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import RateLimited
from custom_components.bosch_buderus_heating.water_heater import BaconWaterHeater
from tests.test_bacon_contracts import token
from tests.test_bacon_wire import packet, publish, string
from tests.test_configuration import OWNER, add_local, legacy
from tests.test_connection_runtime import settle
from tests.test_managed_pointt import Backend, eventually, running, source_for

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
ROOT = "custom_components.bosch_buderus_heating"
A, B, HEAT = "serial-A", "serial-B", "000123"


def family(name):
    group = (
        "rac"
        if name == "cl_7000i"
        else "purifier"
        if name == "air_6001i"
        else "dry"
        if name == "dry_8000i"
        else "dhw"
    )
    return json.loads(
        (
            Path(__file__).parent / f"fixtures/contracts/bacon-{group}/{name}.json"
        ).read_text()
    )


class Broker(Backend):
    def __init__(self, name="midea"):
        super().__init__()
        self.offered = [A, B]
        self.claim_status = 200
        self.claims = []
        self.connections = []
        self.sockets = set()
        self.filters = []
        self.sent = []
        self.writes = []
        self.data = {serial: family(name) for serial in (A, B)}
        self.stamp = 10
        self.retained_info = False
        self.reply_gates = {}
        self.claim_gate = None
        self.claim_entered = asyncio.Event()
        self.connack = 0
        # Reasons for the next connections before falling back to connack.
        self.connacks = []
        self.suback = 1

    @property
    def routes(self):
        return [
            *super().routes,
            ("GET", "/devices", self.claim),
            ("GET", "/mqtt", self.broker),
        ]

    async def claim(self, request):
        self.claims.append(request.headers["Authorization"])
        status, offered = self.claim_status, list(self.offered)
        self.claim_entered.set()
        if self.claim_gate is not None:
            await self.claim_gate.wait()
        if status != 200:
            return web.Response(status=status, headers={"Retry-After": "180"})
        return web.json_response(offered)

    async def descriptions(self, ws, serial):
        data = self.data[serial]
        # A real broker redelivers the unchanged retained information.
        if not self.retained_info:
            self.stamp += 1
        info = {**data["info"], "timestamp": self.stamp}
        for suffix, value in (
            ("topics/info", info),
            ("topics/meta", data["metadata"]),
            ("topics/sensor", data["sensor"]),
        ):
            name = topic("account-A", serial, suffix)
            if suffix == "topics/info" and self.retained_info:
                await ws.send_bytes(
                    packet(0x31, string(name) + b"\0" + json.dumps(value).encode())
                )
            else:
                await ws.send_bytes(publish(name, value))

    async def broker(self, request):
        ws = web.WebSocketResponse(protocols=("mqtt",))
        await ws.prepare(request)
        self.sockets.add(ws)
        self.connections.append(request.headers["Authorization"])
        pending = bytearray()
        try:
            async for frame in ws:
                if frame.type != web.WSMsgType.BINARY:
                    continue
                pending.extend(frame.data)
                while len(pending) > 1:
                    count, shift, pos = 0, 0, 1
                    while pos < len(pending):
                        value = pending[pos]
                        count += (value & 127) << shift
                        pos += 1
                        if value < 128:
                            break
                        shift += 7
                    if len(pending) < pos + count:
                        break
                    header, body = pending[0], bytes(pending[pos : pos + count])
                    del pending[: pos + count]
                    kind = header >> 4
                    if kind == 1:
                        assert body.startswith(b"\0\4MQTT\5")
                        assert string("account-A") in body
                        code = self.connacks.pop(0) if self.connacks else self.connack
                        await ws.send_bytes(packet(0x20, bytes([0, code, 0])))
                    elif kind in (8, 10):
                        size = int.from_bytes(body[3:5], "big")
                        subscription = body[5 : 5 + size].decode()
                        self.filters.append((kind, subscription))
                        await ws.send_bytes(
                            packet(
                                0x90 if kind == 8 else 0xB0,
                                body[:2] + b"\0" + bytes([self.suback])
                                if kind == 8
                                else body[:2] + b"\0\0",
                            )
                        )
                        if kind == 8 and self.suback < 0x80:
                            selected = (
                                self.offered
                                if subscription == "users/account-A/#"
                                else [subscription.split("/")[3]]
                            )
                            for serial in selected:
                                await self.descriptions(ws, serial)
                    elif kind == 3:
                        size = int.from_bytes(body[:2], "big")
                        name = body[2 : 2 + size].decode()
                        pid = body[2 + size : 4 + size]
                        serial = name.split("/")[3]
                        self.sent.append(name)
                        await ws.send_bytes(packet(0x40, pid))
                        shadow = self.data[serial]["shadow"]
                        if name.endswith("/update"):
                            desired = json.loads(body[5 + size :])["state"]["desired"]
                            self.writes.append((serial, desired))
                            shadow["state"]["reported"].update(desired)
                            shadow["version"] += 1
                            shadow["timestamp"] += 1
                        gate = self.reply_gates.get((serial, name.rsplit("/", 1)[-1]))
                        if gate is not None:
                            await gate.wait()
                        await ws.send_bytes(publish(name + "/accepted", shadow))
                    elif kind == 12:
                        await ws.send_bytes(packet(0xD0, b""))
                    elif kind == 14:
                        await ws.close()
                        return ws
        finally:
            self.sockets.discard(ws)
        return ws


def binding(owner, serial):
    return next(
        key
        for key, item in owner.connections.snapshot.bindings.items()
        if item.adapter == "bacon" and item.to_data()["locator"]["value"] == serial
    )


def view(owner, serial=A):
    return owner.connections.source_data(binding(owner, serial))


def ready(owner, serial=A):
    access = view(owner, serial)
    return (
        access
        and access.available(serial)
        and access.device.state_generation == access.generation
        and access.device.metadata_generation == access.generation
    )


class _InstantPause:
    """``asyncio`` for the session module, with a reconnect pause that only
    records its length (no five real seconds per reconnect)."""

    def __init__(self, pauses: list[float]) -> None:
        self._pauses = pauses

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay, result=None):
        self._pauses.append(delay)
        return await asyncio.sleep(0, result)


@contextmanager
def instant_reconnect_pause():
    """The session reconnects without waiting; the pauses it chose are kept."""
    pauses: list[float] = []
    with patch(ROOT + ".bacon.session.asyncio", _InstantPause(pauses)):
        yield pauses


@asynccontextmanager
async def connected(hass, broker, *, only_bacon=False, wrong_account=False):
    def configure(data, options):
        old = legacy([HEAT, "gateway-b"], [A, B])
        old.update(
            bacon_account=account_key("wrong" if wrong_account else "account-A"),
            access_token=token(),
            expires_at=10**12,
            scope=["bacon"],
        )
        snapshot = convert_legacy_configuration(OWNER, old, {})
        fresh, choices = snapshot.to_data(), snapshot.to_options()
        first = next(
            value["target_id"]
            for value in fresh["bindings"].values()
            if value["adapter"] == "pointt" and value["locator"]["value"] == HEAT
        )
        fresh["targets"] = {first: fresh["targets"][first], **fresh["targets"]}
        add_local(fresh, choices)
        cloud = next(
            key
            for key, value in fresh["connections"].items()
            if value["kind"] == "singlekey_cloud"
        )
        credentials = fresh["connections"][cloud]["credentials"]
        credentials.update(access_token=token(), expires_at=10**12, scope=["bacon"])
        if only_bacon:
            for key, item in fresh["bindings"].items():
                if item["adapter"] == "pointt":
                    choices["bindings"][key]["enabled"] = False
        data.clear()
        data.update(fresh)
        options.clear()
        options.update(choices)

    original = ManagedBaconAccount

    def account_factory(pool, client):
        account = original(pool, client)
        target = urlsplit(client.url)

        def mqtt(host, port, **kwargs):
            assert host == "broker.euc1.bacon.bosch-tt-cw.com" and port == 443
            assert kwargs.pop("tls_context").verify_mode == ssl.CERT_REQUIRED
            return aiomqtt.Client(target.hostname, target.port, **kwargs)

        account._factory = mqtt
        account._timeout = 2
        return account

    def client_factory(session, tokens, **kwargs):
        # The running helper supplies the same loopback address to the PointT client.
        url = session_url[0]
        return BaconClient(session, tokens, url=url + "/devices", **kwargs)

    session_url = []
    original_pointt = PointTClient

    def pointt_factory(*args, **kwargs):
        session_url[:] = [kwargs["base_url"]]
        return original_pointt(*args, **kwargs)

    with (
        patch("tests.test_managed_pointt.PointTClient", side_effect=pointt_factory),
        patch(ROOT + ".managed_bacon.ManagedBaconAccount", side_effect=account_factory),
        patch(ROOT + ".managed_bacon.BaconClient", side_effect=client_factory),
    ):
        async with running(hass, broker, configure=configure) as result:
            try:
                yield result
            finally:
                if broker.claim_gate is not None:
                    broker.claim_gate.set()
                for gate in broker.reply_gates.values():
                    gate.set()


@pytest.mark.parametrize("name", ["midea", "rwh", "cl_7000i", "air_6001i", "dry_8000i"])
async def test_existing_families_use_one_session_and_confirm_writes(hass, name):
    broker = Broker(name)
    async with connected(hass, broker) as (_, _, owner, _):
        await eventually(
            lambda: ready(owner, A) and ready(owner, B) and source_for(owner, HEAT)
        )
        a, b, heating = view(owner, A), view(owner, B), source_for(owner, HEAT)
        assert a.account is b.account
        assert a.client.tokens is heating.client._token_provider
        assert a.client.backoff is heating.client.backoff
        assert a.client._semaphore is heating.client._transport._semaphore
        assert len(broker.connections) == 1
        assert len(broker.claims) == 1
        assert set(a.devices) == {A} and set(b.devices) == {B}
        entity = (
            BaconClimate(a, A)
            if name == "cl_7000i"
            else BaconPurifierFan(a, A)
            if name == "air_6001i"
            else BaconDehumidifier(a, A)
            if name == "dry_8000i"
            else BaconWaterHeater(a, A)
        )
        if name in ("midea", "rwh"):
            await entity.async_set_temperature(temperature=55)
            assert entity.native_target_temperature == 55
        else:
            await entity.async_turn_off()
        assert len(broker.writes) == 1 and broker.writes[0][0] == A
        assert a.device.state.reported != b.device.state.reported
        assert BaconConnectivity(a, A).unique_id != BaconConnectivity(b, B).unique_id


async def test_bacon_only_needs_no_pointt_discovery(hass):
    broker = Broker()
    async with connected(hass, broker, only_bacon=True) as (_, _, owner, _):
        await eventually(lambda: ready(owner))
        assert not broker.calls
        assert owner.connections.source_data("local")


async def test_disable_and_reenable_one_device_keeps_peer_and_socket(hass):
    broker = Broker()
    async with connected(hass, broker) as (_, cloud, owner, _):
        await eventually(lambda: ready(owner, A) and ready(owner, B))
        old, peer = view(owner, A), view(owner, B)
        entity = BaconConnectivity(old, A)
        manager = owner.connections.management
        manager.set_binding_enabled(manager.edit(cloud), binding(owner, A), False)
        assert not entity.available
        await settle(hass)
        assert view(owner, B) is peer and peer.available(B)
        with pytest.raises(BaconError, match="source_stopped"):
            await old.write_state(A, {"tempSetpoint": 55}, lambda *_: True)
        manager.set_binding_enabled(manager.edit(cloud), binding(owner, A), True)
        await eventually(lambda: ready(owner, A))
        current = view(owner, A)
        assert current is not old and current.device is not old.device
        assert BaconConnectivity(current, A).unique_id == entity.unique_id
        assert not entity.available
        assert len(broker.connections) == 1
        assert view(owner, B) is peer


async def test_last_additional_device_stops_only_mqtt(hass):
    broker = Broker()
    async with connected(hass, broker) as (_, cloud, owner, _):
        await eventually(
            lambda: ready(owner, A) and ready(owner, B) and source_for(owner, HEAT)
        )
        account, heating = view(owner).account, source_for(owner, HEAT)
        manager = owner.connections.management
        for serial in (A, B):
            manager.set_binding_enabled(
                manager.edit(cloud), binding(owner, serial), False
            )
        await eventually(lambda: account._task.done())
        assert not account.connected
        assert source_for(owner, HEAT) is heating
        assert owner.connections.source_data("local")
        manager.set_binding_enabled(manager.edit(cloud), binding(owner, A), True)
        await eventually(lambda: ready(owner, A))
        assert view(owner).account is not account
        assert len(broker.connections) == 2


@pytest.mark.parametrize("stage", ["get", "update"])
async def test_stopping_a_pending_write_never_confirms_or_replays(hass, stage):
    broker = Broker()
    async with connected(hass, broker) as (_, cloud, owner, _):
        await eventually(lambda: ready(owner, A) and ready(owner, B))
        old = view(owner)
        broker.sent.clear()
        broker.reply_gates[(A, stage)] = asyncio.Event()
        command = asyncio.create_task(
            old.write_state(A, {"tempSetpoint": 55}, lambda *_: True)
        )
        await eventually(
            lambda: topic("account-A", A, "shadows/state/" + stage) in broker.sent
        )
        manager = owner.connections.management
        manager.set_binding_enabled(manager.edit(cloud), binding(owner, A), False)
        with pytest.raises(BaconError, match="source_stopped"):
            await command
        broker.reply_gates[(A, stage)].set()
        assert len(broker.writes) == int(stage == "update")
        assert view(owner, B).available(B)
        assert not old.available(A)
        assert len(broker.connections) == 1


@pytest.mark.parametrize(
    ("category", "stopped", "expected"),
    [
        ("command_unconfirmed", True, "source_stopped"),
        ("connection", True, "source_stopped"),
        ("command_unconfirmed", False, "command_unconfirmed"),
        ("authentication", True, "authentication"),
    ],
)
async def test_a_stop_before_the_cancellation_is_still_a_stop(
    category, stopped, expected
):
    """A publish finishing right after the stop, before close() cancels it."""
    from custom_components.bosch_buderus_heating.managed_bacon import BaconSource

    source = object.__new__(BaconSource)
    source._closed = False
    source._operations = set()
    source.pool = SimpleNamespace(auth_failed=asyncio.Event())
    source.context = SimpleNamespace(current=True)

    async def action():
        source.context.current = not stopped
        raise BaconError(category)

    with pytest.raises(BaconError, match=expected):
        await source.operation(action)
    assert not source._operations


async def test_retired_read_and_wrong_device_cannot_publish(hass):
    broker = Broker()
    async with connected(hass, broker) as (_, _, owner, _):
        await eventually(lambda: ready(owner))
        before = len(broker.sent)
        with pytest.raises(BaconError, match="invalid_identifier"):
            await view(owner).write_state(B, {"tempSetpoint": 55}, lambda *_: True)
        assert len(broker.sent) == before


@pytest.mark.parametrize("where", ["http", "mqtt"])
async def test_additional_authentication_reauths_shared_cloud_only(hass, where):
    broker = Broker()
    if where == "http":
        broker.claim_status = 401
    else:
        broker.connack = 135
    with patch(
        ROOT + ".pointt.auth.OAuthClient.refresh",
        AsyncMock(side_effect=RefreshTokenRejected()),
    ):
        async with connected(hass, broker) as (entry, cloud, owner, _):
            await eventually(
                lambda: (
                    owner.connections.connection_phase(cloud)
                    is ConnectionPhase.AUTH_REQUIRED
                )
            )
            await settle(hass)
            assert source_for(owner, HEAT) is None
            assert owner.connections.source_data("local")
            flows = list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
            assert len(flows) == 1 and flows[0]["context"]["connection_id"] == cloud


async def test_wrong_account_is_rejected_before_claim_or_mqtt(hass):
    broker = Broker()
    async with connected(hass, broker, wrong_account=True) as (_, cloud, owner, _):
        await eventually(
            lambda: (
                owner.connections.connection_phase(cloud)
                is ConnectionPhase.AUTH_REQUIRED
            )
        )
        assert not broker.claims and not broker.connections


@pytest.mark.parametrize("origin", ["http", "mqtt", "pointt"])
async def test_account_limits_are_shared_by_all_cloud_backends(hass, origin):
    broker = Broker()
    if origin == "http":
        broker.claim_status = 429
    if origin == "mqtt":
        broker.connack = 151
    async with connected(hass, broker) as (_, _, owner, _):
        if origin == "pointt":
            await eventually(lambda: ready(owner) and source_for(owner, HEAT))
            source_for(owner, HEAT).client.backoff.activate(180)
        else:
            await eventually(
                lambda: view(owner) and view(owner).account.error == "rate_limited"
            )
        access = view(owner)
        with pytest.raises(RateLimited):
            access.client.backoff.raise_if_active()
        before = len(broker.claims), len(broker.sent), len(broker.calls)
        remaining = access.client.backoff.remaining
        # C49: the local block of the shared pause neither calls nor extends it.
        with pytest.raises(BaconError, match="paused"):
            await access.client.get_devices()
        assert access.client.backoff.remaining <= remaining
        if origin == "pointt":
            with pytest.raises(BaconError):
                await access.read_state(A)
        assert before == (len(broker.claims), len(broker.sent), len(broker.calls))
        assert owner.connections.source_data("local")


@pytest.mark.parametrize("status", [200, 401])
async def test_late_http_reply_after_last_device_disabled_cannot_refresh(hass, status):
    broker = Broker()
    with patch(ROOT + ".pointt.auth.OAuthClient.refresh", AsyncMock()) as refresh:
        async with connected(hass, broker) as (_, cloud, owner, _):
            await eventually(lambda: ready(owner, A) and ready(owner, B))
            client = view(owner).client
            broker.claim_status = status
            broker.claim_gate = asyncio.Event()
            broker.claim_entered.clear()
            pending = asyncio.create_task(client.get_devices())
            await broker.claim_entered.wait()
            manager = owner.connections.management
            for serial in (A, B):
                manager.set_binding_enabled(
                    manager.edit(cloud), binding(owner, serial), False
                )
            broker.claim_gate.set()
            with pytest.raises(BaconError, match="source_stopped"):
                await pending
            refresh.assert_not_awaited()


async def test_disabled_device_cannot_send_a_command_waiting_for_publish(hass):
    broker = Broker()
    async with connected(hass, broker) as (_, cloud, owner, _):
        await eventually(lambda: ready(owner, A) and ready(owner, B))
        access = view(owner)
        before = len(broker.sent)
        lock = access.account._publish_lock
        await lock.acquire()
        command = asyncio.create_task(
            access.write_state(A, {"tempSetpoint": 55}, lambda *_: True)
        )
        try:
            await eventually(lambda: bool(lock._waiters))
            manager = owner.connections.management
            manager.set_binding_enabled(manager.edit(cloud), binding(owner, A), False)
            with pytest.raises(BaconError, match="source_stopped"):
                await command
        finally:
            lock.release()
        assert len(broker.sent) == before and not broker.writes
        assert view(owner, B).available(B)


@pytest.mark.parametrize("missing", [False, True])
async def test_new_login_replaces_session_even_without_disabled_devices(hass, missing):
    broker = Broker()
    async with connected(hass, broker) as (entry, cloud, owner, _):
        await eventually(lambda: ready(owner) and source_for(owner, HEAT))
        previous, heating = view(owner), source_for(owner, HEAT)
        local = owner.connections.source_data("local")
        manager = owner.connections.management
        manager.set_binding_enabled(manager.edit(cloud), binding(owner, B), False)
        await settle(hass)
        if missing:
            broker.offered.remove(B)
        renewed = AuthTokens(
            token(signature="replacement"), "refresh-2", 10**12, scope=("bacon",)
        )
        url = heating.client._transport._base_url
        old_tokens = EntryConfiguration(entry, cloud).tokens()
        with (
            patch(
                ROOT + ".configuration_management.PointTClient",
                side_effect=lambda *a, **kw: PointTClient(*a, **kw, base_url=url),
            ),
            patch(
                ROOT + ".bacon.flow.BaconClient",
                side_effect=lambda *a, **kw: BaconClient(
                    *a, **kw, url=url + "/devices"
                ),
            ),
        ):
            # A switched-off device missing from the account never blocks.
            await manager.async_reauthenticate(manager.edit(cloud), renewed)
        assert EntryConfiguration(entry, cloud).tokens() != old_tokens
        await eventually(
            lambda: (
                ready(owner)
                and view(owner) is not previous
                and source_for(owner, HEAT) is not None
                and source_for(owner, HEAT) is not heating
            )
        )
        current = view(owner)
        assert current.client.tokens is source_for(owner, HEAT).client._token_provider
        assert current.client.tokens is not previous.client.tokens
        assert current.client.backoff is previous.client.backoff
        assert EntryConfiguration(entry, cloud).tokens() == renewed
        assert len(broker.connections) == 2
        assert broker.connections[-1] == "Bearer " + renewed.access_token
        assert view(owner, B) is None
        with pytest.raises(BaconError, match="source_stopped"):
            await previous.read_state(A)
        assert not previous.available(A)
        assert owner.connections.source_data("local") is local


@pytest.mark.parametrize("reason", ["token_rotation", "disconnect"])
async def test_mqtt_reconnect_keeps_pointt_and_device_access(hass, reason):
    broker = Broker()
    with instant_reconnect_pause() as pauses:
        await _reconnect_keeps_pointt_and_device_access(hass, broker, reason)
    # A planned renewal reconnects at once; a lost connection after the
    # first backoff step.
    assert pauses == ([] if reason == "token_rotation" else [5.0])


async def _reconnect_keeps_pointt_and_device_access(hass, broker, reason):
    async with connected(hass, broker) as (_, _, owner, _):
        await eventually(lambda: ready(owner) and source_for(owner, HEAT))
        previous, heating = view(owner), source_for(owner, HEAT)
        account, generation = previous.account, previous.generation
        seen = []
        previous.add_listener(lambda: seen.append(previous.available(A)))
        if reason == "token_rotation":
            renewed = AuthTokens(
                token(signature="rotation"), "refresh-2", 10**12, scope=("bacon",)
            )
            with patch.object(
                previous.client.tokens._oauth_client,
                "refresh",
                AsyncMock(return_value=renewed),
            ):
                await previous.client.tokens.get_access_token(force_refresh=True)
            account.selection_changed.set()
        else:
            for socket in tuple(broker.sockets):
                await socket.close()
        await eventually(lambda: False in seen)
        assert source_for(owner, HEAT) is heating
        await eventually(
            lambda: ready(owner) and previous.generation > generation, timeout=15
        )
        assert view(owner) is previous and previous.account is account
        assert source_for(owner, HEAT) is heating
        assert len(broker.connections) == 2 and not broker.writes
        if reason == "token_rotation":
            assert broker.connections[-1] == "Bearer " + renewed.access_token
            # C52: a planned renewal reconnects at once with the recent list.
            assert len(broker.claims) == 1
            assert (account.renewals, account.reconnects) == (1, 0)
        else:
            assert len(broker.claims) == 2
            assert (account.renewals, account.reconnects) == (0, 1)


async def test_membership_loss_and_fresh_recovery_keep_peer_and_identity(hass):
    broker = Broker()
    async with connected(hass, broker) as (_, _, owner, _):
        await eventually(lambda: ready(owner, A) and ready(owner, B))
        access, peer = view(owner, A), view(owner, B)
        account = access.account
        waiting = asyncio.Event()
        wait = account._wait_for_maintenance

        async def observed_wait(receiver):
            waiting.set()
            await wait(receiver)
            waiting.clear()

        with (
            patch(ROOT + ".bacon.session.RESYNC_INTERVAL", 0),
            patch.object(account, "_wait_for_maintenance", side_effect=observed_wait),
        ):
            broker.offered.remove(A)
            account.selection_changed.set()
            await eventually(lambda: A not in account._claimed)
            await eventually(waiting.is_set)
            assert not access.available(A) and peer.available(B)
            broker.offered.append(A)
            account.selection_changed.set()
            await eventually(lambda: A in account._claimed)
            for socket in tuple(broker.sockets):
                await broker.descriptions(socket, A)
            await eventually(lambda: ready(owner, A))
        assert view(owner, A) is access and view(owner, B) is peer
        assert len(broker.connections) == 1


async def new_sensor_sample(broker, serial=A):
    sample = json.loads(json.dumps(broker.data[serial]["sensor"]))
    for item in sample["items"]:
        item["timestamp"] += 1000
    for socket in tuple(broker.sockets):
        await socket.send_bytes(
            publish(topic("account-A", serial, "topics/sensor"), sample)
        )


async def test_reenabled_device_recovers_from_unchanged_retained_information(hass):
    broker = Broker()
    async with connected(hass, broker) as (_, cloud, owner, _):
        await eventually(lambda: ready(owner, A) and ready(owner, B))
        manager = owner.connections.management
        manager.set_binding_enabled(manager.edit(cloud), binding(owner, A), False)
        await settle(hass)
        broker.retained_info = True
        manager.set_binding_enabled(manager.edit(cloud), binding(owner, A), True)
        # The redelivered information keeps its timestamp; it restores the
        # online state and the model, but is no sign of life of its own.
        await eventually(
            lambda: (
                view(owner, A) is not None
                and view(owner, A).device.online is True
                and view(owner, A).device.model is not None
            )
        )
        current = view(owner, A)
        assert current.device.alive_at is None and not current.available(A)
        await new_sensor_sample(broker)
        await eventually(lambda: ready(owner, A))


async def test_reconnect_with_only_retained_information_keeps_the_device(hass):
    broker = Broker()
    with instant_reconnect_pause() as pauses:
        await _reconnect_with_only_retained_information(hass, broker)
    assert pauses == [5.0]


async def _reconnect_with_only_retained_information(hass, broker):
    async with connected(hass, broker) as (_, _, owner, _):
        await eventually(lambda: ready(owner))
        access = view(owner)
        alive, generation = access.device.alive_at, access.generation
        broker.retained_info = True
        for socket in tuple(broker.sockets):
            await socket.close()
        await eventually(lambda: access.generation > generation, timeout=15)
        # Our reconnect is no device silence: the last sign of life still counts.
        await eventually(lambda: ready(owner), timeout=15)
        assert access.device.alive_at == alive
        # The unchanged sensor sample is no new reading, yet the earlier one
        # stays visible across our reconnect.
        assert access.device.sensor_generation == access.generation
        assert access.device.sensor
        assert len(broker.connections) == 2


async def test_owner_unload_closes_mqtt_and_retires_device_views(hass):
    broker = Broker()
    async with connected(hass, broker) as (_, _, owner, _):
        await eventually(lambda: ready(owner, A) and ready(owner, B))
        access = view(owner)
        notified = []
        access.add_listener(lambda: notified.append(access.available(A)))
        await owner.close()
        await eventually(lambda: not broker.sockets)
        assert access.account._task.done() and not access.account.connected
        assert notified and notified[-1] is False
        assert view(owner) is None
        with pytest.raises(BaconError, match="source_stopped"):
            await access.read_state(A)


async def test_replacement_during_pending_selection_receives_fresh_metadata(hass):
    broker = Broker()
    async with connected(hass, broker) as (_, cloud, owner, _):
        await eventually(lambda: ready(owner, A) and ready(owner, B))
        previous, peer = view(owner, A), view(owner, B)
        account = previous.account
        entered, release = asyncio.Event(), asyncio.Event()
        update = account._selection_update

        async def delayed(*args):
            entered.set()
            await release.wait()
            await update(*args)

        manager = owner.connections.management
        with patch.object(account, "_selection_update", side_effect=delayed):
            try:
                manager.set_binding_enabled(
                    manager.edit(cloud), binding(owner, A), False
                )
                await eventually(entered.is_set)
                manager.set_binding_enabled(
                    manager.edit(cloud), binding(owner, A), True
                )
                await eventually(
                    lambda: view(owner) is not None and view(owner) is not previous
                )
            finally:
                release.set()
            await eventually(lambda: ready(owner))
        assert len(broker.connections) == 1 and view(owner, B) is peer
        assert (8, topic("account-A", A, "#")) in broker.filters
        assert not previous.available(A)


async def test_claim_service_failure_retries_without_stopping_pointt(hass):
    broker = Broker()
    broker.claim_status = 503
    sleeping, release = asyncio.Event(), asyncio.Event()

    async def sleep(delay):
        assert delay == 60
        sleeping.set()
        await release.wait()

    clock = SimpleNamespace(**{**vars(asyncio), "sleep": sleep})
    with patch(ROOT + ".bacon.session.asyncio", clock):
        async with connected(hass, broker) as (_, cloud, owner, _):
            await eventually(lambda: sleeping.is_set() and source_for(owner, HEAT))
            assert view(owner).account.error == "service_unavailable"
            heating = source_for(owner, HEAT)
            assert owner.connections.connection_phase(cloud) is ConnectionPhase.READY
            assert owner.connections.source_data("local")
            broker.claim_status = 200
            release.set()
            await eventually(lambda: ready(owner))
            assert source_for(owner, HEAT) is heating
            assert len(broker.connections) == 1


async def test_refused_mqtt_sign_in_once_keeps_the_heat_pump(hass):
    """C45: one refused broker sign-in is retried with a fresh token."""
    broker = Broker()
    broker.connacks = [135]
    fresh = AuthTokens(token(signature="fresh"), "refresh-2", 10**12, scope=("bacon",))
    with patch(
        ROOT + ".pointt.auth.OAuthClient.refresh", AsyncMock(return_value=fresh)
    ) as refresh:
        async with connected(hass, broker) as (entry, cloud, owner, _):
            await eventually(lambda: ready(owner) and source_for(owner, HEAT))
            assert owner.connections.connection_phase(cloud) is ConnectionPhase.READY
            assert len(broker.connections) == 2
            assert broker.connections[-1] == "Bearer " + fresh.access_token
            refresh.assert_awaited_once()
            assert not list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_rejected_subscription_is_visible_and_keeps_the_heat_pump(hass):
    """C46: a refused SUBACK is a connection failure, not a silent session."""
    broker = Broker()
    broker.suback = 0x87
    async with connected(hass, broker) as (_, cloud, owner, _):
        await eventually(
            lambda: (
                view(owner)
                and view(owner).account.error == "subscription_rejected"
                and source_for(owner, HEAT)
            )
        )
        account = view(owner).account
        assert not account.connected and not view(owner).available(A)
        assert owner.connections.connection_phase(cloud) is ConnectionPhase.READY


async def test_periodic_claim_failure_keeps_the_mqtt_session(hass):
    """C48: a failed five-minute device list keeps devices and connection."""
    broker = Broker()
    async with connected(hass, broker) as (_, _, owner, _):
        await eventually(lambda: ready(owner, A) and ready(owner, B))
        account = view(owner).account
        claims = len(broker.claims)
        broker.claim_status = 503

        def woken(condition):
            # Wake the maintenance loop until the next resynchronization ran.
            account.selection_changed.set()
            return condition()

        with patch(ROOT + ".bacon.session.RESYNC_INTERVAL", 0):
            await eventually(
                lambda: woken(lambda: account.error == "service_unavailable")
            )
            assert ready(owner, A) and ready(owner, B) and account.connected
            assert len(broker.claims) > claims
            broker.claim_status = 200
            await eventually(lambda: woken(lambda: account.error is None))
        assert len(broker.connections) == 1
        assert ready(owner, A) and ready(owner, B)


async def test_added_device_waits_for_the_claim_service_without_reconnecting(hass):
    """C48: a newly enabled device retries later; its peer keeps the socket."""
    broker = Broker()
    with patch(ROOT + ".managed_bacon.MAINTENANCE_INTERVAL", 0.05):
        async with connected(hass, broker) as (_, cloud, owner, _):
            await eventually(lambda: ready(owner, A) and ready(owner, B))
            peer = view(owner, B)
            account = peer.account
            manager = owner.connections.management
            manager.set_binding_enabled(manager.edit(cloud), binding(owner, A), False)
            await settle(hass)
            broker.claim_status = 503
            manager.set_binding_enabled(manager.edit(cloud), binding(owner, A), True)
            await eventually(lambda: account._selection_retry_at is not None)
            assert account.error == "service_unavailable"
            assert not ready(owner, A) and peer.available(B)
            broker.claim_status = 200
            # The retry time has come.
            account._selection_retry_at = 0.0
            await eventually(lambda: ready(owner, A))
            assert account._selection_retry_at is None
            assert len(broker.connections) == 1 and view(owner, B) is peer
            assert (8, topic("account-A", A, "#")) in broker.filters
