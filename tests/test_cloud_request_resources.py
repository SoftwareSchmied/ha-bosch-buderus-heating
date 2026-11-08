"""Cloud management shares live HTTP slots without blocking local observations."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating import (
    cloud_configuration,
    configuration_management,
    managed_cloud,
)
from custom_components.bosch_buderus_heating.auth_lifecycle import (
    connection_backoff,
    connection_resources,
    entry_auth,
    forget_connection_auth,
    forget_entry_auth,
)
from custom_components.bosch_buderus_heating.bacon import flow as bacon_flow
from custom_components.bosch_buderus_heating.bacon.client import BaconClient
from custom_components.bosch_buderus_heating.bacon.models import BaconError
from custom_components.bosch_buderus_heating.cloud_configuration import (
    discover_connection,
)
from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    Brand,
    Gateway,
    OAuthClient,
    PointTClient,
    RateLimited,
    TokenManager,
)
from tests import test_source_isolation as isolation
from tests.test_bacon_contracts import token
from tests.test_config_flow import _redirect_from_result
from tests.test_connection_runtime import entry_for
from tests.test_managed_connections import choose_connection, open_reauth
from tests.test_managed_pointt import Backend, eventually
from tests.test_pointt_transport_and_client import serve

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
prepared = isolation.prepared
combined = isolation.combined


async def occupy(semaphore):
    for _ in range(3):
        await semaphore.acquire()


async def queued_or_sent(semaphore, task, sent):
    await eventually(lambda: semaphore._waiters or task.done() or sent())
    assert semaphore._waiters and not task.done()
    assert not sent()


def management_request(management, edit, operation, tokens, **kwargs):
    if operation == "reauth":
        return management.async_reauthenticate(edit, tokens, **kwargs)
    return discover_connection(management, edit, **kwargs)


@pytest.mark.parametrize("operation", ["reauth", "discovery"])
@pytest.mark.parametrize("outcome", ["complete", "cancel", "changed", "rate_limit"])
async def test_queued_management_preserves_local_publication(
    hass, monkeypatch, combined, operation, outcome
):
    p = combined
    connection = p.runtime.connection_driver(p.cloud)
    resources = connection.resources
    manager = p.runtime.management
    edit = manager.edit(p.cloud)
    monkeypatch.setattr(
        configuration_management, "PointTClient", managed_cloud.PointTClient
    )
    monkeypatch.setattr(cloud_configuration, "PointTClient", managed_cloud.PointTClient)
    devices = AsyncMock(
        return_value=(
            ("softener",),
            connection.configuration.cloud_data()["bacon_account"],
        )
    )
    monkeypatch.setattr(configuration_management, "discover", devices)
    monkeypatch.setattr(cloud_configuration, "discover", devices)
    previous = EntryConfiguration(p.entry, p.cloud).tokens()
    replacement = replace(
        previous, access_token="replacement", refresh_token="replacement-refresh"
    )
    await occupy(resources.semaphore)
    calls, counted = len(p.backend.calls), resources.metrics.requests_total
    task = asyncio.create_task(
        management_request(manager, edit, operation, replacement)
    )
    try:
        await queued_or_sent(
            resources.semaphore, task, lambda: len(p.backend.calls) != calls
        )
        old = p.runtime.local_state("local").current_observations[0]
        local = p.runtime.connection_driver("local").sources["local"]
        p.clock.advance(60)
        local.service.wake()
        await eventually(
            lambda: p.runtime.local_state("local").current_observations[0] is not old
        )
        assert resources.metrics.requests_total == counted
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if outcome == "changed":
                manager.set_enabled(edit, False)
            elif outcome == "rate_limit":
                connection.backoff.activate(900)
            resources.semaphore.release()
            if outcome == "complete":
                await asyncio.wait_for(task, 5)
                assert resources.metrics.requests_total >= counted + 1
                assert (
                    connection_resources(hass, p.entry.entry_id, p.cloud) is resources
                )
                assert EntryConfiguration(p.entry, p.cloud).tokens() == (
                    replacement if operation == "reauth" else previous
                )
                return
            with pytest.raises(
                ConfigurationError if outcome == "changed" else RateLimited
            ):
                await task
        assert len(p.backend.calls) == calls
        assert resources.metrics.requests_total == counted
        assert EntryConfiguration(p.entry, p.cloud).tokens() == previous
        devices.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(3 if outcome == "cancel" else 2):
            resources.semaphore.release()


@pytest.mark.parametrize("operation", ["reauth", "discovery"])
@pytest.mark.parametrize("outcome", ["complete", "cancel", "changed", "rate_limit"])
async def test_bacon_management_uses_same_slots_and_rechecks_after_wait(
    hass, monkeypatch, operation, outcome
):
    entry, cloud, _ = entry_for(hass)
    manager = ConfigurationManagement(hass, entry)
    edit = manager.edit(cloud)
    original = EntryConfiguration(entry, cloud).tokens()
    tokens = replace(original, expires_at=10**12)
    # Existing discovery uses its retained manager; replacement verification must
    # instead use the submitted tokens without rotating the saved credentials.
    retained, _ = entry_auth(hass, entry, OAuthClient(None), connection_id=cloud)
    monkeypatch.setattr(
        retained, "get_access_token", AsyncMock(return_value=tokens.access_token)
    )
    gateways = AsyncMock(return_value=(Gateway("000123"), Gateway("gateway-b")))
    monkeypatch.setattr(PointTClient, "get_gateways", gateways)
    resources = connection_resources(hass, entry.entry_id, cloud)
    calls = []

    async def devices(request):
        calls.append(request.headers["Authorization"])
        return web.json_response(["softener"])

    async with serve([("GET", "/", devices)]) as url:
        monkeypatch.setattr(
            bacon_flow, "BaconClient", lambda *a, **kw: BaconClient(*a, **kw, url=url)
        )
        await occupy(resources.semaphore)
        task = asyncio.create_task(management_request(manager, edit, operation, tokens))
        try:
            await queued_or_sent(resources.semaphore, task, lambda: bool(calls))
            gateways.assert_awaited_once()
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                if outcome == "changed":
                    manager.set_enabled(edit, False)
                elif outcome == "rate_limit":
                    connection_backoff(hass, entry.entry_id, cloud).activate(900)
                resources.semaphore.release()
                if outcome == "complete":
                    await asyncio.wait_for(task, 5)
                    assert calls == ["Bearer " + tokens.access_token]
                    assert resources.metrics.requests_total == 0
                    return
                with pytest.raises(
                    ConfigurationError if outcome == "changed" else BaconError
                ):
                    await task
            assert not calls and EntryConfiguration(entry, cloud).tokens() == original
            assert resources.metrics.requests_total == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for _ in range(3 if outcome == "cancel" else 2):
                resources.semaphore.release()


@pytest.mark.parametrize("operation", ["reauth", "discovery"])
@pytest.mark.parametrize("backend", ["pointt", "bacon"])
async def test_closed_native_dialog_cannot_send_queued_management_request(
    hass, monkeypatch, operation, backend
):
    entry, cloud, _ = entry_for(hass)
    original = deepcopy(dict(entry.data))
    tokens = replace(EntryConfiguration(entry, cloud).tokens(), expires_at=10**12)
    monkeypatch.setattr(OAuthClient, "exchange_code", AsyncMock(return_value=tokens))
    monkeypatch.setattr(OAuthClient, "refresh", AsyncMock(return_value=tokens))
    server = Backend()
    calls = []

    async def devices(request):
        calls.append("bacon")
        return web.json_response(["softener"])

    async with serve([*server.routes, ("GET", "/claim", devices)]) as url:
        for module in (cloud_configuration, configuration_management):
            monkeypatch.setattr(
                module,
                "PointTClient",
                lambda *a, **kw: PointTClient(*a, **kw, base_url=url),
            )
        monkeypatch.setattr(
            bacon_flow,
            "BaconClient",
            lambda *a, **kw: BaconClient(*a, **kw, url=url + "/claim"),
        )
        if backend == "bacon":
            monkeypatch.setattr(
                PointTClient,
                "get_gateways",
                AsyncMock(return_value=(Gateway("000123"), Gateway("gateway-b"))),
            )
        if operation == "reauth":
            flow = await open_reauth(hass, entry, cloud)
            flows = hass.config_entries.flow
            payload = {"redirect_url": _redirect_from_result(flow, Brand.BUDERUS)}
        else:
            flow = await choose_connection(hass, entry, cloud)
            flows = hass.config_entries.options
            payload = {"next_step_id": "connection_discover"}
        resources = connection_resources(hass, entry.entry_id, cloud)
        await occupy(resources.semaphore)
        task = asyncio.create_task(flows.async_configure(flow["flow_id"], payload))
        try:
            await queued_or_sent(
                resources.semaphore, task, lambda: bool(calls or server.calls)
            )
            # Rotate tokens before this snapshot if discovery refreshed them.
            before_abort = deepcopy(dict(entry.data))
            flows.async_abort(flow["flow_id"])
            resources.semaphore.release()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
            assert not calls and not server.calls
            assert resources.metrics.requests_total == 0
            assert entry.data == before_abort
            assert (
                entry.data["connections"][cloud]["auth_epoch"]
                == original["connections"][cloud]["auth_epoch"]
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            resources.semaphore.release()
            resources.semaphore.release()


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_bacon_refresh_releases_http_slot_and_preserves_rotation(expired, cancel):
    semaphore = asyncio.Semaphore(1)
    old = AuthTokens(
        token(signature="old"),
        "old-refresh",
        0 if expired else 10**12,
        scope=("bacon",),
    )
    new = replace(
        old,
        access_token=token(signature="new"),
        refresh_token="new-refresh",
        expires_at=10**12,
    )
    entered, resume = asyncio.Event(), asyncio.Event()
    saved, requests = [], []

    async def refresh(refresh_token):
        assert refresh_token == "old-refresh"
        entered.set()
        await resume.wait()
        return new

    oauth = SimpleNamespace(refresh=AsyncMock(side_effect=refresh))
    manager = TokenManager(oauth, old, saved.append)

    async def devices(request):
        requests.append(request.headers["Authorization"])
        if requests[-1] == "Bearer " + old.access_token:
            return web.Response(status=401)
        return web.json_response(["softener"])

    async with (
        serve([("GET", "/", devices)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = BaconClient(session, manager, url=url, request_semaphore=semaphore)
        task = asyncio.create_task(client.get_devices())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            # Another HTTP operation can use the slot while OAuth is pending.
            await asyncio.wait_for(semaphore.acquire(), 1)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            resume.set()
            await asyncio.wait_for(manager.async_wait_for_refresh(), 5)
            assert saved == [new]
            semaphore.release()
            if not cancel:
                assert await asyncio.wait_for(task, 5) == ("softener",)
            assert requests == ([] if expired else ["Bearer " + old.access_token]) + (
                [] if cancel else ["Bearer " + new.access_token]
            )
            oauth.refresh.assert_awaited_once()
        finally:
            resume.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_request_resources_are_scoped_and_forgotten_only_on_removal(hass):
    entry, cloud, _ = entry_for(hass, second_cloud=True)
    resources = connection_resources(hass, entry.entry_id, cloud)
    other = connection_resources(hass, entry.entry_id, "cloud-b")
    owner = connection_resources(hass, "other-owner", cloud)
    assert resources is connection_resources(hass, entry.entry_id, cloud)
    resources.metrics.requests_total = 7
    forget_connection_auth(hass, entry.entry_id, cloud)
    assert connection_resources(hass, entry.entry_id, cloud).metrics.requests_total == 0
    assert connection_resources(hass, entry.entry_id, "cloud-b") is other
    assert connection_resources(hass, "other-owner", cloud) is owner
    forget_entry_auth(hass, entry)
    assert connection_resources(hass, entry.entry_id, "cloud-b") is not other
    assert connection_resources(hass, "other-owner", cloud) is owner


@pytest.mark.parametrize("reject_first_token", [False, True])
async def test_mixed_cloud_requests_share_three_slots_and_one_token_rotation(
    hass, monkeypatch, reject_first_token
):
    entry, cloud, _ = entry_for(hass)
    old = replace(EntryConfiguration(entry, cloud).tokens(), expires_at=10**12)
    new = replace(
        old,
        access_token=token("synthetic-account", signature="rotated"),
        refresh_token="rotated-refresh",
    )
    data = deepcopy(dict(entry.data))
    data["connections"][cloud]["credentials"].update(tokens_to_data(old))
    hass.config_entries.async_update_entry(entry, data=data)
    # Only the rejected signature causes HTTP 401. The retained provider performs
    # the real serialized refresh and persists it in the same entry.
    entered, resume = asyncio.Event(), asyncio.Event()
    oauth = SimpleNamespace(refresh=AsyncMock(return_value=new))
    manager, backoff = entry_auth(hass, entry, oauth, connection_id=cloud)
    resources = connection_resources(hass, entry.entry_id, cloud)
    calls, active, peak = [], 0, 0

    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append((request.path, request.headers["Authorization"]))
        if active == 3:
            entered.set()
        try:
            await resume.wait()
            if (
                reject_first_token
                and request.headers["Authorization"] == "Bearer " + old.access_token
            ):
                return web.Response(status=401)
            return web.json_response(
                [{"id": "000123"}, {"id": "gateway-b"}]
                if request.path == "/gateways/"
                else ["softener"]
            )
        finally:
            active -= 1

    async with (
        serve([("GET", "/gateways/", respond), ("GET", "/claim", respond)]) as url,
        aiohttp.ClientSession() as session,
    ):
        monkeypatch.setattr(
            cloud_configuration,
            "PointTClient",
            lambda *a, **kw: PointTClient(*a, **kw, base_url=url),
        )
        monkeypatch.setattr(
            bacon_flow,
            "BaconClient",
            lambda *a, **kw: BaconClient(*a, **kw, url=url + "/claim"),
        )
        pointt = PointTClient(
            session,
            manager,
            base_url=url,
            backoff=backoff,
            metrics=resources.metrics,
            request_semaphore=resources.semaphore,
        )
        bacon = BaconClient(
            session,
            manager,
            url=url + "/claim",
            backoff=backoff,
            request_semaphore=resources.semaphore,
        )
        management = ConfigurationManagement(hass, entry)
        tasks = [
            asyncio.create_task(call)
            for call in (
                pointt.get_gateways(),
                bacon.get_devices(),
                discover_connection(management, management.edit(cloud)),
                pointt.get_gateways(),
                bacon.get_devices(),
                pointt.get_gateways(),
                bacon.get_devices(),
            )
        ]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert len(calls) == 3 and resources.semaphore.locked()
            resume.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            assert peak == 3
            pointt_attempts = sum(path == "/gateways/" for path, _ in calls)
            assert resources.metrics.requests_total == pointt_attempts
            assert len(calls) > pointt_attempts >= 4
            if reject_first_token:
                oauth.refresh.assert_awaited_once()
                assert EntryConfiguration(entry, cloud).tokens() == new
                assert resources.metrics.retry_attempts > 0
            else:
                oauth.refresh.assert_not_awaited()
                assert pointt_attempts == 4
        finally:
            resume.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
