"""Stopped PointT sources cannot send queued requests, refreshes or retries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating.pointt import PointTClient
from custom_components.bosch_buderus_heating.pointt.exceptions import SourceStopped
from custom_components.bosch_buderus_heating.pointt.transport import PointTTransport
from tests.test_pointt_transport_and_client import serve

pytestmark = pytest.mark.usefixtures("socket_enabled")


class Scope:
    active = True

    def check(self):
        if not self.active:
            raise SourceStopped()


@pytest.mark.parametrize("stage", ["before_token", "during_token", "waiting_for_slot"])
async def test_stopped_source_does_not_reach_http(stage):
    scope = Scope()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def token(**kwargs):
        entered.set()
        if stage == "during_token":
            await release.wait()
        return "synthetic"

    async def response(request):
        calls.append(request.path)
        return web.json_response({"value": 22})

    provider = SimpleNamespace(get_access_token=AsyncMock(side_effect=token))
    gate = asyncio.Semaphore(0 if stage == "waiting_for_slot" else 1)
    async with (
        serve([("GET", "/gateways/gateway/resource/value", response)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = PointTClient(
            session,
            provider,
            base_url=url,
            request_guard=scope.check,
            request_semaphore=gate,
        )
        if stage == "before_token":
            scope.active = False
        task = asyncio.create_task(client.get_resource("gateway", "/value"))
        if stage != "before_token":
            await entered.wait()
            await asyncio.sleep(0)
            scope.active = False
        release.set()
        gate.release()
        with pytest.raises(SourceStopped):
            await task
    assert not calls
    if stage == "before_token":
        provider.get_access_token.assert_not_awaited()


async def test_stale_401_does_not_force_another_token_refresh():
    scope = Scope()
    calls = []

    async def rejected(request):
        calls.append(request.path)
        scope.active = False
        return web.Response(status=401)

    provider = SimpleNamespace(get_access_token=AsyncMock(return_value="synthetic"))
    async with (
        serve([("GET", "/gateways/gateway/resource/value", rejected)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = PointTClient(
            session, provider, base_url=url, request_guard=scope.check
        )
        with pytest.raises(SourceStopped):
            await client.get_resource("gateway", "/value")
    assert len(calls) == 1
    provider.get_access_token.assert_awaited_once_with()


async def test_stopping_during_retry_delay_prevents_second_http_attempt():
    scope = Scope()
    calls = []

    async def failed(request):
        calls.append(request.path)
        return web.Response(status=503)

    async def delay(seconds):
        scope.active = False

    async with (
        serve([("GET", "/value", failed)]) as url,
        aiohttp.ClientSession() as session,
    ):
        transport = PointTTransport(
            session, base_url=url, request_guard=scope.check, sleep=delay
        )
        with pytest.raises(SourceStopped):
            await transport.request_json("GET", "/value", "synthetic", retryable=True)
    assert calls == ["/value"]


async def test_separate_clients_obey_one_shared_request_limit():
    release, filled = asyncio.Event(), asyncio.Event()
    active, peak, requests = 0, 0, 0

    async def response(request):
        nonlocal active, peak, requests
        active += 1
        requests += 1
        peak = max(peak, active)
        if active == 2:
            filled.set()
        try:
            await release.wait()
            return web.json_response({"value": 22})
        finally:
            active -= 1

    async with (
        serve([("GET", "/gateways/{gateway}/resource/value", response)]) as url,
        aiohttp.ClientSession() as session,
    ):
        semaphore = asyncio.Semaphore(2)
        clients = [
            PointTClient(
                session,
                "synthetic",
                base_url=url,
                gateway_id=f"g{i}",
                request_semaphore=semaphore,
            )
            for i in range(5)
        ]
        tasks = [
            asyncio.create_task(client.get_resource(f"g{i}", "/value"))
            for i, client in enumerate(clients)
        ]
        try:
            async with asyncio.timeout(3):
                await filled.wait()
            assert requests == 2 and peak == 2
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert peak == 2 and requests == 5


@pytest.mark.parametrize(
    "operation",
    [
        "gateway",
        "part",
        "resource",
        "bulk",
        "put",
        "holiday_create",
        "holiday_update",
        "holiday_delete",
        "icom",
        "wallbox",
    ],
)
async def test_gateway_source_rejects_misdirected_reads_and_writes(operation):
    provider = SimpleNamespace(get_access_token=AsyncMock(return_value="synthetic"))
    async with aiohttp.ClientSession() as session:
        client = PointTClient(session, provider, gateway_id="correct")
        operations = {
            "gateway": lambda: client.get_gateway("wrong"),
            "part": lambda: client.get_part_number("wrong"),
            "resource": lambda: client.get_resource("wrong", "/path"),
            "bulk": lambda: client.get_resources_bulk("wrong", ["/path"]),
            "put": lambda: client.put_resource_value("wrong", "/path", 22),
            "holiday_create": lambda: client.create_holiday_period("wrong", {}),
            "holiday_update": lambda: client.update_holiday_period("wrong", 1, {}),
            "holiday_delete": lambda: client.delete_holiday_period("wrong", 1),
            "icom": lambda: client.put_icom_holiday_assignments("wrong", 1, ("hc1",)),
            "wallbox": lambda: client.get_wallbox_section("wrong", "info"),
        }
        with pytest.raises(ValueError, match="source binding"):
            await operations[operation]()
        provider.get_access_token.assert_not_awaited()


async def test_gateway_source_does_not_enumerate_account():
    provider = SimpleNamespace(get_access_token=AsyncMock(return_value="synthetic"))
    async with aiohttp.ClientSession() as session:
        client = PointTClient(session, provider, gateway_id="correct")
        with pytest.raises(ValueError, match="enumerate"):
            await client.get_gateways()
        provider.get_access_token.assert_not_awaited()
