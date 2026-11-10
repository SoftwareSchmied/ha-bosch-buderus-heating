"""T2: all room controller packages together within the cloud budget.

A real setup and one hour of regular cycles on the large CT200-class fixture
and on a tree shaped like the TC100.2 diagnostics of issue 122, with every
package contract active: the discovery completes and reads the zone, program
and device lists, every zone gets its climate, the setting leaves of every
listed device are read, and the requests per setup and per hour stay within
the documented limits (512 discovery paths plus 192 device setting leaves,
batches of at most 30 paths, the poll groups of at least one minute).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import rrc
from custom_components.bosch_buderus_heating.discovery import (
    MAX_DISCOVERY_RESOURCES,
    MAX_RRC_DEVICE_SETTING_PROBES,
)
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches
from tests.test_entry import _entry
from tests.test_rrc_ct200_setup import ct200_tree
from tests.test_rrc_device_scope import issue122_tree

PACKAGE = "custom_components.bosch_buderus_heating"
DEVICE_LIST = "/devices/list"
BATCH = 30

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

# Plant → (tree, gateway, at most single reads and bulk requests at setup,
# at most requests in one hour of regular cycles). Measured with all
# packages on 2026-11-11, before them (a708ad7) in brackets:
# CT200 fixture: setup 21 bulk + 7 single (16 + 113), 526 distinct paths
#   (398), hour 152 requests with 4163 path reads (132, 3470);
# issue-122 tree: setup 24 bulk + 22 single (20 + 146), 570 distinct paths
#   (452), hour 72 requests with 1451 path reads (72, 1451).
# The single reads left are known catalog paths the trees lack (CT-B06).
PLANTS: dict[str, tuple[Callable[[], dict[str, Resource]], Gateway, int, int, int]] = {
    "ct200": (ct200_tree, Gateway("gateway-one"), 10, 25, 160),
    "issue122": (issue122_tree, Gateway("gateway-one", model="MX400"), 25, 28, 80),
}


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


@pytest.mark.parametrize("plant", sorted(PLANTS))
async def test_all_packages_stay_within_the_budget(
    hass: HomeAssistant, plant: str
) -> None:
    build, gateway, max_single, max_bulk, max_hourly = PLANTS[plant]
    tree = build()
    cloud = RrcCloud(tree)
    clock = Clock()
    entry = _entry(hass)
    with cloud_patches(cloud, clock, gateway):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        coordinator = entry.runtime_data.coordinators[0]
        report = coordinator.discovery_diagnostics
        assert report.stop_reason == "complete"
        assert report.device_profile == "rrc2"

        # Zones, programs and devices are read; every listed device's
        # setting leaves are read under their own cap.
        requested = cloud.all_requested()
        assert {"/zones/list", "/programs/list", DEVICE_LIST} <= requested
        settings = rrc.device_setting_paths(tree[DEVICE_LIST])
        assert 0 < len(settings) <= MAX_RRC_DEVICE_SETTING_PROBES
        assert set(settings) <= requested
        assert not [
            path
            for path, item in report.paths.items()
            if item.read_policy in ("rrc_device_setting_limit", "resource_limit")
        ]
        zones = rrc.listed_ids(tree["/zones/list"]) or ()
        climates = [
            item
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            if item.domain == "climate"
        ]
        assert len(climates) == len(zones) > 0

        # Per setup: the documented limits, in few requests.
        assert len(requested - set(settings)) <= MAX_DISCOVERY_RESOURCES
        bulk, single = len(cloud.requests), len(cloud.single)
        assert all(len(paths) <= BATCH for paths in cloud.requests)
        assert bulk <= max_bulk
        assert bulk >= math.ceil(len(requested) / BATCH)
        assert single <= max_single

        # Per hour: sixty one-minute wake-ups of the regular polling.
        cloud.requests.clear()
        cloud.single.clear()
        for _ in range(60):
            clock.now += 60
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        hourly = len(cloud.requests) + len(cloud.single)
        assert hourly <= max_hourly
        assert all(len(paths) <= BATCH for paths in cloud.requests)
        assert not cloud.writes
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
