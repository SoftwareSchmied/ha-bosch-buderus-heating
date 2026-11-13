"""P49b step 8 (M1): a plant set up locally joins its cloud as the same plant.

The dialogs run through Home Assistant's options flow manager on a managed
entry with its real platforms and the real PointT client against a synthetic
cloud. The comparison reads (the cloud's ``/system/info`` and the system
information through the running local access) are synthetic answers.
"""

from __future__ import annotations

import asyncio
import functools
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import local_only_sensor
from custom_components.bosch_buderus_heating.cloud_configuration import (
    LocalFirstProof,
    _merge_targets,
    local_first_targets,
    target_fingerprint,
)
from custom_components.bosch_buderus_heating.configuration import (
    ConfigurationError,
    load_configuration,
)
from custom_components.bosch_buderus_heating.configuration_migration import (
    convert_legacy_configuration,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_completion import (
    LOCAL_FIRST,
    ScopedIdentityContract,
)
from custom_components.bosch_buderus_heating.local_identity import (
    CONTRACT,
    retained_contract,
    verify_local_first_merges,
)
from custom_components.bosch_buderus_heating.local_only_sensor import (
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.ha_test_helpers import MockConfigEntry
from tests.test_cloud_setup import backend as backend
from tests.test_cloud_setup import connect_dialog
from tests.test_cloud_setup import empty_entry as cloud_empty_entry
from tests.test_configuration import OWNER, add_local, legacy
from tests.test_connection_runtime import Source, settle
from tests.test_local_backed import GATEWAY, SENSOR, ReadingBackend, local_values
from tests.test_local_first import local_first, local_first_entry
from tests.test_local_identity import SYSTEM, cloud_system
from tests.test_local_plant_mode import publish_state, readings, shown
from tests.test_local_plant_removal import removed
from tests.test_managed_connections import choose_connection
from tests.test_managed_local_plant import ROOM
from tests.test_managed_platforms import native
from tests.test_managed_pointt import binding_for, eventually, running, source_for

ROOT = "custom_components.bosch_buderus_heating"
pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
LOCAL_ONLY = local_only_unique_id(GATEWAY, ROOM)


def bounded(test):
    """Fail within 90 s instead of hanging."""

    @functools.wraps(test)
    async def run(*args, **kwargs):
        async with asyncio.timeout(90):
            await test(*args, **kwargs)

    return run


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    hass.config.components.add(DOMAIN)


class OfferingBackend(ReadingBackend):
    """The cloud also offers a reading only the local access had (8C-c)."""

    def resource(self, gateway, path):
        if path == "/heatingCircuits":
            return {
                "references": [
                    {"id": "/heatingCircuits/hc2"},
                    {"id": "/heatingCircuits/hc1"},
                ]
            }
        if path == "/heatingCircuits/hc1":
            return {"references": [{"id": ROOM}]}
        if path == ROOM:
            return {
                "id": ROOM,
                "type": "floatValue",
                "value": 35.0,
                "unitOfMeasure": "C",
                "writeable": False,
            }
        return super().resource(gateway, path)


@contextmanager
def evidence(*, cloud=None, local=SYSTEM, metadata=None, side_effect=None):
    """Synthetic answers of both comparison reads."""
    answer = (metadata or {"id": GATEWAY}, cloud or cloud_system())
    cloud_read = AsyncMock(return_value=answer, side_effect=side_effect)
    local_read = AsyncMock(return_value=local)
    with (
        patch(
            ROOT + ".pointt.client.PointTClient.get_local_identity_evidence", cloud_read
        ),
        patch.object(Source, "read_system", local_read, create=True),
    ):
        yield SimpleNamespace(cloud=cloud_read, local=local_read)


def beside_cloud(data, options):
    """A local-first plant in an entry whose cloud lists other systems only."""
    local_values()(data, options)
    target = data["bindings"]["local"]["target_id"]
    gone = [
        key
        for key, item in data["bindings"].items()
        if item["target_id"] == target and item["adapter"] == "pointt"
    ]
    for key in gone:
        del data["bindings"][key]
        del options["bindings"][key]
    options["targets"][target]["read_order"] = [
        b for b in options["targets"][target]["read_order"] if b not in gone
    ]
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


def target_of(owner):
    return owner.connections.snapshot.bindings["local"].target_id


async def local_view(hass, owner):
    """The local plant view with a ready local access and its sensors."""
    await owner.start_platforms()
    await eventually(lambda: owner.connections.target_views())
    await shown(hass, owner, readings())
    publish_state(owner, readings())
    await eventually(lambda: native(owner, "sensor", LOCAL_ONLY) is not None)
    (view,) = owner.connections.target_views().values()
    return view


async def add_cloud(hass, entry, gateways):
    result = await connect_dialog(hass, entry)
    assert result["step_id"] == "cloud_select", result
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"gateway_ids": gateways, "bacon_device_ids": []}
    )


async def rediscover(hass, entry, cloud, gateways):
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    assert result["step_id"] == "connection_discover", result
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"gateway_ids": gateways, "bacon_device_ids": []}
    )


def assert_merged(snapshot, target):
    record = snapshot.targets[target].to_data()
    assert record["kind"] == "pointt_object"
    identities = {item["namespace"]: item for item in record["identities"]}
    assert identities["pointt_gateway"] == {
        "namespace": "pointt_gateway",
        "value": GATEWAY,
        "evidence": "authenticated_listing",
    }
    local = identities["local_system_gateway"]
    assert local["evidence"] == "verified_local_setup"
    assert local["comparison_contract"] == CONTRACT
    assert record["legacy_identity"] == {"format": "pointt_v3", "gateway_id": GATEWAY}
    assert "local_details" not in record
    assert "local_plant" not in snapshot.to_options()["targets"][target]
    assert snapshot.bindings["local"].to_data()["link_evidence"]["contract"] == CONTRACT
    assert retained_contract(snapshot, target, GATEWAY) is not None


async def assert_handed_over(hass, owner, view, sensors):
    """The view gives way to the cloud under the same entities (step 6)."""
    await eventually(lambda: source_for(owner, GATEWAY) is not None)
    current = source_for(owner, GATEWAY)
    await eventually(lambda: not view.local_retained)
    assert not owner.connections.target_views()
    assert not owner.connections._retained
    assert view.faults._handed_over  # the cloud continues its fault baseline
    await eventually(
        lambda: (
            (entity := native(owner, "sensor", SENSOR)) is not None
            and entity.coordinator is current
        )
    )
    registry = er.async_get(hass)
    assert native(owner, "sensor", SENSOR).entity_id == sensors[SENSOR]
    # 8C-c: the local-only reading the cloud now offers keeps its entity.
    (cloud,) = local_only_sensor._cloud_sensors(current)[ROOM]
    cloud_id = cloud.unique_id
    await eventually(lambda: native(owner, "sensor", cloud_id) is not None)
    assert native(owner, "sensor", cloud_id).entity_id == sensors[LOCAL_ONLY]
    assert registry.async_get_entity_id("sensor", DOMAIN, LOCAL_ONLY) is None
    await eventually(
        lambda: (
            (state := hass.states.get(sensors[LOCAL_ONLY])) is not None
            and state.state == "35.0"
        )
    )
    # The inventory records the cloud's entities under the new binding.
    binding = binding_for(owner, GATEWAY)
    assert any(
        item.binding_id == binding and item.unique_id == cloud_id
        for item in owner.inventory.entities
    )
    unique_ids = [
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, owner.entry.entry_id)
    ]
    assert len(unique_ids) == len(set(unique_ids))


def local_starts(harness):
    return sum(len(d.sources.get("local", ())) for d in harness.drivers["local"])


def entity_ids(owner):
    return {
        unique_id: native(owner, "sensor", unique_id).entity_id
        for unique_id in (SENSOR, LOCAL_ONLY)
    }


# ------------------------------------------------------------- the merge


@bounded
async def test_adding_the_cloud_merges_the_local_first_plant(
    hass, backend, caplog
) -> None:
    async with running(hass, OfferingBackend(), configure=local_first) as (
        entry,
        _,
        owner,
        harness,
    ):
        view = await local_view(hass, owner)
        target = target_of(owner)
        targets = set(owner.connections.snapshot.targets)
        sensors = entity_ids(owner)
        local_runs = local_starts(harness)
        with evidence() as reads:
            result = await add_cloud(hass, entry, [GATEWAY])
        assert result["type"] is FlowResultType.CREATE_ENTRY, result
        reads.cloud.assert_awaited_once_with(GATEWAY)
        reads.local.assert_awaited_once()
        snapshot = owner.connections.management.current()
        assert set(snapshot.targets) == targets  # the same plant, no second one
        assert_merged(snapshot, target)
        await assert_handed_over(hass, owner, view, sensors)
        # The local access starts again briefly for the changed target.
        await eventually(lambda: local_starts(harness) > local_runs)
        await eventually(lambda: owner.connections.source_data("local") is not None)
    assert "inventory_identity_conflict" not in caplog.text


@bounded
async def test_selecting_the_plant_on_an_existing_cloud_merges_it(
    hass, backend
) -> None:
    async with running(hass, OfferingBackend(), configure=beside_cloud) as (
        entry,
        cloud,
        owner,
        _,
    ):
        view = await local_view(hass, owner)
        target = target_of(owner)
        sensors = entity_ids(owner)
        with evidence() as reads:
            result = await rediscover(hass, entry, cloud, [GATEWAY, "gateway-b"])
        assert result["type"] is FlowResultType.MENU, result
        reads.cloud.assert_awaited_once_with(GATEWAY)
        snapshot = owner.connections.management.current()
        assert_merged(snapshot, target)
        assert snapshot.bindings[binding_for(owner, GATEWAY)].connection_id == cloud
        await assert_handed_over(hass, owner, view, sensors)


# ---------------------------------------------------------- rejections


def mutated(kind):
    cloud, metadata = cloud_system(), None
    if kind == "serial":
        cloud["values"][0]["ModuleSerialNumber"] = "00099999999999999999999999"
    elif kind == "no_module":
        cloud["values"][0]["Tok"] = "other"
    elif kind == "gateway":
        cloud["gatewayId"] = "other-gateway"
    elif kind == "alias":
        metadata = {"id": GATEWAY, "deviceId": "other-gateway"}
    elif kind == "metadata":
        metadata = {"id": "other-gateway"}
    return cloud, metadata


@bounded
@pytest.mark.parametrize(
    "kind", ["serial", "no_module", "gateway", "alias", "metadata"]
)
async def test_a_failed_comparison_saves_nothing(hass, backend, kind) -> None:
    async with running(hass, OfferingBackend(), configure=local_first) as (
        entry,
        _,
        owner,
        _,
    ):
        await local_view(hass, owner)
        before = (deepcopy(dict(entry.data)), deepcopy(dict(entry.options)))
        cloud, metadata = mutated(kind)
        with evidence(cloud=cloud, metadata=metadata):
            result = await add_cloud(hass, entry, [GATEWAY])
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "cloud_select"
            assert result["errors"] == {"base": "cloud_local_first_conflict"}
            assert (dict(entry.data), dict(entry.options)) == before
            # Deselected, the plant stays local; the other system is added.
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {"gateway_ids": ["gateway-b"], "bacon_device_ids": []},
            )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        snapshot = owner.connections.management.current()
        record = snapshot.targets[target_of(owner)]
        assert record.kind == "local_installation"
        assert snapshot.to_options()["targets"][target_of(owner)]["local_plant"]


@bounded
async def test_a_failed_comparison_on_an_existing_cloud_keeps_the_selection(
    hass, backend
) -> None:
    async with running(hass, OfferingBackend(), configure=beside_cloud) as (
        entry,
        cloud,
        owner,
        _,
    ):
        await local_view(hass, owner)
        before = deepcopy(dict(entry.data))
        bad, _ = mutated("serial")
        with evidence(cloud=bad):
            result = await rediscover(hass, entry, cloud, [GATEWAY, "gateway-b"])
        assert result["step_id"] == "connection_discover"
        assert result["errors"] == {"base": "cloud_local_first_conflict"}
        assert dict(entry.data) == before


@bounded
@pytest.mark.parametrize("how", ["switched_off", "paused", "unreachable", "removed"])
async def test_without_a_ready_local_access_nothing_is_merged(
    hass, backend, how
) -> None:
    async with running(hass, OfferingBackend(), configure=local_first) as (
        entry,
        _,
        owner,
        _,
    ):
        await local_view(hass, owner)
        runtime = owner.connections
        management = runtime.management
        local = runtime.snapshot.bindings["local"].connection_id
        if how == "switched_off":
            management.set_binding_enabled(management.edit(local), "local", False)
            await settle(hass)
        elif how == "paused":
            state = runtime._local_states["local"]
            runtime._local_states["local"] = SimpleNamespace(
                **{**vars(state), "condition": LocalCondition.PAUSED}
            )
        elif how == "removed":
            management.remove(management.edit(local))
            await settle(hass)
        before = (deepcopy(dict(entry.data)), deepcopy(dict(entry.options)))
        with evidence(local=None) if how == "unreachable" else evidence() as reads:
            result = await add_cloud(hass, entry, [GATEWAY])
        assert result["step_id"] == "cloud_select"
        assert result["errors"] == {"base": "cloud_local_first_local_unavailable"}
        assert (dict(entry.data), dict(entry.options)) == before
        reads.cloud.assert_not_awaited()  # never on the gateway ID alone


@bounded
async def test_a_change_during_the_comparison_is_rejected(hass, backend) -> None:
    async with running(hass, OfferingBackend(), configure=local_first) as (
        entry,
        _,
        owner,
        _,
    ):
        await local_view(hass, owner)
        target = target_of(owner)

        async def changed(gateway):
            snapshot = owner.connections.management.current()
            data, options = snapshot.to_data(), snapshot.to_options()
            data["targets"][target]["local_details"]["model"] = "other"
            data["revision"] += 1
            hass.config_entries.async_update_entry(entry, data=data, options=options)
            return {"id": GATEWAY}, cloud_system()

        with evidence(side_effect=changed):
            result = await add_cloud(hass, entry, [GATEWAY])
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "configuration_changed"
        snapshot = owner.connections.management.current()
        assert snapshot.targets[target].kind == "local_installation"
        assert not any(
            c.kind == "singlekey_cloud" for c in snapshot.connections.values()
        )


@bounded
async def test_the_comparison_itself_notices_a_change(hass) -> None:
    async with running(hass, OfferingBackend(), configure=local_first) as (
        entry,
        _,
        owner,
        _,
    ):
        await local_view(hass, owner)
        target = target_of(owner)
        snapshot = owner.connections.management.current()

        async def changed(gateway):
            data, options = snapshot.to_data(), snapshot.to_options()
            data["targets"][target]["local_details"]["model"] = "other"
            data["revision"] += 1
            hass.config_entries.async_update_entry(entry, data=data, options=options)
            return {"id": GATEWAY}, cloud_system()

        client = SimpleNamespace(get_local_identity_evidence=changed)
        with (
            patch.object(
                Source, "read_system", AsyncMock(return_value=SYSTEM), create=True
            ),
            pytest.raises(ConfigurationError, match="configuration_changed"),
        ):
            await verify_local_first_merges(
                hass, snapshot, {GATEWAY}, lambda: client, "c"
            )


# ------------------------------------------------------- the builder rules


def stored(configure=None):
    """Stored data and options of an entry, without running it."""
    snapshot = convert_legacy_configuration(
        OWNER, legacy(["000123", "gateway-b"], ["softener"]), {}
    )
    data, options = snapshot.to_data(), snapshot.to_options()
    add_local(data, options)
    if configure is not None:
        configure(data, options)
    return data, options


def proof_for(data, target, **changes):
    values = {
        "owner_id": OWNER,
        "target_id": target,
        "gateway_id": GATEWAY,
        "binding_id": "local",
        "fingerprint": target_fingerprint(data["targets"][target]),
    }
    return LocalFirstProof(**{**values, **changes})


def merge(hass, data, options, proofs=None):
    _merge_targets(
        hass,
        OWNER,
        data,
        options,
        "cloud-new",
        {GATEWAY},
        set(),
        "",
        None,
        proofs=proofs,
    )
    data["connections"]["cloud-new"] = deepcopy(
        next(
            item
            for item in stored()[0]["connections"].values()
            if item["kind"] == "singlekey_cloud"
        )
    )
    options["connections"]["cloud-new"] = {
        "enabled": True,
        "polling_profile": "standard",
    }
    return load_configuration(OWNER, data, options)


def test_the_builder_converts_only_with_a_matching_proof(hass) -> None:
    data, options = stored(local_first)
    target = data["bindings"]["local"]["target_id"]
    assert local_first_targets(data, options, {GATEWAY}) == {GATEWAY: target}
    with pytest.raises(ConfigurationError, match="cloud_local_first_unverified"):
        merge(hass, deepcopy(data), deepcopy(options))
    for changes in (
        {"owner_id": "other-owner"},
        {"target_id": "target_other"},
        {"binding_id": "other-binding"},
        {"fingerprint": "0" * 64},
        {"issued_at": -(10.0**9)},
    ):
        proof = proof_for(data, target, **changes)
        with pytest.raises(ConfigurationError, match="configuration_changed"):
            merge(hass, deepcopy(data), deepcopy(options), {GATEWAY: proof})
    snapshot = merge(hass, data, options, {GATEWAY: proof_for(data, target)})
    assert_merged(snapshot, target)


def test_after_the_merge_the_local_setup_reuses_the_comparison(hass) -> None:
    data, options = stored(local_first)
    target = data["bindings"]["local"]["target_id"]
    snapshot = merge(hass, data, options, {GATEWAY: proof_for(data, target)})
    contract = retained_contract(snapshot, target, GATEWAY)
    assert contract is not None and contract.contract_id == CONTRACT
    # An attempt started for the local-first plant cannot complete any more.
    pending = ScopedIdentityContract(OWNER, target, GATEWAY, LOCAL_FIRST)
    with pytest.raises(ConfigurationError, match="identity_contract_scope_changed"):
        pending.verified_pairs(snapshot, target, GATEWAY)


async def test_a_local_first_plant_of_another_entry_is_never_claimed(
    hass, backend
) -> None:
    local_first_entry(hass, "other-local-first")
    entry = cloud_empty_entry(hass)
    result = await add_cloud(hass, entry, [GATEWAY])
    assert result["step_id"] == "cloud_select"
    assert result["errors"] == {"base": "cloud_local_first_other_entry"}
    assert not entry.data["connections"]


async def test_r1_with_a_stored_comparison_needs_no_new_read(hass) -> None:
    data, options = stored(removed())
    target = data["bindings"]["local"]["target_id"]
    for item in data["targets"][target]["identities"]:
        if item["namespace"] == "local_system_gateway":
            item["evidence"] = "verified_local_setup"
            item["comparison_contract"] = CONTRACT
    snapshot = load_configuration(OWNER, data, options)
    client = SimpleNamespace(get_local_identity_evidence=AsyncMock())
    factory = Mock(return_value=client)
    proofs = await verify_local_first_merges(hass, snapshot, {GATEWAY}, factory, "c")
    assert proofs == {}
    factory.assert_not_called()  # no cloud client at all
    client.get_local_identity_evidence.assert_not_awaited()
    merge(hass, data, options)  # as in step 6


def test_r1_without_a_stored_comparison_requires_the_proof(hass) -> None:
    data, options = stored(removed())
    target = data["bindings"]["local"]["target_id"]
    assert local_first_targets(data, options, {GATEWAY}) == {GATEWAY: target}
    with pytest.raises(ConfigurationError, match="cloud_local_first_unverified"):
        merge(hass, deepcopy(data), deepcopy(options))
    snapshot = merge(hass, data, options, {GATEWAY: proof_for(data, target)})
    assert retained_contract(snapshot, target, GATEWAY) is not None


@bounded
async def test_r1_without_a_stored_comparison_reads_both_systems(hass, backend) -> None:
    async with running(hass, OfferingBackend(), configure=removed()) as (
        entry,
        _,
        owner,
        _,
    ):
        await local_view(hass, owner)
        target = target_of(owner)
        with evidence() as reads:
            result = await add_cloud(hass, entry, [GATEWAY])
        assert result["type"] is FlowResultType.CREATE_ENTRY, result
        reads.cloud.assert_awaited_once_with(GATEWAY)
        snapshot = owner.connections.management.current()
        assert retained_contract(snapshot, target, GATEWAY) is not None


# ------------------------------------------- 8C-c: local-only to cloud


def sensor_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, entry_id="sensor-entry")
    entry.add_to_hass(hass)
    resource = parse_resource(
        {
            "id": ROOM,
            "type": "floatValue",
            "value": 35.0,
            "unitOfMeasure": "C",
            "writeable": False,
        }
    )
    coordinator = SimpleNamespace(
        config_entry=entry,
        hass=hass,
        gateway=SimpleNamespace(gateway_id=GATEWAY),
        resources={ROOM: resource},
    )
    (cloud,) = local_only_sensor._cloud_sensors(coordinator)[ROOM]
    assert (cloud.native_unit, cloud.state_class) == ("°C", "measurement")
    return entry, coordinator, cloud.unique_id


def register(hass, entry, unique_id, *, native="°C", **kwargs):
    """A local-only entry as the sensor leaves it after a reading."""
    kwargs.setdefault("unit_of_measurement", "°C")
    kwargs.setdefault("capabilities", {"state_class": "measurement"})
    registry = er.async_get(hass)
    entity_id = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id,
        config_entry=entry,
        suggested_object_id="room_temperature",
        **kwargs,
    ).entity_id
    if native is not None:
        registry.async_update_entity_options(entity_id, DOMAIN, {"native_unit": native})
    return entity_id


@pytest.mark.parametrize(
    "shown", ["celsius", "fahrenheit", "user_unit", "alias", "no_statistics"]
)
def test_a_local_only_entry_continues_as_the_cloud_sensor(hass, shown) -> None:
    entry, coordinator, cloud_id = sensor_entry(hass)
    kwargs = {}
    if shown == "no_statistics":  # no state class yet: gains the cloud's
        kwargs["capabilities"] = None
    if shown == "fahrenheit":  # imperial unit system: displayed, not native
        kwargs["unit_of_measurement"] = "°F"
    if shown == "user_unit":
        kwargs["unit_of_measurement"] = "K"
    entity_id = register(
        hass, entry, LOCAL_ONLY, native="C" if shown == "alias" else "°C", **kwargs
    )
    if shown == "user_unit":
        er.async_get(hass).async_update_entity_options(
            entity_id, "sensor", {"unit_of_measurement": "K"}
        )
    local_only_sensor._remove_registered(coordinator)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, cloud_id) == entity_id
    assert registry.async_get_entity_id("sensor", DOMAIN, LOCAL_ONLY) is None
    record = registry.async_get(entity_id)
    assert DOMAIN not in record.options  # the stored local unit is gone
    if shown == "user_unit":  # the user's choice stays
        assert record.options["sensor"] == {"unit_of_measurement": "K"}


@pytest.mark.parametrize(
    "case",
    [
        "unit",
        "unknown_unit",
        "state_class",
        "registered",
        "disabled",
        "ambiguous",
    ],
)
def test_a_local_only_entry_is_removed_when_unclear(hass, case) -> None:
    entry, coordinator, cloud_id = sensor_entry(hass)
    kwargs = {}
    native = "°C"
    if case == "unit":
        native = "K"
    if case == "unknown_unit":  # never showed a reading
        native = None
    if case == "state_class":
        kwargs["capabilities"] = {"state_class": "total_increasing"}
    if case == "disabled":
        kwargs["disabled_by"] = er.RegistryEntryDisabler.INTEGRATION
    entity_id = register(hass, entry, LOCAL_ONLY, native=native, **kwargs)
    registry = er.async_get(hass)
    existing = None
    if case == "registered":
        existing = registry.async_get_or_create(
            "sensor", DOMAIN, cloud_id, config_entry=entry
        ).entity_id
    from custom_components.bosch_buderus_heating import sensor

    build = sensor.build_sensor_descriptions

    def twice(resources):
        items = build(resources)
        return (*items, *(replace(i, unique_key=i.unique_key + "_b") for i in items))

    with patch.object(
        sensor, "build_sensor_descriptions", twice if case == "ambiguous" else build
    ):
        local_only_sensor._remove_registered(coordinator)
    assert registry.async_get(entity_id) is None  # removed as before
    assert registry.async_get_entity_id("sensor", DOMAIN, cloud_id) == existing


VENTILATION_RUN_TIME = "/ventilation/zone1/applianceRunTime"


@pytest.mark.parametrize("unit", ["mins", "h"])
def test_a_local_ventilation_run_time_continues_with_its_statistics(
    hass, unit
) -> None:
    """The local run time is a growing total; the cloud's reading is one too."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="sensor-entry")
    entry.add_to_hass(hass)
    resource = parse_resource(
        {
            "id": VENTILATION_RUN_TIME,
            "type": "floatValue",
            "value": 1234.0,
            "unitOfMeasure": unit,
            "writeable": False,
        }
    )
    coordinator = SimpleNamespace(
        config_entry=entry,
        hass=hass,
        gateway=SimpleNamespace(gateway_id=GATEWAY),
        resources={VENTILATION_RUN_TIME: resource},
    )
    (cloud,) = local_only_sensor._cloud_sensors(coordinator)[VENTILATION_RUN_TIME]
    assert cloud.state_class == "total_increasing"
    native_unit = "min" if unit == "mins" else "h"
    registry = er.async_get(hass)
    entity_id = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id(GATEWAY, VENTILATION_RUN_TIME),
        config_entry=entry,
        capabilities={"state_class": "total_increasing"},
        unit_of_measurement=native_unit,
    ).entity_id
    registry.async_update_entity_options(
        entity_id, DOMAIN, {"native_unit": native_unit}
    )
    local_only_sensor._remove_registered(coordinator)
    # Entity ID and history stay: the entry continues as the cloud sensor.
    assert registry.async_get_entity_id("sensor", DOMAIN, cloud.unique_id) == (
        entity_id
    )


def _sensor_platform(hass, entry):
    import logging
    from datetime import timedelta

    from homeassistant.helpers.entity_platform import EntityPlatform

    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="sensor",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    platform.async_prepare()
    return platform


def _plain_sensor(unique_id, value, state_class="measurement"):
    from homeassistant.components.sensor import SensorEntity

    sensor = SensorEntity()
    sensor._attr_unique_id = unique_id
    sensor._attr_native_value = value
    sensor._attr_state_class = state_class
    sensor._attr_should_poll = False
    return sensor


@pytest.mark.parametrize("clear", [True, False])
async def test_while_running_a_local_only_entity_is_handed_over(hass, clear) -> None:
    """The cloud starts offering the path while Home Assistant runs (8C-c)."""
    entry, coordinator, cloud_id = sensor_entry(hass)
    state_class = "measurement" if clear else "total_increasing"
    entity_id = register(
        hass, entry, LOCAL_ONLY, capabilities={"state_class": state_class}
    )
    removed = []

    @callback
    def note(event) -> None:
        if event.data["action"] == "remove":
            removed.append(event.data["entity_id"])

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, note)
    platform = _sensor_platform(hass, entry)
    await platform.async_add_entities([_plain_sensor(LOCAL_ONLY, 21.5, state_class)])
    assert hass.states.get(entity_id).state == "21.5"

    paths, done = local_only_sensor.hand_over_while_running(coordinator, {ROOM})
    assert paths == {ROOM}
    # Asked again before it finished: the same hand-over, not a second one.
    again, _ = local_only_sensor.hand_over_while_running(coordinator, {ROOM})
    assert again == {ROOM}
    await done
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, LOCAL_ONLY) is None
    assert hass.states.get(entity_id) is None  # the local-only entity left

    cloud = _plain_sensor(cloud_id, 22.0)
    await platform.async_add_entities([cloud])
    await hass.async_block_till_done()
    if clear:
        # The same entry: entity ID, settings and recorded history continue.
        assert registry.async_get_entity_id("sensor", DOMAIN, cloud_id) == entity_id
        assert cloud.entity_id == entity_id and not removed
        assert DOMAIN not in registry.async_get(entity_id).options
    else:
        # Another state class: removed as before, never a mixed statistic.
        assert removed == [entity_id]
    assert hass.states.get(cloud.entity_id).state == "22.0"
    # Nothing left to hand over.
    assert local_only_sensor.hand_over_while_running(coordinator, {ROOM}) is None
    await platform.async_reset()


async def test_a_new_cloud_sensor_waits_for_the_hand_over(hass) -> None:
    """The resource sensors' listener adds the cloud sensor only afterwards."""
    from datetime import UTC, datetime

    from custom_components.bosch_buderus_heating import sensor
    from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
    from custom_components.bosch_buderus_heating.pointt import Gateway
    from tests.test_validity import _coordinator

    entry, _, cloud_id = sensor_entry(hass)
    coordinator = _coordinator(hass)
    coordinator.config_entry = entry
    coordinator.gateway = Gateway(GATEWAY, device_type="MX400")
    entity_id = register(hass, entry, LOCAL_ONLY)
    added: list = []

    def add(entities):
        if entities:
            # When the cloud sensor is added, its entry is already handed over.
            assert (
                er.async_get(hass).async_get_entity_id("sensor", DOMAIN, LOCAL_ONLY)
                is None
            )
        added.extend(entities)

    scope = SimpleNamespace(async_on_unload=lambda _: None)
    sensor._track_resource_sensors(scope, coordinator, add)
    resource = sensor_entry_resource()
    coordinator.resources = {ROOM: resource}
    coordinator.data = {ROOM: ResourceSnapshot(resource, True, datetime.now(UTC))}
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert [item.unique_id for item in added] == [cloud_id]
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, cloud_id) == entity_id
    await coordinator.async_shutdown()


def _resource_sensor_setup(hass):
    from datetime import UTC, datetime

    from custom_components.bosch_buderus_heating import sensor
    from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
    from custom_components.bosch_buderus_heating.pointt import Gateway
    from tests.test_validity import _coordinator

    entry, _, cloud_id = sensor_entry(hass)
    coordinator = _coordinator(hass)
    coordinator.config_entry = entry
    coordinator.gateway = Gateway(GATEWAY, device_type="MX400")
    added: list = []
    scope = SimpleNamespace(async_on_unload=lambda _: None)
    sensor._track_resource_sensors(scope, coordinator, added.extend)
    resource = sensor_entry_resource()
    coordinator.resources = {ROOM: resource}
    coordinator.data = {ROOM: ResourceSnapshot(resource, True, datetime.now(UTC))}
    return entry, coordinator, cloud_id, added


async def test_a_failed_hand_over_never_adds_a_second_entity(hass, caplog) -> None:
    """The hand-over raises midway: no cloud sensor now, a later update retries."""
    entry, coordinator, cloud_id, added = _resource_sensor_setup(hass)
    entity_id = register(hass, entry, LOCAL_ONLY)
    real = local_only_sensor._async_hand_over

    async def broken(*_args) -> None:
        raise RuntimeError("midway")

    caplog.set_level("DEBUG")
    with patch.object(local_only_sensor, "_async_hand_over", broken):
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        coordinator.async_update_listeners()  # fails again: debug only
        await hass.async_block_till_done()
    assert added == []
    failures = [
        record
        for record in caplog.records
        if "Handing local-only entities over to cloud sensors failed"
        in record.getMessage()
    ]
    # O83: one warning per path with the error type, then debug.
    assert [record.levelname for record in failures] == ["WARNING", "DEBUG"]
    assert "(RuntimeError)" in failures[0].getMessage()
    assert "midway" not in caplog.text
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, LOCAL_ONLY) == entity_id
    assert local_only_sensor._async_hand_over is real
    coordinator.async_update_listeners()  # the next update tries again
    await hass.async_block_till_done()
    assert [item.unique_id for item in added] == [cloud_id]
    assert [
        record.levelname
        for record in caplog.records
        if "succeeded on a later try" in record.getMessage()
    ] == ["INFO"]
    assert registry.async_get_entity_id("sensor", DOMAIN, cloud_id) == entity_id
    await coordinator.async_shutdown()


async def test_a_local_only_entity_still_being_added_counts_as_present(
    hass, monkeypatch
) -> None:
    entry, coordinator, cloud_id, added = _resource_sensor_setup(hass)
    clock = {"now": 100.0}
    monkeypatch.setattr(local_only_sensor, "monotonic", lambda: clock["now"])
    # Handed to Home Assistant, not registered yet.
    local_only_sensor._note_pending_adds(coordinator, [ROOM])
    assert local_only_sensor.hand_over_open(coordinator, ROOM)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert added == []  # deferred, never a second entity ID
    # Now registered and added: handed over with the next update.
    entity_id = register(hass, entry, LOCAL_ONLY)
    platform = _sensor_platform(hass, entry)
    await platform.async_add_entities([_plain_sensor(LOCAL_ONLY, 21.5)])
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert [item.unique_id for item in added] == [cloud_id]
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, cloud_id) == entity_id
    # A pending add that never completes is waited for only for a while.
    local_only_sensor._note_pending_adds(coordinator, ["/other"])
    assert local_only_sensor._pending_adds(coordinator, ["/other"])
    clock["now"] += local_only_sensor.PENDING_ADD_SECONDS + 1
    assert not local_only_sensor._pending_adds(coordinator, ["/other"])
    await platform.async_reset()
    await coordinator.async_shutdown()


def sensor_entry_resource():
    return parse_resource(
        {
            "id": ROOM,
            "type": "floatValue",
            "value": 35.0,
            "unitOfMeasure": "C",
            "writeable": False,
        }
    )


async def test_the_sensor_keeps_the_native_unit_of_its_last_reading(hass) -> None:
    """Displayed units (unit system, user choice) are never stored."""
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        BoschBuderusLocalOnlySensor,
    )

    entry, _, _ = sensor_entry(hass)
    entity_id = register(hass, entry, LOCAL_ONLY, native=None, unit_of_measurement="°F")
    registry = er.async_get(hass)
    entity = object.__new__(BoschBuderusLocalOnlySensor)
    entity.hass = hass
    entity.entity_id = entity_id
    entity.registry_entry = registry.async_get(entity_id)
    entity._definition = SimpleNamespace(kind="temperature")
    with (
        patch.object(BoschBuderusLocalOnlySensor, "_shape", return_value=None),
    ):
        entity._remember_unit()  # no reading: nothing is stored
    assert DOMAIN not in registry.async_get(entity_id).options
    with (
        patch.object(
            BoschBuderusLocalOnlySensor, "_shape", return_value=(21.0, "°C", None)
        ),
        patch.object(
            BoschBuderusLocalOnlySensor,
            "native_unit_of_measurement",
            new_callable=lambda: property(lambda self: "°C"),
        ),
    ):
        entity._remember_unit()
    assert registry.async_get(entity_id).options[DOMAIN] == {"native_unit": "°C"}


def test_the_stored_plant_is_valid_before_and_after(hass) -> None:
    """The validation rules of entry format 4.2 hold for both stages."""
    data, options = stored(local_first)
    load_configuration(OWNER, data, options)
    target = data["bindings"]["local"]["target_id"]
    snapshot = merge(hass, data, options, {GATEWAY: proof_for(data, target)})
    assert {b.adapter for b in snapshot.bindings.values() if b.target_id == target} == {
        "k40_local",
        "pointt",
    }


def test_the_hand_over_runs_before_any_sensor_is_added(hass) -> None:
    """Home Assistant may register added entities at once (eager start)."""
    from custom_components.bosch_buderus_heating import sensor

    entry, coordinator, cloud_id = sensor_entry(hass)
    entity_id = register(hass, entry, LOCAL_ONLY)
    coordinator.local_selection = SimpleNamespace(has_configured_local_access=True)
    registry = er.async_get(hass)

    def eager(scope, coordinator, add):
        registry.async_get_or_create("sensor", DOMAIN, cloud_id, config_entry=entry)

    scope = SimpleNamespace(
        bacon=None, wallboxes=(), metrics=None, coordinators=(coordinator,)
    )
    with (
        patch.object(sensor, "_track_resource_sensors", eager),
        patch.object(sensor, "_track_source_sensors", Mock()),
        patch.object(sensor, "track_local_only_sensors", Mock()),
        patch.object(sensor, "track_estimated_energy_sensors", Mock()),
        patch.object(sensor, "track_radio_devices", Mock()),
        # The RRC package trackers need a real coordinator.
        patch.object(sensor.rrc_platforms, "track", Mock()),
        patch.object(sensor, "track_entity_once", Mock()),
        patch.object(sensor, "BoschBuderusActiveFaultsSensor", Mock()),
        patch.object(sensor, "BoschBuderusActiveNotificationsSensor", Mock()),
    ):
        sensor._setup_entities(scope, Mock())
    assert registry.async_get_entity_id("sensor", DOMAIN, cloud_id) == entity_id


# ------------------------------------------- review: requests and busy reads


async def test_a_plain_selection_is_saved_during_a_cloud_pause(hass, backend) -> None:
    """Without a locally set up system, saving makes no request at all."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.bosch_buderus_heating.auth_lifecycle import entry_auth
    from custom_components.bosch_buderus_heating.pointt import OAuthClient
    from tests.test_cloud_setup import existing

    entry, cloud, _ = existing(hass)
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    assert result["step_id"] == "connection_discover"
    _, pause = entry_auth(
        hass, entry, OAuthClient(async_get_clientsession(hass)), connection_id=cloud
    )
    pause.activate(900)
    listings = backend.gateways.await_count
    with evidence() as reads:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"gateway_ids": [GATEWAY], "bacon_device_ids": []}
        )
    assert result["type"] is FlowResultType.MENU, result
    assert backend.gateways.await_count == listings
    reads.cloud.assert_not_awaited()


def deferred(problem="busy"):
    from custom_components.bosch_buderus_heating.local_api.catalog import (
        request_for_path,
    )
    from custom_components.bosch_buderus_heating.local_read import (
        ReadResult,
        ReadState,
    )

    return ReadResult(
        request_for_path("/system/basicInfo"),
        ReadState.DEFERRED,
        problem=problem,
        retry_after_seconds=0.01,
    )


def answered():
    from custom_components.bosch_buderus_heating.local_api.catalog import (
        request_for_path,
    )
    from custom_components.bosch_buderus_heating.local_read import (
        ReadResult,
        ReadState,
    )

    return ReadResult(
        request_for_path("/system/basicInfo"),
        ReadState.SUCCESS,
        observation=SimpleNamespace(data=SYSTEM),
    )


def reading_source(results):
    from custom_components.bosch_buderus_heating.managed_local import LocalSource

    source = object.__new__(LocalSource)
    source.binding = SimpleNamespace(to_data=lambda: {"locator": {"value": GATEWAY}})
    source.client = SimpleNamespace(read=AsyncMock(side_effect=results))
    source.current = lambda: True
    return source


@bounded
async def test_a_busy_local_access_is_read_once_it_is_free() -> None:
    source = reading_source([deferred(), deferred("paused"), answered()])
    assert await source.read_system() is SYSTEM
    assert source.client.read.await_count == 3


@bounded
async def test_a_local_access_that_stays_busy_is_reported_as_busy() -> None:
    from custom_components.bosch_buderus_heating.managed_local import (
        SYSTEM_READ_ATTEMPTS,
    )

    sleep = AsyncMock()
    with patch(ROOT + ".managed_local.asyncio.sleep", sleep):
        source = reading_source([deferred()] * SYSTEM_READ_ATTEMPTS)
        assert await source.read_system() == "busy"
        # No wait after the last attempt.
        assert sleep.await_count == SYSTEM_READ_ATTEMPTS - 1
        # Busy for longer than one wait: busy, without waiting.
        sleep.reset_mock()
        long_busy = replace(deferred(), retry_after_seconds=600.0)
        assert await reading_source([long_busy]).read_system() == "busy"
        # The time budget ends while still busy: busy, not unavailable.
        with patch(ROOT + ".managed_local.SYSTEM_READ_SECONDS", 0.0):
            assert await reading_source([deferred()]).read_system() == "busy"
        sleep.assert_not_awaited()
    # A long pause of the access (for example after a rate limit) is paused.
    long = replace(deferred("paused"), retry_after_seconds=600.0)
    assert await reading_source([long]).read_system() is None


@bounded
async def test_a_busy_local_access_has_its_own_message(hass, backend) -> None:
    async with running(hass, OfferingBackend(), configure=local_first) as (
        entry,
        _,
        owner,
        _,
    ):
        await local_view(hass, owner)
        before = deepcopy(dict(entry.data))
        with evidence(local="busy") as reads:
            result = await add_cloud(hass, entry, [GATEWAY])
        assert result["step_id"] == "cloud_select"
        assert result["errors"] == {"base": "cloud_local_first_local_busy"}
        assert dict(entry.data) == before
        reads.cloud.assert_not_awaited()


async def test_the_last_real_unit_is_kept(hass) -> None:
    """A reading of another quantity stores nothing (no None, no write)."""
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        BoschBuderusLocalOnlySensor,
    )

    entry, _, _ = sensor_entry(hass)
    entity_id = register(hass, entry, LOCAL_ONLY, native="°C")
    registry = er.async_get(hass)

    def entity(kind):
        item = object.__new__(BoschBuderusLocalOnlySensor)
        item.hass = hass
        item.entity_id = entity_id
        item.registry_entry = registry.async_get(entity_id)
        item._definition = SimpleNamespace(kind=kind)
        return item

    unit = property(lambda self: None)
    update = Mock(wraps=registry.async_update_entity_options)
    with (
        patch.object(
            BoschBuderusLocalOnlySensor, "_shape", return_value=(None, None, None)
        ),
        patch.object(
            BoschBuderusLocalOnlySensor,
            "native_unit_of_measurement",
            new_callable=lambda: unit,
        ),
        patch.object(registry, "async_update_entity_options", update),
    ):
        entity("temperature")._remember_unit()
        update.assert_not_called()
        assert registry.async_get(entity_id).options[DOMAIN] == {"native_unit": "°C"}
        entity("state")._remember_unit()  # a state has no unit
        assert registry.async_get(entity_id).options[DOMAIN] == {"native_unit": None}


@bounded
async def test_a_cloud_pause_in_the_selection_shows_its_end(hass, backend) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.bosch_buderus_heating.auth_lifecycle import entry_auth
    from custom_components.bosch_buderus_heating.pointt import OAuthClient

    async with running(hass, OfferingBackend(), configure=beside_cloud) as (
        entry,
        cloud,
        owner,
        _,
    ):
        await local_view(hass, owner)
        before = deepcopy(dict(entry.data))
        result = await choose_connection(hass, entry, cloud)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "connection_discover"}
        )
        assert result["step_id"] == "connection_discover"
        _, pause = entry_auth(
            hass,
            entry,
            OAuthClient(async_get_clientsession(hass)),
            connection_id=cloud,
        )
        pause.activate(900)
        with evidence() as reads:
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {"gateway_ids": [GATEWAY, "gateway-b"], "bacon_device_ids": []},
            )
        assert result["step_id"] == "connection_discover_retry"
        assert result["errors"] == {"base": "cloud_rate_limited_until"}
        assert result["description_placeholders"]["until"]
        reads.local.assert_not_awaited()
        reads.cloud.assert_not_awaited()
        assert dict(entry.data) == before
