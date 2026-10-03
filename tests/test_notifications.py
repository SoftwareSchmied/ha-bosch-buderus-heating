"""Fault notification behavior across polling, dismissal, failures and restarts."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import persistent_notification as pn
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, flush_store

from custom_components.bosch_buderus_heating.const import (
    CONF_FAULT_NOTIFICATIONS,
    CONF_GATEWAY_IDS,
    DOMAIN,
)
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.notifications import (
    FaultNotifications,
    gateway_notification_key,
    notification_id,
    notification_policy,
    notification_store,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    Resource,
)

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
A = {"ccd": "6249", "fc": "12", "occurrenceId": "a"}
B = {"ccd": "1038", "fc": "12", "occurrenceId": "b"}


def _apply(tracker, *faults, when=NOW):
    resource = Resource(path="/notifications", values=faults, has_values=True)
    tracker.process_resources({resource.path: resource}, observed_at=when)


@pytest.fixture
async def rig(hass):
    await hass.config.async_set_time_zone("UTC")
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_GATEWAY_IDS: ["gateway-one"]})
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one"), entry
    )
    events = []
    current = {}

    @callback
    def record(kind, notifications):
        events.append((kind, dict(notifications)))
        for key, value in notifications.items():
            if kind is pn.UpdateType.REMOVED:
                current.pop(key, None)
            else:
                current[key] = value

    remove = pn.async_register_callback(hass, record)
    manager = FaultNotifications(hass, entry, coordinator)
    yield entry, coordinator, manager, current, events
    await manager.async_close()
    await coordinator.async_shutdown()
    remove()


async def test_initial_fault_is_shown_once_and_polling_does_not_rewrite(hass, rig):
    entry, coordinator, manager, current, events = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    await hass.async_block_till_done()
    assert len(current) == 1
    item = current[notification_id(entry.entry_id, "gateway-one")]
    assert "6249" in item["message"]
    assert "Communication between" in item["message"]
    assert "not yet been confirmed" not in item["message"]
    for _ in range(5):
        _apply(coordinator.faults, A)
    await hass.async_block_till_done()
    assert len(events) == 1
    assert item["created_at"] == next(iter(current.values()))["created_at"]
    coordinator.client.get_resources_bulk.assert_not_called()


async def test_no_initial_all_clear_or_notification_for_warning(hass, rig):
    _, coordinator, manager, current, _ = rig
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, {"ccd": "warning", "fc": "WARNING"})
    await hass.async_block_till_done()
    assert not current


async def test_dismissal_survives_subset_resolution_and_same_poll(hass, rig):
    entry, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A, B)
    await manager.async_start()
    pn.async_dismiss(hass, notification_id(entry.entry_id, "gateway-one"))
    await hass.async_block_till_done()
    _apply(coordinator.faults, A, B)
    _apply(coordinator.faults, B)
    _apply(coordinator.faults, B)
    await hass.async_block_till_done()
    assert not current
    _apply(coordinator.faults, B, {**A, "occurrenceId": "new-a"})
    await hass.async_block_till_done()
    assert len(current) == 1
    assert "Active faults: 2" in next(iter(current.values()))["message"]


async def test_same_code_recurrence_and_severity_escalation_reappear(hass, rig):
    entry, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    pn.async_dismiss_all(hass)
    await hass.async_block_till_done()
    _apply(coordinator.faults, {**A, "fc": "CRITICAL"})
    await hass.async_block_till_done()
    assert current
    pn.async_dismiss(hass, notification_id(entry.entry_id, "gateway-one"))
    await hass.async_block_till_done()
    _apply(coordinator.faults)
    _apply(coordinator.faults)
    await hass.async_block_till_done()
    assert not current
    _apply(coordinator.faults, A, when=NOW + timedelta(hours=1))
    await hass.async_block_till_done()
    assert current


@pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
async def test_failed_reads_cannot_resolve_and_restart_empty_confirmation(
    hass, rig, status
):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    _apply(coordinator.faults)
    coordinator.faults.record_results(
        (BatchItemResult("gateway-one", "/notifications", status),)
    )
    coordinator.faults.process_resources({})
    await hass.async_block_till_done()
    assert "not yet been confirmed" in next(iter(current.values()))["message"]
    _apply(coordinator.faults)
    await hass.async_block_till_done()
    assert "6249" in next(iter(current.values()))["message"]
    _apply(coordinator.faults)
    await hass.async_block_till_done()
    assert (
        "previously reported faults are no longer active"
        in next(iter(current.values()))["message"]
    )


async def test_partial_invalid_response_never_resolves(hass, rig):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    for _ in range(3):
        _apply(coordinator.faults, "invalid")
    await hass.async_block_till_done()
    assert "6249" in next(iter(current.values()))["message"]
    assert "not yet been confirmed" in next(iter(current.values()))["message"]


async def test_dismissed_incident_survives_reloaded_tracker(hass, rig):
    entry, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    pn.async_dismiss_all(hass)
    await hass.async_block_till_done()
    await manager.async_close()
    restarted = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one"), entry
    )
    await restarted.async_load_fault_state()
    restored = FaultNotifications(hass, entry, restarted)
    await restored.async_start()
    _apply(restarted.faults, A)
    await hass.async_block_till_done()
    assert not current
    await restored.async_close()
    await restarted.async_shutdown()


async def test_restored_undismissed_fault_is_explicitly_unconfirmed(hass, rig):
    entry, coordinator, _manager, current, _ = rig
    _apply(coordinator.faults, A)
    await coordinator.faults._store.async_save(coordinator.faults._serialize())
    restarted = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one"), entry
    )
    await restarted.async_load_fault_state()
    restored = FaultNotifications(hass, entry, restarted)
    await restored.async_start()
    await hass.async_block_till_done()
    assert "not yet been confirmed" in next(iter(current.values()))["message"]
    _apply(restarted.faults, A)
    await hass.async_block_till_done()
    assert "not yet been confirmed" not in next(iter(current.values()))["message"]
    await restored.async_close()
    await restarted.async_shutdown()


async def test_switch_off_and_explicit_reenable_show_existing_fault(hass, rig):
    entry, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    pn.async_dismiss_all(hass)
    await hass.async_block_till_done()
    key = gateway_notification_key("gateway-one")
    hass.config_entries.async_update_entry(
        entry, options={CONF_FAULT_NOTIFICATIONS: {key: {"enabled": False, "reset": 0}}}
    )
    manager.async_update()
    await hass.async_block_till_done()
    assert not current
    hass.config_entries.async_update_entry(
        entry, options={CONF_FAULT_NOTIFICATIONS: {key: {"enabled": True, "reset": 1}}}
    )
    manager.async_update()
    await hass.async_block_till_done()
    assert current
    coordinator.client.get_resources_bulk.assert_not_called()


async def test_two_gateways_do_not_share_notifications_or_dismissals(hass, rig):
    entry, coordinator, manager, current, _ = rig
    other = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-two"), entry
    )
    other_manager = FaultNotifications(hass, entry, other)
    _apply(coordinator.faults, A)
    _apply(other.faults, A)
    await manager.async_start()
    await other_manager.async_start()
    await hass.async_block_till_done()
    assert len(current) == 2
    pn.async_dismiss(hass, notification_id(entry.entry_id, "gateway-one"))
    await hass.async_block_till_done()
    _apply(coordinator.faults, A)
    _apply(other.faults, A)
    await hass.async_block_till_done()
    assert list(current) == [notification_id(entry.entry_id, "gateway-two")]
    await other_manager.async_close()
    await other.async_shutdown()


@pytest.mark.parametrize(
    "language,phrase",
    [
        ("de", "Anlagenstörung"),
        ("en", "heating system fault"),
        ("fr", "heating system fault"),
    ],
)
async def test_device_name_link_and_system_language(hass, rig, language, phrase):
    entry, coordinator, manager, current, _ = rig
    hass.config.language = language
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "gateway-one")},
        name="Heating",
    )
    dr.async_get(hass).async_update_device(device.id, name_by_user="My installation")
    _apply(coordinator.faults, A)
    await manager.async_start()
    await hass.async_block_till_done()
    item = next(iter(current.values()))
    assert item["title"] == f"My installation: {phrase}"
    assert f"/config/devices/device/{device.id}" in item["message"]
    assert "gateway-one" not in str(item)


async def test_notification_does_not_consume_existing_pending_lifecycle_events(
    hass, rig
):
    _, coordinator, manager, _, _ = rig
    _apply(coordinator.faults)
    _apply(coordinator.faults, A)
    events = []
    await manager.async_start()
    remove = coordinator.faults.async_add_listener(events.append)
    assert len(events) == 1 and events[0].event_type == "appeared"
    _apply(coordinator.faults)
    _apply(coordinator.faults)
    assert len(events) == 2 and events[1].event_type == "resolved"
    remove()


@pytest.mark.parametrize(
    "stored",
    [
        None,
        [],
        {},
        {"dismissed": []},
        {"dismissed": {"invalid": 2, "a" * 64: "bad", "b" * 64: True}, "reset": -1},
    ],
)
async def test_invalid_local_storage_cannot_hide_faults(hass, rig, stored):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    with patch.object(manager._store, "async_load", AsyncMock(return_value=stored)):
        await manager.async_start()
    await hass.async_block_till_done()
    assert current


async def test_storage_and_display_failures_do_not_break_fault_tracking(hass, rig):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    with patch.object(manager._store, "async_load", AsyncMock(side_effect=OSError())):
        await manager.async_start()
    with patch(
        "custom_components.bosch_buderus_heating.notifications.pn.async_create",
        side_effect=RuntimeError(),
    ):
        _apply(coordinator.faults, A, B)
        _apply(coordinator.faults, A, B)
    assert len(coordinator.faults.active_faults) == 2
    _apply(coordinator.faults, A, B)
    await hass.async_block_till_done()
    assert "Active faults: 2" in next(iter(current.values()))["message"]


async def test_long_untrusted_details_are_bounded_and_not_rendered_as_markup(hass, rig):
    _, coordinator, manager, current, _ = rig
    faults = tuple(
        {
            "ccd": f"<script>[link](https://invalid.example)/{index}",
            "fc": "12",
            "occurrenceId": str(index),
        }
        for index in range(30)
    )
    _apply(coordinator.faults, *faults)
    await manager.async_start()
    await hass.async_block_till_done()
    message = next(iter(current.values()))["message"]
    assert "<script>" not in message
    assert "[link](" not in message
    assert message.count("\n- ") == 25
    assert "5 additional" in message


@pytest.mark.parametrize(
    "options",
    [{}, {CONF_FAULT_NOTIFICATIONS: None}, {CONF_FAULT_NOTIFICATIONS: {"bad": False}}],
)
def test_options_default_to_enabled(options):
    assert notification_policy(options, "gateway-one") == (True, 0)


async def test_private_store_contains_only_opaque_dismissal_evidence(
    hass, rig, hass_storage
):
    entry, coordinator, manager, _, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    pn.async_dismiss_all(hass)
    await hass.async_block_till_done()
    await manager.async_close()
    stored = await notification_store(hass, entry.entry_id, "gateway-one").async_load()
    assert set(stored) == {"dismissed", "reset"}
    assert len(stored["dismissed"]) == 1
    assert all(len(key) == 64 for key in stored["dismissed"])
    assert "gateway-one" not in str(stored)
    assert "6249" not in str(stored)


async def test_resolved_notification_is_not_restored_after_immediate_reload(hass, rig):
    entry, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults)
    await hass.async_block_till_done()
    assert "faults resolved" in next(iter(current.values()))["title"]
    await manager.async_close()
    restarted = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one"), entry
    )
    await restarted.async_load_fault_state()
    restored = FaultNotifications(hass, entry, restarted)
    await restored.async_start()
    await hass.async_block_till_done()
    assert not current
    assert not restarted.faults.active_faults
    await restored.async_close()
    await restarted.async_shutdown()


async def test_native_options_listener_applies_changes_without_cloud_calls(hass, rig):
    from types import SimpleNamespace

    from custom_components.bosch_buderus_heating.notifications import (
        async_update_notification_options,
    )

    entry, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    entry.runtime_data = SimpleNamespace(notifications=(manager,))
    remove = entry.add_update_listener(async_update_notification_options)
    key = gateway_notification_key("gateway-one")
    hass.config_entries.async_update_entry(
        entry, options={CONF_FAULT_NOTIFICATIONS: {key: {"enabled": False, "reset": 0}}}
    )
    await hass.async_block_till_done()
    assert not current
    assert coordinator.faults.active_faults
    coordinator.client.get_resources_bulk.assert_not_called()
    remove()


async def test_notification_setup_failure_does_not_fail_integration(hass, rig):
    from types import SimpleNamespace

    from custom_components.bosch_buderus_heating.notifications import (
        async_setup_notifications,
    )

    entry, coordinator, _, current, _ = rig
    entry.runtime_data = SimpleNamespace(coordinators=(coordinator,))
    with patch.object(
        FaultNotifications, "async_start", AsyncMock(side_effect=RuntimeError())
    ):
        assert await async_setup_notifications(hass, entry) == ()
    _apply(coordinator.faults, A)
    assert coordinator.faults.active_faults
    assert not current


async def test_storage_failure_during_close_does_not_block_unloading(hass, rig):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    with patch.object(
        coordinator.faults, "async_flush", AsyncMock(side_effect=OSError())
    ):
        await manager.async_close()
    await hass.async_block_till_done()
    assert not current
    manager.async_update()
    assert not current


async def test_broken_fault_observer_does_not_prevent_other_observers(hass, rig):
    from unittest.mock import Mock

    _, coordinator, _, _, _ = rig
    broken = Mock(side_effect=RuntimeError())
    healthy = Mock()
    remove_broken = coordinator.faults.async_add_state_listener(broken)
    remove_healthy = coordinator.faults.async_add_state_listener(healthy)
    _apply(coordinator.faults, A)
    healthy.assert_called_once()
    assert coordinator.faults.active_faults
    remove_broken()
    remove_healthy()


async def test_identically_named_installations_remain_distinguishable(hass, rig):
    entry, coordinator, manager, current, _ = rig
    hass.config_entries.async_update_entry(
        entry, data={CONF_GATEWAY_IDS: ["gateway-one", "gateway-two"]}
    )
    for gateway in ("gateway-one", "gateway-two"):
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, gateway)},
            name="Buderus Heating",
        )
    other = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-two"), entry
    )
    other_manager = FaultNotifications(hass, entry, other)
    _apply(coordinator.faults, A)
    _apply(other.faults, B)
    await manager.async_start()
    await other_manager.async_start()
    await hass.async_block_till_done()
    assert {item["title"] for item in current.values()} == {
        "Buderus Heating (1): heating system fault",
        "Buderus Heating (2): heating system fault",
    }
    await other_manager.async_close()
    await other.async_shutdown()


@pytest.mark.parametrize(
    "language,first_seen,resolved_at,description,date",
    [
        (
            "de",
            "Erstmals erkannt",
            "Als behoben bestätigt",
            "Kommunikation zwischen",
            "03.10.2026",
        ),
        (
            "en",
            "First detected",
            "Confirmed resolved",
            "Communication between",
            "2026-10-03",
        ),
        (
            "fr",
            "First detected",
            "Confirmed resolved",
            "Communication between",
            "2026-10-03",
        ),
    ],
)
async def test_resolved_details_keep_codes_and_observation_times(
    hass, rig, language, first_seen, resolved_at, description, date
):
    _, coordinator, manager, current, events = rig
    hass.config.language = language
    await hass.config.async_set_time_zone("Europe/Berlin")
    # An appliance/cloud timestamp must not replace the HA observation time.
    _apply(coordinator.faults, {**A, "dcd": "X7", "t": "2026-10-02T00:00:00Z"})
    await manager.async_start()
    _apply(coordinator.faults, when=NOW + timedelta(minutes=1))
    _apply(coordinator.faults, when=NOW + timedelta(minutes=2))
    await hass.async_block_till_done()
    message = next(iter(current.values()))["message"]
    assert "6249: X7" in message and description in message
    assert f"{first_seen}: {date} 14:00:00 +0200" in message
    assert f"{resolved_at}: {date} 14:02:00 +0200" in message
    assert "Home Assistant (Europe/Berlin)" in message
    before = len(events)
    for offset in (3, 4, 5):
        _apply(coordinator.faults, when=NOW + timedelta(minutes=offset))
    manager.async_update()
    await hass.async_block_till_done()
    assert len(events) == before
    coordinator.client.get_resources_bulk.assert_not_called()


async def test_partial_resolution_keeps_each_fault_in_the_correct_section(hass, rig):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A, B)
    await manager.async_start()
    _apply(coordinator.faults, B, when=NOW + timedelta(minutes=1))
    _apply(coordinator.faults, B, when=NOW + timedelta(minutes=2))
    await hass.async_block_till_done()
    message = next(iter(current.values()))["message"]
    active, resolved = message.split("Confirmed resolved: 1", 1)
    assert "Active faults: 1" in active and "1038" in active and "6249" not in active
    assert "6249" in resolved and "1038" not in resolved
    _apply(coordinator.faults, when=NOW + timedelta(minutes=3))
    _apply(coordinator.faults, when=NOW + timedelta(minutes=4))
    await hass.async_block_till_done()
    item = next(iter(current.values()))
    assert "faults resolved" in item["title"]
    assert "Confirmed resolved: 2" in item["message"]
    assert "6249" in item["message"] and "1038" in item["message"]
    assert "12:02:00 +0000" in item["message"]
    assert "12:04:00 +0000" in item["message"]


async def test_same_code_recurrence_keeps_distinct_observed_incidents(hass, rig):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    _apply(coordinator.faults, when=NOW + timedelta(minutes=1))
    _apply(coordinator.faults, when=NOW + timedelta(minutes=2))
    _apply(coordinator.faults, A, when=NOW + timedelta(hours=1))
    await hass.async_block_till_done()
    message = next(iter(current.values()))["message"]
    active, resolved = message.split("Confirmed resolved: 1", 1)
    assert "6249" in active and "13:00:00" in active
    assert "6249" in resolved and "12:00:00" in resolved and "12:02:00" in resolved


@pytest.mark.parametrize("severity", ["WARNING", "MAINTENANCE", "INFO"])
async def test_reclassification_is_not_reported_as_resolution(hass, rig, severity):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    _apply(coordinator.faults, {**A, "fc": severity}, when=NOW + timedelta(minutes=1))
    await hass.async_block_till_done()
    item = next(iter(current.values()))
    assert "faults resolved" not in item["title"]
    assert "6249" in item["message"]
    assert "Still reported with a changed classification: 1" in item["message"]
    assert "Confirmed resolved" not in item["message"]
    coordinator.faults.mark_unavailable()
    await hass.async_block_till_done()
    assert "not yet been confirmed" in next(iter(current.values()))["message"]
    _apply(coordinator.faults, when=NOW + timedelta(minutes=2))
    _apply(coordinator.faults, when=NOW + timedelta(minutes=3))
    await hass.async_block_till_done()
    item = next(iter(current.values()))
    assert "faults resolved" in item["title"] and "6249" in item["message"]
    assert "Confirmed resolved:" in item["message"]


async def test_dismissed_details_do_not_leak_into_the_next_notification(hass, rig):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A, B)
    await manager.async_start()
    _apply(coordinator.faults, B)
    _apply(coordinator.faults, B)
    pn.async_dismiss_all(hass)
    await hass.async_block_till_done()
    _apply(coordinator.faults, B, {"ccd": "new", "fc": "12"})
    await hass.async_block_till_done()
    message = next(iter(current.values()))["message"]
    assert "6249" not in message and "Confirmed resolved:" not in message
    assert "1038" in message and "new" in message


async def test_disable_clears_resolved_details_before_reenable(hass, rig):
    entry, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A, B)
    await manager.async_start()
    _apply(coordinator.faults, B)
    _apply(coordinator.faults, B)
    key = gateway_notification_key("gateway-one")
    for enabled, reset in ((False, 0), (True, 1)):
        hass.config_entries.async_update_entry(
            entry,
            options={
                CONF_FAULT_NOTIFICATIONS: {key: {"enabled": enabled, "reset": reset}}
            },
        )
        manager.async_update()
        await hass.async_block_till_done()
    message = next(iter(current.values()))["message"]
    assert "1038" in message and "6249" not in message


async def test_unconfirmed_reads_never_add_resolved_details(hass, rig):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    _apply(coordinator.faults)
    coordinator.faults.mark_unavailable()
    _apply(coordinator.faults)
    await hass.async_block_till_done()
    message = next(iter(current.values()))["message"]
    assert "6249" in message and "Confirmed resolved:" not in message
    _apply(coordinator.faults, "invalid")
    await hass.async_block_till_done()
    assert "Confirmed resolved:" not in next(iter(current.values()))["message"]


async def test_resolution_history_is_bounded_and_explains_omissions(hass, rig):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, B)
    await manager.async_start()
    for index in range(28):
        _apply(
            coordinator.faults,
            B,
            {"ccd": f"OLD-{index:02}", "fc": "12"},
            when=NOW + timedelta(minutes=index * 3),
        )
        _apply(coordinator.faults, B, when=NOW + timedelta(minutes=index * 3 + 1))
        _apply(coordinator.faults, B, when=NOW + timedelta(minutes=index * 3 + 2))
    await hass.async_block_till_done()
    message = next(iter(current.values()))["message"]
    assert "Active faults: 1" in message and "Confirmed resolved: 28" in message
    assert "3 older resolved faults" in message
    assert "OLD-00" not in message and "OLD-02" not in message
    assert "OLD-03" in message and "OLD-27" in message
    assert message.count("\n- ") == 26


async def test_resolved_untrusted_codes_are_escaped_and_not_persisted(hass, rig):
    entry, coordinator, manager, current, _ = rig
    _apply(
        coordinator.faults,
        {"ccd": "<script>[link](https://invalid.example)", "fc": "12"},
    )
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults)
    await hass.async_block_till_done()
    message = next(iter(current.values()))["message"]
    assert "&lt;script" in message and "[link](" not in message
    await manager.async_close()
    stored = await notification_store(hass, entry.entry_id, "gateway-one").async_load()
    assert stored is None or set(stored) == {"dismissed", "reset"}
    assert "script" not in str(stored)


async def test_observer_keeps_future_events_queued_for_the_event_entity(hass, rig):
    _, coordinator, manager, _, _ = rig
    _apply(coordinator.faults)
    await manager.async_start()
    _apply(coordinator.faults, A)
    _apply(coordinator.faults)
    _apply(coordinator.faults)
    events = []
    remove = coordinator.faults.async_add_listener(events.append)
    assert [event.event_type for event in events] == ["appeared", "resolved"]
    assert all(event.fault.code == "6249" for event in events)
    remove()


async def test_restart_preserves_active_first_seen_but_does_not_restore_history(
    hass, rig
):
    entry, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A, B)
    await manager.async_start()
    _apply(coordinator.faults, B)
    _apply(coordinator.faults, B)
    await manager.async_close()
    restarted = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one"), entry
    )
    await restarted.async_load_fault_state()
    restored = FaultNotifications(hass, entry, restarted)
    await restored.async_start()
    _apply(restarted.faults, B, when=NOW + timedelta(hours=1))
    await hass.async_block_till_done()
    message = next(iter(current.values()))["message"]
    assert "1038" in message and "6249" not in message
    assert "First detected: 2026-10-03 12:00:00" in message
    await restored.async_close()
    await restarted.async_shutdown()


async def test_observation_times_disambiguate_clock_change(hass, rig):
    _, coordinator, manager, current, _ = rig
    await hass.config.async_set_time_zone("Europe/Berlin")
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    _apply(coordinator.faults, A, when=first)
    await manager.async_start()
    _apply(coordinator.faults, when=first + timedelta(minutes=59))
    _apply(coordinator.faults, when=first + timedelta(hours=1))
    await hass.async_block_till_done()
    message = next(iter(current.values()))["message"]
    assert "First detected: 2026-10-25 02:30:00 +0200" in message
    assert "Confirmed resolved: 2026-10-25 02:30:00 +0100" in message


@pytest.mark.parametrize(
    "crash_point,visible_on_restart",
    [
        ("active_saved", True),
        ("dismiss_pending", True),
        ("dismiss_saved", False),
        ("first_absence", True),
        ("resolved_pending", True),
        ("resolved_saved", False),
        ("new_fault_pending", False),
        ("dismiss_saved_before_new_baseline", False),
    ],
)
async def test_crash_restart_uses_only_durable_snapshots(
    hass, rig, hass_storage, crash_point, visible_on_restart
):
    entry, coordinator, manager, current, _ = rig
    _apply(coordinator.faults)
    await flush_store(coordinator.faults._store)
    _apply(coordinator.faults, A)
    if crash_point not in {"new_fault_pending", "dismiss_saved_before_new_baseline"}:
        await flush_store(coordinator.faults._store)
    await manager.async_start()
    if crash_point.startswith("dismiss"):
        pn.async_dismiss_all(hass)
        if crash_point != "dismiss_pending":
            await flush_store(manager._store)
    if crash_point in {"first_absence", "resolved_pending", "resolved_saved"}:
        _apply(coordinator.faults, when=NOW + timedelta(minutes=1))
    if crash_point.startswith("resolved"):
        _apply(coordinator.faults, when=NOW + timedelta(minutes=2))
        if crash_point == "resolved_saved":
            await flush_store(coordinator.faults._store)

    # Capture the persisted files, not Store.async_load(), which can return
    # pending in-memory writes. A crash does not run manager.async_close().
    durable = deepcopy(hass_storage)
    manager.stop()
    restarted = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one"), entry
    )
    restored = FaultNotifications(hass, entry, restarted)
    with (
        patch.object(
            restarted.faults._store,
            "async_load",
            AsyncMock(
                return_value=durable.get(restarted.faults._store.key, {}).get("data")
            ),
        ),
        patch.object(
            restored._store,
            "async_load",
            AsyncMock(return_value=durable.get(restored._store.key, {}).get("data")),
        ),
    ):
        await restarted.async_load_fault_state()
        await restored.async_start()
    await hass.async_block_till_done()
    assert bool(current) is visible_on_restart
    if current:
        item = next(iter(current.values()))
        assert "faults resolved" not in item["title"]
        assert "not yet been confirmed" in item["message"]
    if crash_point in {"first_absence", "resolved_pending"}:
        _apply(restarted.faults, when=NOW + timedelta(minutes=3))
        await hass.async_block_till_done()
        assert "faults resolved" not in next(iter(current.values()))["title"]
        _apply(restarted.faults, when=NOW + timedelta(minutes=4))
        await hass.async_block_till_done()
        assert "6249" in next(iter(current.values()))["message"]
        assert "faults resolved" in next(iter(current.values()))["title"]
    elif crash_point in {"new_fault_pending", "dismiss_saved_before_new_baseline"}:
        _apply(restarted.faults, A, when=NOW + timedelta(hours=1))
        await hass.async_block_till_done()
        assert "6249" in next(iter(current.values()))["message"]
    await restored.async_close()
    await restarted.async_shutdown()


async def test_native_dismiss_and_new_fault_in_same_turn(hass, rig):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    pn.async_dismiss_all(hass)
    _apply(coordinator.faults, A, B)
    await hass.async_block_till_done()
    assert len(current) == 1
    assert "1038" in next(iter(current.values()))["message"]


async def test_disable_and_reenable_in_same_turn_keeps_the_new_notification(hass, rig):
    entry, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    key = gateway_notification_key("gateway-one")
    for enabled, reset in ((False, 0), (True, 1)):
        hass.config_entries.async_update_entry(
            entry,
            options={
                CONF_FAULT_NOTIFICATIONS: {key: {"enabled": enabled, "reset": reset}}
            },
        )
        manager.async_update()
    await hass.async_block_till_done()
    assert len(current) == 1
    _apply(coordinator.faults, A)
    await hass.async_block_till_done()
    assert len(current) == 1


async def test_tracker_save_scheduling_failure_preserves_resolution_details(hass, rig):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    _apply(coordinator.faults)
    with patch.object(
        coordinator.faults._store,
        "async_delay_save",
        side_effect=OSError("test disk unavailable"),
    ):
        _apply(coordinator.faults)
    await hass.async_block_till_done()
    item = next(iter(current.values()))
    assert "faults resolved" in item["title"] and "6249" in item["message"]


async def test_dismissal_save_scheduling_failure_does_not_reopen_same_incident(
    hass, rig
):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    with patch.object(
        manager._store, "async_delay_save", side_effect=OSError("test disk unavailable")
    ):
        pn.async_dismiss_all(hass)
    _apply(coordinator.faults, {**A, "ccd": "updated description"})
    await hass.async_block_till_done()
    assert not current


async def test_failed_preference_save_does_not_prevent_disabling_notifications(
    hass, rig
):
    entry, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    key = gateway_notification_key("gateway-one")
    hass.config_entries.async_update_entry(
        entry, options={CONF_FAULT_NOTIFICATIONS: {key: {"enabled": False, "reset": 1}}}
    )
    with patch.object(
        manager._store, "async_delay_save", side_effect=OSError("test disk unavailable")
    ):
        manager.async_update()
    await hass.async_block_till_done()
    assert not current


async def test_close_attempts_both_independent_storage_writes(hass, rig, hass_storage):
    _, coordinator, manager, _, _ = rig
    _apply(coordinator.faults, A)
    await coordinator.faults.async_flush()
    await manager.async_start()
    pn.async_dismiss_all(hass)
    with patch.object(
        coordinator.faults, "async_flush", AsyncMock(side_effect=OSError())
    ):
        await manager.async_close()
    assert hass_storage[manager._store.key]["data"]["dismissed"]


async def test_stop_while_preferences_load_cannot_register_late_listeners(hass, rig):
    _, coordinator, manager, current, _ = rig
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_load():
        entered.set()
        await release.wait()
        return None

    with patch.object(manager._store, "async_load", side_effect=slow_load):
        startup = asyncio.create_task(manager.async_start())
        await entered.wait()
        manager.stop()
        release.set()
        await startup
    _apply(coordinator.faults, A)
    await hass.async_block_till_done()
    assert not current
    assert not coordinator.faults._event_observers
    assert not coordinator.faults._state_listeners


async def test_cancelled_multi_gateway_setup_removes_already_started_managers(
    hass, rig
):
    from types import SimpleNamespace

    from custom_components.bosch_buderus_heating.notifications import (
        async_setup_notifications,
    )

    entry, coordinator, _, current, _ = rig
    other = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-two"), entry
    )
    entry.runtime_data = SimpleNamespace(coordinators=(coordinator, other))
    _apply(coordinator.faults, A)
    entered = asyncio.Event()
    original_start = FaultNotifications.async_start
    instances = []

    async def start(manager):
        instances.append(manager)
        if len(instances) == 2:
            entered.set()
            await asyncio.Event().wait()
        await original_start(manager)

    try:
        with patch.object(FaultNotifications, "async_start", start):
            task = asyncio.create_task(async_setup_notifications(hass, entry))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await hass.async_block_till_done()
        assert not current
        assert not coordinator.faults._event_observers
    finally:
        for manager in instances:
            manager.stop()
        await other.async_shutdown()


async def test_unreadable_baseline_allows_fresh_cloud_faults(hass, rig):
    _, coordinator, manager, current, _ = rig
    with patch.object(
        coordinator.faults._store,
        "async_load",
        AsyncMock(side_effect=OSError("test disk unavailable")),
    ):
        await coordinator.async_load_fault_state()
    await manager.async_start()
    assert not current
    _apply(coordinator.faults, A)
    await hass.async_block_till_done()
    assert "6249" in next(iter(current.values()))["message"]


async def test_failed_storage_queue_retries_on_normal_updates(hass, rig, hass_storage):
    _, coordinator, manager, current, _ = rig
    with patch.object(
        coordinator.faults._store, "async_delay_save", side_effect=OSError()
    ):
        _apply(coordinator.faults, A)
    await manager.async_start()
    with patch.object(manager._store, "async_delay_save", side_effect=OSError()):
        pn.async_dismiss_all(hass)
    _apply(coordinator.faults, A)
    await flush_store(coordinator.faults._store)
    await flush_store(manager._store)
    assert (
        hass_storage[coordinator.faults._store.key]["data"]["active"][0]["code"]
        == "6249"
    )
    assert hass_storage[manager._store.key]["data"]["dismissed"]
    assert not current


async def test_repeated_reload_removes_old_observers_and_preserves_first_seen(
    hass, rig
):
    entry, coordinator, manager, current, events = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    for offset in range(1, 6):
        await manager.async_close()
        await coordinator.async_shutdown()
        assert not coordinator.faults._event_observers
        assert not coordinator.faults._state_listeners
        coordinator = BoschBuderusDataUpdateCoordinator(
            hass, AsyncMock(), Gateway("gateway-one"), entry
        )
        await coordinator.async_load_fault_state()
        _apply(coordinator.faults, A, when=NOW + timedelta(minutes=offset))
        manager = FaultNotifications(hass, entry, coordinator)
        before = len(events)
        await manager.async_start()
        # A second start must not install duplicate listeners.
        await manager.async_start()
        await hass.async_block_till_done()
        assert len(events) == before + 1
        assert len(current) == 1
        assert (
            "First detected: 2026-10-03 12:00:00"
            in next(iter(current.values()))["message"]
        )
    await manager.async_close()
    await coordinator.async_shutdown()


async def test_normal_shutdown_flushes_pending_dismissal(hass, rig, hass_storage):
    _, coordinator, manager, _, _ = rig
    _apply(coordinator.faults, A)
    await manager.async_start()
    pn.async_dismiss_all(hass)
    # The normal HA final-write event must persist both stores even if the
    # one-second delay has not elapsed and only synchronous cleanup has run.
    manager.stop()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert (
        hass_storage[coordinator.faults._store.key]["data"]["active"][0]["code"]
        == "6249"
    )
    assert hass_storage[manager._store.key]["data"]["dismissed"]


async def test_overlapping_starts_register_observers_only_once(hass, rig):
    _, coordinator, manager, current, _ = rig
    _apply(coordinator.faults, A)
    release = asyncio.Event()

    async def load():
        await release.wait()
        return None

    with (
        patch.object(manager._store, "async_load", side_effect=load),
        patch.object(
            coordinator.faults,
            "async_add_event_observer",
            wraps=coordinator.faults.async_add_event_observer,
        ) as register,
    ):
        first = asyncio.create_task(manager.async_start())
        second = asyncio.create_task(manager.async_start())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        register.assert_called_once()
    await hass.async_block_till_done()
    assert len(current) == 1
