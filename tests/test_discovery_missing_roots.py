"""A gateway answers 5xx for roots it does not have (504 for /ventilation and
/zones, 500 for /pool): the discovery goes on while the cloud answers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating.discovery import (
    MAX_CONSECUTIVE_MISSING_5XX,
    ROOT_RESOURCE_PATHS,
    DiscoveryDiagnostics,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Resource,
    ResourceError,
    ServiceUnavailable,
)

ABSENT = ("/pool", "/pv", "/ventilation", "/zones")


def _client(absent: set[str], status: int = 504) -> AsyncMock:
    client = AsyncMock()

    def bulk(gateway, chunk):
        return tuple(
            BatchItemResult(gateway, path, status, error=ResourceError(path, status))
            if path in absent
            else BatchItemResult(gateway, path, 200, Resource(path))
            for path in chunk
        )

    client.get_resources_bulk.side_effect = bulk
    client.get_resource.side_effect = ServiceUnavailable(status)
    return client


@pytest.mark.parametrize("count", [2, 3, 4])
@pytest.mark.parametrize("status", [500, 504])
async def test_absent_roots_answering_5xx_do_not_end_the_discovery(
    count: int, status: int
) -> None:
    absent = set(ABSENT[:count])
    assert absent <= set(ROOT_RESOURCE_PATHS)
    client = _client(absent, status)
    report = DiscoveryDiagnostics()

    found = await async_discover_resources(
        client, "gateway", roots=ROOT_RESOURCE_PATHS, diagnostics=report
    )

    assert report.completed and report.stop_reason == "complete"
    assert absent.isdisjoint(found)
    assert set(ROOT_RESOURCE_PATHS) - absent <= set(found)
    for path in absent:
        assert report.paths[path].fallback_result == f"http_{status}"
        assert report.paths[path].read_policy == "optional_5xx_assumed_missing"


async def test_a_cloud_that_answers_nothing_still_ends_the_discovery() -> None:
    """Every root 5xx, no resource in the batch: that is an outage."""
    client = _client(set(ROOT_RESOURCE_PATHS), 503)
    report = DiscoveryDiagnostics()

    with pytest.raises(ServiceUnavailable):
        await async_discover_resources(
            client, "gateway", roots=ROOT_RESOURCE_PATHS, diagnostics=report
        )

    assert not report.completed
    assert report.stop_reason == "service_unavailable"


async def test_many_5xx_roots_in_a_row_are_still_an_outage() -> None:
    """The allowance is bounded: ten absent roots in a row end the discovery."""
    paths = tuple(f"/root{index}" for index in range(20))
    client = _client(set(paths[2:18]), 504)
    report = DiscoveryDiagnostics()

    with pytest.raises(ServiceUnavailable):
        await async_discover_resources(
            client, "gateway", roots=paths, diagnostics=report
        )

    assert not report.completed
    assert client.get_resource.await_count == MAX_CONSECUTIVE_MISSING_5XX
