"""C8: a room controller set up by alpha.4 starts with this release.

Alpha.4 read the whole device tree, including the device key arrays, and
kept it in the discovery inventory. The first start of this release drops
the keys, no longer searches the device parts it does not read, and never
requests either of them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.discovery_inventory import (
    STORAGE_VERSION,
    resource_to_data,
)
from tests.test_discovery_recovery import Clock
from tests.test_entry import _entry
from tests.test_rrc_ct200_setup import (
    PACKAGE,
    PRIVATE,
    RrcCloud,
    cloud_patches,
    ct200_tree,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def stored_inventory(entry_id: str, paths: list[str]) -> dict[str, object]:
    tree = ct200_tree()
    now = datetime.now(UTC).isoformat()
    return {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": f"bosch_buderus_heating.discovery_inventory.{entry_id}",
        "data": {
            "gateways": {
                "gateway-one": {
                    "saved_at": now,
                    "gateway": {
                        "device_type": None,
                        "model": None,
                        "firmware_version": None,
                    },
                    "resources": {
                        path: {"seen": now, "resource": resource_to_data(tree[path])}
                        for path in paths
                    },
                    "templates": {},
                }
            }
        },
    }


async def test_alpha4_device_tree_is_neither_searched_nor_requested(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    entry = _entry(hass)
    tree = ct200_tree()
    # The earlier discovery ended in the device tree, before the zones: the
    # stored parts alone do not show a room controller.
    old = sorted(path for path in tree if path.startswith("/devices"))
    assert any(PRIVATE.search(path) for path in old)
    storage = stored_inventory(entry.entry_id, old)
    hass_storage[str(storage["key"])] = storage
    cloud = RrcCloud(tree)
    with cloud_patches(cloud, Clock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        coordinator = entry.runtime_data.coordinators[0]
        recovery = coordinator.discovery_recovery
        assert not [
            path
            for path in recovery.missing
            if path.startswith("/devices/") and path != "/devices/list"
        ]
        assert not {path for path in cloud.all_requested() if PRIVATE.search(path)}
        assert not any(
            "/etrv" in path or path.endswith("/battery")
            for path in cloud.all_requested()
        )
        stored = coordinator.inventory._data()
        kept = stored["gateways"]["gateway-one"]["resources"]
        assert not [path for path in kept if PRIVATE.search(path)]
        assert not [path for path in kept if "/etrv" in path]
        assert "MARKER" not in json.dumps(stored)

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
