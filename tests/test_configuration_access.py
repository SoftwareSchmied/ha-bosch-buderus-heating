"""Exercise real entry updates and overlapping refreshes with synthetic data."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bosch_buderus_heating.auth_lifecycle import (
    _TOKEN_MANAGERS,
    ACCOUNT_BACKOFFS,
    entry_auth,
    forget_entry_auth,
)
from custom_components.bosch_buderus_heating.config_flow import BoschBuderusConfigFlow
from custom_components.bosch_buderus_heating.configuration import (
    ConfigurationError,
    load_configuration,
)
from custom_components.bosch_buderus_heating.configuration_access import (
    AUTH_EPOCH,
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.configuration_changes import (
    ConfigurationRuns,
)
from custom_components.bosch_buderus_heating.configuration_migration import (
    convert_legacy_configuration,
    legacy_cloud_connection_id,
)
from custom_components.bosch_buderus_heating.const import DOMAIN, PollingProfile
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    RateLimited,
    RefreshTokenRejected,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_configuration import OWNER, add_local, legacy

NEW = AuthTokens("new-synthetic", "rotated-synthetic", 10**12)


def configured(hass, version=4):
    data, options = legacy(), {"polling_profile": "standard", "unrelated": {"keep": 1}}
    if version == 4:
        snapshot = convert_legacy_configuration(OWNER, data, options)
        data, options = snapshot.to_data(), snapshot.to_options()
        add_local(data, options)
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=OWNER,
        version=version,
        minor_version=1,
        data=data,
        options=options,
    )
    entry.add_to_hass(hass)
    configuration = EntryConfiguration(entry, legacy_cloud_connection_id(OWNER))
    return entry, configuration


@pytest.mark.parametrize("version", [1, 2, 3, 4])
async def test_readers_keep_exact_targets_tokens_and_options(hass, version):
    entry, configuration = configured(hass, version)
    data = configuration.cloud_data()
    assert data["gateway_ids"] == ["000123"]
    assert configuration.tokens().refresh_token == "synthetic-refresh"
    assert configuration.polling_profile() is PollingProfile.STANDARD
    data["gateway_ids"].append("unintended")
    assert configuration.cloud_data()["gateway_ids"] == ["000123"]
    options = configuration.polling_options(PollingProfile.CLOUD_FRIENDLY)
    assert options["unrelated"] == {"keep": 1}
    assert entry.options != options
    hass.config_entries.async_update_entry(entry, options=options)
    assert configuration.polling_profile() is PollingProfile.CLOUD_FRIENDLY
    if version == 4:
        assert entry.options["connections"]["local"] == {"enabled": True}
        with pytest.raises(ConfigurationError, match="connection_required"):
            EntryConfiguration(entry)
        with pytest.raises(ConfigurationError, match="unsupported_legacy_version"):
            configuration.legacy_update({})
    else:
        with pytest.raises(ConfigurationError, match="unknown_connection"):
            EntryConfiguration(entry, "someone-else")


@pytest.mark.parametrize(
    "version,change",
    [
        (version, change)
        for version in (3, 4)
        for change in ("options", "same_login", "replacement", "removed", "disabled")
        if version == 4 or change != "disabled"
    ],
)
async def test_pending_rotation_checks_login_but_preserves_other_changes(
    hass, version, change
):
    entry, configuration = configured(hass, version)
    oauth = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(_):
        entered.set()
        await release.wait()
        return NEW

    oauth.refresh.side_effect = refresh
    manager, backoff = entry_auth(
        hass, entry, oauth, connection_id=configuration.connection_id
    )
    waiter = asyncio.create_task(manager.get_access_token(force_refresh=True))
    await entered.wait()
    data, options = deepcopy(dict(entry.data)), deepcopy(dict(entry.options))
    options["unrelated"] = {"keep": 2}
    if change in ("same_login", "replacement"):
        backoff.activate(300)
        # An explicit login is different even when it returns identical tokens.
        if version == 3:
            data = configuration.legacy_update(tokens_to_data(configuration.tokens()))
            if change == "replacement":
                data.update(
                    tokens_to_data(AuthTokens("replacement", "replacement", 10**12))
                )
        else:
            connection = data["connections"][configuration.connection_id]
            connection["auth_epoch"] += 1
            if change == "replacement":
                connection["credentials"] = tokens_to_data(
                    AuthTokens("replacement", "replacement", 10**12)
                )
    elif change == "removed":
        if version == 3:
            # Model entry removal without invoking the unrelated platform setup.
            forget_entry_auth(hass, entry)
        else:
            del data["connections"][configuration.connection_id]
    elif change == "disabled" and version == 4:
        options["connections"][configuration.connection_id]["enabled"] = False
    if version == 4:
        data["connections"]["local"]["endpoint"]["host"] = "new.example.invalid"
        # An unrelated broken source must not discard already rotated credentials.
        data["connections"]["local"]["credentials"] = {"needs_repair": True}
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    try:
        if version == 3 and change == "removed":
            with patch.object(
                hass.config_entries, "async_get_entry", return_value=None
            ):
                release.set()
                assert await waiter == NEW.access_token
        else:
            release.set()
            assert await waiter == NEW.access_token
    finally:
        release.set()
    accepted = change in ("options", "disabled")
    if change == "removed" and version == 4:
        assert configuration.connection_id not in entry.data["connections"]
    else:
        assert (configuration.tokens() == NEW) is accepted
    assert entry.options == options
    if version == 4:
        assert entry.data["connections"]["local"] == data["connections"]["local"]
        assert entry.data["revision"] == data["revision"] + int(accepted)
    oauth.refresh.assert_awaited_once()
    if change in ("same_login", "replacement"):
        replacement, still_backing_off = entry_auth(
            hass, entry, oauth, connection_id=configuration.connection_id
        )
        assert replacement is not manager
        assert still_backing_off is backoff
        with pytest.raises(RateLimited):
            await replacement.get_access_token(force_refresh=True)
        oauth.refresh.assert_awaited_once()


async def test_consumers_share_connection_and_rotation_survives_cancelled_waiter(hass):
    entry, configuration = configured(hass)
    data, options = deepcopy(dict(entry.data)), deepcopy(dict(entry.options))
    data["connections"]["second-cloud"] = deepcopy(
        data["connections"][configuration.connection_id]
    )
    options["connections"]["second-cloud"] = {
        "enabled": True,
        "polling_profile": "standard",
    }
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    oauth = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(_):
        entered.set()
        await release.wait()
        return NEW

    oauth.refresh.side_effect = refresh
    first, pause = entry_auth(
        hass, entry, oauth, connection_id=configuration.connection_id
    )
    same, same_pause = entry_auth(
        hass, entry, oauth, connection_id=configuration.connection_id
    )
    other, other_pause = entry_auth(hass, entry, oauth, connection_id="second-cloud")
    assert first is same and pause is same_pause
    assert other is not first and other_pause is not pause
    waiter = asyncio.create_task(first.get_access_token(force_refresh=True))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    continuing, _ = entry_auth(
        hass, entry, oauth, connection_id=configuration.connection_id
    )
    assert continuing is first
    release.set()
    assert await continuing.get_access_token() == NEW.access_token
    assert configuration.tokens() == NEW
    assert EntryConfiguration(entry, "second-cloud").tokens() != NEW
    assert (
        entry_auth(hass, entry, oauth, connection_id=configuration.connection_id)[0]
        is first
    )
    oauth.refresh.assert_awaited_once()
    forget_entry_auth(hass, entry)
    assert not hass.data[_TOKEN_MANAGERS] and not hass.data[ACCOUNT_BACKOFFS]


@pytest.mark.parametrize(
    "change", ["rotation", "notifications", "selection", "profile", "login"]
)
async def test_open_dialog_merges_intent_or_rejects_changed_configuration(hass, change):
    entry, configuration = configured(hass, 3)
    flow = BoschBuderusConfigFlow()
    flow.hass = hass
    flow._configuration_context = configuration.dialog_context()
    data, options = deepcopy(dict(entry.data)), deepcopy(dict(entry.options))
    if change == "rotation":
        data.update(tokens_to_data(NEW))
    elif change == "notifications":
        options["fault_notifications"] = {"somewhere": True}
    elif change == "selection":
        data["gateway_ids"] = ["another-gateway"]
    elif change == "profile":
        options["polling_profile"] = "cloud_friendly"
    else:
        data = configuration.legacy_update(tokens_to_data(configuration.tokens()))
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = flow._update_and_restart(
            entry,
            data_updates={"gateway_ids": ["new-selection"]},
            options={"polling_profile": "standard"},
        )
    if change in ("rotation", "notifications"):
        assert result["reason"] != "configuration_changed"
        assert entry.data["gateway_ids"] == ["new-selection"]
        assert entry.data["refresh_token"] == data["refresh_token"]
        assert entry.options == options
        reload_entry.assert_called_once()
    else:
        assert result["reason"] == "configuration_changed"
        assert entry.data == data and entry.options == options
        reload_entry.assert_not_called()


async def test_conversion_preserves_epoch_and_storage_identity(hass):
    entry, configuration = configured(hass, 3)
    data = configuration.legacy_update(tokens_to_data(configuration.tokens()))
    assert data[AUTH_EPOCH] == 1 and AUTH_EPOCH not in entry.data
    proposal = convert_legacy_configuration(entry.entry_id, data, entry.options)
    assert proposal.connections[configuration.connection_id].auth_epoch == 1
    assert AUTH_EPOCH not in proposal.to_data()["legacy_extra"]


@pytest.mark.parametrize(
    "change", ["epoch", "brand", "tokens", "version", "minor", "broken"]
)
async def test_late_token_patch_rejects_changed_or_invalid_context(hass, change):
    entry, configuration = configured(hass)
    context, tokens = configuration.context(), configuration.tokens()
    data = deepcopy(dict(entry.data))
    connection = data["connections"][configuration.connection_id]
    if change == "epoch":
        connection["auth_epoch"] += 1
    elif change == "brand":
        connection["brand"] = "bosch"
    elif change == "tokens":
        connection["credentials"] = tokens_to_data(NEW)
    elif change == "broken":
        connection["credentials"]["expires_at"] = "invalid"
    hass.config_entries.async_update_entry(
        entry,
        data=data,
        version=3 if change == "version" else 4,
        minor_version=2 if change == "minor" else 1,
    )
    assert configuration.rotated_data(context, tokens, NEW) is None


@pytest.mark.parametrize("field", ["legacy_bacon_account", "account_evidence"])
async def test_account_metadata_change_during_refresh_keeps_rotated_token(hass, field):
    """C16/AUTH-3: only a new sign-in (epoch) invalidates a pending rotation."""
    entry, configuration = configured(hass)
    oauth = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(_):
        entered.set()
        await release.wait()
        return NEW

    oauth.refresh.side_effect = refresh
    manager, _backoff = entry_auth(
        hass, entry, oauth, connection_id=configuration.connection_id
    )
    waiter = asyncio.create_task(manager.get_access_token(force_refresh=True))
    await entered.wait()
    # For example the first separate cloud device chosen while a refresh runs.
    data = deepcopy(dict(entry.data))
    connection = data["connections"][configuration.connection_id]
    connection[field] = (
        "new-account-key" if field == "legacy_bacon_account" else {"opaque": "x"}
    )
    hass.config_entries.async_update_entry(entry, data=data)
    # A restarted connection during the refresh keeps the same manager.
    same, _ = entry_auth(hass, entry, oauth, connection_id=configuration.connection_id)
    assert same is manager
    release.set()
    assert await waiter == NEW.access_token
    assert configuration.tokens() == NEW
    assert (
        entry.data["connections"][configuration.connection_id][field]
        == (connection[field])
    )
    # And afterwards as well: the stored tokens are the manager's tokens.
    again, _ = entry_auth(hass, entry, oauth, connection_id=configuration.connection_id)
    assert again is manager
    oauth.refresh.assert_awaited_once()


async def test_rejected_manager_is_replaced_on_the_next_start(hass):
    """C17/AUTH-4: a reload or a check may try the stored token once more."""
    entry, configuration = configured(hass)
    oauth = AsyncMock()
    oauth.refresh.side_effect = RefreshTokenRejected()
    manager, _ = entry_auth(
        hass, entry, oauth, connection_id=configuration.connection_id
    )
    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token(force_refresh=True)
    # The rejected manager itself never asks again.
    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token(force_refresh=True)
    assert oauth.refresh.await_count == 1
    retry, _ = entry_auth(hass, entry, oauth, connection_id=configuration.connection_id)
    assert retry is not manager and not retry.refresh_rejected
    # The temporary refusal is over: the same stored token works again.
    oauth.refresh.side_effect = None
    oauth.refresh.return_value = NEW
    assert await retry.get_access_token(force_refresh=True) == NEW.access_token
    assert configuration.tokens() == NEW
    assert oauth.refresh.await_count == 2
    # A healthy manager is still reused.
    reused, _ = entry_auth(
        hass, entry, oauth, connection_id=configuration.connection_id
    )
    assert reused is retry


@pytest.mark.parametrize(
    "invalid",
    [float("inf"), float("nan"), True, "soon", pytest.param(10**400, id="overflow")],
)
def test_invalid_expiry_is_rejected_without_sensitive_error(invalid):
    entry = MockConfigEntry(
        domain=DOMAIN, version=3, data={**legacy(), "expires_at": invalid}
    )
    configuration = EntryConfiguration(entry)
    with pytest.raises(ValueError) as caught:
        configuration.tokens()
    assert "synthetic" not in str(caught.value)
    assert "private-owner" not in repr(configuration.context())


async def test_disabled_source_invalidates_read_generation_but_saves_rotation(hass):
    entry, configuration = configured(hass)
    snapshot = load_configuration(OWNER, entry.data, entry.options)
    runs = ConfigurationRuns(snapshot)
    stamp = runs.capture(configuration.connection_id)
    context, previous = configuration.context(), configuration.tokens()
    options = deepcopy(dict(entry.options))
    options["connections"][configuration.connection_id]["enabled"] = False
    hass.config_entries.async_update_entry(entry, options=options)
    runs.update(load_configuration(OWNER, entry.data, entry.options))
    assert not runs.accepts(stamp)
    patch_data = configuration.rotated_data(context, previous, NEW)
    assert patch_data is not None
    hass.config_entries.async_update_entry(entry, data=patch_data)
    assert configuration.tokens() == NEW
    assert entry.options == options


@pytest.mark.parametrize("version,minor", [(0, 1), (5, 1), (4, 3)])
async def test_unknown_storage_versions_fail_closed(hass, version, minor):
    entry = MockConfigEntry(domain=DOMAIN, version=version, minor_version=minor)
    with pytest.raises(ConfigurationError, match="unsupported_version"):
        EntryConfiguration(entry, "cloud")


@pytest.mark.parametrize("epoch", [-1, True, "1", None])
async def test_invalid_login_generation_is_not_accepted(hass, epoch):
    entry, configuration = configured(hass, 3)
    data = {**entry.data, AUTH_EPOCH: epoch}
    hass.config_entries.async_update_entry(entry, data=data)
    with pytest.raises(ConfigurationError, match="invalid_counter"):
        configuration.context()
    with pytest.raises(ConfigurationError, match="invalid_counter"):
        convert_legacy_configuration(entry.entry_id, data, entry.options)


async def test_target_projection_keeps_standalone_devices_in_their_connection(hass):
    proposal = convert_legacy_configuration(
        OWNER, legacy(["gateway", "wallbox"], ["softener"]), {}
    )
    data, options = proposal.to_data(), proposal.to_options()
    cloud = next(iter(proposal.connections))
    data["connections"]["other"] = deepcopy(data["connections"][cloud])
    data["bindings"]["other"] = {
        "connection_id": "other",
        "locator": {"value": "foreign"},
        "adapter": "pointt",
    }
    data["bindings"]["unknown"] = {"connection_id": cloud, "adapter": "future"}
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=OWNER,
        version=4,
        minor_version=1,
        data=data,
        options=options,
    )
    configuration = EntryConfiguration(entry, cloud)
    view = configuration.cloud_data()
    assert view["gateway_ids"] == ["gateway", "wallbox"]
    assert view["bacon_device_ids"] == ["softener"]
    assert "foreign" not in repr(view)
    with pytest.raises(ConfigurationError, match="invalid_object"):
        EntryConfiguration(entry, "removed").tokens()


async def test_late_refresh_cannot_restore_forgotten_session(hass):
    entry, configuration = configured(hass, 3)
    oauth = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(_):
        entered.set()
        await release.wait()
        return NEW

    oauth.refresh.side_effect = refresh
    manager, _ = entry_auth(hass, entry, oauth)
    waiter = asyncio.create_task(manager.get_access_token(force_refresh=True))
    await entered.wait()
    forget_entry_auth(hass, entry)
    release.set()
    assert await waiter == NEW.access_token
    assert configuration.tokens() != NEW


async def test_stale_dialog_after_storage_change_aborts_without_overwrite(hass):
    entry, configuration = configured(hass, 3)
    flow = BoschBuderusConfigFlow()
    flow.hass = hass
    flow._configuration_context = configuration.dialog_context()
    proposal = convert_legacy_configuration(OWNER, entry.data, entry.options)
    hass.config_entries.async_update_entry(
        entry, data=proposal.to_data(), options=proposal.to_options(), version=4
    )
    result = flow._update_and_restart(entry, data_updates={"gateway_ids": ["stale"]})
    assert result["reason"] == "configuration_changed"
    assert entry.data == proposal.to_data()


async def test_diagnostics_remain_available_for_unknown_storage(hass):
    from custom_components.bosch_buderus_heating.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(
        domain=DOMAIN, version=99, data={"credentials": "private-data"}
    )
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["config"]["entry_version"] == 99
    assert result["setup"]["runtime_available"] is False
    assert "private-data" not in repr(result)


async def test_setup_rejects_invalid_auth_epoch_as_authentication_failure(hass):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    from custom_components.bosch_buderus_heating import async_setup_entry

    entry, _ = configured(hass, 3)
    hass.config_entries.async_update_entry(entry, data={**entry.data, AUTH_EPOCH: -1})
    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)
