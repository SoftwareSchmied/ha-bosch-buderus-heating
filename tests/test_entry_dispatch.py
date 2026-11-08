"""Exercise active v4 dispatch through HA setup, reload and unload."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntryState,
    OperationNotAllowed,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import async_migrate_entry
from custom_components.bosch_buderus_heating.auth_lifecycle import entry_auth
from custom_components.bosch_buderus_heating.config_flow import BoschBuderusConfigFlow
from custom_components.bosch_buderus_heating.connection_runtime import ConnectionPhase
from custom_components.bosch_buderus_heating.const import DOMAIN, PLATFORMS
from custom_components.bosch_buderus_heating.managed_entry import (
    ManagedEntry,
    async_setup_managed_entry,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    PointTClient,
    RateLimited,
    RefreshTokenRejected,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.storage_migration import (
    migrate_entry_storage,
)
from tests.test_configuration import legacy
from tests.test_connection_runtime import Harness
from tests.test_connection_runtime import entry_for as managed_entry_for
from tests.test_managed_platforms import NUMBER, available, registered
from tests.test_managed_pointt import Backend, eventually
from tests.test_pointt_transport_and_client import serve
from tests.test_storage_migration import entry_for, metadata, stored

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
ROOT = "custom_components.bosch_buderus_heating"


@pytest.fixture
def active_version():
    # Exercise the advertised product version, never a test-only override.
    assert BoschBuderusConfigFlow.VERSION == 4


@asynccontextmanager
async def loopback(backend):
    async with serve(backend.routes) as url:
        with (
            patch(
                ROOT + ".managed_cloud.PointTClient",
                side_effect=lambda *a, **kw: PointTClient(*a, **kw, base_url=url),
            ),
            patch(
                ROOT + ".managed_pointt.PointTClient",
                side_effect=lambda *a, **kw: PointTClient(*a, **kw, base_url=url),
            ),
        ):
            yield


def legacy_entry(hass):
    return entry_for(hass, data={**legacy(["000123"], []), "expires_at": 10**12})


async def test_ha_setup_migrates_then_reloads_same_registry(hass, active_version):
    entry = legacy_entry(hass)
    original_metadata = metadata(entry)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "000123")}
    )
    item = er.async_get(hass).async_get_or_create(
        "number", DOMAIN, NUMBER, config_entry=entry, device_id=device.id
    )
    er.async_get(hass).async_update_entity(item.entity_id, name="My room temperature")
    backend = Backend()
    async with loopback(backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED and entry.version == 4
        owner = entry.runtime_data
        assert isinstance(owner, ManagedEntry)
        await eventually(lambda: available(owner, "number", NUMBER))
        assert len(owner.platforms._platforms) == len(PLATFORMS)
        original = registered(hass, "number", NUMBER)
        assert original.entity_id == item.entity_id
        assert (
            original.device_id == device.id and original.name == "My room temperature"
        )
        migrated = stored(entry)
        assert await hass.config_entries.async_reload(entry.entry_id)
        replacement = entry.runtime_data
        assert replacement is not owner and owner._closed
        await eventually(lambda: available(replacement, "number", NUMBER))
        assert registered(hass, "number", NUMBER) == original
        assert stored(entry) == migrated and metadata(entry) == original_metadata
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert replacement._closed and not replacement.connections._tasks
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert registered(hass, "number", NUMBER) == original
        assert not any(call[0] == "PUT" for call in backend.calls)


async def test_slow_initial_cloud_does_not_block_ha_setup(hass, active_version):
    entry = legacy_entry(hass)
    backend = Backend()
    backend.gates["gateways"] = asyncio.Event()
    async with loopback(backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        owner = entry.runtime_data
        assert entry.state is ConfigEntryState.LOADED
        await eventually(lambda: "gateways" in backend.entered)
        assert not available(owner, "number", NUMBER)
        backend.gates["gateways"].set()
        await eventually(lambda: available(owner, "number", NUMBER))
        assert entry.runtime_data is owner
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_failure_keeps_owner_and_reports_ha_restart_required(
    hass, active_version
):
    entry = legacy_entry(hass)
    async with loopback(Backend()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        owner = entry.runtime_data
        await eventually(lambda: available(owner, "number", NUMBER))
        with patch.object(
            hass.config_entries, "async_unload_platforms", return_value=False
        ):
            assert not await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.FAILED_UNLOAD
        assert not owner._closed and not owner.connections._closed
        with pytest.raises(OperationNotAllowed):
            await hass.config_entries.async_reload(entry.entry_id)
        # Clean up this isolated runtime; HA itself requires a restart here.
        assert await owner.unload()
        assert owner._closed and not owner.connections._tasks
        assert not entry.update_listeners


@pytest.mark.parametrize("phase", ["inventory", "platforms"])
async def test_setup_failure_cleans_up_and_can_retry(hass, active_version, phase):
    entry = legacy_entry(hass)
    async with loopback(Backend()):
        forward = hass.config_entries.async_forward_entry_setups

        async def partial_platforms(*args):
            await forward(*args)
            raise RuntimeError("Simulated setup failure")

        target = (
            ROOT + ".connection_inventory.ConnectionInventory.load"
            if phase == "inventory"
            else None
        )
        failure = (
            patch(target, side_effect=RuntimeError("Simulated storage failure"))
            if target
            else patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                side_effect=partial_platforms,
            )
        )
        with failure:
            assert not await hass.config_entries.async_setup(entry.entry_id)
        owner = entry.runtime_data
        assert entry.state is ConfigEntryState.SETUP_ERROR and entry.version == 4
        assert owner._closed and not owner.connections._tasks
        assert not entry.update_listeners
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data is not owner
        await eventually(lambda: available(entry.runtime_data, "number", NUMBER))
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_scoped_auth_failure_during_setup_keeps_peer_and_options(
    hass, active_version
):
    entry, cloud, _ = managed_entry_for(hass, second_cloud=True)
    harness = Harness()
    harness.errors[cloud] = RefreshTokenRejected("synthetic rejected token")

    async def setup(hass, entry):
        return await async_setup_managed_entry(
            hass, entry, {"singlekey_cloud": harness.factory}
        )

    with patch(ROOT + ".async_setup_managed_entry", side_effect=setup):
        assert await hass.config_entries.async_setup(entry.entry_id)
        owner = entry.runtime_data
        await eventually(
            lambda: (
                owner.connections.connection_phase("second") is ConnectionPhase.READY
            )
        )
        await eventually(
            lambda: bool(list(entry.async_get_active_flows(hass, {SOURCE_REAUTH})))
        )
        flows = list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
        assert len(flows) == 1 and flows[0]["context"]["connection_id"] == cloud
        assert entry.state is ConfigEntryState.LOADED
        options = await hass.config_entries.options.async_init(entry.entry_id)
        assert "connections" in options["menu_options"]
        hass.config_entries.options.async_abort(options["flow_id"])
        hass.config_entries.flow.async_abort(flows[0]["flow_id"])
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert owner._closed and not owner.connections._tasks


@pytest.mark.parametrize(
    "bad_version", ["invalid_data", "future_minor", "future_major"]
)
async def test_migration_failure_is_reported_without_starting_transport(
    hass, active_version, bad_version
):
    entry = legacy_entry(hass)
    if bad_version == "invalid_data":
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "gateway_ids": None}
        )
    elif bad_version == "future_minor":
        hass.config_entries.async_update_entry(entry, minor_version=2)
    else:
        hass.config_entries.async_update_entry(entry, version=5)
    before = stored(entry)
    with patch(ROOT + ".async_setup_managed_entry") as setup:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        setup.assert_not_called()
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert stored(entry) == before
    with pytest.raises(OperationNotAllowed):
        await hass.config_entries.async_reload(entry.entry_id)
    # A new HA startup rehydrates a stopped entry. Reproduce that boundary here,
    # retaining the full data and metadata, rather than overriding a live state.
    from tests.ha_test_helpers import MockConfigEntry

    await hass.config_entries.async_remove(entry.entry_id)
    restored = MockConfigEntry(
        entry_id=entry.entry_id,
        domain=DOMAIN,
        version=3,
        title=entry.title,
        unique_id=entry.unique_id,
        data={**legacy(["000123"], []), "expires_at": 10**12},
        options=dict(entry.options),
    )
    restored.add_to_hass(hass)
    async with loopback(Backend()):
        assert await hass.config_entries.async_setup(restored.entry_id)
        await eventually(lambda: available(restored.runtime_data, "number", NUMBER))
        assert await hass.config_entries.async_unload(restored.entry_id)


async def test_a_current_entry_only_moves_to_the_advertised_minor_version(
    hass, active_version
):
    entry = legacy_entry(hass)
    migrate_entry_storage(hass, entry)
    assert (entry.version, entry.minor_version) == (4, 1)
    before = stored(entry)
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == BoschBuderusConfigFlow.MINOR_VERSION
    after = stored(entry)
    # Stored data and options stay; no local_values appear on an old entry.
    assert (after["data"], after["options"]) == (before["data"], before["options"])
    assert all(
        "local_values" not in item for item in after["options"]["targets"].values()
    )
    with patch.object(hass.config_entries, "async_update_entry") as update:
        assert await async_migrate_entry(hass, entry)
        update.assert_not_called()


async def test_a_legacy_entry_ends_at_the_advertised_minor_version(
    hass, active_version
):
    entry = legacy_entry(hass)
    assert await async_migrate_entry(hass, entry)
    assert (entry.version, entry.minor_version) == (
        4,
        BoschBuderusConfigFlow.MINOR_VERSION,
    )


@pytest.mark.parametrize("problem", ["unreadable", "newer_minor"])
async def test_an_unreadable_current_entry_is_a_setup_error(
    hass, active_version, problem
):
    from custom_components.bosch_buderus_heating.cloud_configuration import (
        check_new_cloud,
        cloud_snapshots,
    )

    entry = legacy_entry(hass)
    migrate_entry_storage(hass, entry)
    if problem == "unreadable":
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "targets": "broken"}
        )
    else:
        hass.config_entries.async_update_entry(
            entry, minor_version=BoschBuderusConfigFlow.MINOR_VERSION + 1
        )
    before = stored(entry)
    if problem == "unreadable":
        # A new cloud setup is not blocked by "configuration changed".
        assert entry.entry_id not in {s.owner_id for s in cloud_snapshots(hass)}
        check_new_cloud(hass, "new-flow")
    else:
        # A newer format is never interpreted; its ownership is unknown.
        with pytest.raises(ValueError, match="unsupported_version"):
            check_new_cloud(hass, "new-flow")
    with patch(ROOT + ".async_setup_managed_entry") as setup:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        setup.assert_not_called()
    assert stored(entry) == before
    if problem == "unreadable":
        # Not a migration error: a clear setup error, retried at every start.
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert entry.error_reason_translation_key == "invalid_stored_configuration"
    else:
        assert entry.state is ConfigEntryState.MIGRATION_ERROR


@pytest.mark.parametrize("initial_version", [1, 2, 3, 4])
async def test_dispatch_respects_preview_migration_and_repeat(
    hass, active_version, initial_version
):
    entry = legacy_entry(hass)
    if initial_version == 4:
        migrate_entry_storage(hass, entry)
    else:
        hass.config_entries.async_update_entry(entry, version=initial_version)
    original = metadata(entry)
    assert await async_migrate_entry(hass, entry)
    after = stored(entry)
    assert entry.version == 4 and metadata(entry) == original
    with patch.object(hass.config_entries, "async_update_entry") as update:
        assert await async_migrate_entry(hass, entry)
        update.assert_not_called()
    assert stored(entry) == after


@pytest.mark.parametrize(
    "action",
    [
        "normal",
        "cancel_original",
        "cancel_migration",
        "timeout",
        "replace_login",
        "remove_entry",
    ],
)
async def test_migration_waits_for_existing_rotation_without_cancelling_it(
    hass, active_version, action
):
    entry = entry_for(hass, data=legacy(["000123"], []))
    started, release = asyncio.Event(), asyncio.Event()
    rotated = AuthTokens("synthetic-new", "synthetic-rotated", 10**12)

    async def refresh(_):
        started.set()
        await release.wait()
        return rotated

    oauth = AsyncMock()
    oauth.refresh.side_effect = refresh
    manager, pause = entry_auth(hass, entry, oauth)
    waiter = asyncio.create_task(manager.get_access_token())
    await started.wait()
    before = stored(entry)
    if action == "cancel_original":
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    timeout = 0.01 if action == "timeout" else 30
    with patch(ROOT + ".auth_lifecycle.REFRESH_MIGRATION_TIMEOUT_SECONDS", timeout):
        migration = asyncio.create_task(async_migrate_entry(hass, entry))
        await asyncio.sleep(0)
        assert stored(entry) == before and not migration.done()
        if action == "cancel_migration":
            migration.cancel()
            with pytest.raises(asyncio.CancelledError):
                await migration
        if action == "timeout":
            assert not await migration
        if action in {"cancel_migration", "timeout"}:
            assert manager.refresh_in_progress and stored(entry) == before
        replacement = None
        if action == "replace_login":
            replacement = {
                **entry.data,
                "access_token": "replacement",
                "refresh_token": "replacement-refresh",
                "cloud_auth_epoch": 1,
            }
            hass.config_entries.async_update_entry(entry, data=replacement)
        if action == "remove_entry":
            await hass.config_entries.async_remove(entry.entry_id)
        pause.activate(300)
        release.set()
        if action != "cancel_original":
            await waiter
        if action in {"cancel_migration", "timeout"}:
            # Only the first call exercises the short limit; the repeat call
            # must not race against it under load.
            with patch(ROOT + ".auth_lifecycle.REFRESH_MIGRATION_TIMEOUT_SECONDS", 30):
                assert await async_migrate_entry(hass, entry)
        else:
            assert await migration == (action != "remove_entry")
    oauth.refresh.assert_awaited_once()
    if action == "remove_entry":
        assert hass.config_entries.async_get_entry(entry.entry_id) is None
        assert entry.version == 3
        return
    cloud = next(iter(entry.data["connections"]))
    current, same_pause = entry_auth(hass, entry, oauth, connection_id=cloud)
    assert current.tokens.refresh_token == (
        replacement["refresh_token"] if replacement else rotated.refresh_token
    )
    assert same_pause is pause
    with pytest.raises(RateLimited):
        same_pause.raise_if_active()


@pytest.mark.parametrize(
    "error",
    [
        RefreshTokenRejected("synthetic secret"),
        ServiceUnavailable("synthetic secret"),
        RuntimeError("synthetic secret"),
    ],
)
async def test_refresh_failure_preserves_storage_and_safe_logs(
    hass, active_version, caplog, error
):
    entry = entry_for(hass, data=legacy(["000123"], []))
    oauth = AsyncMock()
    oauth.refresh.side_effect = error
    manager, _ = entry_auth(hass, entry, oauth)
    with pytest.raises(type(error)):
        await manager.get_access_token()
    before = stored(entry)
    result = await async_migrate_entry(hass, entry)
    if isinstance(error, RuntimeError):
        assert not result and stored(entry) == before
    else:
        assert result and entry.version == 4
    assert "synthetic secret" not in caplog.text


async def test_other_entry_rotation_does_not_delay_migration(hass, active_version):
    entry = legacy_entry(hass)
    from tests.ha_test_helpers import MockConfigEntry

    peer = MockConfigEntry(
        domain=DOMAIN, version=3, unique_id="peer-rotation", data=legacy()
    )
    peer.add_to_hass(hass)
    release = asyncio.Event()

    async def refresh(_):
        await release.wait()
        return AuthTokens("peer-new", "peer-refresh", 10**12)

    oauth = AsyncMock()
    oauth.refresh.side_effect = refresh
    manager, _ = entry_auth(hass, peer, oauth)
    waiter = asyncio.create_task(manager.get_access_token())
    await asyncio.sleep(0)
    assert manager.refresh_in_progress
    try:
        assert await async_migrate_entry(hass, entry)
        assert entry.version == 4 and peer.version == 3
    finally:
        release.set()
        await waiter


async def test_unload_without_platforms_is_idempotent(hass):
    entry = legacy_entry(hass)
    migrate_entry_storage(hass, entry)
    owner = ManagedEntry(hass, entry, {})
    await owner.start()
    assert await owner.unload()
    assert await owner.unload()
    assert owner._closed and not entry.update_listeners


async def test_failed_token_persistence_prevents_migration(
    hass, active_version, caplog
):
    entry = entry_for(hass, data=legacy(["000123"], []))
    before = stored(entry)
    rotated = AuthTokens("rotated-access", "rotated-refresh", 10**12)
    oauth = AsyncMock()
    oauth.refresh.return_value = rotated
    manager, _ = entry_auth(hass, entry, oauth)
    with (
        patch.object(
            hass.config_entries,
            "async_update_entry",
            side_effect=OSError("private storage details"),
        ),
        pytest.raises(OSError),
    ):
        await manager.get_access_token()
    assert manager.tokens == rotated and stored(entry) == before
    assert not await async_migrate_entry(hass, entry)
    assert stored(entry) == before
    assert "private storage details" not in caplog.text


async def test_late_refresh_error_after_all_waiters_cancel_is_retrieved(
    hass, active_version, caplog
):
    entry = entry_for(hass, data=legacy(["000123"], []))
    before = stored(entry)
    release = asyncio.Event()
    started = asyncio.Event()

    async def refresh(_):
        started.set()
        await release.wait()
        raise RefreshTokenRejected("private response details")

    oauth = AsyncMock()
    oauth.refresh.side_effect = refresh
    manager, _ = entry_auth(hass, entry, oauth)
    waiter = asyncio.create_task(manager.get_access_token())
    await started.wait()
    migration = asyncio.create_task(async_migrate_entry(hass, entry))
    await asyncio.sleep(0)
    for task in (waiter, migration):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert manager.refresh_in_progress
    release.set()
    await eventually(lambda: not manager.refresh_in_progress)
    await hass.async_block_till_done()
    assert stored(entry) == before
    assert "private response details" not in caplog.text
    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()
    oauth.refresh.assert_awaited_once()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("invalid", ["selection", "minor"])
async def test_invalid_preview_keeps_version_devices_and_entities(
    hass, version, invalid
):
    entry = legacy_entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        version=version,
        minor_version=2 if invalid == "minor" else 1,
        data={**entry.data, "gateway_ids": None}
        if invalid == "selection"
        else dict(entry.data),
    )
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "000123", "heating_circuit", "hc2")},
    )
    item = er.async_get(hass).async_get_or_create(
        "number", DOMAIN, NUMBER, config_entry=entry, device_id=device.id
    )
    before = stored(entry)
    assert not await async_migrate_entry(hass, entry)
    assert stored(entry) == before
    assert dr.async_get(hass).async_get(device.id) == device
    assert er.async_get(hass).async_get(item.entity_id) == item


@pytest.mark.parametrize("disabled", [False, True])
async def test_existing_two_cloud_entries_migrate_without_new_login(hass, disabled):
    from homeassistant.config_entries import ConfigEntryDisabler

    from tests.ha_test_helpers import MockConfigEntry

    first = legacy_entry(hass)
    peer = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        unique_id="second-existing-owner",
        data={**legacy(["gateway-b"], []), "expires_at": 10**12},
        disabled_by=ConfigEntryDisabler.USER if disabled else None,
    )
    peer.add_to_hass(hass)
    before = dict(peer.data)
    async with loopback(Backend()):
        assert await hass.config_entries.async_setup(first.entry_id)
        assert first.version == 4
        if disabled:
            assert not await hass.config_entries.async_setup(peer.entry_id)
            assert peer.version == 3 and peer.data == before
            await hass.config_entries.async_set_disabled_by(peer.entry_id, None)
        # Initial domain setup also starts other enabled entries.
        await eventually(lambda: peer.version == 4 and hasattr(peer, "runtime_data"))
        await eventually(lambda: available(first.runtime_data, "number", NUMBER))
        await eventually(
            lambda: available(
                peer.runtime_data,
                "number",
                "gateway-b:heatingCircuits:hc2:manualRoomSetpoint:control",
            )
        )
        assert len(hass.config_entries.async_entries(DOMAIN)) == 2
        for entry in (first, peer):
            assert await hass.config_entries.async_unload(entry.entry_id)
