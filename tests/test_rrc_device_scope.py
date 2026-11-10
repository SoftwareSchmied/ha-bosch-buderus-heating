"""C8: the RRC profile of the discovery, the radio device list and privacy.

``issue122_tree`` rebuilds the shape of the anonymized TC100.2 diagnostics
of issue 122: five zones with about twenty leaves each, nine radio devices
with their valve subtrees, nine device key arrays and a large gateway,
system and circuit tree. On alpha.4 the device tree used 206 of the 512
reads and the program list was never requested. Counts and leaf names
follow the report; every value is constructed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from custom_components.bosch_buderus_heating import rrc, rrc_devices
from custom_components.bosch_buderus_heating.discovery import (
    MAX_RRC_DEVICE_PROBES,
    DiscoveryDiagnostics,
    DiscoveryPathSource,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.discovery_inventory import (
    DiscoveryInventory,
    _gateways,
    resource_to_data,
)
from custom_components.bosch_buderus_heating.faults import fault_resource_candidates
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    Resource,
    ResourceError,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.models import ResourceMetadata
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.resource_catalog import (
    identifier_name,
    is_never_read,
    is_private_resource,
)
from tests.test_discovery_recovery import container
from tests.test_rrc_ct200_setup import PRIVATE, RrcCloud, ct200_tree

PACKAGE = __import__("pathlib").Path(__file__).parents[1] / (
    "custom_components/bosch_buderus_heating"
)


def wire(path: str, **fields: object) -> Resource:
    return parse_resource({"id": path, "writeable": 0, "recordable": 0, **fields})


def text(path: str, value: str = "x") -> Resource:
    return wire(path, type="stringValue", value=value)


def number(path: str, value: float = 1.0, unit: str = "C") -> Resource:
    return wire(
        path,
        type="floatValue",
        unitOfMeasure=unit,
        minValue=0.0,
        maxValue=100.0,
        value=value,
    )


def subtree(tree: dict[str, Resource], root: str, leaves: dict[str, Resource]) -> None:
    tree[root] = container(root, *leaves)
    tree.update(leaves)


def issue122_tree() -> dict[str, Resource]:
    tree: dict[str, Resource] = {
        "/notifications": wire("/notifications", type="errorList", value=[])
    }
    subtree(
        tree,
        "/gateway",
        {f"/gateway/item{n}": text(f"/gateway/item{n}") for n in range(62)},
    )
    subtree(
        tree,
        "/system",
        {f"/system/item{n}": text(f"/system/item{n}") for n in range(40)},
    )
    tree["/heatingCircuits"] = container("/heatingCircuits", "/heatingCircuits/hc1")
    subtree(
        tree,
        "/heatingCircuits/hc1",
        {
            f"/heatingCircuits/hc1/item{n}": number(f"/heatingCircuits/hc1/item{n}")
            for n in range(26)
        },
    )
    tree["/dhwCircuits"] = container("/dhwCircuits", "/dhwCircuits/dhw1")
    subtree(
        tree,
        "/dhwCircuits/dhw1",
        {
            f"/dhwCircuits/dhw1/item{n}": number(f"/dhwCircuits/dhw1/item{n}")
            for n in range(17)
        },
    )
    tree["/heatSources"] = container(
        "/heatSources", "/heatSources/hs1", "/heatSources/hs2", "/heatSources/item0"
    )
    tree["/heatSources/item0"] = number("/heatSources/item0")
    for source in ("hs1", "hs2"):
        root = f"/heatSources/{source}"
        subtree(
            tree, root, {f"{root}/item{n}": number(f"{root}/item{n}") for n in range(4)}
        )
    tree["/solarCircuits"] = container("/solarCircuits")

    # Nine radio devices as in the report: one controller entry, eight
    # radiator thermostats with a valve subtree, key arrays besides them.
    devices = range(1, 10)
    tree["/devices"] = container(
        "/devices",
        "/devices/list",
        "/devices/inclusionWhitelist",
        *(f"/devices/dev{n}" for n in devices),
        *(f"/devices/device{n}" for n in devices),
    )
    tree["/devices/list"] = wire(
        "/devices/list",
        type="deviceArray",
        value=[
            {
                "id": n,
                "type": "thermostat" if n == 1 else "thermostat_valve",
                "zone": min(n, 5),
                "battery": "ok",
                "signal": 50,
            }
            for n in devices
        ],
    )
    for n in devices:
        tree[f"/devices/dev{n}"] = wire(
            f"/devices/dev{n}",
            type="deviceArray",
            writeable=1,
            value=[{"dlk": "DLK-MARKER", "sgtin": "SGTIN-MARKER", "zone": 1}],
        )
        root = f"/devices/device{n}"
        leaves = {
            f"{root}/battery": text(f"{root}/battery", "ok"),
            f"{root}/name": text(f"{root}/name"),
            f"{root}/protocol": text(f"{root}/protocol", "homematicip"),
            f"{root}/signal": number(f"{root}/signal", 50, "%"),
            f"{root}/type": text(f"{root}/type", "thermostat_valve"),
            f"{root}/versionFirmware": text(f"{root}/versionFirmware", "1.6.0"),
            f"{root}/zone": number(f"{root}/zone", 1, ""),
        }
        if n == 1:
            subtree(
                tree,
                f"{root}/thermostat",
                {f"{root}/thermostat/childLock": text(f"{root}/thermostat/childLock")},
            )
            tree[root] = container(root, *leaves, f"{root}/thermostat")
        else:
            etrv = f"{root}/etrv"
            subtree(
                tree,
                etrv,
                {
                    f"{etrv}/item{index}": number(f"{etrv}/item{index}")
                    for index in range(13)
                },
            )
            tree[f"{root}/homematicip"] = container(
                f"{root}/homematicip",
                f"{root}/homematicip/dlk",
                f"{root}/homematicip/sgtin",
            )
            tree[root] = container(root, *leaves, etrv, f"{root}/homematicip")
        tree.update(leaves)

    zones = range(1, 6)
    tree["/zones"] = container(
        "/zones", "/zones/list", *(f"/zones/zn{n}" for n in zones)
    )
    tree["/zones/list"] = wire(
        "/zones/list", type="zoneArray", value=[{"id": n} for n in zones]
    )
    for n in zones:
        root = f"/zones/zn{n}"
        leaves = {
            f"{root}/temperatureActual": number(f"{root}/temperatureActual", 20.5),
            f"{root}/userMode": wire(
                f"{root}/userMode",
                type="stringValue",
                used="true",
                writeable=1,
                value="clock",
            ),
            f"{root}/clockProgram": wire(
                f"{root}/clockProgram",
                type="floatValue",
                used="true",
                writeable=1,
                unitOfMeasure="",
                minValue=0.0,
                maxValue=24.0,
                stepSize=1.0,
                value=float(n % 4 + 1),
            ),
            **{
                f"{root}/item{index}": text(f"{root}/item{index}")
                for index in range(17)
            },
        }
        subtree(tree, root, leaves)
    tree["/programs"] = container("/programs", "/programs/list")
    tree["/programs/list"] = wire(
        "/programs/list", type="programArray", value=[{"id": n} for n in range(1, 5)]
    )
    for n in range(1, 5):
        root = f"/programs/pg{n}"
        week = wire(
            f"{root}/week",
            type="weekProgram",
            writeable=1,
            value=[{"d": "Mo", "t": 420, "Th": 20}],
        )
        subtree(tree, root, {f"{root}/week": week})
    return tree


def requested(cloud: RrcCloud) -> set[str]:
    return cloud.all_requested()


async def test_issue122_shape_reads_programs_and_spares_the_budget() -> None:
    """(1) The program list is read; (2) the device tree costs 11 reads."""
    tree = issue122_tree()
    cloud = RrcCloud(tree)
    cloud.down["/zones/zn1/item0"] = 403  # refused as on the real controller
    report = DiscoveryDiagnostics()
    found = await async_discover_resources(
        cloud, "gw", diagnostics=report, gateway=Gateway("gw")
    )
    assert "/programs/list" in found
    assert report.paths["/programs/list"].discovered
    assert report.device_profile == "rrc2"
    assert report.rrc_evidence == ("zones",)
    assert report.stop_reason == "complete"
    devices = {path for path in requested(cloud) if path.startswith("/devices")}
    assert devices == {
        "/devices",
        "/devices/list",
        *(f"/devices/device{n}/versionFirmware" for n in range(1, 10)),
    }
    assert not {path for path in requested(cloud) if PRIVATE.search(path)}
    # Every zone leaf the controller advertises is there.
    assert all(
        path in found
        for path in tree
        if path.startswith("/zones/") and path != "/zones/zn1/item0"
    )
    snapshot = report.snapshot()
    assert snapshot["forbidden_paths"] == 1
    assert snapshot["access_reason"] == "some_paths_forbidden"
    assert snapshot["paths_not_read"]["never_read_private"] == 9
    assert snapshot["paths_not_read"]["outside_rrc_device_scope"] >= 9


async def test_device_tree_before_the_zones_is_dropped_unrequested() -> None:
    """The controller is recognised after its device tree was queued."""
    tree = issue122_tree()
    cloud = RrcCloud(tree)
    report = DiscoveryDiagnostics()
    await async_discover_resources(
        cloud, "gw", roots=("/devices", "/zones"), diagnostics=report
    )
    assert cloud.requests[0] == ("/devices", "/zones")
    later = {path for paths in cloud.requests[1:] for path in paths}
    assert "/devices/device2" not in later
    assert not any("/etrv" in path or path.endswith("/battery") for path in later)
    assert report.paths["/devices/device2"].read_policy == "outside_rrc_device_scope"
    assert report.paths["/devices/dev2"].read_policy == "never_read_private"


async def test_product_type_alone_selects_the_profile() -> None:
    tree = {
        "/gateway": container("/gateway", rrc.PRODUCT_TYPE_PATH),
        rrc.PRODUCT_TYPE_PATH: text(rrc.PRODUCT_TYPE_PATH, "rrc 2.0"),
        "/devices": container("/devices", "/devices/device1", "/devices/list"),
        "/devices/device1": container("/devices/device1", "/devices/device1/battery"),
        "/devices/device1/battery": text("/devices/device1/battery", "ok"),
        "/devices/list": wire("/devices/list", type="deviceArray", value=[{"id": 1}]),
    }
    cloud = RrcCloud(tree)
    report = DiscoveryDiagnostics()
    found = await async_discover_resources(
        cloud, "gw", roots=("/gateway", "/devices"), diagnostics=report
    )
    assert report.rrc_evidence == ("product_type",)
    assert "/devices/device1/battery" not in requested(cloud)
    assert "/devices/device1/versionFirmware" in requested(cloud)
    assert "/devices/list" in found


async def test_reported_device_type_applies_the_profile_from_the_start() -> None:
    tree = issue122_tree()
    cloud = RrcCloud(tree)
    report = DiscoveryDiagnostics()
    await async_discover_resources(
        cloud,
        "gw",
        roots=("/devices",),
        diagnostics=report,
        gateway=Gateway("gw", device_type="RRC2"),
    )
    # The device list comes in the very first request.
    assert "/devices/list" in cloud.requests[0]
    assert report.rrc_evidence == ("device_type",)


async def test_firmware_probes_are_capped() -> None:
    count = MAX_RRC_DEVICE_PROBES + 6
    tree = {
        "/devices": container("/devices", "/devices/list"),
        "/devices/list": wire(
            "/devices/list",
            type="deviceArray",
            value=[{"id": n} for n in range(1, count + 1)],
        ),
    }
    cloud = RrcCloud(tree)
    await async_discover_resources(
        cloud, "gw", roots=("/devices",), gateway=Gateway("gw", device_type="rrc2")
    )
    probes = {path for path in requested(cloud) if path.endswith("/versionFirmware")}
    assert len(probes) == MAX_RRC_DEVICE_PROBES


def missing_probe_tree() -> dict[str, Resource]:
    """A controller whose circuit advertises leaves it then answers 500 for."""
    tree = {
        "/zones": container("/zones", "/zones/zn1"),
        "/zones/zn1": container("/zones/zn1", "/zones/zn1/temperatureActual"),
        "/zones/zn1/temperatureActual": number("/zones/zn1/temperatureActual", 20.0),
        "/heatingCircuits": container("/heatingCircuits", "/heatingCircuits/hc1"),
        "/heatingCircuits/hc1": container("/heatingCircuits/hc1"),
    }
    return tree


async def test_http_500_on_probes_of_a_controller_does_not_end_discovery() -> None:
    """CT-B06: catalog and optional probes answer 500; nothing aborts."""
    cloud = RrcCloud(missing_probe_tree())
    report = DiscoveryDiagnostics()
    found = await async_discover_resources(
        cloud, "gw", roots=("/zones", "/heatingCircuits"), diagnostics=report
    )
    assert report.stop_reason == "complete"
    assert "/zones/zn1/temperatureActual" in found
    missing = [
        item
        for item in report.paths.values()
        if item.read_policy == "rrc_5xx_assumed_missing"
    ]
    # The heating circuit's catalog and optional probes, and zone fields.
    assert len(missing) > 20
    assert {item.source for item in missing} <= {
        DiscoveryPathSource.CATALOG,
        DiscoveryPathSource.OPTIONAL,
    }
    assert all(item.fallback_result == "http_500" for item in missing)
    assert report.snapshot()["paths_not_read"]["rrc_5xx_assumed_missing"] == len(
        missing
    )


async def test_http_500_on_probes_still_ends_discovery_of_other_gateways() -> None:
    tree = missing_probe_tree()
    del tree["/zones"]
    cloud = RrcCloud(tree)
    report = DiscoveryDiagnostics()
    with pytest.raises(ServiceUnavailable):
        await async_discover_resources(
            cloud, "gw", roots=("/heatingCircuits",), diagnostics=report
        )
    assert report.device_profile == "generic"
    assert report.stop_reason == "service_unavailable"


async def test_http_500_on_advertised_references_still_ends_discovery() -> None:
    tree = missing_probe_tree()
    tree["/zones/zn1"] = container(
        "/zones/zn1", *(f"/zones/zn1/gone{n}" for n in range(3))
    )
    cloud = RrcCloud(tree)
    for n in range(3):
        cloud.down[f"/zones/zn1/gone{n}"] = 500
        cloud.get_errors[f"/zones/zn1/gone{n}"] = ServiceUnavailable(500)
    report = DiscoveryDiagnostics()
    with pytest.raises(ServiceUnavailable):
        await async_discover_resources(
            cloud, "gw", roots=("/zones",), diagnostics=report
        )
    assert report.device_profile == "rrc2"


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (
            {"/zones/zn1/humidity": "http_403", "/zones": "success"},
            "some_paths_forbidden",
        ),
        (
            {"/zones/list": "http_403", "/gateway": "success"},
            "scope_or_permission_denied",
        ),
        ({"/gateway": "http_403", "/system": "http_403"}, "scope_or_permission_denied"),
        ({"/gateway": "success"}, None),
    ],
)
def test_access_reason_tells_single_refusals_from_missing_rights(
    snapshot: dict[str, str], reason: str | None
) -> None:
    report = DiscoveryDiagnostics()
    for path, result in snapshot.items():
        report.scheduled(path, DiscoveryPathSource.ROOT)
        report.bulk_started((path,))
        status = int(result.removeprefix("http_")) if result != "success" else 200
        report.bulk_result(
            BatchItemResult(
                "gw",
                path,
                status,
                resource=Resource(path) if status == 200 else None,
                error=None if status == 200 else ResourceError(path, status),
            )
        )
    assert report.snapshot()["access_reason"] == reason


def test_request_wide_refusal_is_a_rights_problem() -> None:
    report = DiscoveryDiagnostics()
    report.stop_reason = "http_403"
    assert report.snapshot()["access_reason"] == "scope_or_permission_denied"


# ------------------------------------------------------------ device list


def device_list(value: object, **fields: object) -> Resource:
    return wire(rrc.DEVICE_LIST, type="deviceArray", value=value, **fields)


def test_device_list_fields_are_parsed_strictly() -> None:
    devices = rrc_devices.parse_devices(
        device_list(
            [
                {
                    "id": 1,
                    "type": "thermostat",
                    "zone": 1,
                    "battery": "ok",
                    "signal": 0,
                },
                {
                    "id": 2,
                    "type": "Thermostat_Valve",
                    "zone": 2.0,
                    "battery": "LOW",
                    "signal": 84,
                },
                {
                    "id": 3,
                    "type": "odd type",
                    "zone": 1.5,
                    "battery": "empty",
                    "signal": True,
                },
                {"id": 4, "zone": -1, "signal": 101},
                {
                    "id": 5,
                    "type": "repeater",
                    "zone": 0,
                    "battery": "unknown",
                    "signal": 24.5,
                },
            ]
        )
    )
    assert devices is not None
    first, valve, odd, bare, repeater = devices
    assert first.is_controller and first.battery_low is False
    assert (valve.kind, valve.zone, valve.battery_low, valve.signal) == (
        "thermostat_valve",
        2,
        True,
        84.0,
    )
    assert (odd.kind, odd.zone, odd.battery, odd.signal) == (None, None, None, None)
    assert (bare.kind, bare.zone, bare.battery, bare.signal) == (None, None, None, None)
    assert repeater.zone == 0 and repeater.battery_low is None
    assert rrc_devices.field_issues(devices) == {
        "type": 2,
        "zone": 2,
        "battery": 2,
        "signal": 2,
    }
    assert rrc_devices.device_name(odd, "de") == "Funkgerät 3"
    assert rrc_devices.device_name(valve, "en") == "Radiator thermostat 2"


@pytest.mark.parametrize(
    "resource",
    [
        device_list([{"id": 1}, {"id": 1}]),  # duplicate IDs
        device_list([{"id": "1"}]),  # not numeric
        device_list([{"name": "no id"}]),
        device_list([{"id": 1}], used="false"),
        device_list({"id": 1}),
        wire(rrc.DEVICE_LIST, type="zoneArray", value=[{"id": 1}]),
        wire("/devices/other", type="deviceArray", value=[{"id": 1}]),
        None,
    ],
)
def test_invalid_device_lists_supply_no_devices(resource: Resource | None) -> None:
    assert rrc_devices.parse_devices(resource) is None


def test_empty_device_list_is_valid_and_empty() -> None:
    assert rrc_devices.parse_devices(device_list([])) == ()
    assert rrc.device_firmware_paths(device_list([]), 5) == ()


def test_device_ids_come_only_from_the_list() -> None:
    assert rrc.listed_paths(device_list([{"id": 7}])) == ()
    assert rrc.device_firmware_paths(device_list([{"id": 7}]), 5) == (
        "/devices/device7/versionFirmware",
    )
    assert rrc.in_device_scope("/devices/device7/versionFirmware")
    assert not rrc.in_device_scope("/devices/device7")
    assert not rrc.in_device_scope("/devices/device7/etrv/valvePosition")


# --------------------------------------------------------------- privacy


@pytest.mark.parametrize(
    "path",
    [
        "/devices/dev1",
        "/devices/dev12/anything",
        "/devices/productLookup?sgtin=0",
        "/devices/device3/homematicip",
        "/devices/device3/homematicip/dlk",
        "/gateway/userPassword",
        "/gateway/user/email",
        "/gateway/installer",
        "/gateway/installer/phone",
        "/system/location/coordinates",
        "/system/autoAway/users/list",
    ],
)
def test_keys_and_personal_data_are_never_read(path: str) -> None:
    assert is_never_read(path)
    assert is_private_resource(path)


@pytest.mark.parametrize(
    "path",
    [
        "/devices/device1",
        "/devices/device1/battery",
        "/devices/list",
        "/gateway/update",
    ],
)
def test_ordinary_device_paths_stay_readable(path: str) -> None:
    assert not is_never_read(path)


def test_dlk_is_an_identifier_word() -> None:
    assert identifier_name("dlk")
    assert identifier_name("deviceDlk")


def test_radio_devices_have_no_fault_probes() -> None:
    resources = {
        "/zones/zn1/userMode": text("/zones/zn1/userMode", "clock"),
        rrc.DEVICE_LIST: device_list([{"id": 1}, {"id": 2}]),
        "/devices/device1/versionFirmware": text("/devices/device1/versionFirmware"),
    }
    assert not [
        path
        for path in fault_resource_candidates(resources)
        if path.startswith("/devices")
    ]
    # Without the controller's zones the device list keeps its probes.
    del resources["/zones/zn1/userMode"]
    assert "/devices/1/errors" in fault_resource_candidates(resources)


def test_stored_keys_and_unused_device_parts_are_dropped_on_load() -> None:
    """Alpha.4 stored the device key arrays; they never load again."""
    now = datetime.now(UTC)
    tree = issue122_tree()
    rows = {
        path: {"resource": resource_to_data(resource), "seen": now.isoformat()}
        for path, resource in tree.items()
    }
    stored = {
        "gateways": {
            "gw": {
                "gateway": {
                    "device_type": None,
                    "model": None,
                    "firmware_version": None,
                },
                "saved_at": now.isoformat(),
                "resources": rows,
                "templates": {
                    path: resource_to_data(resource) for path, resource in tree.items()
                },
            }
        }
    }
    ((_, inventory),) = list(_gateways(stored, now))
    assert not [path for path in inventory.resources if PRIVATE.search(path)]
    assert not [path for path in inventory.templates if PRIVATE.search(path)]
    kept = {path for path in inventory.resources if path.startswith("/devices/")}
    assert kept == {
        "/devices/list",
        *(f"/devices/device{n}/versionFirmware" for n in range(1, 10)),
    }
    assert "DLK-MARKER" not in json.dumps(
        {
            path: resource_to_data(item.resource)
            for path, item in inventory.resources.items()
        }
    )
    # A gateway without the controller's zones keeps its device tree.
    for path in [path for path in rows if path.startswith("/zones")]:
        del rows[path]
    ((_, other),) = list(_gateways(stored, now))
    assert "/devices/device2/etrv/item0" in other.resources
    assert "/devices/dev2" not in other.resources


def test_record_never_stores_keys(hass) -> None:
    inventory = DiscoveryInventory(hass, "entry")
    inventory._loaded = True
    saved: list[object] = []
    inventory.store.async_delay_save = lambda data, delay: saved.append(data())
    key = wire("/devices/dev1", type="deviceArray", value=[{"dlk": "DLK-MARKER"}])
    inventory.record(Gateway("gw"), {"/devices/dev1": key})
    assert saved == []
    inventory.record(
        Gateway("gw"), {"/devices/dev1": key, "/gateway/a": text("/gateway/a")}
    )
    assert "DLK-MARKER" not in json.dumps(saved)
    assert "/gateway/a" in inventory.resources("gw")


def test_synthetic_fixture_is_large_and_carries_only_markers() -> None:
    tree = ct200_tree()
    assert len(rrc.listed_ids(tree["/zones/list"]) or ()) == 11
    assert len(rrc.listed_ids(tree["/programs/list"]) or ()) == 24
    assert len(rrc_devices.parse_devices(tree[rrc.DEVICE_LIST]) or ()) == 12
    assert len(tree) > 512


def test_no_devices_text_explains_older_firmware() -> None:
    strings = json.loads((PACKAGE / "strings.json").read_text("utf-8"))
    for text_ in (
        strings["config"]["abort"]["no_gateways"],
        strings["options"]["error"]["no_gateways"],
    ):
        assert "older firmware" in text_
        assert "Bosch" not in text_ and "Buderus" not in text_
    german = json.loads((PACKAGE / "translations" / "de.json").read_text("utf-8"))
    assert "älterer Firmware" in german["config"]["abort"]["no_gateways"]


def test_metadata_without_value_is_no_profile_evidence() -> None:
    resources = {
        rrc.PRODUCT_TYPE_PATH: Resource(
            rrc.PRODUCT_TYPE_PATH, metadata=ResourceMetadata()
        )
    }
    assert rrc.profile_evidence(None, resources) == ()
    assert rrc.profile_evidence(" rrc2 ", {}) == ("device_type",)


@pytest.mark.parametrize(
    ("gateway", "label"),
    [
        (Gateway("gw-0001", device_type="rrc2"), "Room controller (…0001)"),
        (Gateway("gw-0002", device_type=" RRC2 "), "Room controller (…0002)"),
        (Gateway("gw-0003", device_type="rrc2", model="CT 1"), "CT 1 (…0003)"),
        (Gateway("gw-0004", device_type="K40RF"), "K40RF (…0004)"),
        (Gateway("gw-0005"), "Gateway (…0005)"),
    ],
)
def test_unnamed_room_controller_is_offered_by_function(
    gateway: Gateway, label: str
) -> None:
    """CT-A01: the cloud lists a room controller without a name."""
    from custom_components.bosch_buderus_heating.config_flow import _gateway_label

    assert _gateway_label(gateway) == label
