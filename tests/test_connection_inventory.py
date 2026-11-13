"""Retained identities cannot grant writes or override HA registry ownership."""

import asyncio
import inspect
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, UnsupportedStorageVersionError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.connection_inventory import (
    ConnectionInventory,
    RetainedEntity,
)
from custom_components.bosch_buderus_heating.const import DOMAIN, PLATFORMS
from tests.test_connection_runtime import build, entry_for, settle


def record(target, platform="sensor"):
    return RetainedEntity(
        platform,
        "stable-private-identity",
        target,
        "local",
        "k40_local",
        "outdoor_temperature",
        "/system/sensors/temperatures/outdoor_t1",
    )


@pytest.mark.parametrize("platform", list(PLATFORMS))
async def test_inventory_roundtrip_retains_platform_identity_without_capabilities(
    hass, platform
):
    entry, _, target, harness, runtime = build(hass)
    inventory = ConnectionInventory(hass, entry)
    runtime.start()
    await settle(hass)
    try:
        item = record(target, platform)
        assert inventory.record(item, harness.source("local", "local").context)
        await inventory.save()
        restored = ConnectionInventory(hass, entry)
        await restored.load()
        assert restored.healthy and restored.entities == (item,)
        assert set(item.to_data()) == {
            "platform",
            "unique_id",
            "target_id",
            "binding_id",
            "adapter",
            "semantic_key",
            "resource_path",
        }
        assert "stable-private-identity" not in repr(item)
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "corruption", ["owner", "field", "value", "target", "duplicate", "root"]
)
async def test_invalid_inventory_preserves_registry_and_is_not_overwritten(
    hass, corruption
):
    entry, _, target = entry_for(hass)
    inventory = ConnectionInventory(hass, entry)
    registered = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "known-entity", config_entry=entry
    )
    raw = {"owner_id": entry.entry_id, "entities": [record(target).to_data()]}
    if corruption == "owner":
        raw["owner_id"] = "another-owner"
    elif corruption == "field":
        raw["entities"][0]["cached_value"] = 42
    elif corruption == "value":
        raw["entities"][0]["platform"] = "arbitrary_platform"
    elif corruption == "target":
        raw["entities"][0]["target_id"] = "unowned-target"
    elif corruption == "duplicate":
        raw["entities"] *= 2
    else:
        raw = []
    with (
        patch.object(inventory._store, "async_load", AsyncMock(return_value=raw)),
        patch.object(inventory._store, "async_save", AsyncMock()) as save,
    ):
        await inventory.load()
        assert not inventory.healthy and not inventory.entities
        await inventory.save()
        save.assert_not_awaited()
        inventory.restore_unavailable()
    assert hass.states.get(registered.entity_id).state == "unavailable"
    assert er.async_get(hass).async_get(registered.entity_id) == registered


@pytest.mark.parametrize(
    "field,value",
    [
        ("resource_path", "https://private.example.invalid"),
        ("resource_path", "/path?access_token=secret"),
        ("resource_path", "/path\nvalue"),
        ("resource_path", 123),
        ("adapter", "unknown"),
        ("unique_id", ""),
    ],
)
def test_unreviewed_inventory_descriptions_are_rejected(field, value):
    data = record("target").to_data()
    data[field] = value
    with pytest.raises(ConfigurationError):
        RetainedEntity.from_data(data)


async def test_inventory_rejects_other_target_and_old_source_and_enforces_limit(hass):
    entry, cloud, target, harness, runtime = build(hass)
    inventory = ConnectionInventory(hass, entry)
    runtime.start()
    await settle(hass)
    try:
        context = harness.source("local", "local").context
        item = record(target)
        with pytest.raises(ConfigurationError, match="inventory_binding_conflict"):
            inventory.record(replace(item, target_id="another-target"), context)
        assert inventory.record(item, context)
        key = next(
            key
            for key, binding in runtime.snapshot.bindings.items()
            if binding.connection_id == cloud and binding.target_id != target
        )
        binding = runtime.snapshot.bindings[key]
        conflicting = replace(
            item, binding_id=key, target_id=binding.target_id, adapter=binding.adapter
        )
        with pytest.raises(ConfigurationError, match="inventory_identity_conflict"):
            inventory.record(conflicting, harness.source(cloud, key).context)
        with (
            patch(
                "custom_components.bosch_buderus_heating.connection_inventory.MAX_ENTITY_RECORDS",
                1,
            ),
            pytest.raises(ConfigurationError, match="inventory_limit"),
        ):
            inventory.record(replace(item, unique_id="another-entity"), context)
        runtime.management.set_enabled(runtime.management.edit("local"), False)
        assert not inventory.record(item, context)
        assert inventory.entities == (item,)
    finally:
        await runtime.close()


async def test_inventory_deletion_waits_for_pending_save_and_prevents_resurrection(
    hass,
):
    entry, _, _ = entry_for(hass)
    inventory = ConnectionInventory(hass, entry)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def save(data):
        entered.set()
        await release.wait()
        calls.append("save")

    async def remove():
        calls.append("remove")

    with (
        patch.object(inventory._store, "async_save", side_effect=save),
        patch.object(inventory._store, "async_remove", side_effect=remove),
    ):
        task = asyncio.create_task(inventory.save())
        await entered.wait()
        deletion = asyncio.create_task(inventory.remove_owner())
        await asyncio.sleep(0)
        assert not deletion.done()
        release.set()
        await task
        await deletion
        await inventory.save()
    assert calls == ["save", "remove"]
    assert inventory._removed and not inventory.entities


@pytest.mark.parametrize("operation", ["load", "save"])
async def test_inventory_storage_failure_does_not_block_source_lifecycle(
    hass, operation
):
    entry, _, _ = entry_for(hass)
    inventory = ConnectionInventory(hass, entry)
    with patch.object(
        inventory._store,
        "async_" + operation,
        AsyncMock(side_effect=OSError("private filename")),
    ):
        await getattr(inventory, operation)()
    assert not inventory.healthy


def test_repeated_inventory_strings_are_shared():
    """O23: identical identifiers of many records share one string object."""

    def fresh(text):
        return "".join(list(text))  # a new, equal string object

    def data(unique_id):
        return {
            "platform": fresh("sensor"),
            "unique_id": unique_id,
            "target_id": fresh("target-identity"),
            "binding_id": fresh("binding-identity"),
            "adapter": fresh("k40_local"),
            "semantic_key": fresh("outdoor_temperature"),
            "resource_path": None,
        }

    first, second = (RetainedEntity.from_data(data(key)) for key in ("one", "two"))
    for name in ("platform", "target_id", "binding_id", "adapter"):
        assert getattr(first, name) is getattr(second, name), name
    assert first.to_data() == data("one")
    assert second == RetainedEntity.from_data(second.to_data())


async def test_inventory_store_is_private_and_atomic(hass):
    """O68: written like the integration's other stores."""
    entry, _, _ = entry_for(hass)
    calls: list[inspect.BoundArguments] = []

    class RecordingStore[T](Store[T]):
        def __init__(self, *args, **kwargs) -> None:
            calls.append(inspect.signature(Store).bind(*args, **kwargs))
            super().__init__(*args, **kwargs)

    # Home Assistant 2026.11 keeps both options in its own file writer, so
    # the options passed to the store are checked instead.
    with patch(f"{ConnectionInventory.__module__}.Store", RecordingStore):
        ConnectionInventory(hass, entry)
    assert len(calls) == 1
    assert calls[0].arguments["private"] is True
    assert calls[0].arguments["atomic_writes"] is True


async def test_newer_inventory_file_does_not_stop_the_entry(hass, hass_storage):
    """STORE-3/TEST-2: a real future-version envelope through HA's Store."""
    entry, _, _ = entry_for(hass)
    key = f"{DOMAIN}.inventory.{entry.entry_id}"
    hass_storage[key] = {
        "version": 2,
        "minor_version": 1,
        "key": key,
        "data": {"owner_id": entry.entry_id, "entities": []},
    }
    inventory = ConnectionInventory(hass, entry)
    await inventory.load()
    assert not inventory.healthy and not inventory.entities
    await inventory.save()
    assert hass_storage[key]["version"] == 2  # the newer file stays untouched


@pytest.mark.parametrize(
    "error",
    [
        HomeAssistantError("Error while loading: [Errno 5] I/O error"),
        UnsupportedStorageVersionError("inventory", 2, 1),
        KeyError("version"),
        TypeError("list indices must be integers"),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_store_errors_leave_the_entry_running_on_the_registry(
    hass, enable_custom_integrations, error, caplog
):
    """STORE-3: the errors HA's Store really raises end in the fallback."""
    from homeassistant.config_entries import ConfigEntryState

    from custom_components.bosch_buderus_heating.managed_entry import (
        async_setup_managed_entry,
    )
    from tests.test_connection_runtime import Harness

    entry, _, _ = entry_for(hass)
    harness = Harness()
    original = Store.async_load

    async def load(self):
        if self.key.startswith(f"{DOMAIN}.inventory."):
            raise error
        return await original(self)

    async def setup(hass, entry):
        return await async_setup_managed_entry(
            hass,
            entry,
            {"singlekey_cloud": harness.factory, "k40_local": harness.factory},
        )

    with (
        patch.object(Store, "async_load", load),
        patch(
            "custom_components.bosch_buderus_heating.async_setup_managed_entry",
            side_effect=setup,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await settle(hass)
        assert entry.state is ConfigEntryState.LOADED
        assert not entry.runtime_data.inventory.healthy
        assert "entity inventory could not be loaded" in caplog.text
        assert await hass.config_entries.async_unload(entry.entry_id)
