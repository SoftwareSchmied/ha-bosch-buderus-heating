"""Connection lifecycle checks with synthetic credentials and cloud responses."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.bosch_buderus_heating import async_setup_entry
from custom_components.bosch_buderus_heating.auth_lifecycle import (
    _TOKEN_MANAGERS,
    ACCOUNT_BACKOFFS,
    entry_auth,
)
from custom_components.bosch_buderus_heating.config_flow import BoschBuderusConfigFlow
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    CIRCUIT_FAILURE_THRESHOLD,
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    Gateway,
    PointTClient,
    RateLimited,
    RefreshTokenRejected,
    RequestTimeout,
    Resource,
    ResourceMetadata,
    RetryPolicy,
    ServiceUnavailable,
    TokenManager,
)
from custom_components.bosch_buderus_heating.pointt.transport import RateLimitBackoff
from custom_components.bosch_buderus_heating.repairs import RateLimitRepairFlow
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from tests.ha_test_helpers import MockConfigEntry
from tests.test_pointt_transport_and_client import serve

ROOT = "custom_components.bosch_buderus_heating"


async def _live_entry(hass, entry=None):
    if entry is None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "brand": "buderus",
                "gateway_ids": ["gateway"],
                **tokens_to_data(AuthTokens("original", "original-refresh", 10**12)),
            },
        )
        entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    with (
        patch.object(PointTClient, "get_gateways", return_value=(Gateway("gateway"),)),
        patch(
            ROOT + ".coordinator.BoschBuderusDataUpdateCoordinator."
            "async_config_entry_first_refresh",
            AsyncMock(),
        ),
        patch(ROOT + ".async_setup_notifications", AsyncMock(return_value=())),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
    ):
        assert await async_setup_entry(hass, entry)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    return entry


@pytest.mark.parametrize("next_action", ["setup", "reconfigure"])
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_pending_rotation_survives_home_assistant_unload(hass, next_action):
    entry = await _live_entry(hass)
    hass.config.components.add(DOMAIN)
    original = entry.runtime_data.token_manager
    entered = asyncio.Event()
    release = asyncio.Event()

    async def refresh(_token):
        entered.set()
        await release.wait()
        return AuthTokens("rotated", "rotated-refresh", 10**12)

    with (
        patch(ROOT + ".OAuthClient.refresh", side_effect=refresh) as refresh_call,
        patch.object(hass.config_entries, "async_unload_platforms", return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
    ):
        waiter = asyncio.create_task(original.get_access_token(force_refresh=True))
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        try:
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert not hasattr(entry, "runtime_data")
            if next_action == "setup":
                await _live_entry(hass, entry)
                manager = entry.runtime_data.token_manager
            else:
                flow = BoschBuderusConfigFlow()
                flow.hass = hass
                flow._reconfigure_entry = entry
                manager = flow._reconfigure_client()._token_provider
            assert manager is original
        finally:
            release.set()
            assert await original.get_access_token() == "rotated"
            await hass.async_block_till_done()
        assert entry.data["refresh_token"] == "rotated-refresh"
        refresh_call.assert_awaited_once()
        reload_entry.assert_not_called()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_removing_entry_during_refresh_does_not_restore_credentials(hass):
    entry = await _live_entry(hass)
    hass.config.components.add(DOMAIN)
    manager = entry.runtime_data.token_manager
    entered = asyncio.Event()
    release = asyncio.Event()

    async def refresh(_token):
        entered.set()
        await release.wait()
        return AuthTokens("rotated", "rotated-refresh", 10**12)

    with (
        patch(ROOT + ".OAuthClient.refresh", side_effect=refresh),
        patch.object(hass.config_entries, "async_unload_platforms", return_value=True),
    ):
        waiter = asyncio.create_task(manager.get_access_token(force_refresh=True))
        await entered.wait()
        try:
            await hass.config_entries.async_remove(entry.entry_id)
            assert hass.config_entries.async_get_entry(entry.entry_id) is None
        finally:
            release.set()
            assert await waiter == "rotated"
    assert entry.data["refresh_token"] == "original-refresh"
    assert not any(key[0] == entry.entry_id for key in hass.data[_TOKEN_MANAGERS])
    assert not any(key[0] == entry.entry_id for key in hass.data[ACCOUNT_BACKOFFS])


async def test_ordinary_rotation_keeps_the_loaded_integration_running(hass):
    entry = await _live_entry(hass)
    runtime = entry.runtime_data
    updated = AuthTokens("rotated", "rotated-refresh", 10**12)
    with (
        patch(ROOT + ".OAuthClient.refresh", return_value=updated) as refresh,
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
    ):
        assert (
            await runtime.token_manager.get_access_token(force_refresh=True)
            == "rotated"
        )
        await hass.async_block_till_done()
    assert entry.runtime_data is runtime
    assert entry.data["refresh_token"] == "rotated-refresh"
    refresh.assert_awaited_once_with("original-refresh")
    reload_entry.assert_not_called()


@pytest.mark.parametrize("change", ["credentials", "gateways", "profile", "unchanged"])
async def test_loaded_entry_restarts_once_for_explicit_configuration(hass, change):
    entry = await _live_entry(hass)
    flow = BoschBuderusConfigFlow()
    flow.hass = hass
    data = {}
    options = dict(entry.options)
    if change == "credentials":
        data = tokens_to_data(AuthTokens("signed-in", "signed-in-refresh", 10**12))
    elif change == "gateways":
        data = {"gateway_ids": ["gateway", "second"]}
    elif change == "profile":
        options["polling_profile"] = "cloud_friendly"
    with (
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
        patch("homeassistant.config_entries.report_usage") as usage,
    ):
        flow._update_and_restart(entry, data_updates=data, options=options)
        await hass.async_block_till_done()
    reload_entry.assert_called_once_with(entry.entry_id)
    usage.assert_not_called()


async def test_notification_preferences_do_not_reload_the_connection(hass):
    entry = await _live_entry(hass)
    with (
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
        patch(ROOT + ".async_update_notification_options", AsyncMock()) as update,
    ):
        hass.config_entries.async_update_entry(
            entry, options={"fault_notifications": {}}
        )
        await hass.async_block_till_done()
    update.assert_awaited_once_with(hass, entry)
    reload_entry.assert_not_called()


async def test_delayed_refresh_cannot_overwrite_new_interactive_credentials(hass):
    entry = await _live_entry(hass)
    entered = asyncio.Event()
    release = asyncio.Event()
    new_login = AuthTokens("signed-in", "signed-in-refresh", 10**12)

    async def refresh(_token):
        entered.set()
        await release.wait()
        return AuthTokens("previous-session", "previous-session-refresh", 10**12)

    with (
        patch(ROOT + ".OAuthClient.refresh", side_effect=refresh),
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
    ):
        waiter = asyncio.create_task(
            entry.runtime_data.token_manager.get_access_token(force_refresh=True)
        )
        await entered.wait()
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, **tokens_to_data(new_login)}
        )
        release.set()
        await waiter
        await hass.async_block_till_done()
    assert entry.data["access_token"] == "signed-in"
    assert entry.data["refresh_token"] == "signed-in-refresh"
    reload_entry.assert_called_once_with(entry.entry_id)


@pytest.mark.parametrize("remove_runtime", [False, True])
async def test_setup_retry_keeps_oauth_rate_limit_without_more_requests(
    hass, remove_runtime
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "gateway_ids": ["gateway"],
            **tokens_to_data(AuthTokens("old", "refresh", 0)),
        },
    )
    entry.add_to_hass(hass)
    with patch(ROOT + ".OAuthClient.refresh", side_effect=RateLimited(3600)) as refresh:
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)
        if remove_runtime:
            entry.runtime_data.startup.detach()
            del entry.runtime_data
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)
    refresh.assert_awaited_once_with("refresh")


@pytest.mark.parametrize("already_friendly", [False, True])
async def test_rate_limit_repair_schedules_only_one_reload(hass, already_friendly):
    entry = await _live_entry(hass)
    if already_friendly:
        with patch.object(hass.config_entries, "async_schedule_reload"):
            hass.config_entries.async_update_entry(
                entry, options={"polling_profile": "cloud_friendly"}
            )
            await hass.async_block_till_done()
    flow = RateLimitRepairFlow(entry.entry_id, "synthetic-issue")
    flow.hass = hass
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        await flow.async_step_confirm({})
        await hass.async_block_till_done()
    reload_entry.assert_called_once_with(entry.entry_id)


@pytest.mark.parametrize("outage", ["service", "disconnect"])
@pytest.mark.usefixtures("socket_enabled")
async def test_gateway_returns_after_breaker_without_reload_or_reauthentication(
    hass, outage
):
    now = 1000.0
    healthy = False
    calls = []
    path = "/heatingCircuits/hc2/roomtemperature"

    async def response(request):
        calls.append(request.method)
        if not healthy:
            if outage == "disconnect":
                request.transport.close()
                return web.Response()
            return web.Response(status=503)
        return web.json_response(
            [
                {
                    "gatewayId": "gateway",
                    "resourcePaths": [
                        {
                            "resourcePath": path,
                            "serverStatus": 200,
                            "gatewayResponse": {
                                "status": 200,
                                "payload": {
                                    "value": 21,
                                    "type": "floatValue",
                                    "unit": "C",
                                },
                            },
                        }
                    ],
                }
            ]
        )

    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    oauth = AsyncMock()
    manager = TokenManager(
        oauth, AuthTokens("valid", "refresh", 10**12), lambda _: None
    )
    async with (
        serve([("POST", "/bulk", response)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = PointTClient(
            session,
            manager,
            base_url=url,
            retry_policy=RetryPolicy(base_delay=0, maximum_delay=0),
        )
        coordinator = BoschBuderusDataUpdateCoordinator(
            hass, client, Gateway("gateway"), entry
        )
        original = Resource(
            path,
            value=20,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
        )
        coordinator.resources = {path: original}
        coordinator.data = {path: ResourceSnapshot(original, True, datetime.now(UTC))}
        coordinator._paths_by_group = {PollGroup.FAST: (path,)}
        coordinator._next_update = {PollGroup.FAST: 0}
        with (
            patch(ROOT + ".coordinator.monotonic", side_effect=lambda: now),
            patch.object(entry, "async_start_reauth") as reauth,
            patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
        ):
            for _ in range(CIRCUIT_FAILURE_THRESHOLD):
                await coordinator.async_refresh()
                assert not coordinator.last_update_success
                assert not coordinator.data[path].available
                now += 60
            assert len(calls) == 2 * CIRCUIT_FAILURE_THRESHOLD
            until = coordinator._circuit_open_until
            now = until - 1
            await coordinator.async_refresh()
            assert len(calls) == 2 * CIRCUIT_FAILURE_THRESHOLD
            healthy = True
            now = until
            await coordinator.async_refresh()
            assert coordinator.last_update_success
            assert coordinator.data[path].available
            assert coordinator.data[path].freshness is Freshness.FRESH
            assert coordinator.data[path].resource.value == 21
            assert len(calls) == 2 * CIRCUIT_FAILURE_THRESHOLD + 1
            assert client.metrics.snapshot()["requests_total"] == len(calls)
            assert coordinator._gateway_failure_count == 0
            assert coordinator._circuit_open_until == 0
            reauth.assert_not_called()
            reload_entry.assert_not_called()
            oauth.refresh.assert_not_awaited()
        await coordinator.async_shutdown()


async def test_reconfigure_and_poll_share_one_refresh_rotation(hass):
    original = AuthTokens("old", "single-use", 0)
    updated = AuthTokens("new", "rotated", 10**12)
    entry = MockConfigEntry(domain=DOMAIN, data=tokens_to_data(original))
    entry.add_to_hass(hass)
    calls = []

    async def refresh(token):
        calls.append(token)
        await asyncio.sleep(0)
        if len(calls) > 1:
            raise RefreshTokenRejected("Already consumed")
        return updated

    oauth = AsyncMock()
    oauth.refresh.side_effect = refresh
    manager, backoff = entry_auth(hass, entry, oauth)
    client = PointTClient(async_get_clientsession(hass), manager, backoff=backoff)
    entry.runtime_data = BoschBuderusRuntimeData(client, manager, (), ())
    flow = BoschBuderusConfigFlow()
    flow.hass = hass
    flow._reconfigure_entry = entry
    with (
        patch(
            "custom_components.bosch_buderus_heating.config_flow.OAuthClient.refresh",
            side_effect=refresh,
        ),
        patch(
            "custom_components.bosch_buderus_heating.pointt.transport."
            "PointTTransport.request_json_with_sequence",
            AsyncMock(return_value=([], 1)),
        ),
    ):
        results = await asyncio.gather(
            client.get_gateways(),
            flow._reconfigure_client().get_gateways(),
            return_exceptions=True,
        )
    assert results == [(), ()]
    assert calls == ["single-use"]


@pytest.mark.parametrize("error", [RequestTimeout(), ServiceUnavailable()])
async def test_concurrent_transient_refresh_failure_recovers_next_cycle(error):
    entered = asyncio.Event()
    release = asyncio.Event()
    updated = AuthTokens("new", "rotated", 10**12)
    calls = []

    async def refresh(token):
        calls.append(token)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            raise error
        return updated

    oauth = AsyncMock()
    oauth.refresh.side_effect = refresh
    saved = []
    manager = TokenManager(oauth, AuthTokens("old", "refresh", 0), saved.append)
    waiters = [asyncio.create_task(manager.get_access_token()) for _ in range(8)]
    await entered.wait()
    release.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(result, type(error)) for result in results)
    assert calls == ["refresh"]
    assert not saved
    assert await manager.get_access_token() == "new"
    assert calls == ["refresh", "refresh"]
    assert saved == [updated]


async def test_rejected_refresh_is_not_repeated_by_other_gateways():
    oauth = AsyncMock()
    oauth.refresh.side_effect = RefreshTokenRejected("Rejected")
    manager = TokenManager(oauth, AuthTokens("old", "refresh", 0), lambda _: None)
    for _ in range(5):
        with pytest.raises(RefreshTokenRejected):
            await manager.get_access_token(force_refresh=True)
    oauth.refresh.assert_awaited_once_with("refresh")


async def test_oauth_rate_limit_blocks_repeated_refresh_and_recovers():
    now = 1000.0
    backoff = RateLimitBackoff(clock=lambda: now)
    updated = AuthTokens("new", "rotated", 10**12)
    oauth = AsyncMock()
    oauth.refresh.side_effect = [RateLimited(120), updated]
    manager = TokenManager(
        oauth, AuthTokens("old", "refresh", 0), lambda _: None, backoff=backoff
    )
    with pytest.raises(RateLimited):
        await manager.get_access_token()
    now += 119
    for _ in range(3):
        with pytest.raises(RateLimited):
            await manager.get_access_token(force_refresh=True)
    oauth.refresh.assert_awaited_once()
    now += 1
    assert await manager.get_access_token() == "new"
    assert oauth.refresh.await_count == 2


async def test_reauth_does_not_use_deprecated_reload_helper(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"gateway_ids": ["gateway"], **tokens_to_data(AuthTokens("a", "r", 0))},
    )
    entry.add_to_hass(hass)
    remove = entry.add_update_listener(AsyncMock())
    flow = BoschBuderusConfigFlow()
    flow.hass = hass
    flow._reauth_entry = entry
    flow._tokens = AuthTokens("new", "new-refresh", 10**12)
    try:
        with (
            patch.object(
                PointTClient, "get_gateways", return_value=(Gateway("gateway"),)
            ),
            patch.object(flow, "_discover_bacon", AsyncMock()),
            patch("homeassistant.config_entries.report_usage") as usage,
            patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
        ):
            result = await flow._async_discover_gateways()
            await hass.async_block_till_done()
        assert result["reason"] == "reauth_successful"
        reload_entry.assert_called_once_with(entry.entry_id)
        usage.assert_not_called()
    finally:
        remove()


async def test_cancelled_refresh_waiter_does_not_lose_rotated_tokens():
    entered = asyncio.Event()
    release = asyncio.Event()
    persisted = []
    updated = AuthTokens("new", "rotated", 10**12)
    calls = []

    async def refresh(token):
        calls.append(token)
        if len(calls) > 1:
            raise RefreshTokenRejected("Already consumed")
        entered.set()
        await release.wait()
        return updated

    oauth = AsyncMock()
    oauth.refresh.side_effect = refresh
    manager = TokenManager(oauth, AuthTokens("old", "single-use", 0), persisted.append)
    waiter = asyncio.create_task(manager.get_access_token())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert await manager.get_access_token() == "new"
    assert persisted == [updated]
    assert calls == ["single-use"]


async def test_following_requests_wait_for_token_persistence_after_cancellation():
    entered = asyncio.Event()
    release = asyncio.Event()
    updated = AuthTokens("new", "rotated", 10**12)
    saved = []

    async def persist(tokens):
        entered.set()
        await release.wait()
        saved.append(tokens)

    oauth = AsyncMock()
    oauth.refresh.return_value = updated
    manager = TokenManager(oauth, AuthTokens("old", "refresh", 0), persist)
    owner = asyncio.create_task(manager.get_access_token())
    await entered.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    follower = asyncio.create_task(manager.get_access_token())
    await asyncio.sleep(0)
    assert not follower.done()
    release.set()
    assert await follower == "new"
    assert saved == [updated]
    oauth.refresh.assert_awaited_once()
