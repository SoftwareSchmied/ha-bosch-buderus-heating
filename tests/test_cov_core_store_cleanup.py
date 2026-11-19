"""Store cleanup on entry removal: failure paths that must never block a
removal and never delete what still belongs to a living entry."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bosch_buderus_heating import store_cleanup
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.store_cleanup import (
    async_remove_entry_stores,
    async_remove_local_schedules,
    async_sweep_orphaned_stores,
    local_schedule_scopes,
)
from tests.test_connection_runtime import entry_for

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
def storage_dir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    folder = tmp_path / ".storage"
    folder.mkdir()
    return folder


# ------------------------------------------------------ schedule scopes


@pytest.mark.parametrize(
    "data",
    [
        {"bindings": "broken"},
        {"bindings": {"local": "broken"}},
        {"bindings": {"local": {"target_id": 7}}},
        {"bindings": {"local": {}}},
        {"bindings": {3: {"target_id": "t"}}},
    ],
)
def test_unreadable_bindings_give_no_scopes(data) -> None:
    assert local_schedule_scopes("entry", data) is None


def test_the_scopes_name_entry_target_and_binding() -> None:
    data = {"bindings": {"b1": {"target_id": "t1"}, "b2": {"target_id": "t2"}}}
    assert local_schedule_scopes("entry", data) == {"entry:t1:b1", "entry:t2:b2"}
    assert local_schedule_scopes("entry", {}) == set()


async def test_a_schedule_that_cannot_be_deleted_only_warns(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch.object(
            hass, "async_add_executor_job", AsyncMock(side_effect=OSError("locked"))
        ),
        caplog.at_level(logging.WARNING),
    ):
        await async_remove_local_schedules(hass, ["entry:t:b"])
    assert "A local schedule of a removed access could not be deleted" in caplog.text
    assert "locked" not in caplog.text


async def test_removing_missing_schedules_is_fine(hass, storage_dir) -> None:
    await async_remove_local_schedules(hass, ["entry:t:b"])
    assert list(storage_dir.iterdir()) == []


# ------------------------------------------------- the removal itself


async def test_an_unreadable_entry_keeps_its_schedules_on_removal(hass) -> None:
    entry, _, _ = entry_for(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "bindings": {"local": "broken"}}
    )
    removed = AsyncMock()
    with patch.object(store_cleanup, "async_remove_local_schedules", removed):
        await async_remove_entry_stores(hass, entry)
    removed.assert_not_awaited()


async def test_an_entry_without_readable_gateways_removes_no_gateway_files(
    hass,
) -> None:
    entry, _, _ = entry_for(hass)
    from custom_components.bosch_buderus_heating import entry_sources

    with (
        patch.object(entry_sources, "notification_gateways", side_effect=KeyError("x")),
        patch(
            "custom_components.bosch_buderus_heating.faults.async_remove_fault_store",
            new=AsyncMock(),
        ) as faults,
        patch(
            "custom_components.bosch_buderus_heating.recordings.manager."
            "async_offer_statistics_removal",
            new=AsyncMock(),
        ) as offer,
    ):
        await async_remove_entry_stores(hass, entry)
    # The next start removes what is left, by entry ID.
    faults.assert_not_awaited()
    offer.assert_not_awaited()


async def test_a_failing_cache_update_never_stops_the_other_cleanup(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry, _, _ = entry_for(hass)
    derived = AsyncMock()
    with (
        patch(
            "custom_components.bosch_buderus_heating.recordings.manager."
            "async_offer_statistics_removal",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.bosch_buderus_heating.estimated_energy."
            "estimated_energy_store"
        ) as energy,
        patch(
            "custom_components.bosch_buderus_heating.derived_store.derived_store"
        ) as calculated,
        caplog.at_level(logging.WARNING),
    ):
        energy.return_value.async_forget_gateways = AsyncMock(
            side_effect=RuntimeError("disk full")
        )
        calculated.return_value.async_forget_gateways = derived
        await async_remove_entry_stores(hass, entry)
    assert "Estimated energy totals could not be updated" in caplog.text
    derived.assert_awaited_once()


async def test_a_failing_calculated_value_cache_only_warns(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry, _, _ = entry_for(hass)
    with (
        patch(
            "custom_components.bosch_buderus_heating.recordings.manager."
            "async_offer_statistics_removal",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.bosch_buderus_heating.derived_store.derived_store"
        ) as calculated,
        caplog.at_level(logging.WARNING),
    ):
        calculated.return_value.async_forget_gateways = AsyncMock(
            side_effect=RuntimeError("disk full")
        )
        await async_remove_entry_stores(hass, entry)
    assert "Saved calculated values could not be updated" in caplog.text


async def test_a_gateway_another_entry_uses_keeps_its_caches(hass) -> None:
    from copy import deepcopy

    from tests.ha_test_helpers import MockConfigEntry

    first, _, _ = entry_for(hass)
    second = MockConfigEntry(
        domain=DOMAIN,
        entry_id="second-entry",
        version=4,
        minor_version=1,
        data=deepcopy(dict(first.data)),
        options=deepcopy(dict(first.options)),
    )
    second.add_to_hass(hass)
    assert store_cleanup._entry_gateways(second) == store_cleanup._entry_gateways(first)
    with (
        patch(
            "custom_components.bosch_buderus_heating.recordings.manager."
            "async_offer_statistics_removal",
            new=AsyncMock(),
        ) as offer,
        patch(
            "custom_components.bosch_buderus_heating.estimated_energy."
            "estimated_energy_store"
        ) as energy,
    ):
        energy.return_value.async_forget_gateways = AsyncMock()
        await async_remove_entry_stores(hass, first)
    # The gateways are still in use: no offer to delete their statistics and
    # no dropped totals.
    offer.assert_not_awaited()
    energy.return_value.async_forget_gateways.assert_not_awaited()


# ------------------------------------------------------------ the sweep


async def test_a_missing_storage_folder_has_nothing_to_sweep(
    hass, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    hass.config.config_dir = str(tmp_path / "nowhere")
    with caplog.at_level(logging.DEBUG, logger=store_cleanup.__name__):
        await async_sweep_orphaned_stores(hass)
    assert not (tmp_path / "nowhere").exists()
    assert "could not be listed" not in caplog.text


async def test_an_unreadable_storage_folder_is_not_an_error(
    hass, storage_dir, caplog: pytest.LogCaptureFixture
) -> None:
    (storage_dir / f"{DOMAIN}.inventory.removed-entry").write_text("{}")
    with (
        patch.object(
            hass, "async_add_executor_job", AsyncMock(side_effect=OSError("denied"))
        ),
        caplog.at_level(logging.DEBUG),
    ):
        await async_sweep_orphaned_stores(hass)
    assert (storage_dir / f"{DOMAIN}.inventory.removed-entry").exists()
    assert "The storage folder could not be listed" in caplog.text


async def test_files_that_cannot_be_deleted_only_warn(
    hass, storage_dir, caplog: pytest.LogCaptureFixture
) -> None:
    orphan = storage_dir / f"{DOMAIN}.inventory.removed-entry"
    orphan.write_text("{}")
    real = hass.async_add_executor_job
    calls = 0

    async def job(function, *args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await real(function, *args)  # the listing works
        raise OSError("read-only")

    with (
        patch.object(hass, "async_add_executor_job", job),
        caplog.at_level(logging.INFO),
    ):
        await async_sweep_orphaned_stores(hass)
    assert "Files of removed entries could not be deleted" in caplog.text
    assert "Removed 1 stored files" not in caplog.text
    assert orphan.exists()


async def test_a_sweep_without_orphans_logs_nothing(
    hass, storage_dir, caplog: pytest.LogCaptureFixture
) -> None:
    entry, _, _ = entry_for(hass)
    (storage_dir / f"{DOMAIN}.inventory.{entry.entry_id}").write_text("{}")
    with caplog.at_level(logging.INFO):
        await async_sweep_orphaned_stores(hass)
    assert "Removed" not in caplog.text
    assert (storage_dir / f"{DOMAIN}.inventory.{entry.entry_id}").exists()
