"""STORE-4/STORE-5: removed entries and accesses leave no notes or files."""

from unittest.mock import patch

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating.auth_diagnostics import _storage_key
from custom_components.bosch_buderus_heating.compatibility import FIRMWARE_ISSUE_PREFIX
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.estimated_energy import (
    STORAGE_KEY as ENERGY_KEY,
)
from custom_components.bosch_buderus_heating.faults import fault_store_key
from custom_components.bosch_buderus_heating.local_enrollment import (
    JOURNAL_KEY,
    enrollment_journal,
)
from custom_components.bosch_buderus_heating.managed_entry import (
    async_setup_managed_entry,
)
from custom_components.bosch_buderus_heating.notifications import notification_id
from custom_components.bosch_buderus_heating.repair_scope import repair_data, repair_id
from custom_components.bosch_buderus_heating.store_cleanup import (
    async_sweep_orphaned_stores,
    local_schedule_path,
)
from tests.test_connection_runtime import Harness, entry_for, settle

ROOT = "custom_components.bosch_buderus_heating"
pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def envelope(data, version=1):
    return {"version": version, "minor_version": 1, "key": "", "data": data}


@pytest.fixture
def storage_dir(hass, tmp_path):
    """A private config folder: the cleanup works on real files there."""
    hass.config.config_dir = str(tmp_path)
    folder = tmp_path / ".storage"
    folder.mkdir()
    return folder


async def test_removing_a_loaded_entry_clears_its_repairs(hass):
    """STORE-4: HA drops the runtime before the removal hook runs."""
    entry, cloud, _ = entry_for(hass)
    harness = Harness()

    async def setup(hass, entry):
        return await async_setup_managed_entry(
            hass,
            entry,
            {"singlekey_cloud": harness.factory, "k40_local": harness.factory},
        )

    binding = next(
        key for key, b in entry.data["bindings"].items() if b["connection_id"] == cloud
    )
    issue_id = repair_id(FIRMWARE_ISSUE_PREFIX, entry, cloud, binding)
    foreign = "managed_rate_limit_other"
    with patch(ROOT + ".async_setup_managed_entry", side_effect=setup):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await settle(hass)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            data=repair_data(entry, cloud, binding),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="managed_firmware",
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            foreign,
            data={"entry_id": "another-entry"},
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="managed_rate_limit",
        )
        await hass.config_entries.async_remove(entry.entry_id)
        await settle(hass)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert registry.async_get_issue(DOMAIN, foreign) is not None


async def test_removing_an_entry_deletes_its_private_files(
    hass, hass_storage, storage_dir
):
    """STORE-5/PRIV-1: per gateway and per access, also when never loaded."""
    entry, cloud, target = entry_for(hass)
    gateways = ("000123", "gateway-b")
    kept = "other-gateway:heatSources:hs1:actualPower:estimated_energy"
    for gateway in gateways:
        hass_storage[fault_store_key(entry.entry_id, gateway)] = envelope({})
        hass_storage[notification_id(entry.entry_id, gateway)] = envelope({})
    history = _storage_key(entry.entry_id, cloud)
    hass_storage[history] = envelope({"events": []})
    other_faults = fault_store_key("another-entry", "000123")
    hass_storage[other_faults] = envelope({})
    hass_storage[ENERGY_KEY] = envelope(
        {
            "totals": {
                "000123:heatSources:hs1:actualPower:estimated_energy": 5.0,
                kept: 7.0,
            }
        }
    )
    schedule = local_schedule_path(hass, f"{entry.entry_id}:{target}:local")
    schedule.write_text("{}")
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    for gateway in gateways:
        assert fault_store_key(entry.entry_id, gateway) not in hass_storage
        assert notification_id(entry.entry_id, gateway) not in hass_storage
    assert history not in hass_storage
    assert other_faults in hass_storage
    assert hass_storage[ENERGY_KEY]["data"]["totals"] == {kept: 7.0}
    assert not schedule.exists()


async def test_removing_a_local_access_deletes_its_schedule(hass, storage_dir):
    entry, _, target = entry_for(hass)
    journal = enrollment_journal(hass)
    journal._path = storage_dir / JOURNAL_KEY
    schedule = local_schedule_path(hass, f"{entry.entry_id}:{target}:local")
    schedule.write_text("{}")
    other = local_schedule_path(hass, f"{entry.entry_id}:{target}:other-binding")
    other.write_text("{}")
    management = ConfigurationManagement(hass, entry)
    await journal.remove_access(management, management.edit("local"))
    assert "local" not in entry.data["connections"]
    assert not schedule.exists() and other.exists()


async def test_start_removes_only_files_of_entries_that_no_longer_exist(
    hass, storage_dir
):
    """STORE-5 Variant 2, on real files in the storage folder."""
    entry, cloud, target = entry_for(hass)
    known, gone = entry.entry_id, "removed-entry"
    digest, key = "0" * 16, "a" * 24
    names = {
        f"{DOMAIN}.faults.{known}.{digest}": True,
        f"{DOMAIN}.faults.{gone}.{digest}": False,
        f"{DOMAIN}_faults_{known}_{key}": True,
        f"{DOMAIN}_faults_{gone}_{key}": False,
        f"{DOMAIN}.inventory.{known}": True,
        f"{DOMAIN}.inventory.{gone}": False,
        f"{DOMAIN}.discovery_inventory.{gone}": False,
        f"{DOMAIN}.module_fingerprints.{gone}": False,
        f"{DOMAIN}.gateway_exchange.{gone}": False,
        f"{DOMAIN}.auth_history.{known}": True,
        f"{DOMAIN}.auth_history.{known}.{cloud}": True,
        f"{DOMAIN}.auth_history.{gone}": False,
        f"{DOMAIN}.auth_history.{gone}.{cloud}": False,
        # Not per entry, or a backup set aside: always kept.
        f"{DOMAIN}.inventory.{gone}.corrupt.2026-11-09T00:00:00": True,
        f"{DOMAIN}.local_budget": True,
        f"{DOMAIN}.local_enrollment": True,
        f"{DOMAIN}.estimated_energy": True,
        "core.config_entries": True,
        local_schedule_path(hass, f"{known}:{target}:local").name: True,
        f"{DOMAIN}.local_schedule.{'b' * 64}": False,
    }
    for name in names:
        (storage_dir / name).write_text("{}")
    await async_sweep_orphaned_stores(hass)
    assert {p.name for p in storage_dir.iterdir()} == {
        name for name, keep in names.items() if keep
    }


async def test_an_unreadable_entry_keeps_every_local_schedule(hass, storage_dir):
    entry, _, _ = entry_for(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "bindings": {"local": "broken"}}
    )
    orphan = storage_dir / f"{DOMAIN}.local_schedule.{'b' * 64}"
    orphan.write_text("{}")
    gone = storage_dir / f"{DOMAIN}.inventory.removed-entry"
    gone.write_text("{}")
    await async_sweep_orphaned_stores(hass)
    assert orphan.exists() and not gone.exists()


async def test_exchange_moves_the_old_gateway_stores(hass, hass_storage):
    """FAULT-5: after the copy, the old gateway's file is deleted."""
    from custom_components.bosch_buderus_heating.gateway_exchange import (
        _async_copy_store,
    )

    old, new = fault_store_key("owner", "old"), fault_store_key("owner", "new")
    hass_storage[old] = envelope({"faults": ["synthetic"]})
    await _async_copy_store(hass, 1, old, new)
    assert old not in hass_storage
    assert hass_storage[new]["data"] == {"faults": ["synthetic"]}
    await _async_copy_store(hass, 1, old, new)  # repeatable
    assert hass_storage[new]["data"] == {"faults": ["synthetic"]}
