"""Real HA configuration events with controlled independent source lifetimes."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.configuration_migration import (
    convert_legacy_configuration,
)
from custom_components.bosch_buderus_heating.connection_inventory import (
    ConnectionInventory,
    RetainedEntity,
)
from custom_components.bosch_buderus_heating.connection_runtime import (
    ConnectionPhase,
    ConnectionRuntime,
    SourceFailure,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    Gateway,
    RateLimited,
    RefreshTokenRejected,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_configuration import OWNER, add_local, legacy

MODULE = "custom_components.bosch_buderus_heating.configuration_management"


def entry_for(hass, *, second_cloud=False):
    snapshot = convert_legacy_configuration(
        OWNER, legacy(["000123", "gateway-b"], ["softener"]), {}
    )
    data, options = snapshot.to_data(), snapshot.to_options()
    target = add_local(data, options)
    cloud = next(iter(snapshot.connections))
    if second_cloud:
        data["connections"]["second"] = deepcopy(data["connections"][cloud])
        options["connections"]["second"] = deepcopy(options["connections"][cloud])
        binding = next(
            key
            for key, value in data["bindings"].items()
            if value["locator"]["value"] == "gateway-b"
        )
        data["bindings"][binding]["connection_id"] = "second"
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=OWNER,
        version=4,
        minor_version=1,
        data=data,
        options=options,
    )
    entry.add_to_hass(hass)
    return entry, cloud, target


class Source:
    def __init__(self, binding_id):
        self.binding_id = binding_id
        self.context = None
        self.closed = 0
        self.messages = asyncio.Queue()
        self.cancelled = asyncio.Event()
        self.late = None

    async def run(self, context):
        self.context = context
        assert context.publish({"sample": self.binding_id})
        try:
            while True:
                message = await self.messages.get()
                if isinstance(message, Exception):
                    raise message
                context.publish(message)
        except asyncio.CancelledError:
            self.cancelled.set()
            if self.late is not None:
                await self.late.wait()
                assert not context.publish({"stale": True})
            raise

    async def close(self):
        self.closed += 1


class Driver:
    def __init__(self, name, harness):
        self.name = name
        self.harness = harness
        self.sources = {}
        self.closed = 0

    async def connect(self):
        if gate := self.harness.gates.get(self.name):
            await gate.wait()
        if error := self.harness.errors.get(self.name):
            raise error

    def source(self, binding_id, binding):
        source = Source(binding_id)
        self.sources.setdefault(binding_id, []).append(source)
        return source

    async def close(self):
        self.closed += 1


class Harness:
    def __init__(self):
        self.drivers = {}
        self.gates = {}
        self.errors = {}
        self.sleeps = asyncio.Queue()
        self.retry = asyncio.Event()

    def factory(self, key, record):
        driver = Driver(key, self)
        self.drivers.setdefault(key, []).append(driver)
        return driver

    async def sleep(self, delay):
        self.sleeps.put_nowait(delay)
        await self.retry.wait()

    def source(self, connection, binding):
        return self.drivers[connection][-1].sources[binding][-1]


async def settle(hass):
    for _ in range(16):
        await hass.async_block_till_done()
        await asyncio.sleep(0)


def build(hass, **kwargs):
    entry, cloud, target = entry_for(hass, **kwargs)
    harness = Harness()
    runtime = ConnectionRuntime(
        hass,
        entry,
        {"singlekey_cloud": harness.factory, "k40_local": harness.factory},
        sleep=harness.sleep,
    )
    return entry, cloud, target, harness, runtime


async def test_slow_cloud_does_not_block_local_or_owner_start(hass):
    _, cloud, _, harness, runtime = build(hass)
    harness.gates[cloud] = asyncio.Event()
    runtime.start()
    await settle(hass)
    try:
        assert runtime.connection_phase(cloud) is ConnectionPhase.CONNECTING
        assert runtime.source_data("local") == {"sample": "local"}
        harness.gates[cloud].set()
        await settle(hass)
        assert runtime.connection_phase(cloud) is ConnectionPhase.READY
        assert len(harness.drivers[cloud]) == 1
        assert len(harness.drivers[cloud][0].sources) == 3
    finally:
        await runtime.close()
    assert not runtime._tasks


async def test_disabling_one_target_preserves_shared_session_and_other_targets(hass):
    entry, cloud, target, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)
    first = next(
        key
        for key, binding in runtime.snapshot.bindings.items()
        if binding.target_id == target and binding.connection_id == cloud
    )
    others = [
        key
        for key, binding in runtime.snapshot.bindings.items()
        if binding.connection_id == cloud and key != first
    ]
    old = harness.source(cloud, first)
    try:
        runtime.management.set_binding_enabled(
            runtime.management.edit(cloud), first, False
        )
        assert not old.context.publish({"unexpected": True})
        await settle(hass)
        assert runtime.source_data(first) is None
        assert all(runtime.source_data(key) for key in others)
        assert runtime.source_data("local")
        assert len(harness.drivers[cloud]) == 1
        assert old.closed == 1
        runtime.management.set_binding_enabled(
            runtime.management.edit(cloud), first, True
        )
        await settle(hass)
        assert runtime.source_data(first)
        assert len(harness.drivers[cloud]) == 1
        assert harness.source(cloud, first) is not old
        assert entry.version == 4
    finally:
        await runtime.close()


async def test_late_cancelled_answer_cannot_replace_new_connection_result(hass):
    entry, _, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)
    old = harness.source("local", "local")
    old.late = asyncio.Event()
    try:
        data = deepcopy(dict(entry.data))
        data["connections"]["local"]["endpoint"]["host"] = "new.example.invalid"
        data["connections"]["local"]["config_revision"] += 1
        hass.config_entries.async_update_entry(entry, data=data)
        # A result arriving before the HA listener also observes the new generation.
        assert not old.context.publish({"stale": True})
        await settle(hass)
        assert runtime.source_data("local") is None
        assert all(
            runtime.source_data(key)
            for key, binding in runtime.snapshot.bindings.items()
            if binding.connection_id != "local"
        )
        old.late.set()
        await settle(hass)
        fresh = harness.source("local", "local")
        assert fresh is not old
        assert fresh.context.publish({"fresh": 42})
        assert old.closed == 1
        assert runtime.source_data("local") == {"fresh": 42}
    finally:
        old.late.set()
        await runtime.close()


@pytest.mark.parametrize(
    "error", [SourceFailure(), RefreshTokenRejected("private-login-data")]
)
async def test_source_errors_are_scoped_and_auth_errors_stop_siblings(hass, error):
    _, cloud, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)
    keys = [
        key
        for key, binding in runtime.snapshot.bindings.items()
        if binding.connection_id == cloud
    ]
    try:
        source = harness.source(cloud, keys[0])
        source.messages.put_nowait(error)
        await settle(hass)
        assert runtime.source_data(keys[0]) is None
        assert runtime.source_data("local")
        if isinstance(error, RefreshTokenRejected):
            assert runtime.pending_reauthentication == (cloud,)
            assert all(runtime.source_data(key) is None for key in keys)
            assert not source.context.current
            assert harness.sleeps.empty()
        else:
            assert not runtime.pending_reauthentication
            assert all(runtime.source_data(key) for key in keys[1:])
            assert await harness.sleeps.get() == 60
            harness.retry.set()
            await settle(hass)
            assert runtime.source_data(keys[0])
    finally:
        await runtime.close()


async def test_connection_retry_keeps_local_running_and_preserves_pause(hass):
    _, cloud, _, harness, runtime = build(hass)
    harness.errors[cloud] = RateLimited(900)
    runtime.start()
    await settle(hass)
    try:
        assert await harness.sleeps.get() == 900
        assert runtime.connection_phase(cloud) is ConnectionPhase.UNAVAILABLE
        assert runtime.source_data("local")
        assert harness.drivers[cloud][0].closed == 1
        del harness.errors[cloud]
        harness.retry.set()
        await settle(hass)
        assert runtime.connection_phase(cloud) is ConnectionPhase.READY
        assert len(harness.drivers[cloud]) == 2
    finally:
        await runtime.close()


async def test_removing_last_access_keeps_targets_registry_and_preferences(hass):
    entry, cloud, target, harness, runtime = build(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "000123")},
    )
    device = dr.async_get(hass).async_update_device(
        device.id, name_by_user="My heating"
    )
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "sensor", DOMAIN, "stable-identity", config_entry=entry, device_id=device.id
    )
    entity = registry.async_update_entity(
        entity.entity_id, name="My temperature", icon="mdi:thermometer"
    )
    disabled = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "disabled-identity",
        config_entry=entry,
        device_id=device.id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    inventory = ConnectionInventory(hass, entry)
    await inventory.load()
    inventory.restore_unavailable()
    runtime.start()
    await settle(hass)
    item = RetainedEntity(
        "sensor",
        entity.unique_id,
        target,
        "local",
        "k40_local",
        "outdoor_temperature",
        "/system/sensors/temperatures/outdoor_t1",
    )
    assert inventory.record(item, harness.source("local", "local").context)
    await inventory.save()
    try:
        targets = deepcopy(entry.data["targets"])
        runtime.management.remove(runtime.management.edit(cloud))
        runtime.management.remove(runtime.management.edit("local"))
        await settle(hass)
        assert not entry.data["connections"] and not entry.data["bindings"]
        assert entry.data["targets"] == targets
        assert not runtime._runs
        assert registry.async_get(entity.entity_id) == entity
        assert registry.async_get(disabled.entity_id) == disabled
        assert dr.async_get(hass).async_get(device.id) == device
        assert hass.states.get(entity.entity_id).state == STATE_UNAVAILABLE
        assert hass.states.get(disabled.entity_id) is None
        restored = ConnectionInventory(hass, entry)
        await restored.load()
        assert restored.entities == (item,)
        restored.restore_unavailable()
        assert hass.states.get(entity.entity_id).attributes["supported_features"] == 0
        assert not restored._removed
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "mode", ["all", "missing_gateway", "missing_device", "wrong_account"]
)
async def test_reauthentication_confirms_every_assigned_target(hass, mode):
    entry, cloud, _ = entry_for(hass)
    management = ConfigurationManagement(hass, entry)
    edit = management.edit(cloud)
    before = deepcopy(dict(entry.data))
    tokens = AuthTokens("new-access", "new-refresh", 10**12, scope=("bacon",))
    selected = EntryConfiguration(entry, cloud).cloud_data()
    gateways = (
        (Gateway("000123"), Gateway("gateway-b"))
        if mode != "missing_gateway"
        else (Gateway("000123"),)
    )
    devices = ("softener",) if mode != "missing_device" else ()
    account = selected["bacon_account"] if mode != "wrong_account" else "wrong"
    with (
        patch(MODULE + ".PointTClient.get_gateways", AsyncMock(return_value=gateways)),
        patch(MODULE + ".discover", AsyncMock(return_value=(devices, account))),
    ):
        if mode == "all":
            await management.async_reauthenticate(edit, tokens)
            assert EntryConfiguration(entry, cloud).tokens() == tokens
            assert entry.data["connections"][cloud]["auth_epoch"] == 1
            assert entry.data["connections"]["local"] == before["connections"]["local"]
        else:
            with pytest.raises(ConfigurationError, match="reauth_targets_missing"):
                await management.async_reauthenticate(edit, tokens)
            assert entry.data == before


@pytest.mark.parametrize(
    "change", ["rotation", "other_connection", "selection", "removal"]
)
async def test_open_reauthentication_does_not_overwrite_concurrent_changes(
    hass, change
):
    entry, cloud, _ = entry_for(hass)
    management = ConfigurationManagement(hass, entry)
    edit = management.edit(cloud)
    entered, release = asyncio.Event(), asyncio.Event()
    selected = EntryConfiguration(entry, cloud).cloud_data()

    async def gateways():
        entered.set()
        await release.wait()
        return (Gateway("000123"), Gateway("gateway-b"))

    tokens = AuthTokens("new", "new", 10**12)
    with (
        patch(MODULE + ".PointTClient.get_gateways", side_effect=gateways),
        patch(
            MODULE + ".discover",
            AsyncMock(return_value=(("softener",), selected["bacon_account"])),
        ),
    ):
        task = asyncio.create_task(management.async_reauthenticate(edit, tokens))
        await entered.wait()
        if change == "rotation":
            data = deepcopy(dict(entry.data))
            data["connections"][cloud]["credentials"].update(
                tokens_to_data(AuthTokens("rotated", "rotated", 10**12))
            )
            hass.config_entries.async_update_entry(entry, data=data)
        elif change == "other_connection":
            management.set_enabled(management.edit("local"), False)
        elif change == "selection":
            binding = next(
                key
                for key, item in management.current().bindings.items()
                if item.connection_id == cloud
            )
            management.set_binding_enabled(management.edit(cloud), binding, False)
        else:
            management.remove(management.edit(cloud))
        changed = deepcopy(dict(entry.data))
        release.set()
        if change in ("selection", "removal"):
            with pytest.raises(ConfigurationError):
                await task
            assert entry.data == changed
        else:
            await task
            assert EntryConfiguration(entry, cloud).tokens() == tokens
            if change == "other_connection":
                assert not entry.options["connections"]["local"]["enabled"]


async def test_source_driver_is_only_the_running_source(hass):
    _, _, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)
    try:
        connection = runtime.snapshot.bindings["local"].connection_id
        running = harness.source(connection, "local")
        assert runtime.source_driver("local") is running
        assert runtime.source_driver("missing") is None
        running.messages.put_nowait(RuntimeError("source failed"))
        await settle(hass)
        assert runtime.source_driver("local") is None
    finally:
        await runtime.close()
