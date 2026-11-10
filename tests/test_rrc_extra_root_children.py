"""Discovery of the fixed leaves below an RRC contract's extra root.

A contract's ``optional_children`` of one of its ``extra_roots`` are read
when the controller is recognised, also if the root itself is missing or
does not name them (the event lists of W2 are fixed paths). Other gateways
read none of them (S11).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import rrc_contracts
from custom_components.bosch_buderus_heating.pointt import Gateway
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_contract_base import RrcContract
from custom_components.bosch_buderus_heating.rrc_contracts import Registry
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches, load_tree
from tests.test_entry import _entry

PACKAGE = "custom_components.bosch_buderus_heating"
CONTRACT = RrcContract(
    name="extra_root_probe",
    extra_roots=("/probe",),
    optional_children=MappingProxyType({r"^/probe$": ("first", "second/leaf")}),
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


@contextmanager
def installed() -> Iterator[None]:
    original = rrc_contracts._registry
    rrc_contracts._registry = Registry((*rrc_contracts.CONTRACTS, CONTRACT))
    try:
        yield
    finally:
        rrc_contracts._registry = original


async def _requested(hass: HomeAssistant, cloud: RrcCloud, gateway: Gateway) -> set:
    entry = _entry(hass)
    with installed(), cloud_patches(cloud, Clock(), gateway):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinators[0]
        requested = cloud.all_requested()
        data = set(coordinator.data or {})
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    return requested | {f"data:{path}" for path in data}


@pytest.mark.parametrize("root", ["missing", "silent"])
async def test_fixed_leaves_are_read_without_the_root_naming_them(
    hass: HomeAssistant, root: str
) -> None:
    tree = load_tree("rrc/ct200-shapes.json")
    if root == "silent":
        tree["/probe"] = parse_resource(
            {"id": "/probe", "type": "refEnum", "references": []}
        )
    tree["/probe/first"] = parse_resource(
        {"id": "/probe/first", "type": "floatValue", "writeable": 0, "value": 1.0}
    )
    cloud = RrcCloud(tree)
    seen = await _requested(hass, cloud, Gateway("gateway-one", device_type="rrc2"))
    assert {"/probe", "/probe/first", "/probe/second/leaf"} <= seen
    assert "data:/probe/first" in seen
    # The missing leaf answered 500 and counts as missing, once.
    assert cloud.requested("/probe/second/leaf") >= 1


async def test_other_gateways_read_no_contract_leaves(hass: HomeAssistant) -> None:
    cloud = RrcCloud(
        load_tree("captures/k40-cloud-2026-11-05.json"),
        forbidden=set(),
        missing_as_500=False,
    )
    seen = await _requested(hass, cloud, Gateway("gateway-one", device_type="k40"))
    assert not {path for path in seen if "/probe" in path}
