"""Rehearse the active storage transition with HA registries and local stores."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import persistent_notification as pn
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import async_migrate_entry
from custom_components.bosch_buderus_heating.auth_lifecycle import entry_auth
from custom_components.bosch_buderus_heating.bacon.models import device_key
from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.configuration_migration import (
    convert_legacy_configuration,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry
from custom_components.bosch_buderus_heating.notifications import (
    FaultNotifications,
    notification_id,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    Gateway,
    RateLimited,
)
from custom_components.bosch_buderus_heating.storage_migration import (
    migrate_entry_storage,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_configuration import SUBJECT, legacy
from tests.test_notifications import A, _apply


def entry_for(hass, **kwargs):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        unique_id="existing-entry-fingerprint",
        title="My installation",
        data=kwargs.pop("data", legacy()),
        options=kwargs.pop(
            "options",
            {
                "polling_profile": "cloud_friendly",
                "fault_notifications": {"enabled": False},
                "future_option": {"kept": True},
            },
        ),
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


def stored(entry):
    return deepcopy(
        {
            "data": dict(entry.data),
            "options": dict(entry.options),
            "version": entry.version,
            "minor_version": entry.minor_version,
        }
    )


def metadata(entry):
    return (
        entry.entry_id,
        entry.unique_id,
        entry.title,
        entry.source,
        entry.disabled_by,
        entry.pref_disable_new_entities,
        entry.pref_disable_polling,
    )


@pytest.mark.parametrize(
    "gateways,devices",
    [(["000123"], []), (["000123", "other"], []), ([], ["air"]), (["000123"], ["air"])],
)
async def test_one_update_preserves_metadata_registries_and_options(
    hass, gateways, devices
):
    entry = entry_for(
        hass,
        data=legacy(gateways, devices),
        pref_disable_new_entities=True,
        pref_disable_polling=True,
    )
    registry, devices_registry = er.async_get(hass), dr.async_get(hass)
    identities = gateways + [device_key(SUBJECT, item) for item in devices]
    for index, identity in enumerate(identities):
        device = devices_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, identity)}
        )
        devices_registry.async_update_device(device.id, name_by_user="My device")
        item = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            identity + ":reading",
            config_entry=entry,
            device_id=device.id,
        )
        registry.async_update_entity(
            item.entity_id,
            name="" if index == 0 else "My reading",
            icon="mdi:thermometer",
            disabled_by=er.RegistryEntryDisabler.USER,
            hidden_by=er.RegistryEntryHider.USER,
            aliases={"My alias"},
        )
        registry.async_update_entity_options(
            item.entity_id, "sensor", {"display_precision": 1}
        )
    before_entities = dict(registry.entities)
    before_devices = tuple(devices_registry.devices)
    before, original = metadata(entry), stored(entry)
    with patch.object(
        hass.config_entries,
        "async_update_entry",
        wraps=hass.config_entries.async_update_entry,
    ) as update:
        migrate_entry_storage(hass, entry)
        assert update.call_count == 1
        assert set(update.call_args.kwargs) == {
            "data",
            "options",
            "version",
            "minor_version",
        }
        migrate_entry_storage(hass, entry)
        assert update.call_count == 1
    assert metadata(entry) == before
    assert registry.entities == before_entities
    assert tuple(devices_registry.devices) == before_devices
    cloud = next(iter(entry.data["connections"]))
    assert (
        EntryConfiguration(entry, cloud).tokens()
        == EntryConfiguration(
            MockConfigEntry(domain=DOMAIN, version=3, data=original["data"])
        ).tokens()
    )
    assert (
        entry.options["fault_notifications"]
        == original["options"]["fault_notifications"]
    )
    assert entry.options["future_option"] == original["options"]["future_option"]
    assert entry.options["connections"][cloud]["polling_profile"] == "cloud_friendly"


@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
async def test_restart_from_either_complete_stored_version_is_repeatable(hass, phase):
    entry = entry_for(hass)
    proposal = convert_legacy_configuration(entry.entry_id, entry.data, entry.options)
    saved = stored(entry)
    if phase == "after_commit":
        saved.update(data=proposal.to_data(), options=proposal.to_options(), version=4)
    # Disk reload supplies one complete old or new record, never mixed fields.
    hass.config_entries.async_update_entry(entry, **json.loads(json.dumps(saved)))
    migrate_entry_storage(hass, entry)
    result = stored(entry)
    migrate_entry_storage(hass, entry)
    assert stored(entry) == result
    assert (
        result["data"] == proposal.to_data()
        and result["options"] == proposal.to_options()
    )


@pytest.mark.parametrize("exception", [OSError, asyncio.CancelledError])
async def test_interruption_before_update_keeps_legacy_data_and_can_retry(
    hass, exception
):
    entry = entry_for(hass)
    before = stored(entry)
    with (
        patch.object(hass.config_entries, "async_update_entry", side_effect=exception),
        pytest.raises(exception),
    ):
        migrate_entry_storage(hass, entry)
    assert stored(entry) == before
    migrate_entry_storage(hass, entry)
    assert entry.version == 4


@pytest.mark.parametrize(
    "change", ["duplicate", "brand", "credential", "empty", "minor", "future"]
)
async def test_invalid_proposal_never_changes_stored_data(hass, change):
    entry = entry_for(hass)
    data = dict(entry.data)
    if change == "duplicate":
        data["gateway_ids"] *= 2
    elif change == "brand":
        data["brand"] = "unsupported"
    elif change == "credential":
        data["refresh_token"] = []
    elif change == "empty":
        data["gateway_ids"] = []
    hass.config_entries.async_update_entry(
        entry,
        data=data,
        minor_version=2 if change == "minor" else 1,
        version=5 if change == "future" else 3,
    )
    before = stored(entry)
    with patch.object(hass.config_entries, "async_update_entry") as update:
        with pytest.raises(ConfigurationError):
            migrate_entry_storage(hass, entry)
        update.assert_not_called()
    assert stored(entry) == before


@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.LOADED,
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.FAILED_UNLOAD,
        ConfigEntryState.UNLOAD_IN_PROGRESS,
    ],
)
async def test_runtime_must_be_stopped_before_storage_transition(hass, state):
    entry = entry_for(hass, state=state)
    before = stored(entry)
    with pytest.raises(ConfigurationError, match="entry_not_stopped"):
        migrate_entry_storage(hass, entry)
    assert stored(entry) == before


async def test_removed_entry_cannot_be_migrated(hass):
    entry = MockConfigEntry(domain=DOMAIN, version=3, data=legacy())
    with pytest.raises(ConfigurationError, match="entry_not_current"):
        migrate_entry_storage(hass, entry)


@pytest.mark.parametrize("change", ["references", "version"])
async def test_existing_v4_is_validated_without_repairing_or_rewriting(hass, change):
    entry = entry_for(hass)
    migrate_entry_storage(hass, entry)
    data = deepcopy(dict(entry.data))
    if change == "references":
        data["targets"] = {}
    hass.config_entries.async_update_entry(
        entry, data=data, minor_version=3 if change == "version" else 1
    )
    before = stored(entry)
    with patch.object(hass.config_entries, "async_update_entry") as update:
        with pytest.raises(ConfigurationError):
            migrate_entry_storage(hass, entry)
        update.assert_not_called()
    assert stored(entry) == before


@pytest.mark.parametrize("version", [1, 2])
async def test_preview_registry_migration_finishes_before_storage_transition(
    hass, version
):
    entry = entry_for(hass)
    hass.config_entries.async_update_entry(entry, version=version)
    devices = dr.async_get(hass)
    preview = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "000123", "heating_circuit", "hc1")},
    )
    with pytest.raises(ConfigurationError, match="unsupported_legacy_version"):
        migrate_entry_storage(hass, entry)
    assert await async_migrate_entry(hass, entry)
    intermediate = devices.async_get(preview.id)
    assert entry.version == 4
    migrate_entry_storage(hass, entry)
    assert devices.async_get(preview.id) == intermediate and entry.version == 4


async def test_active_hook_converts_once_and_accepts_existing_v4(hass):
    entry = entry_for(hass)
    before = stored(entry)
    assert await async_migrate_entry(hass, entry)
    assert stored(entry) != before and entry.version == 4
    converted = stored(entry)
    assert await async_migrate_entry(hass, entry)
    assert stored(entry) == converted


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_pending_rotation_finishes_in_old_storage_before_migration(
    hass, cancel_waiter
):
    entry = entry_for(hass)
    started, release = asyncio.Event(), asyncio.Event()
    rotated = AuthTokens("synthetic-new-access", "synthetic-new-refresh", 10**12)

    async def refresh(_):
        started.set()
        await release.wait()
        return rotated

    oauth = AsyncMock()
    oauth.refresh.side_effect = refresh
    tokens, pause = entry_auth(hass, entry, oauth)
    waiter = asyncio.create_task(tokens.get_access_token())
    await started.wait()
    if cancel_waiter:
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    before = stored(entry)
    with pytest.raises(ConfigurationError, match="authentication_in_progress"):
        migrate_entry_storage(hass, entry)
    assert stored(entry) == before
    peer = MockConfigEntry(domain=DOMAIN, unique_id="peer", version=3, data=legacy())
    peer.add_to_hass(hass)
    migrate_entry_storage(hass, peer)
    assert peer.version == 4
    pause.activate(300)
    release.set()
    if not cancel_waiter:
        assert await waiter == rotated.access_token
    await hass.async_block_till_done()
    assert not tokens.refresh_in_progress
    assert EntryConfiguration(entry).tokens() == rotated
    migrate_entry_storage(hass, entry)
    cloud = next(iter(entry.data["connections"]))
    current, same_pause = entry_auth(hass, entry, oauth, connection_id=cloud)
    assert current is not tokens and current.tokens == rotated
    assert same_pause is pause
    with pytest.raises(RateLimited):
        same_pause.raise_if_active()
    oauth.refresh.assert_awaited_once()


@pytest.mark.parametrize("dismissed", [False, True])
async def test_faults_and_dismissals_survive_transition_and_backup_restore(
    hass, dismissed
):
    entry = entry_for(hass, options={})
    backup = stored(entry)
    original = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("000123"), entry
    )
    _apply(original.faults, A)
    manager = FaultNotifications(hass, entry, original)
    current = {}

    @callback  # deliver notification changes in order on every HA release
    def capture(kind, notifications):
        for key, value in notifications.items():
            if kind is pn.UpdateType.REMOVED:
                current.pop(key, None)
            else:
                current[key] = value

    remove = pn.async_register_callback(hass, capture)
    key = notification_id(entry.entry_id, "000123")
    await manager.async_start()
    await hass.async_block_till_done()
    assert key in current
    if dismissed:
        pn.async_dismiss(hass, key)
        await hass.async_block_till_done()
    await manager.async_close()
    await original.async_shutdown()
    migrate_entry_storage(hass, entry)
    owner = ManagedEntry(hass, entry, {})
    try:
        await owner.start()
        await hass.async_block_till_done()
        # Background jobs are independent of transport, including with no driver.
        await asyncio.gather(*tuple(owner.services._jobs.values()))
        await hass.async_block_till_done()
        assert (key not in current) == dismissed
        if not dismissed:
            assert "not yet been confirmed" in current[key]["message"]
    finally:
        await owner.close()
    # Rehearse restoring the complete saved v3 entry, never just its version.
    hass.config_entries.async_update_entry(entry, **backup)
    assert stored(entry) == backup
    # A real software rollback uses the previous code as well as this backup.
    # The separate isolated rollback rehearsal tests that prior release.
    restored = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("000123"), entry
    )
    restored_manager = FaultNotifications(hass, entry, restored)
    try:
        await restored.faults.async_load()
        await restored_manager.async_start()
        await hass.async_block_till_done()
        assert (key not in current) == dismissed
    finally:
        await restored_manager.async_close()
        await restored.async_shutdown()
        remove()
