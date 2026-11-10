"""W0: the discovery hooks of the RRC contracts.

Without contract entries nothing changes (no new request). With them, an
RRC controller reads the extra roots, the optional leaves and the setting
leaves of its listed radio devices; the latter are bounded and come last.
Another gateway reads none of them, even with the same paths present.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import MappingProxyType

from custom_components.bosch_buderus_heating import rrc
from custom_components.bosch_buderus_heating.discovery import (
    MAX_RRC_DEVICE_SETTING_PROBES,
    DiscoveryDiagnostics,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.discovery_inventory import _not_kept
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_contract_base import RrcContract
from tests.helpers.rrc_cloud import RrcCloud, with_containers
from tests.helpers.rrc_w0_state import w0_state

LEAVES = tuple(f"etrv/setting{index}" for index in range(10))
CONTRACT = RrcContract(
    name="w_zero_hooks",
    extra_roots=("/events",),
    optional_children=MappingProxyType({r"^/system/appliance$": ("displayCode",)}),
    device_leaves=MappingProxyType({"thermostat_valve": LEAVES}),
)


@contextmanager
def installed(*contracts: RrcContract) -> Iterator[None]:
    """The W0 contracts (whatever the packages added since) and ``contracts``."""
    with w0_state(*contracts):
        yield


def wire(path: str, **fields: object) -> Resource:
    return parse_resource({"id": path, "writeable": 0, "recordable": 0, **fields})


def tree(*, rrc_plant: bool = True, devices: int = 24) -> dict[str, Resource]:
    resources = {
        "/system/appliance/model": wire(
            "/system/appliance/model", type="stringValue", value="x"
        ),
        "/events/list1": wire("/events/list1", type="eventArray", value=[]),
        "/devices/list": wire(
            "/devices/list",
            type="deviceArray",
            value=[
                {"id": number, "type": "thermostat_valve", "zone": 1}
                for number in range(1, devices + 1)
            ],
        ),
    }
    for number in range(1, devices + 1):
        # Half of the leaves exist; the others answer 500 (P31).
        for leaf in LEAVES[:5]:
            path = f"/devices/device{number}/{leaf}"
            resources[path] = wire(path, type="stringValue", value="false")
    if rrc_plant:
        resources["/gateway/productType"] = wire(
            "/gateway/productType", type="stringValue", value="RRC 2.0"
        )
        resources["/zones/list"] = wire(
            "/zones/list", type="zoneArray", value=[{"id": 1}]
        )
        resources["/zones/zn1/temperatureActual"] = wire(
            "/zones/zn1/temperatureActual",
            type="floatValue",
            unitOfMeasure="C",
            value=20.0,
        )
    return with_containers(resources)


async def _discover(cloud: RrcCloud, gateway: Gateway):
    report = DiscoveryDiagnostics()
    found = await async_discover_resources(
        cloud,  # type: ignore[arg-type]
        gateway.gateway_id,
        diagnostics=report,
        gateway=gateway,
    )
    order = [path for paths in cloud.requests for path in paths] + cloud.single
    return found, report, order


def _setting(path: str) -> bool:
    return "/etrv/setting" in path


async def test_empty_hooks_add_no_request() -> None:
    cloud = RrcCloud(tree())
    with installed():
        _found, report, order = await _discover(
            cloud, Gateway("gw", device_type="rrc2")
        )
    assert report.device_profile == "rrc2"
    assert not any(_setting(path) or path.startswith("/events") for path in order)
    assert "/system/appliance/displayCode" not in order
    assert report.stop_reason == "complete"


async def test_hooks_reach_an_rrc_controller_bounded_and_last() -> None:
    cloud = RrcCloud(tree())
    with installed(CONTRACT):
        found, report, order = await _discover(cloud, Gateway("gw", device_type="rrc2"))
    assert report.stop_reason == "complete"
    assert "/events" in order and "/events/list1" in found
    assert "/system/appliance/displayCode" in order
    settings = [path for path in dict.fromkeys(order) if _setting(path)]
    assert len(settings) == MAX_RRC_DEVICE_SETTING_PROBES
    # In list order, all ten leaves of each device before the next device.
    assert settings[:10] == [f"/devices/device1/{leaf}" for leaf in LEAVES]
    # After the first setting leaf, the bulk reads carry setting leaves only.
    bulk = [path for paths in cloud.requests for path in paths]
    first = next(index for index, path in enumerate(bulk) if _setting(path))
    assert all(_setting(path) for path in bulk[first:])
    # Bulk requests: no other path shares a request with a setting leaf.
    for paths in cloud.requests:
        if any(_setting(path) for path in paths):
            assert all(_setting(path) for path in paths)
    limited = [
        path
        for path, item in report.paths.items()
        if item.read_policy == "rrc_device_setting_limit"
    ]
    assert len(limited) == 240 - MAX_RRC_DEVICE_SETTING_PROBES
    assert report.snapshot()["paths_not_read"]["rrc_device_setting_limit"] == 48
    # CT-B06: a missing leaf answers 500 and is taken as missing.
    missing = report.paths["/devices/device1/etrv/setting9"]
    assert missing.read_policy == "rrc_5xx_assumed_missing"
    assert "/devices/device1/etrv/setting0" in found
    # The stored inventory keeps the found leaves while a contract reads
    # them, and drops them as before without one.
    gateway = Gateway("gw", device_type="rrc2")
    with installed(CONTRACT):
        assert not {path for path in _not_kept(gateway, found) if _setting(path)}
    with installed():
        assert {path for path in _not_kept(gateway, found) if _setting(path)} == {
            path for path in found if _setting(path)
        }


async def test_hooks_never_reach_another_gateway() -> None:
    cloud = RrcCloud(tree(rrc_plant=False), missing_as_500=False)
    with installed(CONTRACT):
        _found, report, order = await _discover(cloud, Gateway("gw", device_type="k40"))
    assert report.device_profile == "generic"
    assert not any(path.startswith("/events") for path in order)
    assert "/system/appliance/displayCode" not in order
    # The generic tree reads the device leaves it is referred to, as before.
    assert not any(path.endswith("/etrv/setting9") for path in order)


async def test_a_controller_recognised_late_still_gets_the_optional_leaves() -> None:
    """The container came before the zones showed an RRC controller."""
    cloud = RrcCloud(tree())
    with installed(CONTRACT):
        _found, report, order = await _discover(cloud, Gateway("gw"))
    assert report.device_profile == "rrc2"
    assert "/system/appliance/displayCode" in order
    assert "/events/list1" in order


def test_device_scope_follows_the_listed_types() -> None:
    listed = tree(devices=2)["/devices/list"]
    with installed(CONTRACT):
        assert rrc.device_setting_paths(listed, 3) == (
            "/devices/device1/etrv/setting0",
            "/devices/device1/etrv/setting1",
            "/devices/device1/etrv/setting2",
        )
        assert rrc.in_device_scope("/devices/device2/etrv/setting9", listed)
        assert not rrc.in_device_scope("/devices/device3/etrv/setting0", listed)
        assert not rrc.in_device_scope("/devices/device2/etrv/setting0")
        assert not rrc.in_device_scope("/devices/device2/etrv/other", listed)
        assert rrc.device_setting_paths(wire("/zones/list", type="zoneArray")) == ()
    with installed():
        assert rrc.device_setting_paths(listed) == ()
