"""P50: an exchanged gateway continues the same system after confirmation."""

from copy import deepcopy
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from aiohttp import web
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating import gateway_exchange
from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.connection_inventory import (
    ConnectionInventory,
    RetainedEntity,
)
from custom_components.bosch_buderus_heating.connection_runtime import ConnectionPhase
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
from custom_components.bosch_buderus_heating.counter_selection import (
    CounterGuard,
    ShownCounter,
    counter_source_store,
)
from custom_components.bosch_buderus_heating.faults import (
    FAULT_STORAGE_VERSION,
    fault_store_key,
)
from custom_components.bosch_buderus_heating.gateway_exchange import (
    ModuleFingerprints,
    async_check_exchange,
    exchange_issue_id,
    exchange_journal,
    module_fingerprints,
    plan_exchange,
    sync_exchange_issue,
)
from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry
from custom_components.bosch_buderus_heating.notifications import (
    gateway_notification_key,
    notification_store,
)
from custom_components.bosch_buderus_heating.pointt import AuthTokens, Gateway, Resource
from custom_components.bosch_buderus_heating.repairs import (
    GatewayExchangeRepairFlow,
    async_create_fix_flow,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_configuration import legacy
from tests.test_connection_runtime import MODULE, entry_for
from tests.test_managed_connections import choose_connection
from tests.test_managed_platforms import available, registered
from tests.test_managed_pointt import (
    Backend,
    binding_for,
    eventually,
    running,
    source_for,
)

REPAIRS = "custom_components.bosch_buderus_heating.repairs"


def _pointt_bindings(entry, cloud):
    return {
        key: value["locator"]["value"]
        for key, value in entry.data["bindings"].items()
        if value["connection_id"] == cloud and value["adapter"] == "pointt"
    }


# ------------------------------------------------------------- R1: forgetting


async def test_a_switched_off_or_forgotten_gateway_never_blocks_a_sign_in(
    hass,
) -> None:
    entry, cloud, _ = entry_for(hass)
    management = ConfigurationManagement(hass, entry)
    bindings = _pointt_bindings(entry, cloud)
    old = next(key for key, value in bindings.items() if value == "gateway-b")
    kept = set(bindings) - {old}
    other = {
        key
        for key, value in entry.data["bindings"].items()
        if value["connection_id"] == cloud and value["adapter"] != "pointt"
    }
    management.set_targets_enabled(management.edit(cloud), kept | other)
    visible = AsyncMock(return_value=(Gateway("000123"),))  # gateway-b is gone
    tokens = AuthTokens("new", "new", 10**12, scope=("bacon",))
    with (
        patch(MODULE + ".PointTClient.get_gateways", visible),
        patch(
            MODULE + ".discover",
            AsyncMock(return_value=(("softener",), _account(entry, cloud))),
        ),
    ):
        await management.async_reauthenticate(management.edit(cloud), tokens)
        management.forget_bindings(management.edit(cloud), {old})
        assert old not in entry.data["bindings"]
        await management.async_reauthenticate(management.edit(cloud), tokens)


async def test_a_switched_off_cloud_device_can_be_forgotten(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    management = ConfigurationManagement(hass, entry)
    device = next(
        key
        for key, value in entry.data["bindings"].items()
        if value["connection_id"] == cloud and value["adapter"] == "bacon"
    )
    target = entry.data["bindings"][device]["target_id"]
    with pytest.raises(ConfigurationError, match="binding_changed"):
        management.forget_bindings(management.edit(cloud), {device})  # still on
    management.set_binding_enabled(management.edit(cloud), device, False)
    management.forget_bindings(management.edit(cloud), {device})
    assert device not in entry.data["bindings"]
    assert target in entry.data["targets"]  # devices and entities stay


def _account(entry, cloud):
    from custom_components.bosch_buderus_heating.configuration_access import (
        EntryConfiguration,
    )

    return EntryConfiguration(entry, cloud).cloud_data()["bacon_account"]


async def test_only_deselected_systems_can_be_forgotten(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    management = ConfigurationManagement(hass, entry)
    before = deepcopy(dict(entry.data))
    enabled = next(iter(_pointt_bindings(entry, cloud)))
    with pytest.raises(ConfigurationError, match="binding_changed"):
        management.forget_bindings(management.edit(cloud), {enabled})
    with pytest.raises(ConfigurationError, match="binding_changed"):
        management.forget_bindings(management.edit(cloud), {"local"})
    with pytest.raises(ConfigurationError, match="binding_changed"):
        management.forget_bindings(management.edit(cloud), set())
    assert entry.data == before


async def test_forgetting_keeps_the_target_and_cleans_the_read_order(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    management = ConfigurationManagement(hass, entry)
    key = next(iter(_pointt_bindings(entry, cloud)))
    target = entry.data["bindings"][key]["target_id"]
    management.set_targets_enabled(
        management.edit(cloud),
        {
            other
            for other, value in entry.data["bindings"].items()
            if value["connection_id"] == cloud and other != key
        },
    )
    management.forget_bindings(management.edit(cloud), {key})
    assert target in entry.data["targets"]
    assert all(
        key not in item["read_order"] for item in entry.options["targets"].values()
    )


async def test_the_dialog_offers_only_deselected_systems(
    hass, enable_custom_integrations
) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud)
    assert "connection_forget" not in result["menu_options"]
    management = ConfigurationManagement(hass, entry)
    bindings = _pointt_bindings(entry, cloud)
    old = next(iter(bindings))
    management.set_targets_enabled(
        management.edit(cloud),
        {
            key
            for key, value in entry.data["bindings"].items()
            if value["connection_id"] == cloud and key != old
        },
    )
    result = await choose_connection(hass, entry, cloud)
    assert "connection_forget" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_forget"}
    )
    assert result["type"] is FlowResultType.FORM
    options = result["data_schema"].schema
    choices = next(iter(options.values())).config["options"]
    assert [item["value"] for item in choices] == [old]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"bindings": [old]}
    )
    assert result["type"] is FlowResultType.MENU
    assert old not in entry.data["bindings"]


async def test_the_local_access_offers_no_forgetting(
    hass, enable_custom_integrations
) -> None:
    entry, _, _ = entry_for(hass)
    result = await choose_connection(hass, entry, "local")
    assert "connection_forget" not in result["menu_options"]


# ------------------------------------------------- 2B: module fingerprint


def _system_info():
    import json
    from pathlib import Path

    capture = json.loads(
        Path("tests/fixtures/contracts/captures/k40-cloud-2026-11-05.json").read_text(
            encoding="utf-8"
        )
    )
    values = capture["resources"]["/system/info"]["values"]
    # Distinct serials per row; the capture redacts them.
    return [
        dict(item, ModuleSerialNumber=f"serial-{index}")
        for index, item in enumerate(values)
    ]


def test_the_fingerprint_leaves_out_the_gateway_rows() -> None:
    from custom_components.bosch_buderus_heating.gateway_exchange import (
        module_fingerprint,
    )

    values = _system_info()
    full = module_fingerprint(values)
    gateway_rows = [item for item in values if item.get("Tok") in ("72", "75")]
    assert gateway_rows  # the capture has them
    devices = [item for item in values if item not in gateway_rows]
    assert full == module_fingerprint(devices)
    assert len(full) == len(devices)
    # Another gateway with new serials does not change it.
    exchanged = devices + [
        dict(item, ModuleSerialNumber="new-gateway") for item in gateway_rows
    ]
    assert module_fingerprint(exchanged) == full
    # No serial number appears in the fingerprint.
    assert not any("serial" in value for value in full)


def test_shared_device_modules_are_evidence() -> None:
    from custom_components.bosch_buderus_heating.gateway_exchange import (
        ModuleEvidence,
        compare_modules,
        module_fingerprint,
    )

    full = module_fingerprint(_system_info())
    other = module_fingerprint(
        [
            dict(item, ModuleSerialNumber="x" + str(i))
            for i, item in enumerate(_system_info())
        ]
    )
    assert compare_modules(full, full) is ModuleEvidence.MATCH
    assert compare_modules(full, frozenset(list(full)[:1])) is ModuleEvidence.MATCH
    assert compare_modules(full, other) is ModuleEvidence.NO_MATCH
    assert compare_modules(None, full) is ModuleEvidence.UNKNOWN
    assert compare_modules(full, frozenset()) is ModuleEvidence.UNKNOWN


async def test_fingerprints_are_stored_per_target_and_survive_a_reload(hass) -> None:
    from custom_components.bosch_buderus_heating.gateway_exchange import (
        ModuleFingerprints,
        module_fingerprint,
    )

    store = ModuleFingerprints(hass, "entry")
    assert not store.observe("t1", _system_info())  # not loaded yet: nothing
    await store.async_load()
    assert store.observe("t1", _system_info())
    assert not store.observe("t1", _system_info())  # unchanged
    assert not store.observe("t1", [])  # an empty answer never erases it
    await hass.async_block_till_done()
    await store._store.async_save(store._data())
    again = ModuleFingerprints(hass, "entry")
    await again.async_load()
    assert again.get("t1") == module_fingerprint(_system_info())
    assert again.get("t2") is None
    again.forget("t1")
    assert again.get("t1") is None


# ------------------------------------------------- 2B/3C: detection and repair

# The local access of the test entry belongs to the "gateway-b" system.
OLD, KEPT = "gateway-b", "000123"
NEW, OTHER = "gw-new-4711", "gw-other-0815"
SETPOINT = ":heatingCircuits:hc2:manualRoomSetpoint:control"
uses_network = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")


class ExchangeBackend(Backend):
    """The old gateway is gone from the list; the new one answers like it."""

    def __init__(self, gateways=(KEPT, NEW)):
        super().__init__()
        self.gateways = list(gateways)
        self.values.update({NEW: 22.0, OTHER: 18.0})
        self.modules = {}
        self.metadata_status = None

    def resource(self, gateway, path):
        if path == "/system/info":
            if gateway not in self.modules:
                return None
            return {
                "id": "/system/info",
                "type": "systeminfo",
                "values": self.modules[gateway],
            }
        return super().resource(gateway, path)

    async def metadata(self, request):
        gateway = request.match_info["gateway"]
        self.calls.append(("metadata", gateway))
        if self.metadata_status is not None:
            return web.Response(status=self.metadata_status)
        return web.json_response({"id": gateway, "deviceType": "MX400"})

    @property
    def routes(self):
        return [("GET", "/gateways/{gateway}", self.metadata), *super().routes]


def _issue(hass, entry, cloud, binding):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, exchange_issue_id(entry, cloud, binding)
    )


def _cloud_binding(entry, gateway):
    return next(
        key
        for key, value in entry.data["bindings"].items()
        if value["adapter"] == "pointt" and value["locator"]["value"] == gateway
    )


async def _detected(hass, entry, cloud, owner):
    binding = binding_for(owner, OLD)
    await eventually(lambda: _issue(hass, entry, cloud, binding) is not None)
    return binding


@uses_network
async def test_one_free_gateway_raises_the_repair_without_extra_requests(
    hass,
) -> None:
    backend = ExchangeBackend()
    async with running(hass, backend) as (entry, cloud, owner, harness):
        binding = await _detected(hass, entry, cloud, owner)
        issue = _issue(hass, entry, cloud, binding)
        assert issue.is_fixable and issue.severity is ir.IssueSeverity.WARNING
        assert issue.translation_key == "gateway_exchange"
        assert issue.translation_placeholders == {"old": "ay-b", "new": "4711"}
        assert issue.data == {
            "entry_id": entry.entry_id,
            "connection_id": cloud,
            "binding_id": binding,
            "new_gateway": NEW,
        }
        assert backend.calls.count(("gateways",)) == 1
        assert not any(NEW in call for call in backend.calls)
        # The old gateway is back: the question is gone.
        backend.gateways.insert(0, OLD)
        owner.connections.connection_driver(cloud)._gateways_expiry = 0
        harness.retry.set()
        await eventually(lambda: source_for(owner, OLD) is not None)
        assert _issue(hass, entry, cloud, binding) is None


@pytest.mark.parametrize(
    "case", ["one", "none", "two", "wallbox", "owned", "declined", "two_missing"]
)
async def test_only_one_unambiguous_free_gateway_raises_the_repair(hass, case) -> None:
    entry, cloud, _ = entry_for(hass)
    binding = _cloud_binding(entry, OLD)
    gateways = {
        "one": [Gateway(KEPT), Gateway(NEW)],
        "none": [Gateway(KEPT)],
        "two": [Gateway(KEPT), Gateway(NEW), Gateway(OTHER)],
        "wallbox": [Gateway(KEPT), Gateway(NEW, device_type="Commodule2")],
        "owned": [Gateway(KEPT), Gateway(NEW)],
        "declined": [Gateway(KEPT), Gateway(NEW)],
        "two_missing": [Gateway(NEW)],
    }[case]
    if case == "owned":
        MockConfigEntry(
            domain=DOMAIN, entry_id="other", version=3, data=legacy([NEW])
        ).add_to_hass(hass)
    fingerprints = ModuleFingerprints(hass, entry.entry_id)
    await fingerprints.async_load()
    if case == "declined":
        await fingerprints.async_decline(binding, NEW)
    snapshot = ConfigurationManagement(hass, entry).current()
    result = sync_exchange_issue(
        hass, entry, snapshot, cloud, binding, gateways, fingerprints
    )
    assert result == (NEW if case == "one" else None)
    assert (_issue(hass, entry, cloud, binding) is not None) is (case == "one")
    # A later answer without a candidate takes the question back.
    sync_exchange_issue(
        hass, entry, snapshot, cloud, binding, [Gateway(KEPT)], fingerprints
    )
    assert _issue(hass, entry, cloud, binding) is None


async def test_an_exchanged_wallbox_is_never_offered(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    binding = _cloud_binding(entry, OLD)
    fingerprints = ModuleFingerprints(hass, entry.entry_id)
    await fingerprints.async_load()
    snapshot = ConfigurationManagement(hass, entry).current()
    assert (
        sync_exchange_issue(
            hass,
            entry,
            snapshot,
            cloud,
            binding,
            [Gateway(KEPT), Gateway(NEW)],
            fingerprints,
            old_gateway=Gateway(OLD, device_type="Commodule"),
        )
        is None
    )


@uses_network
async def test_the_repair_goes_with_a_switched_off_system_and_entry(hass) -> None:
    async with running(hass, ExchangeBackend()) as (entry, cloud, owner, _):
        binding = await _detected(hass, entry, cloud, owner)
        management = ConfigurationManagement(hass, entry)
        management.set_binding_enabled(management.edit(cloud), binding, False)
        await eventually(lambda: _issue(hass, entry, cloud, binding) is None)
        management.set_binding_enabled(management.edit(cloud), binding, True)
    fingerprints = ModuleFingerprints(hass, entry.entry_id)
    await fingerprints.async_load()
    sync_exchange_issue(
        hass,
        entry,
        ConfigurationManagement(hass, entry).current(),
        cloud,
        binding,
        [Gateway(KEPT), Gateway(NEW)],
        fingerprints,
    )
    assert _issue(hass, entry, cloud, binding) is not None
    from custom_components.bosch_buderus_heating import async_remove_entry

    await async_remove_entry(hass, entry)
    assert _issue(hass, entry, cloud, binding) is None


def _rows(prefix):
    return [
        dict(item, ModuleSerialNumber=prefix + str(i))
        for i, item in enumerate(_system_info())
    ]


async def _flow(hass, entry, cloud, binding):
    issue = _issue(hass, entry, cloud, binding)
    flow = await async_create_fix_flow(hass, issue.issue_id, issue.data)
    assert isinstance(flow, GatewayExchangeRepairFlow)
    flow.hass = hass
    return flow


def _target(entry, binding):
    return entry.data["bindings"][binding]["target_id"]


@uses_network
@pytest.mark.parametrize("evidence", ["match", "no_match", "unknown"])
async def test_the_repair_follows_the_module_evidence(hass, evidence) -> None:
    backend = ExchangeBackend()
    if evidence != "unknown":
        backend.modules[NEW] = _rows("serial-")
    async with running(hass, backend) as (entry, cloud, owner, _):
        binding = await _detected(hass, entry, cloud, owner)
        fingerprints = module_fingerprints(hass, entry.entry_id)
        await fingerprints.async_load()
        fingerprints.observe(
            _target(entry, binding),
            _rows("serial-" if evidence == "match" else "elsewhere-"),
        )
        before = len(backend.calls)
        flow = await _flow(hass, entry, cloud, binding)
        result = await flow.async_step_init()
        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == f"evidence_{evidence}"
        assert result["menu_options"] == ["continue", "separate", "later"]
        assert result["description_placeholders"] == {"old": "ay-b", "new": "4711"}
        # One list (shared, cached) and one /system/info of the new gateway.
        requested = backend.calls[before:]
        assert ("GET", NEW, "/system/info") in requested
        assert [call for call in requested if NEW in call] == [
            ("metadata", NEW),
            ("GET", NEW, "/system/info"),
        ]
        assert not any(call[0] == "PUT" for call in backend.calls)
        with patch(REPAIRS + ".async_exchange_gateway") as exchange:
            result = await flow.async_step_continue()
            assert result["type"] is FlowResultType.FORM
            fields = {str(key) for key in result["data_schema"].schema}
            if evidence == "match":
                assert result["step_id"] == "continue_match"
                assert fields == {"confirm"}
                result = await flow.async_step_continue_match({"confirm": False})
                assert result["errors"] == {"base": "confirmation_required"}
                result = await flow.async_step_continue_match({"confirm": True})
            else:
                assert result["step_id"] == "continue_unverified"
                assert fields == {"confirm", "confirm_unverified"}
                # The simple confirmation is not enough without evidence.
                assert (await flow.async_step_continue_match({"confirm": True}))[
                    "step_id"
                ] == "continue_unverified"
                result = await flow.async_step_continue_unverified({"confirm": True})
                assert result["errors"] == {"base": "confirmation_required"}
                exchange.assert_not_called()
                result = await flow.async_step_continue_unverified(
                    {"confirm": True, "confirm_unverified": True}
                )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            exchange.assert_awaited_once()
            assert exchange.await_args.args[2].evidence.value == evidence


@uses_network
async def test_later_keeps_and_separate_declines_the_question(hass) -> None:
    async with running(hass, ExchangeBackend()) as (entry, cloud, owner, _):
        binding = await _detected(hass, entry, cloud, owner)
        flow = await _flow(hass, entry, cloud, binding)
        await flow.async_step_init()
        result = await flow.async_step_later()
        assert result == {**result, "type": FlowResultType.ABORT, "reason": "later"}
        assert _issue(hass, entry, cloud, binding) is not None
        flow = await _flow(hass, entry, cloud, binding)
        await flow.async_step_init()
        result = await flow.async_step_separate()
        assert result["type"] is FlowResultType.FORM
        result = await flow.async_step_separate({})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert _issue(hass, entry, cloud, binding) is None
        stored = ModuleFingerprints(hass, entry.entry_id)
        await stored.async_load()
        assert stored.declined(binding, NEW)
        assert entry.data["bindings"][binding]["locator"]["value"] == OLD


@uses_network
async def test_the_repair_refuses_a_changed_situation(hass) -> None:
    backend = ExchangeBackend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        binding = await _detected(hass, entry, cloud, owner)
        flow = await _flow(hass, entry, cloud, binding)
        backend.gateways.append(OTHER)
        owner.connections.connection_driver(cloud)._gateways_expiry = 0
        result = await flow.async_step_init()
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "exchange_changed"
        assert not any(NEW in call for call in backend.calls if call[0] != "bulk")


# ---------------------------------------------------------- 1A: migration


def _registry_setup(hass, entry):
    """Old device and entities, plus identities that must never change."""
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, OLD)}
    )
    number = entities.async_get_or_create(
        "number", DOMAIN, OLD + SETPOINT, config_entry=entry, device_id=device.id
    )
    entities.async_update_entity(number.entity_id, name="My room temperature")
    faults = entities.async_get_or_create(
        "sensor", DOMAIN, OLD + ":active_faults", config_entry=entry
    )
    kept = [
        entities.async_get_or_create("sensor", DOMAIN, unique_id, config_entry=entry)
        for unique_id in (
            f"{entry.entry_id}:request_metrics:requests",
            "bacon:" + "a" * 64 + ":filter",
            KEPT + SETPOINT,
        )
    ]
    return device, number, faults, kept


def _notifications(data, options):
    options["fault_notifications"] = {
        gateway_notification_key(OLD): {"enabled": False, "reset": 2}
    }


def _assert_exchanged(hass, entry, cloud, binding, device, number, faults, kept):
    entities, devices = er.async_get(hass), dr.async_get(hass)
    moved = entities.async_get(number.entity_id)
    assert moved.unique_id == NEW + SETPOINT
    assert moved.device_id == device.id and moved.name == "My room temperature"
    assert entities.async_get(faults.entity_id).unique_id == NEW + ":active_faults"
    for item in kept:
        assert entities.async_get(item.entity_id).unique_id == item.unique_id
    assert devices.async_get(device.id).identifiers == {(DOMAIN, NEW)}
    data = entry.data
    assert data["bindings"][binding]["locator"]["value"] == NEW
    target = data["targets"][_target(entry, binding)]
    assert [item["value"] for item in target["identities"]] == [NEW]
    assert target["legacy_identity"]["gateway_id"] == NEW
    # The local access belonged to the old gateway.
    assert "local" not in data["connections"] and "local" not in data["bindings"]
    assert entry.options["fault_notifications"] == {
        gateway_notification_key(NEW): {"enabled": False, "reset": 2}
    }
    assert _issue(hass, entry, cloud, binding) is None


async def _later_start(hass, entry):
    """Home Assistant starts again: a new process loads what is on disk."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch.object(gateway_exchange, "_RUN_ID", uuid4().hex):
        assert await hass.config_entries.async_setup(entry.entry_id)


async def _journal_kept_until_a_later_start(hass, entry):
    journal = exchange_journal(hass, entry.entry_id)
    state = await journal.async_load()
    assert state is not None and state.finished
    assert state.applied_run == gateway_exchange._RUN_ID
    # Never in the same process: the registry may not be on disk yet.
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert await journal.async_load() is not None
    await _later_start(hass, entry)
    assert await journal.async_load() is None


async def _real_cycle(hass, entry, backend, number):
    owner = entry.runtime_data
    await eventually(lambda: available(owner, "number", NEW + SETPOINT), timeout=10)
    assert registered(hass, "number", NEW + SETPOINT).entity_id == number.entity_id
    assert hass.states.get(number.entity_id).state == "22.0"
    assert any(call[0] == "bulk" and call[1] == NEW for call in backend.calls)
    assert not any(call[0] == "PUT" for call in backend.calls)


@uses_network
async def test_continuing_keeps_history_and_moves_the_identities(hass) -> None:
    backend = ExchangeBackend()
    async with running(hass, backend, configure=_notifications) as (
        entry,
        cloud,
        owner,
        _,
    ):
        registry = _registry_setup(hass, entry)
        binding = await _detected(hass, entry, cloud, owner)
        flow = await _flow(hass, entry, cloud, binding)
        assert (await flow.async_step_init())["step_id"] == "evidence_unknown"
        await flow.async_step_continue()
        result = await flow.async_step_continue_unverified(
            {"confirm": True, "confirm_unverified": True}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.state is ConfigEntryState.LOADED
        assert isinstance(entry.runtime_data, ManagedEntry)
        assert entry.runtime_data is not owner and owner._closed
        _assert_exchanged(hass, entry, cloud, binding, *registry)
        stored = module_fingerprints(hass, entry.entry_id)
        assert stored.exchange(_target(entry, binding)) is not None
        await _real_cycle(hass, entry, backend, registry[1])
        await _journal_kept_until_a_later_start(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)


@uses_network
@pytest.mark.parametrize("taken", ["entity", "device"])
async def test_a_taken_identity_aborts_without_any_change(hass, taken) -> None:
    async with running(hass, ExchangeBackend()) as (entry, cloud, owner, _):
        _registry_setup(hass, entry)
        if taken == "entity":
            er.async_get(hass).async_get_or_create(
                "number", DOMAIN, NEW + SETPOINT, config_entry=entry
            )
        else:
            dr.async_get(hass).async_get_or_create(
                config_entry_id=entry.entry_id, identifiers={(DOMAIN, NEW)}
            )
        binding = await _detected(hass, entry, cloud, owner)
        before = deepcopy((dict(entry.data), dict(entry.options)))
        unique_ids = {
            item.entity_id: item.unique_id
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        }
        flow = await _flow(hass, entry, cloud, binding)
        await flow.async_step_init()
        await flow.async_step_continue()
        result = await flow.async_step_continue_unverified(
            {"confirm": True, "confirm_unverified": True}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "exchange_collision"
        assert (dict(entry.data), dict(entry.options)) == before
        assert unique_ids == {
            item.entity_id: item.unique_id
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        }
        assert entry.runtime_data is owner and entry.state is ConfigEntryState.LOADED
        assert await exchange_journal(hass, entry.entry_id).async_load() is None


@uses_network
async def test_a_failure_inside_the_block_reverts_the_registry(hass) -> None:
    async with running(hass, ExchangeBackend()) as (entry, cloud, owner, _):
        device, number, faults, _ = _registry_setup(hass, entry)
        binding = await _detected(hass, entry, cloud, owner)
        flow = await _flow(hass, entry, cloud, binding)
        await flow.async_step_init()
        await flow.async_step_continue()
        with patch.object(
            ConfigurationManagement,
            "exchange_gateway",
            side_effect=ConfigurationError("storage_limit"),
        ):
            result = await flow.async_step_continue_unverified(
                {"confirm": True, "confirm_unverified": True}
            )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "exchange_failed"
        entities = er.async_get(hass)
        assert entities.async_get(number.entity_id).unique_id == OLD + SETPOINT
        assert entities.async_get(faults.entity_id).unique_id == OLD + ":active_faults"
        assert dr.async_get(hass).async_get(device.id).identifiers == {(DOMAIN, OLD)}
        assert entry.data["bindings"][binding]["locator"]["value"] == OLD
        # Nothing changed, so the local access is still there.
        assert "local" in entry.data["connections"]
        assert await exchange_journal(hass, entry.entry_id).async_load() is None
        # The entry runs again with the old gateway; the question stays.
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data is not owner
        await eventually(lambda: _issue(hass, entry, cloud, binding) is not None)
        assert await hass.config_entries.async_unload(entry.entry_id)


@uses_network
async def test_an_entry_that_cannot_stop_changes_nothing(hass) -> None:
    async with running(hass, ExchangeBackend()) as (entry, cloud, owner, _):
        _, number, _, _ = _registry_setup(hass, entry)
        binding = await _detected(hass, entry, cloud, owner)
        flow = await _flow(hass, entry, cloud, binding)
        await flow.async_step_init()
        await flow.async_step_continue()
        before = deepcopy(dict(entry.data))
        with patch.object(ManagedEntry, "unload", AsyncMock(return_value=False)):
            result = await flow.async_step_continue_unverified(
                {"confirm": True, "confirm_unverified": True}
            )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "unload_failed"
        assert dict(entry.data) == before
        assert er.async_get(hass).async_get(number.entity_id).unique_id == (
            OLD + SETPOINT
        )
        assert await exchange_journal(hass, entry.entry_id).async_load() is None


@uses_network
async def test_the_confirmation_checks_the_gateway_list_again(hass) -> None:
    backend = ExchangeBackend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        binding = await _detected(hass, entry, cloud, owner)
        flow = await _flow(hass, entry, cloud, binding)
        await flow.async_step_init()
        await flow.async_step_continue()
        before = deepcopy(dict(entry.data))
        backend.gateways.append(OLD)  # the old gateway is back meanwhile
        owner.connections.connection_driver(cloud)._gateways_expiry = 0
        result = await flow.async_step_continue_unverified(
            {"confirm": True, "confirm_unverified": True}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "exchange_changed"
        assert dict(entry.data) == before
        assert entry.runtime_data is owner
        assert await exchange_journal(hass, entry.entry_id).async_load() is None


@uses_network
async def test_a_cloud_that_cannot_answer_now_never_leads_to_the_warning(
    hass,
) -> None:
    backend = ExchangeBackend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        binding = await _detected(hass, entry, cloud, owner)
        backend.metadata_status = 503
        flow = await _flow(hass, entry, cloud, binding)
        result = await flow.async_step_init()
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "cloud_unavailable"


@uses_network
async def test_unreadable_other_entries_raise_no_question(hass) -> None:
    with patch(
        "custom_components.bosch_buderus_heating.cloud_configuration"
        ".foreign_identities",
        side_effect=ConfigurationError("invalid_json"),
    ):
        async with running(hass, ExchangeBackend()) as (entry, cloud, owner, _):
            binding = binding_for(owner, OLD)
            await eventually(
                lambda: (
                    owner.connections.source_phase(binding)
                    is ConnectionPhase.UNAVAILABLE
                )
            )
            assert _issue(hass, entry, cloud, binding) is None
            await eventually(lambda: source_for(owner, KEPT) is not None)


@uses_network
@pytest.mark.parametrize(
    "stage", ["before_block", "after_block", "config_only", "applied_earlier"]
)
async def test_an_interrupted_exchange_is_finished_at_the_next_start(
    hass, stage
) -> None:
    backend = ExchangeBackend()
    async with running(hass, backend, configure=_notifications) as (
        entry,
        cloud,
        owner,
        _,
    ):
        registry = _registry_setup(hass, entry)
        binding = await _detected(hass, entry, cloud, owner)
        check = await async_check_exchange(hass, entry, cloud, binding, NEW)
        plan = plan_exchange(hass, entry, check)
        journal = exchange_journal(hass, entry.entry_id)
        await journal.async_save(plan)
        # Home Assistant stops here (the journal is already on disk).
        assert await hass.config_entries.async_unload(entry.entry_id)
        if stage in ("after_block", "applied_earlier"):
            gateway_exchange._apply(hass, entry, plan)
        elif stage == "config_only":  # the config was saved, the registry not
            ConfigurationManagement(hass, entry).exchange_gateway(binding, OLD, NEW)
        if stage == "applied_earlier":  # a previous process; follow-up missing
            await journal.async_save(plan, applied_run="previous-process")
        assert "local" in entry.data["connections"]
        assert await hass.config_entries.async_setup(entry.entry_id)
        _assert_exchanged(hass, entry, cloud, binding, *registry)
        await _real_cycle(hass, entry, backend, registry[1])
        if stage == "applied_earlier":
            # A later process found the change on disk: the journal is done.
            assert await journal.async_load() is None
        else:
            await _journal_kept_until_a_later_start(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)


@uses_network
async def test_a_registry_lost_in_a_crash_is_moved_again_at_the_next_start(
    hass,
) -> None:
    backend = ExchangeBackend()
    async with running(hass, backend, configure=_notifications) as (
        entry,
        cloud,
        owner,
        _,
    ):
        registry = _registry_setup(hass, entry)
        device, number, faults, _ = registry
        binding = await _detected(hass, entry, cloud, owner)
        check = await async_check_exchange(hass, entry, cloud, binding, NEW)
        await gateway_exchange.async_exchange_gateway(hass, entry, check)
        _assert_exchanged(hass, entry, cloud, binding, *registry)
        # Crash before the delayed registry save: the next start loads the new
        # configuration with the old registry.
        assert await hass.config_entries.async_unload(entry.entry_id)
        entities = er.async_get(hass)
        entities.async_update_entity(number.entity_id, new_unique_id=OLD + SETPOINT)
        entities.async_update_entity(
            faults.entity_id, new_unique_id=OLD + ":active_faults"
        )
        dr.async_get(hass).async_update_device(
            device.id, new_identifiers={(DOMAIN, OLD)}
        )
        run = uuid4().hex
        with patch.object(gateway_exchange, "_RUN_ID", run):
            assert await hass.config_entries.async_setup(entry.entry_id)
        _assert_exchanged(hass, entry, cloud, binding, *registry)
        state = await exchange_journal(hass, entry.entry_id).async_load()
        assert state is not None and state.applied_run == run
        await _real_cycle(hass, entry, backend, number)
        await _later_start(hass, entry)
        assert await exchange_journal(hass, entry.entry_id).async_load() is None
        assert await hass.config_entries.async_unload(entry.entry_id)


@uses_network
async def test_an_impossible_resume_keeps_the_journal_and_starts_nothing(
    hass, caplog
) -> None:
    backend = ExchangeBackend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        _registry_setup(hass, entry)
        binding = await _detected(hass, entry, cloud, owner)
        check = await async_check_exchange(hass, entry, cloud, binding, NEW)
        plan = plan_exchange(hass, entry, check)
        await exchange_journal(hass, entry.entry_id).async_save(plan)
        assert await hass.config_entries.async_unload(entry.entry_id)
        er.async_get(hass).async_get_or_create(
            "number", DOMAIN, NEW + SETPOINT, config_entry=entry
        )
        before = deepcopy(dict(entry.data))
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert "could not be finished (exchange_collision)" in caplog.text
        assert dict(entry.data) == before
        assert registered(hass, "number", OLD + SETPOINT) is not None
        state = await exchange_journal(hass, entry.entry_id).async_load()
        assert state is not None and state.plan == plan
        # No source ran, so none could add entities next to the old ones.
        assert not any(call[0] == "bulk" and call[1] == NEW for call in backend.calls)
        assert not any(
            item.unique_id.startswith(NEW + ":") and item.unique_id != NEW + SETPOINT
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        )


async def test_the_follow_up_continues_faults_and_frees_counters(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    binding = _cloud_binding(entry, OLD)
    target = _target(entry, binding)
    other = _target(entry, _cloud_binding(entry, KEPT))
    ConfigurationManagement(hass, entry).exchange_gateway(binding, OLD, NEW)
    fault = {"active": [], "required_source_keys": ["k"], "unverified_faults": []}
    await Store(
        hass, FAULT_STORAGE_VERSION, fault_store_key(entry.entry_id, OLD), private=True
    ).async_save(fault)
    dismissed = {"dismissed": {"incident": 1}, "reset": 2}
    await notification_store(hass, entry.entry_id, OLD).async_save(dismissed)
    counters = counter_source_store(hass)
    await counters.async_initialize()
    own, kept = (f"{entry.entry_id}|{key}|/energy" for key in (target, other))
    counters.set(own, ShownCounter("local", {"value": 100.0}))
    counters.set(kept, ShownCounter("local", {"value": 5.0}))
    restarted = ResourceSnapshot(
        Resource(path="/energy", value=3.0), True, dt_util.utcnow()
    )
    # Before: the restarted counter would stay held forever.
    assert (
        CounterGuard(counters, entry.entry_id, target).decide(
            "/energy", [("cloud", restarted)]
        )
        is None
    )
    plan = gateway_exchange.ExchangePlan(
        "attempt",
        entry.entry_id,
        cloud,
        binding,
        target,
        OLD,
        NEW,
        "2026-11-06T00:00:00+00:00",
        (),
        None,
        (),
    )
    assert await gateway_exchange._async_finish(hass, entry, plan)
    assert (
        await Store(
            hass,
            FAULT_STORAGE_VERSION,
            fault_store_key(entry.entry_id, NEW),
            private=True,
        ).async_load()
        == fault
    )
    assert await notification_store(hass, entry.entry_id, NEW).async_load() == (
        dismissed
    )
    assert counters.get(own) is None and counters.get(kept) is not None
    assert (
        CounterGuard(counters, entry.entry_id, target).decide(
            "/energy", [("cloud", restarted)]
        )
        is restarted
    )
    # The local access of the old gateway is gone with its identity.
    assert "local" not in entry.data["connections"]
    assert [item["value"] for item in entry.data["targets"][target]["identities"]] == [
        NEW
    ]
    assert module_fingerprints(hass, entry.entry_id).exchange(target) == plan.at
    # Repeating the follow-up keeps everything as it is.
    assert await gateway_exchange._async_finish(hass, entry, plan)
    assert counters.get(kept) is not None


async def test_inventory_records_follow_the_new_gateway(hass) -> None:
    entry, _, _ = entry_for(hass)
    binding = _cloud_binding(entry, OLD)
    other = _cloud_binding(entry, KEPT)
    target = _target(entry, binding)
    inventory = ConnectionInventory(hass, entry)
    for key, unique_id in ((binding, OLD + SETPOINT), (other, KEPT + SETPOINT)):
        inventory._entities[("number", unique_id)] = RetainedEntity(
            "number",
            unique_id,
            entry.data["bindings"][key]["target_id"],
            key,
            "pointt",
            "manual_room_setpoint",
        )
    await inventory.save()
    assert await ConnectionInventory(hass, entry).rekey(binding, OLD + ":", NEW + ":")
    again = ConnectionInventory(hass, entry)
    await again.load()
    assert sorted(item.unique_id for item in again.entities) == [
        KEPT + SETPOINT,
        NEW + SETPOINT,
    ]
    assert target in entry.data["targets"]
    assert not await ConnectionInventory(hass, entry).rekey(
        binding, OLD + ":", NEW + ":"
    )
