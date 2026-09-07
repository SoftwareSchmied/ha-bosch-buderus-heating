"""Synthetic regressions for advertised circuits with unreadable directories."""

from collections import Counter
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating.discovery import (
    DiscoveryDiagnostics,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Resource,
    ResourceReference,
)

ROOT = "/heatingCircuits"
CORE_SUFFIXES = (
    "/operationMode",
    "/manualRoomSetpoint",
    "/controlType",
    "/temperatureLevels",
    "/temperatureLevels/comfort2",
    "/temperatureLevels/eco",
)


def response_client(resources, failures=None):
    """Return synthetic responses only for requested paths, never real payloads."""
    failures = failures or {}
    client = AsyncMock()
    client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
        BatchItemResult(gateway, path, 200, resource=resources[path])
        if path in resources
        else BatchItemResult(gateway, path, failures.get(path, 404))
        for path in paths
    )
    return client


def advertised(*circuits):
    return Resource(
        ROOT, references=tuple(ResourceReference(f"{ROOT}/{c}") for c in circuits)
    )


@pytest.mark.parametrize("parent_status", [403, 404, 406])
@pytest.mark.parametrize("levels_status", [403, 404, 406])
async def test_readable_leaves_survive_unreadable_circuit_and_temperature_directories(
    parent_status, levels_status
):
    circuit = f"{ROOT}/hc2"
    resources = {
        ROOT: advertised("hc1", "hc2"),
        f"{ROOT}/hc1": Resource(f"{ROOT}/hc1"),
        f"{circuit}/operationMode": Resource(f"{circuit}/operationMode"),
        f"{circuit}/temperatureLevels/comfort2": Resource(
            f"{circuit}/temperatureLevels/comfort2"
        ),
        f"{circuit}/temperatureLevels/eco": Resource(
            f"{circuit}/temperatureLevels/eco"
        ),
    }
    client = response_client(
        resources,
        {circuit: parent_status, f"{circuit}/temperatureLevels": levels_status},
    )
    report = DiscoveryDiagnostics()

    discovered = await async_discover_resources(
        client, "gateway", roots=(ROOT,), diagnostics=report
    )

    assert discovered == resources
    for suffix in CORE_SUFFIXES:
        assert report.paths[f"{circuit}{suffix}"].source.value == "catalog"
    assert report.paths[circuit].bulk_result == f"http_{parent_status}"
    assert report.paths[f"{circuit}/temperatureLevels"].bulk_result == (
        f"http_{levels_status}"
    )
    assert report.completed
    client.get_resource.assert_not_awaited()


@pytest.mark.parametrize("circuits", [("hc1",), ("hc1", "hc3"), ("hc2", "hc7")])
async def test_catalog_is_limited_to_advertised_circuits_and_does_not_invent_resources(
    circuits,
):
    resources = {ROOT: advertised(*circuits)}
    client = response_client(resources)
    report = DiscoveryDiagnostics()

    discovered = await async_discover_resources(
        client, "gateway", roots=(ROOT,), diagnostics=report
    )

    assert discovered == resources
    requested = [
        p for c in client.get_resources_bulk.await_args_list for p in c.args[1]
    ]
    assert {p.split("/")[2] for p in requested if p != ROOT} == set(circuits)
    assert all(count == 1 for count in Counter(requested).values())
    assert report.snapshot()["catalog_paths"] == len(circuits) * len(CORE_SUFFIXES)
    assert report.snapshot()["catalog_paths_discovered"] == 0
    client.get_resource.assert_not_awaited()


async def test_partial_references_promote_catalog_paths_without_duplicate_reads():
    circuit = f"{ROOT}/hc2"
    mode = f"{circuit}/operationMode"
    levels = f"{circuit}/temperatureLevels"
    comfort = f"{levels}/comfort2"
    resources = {
        ROOT: advertised("hc2"),
        circuit: Resource(circuit, references=(ResourceReference(mode),)),
        mode: Resource(mode),
        levels: Resource(levels, references=(ResourceReference(comfort),)),
        comfort: Resource(comfort),
    }
    client = response_client(resources)
    report = DiscoveryDiagnostics()

    assert (
        await async_discover_resources(
            client, "gateway", roots=(ROOT,), diagnostics=report
        )
        == resources
    )

    requested = [
        p for c in client.get_resources_bulk.await_args_list for p in c.args[1]
    ]
    assert len(requested) == len(set(requested))
    assert requested.index(mode) < requested.index(levels)
    assert report.paths[mode].source.value == "reference"
    assert report.paths[comfort].source.value == "reference"
    assert report.paths[comfort].discovered
    assert report.snapshot()["catalog_paths"] == 4


async def test_references_then_catalog_precede_optional_probes_near_path_limit():
    circuit = f"{ROOT}/hc2"
    references = tuple(f"{ROOT}/reported{index}" for index in range(502))
    resources = {
        ROOT: Resource(
            ROOT,
            references=tuple(ResourceReference(p) for p in (*references, circuit)),
        ),
        **{p: Resource(p) for p in references},
        **{f"{circuit}{s}": Resource(f"{circuit}{s}") for s in CORE_SUFFIXES},
    }
    client = response_client(resources)
    report = DiscoveryDiagnostics()

    discovered = await async_discover_resources(
        client, "gateway", roots=(ROOT,), diagnostics=report
    )

    assert discovered == resources
    batches = [c.args[1] for c in client.get_resources_bulk.await_args_list]
    requested = [p for batch in batches for p in batch]
    assert len(requested) == 512
    assert max(map(len, batches)) <= 30
    assert requested[1:504] == [*references, circuit]
    assert requested[504:510] == [f"{circuit}{s}" for s in CORE_SUFFIXES]
    assert report.stop_reason == "resource_limit"
    assert report.snapshot()["catalog_paths_discovered"] == 6


async def test_catalog_respects_depth_bound_and_reports_unattempted_paths():
    resources = {ROOT: advertised("hc2")}
    client = response_client(resources)
    report = DiscoveryDiagnostics()

    await async_discover_resources(
        client, "gateway", roots=(ROOT,), maximum_depth=1, diagnostics=report
    )

    assert report.stop_reason == "depth_limit"
    assert report.paths[f"{ROOT}/hc2/operationMode"].bulk_result == "not_attempted"
    assert [c.args[1] for c in client.get_resources_bulk.await_args_list] == [
        (ROOT,),
        (f"{ROOT}/hc2",),
    ]
