"""P49b step 7: a plant set up from its local access alone (local first).

The local setup dialogs run through Home Assistant's native flow managers,
the pending enrollment storage and the real configuration checks; only the
gateway's network answers are synthetic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_completion import (
    ScopedIdentityContract,
)
from custom_components.bosch_buderus_heating.local_flow import NEW_PLANT
from custom_components.bosch_buderus_heating.local_support import (
    SUPPORT_KEY,
    LocalSetupSupport,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_cloud_setup import backend as backend
from tests.test_local_enrollment import journal as journal
from tests.test_local_flow import submit
from tests.test_local_flow import supported as supported
from tests.test_local_setup import TOKEN, registered

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


@pytest.fixture
def loaded_integration(hass):
    hass.config.components.add(DOMAIN)


GATEWAY = "000123"
EMPTY_DATA = {
    "revision": 0,
    "connections": {},
    "targets": {},
    "bindings": {},
    "legacy_extra": {},
}
EMPTY_OPTIONS = {"connections": {}, "targets": {}, "bindings": {}}


def empty_entry(hass, entry_id="local-first"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        title="Home Comfort (local)",
        version=4,
        minor_version=2,
        data=dict(EMPTY_DATA),
        options=dict(EMPTY_OPTIONS),
    )
    entry.add_to_hass(hass)
    return entry, ConfigurationManagement(hass, entry)


async def local_setup(hass, entry, *, target=None):
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result = await submit(hass, manager, result, {"next_step_id": "local_setup"})
    if target is not None:
        assert result["step_id"] == "local_target"
        result = await submit(hass, manager, result, {"target": target})
    assert result["step_id"] == "local_address"
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "token"})
    result = await submit(hass, manager, result, {"token": TOKEN})
    if result.get("step_id") == "local_certificate":
        # A saved certificate of the same access needs no new confirmation.
        result = await submit(hass, manager, result, {"confirm": True})
    return manager, result


async def test_an_entry_without_a_plant_sets_up_a_new_one(
    hass, supported, journal
) -> None:
    entry, management = empty_entry(hass)
    manager, result = await local_setup(hass, entry)
    assert result.get("step_id") == "local_result", result
    assert result["description_placeholders"]["gateway"] == GATEWAY
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_setup_complete"
    snapshot = management.current()
    assert len(snapshot.connections) == 1
    (connection,) = snapshot.connections.values()
    assert connection.kind == "k40_local"
    (target_id, target) = next(iter(snapshot.targets.items()))
    assert len(snapshot.targets) == 1 and target.kind == "local_installation"
    data = target.to_data()
    assert [(i["namespace"], i["value"]) for i in data["identities"]] == [
        ("local_system_gateway", GATEWAY)
    ]
    # Manufacturer and model only, never an identifier or serial number.
    assert set(data["local_details"]) <= {"brand", "model"}
    options = snapshot.to_options()
    assert options["targets"][target_id]["local_plant"] is True
    (binding,) = snapshot.bindings.values()
    assert binding.adapter == "k40_local" and binding.target_id == target_id
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_a_gateway_of_a_known_plant_is_never_a_second_plant(
    hass, supported, journal
) -> None:
    entry, management, _, _ = registered(hass)
    before = management.current()
    _, result = await local_setup(hass, entry, target=NEW_PLANT)
    # The probe reads the cloud plant's gateway: a new plant is refused.
    # Refused, and the attempt is discarded: it can never complete.
    assert result["step_id"] == "local_retry"
    assert result["errors"] == {"base": "local_plant_known"}
    assert management.current() == before


# ------------------------------------------------------------ setup dialog


async def test_the_first_step_offers_three_choices(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["cloud", "cloud_local", "local_only"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "cloud"


async def test_local_only_creates_an_entry_and_continues_locally(
    hass, supported, journal
) -> None:
    from homeassistant.config_entries import FlowType
    from homeassistant.helpers import issue_registry as ir

    from custom_components.bosch_buderus_heating.entry_sources import (
        notification_gateways,
    )

    with patch(
        "custom_components.bosch_buderus_heating.async_setup_entry",
        AsyncMock(return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "local_only"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.title == "Home Comfort (local)" and entry.version == 4
    assert entry.data["connections"] == {} and entry.data["targets"] == {}
    flow_type, flow_id = result["next_flow"]
    assert flow_type == FlowType.OPTIONS_FLOW
    manager = hass.config_entries.options
    progress = next(
        item for item in manager.async_progress() if item["flow_id"] == flow_id
    )
    assert progress["handler"] == entry.entry_id
    # The options flow continues at the address of a new plant.
    step = await manager.async_configure(
        flow_id, {"address": "gateway.example.invalid"}
    )
    step = await submit(hass, manager, step, {"method": "token"})
    step = await submit(hass, manager, step, {"token": TOKEN})
    step = await submit(hass, manager, step, {"confirm": True})
    step = await submit(hass, manager, step, {"confirm": True})
    assert step["reason"] == "local_setup_complete"
    snapshot = ConfigurationManagement(hass, entry).current()
    assert [t.kind for t in snapshot.targets.values()] == ["local_installation"]
    assert notification_gateways(entry) == (GATEWAY,)
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "setup_unfinished_" + entry.entry_id)
        is None
    )


async def test_cloud_and_local_continue_with_the_new_plant(
    hass, backend, supported, journal
) -> None:
    from homeassistant.config_entries import FlowType

    from custom_components.bosch_buderus_heating.pointt import Brand
    from tests.test_cloud_setup import _redirect_from_result

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud_local"}
    )
    assert result["step_id"] == "cloud"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"brand": "buderus"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_ids": [GATEWAY], "bacon_device_ids": []}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    flow_type, flow_id = result["next_flow"]
    assert flow_type == FlowType.OPTIONS_FLOW
    management = ConfigurationManagement(hass, entry)
    (target,) = management.current().targets
    manager = hass.config_entries.options
    # The identity check compares with the running cloud; here synthetic, as
    # in the other local dialog tests (the cloud is not started).
    hass.data[SUPPORT_KEY] = LocalSetupSupport(
        lambda: True,
        lambda snapshot, target, gateway: ScopedIdentityContract(
            snapshot.owner_id, target, gateway, "synthetic-device-contract"
        ),
    )
    # The local setup attaches to the system the cloud just listed.
    step = await manager.async_configure(flow_id, {"target": target})
    step = await submit(hass, manager, step, {"address": "gateway.example.invalid"})
    step = await submit(hass, manager, step, {"method": "token"})
    step = await submit(hass, manager, step, {"token": TOKEN})
    step = await submit(hass, manager, step, {"confirm": True})
    step = await submit(hass, manager, step, {"confirm": True})
    assert step["reason"] == "local_setup_complete"
    snapshot = management.current()
    assert {b.adapter for b in snapshot.bindings.values()} == {"pointt", "k40_local"}
    assert {b.target_id for b in snapshot.bindings.values()} == {target}
    # Owner decision: cloud and local access use the local values by default.
    assert snapshot.to_options()["targets"][target]["local_values"] is True


# ------------------------------------------------------- 7D: no double claim

LOCAL_FIRST_TARGET = {
    "kind": "local_installation",
    "identities": [
        {
            "namespace": "local_system_gateway",
            "value": GATEWAY,
            "evidence": "verified_local_setup",
        }
    ],
    "local_details": {"brand": "buderus", "model": "MX400"},
}


def local_first_entry(hass, entry_id="local-first"):
    data = {**EMPTY_DATA, "targets": {"target_local_a": LOCAL_FIRST_TARGET}}
    options = {
        **EMPTY_OPTIONS,
        "targets": {"target_local_a": {"read_order": [], "local_plant": True}},
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        title="Home Comfort (local)",
        version=4,
        minor_version=2,
        data=data,
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def test_the_cloud_cannot_claim_a_local_first_gateway_elsewhere(
    hass, backend
) -> None:
    from tests.test_cloud_setup import new_setup

    local_first_entry(hass)
    result = await new_setup(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_ids": [GATEWAY], "bacon_device_ids": []}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cloud_local_first_other_entry"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


def test_the_same_entry_merges_a_local_first_gateway_only_with_a_proof(
    hass,
) -> None:
    """P49b step 8: no merge on the gateway ID alone (see test_local_first_merge)."""
    from copy import deepcopy

    from custom_components.bosch_buderus_heating.cloud_configuration import (
        _merge_targets,
    )
    from custom_components.bosch_buderus_heating.configuration import (
        ConfigurationError,
    )

    entry = local_first_entry(hass)
    data, options = deepcopy(dict(entry.data)), deepcopy(dict(entry.options))
    with pytest.raises(ConfigurationError, match="cloud_local_first_unverified"):
        _merge_targets(
            hass, entry.entry_id, data, options, "cloud", {GATEWAY}, set(), "", None
        )
    # Other gateways are not affected.
    _merge_targets(
        hass, entry.entry_id, data, options, "cloud", {"other-gw"}, set(), "", None
    )


@pytest.mark.parametrize("stored", [None, False, True])
def test_a_local_first_plant_joining_the_cloud_uses_local_values_unless_chosen(
    stored,
) -> None:
    """Owner decision: joining the cloud makes a cloud and local setup."""
    from copy import deepcopy

    from custom_components.bosch_buderus_heating.cloud_configuration import (
        LocalFirstProof,
        _join_local_first,
        target_fingerprint,
    )

    data = {
        "targets": {"t": deepcopy(LOCAL_FIRST_TARGET)},
        "bindings": {
            "b": {
                "target_id": "t",
                "adapter": "k40_local",
                "link_evidence": {"basis": "verified_local_setup", "contract": None},
            }
        },
    }
    options = {"targets": {"t": {"read_order": ["b"], "local_plant": True}}}
    if stored is not None:
        options["targets"]["t"]["local_values"] = stored
    proof = LocalFirstProof(
        "owner", "t", GATEWAY, "b", target_fingerprint(data["targets"]["t"])
    )
    _join_local_first("owner", data, options, "t", GATEWAY, proof)
    assert data["targets"]["t"]["kind"] == "pointt_object"
    assert options["targets"]["t"]["local_values"] is (
        True if stored is None else stored
    )
    assert "local_plant" not in options["targets"]["t"]


# ------------------------------------------------- the plant without a cloud


def local_first(data, options):
    """A stored entry whose plant was set up from its local access alone."""
    from tests.test_local_backed import local_values

    local_values()(data, options)
    target = data["bindings"]["local"]["target_id"]
    for key in [
        key
        for key, item in data["connections"].items()
        if item["kind"] == "singlekey_cloud"
    ]:
        del data["connections"][key]
        del options["connections"][key]
        gone = [
            binding
            for binding, item in data["bindings"].items()
            if item["connection_id"] == key
        ]
        for binding in gone:
            del data["bindings"][binding]
            del options["bindings"][binding]
        for item in options["targets"].values():
            item["read_order"] = [b for b in item["read_order"] if b not in gone]
    record = data["targets"][target]
    record["kind"] = "local_installation"
    record["identities"] = [
        item
        for item in record["identities"]
        if item["namespace"] == "local_system_gateway"
    ]
    record.pop("legacy_identity", None)
    record["local_details"] = {"brand": "buderus", "model": "MX400"}
    options["targets"][target]["local_plant"] = True


@pytest.mark.usefixtures("loaded_integration")
async def test_a_local_first_plant_is_shown_under_its_gateway(hass) -> None:
    import asyncio

    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from custom_components.bosch_buderus_heating.entry_sources import (
        notification_gateways,
    )
    from tests.test_local_backed import SENSOR, ReadingBackend
    from tests.test_local_plant_mode import readings, shown
    from tests.test_managed_platforms import NUMBER, native
    from tests.test_managed_pointt import eventually, running

    backend = ReadingBackend()
    async with asyncio.timeout(90):
        async with running(hass, backend, configure=local_first) as (
            entry,
            _,
            owner,
            _,
        ):
            await owner.start_platforms()
            runtime = owner.connections
            await eventually(lambda: runtime.target_views())
            (view,) = runtime.target_views().values()
            assert view.gateway.gateway_id == GATEWAY
            await shown(hass, owner, readings())
            # The prefix is the gateway ID read locally: the cloud's one.
            assert SENSOR.startswith(GATEWAY + ":")
            entity = native(owner, "sensor", SENSOR)
            assert entity.coordinator is view
            device = dr.async_get(hass).async_get(
                er.async_get(hass).async_get(entity.entity_id).device_id
            )
            assert (device.manufacturer, device.model) == ("Buderus", "MX400")
            assert device.serial_number is None
            assert native(owner, "number", NUMBER) is None
            assert set(notification_gateways(entry)) == {GATEWAY, "gateway-b"}
            assert not backend.calls  # there is no cloud
            # Removing the local access keeps the entry and the plant.
            management = runtime.management
            target = runtime.snapshot.bindings["local"].target_id
            management.remove(management.edit("local"))
            await eventually(lambda: not runtime.target_views())
            assert target in runtime.snapshot.targets
            assert hass.config_entries.async_get_entry(entry.entry_id) is entry
            # The repair note waits for the next start without an access.
            from homeassistant.helpers import issue_registry as ir

            issue = ir.async_get(hass).async_get_issue(
                DOMAIN, "setup_unfinished_" + entry.entry_id
            )
            assert issue is None


# ------------------------------------------------------------ review fixes


@pytest.fixture
def gateway_only(hass, monkeypatch):
    """Synthetic gateway answers only: the real component installs its local
    support when the entry is set up (no injected support)."""
    from custom_components.bosch_buderus_heating import local_flow as flow
    from custom_components.bosch_buderus_heating import local_setup as setup
    from custom_components.bosch_buderus_heating.config_flow import (
        BoschBuderusConfigFlow,
    )
    from custom_components.bosch_buderus_heating.local_probe import ProbePath
    from custom_components.bosch_buderus_heating.local_transport import (
        CertificateObservation,
        LocalProbeExchange,
    )
    from tests.test_local_probe import response

    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)

    async def inspect(context, role, *, is_current, budget):
        return CertificateObservation(context, role, b"a" * 32, "192.0.2.1", "TLSv1.3")

    async def read(context, token, binding, *, is_current, budget):
        return LocalProbeExchange(
            tuple(
                response(path, context=context, gatewayId=GATEWAY) for path in ProbePath
            )
        )

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    monkeypatch.setattr(setup, "read_local_probe", read)
    assert SUPPORT_KEY not in hass.data


async def finish_new_plant(hass, flow_id):
    manager = hass.config_entries.options
    step = await manager.async_configure(
        flow_id, {"address": "gateway.example.invalid"}
    )
    step = await submit(hass, manager, step, {"method": "token"})
    step = await submit(hass, manager, step, {"token": TOKEN})
    step = await submit(hass, manager, step, {"confirm": True})
    return await submit(hass, manager, step, {"confirm": True})


async def test_local_only_works_on_a_fresh_install(hass, gateway_only, journal) -> None:
    """Review step 7: the component is set up only when the entry is added."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local_only"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    _, flow_id = result["next_flow"]
    step = await finish_new_plant(hass, flow_id)
    assert step["reason"] == "local_setup_complete"
    snapshot = ConfigurationManagement(hass, entry).current()
    assert [t.kind for t in snapshot.targets.values()] == ["local_installation"]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_kept_local_first_system_can_be_set_up_again(
    hass, supported, journal
) -> None:
    entry, management = empty_entry(hass)
    manager = hass.config_entries.options
    _, result = await local_setup(hass, entry)
    await submit(hass, manager, result, {"confirm": True})
    (target,) = management.current().targets
    first = management.current()

    async def restarted():
        # A completed attempt is closed by the next start of Home Assistant.
        for record in await journal.records():
            if not record.resolved:
                await journal.discard(
                    record.attempt_id, record.owner_id, confirmed=True
                )

    await restarted()
    # Token replacement on the same system: its own identity, no cloud.
    _, result = await local_setup(hass, entry, target=target)
    assert result.get("step_id") == "local_result", result
    step = await submit(hass, manager, result, {"confirm": True})
    assert step["reason"] == "local_setup_complete"
    assert set(management.current().targets) == {target}
    # The local access removed: the kept system is set up again.
    local = next(iter(first.connections))
    management.remove(management.edit(local))
    assert not management.current().connections
    await restarted()
    _, result = await local_setup(hass, entry, target=target)
    step = await submit(hass, manager, result, {"confirm": True})
    assert step["reason"] == "local_setup_complete"
    snapshot = management.current()
    assert set(snapshot.targets) == {target} and len(snapshot.connections) == 1


async def test_an_interrupted_new_system_attempt_is_resumed(
    hass, supported, journal
) -> None:
    entry, management = empty_entry(hass)
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result = await submit(hass, manager, result, {"next_step_id": "local_setup"})
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "token"})
    result = await submit(hass, manager, result, {"token": TOKEN})
    manager.async_abort(result["flow_id"])  # closed after the token import
    (record,) = await journal.records()
    assert not record.resolved
    # Opened again: the pending attempt is offered, not a second new system.
    result = await manager.async_init(entry.entry_id)
    result = await submit(hass, manager, result, {"next_step_id": "local_setup"})
    assert result["step_id"] == "local_pending"
    result = await submit(hass, manager, result, {"action": "continue"})
    if result.get("step_id") == "local_certificate":
        result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_result", result
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["reason"] == "local_setup_complete"
    assert [t.kind for t in management.current().targets.values()] == [
        "local_installation"
    ]


async def test_a_refused_new_system_leaves_no_pending_attempt(
    hass, supported, journal
) -> None:
    entry, _, _, _ = registered(hass)
    _, result = await local_setup(hass, entry, target=NEW_PLANT)
    assert result["errors"] == {"base": "local_plant_known"}
    assert all(record.resolved for record in await journal.records())


async def test_the_setup_note_ends_with_its_entry(hass) -> None:
    from homeassistant.helpers import issue_registry as ir

    from custom_components.bosch_buderus_heating.managed_entry import (
        sync_setup_issue,
    )

    entry, _ = empty_entry(hass)
    sync_setup_issue(hass, entry)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "setup_unfinished_" + entry.entry_id)
    await hass.config_entries.async_remove(entry.entry_id)
    assert not registry.async_get_issue(DOMAIN, "setup_unfinished_" + entry.entry_id)


# -------------------------------------------- hand-off to the local setup


async def local_only_entry(hass):
    """A local-only entry and its prepared local setup (next_flow)."""
    with patch(
        "custom_components.bosch_buderus_heating.async_setup_entry",
        AsyncMock(return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "local_only"}
        )
    return result["result"], result["next_flow"][1]


def setup_note(hass, entry):
    from homeassistant.helpers import issue_registry as ir

    return ir.async_get(hass).async_get_issue(
        DOMAIN, "setup_unfinished_" + entry.entry_id
    )


async def test_a_new_options_flow_takes_over_the_prepared_setup(
    hass, supported, journal
) -> None:
    entry, prepared = await local_only_entry(hass)
    manager = hass.config_entries.options
    # As the frontend does: a new options flow instead of the prepared one.
    result = await manager.async_init(entry.entry_id)
    assert result["step_id"] == "local_address"
    flows = manager.async_progress_by_handler(entry.entry_id)
    assert [flow["flow_id"] for flow in flows] == [result["flow_id"]]
    assert prepared not in {flow["flow_id"] for flow in flows}
    # Handed off, not abandoned: no repair note while the setup continues.
    assert setup_note(hass, entry) is None
    step = await finish_new_plant(hass, result["flow_id"])
    assert step["reason"] == "local_setup_complete"
    # Later the options show their menu again.
    menu = await manager.async_init(entry.entry_id)
    assert menu["type"] is FlowResultType.MENU
    manager.async_abort(menu["flow_id"])


async def test_an_abandoned_prepared_setup_leaves_the_menu_and_a_note(
    hass, supported, journal
) -> None:
    entry, prepared = await local_only_entry(hass)
    manager = hass.config_entries.options
    manager.async_abort(prepared)  # closed by the user
    assert setup_note(hass, entry) is not None
    menu = await manager.async_init(entry.entry_id)
    assert menu["type"] is FlowResultType.MENU
    manager.async_abort(menu["flow_id"])


async def test_nothing_is_left_after_the_entry_is_removed(
    hass, supported, journal
) -> None:
    entry, _ = await local_only_entry(hass)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.config_entries.options.async_progress_by_handler(entry.entry_id)
    assert setup_note(hass, entry) is None
    assert not [key for key in hass.data if "handoff" in str(key)]
