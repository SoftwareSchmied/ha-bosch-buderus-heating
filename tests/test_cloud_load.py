"""Shared cloud slots with several gateways: fairness, bounds and visibility."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating.const import DOMAIN, PollingProfile
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    PointTClient,
    RequestMetrics,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from tests.ha_test_helpers import MockConfigEntry
from tests.test_pointt_transport_and_client import serve

GATEWAYS = 3
PATHS = 120
LATENCY = 0.01


def _coordinator(hass, client, number: int, paths: int):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway(f"gw{number}"), entry, PollingProfile.STANDARD
    )
    resources = {
        path: Resource(
            path,
            value=21.5,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
        )
        for path in (
            f"/heatingCircuits/hc{index % 8 + 1}/synthetic{number}_{index}"
            for index in range(paths)
        )
    }
    coordinator.resources = resources
    coordinator.data = {
        path: ResourceSnapshot(resource, True, datetime.now(UTC))
        for path, resource in resources.items()
    }
    coordinator._paths_by_group = {PollGroup.FAST: tuple(resources)}
    coordinator._next_update = {group: 0.0 for group in coordinator._poll_intervals}
    return coordinator


@pytest.mark.usefixtures("socket_enabled")
async def test_gateways_share_three_slots_fairly_with_management(hass) -> None:
    active = 0
    peak = 0
    batches: dict[str, int] = {}

    async def bulk(request: web.Request) -> web.Response:
        nonlocal active, peak
        body = await request.json()
        gateway = body[0]["gatewayId"]
        batches[gateway] = batches.get(gateway, 0) + 1
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(LATENCY)
        finally:
            active -= 1
        return web.json_response(
            [
                {
                    "gatewayId": gateway,
                    "resourcePaths": [
                        {
                            "resourcePath": path,
                            "serverStatus": 200,
                            "gatewayResponse": {
                                "status": 200,
                                "payload": {
                                    "value": 21.5,
                                    "type": "floatValue",
                                    "unit": "C",
                                },
                            },
                        }
                        for path in body[0]["resourcePaths"]
                    ],
                }
            ]
        )

    async def gateways(_request: web.Request) -> web.Response:
        return web.json_response([])

    metrics = RequestMetrics()
    async with (
        serve([("POST", "/bulk", bulk), ("GET", "/gateways/", gateways)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = PointTClient(
            session,
            "synthetic",
            base_url=url,
            metrics=metrics,
            request_semaphore=asyncio.Semaphore(3),
        )
        coordinators = [
            _coordinator(hass, client, number, PATHS) for number in range(GATEWAYS)
        ]

        async def management() -> None:
            await asyncio.sleep(LATENCY)
            await client.get_gateways()

        await asyncio.gather(
            *(coordinator._async_update_data() for coordinator in coordinators),
            management(),
        )
        summaries = [coordinator.diagnostics_summary() for coordinator in coordinators]
        for coordinator in coordinators:
            await coordinator.async_shutdown()

    assert peak == 3
    assert batches == {f"gw{number}": 4 for number in range(GATEWAYS)}
    snapshot = metrics.snapshot()
    assert snapshot["requests_total"] == GATEWAYS * 4 + 1
    assert snapshot["slot_wait"]["waits_total"] == snapshot["requests_total"]
    assert snapshot["slot_wait"]["maximum_ms"] > 0
    assert snapshot["slot_wait"]["waits_over_10_seconds"] == 0
    for summary in summaries:
        assert summary["polls_total"] == 1
        assert summary["maximum_poll_duration_ms"] > 0
        assert summary["poll_interval_seconds"] == 60
        assert summary["polls_longer_than_interval"] == 0


async def test_cycle_longer_than_interval_is_counted_without_overlap(hass) -> None:
    client = AsyncMock()
    client.metrics = RequestMetrics()
    coordinator = _coordinator(hass, client, 0, 1)
    path = next(iter(coordinator.resources))
    client.get_resources_bulk.return_value = (
        BatchItemResult("gw0", path, 200, coordinator.resources[path]),
    )
    clock = [1000.0]

    async def slow(*_args, **_kwargs):
        clock[0] += 75
        return client.get_resources_bulk.return_value

    client.get_resources_bulk.side_effect = slow
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.monotonic",
        side_effect=lambda: clock[0],
    ):
        await coordinator._async_update_data()
        assert coordinator.update_interval == timedelta(seconds=60)
        summary = coordinator.diagnostics_summary()
    assert summary["polls_longer_than_interval"] == 1
    assert summary["maximum_poll_duration_ms"] >= 75000
    assert client.get_resources_bulk.await_count == 1
    assert coordinator.diagnostics_summary()["polls_total"] == 1
    await coordinator.async_shutdown()


async def test_unpolled_coordinator_reports_no_cycle_duration(hass) -> None:
    client = AsyncMock()
    client.metrics = RequestMetrics()
    coordinator = _coordinator(hass, client, 0, 1)
    summary = coordinator.diagnostics_summary()
    assert summary["maximum_poll_duration_ms"] is None
    assert summary["polls_longer_than_interval"] == 0
    await coordinator.async_shutdown()
