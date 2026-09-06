"""Regression tests for discovery batching, priority, and failure diagnostics."""

from unittest.mock import AsyncMock, call, patch

import pytest

from custom_components.bosch_buderus_heating.discovery import (
    DiscoveryDiagnostics,
    DiscoveryPathSource,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    BatchItemResult,
    InvalidPayload,
    PointTClient,
    PointTError,
    RateLimited,
    RequestTimeout,
    Resource,
    ResourceError,
    ResourceReference,
    ServiceUnavailable,
    TransportError,
)


async def test_discovery_recovers_only_the_failed_physical_batch() -> None:
    """A bad middle envelope must not discard or replace other bulk chunks."""
    client = PointTClient(AsyncMock(), "token")
    paths = tuple(f"/root{index}" for index in range(65))
    batches = []

    async def request(_method, _path, *, json_body, **_kwargs):
        chunk = tuple(json_body[0]["resourcePaths"])
        batches.append(chunk)
        if chunk[0] == paths[30]:
            return [], 0
        return [
            {
                "gatewayId": "gateway",
                "resourcePaths": [
                    {
                        "resourcePath": path,
                        "serverStatus": 200,
                        "gatewayResponse": {"status": 200, "payload": {}},
                    }
                    for path in chunk
                ],
            }
        ], 0

    report = DiscoveryDiagnostics()
    with (
        patch.object(client, "_request_with_sequence", side_effect=request),
        patch.object(
            client,
            "get_resource",
            side_effect=lambda gateway, path, **kwargs: Resource(path),
        ) as fallback,
    ):
        discovered = await async_discover_resources(
            client, "gateway", roots=paths, diagnostics=report
        )

    assert batches == [paths[:30], paths[30:60], paths[60:]]
    assert fallback.await_args_list == [
        call("gateway", path, fallback_reason="malformed") for path in paths[30:60]
    ]
    assert tuple(discovered) == paths
    assert report.bulk_calls == 3
    assert report.paths[paths[0]].bulk_result == "success"
    assert report.paths[paths[30]].bulk_result == "malformed"
    assert report.paths[paths[-1]].bulk_result == "success"
    assert report.snapshot()["fallback_attempts"] == 30


@pytest.mark.parametrize("stage", ["bulk", "fallback"])
@pytest.mark.parametrize(
    ("error", "category"),
    [
        (AuthenticationError(), "authentication_error"),
        (RateLimited(60), "rate_limited"),
        (RequestTimeout(), "timeout"),
        (ServiceUnavailable(503), "service_unavailable"),
        (TransportError(), "transport_error"),
    ],
)
async def test_discovery_records_request_wide_abort_without_more_reads(
    stage: str, error: PointTError, category: str
) -> None:
    client = AsyncMock()
    paths = tuple(f"/root{index}" for index in range(35))
    if stage == "bulk":
        client.get_resources_bulk.side_effect = error
    else:
        client.get_resources_bulk.side_effect = lambda gateway, chunk: tuple(
            BatchItemResult(gateway, path, 200, error=InvalidPayload())
            for path in chunk
        )
        client.get_resource.side_effect = [Resource(paths[0]), error]
    report = DiscoveryDiagnostics()

    with pytest.raises(type(error)) as raised:
        await async_discover_resources(
            client, "gateway", roots=paths, diagnostics=report
        )

    assert raised.value is error
    client.get_resources_bulk.assert_awaited_once_with("gateway", paths[:30])
    assert not report.completed
    assert report.stop_reason == category
    assert report.paths[paths[30]].bulk_result == "not_attempted"
    assert report.snapshot()["paths_requested"] == 30
    if stage == "bulk":
        client.get_resource.assert_not_awaited()
        assert report.paths[paths[0]].bulk_result == category
    else:
        assert client.get_resource.await_count == 2
        assert report.paths[paths[1]].fallback_result == category
        assert report.snapshot()["fallback_attempts"] == 2
        assert report.snapshot()["fallback_successes"] == 1
        assert report.snapshot()["fallback_failures"] == 1


@pytest.mark.parametrize("error", [PointTError(), ResourceError("/root", 403)])
async def test_discovery_records_other_bulk_errors(error: PointTError) -> None:
    client = AsyncMock()
    client.get_resources_bulk.side_effect = error
    report = DiscoveryDiagnostics()
    with pytest.raises(type(error)):
        await async_discover_resources(
            client, "gateway", roots=("/root",), diagnostics=report
        )
    assert report.stop_reason in ("request_failed", "http_403")
    assert report.paths["/root"].bulk_result == report.stop_reason
    client.get_resource.assert_not_awaited()


async def test_discovery_keeps_local_failures_local() -> None:
    client = AsyncMock()
    paths = tuple(f"/root{index}" for index in range(9))
    client.get_resources_bulk.return_value = tuple(
        BatchItemResult("gateway", path, status, error=ResourceError(path, status))
        for path, status in zip(paths[:3], (403, 404, 406), strict=True)
    ) + tuple(
        BatchItemResult("gateway", path, 200, error=InvalidPayload())
        for path in paths[3:]
    )
    client.get_resource.side_effect = [
        ResourceError(paths[3], 404),
        InvalidPayload(),
        PointTError(),
        ResourceError(paths[6], 403),
        ResourceError(paths[7], 406),
        Resource(paths[8]),
    ]
    report = DiscoveryDiagnostics()

    discovered = await async_discover_resources(
        client, "gateway", roots=paths, diagnostics=report
    )

    assert tuple(discovered) == (paths[8],)
    assert client.get_resource.await_count == 6
    assert report.completed
    assert report.snapshot()["fallback_failures"] == 5
    assert report.paths[paths[4]].fallback_result == "malformed"
    assert report.paths[paths[5]].fallback_result == "request_failed"
    assert all(report.paths[path].fallback_reason is None for path in paths[:3])


@pytest.mark.parametrize(
    ("has_child", "maximum_depth", "reason"),
    [(True, 8, "resource_limit"), (True, 0, "depth_limit"), (False, 8, "complete")],
)
async def test_discovery_reports_truncation_at_exact_resource_boundary(
    has_child: bool, maximum_depth: int, reason: str
) -> None:
    client = AsyncMock()
    root = Resource(
        "/root",
        references=(ResourceReference("/root/child"),) if has_child else (),
    )
    client.get_resources_bulk.return_value = (
        BatchItemResult("gateway", root.path, 200, root),
    )
    report = DiscoveryDiagnostics()

    discovered = await async_discover_resources(
        client,
        "gateway",
        roots=(root.path,),
        maximum_resources=1,
        maximum_depth=maximum_depth,
        diagnostics=report,
    )

    assert discovered == {root.path: root}
    assert report.stop_reason == reason
    assert report.completed == (reason == "complete")
    assert report.depth_limit_reached == (reason == "depth_limit")
    if has_child:
        assert report.paths["/root/child"].bulk_result == "not_attempted"


async def test_discovery_preserves_results_when_processed_optional_is_referenced() -> (
    None
):
    client = AsyncMock()
    resources = {
        "/zones": Resource("/zones"),
        "/zones/configuration": Resource("/zones/configuration"),
        "/zones/list": Resource(
            "/zones/list", references=(ResourceReference("/zones/configuration"),)
        ),
    }
    client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
        BatchItemResult(gateway, path, 200, resources[path]) for path in paths
    )
    report = DiscoveryDiagnostics()

    discovered = await async_discover_resources(
        client, "gateway", roots=("/zones",), maximum_resources=3, diagnostics=report
    )

    assert discovered == resources
    assert report.completed
    item = report.paths["/zones/configuration"]
    assert item.source is DiscoveryPathSource.REFERENCE
    assert item.discovered
    assert item.bulk_result == "success"
    assert client.get_resources_bulk.await_count == 2


async def test_new_references_interrupt_remaining_optional_batches() -> None:
    client = AsyncMock()

    def response(gateway, paths):
        return tuple(
            BatchItemResult(
                gateway,
                path,
                200,
                Resource(
                    path,
                    references=(ResourceReference("/system/reported"),)
                    if path == "/system/appliance/enabled"
                    else (),
                ),
            )
            for path in paths
        )

    client.get_resources_bulk.side_effect = response
    await async_discover_resources(client, "gateway", roots=("/system", "/heatSources"))

    calls = client.get_resources_bulk.await_args_list
    assert len(calls[1].args[1]) == 30
    assert calls[2] == call("gateway", ("/system/reported",))
    assert len(calls) == 4
