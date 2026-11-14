"""W0 changes nothing for existing installations: entities and requests.

The entity registry and the cloud requests of a real setup and one regular
cycle are compared with ``w0-entity-baseline.json``, recorded with the same
test at the state before W0 (alpha.5 integration state ``83fa1b1``) for a
CT200-class controller, a TC100.2-shaped controller and a K40 heat pump.
Record it again only on purpose (``W0_WRITE_BASELINE=1``), after a review.
The packages W1 to W6 add entities and requests to the RRC plants on
purpose; the snapshot is recorded again after they are merged and pins the
shipped state. The K40 heat pump must stay unchanged; the 0.9.0 entity
review changed it on purpose: a fresh setup creates the totals of its only
heat source and its burner status disabled, and the request sensors carry
neutral names. 0.9.1 adds the heat generator sensor of the module list on
purpose, without a request of its own, and corrected the name of its system
supply temperature (before "Supply temperature"); its unique ID stays.
"""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from enum import Enum
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.pointt import Gateway
from tests.helpers.rrc_cloud import (
    Clock,
    RrcCloud,
    cloud_patches,
    load_tree,
    with_containers,
)
from tests.test_entry import _entry
from tests.test_rrc_tc100_setup import plant as tc100_plant

# Not a resource fixture, so outside tests/fixtures/contracts.
BASELINE = Path(__file__).parent / "fixtures" / "w0-entity-baseline.json"
PACKAGE = "custom_components.bosch_buderus_heating"
PLANTS = {
    "ct200": (
        lambda: RrcCloud(load_tree("rrc/ct200-shapes.json")),
        Gateway("gateway-one"),
    ),
    # The TC100.2 wire format of test_rrc_tc100_setup (string flags, no
    # allowedValues, Base64 names), with the string-flag fixture's leaves.
    "tc100": (
        lambda: RrcCloud(
            {
                **with_containers(load_tree("rrc/tc100-string-flags.json")),
                **tc100_plant(),
            },
            forbidden=set(),
            missing_as_500=False,
        ),
        Gateway("gateway-one", model="MX400"),
    ),
    "k40": (
        lambda: RrcCloud(
            load_tree("captures/k40-cloud-2026-11-05.json"),
            forbidden=set(),
            missing_as_500=False,
        ),
        Gateway("gateway-one", device_type="k40"),
    ),
}

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def _plain(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


async def _snapshot(hass: HomeAssistant, plant: str) -> dict[str, object]:
    build, gateway = PLANTS[plant]
    cloud = build()
    clock = Clock()
    entry = _entry(hass)
    faults = (
        # As test_rrc_tc100_setup: no fault probes for the small tree.
        patch(f"{PACKAGE}.coordinator.fault_resource_candidates", return_value=())
        if plant == "tc100"
        else nullcontext()
    )
    with cloud_patches(cloud, clock, gateway), faults:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        setup_requests = sorted(cloud.all_requested())
        setup_calls = {"bulk": len(cloud.requests), "single": len(cloud.single)}
        cloud.requests.clear()
        cloud.single.clear()
        coordinator = entry.runtime_data.coordinators[0]
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        entities = sorted(
            (
                {
                    "domain": item.domain,
                    # Some IDs carry the random entry ID.
                    "unique_id": item.unique_id.replace(entry.entry_id, "<entry>"),
                    "disabled_by": _plain(item.disabled_by),
                    "entity_category": _plain(item.entity_category),
                    "original_name": item.original_name,
                    "translation_key": item.translation_key,
                    "original_icon": item.original_icon,
                    "device_class": item.original_device_class,
                    "unit": item.unit_of_measurement,
                    "capabilities": _plain(item.capabilities),
                }
                for item in er.async_entries_for_config_entry(registry, entry.entry_id)
            ),
            key=lambda item: (item["domain"], item["unique_id"]),
        )
        snapshot = {
            "entities": entities,
            "setup_requests": setup_requests,
            "setup_calls": setup_calls,
            "cycle_requests": sorted(cloud.all_requested()),
            "writes": len(cloud.writes),
        }
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    return snapshot


@pytest.mark.parametrize("plant", sorted(PLANTS))
async def test_entities_and_requests_match_the_state_before_w0(
    hass: HomeAssistant, plant: str
) -> None:
    snapshot = await _snapshot(hass, plant)
    if os.environ.get("W0_WRITE_BASELINE"):
        stored = (
            json.loads(BASELINE.read_text(encoding="utf-8"))
            if BASELINE.exists()
            else {
                "provenance": {
                    "kind": "test_snapshot",
                    "physical_device_verified": False,
                    "recorded_at": "83fa1b1",
                    "note": "entity registry and requests before W0",
                },
                "plants": {},
            }
        )
        stored["plants"][plant] = snapshot
        BASELINE.write_text(
            json.dumps(stored, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        return
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))["plants"][plant]
    assert snapshot["entities"] == expected["entities"]
    assert snapshot["setup_requests"] == expected["setup_requests"]
    assert snapshot["setup_calls"] == expected["setup_calls"]
    assert snapshot["cycle_requests"] == expected["cycle_requests"]
    assert snapshot["writes"] == expected["writes"] == 0
