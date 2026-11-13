"""Exercise the managed owner and its dialogs through Home Assistant APIs."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntryState,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import (
    async_remove_entry,
    async_unload_entry,
)
from custom_components.bosch_buderus_heating.auth_lifecycle import connection_backoff
from custom_components.bosch_buderus_heating.bacon.models import BaconError
from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.connection_inventory import (
    ConnectionInventory,
    RetainedEntity,
)
from custom_components.bosch_buderus_heating.connection_runtime import (
    ConnectionPhase,
    SourceFailure,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.managed_entry import (
    async_setup_managed_entry,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    Brand,
    Gateway,
    RateLimited,
    RefreshTokenRejected,
    ServiceUnavailable,
)
from tests.helpers.options_menu import connection_choice
from tests.test_config_flow import _redirect_from_result
from tests.test_connection_runtime import MODULE, Harness, build, entry_for, settle

FLOW = "custom_components.bosch_buderus_heating.config_flow"


async def choose_connection(hass, entry, connection):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "connections" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connections"}
    )
    assert result["step_id"] == "connections"
    return await hass.config_entries.options.async_configure(
        result["flow_id"], connection_choice(hass, result, connection)
    )


async def open_reauth(hass, entry, connection):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "connection_id": connection,
        },
        data=dict(entry.data),
    )
    assert result["step_id"] == "reauth_confirm"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {})


@pytest.mark.parametrize("action", ["disable", "targets", "remove", "cancel_remove"])
async def test_connection_options_work_offline_without_global_reload(
    hass, enable_custom_integrations, action
):
    entry, cloud, _ = entry_for(hass)
    before = deepcopy(dict(entry.data))
    result = await choose_connection(hass, entry, cloud)
    assert "connection_reauthenticate" in result["menu_options"]
    step = {"disable": "connection_enabled", "targets": "connection_targets"}.get(
        action, "connection_remove"
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    submission = {
        "disable": {"enabled": False},
        "targets": {"targets": []},
        "remove": {"confirm": True},
        "cancel_remove": {"confirm": False},
    }[action]
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], submission
        )
        await settle(hass)
        reload.assert_not_awaited()
    assert entry.data["targets"] == before["targets"]
    assert entry.data["connections"]["local"] == before["connections"]["local"]
    if action == "cancel_remove":
        assert result["type"] is FlowResultType.MENU
        assert entry.data == before
    else:
        assert result["type"] is FlowResultType.MENU
        if action == "remove":
            assert cloud not in entry.data["connections"]
        elif action == "disable":
            assert entry.options["connections"][cloud]["enabled"] is False
        else:
            assert all(
                not entry.options["bindings"][key]["enabled"]
                for key, binding in entry.data["bindings"].items()
                if binding["connection_id"] == cloud
            )


@pytest.mark.parametrize(
    "step",
    [
        "connection_enabled",
        "connection_targets",
        "connection_remove",
        "connection_reauthenticate",
    ],
)
async def test_open_connection_dialog_cannot_resurrect_removed_access(
    hass, enable_custom_integrations, step
):
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud)
    manager = ConfigurationManagement(hass, entry)
    manager.remove(manager.edit(cloud))
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    if step == "connection_remove":
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm": True}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "configuration_changed"
    assert cloud not in entry.data["connections"]


async def test_local_connection_does_not_offer_cloud_login(
    hass, enable_custom_integrations
):
    entry, _, _ = entry_for(hass)
    result = await choose_connection(hass, entry, "local")
    assert "connection_reauthenticate" not in result["menu_options"]


@pytest.mark.parametrize(
    "connection,reason",
    [
        (None, "connection_required"),
        ("local", "connection_settings_required"),
        ("missing", "configuration_changed"),
    ],
)
async def test_managed_reauth_requires_an_existing_explicit_cloud_connection(
    hass, enable_custom_integrations, connection, reason
):
    entry, _, _ = entry_for(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "connection_id": connection,
        },
        data=dict(entry.data),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


@pytest.mark.parametrize(
    "mode",
    [
        "all",
        "missing",
        "none",
        "wrong_account",
        "pointt_service",
        "pointt_rate_limited",
        "bacon_service",
        "bacon_rate_limited",
        "bacon_auth",
    ],
)
async def test_cloud_reauth_dialog_confirms_the_account(
    hass, enable_custom_integrations, mode
):
    entry, cloud, _ = entry_for(hass)
    before = deepcopy(dict(entry.data))
    result = await open_reauth(hass, entry, cloud)
    tokens = AuthTokens("new-access", "new-refresh", 10**12, scope=("bacon",))
    pointt = AsyncMock(return_value=(Gateway("000123"), Gateway("gateway-b")))
    bacon = AsyncMock(
        return_value=(
            ("softener",),
            EntryConfiguration(entry, cloud).cloud_data()["bacon_account"],
        )
    )
    if mode == "missing":
        pointt.return_value = (Gateway("000123"),)
    if mode == "none":
        pointt.return_value = ()
        bacon.return_value = ((), bacon.return_value[1])
    if mode == "wrong_account":
        bacon.return_value = (("softener",), "another-account")
    if mode == "pointt_service":
        pointt.side_effect = ServiceUnavailable()
    if mode == "pointt_rate_limited":
        pointt.side_effect = RateLimited(120)
    if mode.startswith("bacon_"):
        bacon.side_effect = BaconError(
            {"bacon_auth": "authentication", "bacon_rate_limited": "rate_limited"}.get(
                mode, "service_unavailable"
            )
        )
    with (
        patch(FLOW + ".OAuthClient.exchange_code", AsyncMock(return_value=tokens)),
        patch(MODULE + ".PointTClient.get_gateways", pointt),
        patch(MODULE + ".discover", bacon),
        patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
        )
        await settle(hass)
        reload.assert_not_awaited()
        if mode in ("all", "missing"):
            # A switched-on system missing from the account no longer blocks.
            assert result["reason"] == (
                "reauth_successful"
                if mode == "all"
                else "reauth_successful_targets_missing"
            )
            if mode == "missing":
                assert result["description_placeholders"] == {"count": "1"}
            assert EntryConfiguration(entry, cloud).tokens() == tokens
        elif mode == "wrong_account":
            assert result["reason"] == "reauth_wrong_account"
            assert entry.data == before
        else:
            assert entry.data == before
            assert result["errors"] == {
                "base": {
                    "none": "reauth_targets_missing",
                    "bacon_auth": "invalid_auth",
                    "pointt_rate_limited": "cloud_rate_limited",
                    "bacon_rate_limited": "cloud_rate_limited",
                }.get(mode, "cannot_connect")
            }
            # Without a found system only a new sign-in can help, so a fresh
            # link is offered instead of repeating the same tokens.
            assert result["step_id"] == (
                "auth" if mode in ("bacon_auth", "none") else "retry"
            )
            if mode == "none":
                pointt.return_value = (Gateway("000123"), Gateway("gateway-b"))
                bacon.return_value = (("softener",), bacon.return_value[1])
                result = await hass.config_entries.flow.async_configure(
                    result["flow_id"],
                    {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
                )
                assert result["reason"] == "reauth_successful"
    assert entry.data["connections"]["local"] == before["connections"]["local"]


@pytest.mark.parametrize("found", [True, False])
async def test_with_every_target_switched_off_one_listed_target_suffices(hass, found):
    entry, cloud, _ = entry_for(hass)
    management = ConfigurationManagement(hass, entry)
    management.set_targets_enabled(management.edit(cloud), set())
    before = deepcopy(dict(entry.data))
    tokens = AuthTokens("new", "new", 10**12)
    account = EntryConfiguration(entry, cloud).cloud_data()["bacon_account"]
    bacon = AsyncMock(return_value=((), account))
    with (
        patch(
            MODULE + ".PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("000123"),) if found else ()),
        ),
        patch(MODULE + ".discover", bacon),
    ):
        if found:
            await management.async_reauthenticate(management.edit(cloud), tokens)
            assert EntryConfiguration(entry, cloud).tokens() == tokens
            bacon.assert_not_awaited()  # a listed system confirmed the account
        else:
            with pytest.raises(ConfigurationError, match="reauth_targets_missing"):
                await management.async_reauthenticate(management.edit(cloud), tokens)
            assert entry.data == before
            bacon.assert_awaited_once()


async def test_cloud_rate_limit_during_verification_is_retained_between_attempts(hass):
    entry, cloud, _ = entry_for(hass)
    manager = ConfigurationManagement(hass, entry)
    tokens = AuthTokens("new", "new", 10**12, scope=("bacon",))
    before = deepcopy(dict(entry.data))
    with (
        patch(
            MODULE + ".PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("000123"), Gateway("gateway-b"))),
        ) as pointt,
        patch(
            MODULE + ".discover", AsyncMock(side_effect=BaconError("rate_limited", 900))
        ) as bacon,
    ):
        with pytest.raises(BaconError):
            await manager.async_reauthenticate(manager.edit(cloud), tokens)
        with pytest.raises(RateLimited):
            await manager.async_reauthenticate(manager.edit(cloud), tokens)
        pointt.assert_awaited_once()
        bacon.assert_awaited_once()
    assert entry.data == before
    assert connection_backoff(hass, entry.entry_id, cloud)


async def test_source_retry_invalidates_previous_attempt_even_without_config_changes(
    hass,
):
    _, cloud, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)
    try:
        key = next(
            key
            for key, binding in runtime.snapshot.bindings.items()
            if binding.connection_id == cloud
        )
        old = harness.source(cloud, key)
        old.messages.put_nowait(SourceFailure())
        await settle(hass)
        assert not old.context.current
        harness.retry.set()
        await settle(hass)
        new = harness.source(cloud, key)
        assert new is not old
        assert not old.context.publish({"obsolete": True})
        assert new.context.publish({"fresh": True})
        assert runtime.source_data(key) == {"fresh": True}
    finally:
        await runtime.close()


async def test_runtime_ignores_token_rotation_and_display_options(hass):
    entry, cloud, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)
    before = {key: items[0] for key, items in harness.drivers.items()}
    try:
        data, options = deepcopy(dict(entry.data)), deepcopy(dict(entry.options))
        data["connections"][cloud]["credentials"]["access_token"] = "rotated"
        options["future_display"] = {"show": False}
        hass.config_entries.async_update_entry(entry, data=data, options=options)
        await settle(hass)
        assert {key: items[0] for key, items in harness.drivers.items()} == before
        assert all(len(items) == 1 for items in harness.drivers.values())
        assert all(runtime.source_data(key) for key in runtime.snapshot.bindings)
    finally:
        await runtime.close()


async def test_unknown_access_is_retained_without_running_an_adapter(hass):
    entry, cloud, _, harness, runtime = build(hass)
    data = deepcopy(dict(entry.data))
    data["connections"]["local"]["kind"] = "future_connection"
    data["bindings"]["local"]["adapter"] = "future_adapter"
    hass.config_entries.async_update_entry(entry, data=data)
    runtime.start()
    await settle(hass)
    try:
        assert runtime.connection_phase("local") is ConnectionPhase.UNSUPPORTED
        assert "local" not in harness.drivers
        assert runtime.connection_phase(cloud) is ConnectionPhase.READY
        assert entry.data["connections"]["local"]["kind"] == "future_connection"
    finally:
        await runtime.close()


async def test_a_not_found_note_ends_when_the_target_delivers_again(
    hass, enable_custom_integrations
):
    import asyncio

    from custom_components.bosch_buderus_heating.configuration_management import (
        not_found_bindings,
        set_not_found_issue,
    )

    entry, cloud, _ = entry_for(hass)
    management = ConfigurationManagement(hass, entry)
    missing = next(
        key
        for key, value in entry.data["bindings"].items()
        if value["locator"]["value"] == "gateway-b"
    )
    set_not_found_issue(hass, entry, management.current(), cloud, {missing})
    assert not_found_bindings(hass, entry, cloud) == (missing,)
    harness = Harness()
    harness.gates[cloud] = asyncio.Event()  # the cloud is still connecting
    hass.config.components.add(DOMAIN)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    owner = await async_setup_managed_entry(
        hass, entry, {"singlekey_cloud": harness.factory, "k40_local": harness.factory}
    )
    try:
        await settle(hass)
        assert owner.connections.source_phase(missing) is not ConnectionPhase.READY
        assert not_found_bindings(hass, entry, cloud) == (missing,)
        harness.gates[cloud].set()  # the system delivers data again
        await settle(hass)
        assert owner.connections.source_phase(missing) is ConnectionPhase.READY
        assert not not_found_bindings(hass, entry, cloud)
    finally:
        await owner.close()


async def test_managed_owner_retains_only_affected_entities_and_handles_global_unload(
    hass,
    enable_custom_integrations,
):
    entry, cloud, target = entry_for(hass)
    harness = Harness()
    hass.config.components.add(DOMAIN)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    owner = await async_setup_managed_entry(
        hass, entry, {"singlekey_cloud": harness.factory, "k40_local": harness.factory}
    )
    await settle(hass)
    registry = er.async_get(hass)
    local = registry.async_get_or_create(
        "sensor", DOMAIN, "retained-local", config_entry=entry
    )
    cloud_binding = next(
        key
        for key, binding in owner.connections.snapshot.bindings.items()
        if binding.connection_id == cloud
        and binding.target_id != target
        and binding.adapter == "pointt"
    )
    remote = registry.async_get_or_create(
        "sensor", DOMAIN, "retained-cloud", config_entry=entry
    )
    for registered, key, adapter in [
        (local, "local", "k40_local"),
        (remote, cloud_binding, "pointt"),
    ]:
        context = harness.source("local" if key == "local" else cloud, key).context
        assert owner.inventory.record(
            RetainedEntity(
                "sensor",
                registered.unique_id,
                owner.connections.snapshot.bindings[key].target_id,
                key,
                adapter,
                "temperature",
            ),
            context,
        )
        hass.states.async_set(registered.entity_id, "42")
        context.publish({"fresh": True})
    owner.connections.management.remove(owner.connections.management.edit("local"))
    await settle(hass)
    assert hass.states.get(local.entity_id).state == "unavailable"
    assert hass.states.get(remote.entity_id).state == "42"
    assert await async_unload_entry(hass, entry)
    assert hass.states.get(remote.entity_id).state == "unavailable"
    assert all(
        driver.closed == 1 for items in harness.drivers.values() for driver in items
    )
    restored = ConnectionInventory(hass, entry)
    await restored.load()
    assert len(restored.entities) == 2
    await async_remove_entry(hass, entry)
    assert await ConnectionInventory(hass, entry)._store.async_load() is None
    assert registry.async_get(local.entity_id) == local


async def test_cloud_auth_failures_are_queued_without_blocking_local(
    hass, enable_custom_integrations
):
    entry, cloud, _ = entry_for(hass, second_cloud=True)
    harness = Harness()
    harness.errors[cloud] = harness.errors["second"] = RefreshTokenRejected()
    hass.config.components.add(DOMAIN)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    owner = await async_setup_managed_entry(
        hass, entry, {"singlekey_cloud": harness.factory, "k40_local": harness.factory}
    )
    try:
        await settle(hass)
        flows = list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
        assert len(flows) == 1
        assert flows[0]["context"]["connection_id"] == cloud
        assert owner.connections.source_data("local")
        result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {})
        tokens = AuthTokens("new", "new", 10**12, scope=("bacon",))
        harness.errors.pop(cloud)
        with (
            patch(FLOW + ".OAuthClient.exchange_code", AsyncMock(return_value=tokens)),
            patch(
                MODULE + ".PointTClient.get_gateways",
                AsyncMock(return_value=(Gateway("000123"),)),
            ),
            patch(
                MODULE + ".discover",
                AsyncMock(
                    return_value=(
                        ("softener",),
                        EntryConfiguration(entry, cloud).cloud_data()["bacon_account"],
                    )
                ),
            ),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
            )
        assert result["reason"] == "reauth_successful"
        await settle(hass)
        flows = list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
        assert len(flows) == 1
        assert flows[0]["context"]["connection_id"] == "second"
        assert owner.connections.source_data("local")
    finally:
        await owner.close()


async def test_managed_reconfigure_routes_to_connection_settings(
    hass, enable_custom_integrations
):
    entry, _, _ = entry_for(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        data=dict(entry.data),
    )
    assert result["reason"] == "connection_settings_required"
    assert not hass.config_entries.options.async_progress()
    options = await hass.config_entries.options.async_init(entry.entry_id)
    options = await hass.config_entries.options.async_configure(
        options["flow_id"], {"next_step_id": "connections"}
    )
    assert options["step_id"] == "connections"


async def test_missing_connection_inventory_never_creates_entities_or_devices(
    hass, enable_custom_integrations
):
    entry, _, _ = entry_for(hass)
    hass.config.components.add(DOMAIN)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    owner = await async_setup_managed_entry(hass, entry, {})
    try:
        assert not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        assert not owner.inventory.entities
        assert all(
            owner.connections.connection_phase(key) is ConnectionPhase.UNSUPPORTED
            for key in entry.data["connections"]
        )
    finally:
        await owner.close()


async def test_invalid_changed_storage_stops_old_routing_and_recovers_after_repair(
    hass,
):
    entry, _, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)
    old = harness.source("local", "local")
    valid = deepcopy(dict(entry.data))
    invalid = deepcopy(valid)
    invalid["bindings"]["local"]["target_id"] = "missing-target"
    try:
        hass.config_entries.async_update_entry(entry, data=invalid)
        assert not old.context.publish({"stale": True})
        await settle(hass)
        assert not runtime._runs
        assert all(
            runtime.connection_phase(key) is ConnectionPhase.UNAVAILABLE
            for key in valid["connections"]
        )
        hass.config_entries.async_update_entry(entry, data=valid)
        await settle(hass)
        assert runtime.source_data("local")
        assert harness.source("local", "local") is not old
        assert not old.context.current
    finally:
        await runtime.close()


async def test_disabling_connection_during_login_prevents_sources_from_starting(hass):
    _, cloud, _, harness, runtime = build(hass)
    import asyncio

    harness.gates[cloud] = asyncio.Event()
    runtime.start()
    await settle(hass)
    try:
        runtime.management.set_enabled(runtime.management.edit(cloud), False)
        harness.gates[cloud].set()
        await settle(hass)
        assert not harness.drivers[cloud][0].sources
        assert harness.drivers[cloud][0].closed == 1
        assert runtime.connection_phase(cloud) is ConnectionPhase.DISABLED
        assert runtime.source_data("local")
        runtime.management.set_enabled(runtime.management.edit(cloud), True)
        await settle(hass)
        assert len(harness.drivers[cloud]) == 2
        assert len(harness.drivers[cloud][-1].sources) == 3
    finally:
        await runtime.close()


async def test_no_enabled_bindings_stops_only_the_unused_session(hass):
    _, cloud, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)
    try:
        runtime.management.set_targets_enabled(runtime.management.edit(cloud), set())
        await settle(hass)
        assert runtime.connection_phase(cloud) is ConnectionPhase.IDLE
        assert harness.drivers[cloud][0].closed == 1
        assert runtime.source_data("local")
    finally:
        await runtime.close()


@pytest.mark.parametrize("mode", ["unknown_adapter", "empty", "malformed_tokens"])
async def test_unverifiable_reauth_never_sends_credentials_to_backends(hass, mode):
    entry, cloud, _ = entry_for(hass)
    data, options = deepcopy(dict(entry.data)), deepcopy(dict(entry.options))
    tokens = AuthTokens("new", "new", 10**12)
    if mode == "unknown_adapter":
        next(
            value
            for value in data["bindings"].values()
            if value["connection_id"] == cloud
        )["adapter"] = "future_adapter"
    elif mode == "empty":
        removed = {
            key
            for key, value in data["bindings"].items()
            if value["connection_id"] == cloud
        }
        for key in removed:
            del data["bindings"][key]
            del options["bindings"][key]
        for target in options["targets"].values():
            target["read_order"] = [
                key for key in target["read_order"] if key not in removed
            ]
    else:
        tokens = AuthTokens("", "new", 10**12)
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    manager = ConfigurationManagement(hass, entry)
    with (
        patch(MODULE + ".PointTClient.get_gateways", AsyncMock()) as pointt,
        patch(MODULE + ".discover", AsyncMock()) as bacon,
        pytest.raises(ConfigurationError),
    ):
        await manager.async_reauthenticate(manager.edit(cloud), tokens)
    pointt.assert_not_awaited()
    bacon.assert_not_awaited()
    assert entry.data == data


async def test_unknown_connection_can_be_reviewed_and_removed_in_options(
    hass, enable_custom_integrations
):
    entry, _, _ = entry_for(hass)
    data = deepcopy(dict(entry.data))
    data["connections"]["local"]["kind"] = "future_connection"
    data["connections"]["local"]["endpoint"] = "unrecognized-future-metadata"
    data["bindings"]["local"]["adapter"] = "future_adapter"
    hass.config_entries.async_update_entry(entry, data=data)
    result = await choose_connection(hass, entry, "local")
    assert "connection_reauthenticate" not in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_remove"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    assert result["type"] is FlowResultType.MENU
    assert "local" not in entry.data["connections"]


async def test_empty_owner_still_opens_connection_settings(
    hass, enable_custom_integrations
):
    entry, cloud, _ = entry_for(hass)
    manager = ConfigurationManagement(hass, entry)
    manager.remove(manager.edit(cloud))
    manager.remove(manager.edit("local"))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connections"}
    )
    assert result["reason"] == "no_connections"
    assert entry.data["targets"]


async def test_notification_options_find_gateways_across_explicit_cloud_connections(
    hass, enable_custom_integrations
):
    entry, _, _ = entry_for(hass, second_cloud=True)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "notifications"}
    )
    assert result["step_id"] == "notifications"
    field = next(iter(result["data_schema"].schema.values()))
    assert len(field.config["options"]) == 2


async def test_owner_loads_both_inventories_side_by_side(
    hass, enable_custom_integrations
):
    """O64: the entity and the discovery inventory load in parallel."""
    from custom_components.bosch_buderus_heating.discovery_inventory import (
        DiscoveryInventory,
        discovery_inventory,
    )

    entry, _, _ = entry_for(hass)
    hass.config.components.add(DOMAIN)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    started = []
    together = asyncio.Event()

    def waiting(name, original):
        async def load(self):
            started.append(name)
            if len(started) == 2:
                together.set()
            await asyncio.wait_for(together.wait(), 5)
            await original(self)

        return load

    with (
        patch.object(
            ConnectionInventory, "load", waiting("entities", ConnectionInventory.load)
        ),
        patch.object(
            DiscoveryInventory,
            "async_load",
            waiting("discovery", DiscoveryInventory.async_load),
        ),
    ):
        owner = await async_setup_managed_entry(hass, entry, {})
    try:
        assert sorted(started) == ["discovery", "entities"]
        assert owner.inventory.healthy
        assert discovery_inventory(hass, entry.entry_id).loaded
    finally:
        await owner.close()


async def test_a_failing_inventory_load_still_loads_the_other(
    hass, enable_custom_integrations
):
    from custom_components.bosch_buderus_heating.discovery_inventory import (
        discovery_inventory,
    )

    entry, _, _ = entry_for(hass)
    hass.config.components.add(DOMAIN)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    with (
        patch.object(
            ConnectionInventory, "load", AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await async_setup_managed_entry(hass, entry, {})
    assert discovery_inventory(hass, entry.entry_id).loaded


@pytest.mark.parametrize("damage", ["content", "unreadable"])
async def test_local_access_removal_with_a_damaged_setup_record(
    hass, enable_custom_integrations, tmp_path, damage
):
    """FLOW-3: unreadable content never blocks the removal; I/O errors say so."""
    from custom_components.bosch_buderus_heating.local_enrollment import (
        JOURNAL_KEY,
        enrollment_journal,
    )

    entry, _, _ = entry_for(hass)
    journal = enrollment_journal(hass)
    journal._path = tmp_path / JOURNAL_KEY
    if damage == "content":
        # Written by a newer format, or damaged.
        journal._path.write_text('{"version": 4, "attempts": []}')
    else:
        journal._path.mkdir()  # reading fails with an OS error
    result = await choose_connection(hass, entry, "local")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_remove"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    if damage == "unreadable":
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "local_storage_failed"
        assert "local" in entry.data["connections"]
        return
    assert result["type"] is FlowResultType.MENU
    assert "local" not in entry.data["connections"]
    assert not journal._path.exists()
    aside = tmp_path / (JOURNAL_KEY + ".corrupt")
    assert aside.read_text() == '{"version": 4, "attempts": []}'
    assert await journal.records() == ()


# Back navigation and result notes (options dialog, option B) --------------


async def _menu(hass, flow_id, step):
    return await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": step}
    )


async def test_every_submenu_leads_back_to_its_parent(hass, enable_custom_integrations):
    from tests.helpers.options_menu import connection_choice

    hass.config.language = "de"
    entry, cloud, _ = entry_for(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = result["flow_id"]
    assert result["step_id"] == "init"
    assert "back" not in result["menu_options"]  # the main menu has none
    result = await _menu(hass, flow_id, "connections")
    assert result["type"] is FlowResultType.MENU
    assert list(result["menu_options"])[-1] == "back"
    assert result["menu_options"]["back"] == "← Zurück"
    # Connections -> main menu.
    result = await _menu(hass, flow_id, "back")
    assert result["step_id"] == "init"
    # Main menu -> connections -> connection -> connections -> main menu.
    result = await _menu(hass, flow_id, "connections")
    result = await hass.config_entries.options.async_configure(
        flow_id, connection_choice(hass, result, cloud)
    )
    assert result["step_id"] == "connection"
    assert result["menu_options"][-1] == "back"
    result = await _menu(hass, flow_id, "back")
    assert result["step_id"] == "connections"
    result = await _menu(hass, flow_id, "back")
    assert result["step_id"] == "init"
    assert result["description_placeholders"]["result"] == ""
    # Going back and forth many times keeps one flow, nothing piles up.
    for _ in range(20):
        result = await _menu(hass, flow_id, "connections")
        result = await _menu(hass, flow_id, "back")
    assert result["step_id"] == "init"
    assert [
        flow["flow_id"] for flow in hass.config_entries.options.async_progress()
    ] == [flow_id]
    hass.config_entries.options.async_abort(flow_id)
    assert not hass.config_entries.options.async_progress()


async def test_a_saved_connection_setting_returns_to_its_menu(
    hass, enable_custom_integrations
):
    from tests.helpers.options_menu import menu_note

    hass.config.language = "en"
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud)
    flow_id = result["flow_id"]
    result = await _menu(hass, flow_id, "connection_enabled")
    with patch.object(hass.config_entries, "async_reload", AsyncMock()):
        result = await hass.config_entries.options.async_configure(
            flow_id, {"enabled": False}
        )
        await settle(hass)
    note = await menu_note(hass, result, "connection_saved", step="connection")
    assert note.startswith("The change was saved.")
    # The note is shown once: the same menu again has none.
    flow = hass.config_entries.options._progress[flow_id]
    again = await flow.async_step_connection()
    assert again["description_placeholders"]["result"] == ""
    # Saved, and the next change in the same dialog works on the new state.
    assert entry.options["connections"][cloud]["enabled"] is False
    result = await _menu(hass, flow_id, "connection_enabled")
    assert result["step_id"] == "connection_enabled"
    with patch.object(hass.config_entries, "async_reload", AsyncMock()):
        result = await hass.config_entries.options.async_configure(
            flow_id, {"enabled": True}
        )
        await settle(hass)
    assert result["step_id"] == "connection"
    assert entry.options["connections"][cloud]["enabled"] is True
    # The note is shown once.
    result = await _menu(hass, flow_id, "back")
    assert result["step_id"] == "connections"
    assert result["description_placeholders"]["result"] == ""
    hass.config_entries.options.async_abort(flow_id)


async def test_a_removed_access_returns_to_the_connections(
    hass, enable_custom_integrations
):
    from tests.helpers.options_menu import connection_choice, menu_note

    hass.config.language = "en"
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud)
    flow_id = result["flow_id"]
    result = await _menu(hass, flow_id, "connection_remove")
    with patch.object(hass.config_entries, "async_reload", AsyncMock()):
        result = await hass.config_entries.options.async_configure(
            flow_id, {"confirm": True}
        )
        await settle(hass)
    await menu_note(hass, result, "connection_removed", step="connections")
    assert cloud not in entry.data["connections"]
    with pytest.raises(AssertionError):
        connection_choice(hass, result, cloud)
    # The last access: removed, back in the main menu.
    (local,) = entry.data["connections"]
    result = await hass.config_entries.options.async_configure(
        flow_id, connection_choice(hass, result, local)
    )
    result = await _menu(hass, flow_id, "connection_remove")
    with patch.object(hass.config_entries, "async_reload", AsyncMock()):
        result = await hass.config_entries.options.async_configure(
            flow_id, {"confirm": True}
        )
        await settle(hass)
    await menu_note(hass, result, "connection_removed", step="init")
    assert not entry.data["connections"]
    hass.config_entries.options.async_abort(flow_id)
