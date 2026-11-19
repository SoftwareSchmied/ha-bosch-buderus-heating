"""Direct reads below circuit containers that cannot be listed (synthetic).

The first fixture has the shape of a real report: the second heating circuit
answers its directory with HTTP 403 while its values are readable one by one.
No payload of a real installation is used.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import discovery_containers
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.device import grouped_entity_name
from custom_components.bosch_buderus_heating.discovery import (
    DiscoveryDiagnostics,
    DiscoveryPathSource,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.discovery_containers import (
    ContainerProbes,
    family_of,
    known_child_suffixes,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    Resource,
    ResourceMetadata,
    ResourceReference,
)
from tests.ha_test_helpers import MockConfigEntry

HC = "/heatingCircuits"
DIRECTORY = ResourceMetadata(resource_type="refEnum")


def directory(path: str, *children: str) -> Resource:
    return Resource(
        path,
        metadata=DIRECTORY,
        references=tuple(ResourceReference(f"{path}{child}") for child in children),
    )


def value(path: str, reading: object = 1) -> Resource:
    return Resource(path, reading, True)


def response_client(resources, failures=None):
    """Answer only requested paths: a resource, a given status or 404."""
    failures = failures or {}
    client = AsyncMock()
    client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
        BatchItemResult(gateway, path, 200, resource=resources[path])
        if path in resources
        else BatchItemResult(gateway, path, failures.get(path, 404))
        for path in paths
    )
    return client


def requested(client) -> list[str]:
    return [
        p for call in client.get_resources_bulk.await_args_list for p in call.args[1]
    ]


def issue_shape() -> tuple[dict[str, Resource], dict[str, int]]:
    """hc1 lists its children; hc2 refuses its directory and its programs."""
    hc1 = f"{HC}/hc1"
    hc2 = f"{HC}/hc2"
    resources = {
        HC: directory(HC, "/hc1", "/hc2"),
        hc1: directory(
            hc1,
            "/name",
            "/currentRoomSetpoint",
            "/overallStatus",
            "/operationMode",
            "/switchPrograms",
            "/vendorOnlyValue",
        ),
        f"{hc1}/name": value(f"{hc1}/name", "Erdgeschoss"),
        f"{hc1}/currentRoomSetpoint": value(f"{hc1}/currentRoomSetpoint", 21.0),
        f"{hc1}/overallStatus": value(f"{hc1}/overallStatus", "ok"),
        f"{hc1}/operationMode": value(f"{hc1}/operationMode", "auto"),
        f"{hc1}/switchPrograms": directory(f"{hc1}/switchPrograms", "/A", "/C"),
        f"{hc1}/switchPrograms/A": value(f"{hc1}/switchPrograms/A"),
        f"{hc1}/switchPrograms/C": value(f"{hc1}/switchPrograms/C"),
        f"{hc1}/vendorOnlyValue": value(f"{hc1}/vendorOnlyValue"),
        f"{hc2}/name": value(f"{hc2}/name", "Obergeschoss"),
        f"{hc2}/currentRoomSetpoint": value(f"{hc2}/currentRoomSetpoint", 20.5),
        f"{hc2}/operationMode": value(f"{hc2}/operationMode", "auto"),
        f"{hc2}/temporaryRoomSetpoint": value(f"{hc2}/temporaryRoomSetpoint", -1),
        f"{hc2}/vendorOnlyValue": value(f"{hc2}/vendorOnlyValue"),
        f"{hc2}/switchPrograms/A": value(f"{hc2}/switchPrograms/A"),
        f"{hc2}/switchPrograms/C": value(f"{hc2}/switchPrograms/C"),
    }
    failures = {hc2: 403, f"{hc2}/switchPrograms": 403}
    return resources, failures


async def discover(resources, failures=None, **kwargs):
    client = response_client(resources, failures)
    report = DiscoveryDiagnostics()
    found = await async_discover_resources(
        client,
        "gateway",
        roots=kwargs.pop("roots", (HC,)),
        diagnostics=report,
        **kwargs,
    )
    return found, report, client


async def test_refused_circuit_directory_reads_name_setpoint_and_program():
    resources, failures = issue_shape()
    hc2 = f"{HC}/hc2"

    found, report, client = await discover(resources, failures)

    assert found == resources
    for path in (f"{hc2}/name", f"{hc2}/currentRoomSetpoint"):
        assert report.paths[path].source is DiscoveryPathSource.CATALOG
        assert report.paths[path].discovered
    # Only a sibling lists it, the catalog does not: still read directly.
    assert report.paths[f"{hc2}/vendorOnlyValue"].discovered
    # An always-requested optional leaf keeps its source and is read once.
    assert report.paths[f"{hc2}/temporaryRoomSetpoint"].source is (
        DiscoveryPathSource.OPTIONAL
    )
    assert f"{hc2}/temporaryRoomSetpoint" not in report.direct_probes[hc2].paths
    # The refused program directory below it is read the same way.
    assert report.paths[f"{hc2}/switchPrograms/A"].discovered
    assert report.paths[f"{hc2}/switchPrograms/C"].discovered
    paths = requested(client)
    assert all(count == 1 for count in Counter(paths).values())
    assert report.completed
    client.get_resource.assert_not_awaited()

    snapshot = report.snapshot()
    probes = {entry["container"]: entry for entry in snapshot["direct_probes"]}
    assert set(probes) == {hc2, f"{hc2}/switchPrograms"}
    assert probes[hc2]["reason"] == "http_403"
    assert probes[hc2]["reference_containers"] == [f"{HC}/hc1"]
    assert probes[f"{hc2}/switchPrograms"]["reference_containers"] == [
        f"{HC}/hc1/switchPrograms"
    ]
    assert probes[hc2]["resources_discovered"] >= 4
    assert snapshot["direct_probe_containers"] == 2
    assert snapshot["direct_probe_paths"] == sum(
        entry["paths_scheduled"] for entry in probes.values()
    )
    assert snapshot["direct_probe_paths_discovered"] == sum(
        entry["resources_discovered"] for entry in probes.values()
    )
    assert snapshot["direct_probe_paths_over_limit"] == 0


async def test_refused_directory_order_does_not_matter():
    """The refusal may be read before or after the sibling list."""
    resources, failures = issue_shape()
    resources[HC] = directory(HC, "/hc2", "/hc1")

    found, report, _ = await discover(resources, failures)

    assert found == resources
    assert report.direct_probes[f"{HC}/hc2"].references == {f"{HC}/hc1"}


async def test_single_refused_circuit_uses_the_catalog_of_known_children():
    hc2 = f"{HC}/hc2"
    resources = {
        HC: directory(HC, "/hc2"),
        f"{hc2}/name": value(f"{hc2}/name", "Obergeschoss"),
        f"{hc2}/currentRoomSetpoint": value(f"{hc2}/currentRoomSetpoint", 20.0),
        f"{hc2}/overallStatus": value(f"{hc2}/overallStatus", "ok"),
        f"{hc2}/activeSwitchProgram": value(f"{hc2}/activeSwitchProgram", "A"),
        f"{hc2}/currentSuWiMode": value(f"{hc2}/currentSuWiMode", "forced"),
        f"{hc2}/switchPrograms/A": value(f"{hc2}/switchPrograms/A"),
    }

    found, report, client = await discover(resources, {hc2: 403})

    assert found == resources
    assert report.direct_probes[hc2].reason == "http_403"
    assert report.direct_probes[hc2].references == set()
    paths = requested(client)
    assert all(count == 1 for count in Counter(paths).values())
    assert {p.split("/")[2] for p in paths if p != HC} == {"hc2"}


@pytest.mark.parametrize(("status", "probed"), [(403, True), (406, True), (404, False)])
async def test_only_a_refused_directory_is_probed(status, probed):
    hc2 = f"{HC}/hc2"
    resources = {
        HC: directory(HC, "/hc1", "/hc2"),
        f"{HC}/hc1": directory(f"{HC}/hc1", "/name"),
        f"{HC}/hc1/name": value(f"{HC}/hc1/name", "EG"),
        f"{hc2}/name": value(f"{hc2}/name", "OG"),
    }

    found, report, client = await discover(resources, {hc2: status})

    assert (f"{hc2}/name" in requested(client)) is probed
    assert (f"{hc2}/name" in found) is probed
    assert (hc2 in report.direct_probes) is probed
    if probed:
        assert report.direct_probes[hc2].reason == f"http_{status}"


async def test_not_advertised_circuits_are_never_probed():
    resources, failures = issue_shape()
    resources[HC] = directory(HC, "/hc1")

    _, report, client = await discover(resources, failures)

    assert not any(p.startswith(f"{HC}/hc2") for p in requested(client))
    assert report.direct_probes == {}


async def test_readable_complete_siblings_cost_no_extra_request():
    """A tree with fully listed circuits is read exactly as before."""
    hc1, hc2 = f"{HC}/hc1", f"{HC}/hc2"
    resources = {
        HC: directory(HC, "/hc1", "/hc2"),
        hc1: directory(hc1, "/name"),
        hc2: directory(hc2, "/name"),
        f"{hc1}/name": value(f"{hc1}/name", "EG"),
        f"{hc2}/name": value(f"{hc2}/name", "OG"),
    }

    found, report, client = await discover(resources)

    assert found == resources
    assert report.direct_probes == {}
    assert report.snapshot()["direct_probe_containers"] == 0
    # Root, both circuits, their names, and the fixed catalog and optional
    # leaves every advertised circuit gets anyway.
    assert not any(
        p.endswith(("/currentRoomSetpoint", "/overallStatus", "/switchPrograms"))
        for p in requested(client)
    )


async def test_shorter_sibling_list_reads_the_missing_children_as_optional():
    hc1, hc2 = f"{HC}/hc1", f"{HC}/hc2"
    resources = {
        HC: directory(HC, "/hc1", "/hc2"),
        hc1: directory(hc1, "/name", "/overallStatus"),
        hc2: directory(hc2, "/name", "/heatCurve"),
        f"{hc1}/name": value(f"{hc1}/name"),
        f"{hc1}/overallStatus": value(f"{hc1}/overallStatus"),
        f"{hc2}/name": value(f"{hc2}/name"),
        f"{hc2}/heatCurve": value(f"{hc2}/heatCurve"),
        f"{hc2}/overallStatus": value(f"{hc2}/overallStatus"),
    }

    found, report, client = await discover(resources)

    assert found == resources
    assert report.paths[f"{hc2}/overallStatus"].source is DiscoveryPathSource.OPTIONAL
    assert report.direct_probes[hc2].reason == "missing_children"
    # Both ways: hc1 is asked for what only hc2 lists (it answers 404).
    assert report.paths[f"{hc1}/heatCurve"].bulk_result == "http_404"
    assert report.direct_probes[hc1].references == {hc2}
    assert all(count == 1 for count in Counter(requested(client)).values())


async def test_an_empty_zone_without_sibling_is_a_valid_answer():
    """Like the ventilation zone of a heat pump without ventilation."""
    vent = "/ventilation"
    resources = {
        vent: directory(vent, "/zone1"),
        f"{vent}/zone1": directory(f"{vent}/zone1"),
        # Readable, but the directory does not list it: never asked for.
        f"{vent}/zone1/applianceRunTime": value(f"{vent}/zone1/applianceRunTime"),
    }

    found, report, client = await discover(resources, roots=(vent,))

    assert report.direct_probes == {}
    assert report.snapshot()["direct_probe_paths"] == 0
    assert f"{vent}/zone1/applianceRunTime" not in requested(client)
    assert f"{vent}/zone1/applianceRunTime" not in found


@pytest.mark.parametrize("order", [("/hc1", "/hc2"), ("/hc2", "/hc1")])
async def test_an_empty_directory_next_to_a_listing_sibling_is_read(order):
    """Whichever circuit answers first, the empty one gets the sibling's list."""
    hc1, hc2 = f"{HC}/hc1", f"{HC}/hc2"
    resources = {
        HC: directory(HC, *order),
        hc1: directory(hc1),
        hc2: directory(hc2, "/name", "/vendorOnlyValue"),
        f"{hc1}/name": value(f"{hc1}/name", "EG"),
        f"{hc1}/vendorOnlyValue": value(f"{hc1}/vendorOnlyValue"),
        f"{hc2}/name": value(f"{hc2}/name", "OG"),
        f"{hc2}/vendorOnlyValue": value(f"{hc2}/vendorOnlyValue"),
    }

    found, report, client = await discover(resources)

    assert found == resources
    assert report.direct_probes[hc1].reason == "empty_listing"
    assert report.direct_probes[hc1].references == {hc2}
    assert hc2 not in report.direct_probes
    assert report.paths[f"{hc1}/vendorOnlyValue"].source is (
        DiscoveryPathSource.OPTIONAL
    )
    assert all(count == 1 for count in Counter(requested(client)).values())


def test_an_empty_directory_alone_plans_nothing():
    records: dict = {}
    probes = ContainerProbes(
        known_child_suffixes(), known=lambda _: False, records=records
    )
    zone = directory("/ventilation/zone1")

    assert probes.listed(zone.path, zone, 1) == []
    assert records == {}


async def test_hot_water_heat_source_solar_ventilation_and_zones():
    dhw, hs, sc, vent, zones = (
        "/dhwCircuits",
        "/heatSources",
        "/solarCircuits",
        "/ventilation",
        "/zones",
    )
    resources = {
        dhw: directory(dhw, "/dhw1", "/dhw2"),
        f"{dhw}/dhw1": directory(f"{dhw}/dhw1", "/currentSetpoint", "/charge"),
        f"{dhw}/dhw1/currentSetpoint": value(f"{dhw}/dhw1/currentSetpoint", 50),
        f"{dhw}/dhw1/charge": value(f"{dhw}/dhw1/charge", "stop"),
        f"{dhw}/dhw2/currentSetpoint": value(f"{dhw}/dhw2/currentSetpoint", 48),
        f"{dhw}/dhw2/name": value(f"{dhw}/dhw2/name", "Hot water 2"),
        hs: directory(hs, "/hs1"),
        # An empty directory without a listing sibling is a valid answer.
        f"{hs}/hs1": directory(f"{hs}/hs1"),
        sc: directory(sc, "/sc1", "/sc2"),
        f"{sc}/sc1": directory(f"{sc}/sc1", "/collectorTemperature", "/extraYield"),
        f"{sc}/sc1/collectorTemperature": value(f"{sc}/sc1/collectorTemperature"),
        f"{sc}/sc1/extraYield": value(f"{sc}/sc1/extraYield"),
        f"{sc}/sc2": directory(f"{sc}/sc2"),
        f"{sc}/sc2/extraYield": value(f"{sc}/sc2/extraYield"),
        vent: directory(vent, "/zone1"),
        f"{vent}/zone1/applianceRunTime": value(f"{vent}/zone1/applianceRunTime"),
        zones: directory(zones, "/zone1", "/zone2"),
        f"{zones}/zone1": directory(f"{zones}/zone1", "/name", "/icon", "/extra"),
        f"{zones}/zone1/name": value(f"{zones}/zone1/name", "Bad"),
        f"{zones}/zone1/icon": value(f"{zones}/zone1/icon", "bath"),
        f"{zones}/zone1/extra": value(f"{zones}/zone1/extra"),
        f"{zones}/zone2/name": value(f"{zones}/zone2/name", "Kind"),
        f"{zones}/zone2/extra": value(f"{zones}/zone2/extra"),
    }
    failures = {f"{dhw}/dhw2": 403, f"{vent}/zone1": 403, f"{zones}/zone2": 403}

    found, report, client = await discover(
        resources, failures, roots=(dhw, hs, sc, vent, zones)
    )

    assert found == resources
    reasons = {path: record.reason for path, record in report.direct_probes.items()}
    assert reasons == {
        f"{dhw}/dhw2": "http_403",
        # Empty, but its sibling sc1 lists children.
        f"{sc}/sc2": "empty_listing",
        f"{vent}/zone1": "http_403",
        f"{zones}/zone2": "http_403",
    }
    assert all(count == 1 for count in Counter(requested(client)).values())


async def test_refused_nested_directory_without_sibling_uses_known_children():
    """Like a refused hot-water program directory on a real installation."""
    dhw, hs = "/dhwCircuits/dhw1", "/heatSources/hs1"
    resources = {
        "/dhwCircuits": directory("/dhwCircuits", "/dhw1"),
        dhw: directory(dhw, "/switchPrograms"),
        f"{dhw}/switchPrograms/A": value(f"{dhw}/switchPrograms/A"),
        f"{dhw}/switchPrograms/B": value(f"{dhw}/switchPrograms/B"),
        "/heatSources": directory("/heatSources", "/hs1"),
        hs: directory(hs, "/refrigerant"),
        f"{hs}/refrigerant/hotGasTemp": value(f"{hs}/refrigerant/hotGasTemp"),
    }
    failures = {f"{dhw}/switchPrograms": 403, f"{hs}/refrigerant": 403}

    found, report, _ = await discover(
        resources, failures, roots=("/dhwCircuits", "/heatSources")
    )

    assert found == resources
    assert set(report.direct_probes) == {
        f"{dhw}/switchPrograms",
        f"{hs}/refrigerant",
    }
    assert all(
        path.startswith(f"{container}/")
        for container, record in report.direct_probes.items()
        for path in record.paths
    )


async def test_direct_reads_stay_within_the_discovery_budget():
    resources, failures = issue_shape()

    _, report, client = await discover(resources, failures, maximum_resources=12)

    assert len(requested(client)) == 12
    assert report.stop_reason == "resource_limit"


async def test_direct_reads_respect_the_depth_bound():
    resources, failures = issue_shape()

    _, report, client = await discover(resources, failures, maximum_depth=2)

    paths = requested(client)
    assert f"{HC}/hc2/name" in paths
    assert f"{HC}/hc2/switchPrograms/A" not in paths
    assert report.depth_limit_reached
    assert report.paths[f"{HC}/hc2/switchPrograms/A"].bulk_result == "not_attempted"


async def test_rediscovery_of_a_refused_circuit_probes_it_again():
    resources, failures = issue_shape()

    found, report, _ = await discover(
        resources, failures, roots=(f"{HC}/hc2",), rediscovery=True
    )

    assert f"{HC}/hc2/name" in found
    assert report.direct_probes[f"{HC}/hc2"].reason == "http_403"


def test_probe_limit_is_counted_not_requested():
    records: dict = {}
    known: set[str] = set()
    probes = ContainerProbes(
        known_child_suffixes(), known=known.__contains__, records=records, limit=3
    )

    planned = probes.refused(f"{HC}/hc2", 403, 1)

    assert len(planned) == 3
    assert len(probes.over_limit) > 0
    assert records[f"{HC}/hc2"].paths == [probe.path for probe in planned]
    assert probes.refused(f"{HC}/hc3", 403, 1) == []


def test_early_containers_leave_direct_reads_for_the_others():
    """Many refused heat sources first: the heating circuit planned later
    still gets its direct reads, also below its refused programs."""
    records: dict = {}
    probes = ContainerProbes(
        known_child_suffixes(), known=lambda _: False, records=records
    )
    for n in range(1, 13):
        probes.refused(f"/heatSources/hs{n}", 403, 1)
    heat_sources = sum(
        len(record.paths)
        for container, record in records.items()
        if container.startswith("/heatSources/")
    )
    assert heat_sources == discovery_containers.MAX_DIRECT_PROBES_PER_KIND

    planned = {probe.path for probe in probes.refused(f"{HC}/hc2", 403, 1)}
    assert {f"{HC}/hc2/name", f"{HC}/hc2/switchPrograms/A"} <= planned
    nested = probes.refused(f"{HC}/hc2/cooling/switchPrograms", 403, 2)
    assert [probe.path for probe in nested] == [f"{HC}/hc2/cooling/switchPrograms/A"]


def test_one_circuit_has_a_limit_of_its_own():
    probes = ContainerProbes(
        known_child_suffixes(),
        known=lambda _: False,
        records={},
        kind_limit=1000,
        circuit_limit=5,
    )

    assert len(probes.refused(f"{HC}/hc2", 403, 1)) == 5
    assert probes.refused(f"{HC}/hc2/switchPrograms", 403, 2) == []
    assert len(probes.refused(f"{HC}/hc3", 403, 1)) == 5


@pytest.mark.parametrize(
    ("container", "programs"),
    [
        ("/zones/zone2/switchPrograms", ("heating", "cooling")),
        ("/ventilation/zone1/switchPrograms", ("cp",)),
        ("/heatingCircuits/hc2/cooling/switchPrograms", ("A",)),
        ("/dhwCircuits/dhw1/switchPrograms", ("A", "B", "program1")),
    ],
)
def test_refused_program_directories_read_their_programs(container, programs):
    probes = ContainerProbes(known_child_suffixes(), known=lambda _: False, records={})

    planned = [probe.path for probe in probes.refused(container, 403, 2)]

    assert planned == [f"{container}/{program}" for program in programs]


def test_every_program_kind_below_a_circuit_is_a_known_child():
    """The time programs of the register are read directly when their
    directory is refused, for every circuit of a known kind."""
    from custom_components.bosch_buderus_heating.switch_programs import KINDS

    examples = {
        "heating": "/heatingCircuits/hc1/switchPrograms/{p}",
        "dhw": "/dhwCircuits/dhw1/switchPrograms/{p}",
        "cooling": "/heatingCircuits/hc1/cooling/switchPrograms/{p}",
        "zone": "/zones/zone1/switchPrograms/{p}",
        "ventilation": "/ventilation/zone1/switchPrograms/{p}",
        "wddw2": "/dhwCircuits/dhw1/switchPrograms/{p}",
    }
    suffixes = known_child_suffixes()
    programs = {
        "heating": ("A", "B"),
        "dhw": ("A", "B"),
        "cooling": ("A",),
        "zone": ("heating", "cooling"),
        "ventilation": ("cp",),
        "wddw2": ("program1",),
    }
    for kind in KINDS:
        if kind.key == "aircon":
            continue  # not below a circuit
        for program in programs[kind.key]:
            path = examples[kind.key].format(p=program)
            assert kind.pattern.fullmatch(path), path
            root, _, rest = family_of(path)
            assert rest in suffixes[root], path


def test_value_leaves_and_unknown_roots_are_not_containers():
    probes = ContainerProbes(known_child_suffixes(), known=lambda _: False, records={})

    assert probes.listed(f"{HC}/hc1/name", value(f"{HC}/hc1/name"), 2) == []
    assert probes.refused("/system/sensors", 403, 1) == []
    assert probes.refused("/heatSources/Source", 403, 1) == []
    assert probes.refused(f"{HC}/hc2", 500, 1) == []
    assert probes.refused(f"{HC}/hc2", None, 1) == []
    assert family_of("/heatingCircuits") is None
    assert family_of("/heatingCircuits/hc2") == ("heatingCircuits", "hc2", "")
    assert family_of("/zones/zn1/name") is None


def test_a_value_is_not_an_empty_directory():
    records: dict = {}
    probes = ContainerProbes(
        known_child_suffixes(), known=lambda _: False, records=records
    )
    programs = directory(f"{HC}/hc2/switchPrograms", "/A")

    assert probes.listed(programs.path, programs, 2) == []
    assert probes.listed(f"{HC}/hc1/switchPrograms", value("x"), 2) == []
    assert records == {}


async def test_a_new_discovery_starts_without_earlier_direct_reads():
    resources, failures = issue_shape()
    report = DiscoveryDiagnostics()
    await async_discover_resources(
        response_client(resources, failures), "gw", roots=(HC,), diagnostics=report
    )
    assert report.direct_probes
    resources[f"{HC}/hc2"] = directory(f"{HC}/hc2", "/name")
    resources[f"{HC}/hc1"] = directory(f"{HC}/hc1", "/name")

    await async_discover_resources(
        response_client(resources), "gw", roots=(HC,), diagnostics=report
    )

    assert report.direct_probes == {}
    assert report.snapshot()["direct_probe_paths_over_limit"] == 0


def test_references_outside_the_container_are_not_children():
    records: dict = {}
    probes = ContainerProbes(
        known_child_suffixes(), known=lambda _: False, records=records
    )
    hc1 = directory(f"{HC}/hc1", "/name")
    hc1 = Resource(
        hc1.path,
        metadata=DIRECTORY,
        references=(
            *hc1.references,
            ResourceReference("/system/sensors/temperatures/foreignValue"),
        ),
    )
    probes.refused(f"{HC}/hc2", 403, 1)

    probes.listed(hc1.path, hc1, 1)

    assert not any("foreign" in path for path in records[f"{HC}/hc2"].paths)


def test_known_children_cover_the_reported_circuit_values():
    table = known_child_suffixes()

    for suffix in (
        "/name",
        "/currentRoomSetpoint",
        "/overallStatus",
        "/switchPrograms/A",
    ):
        assert suffix in table["heatingCircuits"]
    # From the local API description the integration ships.
    assert "/refrigerant/hotGasTemp" in table["heatSources"]
    assert "/sensors/exhaustTemp" in table["ventilation"]
    assert all(
        suffix.startswith("/") and "{" not in suffix
        for children in table.values()
        for suffix in children
    )
    assert discovery_containers.MAX_DIRECT_CONTAINER_PROBES <= 256


async def test_refused_circuit_gets_its_configured_name(hass: HomeAssistant) -> None:
    resources, failures = issue_shape()
    found, _, _ = await discover(resources, failures)
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type="k30"), entry
    )
    coordinator.resources = dict(found)
    coordinator.data = {
        path: ResourceSnapshot(resource, True, datetime.now(UTC))
        for path, resource in found.items()
    }

    name = grouped_entity_name(coordinator, f"{HC}/hc2/operationMode", "Operation mode")

    assert name == "Obergeschoss \N{EN DASH} Operation mode"
