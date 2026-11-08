"""Managed diagnostics, notices, repairs and dialogs through real HA APIs."""

import json
from copy import deepcopy
from dataclasses import replace
from time import monotonic
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import persistent_notification as pn
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating.config_flow import BoschBuderusOptionsFlow
from custom_components.bosch_buderus_heating.const import (
    CONF_FAULT_NOTIFICATIONS,
    DOMAIN,
    FIRMWARE_ISSUE_PREFIX,
    RATE_LIMIT_ISSUE_PREFIX,
)
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.entry_sources import notification_gateways
from custom_components.bosch_buderus_heating.inventory import build_anonymized_inventory
from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry
from custom_components.bosch_buderus_heating.notifications import (
    gateway_notification_key,
    notification_id,
)
from custom_components.bosch_buderus_heating.pointt import RateLimited
from custom_components.bosch_buderus_heating.repair_scope import repair_data, repair_id
from custom_components.bosch_buderus_heating.repairs import async_create_fix_flow
from tests.test_config_flow import _entry_with_writable_holiday, _holiday_form_input
from tests.test_dynamic_controls import snapshots
from tests.test_heating_controls import scalar
from tests.test_managed_bacon import Broker, connected
from tests.test_managed_bacon import ready as bacon_ready
from tests.test_managed_pointt import (
    TEMP,
    Backend,
    binding_for,
    eventually,
    running,
    source_for,
)
from tests.test_managed_wallbox import WallboxBackend
from tests.test_notifications import A, _apply
from tests.test_rrc import RESOURCES, WEEK

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
HEAT = "000123"


class FaultBackend(Backend):
    def resource(self, gateway, path):
        if path == "/notifications":
            return {"id": path, "values": [A]}
        return super().resource(gateway, path)


@pytest.fixture
def notices(hass):
    current = {}

    @callback  # deliver notification changes in order on every HA release
    def capture(kind, notifications):
        for key, value in notifications.items():
            if kind is pn.UpdateType.REMOVED:
                current.pop(key, None)
            else:
                current[key] = value

    remove = pn.async_register_callback(hass, capture)
    yield current
    remove()


def flow_for(hass, entry):
    flow = BoschBuderusOptionsFlow(entry)
    flow.hass = hass
    return flow


async def ready(owner):
    await eventually(lambda: source_for(owner, HEAT) and source_for(owner, "gateway-b"))
    await eventually(lambda: not owner.services._jobs)


async def reconnect(owner, cloud):
    old = source_for(owner, HEAT)
    manager = owner.connections.management
    manager.set_binding_enabled(manager.edit(cloud), binding_for(owner, HEAT), False)
    await eventually(lambda: source_for(owner, HEAT) is None)
    manager.set_binding_enabled(manager.edit(cloud), binding_for(owner, HEAT), True)
    await eventually(
        lambda: source_for(owner, HEAT) and source_for(owner, HEAT) is not old
    )
    await eventually(lambda: not owner.services._jobs)
    return source_for(owner, HEAT)


@pytest.mark.parametrize("state", ["online", "disabled", "removed"])
async def test_diagnostics_keep_scoped_relationships_without_requests_or_ids(
    hass, state
):
    backend = Backend()
    async with running(hass, backend, second_cloud=True) as (entry, cloud, owner, _):
        await ready(owner)
        manager = owner.connections.management
        if state == "disabled":
            manager.set_enabled(manager.edit(cloud), False)
        elif state == "removed":
            manager.remove(manager.edit(cloud))
        calls = list(backend.calls)
        result = await async_get_config_entry_diagnostics(hass, entry)
        assert backend.calls == calls
        assert result["diagnostics_schema"] == 43
        assert result["setup"]["runtime_available"]
        assert len(result["connections"]) == (2 if state == "removed" else 3)
        assert len(result["targets"]) == len(manager.current().targets)
        assert result["request_metrics"] == {}
        assert result["sources"]
        for item in result["gateways"]:
            assert item["target"] in {t["target"] for t in result["targets"]}
            assert item["connection"] in {
                c["connection"] for c in result["connections"]
            }
        serialized = json.dumps(result)
        for private in (entry.entry_id, HEAT, "gateway-b", cloud, "softener"):
            assert json.dumps(private) not in serialized
        inventory = build_anonymized_inventory(result)
        assert inventory["gateway_count"] == len(result["gateways"])
        assert inventory["privacy"]["contains_raw_resource_values"] is False
        assert "connections" not in inventory and "sources" not in inventory


@pytest.mark.parametrize("action", ["binding", "connection", "remove"])
@pytest.mark.parametrize("language", ["de", "en"])
async def test_visible_fault_remains_unconfirmed_after_access_stops(
    hass, notices, action, language
):
    hass.config.language = language
    async with running(hass, FaultBackend(), second_cloud=True) as (
        entry,
        cloud,
        owner,
        _,
    ):
        await ready(owner)
        key = notification_id(entry.entry_id, HEAT)
        await eventually(lambda: key in notices)
        peer = source_for(owner, "gateway-b")
        old = source_for(owner, HEAT)
        manager = owner.connections.management
        if action == "binding":
            manager.set_binding_enabled(
                manager.edit(cloud), binding_for(owner, HEAT), False
            )
        elif action == "connection":
            manager.set_enabled(manager.edit(cloud), False)
        else:
            manager.remove(manager.edit(cloud))
        await eventually(lambda: owner.services.notifications[HEAT].coordinator is None)
        await hass.async_block_till_done()
        assert key in notices and "6249" in notices[key]["message"]
        expected = (
            "noch nicht bestätigt" if language == "de" else "not yet been confirmed"
        )
        assert expected in notices[key]["message"]
        _apply(old.faults)
        _apply(old.faults)
        assert expected in notices[key]["message"]
        assert source_for(owner, "gateway-b") is peer
        assert HEAT in notification_gateways(entry)
        pn.async_dismiss(hass, key)
        await hass.async_block_till_done()
        assert key not in notices


@pytest.mark.parametrize("dismiss", [False, True])
async def test_notification_rebind_preserves_dismissal_and_requires_fresh_resolution(
    hass, notices, dismiss
):
    async with running(hass, FaultBackend()) as (entry, cloud, owner, _):
        await ready(owner)
        key = notification_id(entry.entry_id, HEAT)
        await eventually(lambda: key in notices)
        notification = owner.services.notifications[HEAT]
        manager = owner.connections.management
        manager.set_binding_enabled(
            manager.edit(cloud), binding_for(owner, HEAT), False
        )
        if dismiss:
            pn.async_dismiss(hass, key)
            await hass.async_block_till_done()
        manager.set_binding_enabled(manager.edit(cloud), binding_for(owner, HEAT), True)
        await eventually(lambda: source_for(owner, HEAT))
        await eventually(lambda: notification.coordinator is source_for(owner, HEAT))
        await hass.async_block_till_done()
        assert owner.services.notifications[HEAT] is notification
        assert (key not in notices) == dismiss
        _apply(source_for(owner, HEAT).faults)
        _apply(source_for(owner, HEAT).faults)
        await hass.async_block_till_done()
        if not dismiss:
            assert "no longer active" in notices[key]["message"]


async def test_offline_notification_option_remains_available_after_last_connection(
    hass, notices
):
    async with running(hass, FaultBackend()) as (entry, _, owner, _):
        await ready(owner)
        key = notification_id(entry.entry_id, HEAT)
        await eventually(lambda: key in notices)
        manager = owner.connections.management
        for connection in tuple(manager.current().connections):
            manager.remove(manager.edit(connection))
        flow = flow_for(hass, entry)
        form = await flow.async_step_notifications()
        assert form["type"] is FlowResultType.FORM
        result = await flow.async_step_notifications({CONF_FAULT_NOTIFICATIONS: []})
        assert result["data"]["targets"] == entry.options["targets"]
        assert not result["data"][CONF_FAULT_NOTIFICATIONS][
            gateway_notification_key(HEAT)
        ]["enabled"]
        assert not manager.current().connections


async def repair(hass, entry, connection, binding=None):
    prefix = FIRMWARE_ISSUE_PREFIX if binding else RATE_LIMIT_ISSUE_PREFIX
    issue = repair_id(prefix, entry, connection, binding)
    flow = await async_create_fix_flow(
        hass, issue, repair_data(entry, connection, binding)
    )
    flow.hass = hass
    assert (await flow.async_step_init())["type"] is FlowResultType.FORM
    return flow, issue


async def test_rate_limit_repairs_are_separate_and_change_only_affected_connection(
    hass,
):
    async with running(hass, Backend(), second_cloud=True) as (entry, cloud, owner, _):
        await ready(owner)
        peer = source_for(owner, "gateway-b")
        for source in (source_for(owner, HEAT), peer):
            for _ in range(3):
                source._activate_rate_limit_backoff(RateLimited(60), monotonic())
        first = repair_id(RATE_LIMIT_ISSUE_PREFIX, entry, cloud)
        second = repair_id(RATE_LIMIT_ISSUE_PREFIX, entry, "second")
        registry = ir.async_get(hass)
        assert registry.async_get_issue(DOMAIN, first)
        assert registry.async_get_issue(DOMAIN, second)
        old_options = deepcopy(dict(entry.options))
        flow, _ = await repair(hass, entry, cloud)
        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            assert (await flow.async_step_confirm({}))[
                "type"
            ] is FlowResultType.CREATE_ENTRY
        reload.assert_not_called()
        assert (
            entry.options["connections"][cloud]["polling_profile"] == "cloud_friendly"
        )
        assert (
            entry.options["connections"]["second"]
            == old_options["connections"]["second"]
        )
        assert source_for(owner, "gateway-b") is peer
        assert registry.async_get_issue(DOMAIN, second)


@pytest.mark.parametrize("action", ["disable", "remove", "replace"])
async def test_open_repair_cannot_change_a_modified_connection(hass, action):
    async with running(hass, Backend()) as (entry, cloud, owner, _):
        await ready(owner)
        flow, _ = await repair(hass, entry, cloud)
        manager = owner.connections.management
        if action == "remove":
            manager.remove(manager.edit(cloud))
        elif action == "disable":
            manager.set_enabled(manager.edit(cloud), False)
        else:
            current = manager.current()
            data = current.to_data()
            data["connections"][cloud]["auth_epoch"] += 1
            manager._save(data, current.to_options())
        before = deepcopy(dict(entry.options))
        result = await flow.async_step_confirm({})
        assert result["type"] is FlowResultType.ABORT
        assert entry.options == before


async def test_firmware_repair_restarts_one_source_and_keeps_peer(hass):
    async with running(hass, Backend()) as (entry, cloud, owner, _):
        await ready(owner)
        old, peer = source_for(owner, HEAT), source_for(owner, "gateway-b")
        flow, _ = await repair(hass, entry, cloud, binding_for(owner, HEAT))
        before = deepcopy(dict(entry.options))
        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            result = await flow.async_step_confirm({})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        reload.assert_not_called()
        await eventually(
            lambda: source_for(owner, HEAT) and source_for(owner, HEAT) is not old
        )
        assert source_for(owner, "gateway-b") is peer
        assert entry.options == before


@pytest.mark.parametrize("changed", [False, True])
async def test_control_dialog_writes_only_to_its_displayed_source(hass, changed):
    backend = Backend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        await ready(owner)
        flow = flow_for(hass, entry)
        await flow.async_step_controls()
        await flow.async_step_controls({"installation": gateway_notification_key(HEAT)})
        if changed:
            await reconnect(owner, cloud)
        result = await flow.async_step_operate({"controls": {"changes": {TEMP: 22}}})
        if changed:
            assert result["type"] is FlowResultType.ABORT
            assert backend.values[HEAT] == 20
        else:
            assert backend.values[HEAT] == 22
        assert backend.values["gateway-b"] == 20


@pytest.mark.parametrize("action", ["edit", "create", "delete"])
async def test_holiday_dialog_rejects_replaced_source(hass, action):
    async with running(hass, Backend()) as (entry, cloud, owner, _):
        await ready(owner)
        _, sample = _entry_with_writable_holiday(hass)
        resources = [item.resource for item in sample.data.values()]
        old = source_for(owner, HEAT)
        old.async_set_updated_data({**old.data, **snapshots(*resources)})
        flow = flow_for(hass, entry)
        choices = flow._holiday_choices()
        flow._selected_key = next(
            key
            for key, choice in choices.items()
            if choice.coordinator is old
            and key.startswith("create:") == (action == "create")
        )
        form = await flow.async_step_holiday()
        if action == "delete":
            form = await flow.async_step_delete_holiday()
        current = await reconnect(owner, cloud)
        current.async_set_updated_data({**current.data, **snapshots(*resources)})
        with (
            patch.object(current, "async_update_holiday", AsyncMock()) as update,
            patch.object(current, "async_create_holiday", AsyncMock()) as create,
            patch.object(current, "async_delete_holiday", AsyncMock()) as delete,
        ):
            result = (
                await flow.async_step_delete_holiday({"confirm_delete": True})
                if action == "delete"
                else await flow.async_step_holiday(_holiday_form_input(form))
            )
        assert result["type"] is FlowResultType.ABORT
        update.assert_not_called()
        create.assert_not_called()
        delete.assert_not_called()


async def test_rrc_selection_does_not_switch_installations_when_list_changes(hass):
    async with running(hass, Backend()) as (entry, cloud, owner, _):
        await ready(owner)
        for source in (source_for(owner, HEAT), source_for(owner, "gateway-b")):
            source.async_set_updated_data({**source.data, **snapshots(RESOURCES[WEEK])})
        flow = flow_for(hass, entry)
        await flow.async_step_rrc_programs()
        chosen = next(
            key
            for key, choice in flow._rrc_offered.items()
            if choice.coordinator is source_for(owner, HEAT)
        )
        manager = owner.connections.management
        manager.set_binding_enabled(
            manager.edit(cloud), binding_for(owner, HEAT), False
        )
        result = await flow.async_step_rrc_programs({"program": chosen})
        assert result["type"] is FlowResultType.ABORT


@pytest.mark.parametrize("dismiss", [False, True])
async def test_fault_baseline_restores_offline_without_reopening_dismissed_notice(
    hass, notices, dismiss
):
    backend = FaultBackend()
    async with running(hass, backend) as (entry, _, owner, _):
        await ready(owner)
        key = notification_id(entry.entry_id, HEAT)
        await eventually(lambda: key in notices)
        if dismiss:
            pn.async_dismiss(hass, key)
            await hass.async_block_till_done()
        manager = owner.connections.management
        for connection in tuple(manager.current().connections):
            manager.remove(manager.edit(connection))
        await owner.close()
        calls = list(backend.calls)
        restored = ManagedEntry(hass, entry, {})
        entry.runtime_data = restored
        try:
            await restored.start()
            await eventually(lambda: not restored.services._jobs)
            await hass.async_block_till_done()
            assert (key not in notices) == dismiss
            if not dismiss:
                assert "not yet been confirmed" in notices[key]["message"]
                assert "6249" in notices[key]["message"]
            assert calls == backend.calls
        finally:
            await restored.close()


async def test_transport_failure_keeps_existing_fault_unconfirmed(hass, notices):
    backend = FaultBackend()
    async with running(hass, backend) as (entry, _, owner, _):
        await ready(owner)
        key = notification_id(entry.entry_id, HEAT)
        await eventually(lambda: key in notices)
        coordinator = source_for(owner, HEAT)
        backend.status[HEAT] = 503
        coordinator._next_update = dict.fromkeys(coordinator._poll_intervals, 0)
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert "not yet been confirmed" in notices[key]["message"]
        assert "6249" in notices[key]["message"]


async def test_firmware_issue_tracks_its_own_source_and_is_removed_with_access(hass):
    async with running(hass, Backend(), second_cloud=True) as (entry, cloud, owner, _):
        await ready(owner)
        coordinator = source_for(owner, HEAT)
        path = "/heatingCircuits/hc2/operationMode"
        good = scalar(path, "manual", options=("manual", "auto"))
        bad = replace(good, metadata=replace(good.metadata, resource_type="floatValue"))
        coordinator.resources[path] = bad
        coordinator.async_update_listeners()
        identity = repair_id(
            FIRMWARE_ISSUE_PREFIX, entry, cloud, binding_for(owner, HEAT)
        )
        registry = ir.async_get(hass)
        assert registry.async_get_issue(DOMAIN, identity)
        peer = source_for(owner, "gateway-b")
        peer.async_update_listeners()
        assert registry.async_get_issue(DOMAIN, identity)
        coordinator.resources[path] = good
        coordinator.async_update_listeners()
        assert registry.async_get_issue(DOMAIN, identity) is None
        coordinator.resources[path] = bad
        coordinator.async_update_listeners()
        manager = owner.connections.management
        manager.remove(manager.edit(cloud))
        assert registry.async_get_issue(DOMAIN, identity) is None


async def test_wddw2_mode_form_rejects_identical_values_from_replacement_source(hass):
    async with running(hass, Backend()) as (entry, cloud, owner, _):
        await ready(owner)
        path = "/system/holidayMode"
        resource = scalar(path, "off", options=("off", "on"))
        coordinator = source_for(owner, HEAT)
        coordinator.async_set_updated_data({**coordinator.data, **snapshots(resource)})
        flow = flow_for(hass, entry)
        flow._selected_key = next(iter(flow._holiday_mode_choices()))
        await flow.async_step_holiday_mode()
        coordinator = await reconnect(owner, cloud)
        coordinator.async_set_updated_data({**coordinator.data, **snapshots(resource)})
        with patch.object(coordinator, "async_write_control", AsyncMock()) as write:
            result = await flow.async_step_holiday_mode({"holiday_mode": "on"})
        assert result["type"] is FlowResultType.ABORT
        write.assert_not_called()


async def test_open_firmware_confirmation_cannot_restart_new_source(hass):
    async with running(hass, Backend()) as (entry, cloud, owner, _):
        await ready(owner)
        flow, _ = await repair(hass, entry, cloud, binding_for(owner, HEAT))
        current = await reconnect(owner, cloud)
        result = await flow.async_step_confirm({})
        assert result["type"] is FlowResultType.ABORT
        assert source_for(owner, HEAT) is current


async def test_notification_preferences_can_hide_and_reshow_retained_offline_fault(
    hass, notices
):
    async with running(hass, FaultBackend()) as (entry, cloud, owner, _):
        await ready(owner)
        key = notification_id(entry.entry_id, HEAT)
        await eventually(lambda: key in notices)
        manager = owner.connections.management
        manager.set_enabled(manager.edit(cloud), False)
        for enabled in (False, True):
            flow = flow_for(hass, entry)
            await flow.async_step_notifications()
            result = await flow.async_step_notifications(
                {
                    CONF_FAULT_NOTIFICATIONS: [gateway_notification_key(HEAT)]
                    if enabled
                    else [],
                }
            )
            hass.config_entries.async_update_entry(entry, options=result["data"])
            await hass.async_block_till_done()
            assert (key in notices) == enabled
        assert "not yet been confirmed" in notices[key]["message"]


async def test_managed_additional_cloud_diagnostics_are_separate_and_redacted(hass):
    broker = Broker()
    async with connected(hass, broker) as (entry, _, owner, _):
        await eventually(lambda: bacon_ready(owner, "serial-A"))
        before = (len(broker.claims), len(broker.sent), len(broker.writes))
        data = await async_get_config_entry_diagnostics(hass, entry)
        assert before == (len(broker.claims), len(broker.sent), len(broker.writes))
        accounts = [item["bacon"] for item in data["connections"] if "bacon" in item]
        assert len(accounts) == 1
        assert accounts[0]["connected"] is True
        assert accounts[0]["states_received"] >= 1
        serialized = json.dumps(data)
        assert "serial-A" not in serialized and "account-A" not in serialized


async def test_managed_wallbox_diagnostics_reference_correct_source(hass):
    backend = WallboxBackend()
    async with running(hass, backend) as (entry, _, owner, _):
        await ready(owner)
        calls = list(backend.calls)
        data = await async_get_config_entry_diagnostics(hass, entry)
        assert backend.calls == calls
        assert len(data["wallboxes"]) == 1
        report = data["wallboxes"][0]
        assert any(
            item["binding"] == report["binding"] and item["target"] == report["target"]
            for item in data["sources"]
        )
        assert "gateway-b" not in json.dumps(data)


async def test_notification_of_a_gateway_no_longer_configured_ends_at_once(
    hass, notices
):
    async with running(hass, FaultBackend()) as (entry, _, owner, _):
        await ready(owner)
        key = notification_id(entry.entry_id, HEAT)
        await eventually(lambda: key in notices)
        notification = owner.services.notifications[HEAT]
        manager = owner.connections.management
        # The system continues on another gateway (P50): the old one is gone.
        manager.exchange_gateway(binding_for(owner, HEAT), HEAT, "000999")
        assert HEAT not in notification_gateways(entry)
        await eventually(lambda: HEAT not in owner.services.notifications)
        await hass.async_block_till_done()
        assert key not in notices and notification._closed
        await eventually(lambda: not owner.services._closing)
