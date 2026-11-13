"""R1 (P49b): a system keeps its local readings after its cloud access is removed.

The managed entry runs with its real platforms and the real PointT client
against a synthetic cloud. Removing the cloud access keeps a system with a
local access as a read-only view of its local readings, under the same
entities; systems without a local access keep their entities unavailable.
"""

import asyncio
import functools
import importlib
import logging
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.configuration import (
    ConfigurationError,
    load_configuration,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_identity import CONTRACT
from custom_components.bosch_buderus_heating.local_only_sensor import (
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.managed_cloud import CloudDriverFactory
from custom_components.bosch_buderus_heating.managed_local_plant import (
    LocalPlantCoordinator,
)
from tests.test_connection_runtime import settle
from tests.test_local_backed import (
    GATEWAY,
    SENSOR,
    ReadingBackend,
    local_values,
    publish_local,
    started,
    state,
)
from tests.test_local_only_mode import cloud_off, first_run
from tests.test_local_plant_mode import (
    _faults_patch,
    publish_state,
    readings,
    retained,
    shown,
)
from tests.test_managed_platforms import NUMBER, native
from tests.test_managed_pointt import binding_for, eventually, running, source_for

INVENTORY = importlib.import_module(
    "custom_components.bosch_buderus_heating.discovery_inventory"
)
pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
SETPOINT = "/heatSources/currentSupplySetpoint"


def bounded(test):
    """Fail within 90 s instead of hanging (review R1)."""

    @functools.wraps(test)
    async def run(*args, **kwargs):
        async with asyncio.timeout(90):
            await test(*args, **kwargs)

    return run


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    hass.config.components.add(DOMAIN)


def target_of(owner):
    return owner.connections.snapshot.bindings["local"].target_id


def view_of(owner):
    return owner.connections.target_views().get(target_of(owner))


def startup_finished(backend) -> bool:
    """Every gateway read at the start has had its fault read."""
    gateways = {call[1] for call in backend.calls if call[0] == "bulk"}
    return bool(gateways) and all(
        ("bulk", gateway, ("/notifications",)) in backend.calls
        for gateway in gateways
    )


def remove_cloud(owner, cloud):
    management = owner.connections.management
    management.remove(management.edit(cloud))


def removed(*, local_access=True):
    """The stored configuration after the cloud access was removed (restart)."""
    base = local_values(local_access=local_access)

    def configure(data, options):
        base(data, options)
        for key in [
            key
            for key, item in data["connections"].items()
            if item["kind"] == "singlekey_cloud"
        ]:
            del data["connections"][key]
            del options["connections"][key]
            gone = [
                b
                for b, item in data["bindings"].items()
                if item["connection_id"] == key
            ]
            for binding in gone:
                target = data["bindings"][binding]["target_id"]
                if data["bindings"][binding]["adapter"] == "pointt" and any(
                    item["adapter"] == "k40_local" and item["target_id"] == target
                    for item in data["bindings"].values()
                ):
                    options["targets"][target]["local_plant"] = True
                del data["bindings"][binding]
                del options["bindings"][binding]
            for target in options["targets"].values():
                target["read_order"] = [
                    b for b in target["read_order"] if b not in gone
                ]

    return configure


@bounded
async def test_removing_a_running_cloud_keeps_the_local_entities(hass) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, cloud, owner, _):
        await started(hass, owner)
        entity_id = native(owner, "sensor", SENSOR).entity_id
        devices = dr.async_get(hass)
        device_id = er.async_get(hass).async_get(entity_id).device_id
        before = devices.async_get(device_id)
        old = source_for(owner, GATEWAY)
        # The start ends with one fault read per gateway; under load the last
        # one can still be running here. Count only after it.
        await eventually(lambda: startup_finished(backend))
        await settle(hass)
        calls = len(backend.calls)
        remove_cloud(owner, cloud)
        await eventually(lambda: isinstance(view_of(owner), LocalPlantCoordinator))
        view = view_of(owner)
        assert view.gateway.gateway_id == GATEWAY and view.local_retained
        await shown(hass, owner, readings(current=22.5))
        assert native(owner, "sensor", SENSOR).entity_id == entity_id
        assert native(owner, "sensor", SENSOR).coordinator is view
        await eventually(lambda: native(owner, "number", NUMBER) is None)
        publish_local(owner, 23.0)
        await eventually(lambda: state(hass, owner) == "23.0")
        await settle(hass)
        assert len(backend.calls) == calls  # nothing is requested
        assert not old.local_retained
        after = devices.async_get(device_id)
        assert (after.model, after.name, after.sw_version) == (
            before.model,
            before.name,
            before.sw_version,
        )
        # The other system has no local access: no view.
        assert len(owner.connections.target_views()) == 1
        # 6B-a: its descriptions stay for the view.
        inventory = INVENTORY.discovery_inventory(hass, owner.entry.entry_id)
        assert inventory.templates(GATEWAY)
        assert not inventory.templates("gateway-b")  # no local access: pruned
        # Read-only: only sensor, binary sensor and event platforms attach.
        key = "\0target:" + target_of(owner)
        attached = {
            platform
            for platform, controller in owner.platforms._platforms.items()
            if key in controller._active
        }
        assert {str(item) for item in attached} == {"sensor", "binary_sensor", "event"}
        unique_ids = [
            item.unique_id
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), owner.entry.entry_id
            )
        ]
        assert len(unique_ids) == len(set(unique_ids))


@bounded
@pytest.mark.parametrize("expired", [False, True])
async def test_removing_a_switched_off_cloud_keeps_the_view_and_faults(
    hass, expired
) -> None:
    from tests.test_local_faults import ITEM

    backend = ReadingBackend()
    await first_run(hass, backend)
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, SENSOR)
    with (
        _faults_patch(),
        patch.object(
            INVENTORY, "MAX_AGE", timedelta(0) if expired else INVENTORY.MAX_AGE
        ),
    ):
        async with running(hass, backend, configure=cloud_off()) as (
            _,
            cloud,
            owner,
            _,
        ):
            await owner.start_platforms()
            await eventually(lambda: retained(owner) is not None)
            before = retained(owner)
            assert isinstance(before, LocalPlantCoordinator) is expired
            await eventually(lambda: "local" in owner.connections._contexts)
            publish_state(owner, readings(), ITEM)
            await eventually(lambda: before.faults.active)
            fingerprint = before.faults.active[0].fingerprint
            gate = asyncio.Event()
            release = before.async_release_retention

            async def slow_release():
                await gate.wait()
                await release()

            with patch.object(before, "async_release_retention", slow_release):
                remove_cloud(owner, cloud)
                await settle(hass)
                # Not before the removed view saved its fault baseline.
                assert view_of(owner) is None
                gate.set()
                await eventually(lambda: view_of(owner) is not None)
            view = view_of(owner)
            assert view is not before and not owner.connections._retained
            # The removed view saved its baseline before this one loaded it.
            assert fingerprint in {item.fingerprint for item in view.faults.active}
            await shown(hass, owner, readings(current=24.0))
            assert native(owner, "sensor", SENSOR).entity_id == entity_id


@bounded
async def test_a_restart_after_the_removal_shows_the_same_entities(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    sets = []
    for _ in range(2):
        async with running(hass, backend, configure=removed()) as (_, _, owner, _):
            await owner.start_platforms()
            await eventually(lambda owner=owner: view_of(owner) is not None)
            await shown(hass, owner, readings())
            setpoint = local_only_unique_id(GATEWAY, SETPOINT)
            await eventually(
                lambda owner=owner, setpoint=setpoint: native(owner, "sensor", setpoint)
            )
            await settle(hass)
            sets.append(
                {
                    (platform, entity.unique_id)
                    for platform, controller in owner.platforms._platforms.items()
                    for entity in controller.platform.entities.values()
                }
            )
            assert not backend.status
    assert sets[0] == sets[1] and ("sensor", SENSOR) in sets[0]


@bounded
async def test_with_the_local_access_switched_off_nothing_is_shown(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    configure = removed(local_access=False)
    async with running(hass, backend, configure=configure) as (_, _, owner, _):
        await owner.start_platforms()
        await settle(hass)
        assert not owner.connections.target_views()
        assert native(owner, "sensor", SENSOR) is None
        # The descriptions stay for a configured local access (switched off).
        inventory = INVENTORY.discovery_inventory(hass, owner.entry.entry_id)
        assert inventory.templates(GATEWAY)


@bounded
@pytest.mark.parametrize("how", ["deselected", "forgotten"])
async def test_a_deselected_or_forgotten_system_stays_hidden(hass, how) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, cloud, owner, _):
        await started(hass, owner)
        management = owner.connections.management
        binding = binding_for(owner, GATEWAY)
        target = target_of(owner)
        # An earlier mark (for example from a removal before) never stays.
        snapshot = management.current()
        data, options = snapshot.to_data(), snapshot.to_options()
        options["targets"][target]["local_plant"] = True
        data["revision"] += 1
        hass.config_entries.async_update_entry(owner.entry, data=data, options=options)
        await settle(hass)
        management.set_binding_enabled(management.edit(cloud), binding, False)
        if how == "forgotten":
            management.forget_bindings(management.edit(cloud), {binding})
            await settle(hass)
            assert not owner.connections.target_views()
        remove_cloud(owner, cloud)
        await settle(hass)
        assert not owner.connections.target_views()
        assert native(owner, "sensor", SENSOR) is None
        targets = owner.connections.snapshot.to_options()["targets"]
        assert "local_plant" not in targets[target]


@bounded
async def test_removing_the_local_access_keeps_the_mark_and_descriptions(
    hass,
) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (
        entry,
        cloud,
        owner,
        _,
    ):
        await started(hass, owner)
        remove_cloud(owner, cloud)
        await eventually(lambda: view_of(owner) is not None)
        target = target_of(owner)
        management = owner.connections.management
        before = management.current()
        local = before.bindings["local"].connection_id
        kept = (
            deepcopy(before.to_data()["connections"][local]),
            deepcopy(before.to_data()["bindings"]["local"]),
            deepcopy(before.to_options()["connections"][local]),
            deepcopy(before.to_options()["bindings"]["local"]),
        )
        management.remove(management.edit(local))
        await settle(hass)
        assert not owner.connections.target_views()
        assert native(owner, "sensor", SENSOR) is None
        options = owner.connections.snapshot.to_options()
        assert options["targets"][target]["local_plant"] is True  # kept
        inventory = INVENTORY.discovery_inventory(hass, owner.entry.entry_id)
        assert inventory.templates(GATEWAY)  # kept with the mark
        # A local access is added again: the view comes back.
        snapshot = management.current()
        data, options = snapshot.to_data(), snapshot.to_options()
        data["connections"][local], data["bindings"]["local"] = kept[:2]
        options["connections"][local], options["bindings"]["local"] = kept[2:]
        options["targets"][target]["read_order"].append("local")
        data["revision"] += 1
        hass.config_entries.async_update_entry(entry, data=data, options=options)
        await eventually(lambda: view_of(owner) is not None)
        await shown(hass, owner, readings(current=26.0))


@bounded
async def test_switching_the_local_access_off_and_on_shows_it_again(hass) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, cloud, owner, _):
        await started(hass, owner)
        remove_cloud(owner, cloud)
        await eventually(lambda: view_of(owner) is not None)
        view = view_of(owner)
        gate = asyncio.Event()
        release = view.async_release_retention

        async def slow_release():
            await gate.wait()
            await release()

        management = owner.connections.management
        local = owner.connections.snapshot.bindings["local"].connection_id
        inventory = INVENTORY.discovery_inventory(hass, owner.entry.entry_id)
        with patch.object(view, "async_release_retention", slow_release):
            management.set_binding_enabled(management.edit(local), "local", False)
            await settle(hass)
            assert not owner.connections.target_views()
            await eventually(lambda: native(owner, "sensor", SENSOR) is None)
            # 6B-a: the descriptions stay while the local access is off.
            assert inventory.templates(GATEWAY)
            management.set_binding_enabled(management.edit(local), "local", True)
            await settle(hass)
            # Not before the previous view saved its fault baseline.
            assert view_of(owner) is None
            gate.set()
            await eventually(lambda: view_of(owner) is not None)
        assert view_of(owner) is not view and not view.local_retained


def failing_builds(count):
    """The first ``count`` builds fail; the half-built views are kept."""
    built = []
    original = LocalPlantCoordinator.refresh_view

    def refresh(self):
        if len(built) < count and self not in built:
            built.append(self)
            raise RuntimeError("broken reading")
        return original(self)

    return built, patch.object(LocalPlantCoordinator, "refresh_view", refresh)


@bounded
async def test_a_failed_build_is_tried_again(hass, caplog) -> None:
    runtime_log = "custom_components.bosch_buderus_heating.connection_runtime"
    caplog.set_level(logging.DEBUG, logger=runtime_log)
    built, failing = failing_builds(2)
    backend = ReadingBackend()
    with failing:
        async with running(hass, backend, configure=local_values()) as (
            _,
            cloud,
            owner,
            harness,
        ):
            await started(hass, owner)
            remove_cloud(owner, cloud)
            await eventually(lambda: built)
            await settle(hass)
            runtime = owner.connections
            assert not runtime.target_views()
            assert built[0]._remove_local_listener is None  # stopped
            assert built[0].faults._handed_over  # never writes
            assert "could not be shown; retrying" in caplog.text
            # ERR-9: the class in the warning, one traceback at debug level.
            tracebacks = [
                record
                for record in caplog.records
                if record.name == runtime_log and record.exc_info
            ]
            assert len(tracebacks) == len(built) == 1
            assert tracebacks[0].levelname == "DEBUG"
            assert "broken reading" not in " ".join(
                record.getMessage()
                for record in caplog.records
                if record.levelno >= logging.WARNING
            )
            retry = runtime._target_retry
            assert retry is not None and not retry.done()
            # Another failure (here: the next change) adds no second retry.
            runtime._synchronize_target_views()
            await eventually(lambda: len(built) == 2)
            await settle(hass)
            assert runtime._target_retry is retry
            harness.retry.set()  # the pause before the next attempt ends
            await eventually(lambda: view_of(owner) is not None)


@bounded
async def test_closing_never_waits_for_a_pending_retry(hass) -> None:
    built, failing = failing_builds(1)
    backend = ReadingBackend()
    with failing:
        async with running(hass, backend, configure=local_values()) as (
            _,
            cloud,
            owner,
            _,
        ):
            await started(hass, owner)
            remove_cloud(owner, cloud)
            await eventually(lambda: built)
            await settle(hass)
            retry = owner.connections._target_retry
            assert retry is not None
            loop = asyncio.get_running_loop()
            started_at = loop.time()
            await owner.connections.close()
            assert loop.time() - started_at < 3  # not the close timeout
            assert retry.done()


@bounded
async def test_a_retry_after_a_rejected_configuration_builds_nothing(hass) -> None:
    built, failing = failing_builds(1)
    backend = ReadingBackend()
    with failing:
        async with running(hass, backend, configure=local_values()) as (
            _,
            cloud,
            owner,
            harness,
        ):
            await started(hass, owner)
            remove_cloud(owner, cloud)
            await eventually(lambda: built)
            await settle(hass)
            runtime = owner.connections
            assert runtime._target_retry is not None
            runtime._reject_configuration()
            assert runtime._target_retry is None
            harness.retry.set()
            await settle(hass)
            assert not runtime.target_views()
            assert not runtime._target_building


@bounded
async def test_a_slow_removal_delays_but_never_loses_entities(hass) -> None:
    from custom_components.bosch_buderus_heating import managed_platforms

    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, cloud, owner, _):
        await started(hass, owner)
        old = native(owner, "sensor", SENSOR)
        gate = asyncio.Event()
        remove = old.async_remove

        async def slow_remove(*args, **kwargs):
            await gate.wait()
            await remove(*args, **kwargs)

        with (
            patch.object(managed_platforms, "_PREFIX_WAIT_SECONDS", 0.2),
            patch.object(old, "async_remove", slow_remove),
        ):
            remove_cloud(owner, cloud)
            await eventually(lambda: view_of(owner) is not None)
            await asyncio.sleep(0.5)  # longer than the bounded wait
            await settle(hass)
            # Nothing is added while the old entity still holds its ID.
            assert native(owner, "sensor", SENSOR) is old
            gate.set()
            await eventually(
                lambda: (
                    (entity := native(owner, "sensor", SENSOR)) is not None
                    and entity.coordinator is view_of(owner)
                )
            )
        await shown(hass, owner, readings(current=25.5))


@bounded
async def test_a_rejected_configuration_ends_the_view(hass) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, cloud, owner, _):
        await started(hass, owner)
        remove_cloud(owner, cloud)
        await eventually(lambda: view_of(owner) is not None)
        view = view_of(owner)
        owner.connections._reject_configuration()
        assert not owner.connections.target_views()
        await eventually(lambda: not view.local_retained)


@bounded
async def test_adding_the_cloud_again_takes_over_the_same_entities(
    hass, caplog
) -> None:
    from custom_components.bosch_buderus_heating.cloud_configuration import (
        _merge_targets,
    )

    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (
        entry,
        cloud,
        owner,
        _,
    ):
        await started(hass, owner)
        entity_id = native(owner, "sensor", SENSOR).entity_id
        old_binding = binding_for(owner, GATEWAY)
        record = deepcopy(entry.data["connections"][cloud])
        remove_cloud(owner, cloud)
        await eventually(lambda: view_of(owner) is not None)
        view = view_of(owner)
        await shown(hass, owner, readings())
        # Signed in again: a new access selects the same system, as the add
        # dialog stores it; the records of the removed binding remain.
        assert any(item.binding_id == old_binding for item in owner.inventory.entities)
        snapshot = owner.connections.management.current()
        data, options = snapshot.to_data(), snapshot.to_options()
        # A real local setup stored its comparison with the cloud (P49b step
        # 8: without it, a fresh comparison is required, see the merge tests).
        for item in data["targets"][target_of(owner)]["identities"]:
            if item["namespace"] == "local_system_gateway":
                item["evidence"] = "verified_local_setup"
                item["comparison_contract"] = CONTRACT
        data["connections"]["cloud-again"] = record
        options["connections"]["cloud-again"] = {
            "enabled": True,
            "polling_profile": "standard",
        }
        _merge_targets(
            hass,
            entry.entry_id,
            data,
            options,
            "cloud-again",
            {GATEWAY},
            set(),
            "",
            None,
        )
        assert "local_plant" not in options["targets"][target_of(owner)]
        data["revision"] += 1
        hass.config_entries.async_update_entry(entry, data=data, options=options)
        await eventually(lambda: source_for(owner, GATEWAY) is not None)
        current = source_for(owner, GATEWAY)
        assert current is not view and binding_for(owner, GATEWAY) != old_binding
        await eventually(lambda: not view.local_retained)
        assert not owner.connections.target_views()
        assert not owner.connections._retained
        assert view.faults._handed_over  # the cloud continues its baseline
        await eventually(lambda: native(owner, "sensor", SENSOR) is not None)
        assert native(owner, "sensor", SENSOR).entity_id == entity_id
        await eventually(lambda: native(owner, "sensor", SENSOR).coordinator is current)
        assert owner.inventory.healthy
    assert "inventory_identity_conflict" not in caplog.text


@bounded
async def test_a_change_during_the_build_drops_the_view(hass) -> None:
    gate, entered = asyncio.Event(), asyncio.Event()
    original = CloudDriverFactory.target_plant_view

    async def slow(self, target_id, gateway_id):
        entered.set()
        await gate.wait()
        return await original(self, target_id, gateway_id)

    backend = ReadingBackend()
    with patch.object(CloudDriverFactory, "target_plant_view", slow):
        async with running(hass, backend, configure=local_values()) as (
            _,
            cloud,
            owner,
            _,
        ):
            await started(hass, owner)
            remove_cloud(owner, cloud)
            await eventually(entered.is_set)
            management = owner.connections.management
            local = owner.connections.snapshot.bindings["local"].connection_id
            management.set_binding_enabled(management.edit(local), "local", False)
            gate.set()
            await settle(hass)
            assert not owner.connections.target_views()
            assert not owner.connections._target_building


def test_the_mark_is_validated_and_kept() -> None:
    from custom_components.bosch_buderus_heating.configuration_migration import (
        convert_legacy_configuration,
    )
    from tests.test_configuration import OWNER, legacy

    snapshot = convert_legacy_configuration(OWNER, legacy(["000123"], []), {})
    data, options = snapshot.to_data(), snapshot.to_options()
    target = next(iter(options["targets"]))
    options["targets"][target]["local_plant"] = True
    loaded = load_configuration(OWNER, data, options)
    assert loaded.to_options()["targets"][target]["local_plant"] is True
    options["targets"][target]["local_plant"] = "yes"
    with pytest.raises(ConfigurationError, match="invalid_local_plant"):
        load_configuration(OWNER, data, options)
