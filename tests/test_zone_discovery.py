"""Discovery of a zone controller with radio room devices (synthetic).

The fixture has the shape of a real report: eight zones with gaps in their
numbers, twelve radio devices that refuse the same eight leaves each, a
second heating circuit and three heat sources whose directories are refused
(their names are outside the ``hsN`` scheme, as the redacted report shows
them as ``{hs}``), and zone side directories (``heating``, ``cooling``) that are refused while
their leaves are readable. No payload of a real installation is used.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.bosch_buderus_heating.circuit_controls import (
    room_controller_check_complete,
)
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.discovery import (
    MAX_EXTRA_PROBES,
    ROOT_RESOURCE_PATHS,
    DiscoveryDiagnostics,
    DiscoveryPathSource,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.discovery_zones import (
    IDENTICAL_DEVICE_REFUSED,
    ZONE_SIDE_LEAVES,
    ZONE_SIDES,
    Admission,
    IdenticalDeviceRefusals,
    zone_side_paths,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    Resource,
    ResourceMetadata,
    ResourceReference,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.startup_data import StartupGatewayData

FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures/contracts/connectkey/zone-climate.json"
    ).read_text(encoding="utf-8")
)
ZONES = (1, 2, 4, 5, 6, 7, 8, 10)
ROOTS = (
    "/heatSources/hs1/numberOfStarts",
    "/heatSources/numberOfStarts",
    *ROOT_RESOURCE_PATHS,
)
RADIO_REFUSED = (
    "actuatorModulation",
    "actuatorPower",
    "currentRoomSetpoint",
    "operationMode",
    "rfConnectionStatus",
    "rfErrorCause",
    "rfPairedStatus",
    "signalIcon",
)
DIRECTORY = ResourceMetadata(resource_type="refEnum")


def fixture_tree() -> tuple[dict[str, Resource], set[str]]:
    resources = {item["id"]: parse_resource(item) for item in FIXTURE["resources"]}
    return resources, set(FIXTURE["refused"])


def response_client(resources, refused, statuses=None):
    """Answer only requested paths: a resource, 403, a given status or 404."""
    statuses = statuses or {}
    client = AsyncMock()
    client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
        BatchItemResult(gateway, path, 200, resource=resources[path])
        if path in resources
        else BatchItemResult(
            gateway, path, statuses.get(path, 403 if path in refused else 404)
        )
        for path in paths
    )
    return client


def requested(client) -> list[str]:
    return [
        p for call in client.get_resources_bulk.await_args_list for p in call.args[1]
    ]


def batch_of(client, path: str) -> int:
    for index, call in enumerate(client.get_resources_bulk.await_args_list):
        if path in call.args[1]:
            return index
    raise AssertionError(f"{path} was not requested")


def directory(path: str, *children: str) -> Resource:
    return Resource(
        path,
        metadata=DIRECTORY,
        references=tuple(ResourceReference(f"{path}{child}") for child in children),
    )


def value(path: str, reading: object = 1) -> Resource:
    return Resource(path, reading, True)


async def discover(resources, refused, statuses=None, **kwargs):
    client = response_client(resources, refused, statuses)
    report = DiscoveryDiagnostics()
    found = await async_discover_resources(
        client,
        "gateway",
        roots=kwargs.pop("roots", ROOTS),
        diagnostics=report,
        **kwargs,
    )
    return found, report, client


def side_paths(zone: int, side: str) -> list[str]:
    return [f"/zones/zone{zone}/{side}/{leaf}" for leaf in ZONE_SIDE_LEAVES]


async def test_zone_modes_and_setpoints_are_read_on_a_large_zone_controller():
    """The shape that ended its discovery before every zone leaf."""
    resources, refused = fixture_tree()

    found, report, client = await discover(resources, refused)

    paths = requested(client)
    assert all(count == 1 for count in Counter(paths).values())
    for zone in ZONES:
        for path in side_paths(zone, "heating"):
            assert path in found
            assert report.paths[path].source is DiscoveryPathSource.CATALOG
        for path in side_paths(zone, "cooling"):
            assert report.paths[path].bulk_result == "http_403"
    # They come right after their zone directory (48 paths: two batches),
    # long before the budget ends.
    assert all(
        batch_of(client, path) - batch_of(client, f"/zones/zone{zone}") in (1, 2)
        for zone in ZONES
        for side in ZONE_SIDES
        for path in side_paths(zone, side)
    )
    # Older side names stay optional probes; no zone outside the list.
    assert report.paths["/zones/zone1/heat/operationMode"].source is (
        DiscoveryPathSource.OPTIONAL
    )
    assert not any(p.startswith(("/zones/zone3", "/zones/zone9")) for p in paths)
    # Without the extra allowance the tree is larger than the budget; the
    # stop stays reported, with the limit that ended it.
    snapshot = report.snapshot()
    assert snapshot["paths_requested"] == 512
    assert report.stop_reason == "resource_limit"
    assert snapshot["resource_limit_scope"] == "shared_budget"
    assert snapshot["extra_probe_limit"] == 0
    assert not report.completed


async def test_a_leaf_refused_on_one_radio_device_is_not_requested_for_identical_ones():
    resources, refused = fixture_tree()

    _, report, client = await discover(resources, refused)

    paths = requested(client)
    for leaf in RADIO_REFUSED:
        asked = [p for p in paths if p.startswith("/devices/") and p.endswith(leaf)]
        assert len(asked) == 1, leaf
        skipped = [
            path
            for path, item in report.paths.items()
            if path.endswith(f"/{leaf}")
            and path.startswith("/devices/")
            and item.read_policy == IDENTICAL_DEVICE_REFUSED
        ]
        assert len(skipped) == 11, leaf
        assert all(report.paths[p].bulk_result == "not_attempted" for p in skipped)
        assert not any(report.paths[p].failed for p in skipped)
    # Every readable leaf is still read on every device.
    for n in range(1, 13):
        for leaf in ("battery", "signal", "zoneId", "assignedHC", "roomtemperature"):
            assert f"/devices/device{n}/{leaf}" in paths
    snapshot = report.snapshot()
    assert snapshot["paths_not_read"] == {IDENTICAL_DEVICE_REFUSED: 88}
    assert snapshot["forbidden_paths"] < 100


def radio_devices(*listings: tuple[str, ...]) -> dict[str, Resource]:
    resources: dict[str, Resource] = {
        "/devices": directory(
            "/devices", *(f"/device{n}" for n in range(1, len(listings) + 1))
        )
    }
    for n, leaves in enumerate(listings, start=1):
        root = f"/devices/device{n}"
        resources[root] = directory(root, *(f"/{leaf}" for leaf in leaves))
    return resources


THERMOSTAT = ("battery", "signal", "actuatorPower", "roomtemperature")
VALVE = ("battery", "signal", "actuatorPower", "valvePosition")


async def test_a_device_listing_other_children_keeps_its_own_requests():
    """A different kind of device may answer what a thermostat is refused."""
    resources = radio_devices(THERMOSTAT, THERMOSTAT, VALVE, THERMOSTAT)
    resources["/devices/device3/actuatorPower"] = value(
        "/devices/device3/actuatorPower", 40
    )
    refused = {f"/devices/device{n}/actuatorPower" for n in (1, 2, 4)}

    found, report, client = await discover(resources, refused, roots=("/devices",))

    assert "/devices/device3/actuatorPower" in found
    paths = requested(client)
    thermostats = [
        p for p in paths if p.endswith("/actuatorPower") and "device3" not in p
    ]
    assert len(thermostats) == 1
    assert report.completed


@pytest.mark.parametrize("status", [404, 406, 200])
async def test_any_other_answer_requests_the_leaf_on_every_identical_device(status):
    resources = radio_devices(THERMOSTAT, THERMOSTAT, THERMOSTAT)
    statuses = {}
    for n in (1, 2, 3):
        path = f"/devices/device{n}/actuatorPower"
        if status == 200:
            resources[path] = value(path, 10)
        else:
            statuses[path] = status

    _, report, client = await discover(resources, set(), statuses, roots=("/devices",))

    paths = requested(client)
    for n in (1, 2, 3):
        assert f"/devices/device{n}/actuatorPower" in paths
    assert not any(
        item.read_policy == IDENTICAL_DEVICE_REFUSED for item in report.paths.values()
    )


async def test_identical_devices_wait_for_the_first_answer_only_one_batch():
    resources = radio_devices(*(THERMOSTAT,) * 5)
    refused = {f"/devices/device{n}/actuatorPower" for n in range(1, 6)}

    _, report, client = await discover(resources, refused, roots=("/devices",))

    # The first device's leaves go first; every other device follows in the
    # next batch, without the refused leaf.
    first = batch_of(client, "/devices/device1/actuatorPower")
    for n in range(2, 6):
        assert batch_of(client, f"/devices/device{n}/battery") == first + 1
    for n in range(2, 6):
        assert f"/devices/device{n}/actuatorPower" not in requested(client)
    assert report.completed
    assert report.stop_reason == "complete"


async def test_a_zone_without_a_listed_side_gets_no_catalog_reads():
    zones = {
        "/zones": directory("/zones", "/zone1", "/zone2"),
        "/zones/zone1": directory("/zones/zone1", "/heating", "/name"),
        "/zones/zone2": directory("/zones/zone2", "/name"),
        "/zones/zone1/name": value("/zones/zone1/name", "Zone 1"),
        "/zones/zone2/name": value("/zones/zone2/name", "Zone 2"),
        "/zones/zone1/heating/operationMode": value(
            "/zones/zone1/heating/operationMode", "auto"
        ),
    }

    found, report, _ = await discover(
        zones, {"/zones/zone1/heating"}, roots=("/zones",)
    )

    assert "/zones/zone1/heating/operationMode" in found
    for path in side_paths(1, "heating"):
        assert report.paths[path].source is DiscoveryPathSource.CATALOG
    for path in side_paths(1, "cooling") + side_paths(2, "heating"):
        assert report.paths[path].source is DiscoveryPathSource.OPTIONAL
    assert report.completed


def test_zone_side_paths_only_for_listed_sides_of_connectkey_zones():
    zone = directory("/zones/zone3", "/cooling", "/name")
    assert zone_side_paths("/zones/zone3", zone) == tuple(side_paths(3, "cooling"))
    rrc = directory("/zones/zn1", "/heating")
    assert zone_side_paths("/zones/zn1", rrc) == ()
    nested = directory("/zones/zone3/heating", "/operationMode")
    assert zone_side_paths("/zones/zone3/heating", nested) == ()


def test_identical_device_groups_need_the_same_children_and_two_of_them():
    refusals = IdenticalDeviceRefusals()
    refusals.listed("/devices/device1", directory("/devices/device1", "/a", "/b"))
    refusals.listed("/devices/device2", directory("/devices/device2", "/a", "/b"))
    refusals.listed("/devices/device3", directory("/devices/device3", "/a", "/c"))
    refusals.listed("/devices/device4", directory("/devices/device4", "/a"))
    refusals.listed("/devices/list", directory("/devices/list", "/a", "/b"))
    refusals.begin_batch()
    assert refusals.admit("/devices/device1/a") is Admission.SEND
    assert refusals.admit("/devices/device2/a") is Admission.HOLD
    assert refusals.admit("/devices/device3/a") is Admission.SEND
    assert refusals.admit("/devices/device4/a") is Admission.SEND
    refusals.answered("/devices/device1/a", 403)
    refusals.begin_batch()
    assert refusals.admit("/devices/device2/a") is Admission.SKIP
    assert refusals.skipped == {"/devices/device2/a": "/devices/device1/a"}
    assert refusals.admit("/devices/device2/b") is Admission.SEND
    refusals.answered("/devices/device3/a", 403)  # a group of its own
    assert refusals.admit("/devices/device4/a") is Admission.SEND
    # Devices listing a single leaf are never grouped.
    refusals.listed("/devices/device5", directory("/devices/device5", "/a"))
    refusals.begin_batch()
    assert refusals.admit("/devices/device4/a") is Admission.SEND
    assert refusals.admit("/devices/device5/a") is Admission.SEND
    refusals.answered("/devices/device4/a", 403)
    refusals.begin_batch()
    assert refusals.admit("/devices/device5/a") is Admission.SEND


# The allowance outside the shared budget ----------------------------------


def extra_paths(client, report, after: int) -> list[str]:
    """The paths requested once ``after`` distinct paths had been sent."""
    seen: list[str] = []
    for path in requested(client):
        if path not in seen:
            seen.append(path)
    return seen[after:]


async def test_large_zone_controller_completes_with_the_extra_allowance():
    resources, refused = fixture_tree()

    found, report, client = await discover(
        resources, refused, maximum_extra_probes=MAX_EXTRA_PROBES
    )

    snapshot = report.snapshot()
    assert report.completed and report.stop_reason == "complete"
    assert snapshot["resource_limit_scope"] is None
    assert snapshot["extra_probe_limit"] == MAX_EXTRA_PROBES
    # Room to spare for a larger plant of the same kind.
    assert 0 < snapshot["extra_probes_requested"] <= MAX_EXTRA_PROBES - 50
    assert snapshot["paths_requested"] == 512 + snapshot["extra_probes_requested"]
    assert not any(
        item.bulk_result == "not_attempted" and item.read_policy == "normal"
        for item in report.paths.values()
    )
    # Only optional probes and direct reads used the allowance.
    direct = {p for record in report.direct_probes.values() for p in record.paths}
    for path in extra_paths(client, report, 512):
        assert (
            report.paths[path].source is DiscoveryPathSource.OPTIONAL or path in direct
        ), path
    for zone in ZONES:
        assert all(path in found for path in side_paths(zone, "heating"))


async def test_an_oversized_tree_stops_at_the_extra_allowance():
    resources, refused = fixture_tree()

    _, report, client = await discover(resources, refused, maximum_extra_probes=100)

    snapshot = report.snapshot()
    assert report.stop_reason == "resource_limit" and not report.completed
    assert snapshot["resource_limit_scope"] == "extra_allowance"
    assert snapshot["extra_probes_requested"] == 100
    assert len(set(requested(client))) == 612


async def test_references_left_after_the_budget_are_not_read_by_the_allowance():
    resources, refused = fixture_tree()

    _, report, client = await discover(
        resources, refused, maximum_resources=40, maximum_extra_probes=1000
    )

    snapshot = report.snapshot()
    assert report.stop_reason == "resource_limit"
    assert snapshot["resource_limit_scope"] == "shared_budget"
    direct = {p for record in report.direct_probes.values() for p in record.paths}
    extra = extra_paths(client, report, 40)
    assert extra and len(extra) == snapshot["extra_probes_requested"]
    assert all(
        report.paths[path].source is DiscoveryPathSource.OPTIONAL or path in direct
        for path in extra
    )
    assert any(
        item.source is DiscoveryPathSource.REFERENCE
        and item.bulk_result == "not_attempted"
        for item in report.paths.values()
    )


def canonical_heat_sources(resources, refused) -> None:
    """A synthetic variant: the refused heat sources are named ``hs2`` to
    ``hs4``, so their known children are read directly."""
    names = ("/hs2", "/hs3", "/hs4")
    listed = tuple(
        reference.path[len("/heatSources") :]
        for reference in resources["/heatSources"].references
        if "/source" not in reference.path
    )
    resources["/heatSources"] = directory("/heatSources", *listed, *names)
    refused.difference_update({f"/heatSources/source{n}" for n in "ABC"})
    refused.update(f"/heatSources{name}" for name in names)


SOLAR = (
    "/collectorTemperature",
    "/pumpModulation",
    "/solarYield",
    "/dhwTankBottomTemperature",
    "/actualPower",
    "/status",
)


async def test_heat_sources_outside_the_hs_scheme_get_no_direct_reads():
    """The real shape: refused names outside ``hsN`` are not circuits."""
    resources, refused = fixture_tree()

    _, report, _ = await discover(
        resources, refused, maximum_extra_probes=MAX_EXTRA_PROBES
    )

    assert report.completed
    assert not any(c.startswith("/heatSources/") for c in report.direct_probes)
    assert set(report.direct_probes) == {"/heatingCircuits/hc2"} | {
        c for c in report.direct_probes if c.startswith("/zones/")
    }


async def test_refused_canonical_heat_sources_are_read_directly():
    resources, refused = fixture_tree()
    canonical_heat_sources(resources, refused)

    _, report, _ = await discover(
        resources, refused, maximum_extra_probes=MAX_EXTRA_PROBES
    )

    assert report.completed and report.stop_reason == "complete"
    assert {"/heatSources/hs2", "/heatSources/hs3", "/heatSources/hs4"} <= set(
        report.direct_probes
    )
    assert report.extra_probes_requested <= MAX_EXTRA_PROBES - 50


async def test_a_directory_read_from_the_allowance_has_its_children_read():
    """A solar circuit only an optional probe reaches, read after the shared
    budget: its children are read from the allowance too, and the discovery
    finishes (the room controller check needs a finished one)."""
    resources, refused = fixture_tree()
    canonical_heat_sources(resources, refused)
    resources["/solarCircuits/sc1"] = directory("/solarCircuits/sc1", *SOLAR)
    for child in SOLAR:
        resources[f"/solarCircuits/sc1{child}"] = value(f"/solarCircuits/sc1{child}")

    found, report, client = await discover(
        resources, refused, maximum_extra_probes=MAX_EXTRA_PROBES
    )

    assert report.completed and report.stop_reason == "complete"
    late = extra_paths(client, report, 512)
    assert "/solarCircuits/sc1" in late
    for child in SOLAR:
        assert f"/solarCircuits/sc1{child}" in found
        assert f"/solarCircuits/sc1{child}" in late
    assert room_controller_check_complete(report)


async def test_the_allowance_reads_below_its_own_directories_only():
    """References of directories the shared budget read stay unread."""
    resources, refused = fixture_tree()
    resources["/solarCircuits/sc1"] = directory("/solarCircuits/sc1", *SOLAR)
    for child in SOLAR:
        resources[f"/solarCircuits/sc1{child}"] = value(f"/solarCircuits/sc1{child}")

    found, report, client = await discover(
        resources, refused, maximum_resources=40, maximum_extra_probes=1000
    )

    assert report.stop_reason == "resource_limit"
    late = set(extra_paths(client, report, 40))
    direct = {p for record in report.direct_probes.values() for p in record.paths}
    read_late = {p for p in late if p in found and p not in direct}
    for path in late:
        item = report.paths[path]
        if item.source is DiscoveryPathSource.OPTIONAL or path in direct:
            continue
        # Anything else read late lies below a directory read late.
        assert any(
            path.startswith(f"{parent}/") for parent in read_late
        ), path


@pytest.mark.parametrize("budget", [1000, 512 + 400])
async def test_a_discovery_within_the_budget_is_unchanged_by_the_allowance(budget):
    """Installations that never use up the shared budget behave as before."""
    resources, refused = fixture_tree()

    _, before, without = await discover(resources, refused, maximum_resources=budget)
    _, after, with_extra = await discover(
        resources, refused, maximum_resources=budget, maximum_extra_probes=400
    )

    assert [c.args for c in without.get_resources_bulk.await_args_list] == [
        c.args for c in with_extra.get_resources_bulk.await_args_list
    ]
    assert after.extra_probes_requested == 0
    assert before.completed and after.completed


async def test_extra_reads_are_not_charged_to_the_start_up_budget():
    resources, refused = fixture_tree()
    startup = StartupGatewayData()
    # Supplementary reads already took part of the shared budget: it ends
    # in the start-up data before the discovery's own count.
    startup.reserve_discovery(tuple(f"/system/earlier{n}" for n in range(100)))

    _, report, _ = await discover(
        resources,
        refused,
        startup_data=startup,
        maximum_extra_probes=MAX_EXTRA_PROBES,
    )

    assert report.completed
    assert report.extra_probes_requested > 0
    assert len(startup.requested) <= 512


async def test_the_start_up_discovery_passes_the_allowance(hass):
    coordinator = SimpleNamespace(
        client=AsyncMock(),
        gateway=Gateway("gateway"),
        registered_start_counter_paths=(),
        discovery_diagnostics=DiscoveryDiagnostics(),
        startup_data=None,
        rrc_evidence=lambda: (),
        _shared_pause_remaining=lambda: 0,
    )
    with (
        patch(
            "custom_components.bosch_buderus_heating.coordinator.async_discover_resources",
            AsyncMock(return_value={}),
        ) as discover_call,
        pytest.raises(UpdateFailed),
    ):
        await BoschBuderusDataUpdateCoordinator._async_discover_data(coordinator)
    assert discover_call.await_args.kwargs["maximum_extra_probes"] == MAX_EXTRA_PROBES
