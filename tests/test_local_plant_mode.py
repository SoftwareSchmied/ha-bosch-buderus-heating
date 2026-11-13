"""P49b step 4: the local plant view of a switched-off access, behind its switch.

The managed entry runs with its real platforms and the real PointT client
against a synthetic cloud. A first run stores the inventory and registers the
cloud entities; a later run with the cloud access switched off and without a
usable inventory shows the plant's entities from its local readings alone.
"""

import asyncio
import importlib
import itertools
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import managed_pointt
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_only_sensor import (
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.managed_local_plant import (
    LocalPlantCoordinator,
)
from tests.ha_test_helpers import async_fire_time_changed
from tests.test_connection_runtime import settle
from tests.test_local_backed import GATEWAY, SENSOR, ReadingBackend, state
from tests.test_local_only_mode import cloud_off, first_run
from tests.test_local_plant_view import _publish
from tests.test_managed_local_plant import ROOM, SETPOINT, reading
from tests.test_managed_platforms import NUMBER, native
from tests.test_managed_pointt import (
    TEMP,
    binding_for,
    eventually,
    running,
    source_for,
)

INVENTORY = importlib.import_module(
    "custom_components.bosch_buderus_heating.discovery_inventory"
)
pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
CURRENT = "/heatingCircuits/hc2/currentRoomSetpoint"


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    hass.config.components.add(DOMAIN)


@contextmanager
def plant_view(*, old_inventory=True):
    with (
        patch.object(managed_pointt, "LOCAL_PLANT_VIEW", True),
        patch.object(
            INVENTORY, "MAX_AGE", timedelta(0) if old_inventory else INVENTORY.MAX_AGE
        ),
    ):
        yield


def readings(current=21.5, setpoint=34.5, room=21.25):
    return [
        reading(CURRENT, "floatValue", current, unitOfMeasure="C"),
        reading(SETPOINT, "floatValue", setpoint, unitOfMeasure="C"),
        reading(ROOM, "floatValue", room, unitOfMeasure="C"),
        reading("/gateway/eth/ip/ipv4", "stringValue", "192.0.2.10"),
    ]


def retained(owner):
    item = next(iter(owner.connections._retained.values()), None)
    return item[0] if item is not None else None


def entity_state(hass, owner, platform, unique_id):
    entity = native(owner, platform, unique_id)
    return hass.states.get(entity.entity_id).state if entity else None


def publish(owner, observations):
    """Current local readings, as the running local source publishes them."""
    _publish(owner, observations)


async def shown(hass, owner, observations):
    await eventually(lambda: "local" in owner.connections._contexts)
    publish(owner, observations)
    await eventually(lambda: native(owner, "sensor", SENSOR))
    await eventually(lambda: state(hass, owner) == str(observations[0].data.value))


def test_the_switch_is_on() -> None:
    assert managed_pointt.LOCAL_PLANT_VIEW is True


async def test_the_local_plant_view_shows_the_local_entities(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    registry = er.async_get(hass)
    cloud_entity = registry.async_get_entity_id("sensor", DOMAIN, SENSOR)
    calls = len(backend.calls)
    with plant_view():
        async with running(hass, backend, configure=cloud_off()) as (_, _, owner, _):
            await owner.start_platforms()
            await eventually(lambda: isinstance(retained(owner), LocalPlantCoordinator))
            view = retained(owner)
            assert view.local_retained and view.update_interval is None
            # 1A: the stored descriptions, without values, as templates.
            assert {CURRENT, TEMP} <= set(view._templates)
            assert not view._templates[TEMP].has_value
            assert view._templates[TEMP].metadata.minimum == 5
            await shown(hass, owner, readings())
            # The paired reading keeps the cloud entity and its history.
            assert native(owner, "sensor", SENSOR).entity_id == cloud_entity
            setpoint = local_only_unique_id(GATEWAY, SETPOINT)
            room = local_only_unique_id(GATEWAY, ROOM)
            await eventually(lambda: native(owner, "sensor", setpoint) is not None)
            await eventually(lambda: entity_state(hass, owner, "sensor", setpoint))
            assert entity_state(hass, owner, "sensor", setpoint) == "34.5"
            # Disabled by default (a generic reading), but offered.
            assert registry.async_get_entity_id("sensor", DOMAIN, room)
            # Read-only: no controls from local readings.
            await settle(hass)
            assert native(owner, "number", NUMBER) is None
            attached = {
                platform
                for platform, controller in owner.platforms._platforms.items()
                if binding_for(owner, GATEWAY) in controller._active
            }
            assert attached == {Platform.SENSOR, Platform.BINARY_SENSOR, Platform.EVENT}
            # New readings arrive by themselves.
            await shown(hass, owner, readings(current=22.0))
            # The local access offline: unavailable, but kept.
            owner.connections._local_states.pop("local")
            owner.connections._notify()
            await eventually(lambda: state(hass, owner) == "unavailable")
            assert native(owner, "sensor", SENSOR) is not None
            await shown(hass, owner, readings(current=22.5))
            await view.async_refresh()
            await settle(hass)
            assert len(backend.calls) == calls  # not a single cloud request


async def test_without_the_switch_nothing_is_shown(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    with (
        patch.object(managed_pointt, "LOCAL_PLANT_VIEW", False),
        patch.object(INVENTORY, "MAX_AGE", timedelta(0)),
    ):
        async with running(hass, backend, configure=cloud_off()) as (_, _, owner, _):
            await owner.start_platforms()
            await eventually(lambda: "local" in owner.connections._contexts)
            publish(owner, readings())
            await settle(hass)
            assert not owner.connections._retained
            assert native(owner, "sensor", SENSOR) is None


async def test_without_a_local_access_nothing_is_built(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    configure = cloud_off(local_access=False)
    with plant_view():
        async with running(hass, backend, configure=configure) as (_, _, owner, _):
            await owner.start_platforms()
            await settle(hass)
            assert not owner.connections._retained


async def test_a_usable_inventory_still_starts_the_dormant_view(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    with plant_view(old_inventory=False):
        async with running(hass, backend, configure=cloud_off()) as (_, _, owner, _):
            await owner.start_platforms()
            await eventually(lambda: retained(owner) is not None)
            assert not isinstance(retained(owner), LocalPlantCoordinator)
            assert retained(owner).startup_mode == "inventory"


async def test_the_same_entity_from_dormant_to_local_to_cloud(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, SENSOR)
    with plant_view(old_inventory=False):
        async with running(hass, backend, configure=cloud_off()) as (
            _,
            cloud,
            owner,
            _,
        ):
            await owner.start_platforms()
            await eventually(lambda: retained(owner) is not None)
            dormant = retained(owner)
            await shown(hass, owner, readings())
            assert native(owner, "sensor", SENSOR).entity_id == entity_id
            await eventually(lambda: native(owner, "number", NUMBER) is not None)
            runtime = owner.connections
            binding = binding_for(owner, GATEWAY)
            gate = asyncio.Event()
            release = dormant.async_release_retention

            async def slow_release():
                await gate.wait()
                await release()

            # The inventory passed its age limit: the view is built again.
            with (
                patch.object(INVENTORY, "MAX_AGE", timedelta(0)),
                patch.object(dormant, "async_release_retention", slow_release),
            ):
                assert runtime.expire_dormant(binding, dormant)
                assert not runtime.expire_dormant(binding, dormant)  # once
                await settle(hass)
                # Not before the old view finished its release (fault baseline).
                assert retained(owner) is None
                gate.set()
                await eventually(
                    lambda: isinstance(retained(owner), LocalPlantCoordinator)
                )
            plant = retained(owner)
            await shown(hass, owner, readings(current=23.0))
            assert native(owner, "sensor", SENSOR).entity_id == entity_id
            # The dormant view's controls are gone; nothing else claims the ID.
            await eventually(lambda: native(owner, "number", NUMBER) is None)
            assert not dormant.local_retained
            # The cloud switched on again takes over the same entity.
            management = runtime.management
            management.set_enabled(management.edit(cloud), True)
            await eventually(lambda: source_for(owner, GATEWAY) is not None)
            await eventually(lambda: not plant.local_retained)
            assert not runtime._retained
            await eventually(lambda: native(owner, "sensor", SENSOR) is not None)
            assert native(owner, "sensor", SENSOR).entity_id == entity_id
            assert native(owner, "sensor", SENSOR).coordinator is not plant
            unique_ids = [
                item.unique_id
                for item in er.async_entries_for_config_entry(
                    registry, owner.entry.entry_id
                )
            ]
            assert len(unique_ids) == len(set(unique_ids))


async def test_a_restart_gives_the_same_entities(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    device_id = registry.async_get(
        registry.async_get_entity_id("sensor", DOMAIN, SENSOR)
    ).device_id
    before = devices.async_get(device_id)
    assert before is not None and before.model is not None
    sets = []
    for _ in range(2):
        with plant_view():
            async with running(hass, backend, configure=cloud_off()) as (
                _,
                _,
                owner,
                _,
            ):
                await owner.start_platforms()
                await eventually(
                    lambda owner=owner: isinstance(
                        retained(owner), LocalPlantCoordinator
                    )
                )
                await shown(hass, owner, readings())
                await eventually(
                    lambda owner=owner: (
                        native(owner, "sensor", local_only_unique_id(GATEWAY, SETPOINT))
                        is not None
                    )
                )
                await settle(hass)
                sets.append(
                    {
                        (platform, entity.unique_id)
                        for platform, controller in owner.platforms._platforms.items()
                        for entity in controller.platform.entities.values()
                    }
                )
    assert sets[0] == sets[1] and sets[0]
    # The device keeps what the cloud reported: nothing is overwritten.
    after = devices.async_get(device_id)
    assert (after.model, after.sw_version, after.serial_number, after.name) == (
        before.model,
        before.sw_version,
        before.serial_number,
        before.name,
    )


# ------------------------------------------------- S1: expiry and return


def publish_state(owner, observations, *faults):
    """A ready local state, optionally with a current error list."""
    from custom_components.bosch_buderus_heating.gateway_identity import (
        GatewayNamespace,
    )
    from custom_components.bosch_buderus_heating.local_state import LocalCondition
    from tests.test_managed_local_plant import fault_view

    runtime = owner.connections
    context = runtime._contexts["local"]
    runtime._local_states["local"] = SimpleNamespace(
        context=context.identity_context(GatewayNamespace.K40_LOCAL),
        condition=LocalCondition.READY,
        current_observations=tuple(observations),
        resources=(fault_view(*faults, monotonic=next(_LISTS)),) if faults else (),
    )
    runtime._notify()


_LISTS = itertools.count(1.0)


async def test_the_inventory_expiry_hands_over_to_the_local_readings(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, SENSOR)
    async with running(hass, backend, configure=cloud_off()) as (_, _, owner, _):
        await owner.start_platforms()
        await eventually(lambda: retained(owner) is not None)
        dormant = retained(owner)
        assert not isinstance(dormant, LocalPlantCoordinator)
        await shown(hass, owner, readings())
        await eventually(lambda: native(owner, "number", NUMBER) is not None)
        calls = len(backend.calls)
        stored = dormant.inventory.gateway(GATEWAY)
        due = stored.saved_at + INVENTORY.MAX_AGE
        assert dormant._inventory_expiry is not None
        # Home Assistant's clock runs ahead of the inventory's clock: by the
        # inventory's own rule it is still valid, so it is checked again later.
        async_fire_time_changed(hass, due + timedelta(seconds=5))
        await settle(hass)
        assert retained(owner) is dormant
        assert dormant._inventory_expiry is not None
        with patch.object(INVENTORY, "MAX_AGE", timedelta(0)):
            async_fire_time_changed(hass, due + timedelta(days=1))
            await eventually(lambda: isinstance(retained(owner), LocalPlantCoordinator))
            assert not dormant.local_retained
            assert dormant._inventory_expiry is None
            await shown(hass, owner, readings(current=24.0))
            assert native(owner, "sensor", SENSOR).entity_id == entity_id
            await eventually(lambda: native(owner, "number", NUMBER) is None)
            await settle(hass)
        assert len(backend.calls) == calls  # not a single cloud request


async def test_the_returning_cloud_ends_the_expiry_timer(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    async with running(hass, backend, configure=cloud_off()) as (_, cloud, owner, _):
        await owner.start_platforms()
        await eventually(lambda: retained(owner) is not None)
        dormant = retained(owner)
        assert dormant._inventory_expiry is not None
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), True)
        await eventually(lambda: source_for(owner, GATEWAY) is not None)
        await eventually(lambda: not dormant.local_retained)
        assert dormant._inventory_expiry is None
        assert source_for(owner, GATEWAY)._inventory_expiry is None


async def test_unloading_ends_the_expiry_timer(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    views = []
    for _ in range(2):  # unload, then load again
        async with running(hass, backend, configure=cloud_off()) as (_, _, owner, _):
            await owner.start_platforms()
            await eventually(lambda owner=owner: retained(owner) is not None)
            views.append(retained(owner))
            assert views[-1]._inventory_expiry is not None
        assert views[-1]._inventory_expiry is None  # no lingering timer
    assert views[0] is not views[1]
    assert views[0]._inventory_expiry is None


async def test_a_failed_cloud_start_gives_the_fault_baseline_back(hass) -> None:
    """Review P49b step 5: the cloud starts, fails before it publishes, and
    the access is switched off again. The local plant view keeps writing the
    baseline, so a later local fault survives a stop."""
    from custom_components.bosch_buderus_heating.faults import FaultTracker
    from tests.test_local_faults import ITEM

    second = {**ITEM, "ccd": 522, "dcd": "4P"}
    faults_module = importlib.import_module(
        "custom_components.bosch_buderus_heating.faults"
    )
    backend = ReadingBackend()
    await first_run(hass, backend)
    with (
        patch.object(INVENTORY, "MAX_AGE", timedelta(0)),
        # Only an explicit save reaches the store during this test.
        patch.object(faults_module, "FAULT_STORAGE_SAVE_DELAY", 3600),
    ):
        async with running(hass, backend, configure=cloud_off()) as (
            entry,
            cloud,
            owner,
            _,
        ):
            await owner.start_platforms()
            await eventually(lambda: isinstance(retained(owner), LocalPlantCoordinator))
            plant = retained(owner)
            await eventually(lambda: "local" in owner.connections._contexts)
            publish_state(owner, readings(), ITEM)
            await eventually(lambda: plant.faults.active)
            calls = len(backend.calls)
            backend.status[GATEWAY] = 500  # the cloud's first refresh fails
            management = owner.connections.management
            management.set_enabled(management.edit(cloud), True)
            await eventually(
                lambda: any(
                    call[0] == "bulk" and call[1] == GATEWAY
                    for call in backend.calls[calls:]
                )
            )
            await eventually(lambda: not plant.faults._handed_over)
            assert retained(owner) is plant and plant.local_retained
            management.set_enabled(management.edit(cloud), False)
            await settle(hass)
            assert retained(owner) is plant  # still the same view
            publish_state(owner, readings(), ITEM, second)
            await eventually(lambda: len(plant.faults.active) == 2)
            expected = {item.fingerprint for item in plant.faults.active}
        stored = FaultTracker(hass, entry.entry_id, GATEWAY)
        await stored.async_load()
    assert {item.fingerprint for item in stored.active} == expected


async def test_the_returning_cloud_continues_the_local_fault_baseline(
    hass,
) -> None:
    from tests.test_local_faults import ITEM

    faults_module = importlib.import_module(
        "custom_components.bosch_buderus_heating.faults"
    )
    backend = ReadingBackend()
    await first_run(hass, backend)
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, SENSOR)
    with (
        patch.object(INVENTORY, "MAX_AGE", timedelta(0)),
        # Only an explicit save reaches the store during this test.
        patch.object(faults_module, "FAULT_STORAGE_SAVE_DELAY", 3600),
    ):
        async with running(hass, backend, configure=cloud_off()) as (
            _,
            cloud,
            owner,
            _,
        ):
            await owner.start_platforms()
            await eventually(lambda: isinstance(retained(owner), LocalPlantCoordinator))
            plant = retained(owner)
            await eventually(lambda: "local" in owner.connections._contexts)
            publish_state(owner, readings(), ITEM)
            await eventually(lambda: plant.faults.active)
            fingerprint = plant.faults.active[0].fingerprint
            await shown(hass, owner, readings())  # no new list: nothing changes
            management = owner.connections.management
            management.set_enabled(management.edit(cloud), True)
            await eventually(lambda: source_for(owner, GATEWAY) is not None)
            current = source_for(owner, GATEWAY)
            assert current is not plant
            # The cloud loaded the baseline the local plant view kept.
            assert fingerprint in {item.fingerprint for item in current.faults.active}
            assert plant.faults._handed_over
            await eventually(lambda: not plant.local_retained)
            assert not owner.connections._retained
            await eventually(lambda: native(owner, "sensor", SENSOR) is not None)
            assert native(owner, "sensor", SENSOR).entity_id == entity_id


@pytest.mark.parametrize("failure", ["sign_in", "outage", "not_listed"])
async def test_a_failing_cloud_after_30_days_starts_the_local_plant_view(
    hass, failure
) -> None:
    """2A: without a usable inventory a cloud that needs a new sign-in or does
    not answer shows the local readings; the cloud takes over when it answers.
    """
    from unittest.mock import AsyncMock

    from custom_components.bosch_buderus_heating.connection_runtime import (
        ConnectionPhase,
    )
    from custom_components.bosch_buderus_heating.pointt.exceptions import (
        RefreshTokenRejected,
    )
    from tests.test_local_backed import local_values

    backend = ReadingBackend()
    await first_run(hass, backend)
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, SENSOR)
    calls = len(backend.calls)
    if failure == "not_listed":
        # START-4: the list answers, but without this gateway.
        backend.gateways = ["gateway-b"]
    else:
        backend.status["gateways"] = 401 if failure == "sign_in" else 503
    refresh = AsyncMock(side_effect=RefreshTokenRejected())
    with (
        patch.object(INVENTORY, "MAX_AGE", timedelta(0)),
        patch(
            "custom_components.bosch_buderus_heating.pointt.auth.OAuthClient.refresh",
            refresh,
        ),
    ):
        async with running(hass, backend, configure=local_values()) as (
            _,
            cloud,
            owner,
            harness,
        ):
            await owner.start_platforms()
            await eventually(lambda: isinstance(retained(owner), LocalPlantCoordinator))
            plant = retained(owner)
            await shown(hass, owner, readings())
            assert native(owner, "sensor", SENSOR).entity_id == entity_id
            await eventually(lambda: native(owner, "number", NUMBER) is None)
            runtime = owner.connections
            if failure == "sign_in":
                # The sign-in stays required (repair), nothing else is tried.
                assert runtime.connection_phase(cloud) is ConnectionPhase.AUTH_REQUIRED
                assert cloud in runtime.pending_reauthentication
                return
            # A retry that fails again keeps the same view.
            await eventually(lambda: not harness.sleeps.empty())
            # The cloud answers again: its first publication takes over.
            backend.status.pop("gateways", None)
            backend.gateways = [GATEWAY, "gateway-b"]
            # The failed list is kept for a minute: the next one is asked now.
            runtime.connection_driver(cloud)._gateways = None
            harness.retry.set()
            await eventually(lambda: source_for(owner, GATEWAY) is not None)
            await eventually(lambda: not plant.local_retained)
            assert not runtime._retained
            await eventually(lambda: native(owner, "sensor", SENSOR) is not None)
            assert native(owner, "sensor", SENSOR).entity_id == entity_id
    assert len(backend.calls) > calls


async def test_within_30_days_a_failing_cloud_keeps_the_inventory_view(
    hass,
) -> None:
    """2A: the first 30 days stay as before (P40 inventory start)."""
    from unittest.mock import AsyncMock

    from custom_components.bosch_buderus_heating.pointt.exceptions import (
        RefreshTokenRejected,
    )
    from tests.test_local_backed import local_values

    backend = ReadingBackend()
    await first_run(hass, backend)
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, SENSOR)
    backend.status["gateways"] = 401
    with patch(
        "custom_components.bosch_buderus_heating.pointt.auth.OAuthClient.refresh",
        AsyncMock(side_effect=RefreshTokenRejected()),
    ):
        async with running(hass, backend, configure=local_values()) as (_, _, owner, _):
            await owner.start_platforms()
            await eventually(lambda: retained(owner) is not None)
            view = retained(owner)
            assert not isinstance(view, LocalPlantCoordinator)
            assert view.startup_mode == "inventory"
            await eventually(lambda: view._inventory_expiry is not None)
            await shown(hass, owner, readings())
            calls = len(backend.calls)
            # After its 30 days, while running: the local plant view.
            due = view.inventory.startup_expiry(GATEWAY)
            with patch.object(INVENTORY, "MAX_AGE", timedelta(0)):
                async_fire_time_changed(hass, due + timedelta(days=1))
                await eventually(
                    lambda: isinstance(retained(owner), LocalPlantCoordinator)
                )
                await shown(hass, owner, readings(current=24.5))
                assert native(owner, "sensor", SENSOR).entity_id == entity_id
                await settle(hass)
            assert len(backend.calls) == calls  # no request, no sign-in


# ------------------------------------------- 5A: dormant view and faults


def _faults_patch():
    faults_module = importlib.import_module(
        "custom_components.bosch_buderus_heating.faults"
    )
    # Only an explicit save reaches the store during these tests.
    return patch.object(faults_module, "FAULT_STORAGE_SAVE_DELAY", 3600)


async def test_the_dormant_view_uses_the_local_error_list(hass) -> None:
    from tests.test_local_faults import ITEM

    backend = ReadingBackend()
    await first_run(hass, backend)
    with _faults_patch():
        async with running(hass, backend, configure=cloud_off()) as (_, _, owner, _):
            await owner.start_platforms()
            await eventually(lambda: retained(owner) is not None)
            dormant = retained(owner)
            assert not isinstance(dormant, LocalPlantCoordinator)
            assert dormant.faults.local_authority
            faults_id, problem_id = (
                f"{GATEWAY}:active_faults",
                f"{GATEWAY}:system_fault",
            )
            await eventually(lambda: native(owner, "sensor", faults_id) is not None)
            # Without a current list the state is not known.
            assert entity_state(hass, owner, "sensor", faults_id) == "unavailable"
            await eventually(lambda: "local" in owner.connections._contexts)
            publish_state(owner, readings(), ITEM)
            await eventually(lambda: dormant.faults.active)
            fingerprint = dormant.faults.active[0].fingerprint
            await eventually(
                lambda: entity_state(hass, owner, "sensor", faults_id) == "1"
            )
            assert entity_state(hass, owner, "binary_sensor", problem_id) == "on"
            # Known faults, but no current list: no state without a cloud.
            owner.connections._local_states.pop("local")
            owner.connections._notify()
            await eventually(
                lambda: entity_state(hass, owner, "sensor", faults_id) == "unavailable"
            )
            assert dormant.faults.active  # kept, only not confirmed
            publish_state(owner, readings(), ITEM)
            await eventually(
                lambda: entity_state(hass, owner, "sensor", faults_id) == "1"
            )
            # After 30 days the local plant view continues the same baseline.
            due = dormant.inventory.startup_expiry(GATEWAY)
            with patch.object(INVENTORY, "MAX_AGE", timedelta(0)):
                async_fire_time_changed(hass, due + timedelta(days=1))
                await eventually(
                    lambda: isinstance(retained(owner), LocalPlantCoordinator)
                )
                plant = retained(owner)
                assert fingerprint in {item.fingerprint for item in plant.faults.active}


async def test_the_returning_cloud_continues_the_dormant_fault_baseline(
    hass,
) -> None:
    from tests.test_local_faults import ITEM

    backend = ReadingBackend()
    await first_run(hass, backend)
    with _faults_patch():
        async with running(hass, backend, configure=cloud_off()) as (
            _,
            cloud,
            owner,
            _,
        ):
            await owner.start_platforms()
            await eventually(lambda: retained(owner) is not None)
            dormant = retained(owner)
            await eventually(lambda: "local" in owner.connections._contexts)
            publish_state(owner, readings(), ITEM)
            await eventually(lambda: dormant.faults.active)
            fingerprint = dormant.faults.active[0].fingerprint
            await shown(hass, owner, readings())  # no new list
            management = owner.connections.management
            management.set_enabled(management.edit(cloud), True)
            await eventually(lambda: source_for(owner, GATEWAY) is not None)
            current = source_for(owner, GATEWAY)
            assert fingerprint in {item.fingerprint for item in current.faults.active}
            assert dormant.faults._handed_over


# ------------------------------------------------------ review fixes (2A)


async def test_a_cloud_only_outage_builds_and_writes_nothing(hass) -> None:
    from unittest.mock import AsyncMock, Mock

    from custom_components.bosch_buderus_heating.faults import FaultTracker
    from tests.test_local_backed import local_values

    backend = ReadingBackend()
    await first_run(hass, backend)
    calls = len(backend.calls)
    backend.status["gateways"] = 503
    built, flush = Mock(), AsyncMock()
    with (
        patch.object(INVENTORY, "MAX_AGE", timedelta(0)),
        patch.object(LocalPlantCoordinator, "refresh_view", built),
        patch.object(FaultTracker, "async_flush", flush),
    ):
        configure = local_values(local_access=False)
        async with running(hass, backend, configure=configure) as (
            _,
            cloud,
            owner,
            harness,
        ):
            await owner.start_platforms()
            await eventually(lambda: not harness.sleeps.empty())
            harness.retry.set()  # retries go on, each without a view
            runtime = owner.connections
            await eventually(lambda: len(backend.calls) > calls + 1)
            runtime.connection_driver(cloud)._gateways = None
            await eventually(lambda: len(backend.calls) > calls + 2)
            await settle(hass)
            assert not runtime._retained
            built.assert_not_called()  # no view is built
            flush.assert_not_awaited()  # and no fault baseline written


async def test_an_expired_view_that_cannot_switch_now_tries_again(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    async with running(hass, backend, configure=cloud_off()) as (_, _, owner, _):
        await owner.start_platforms()
        await eventually(lambda: retained(owner) is not None)
        dormant = retained(owner)
        due = dormant.inventory.startup_expiry(GATEWAY)
        runtime = owner.connections
        with (
            patch.object(INVENTORY, "MAX_AGE", timedelta(0)),
            patch.object(runtime, "expire_dormant", return_value=False) as expire,
        ):
            async_fire_time_changed(hass, due + timedelta(days=1))
            await eventually(lambda: expire.called)
            await settle(hass)
        assert retained(owner) is dormant
        assert dormant._inventory_expiry is not None  # checked again later


async def test_a_live_view_kept_for_a_sign_in_gets_no_expiry(hass) -> None:
    from custom_components.bosch_buderus_heating.connection_runtime import (
        ConnectionPhase,
    )
    from tests.test_local_backed import local_values, started

    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, cloud, owner, _):
        await started(hass, owner)
        coordinator = source_for(owner, GATEWAY)
        coordinator._request_reauthentication()
        await eventually(
            lambda: (
                owner.connections.connection_phase(cloud)
                is ConnectionPhase.AUTH_REQUIRED
            )
        )
        await eventually(lambda: coordinator.local_retained)
        await settle(hass)
        assert not coordinator.inventory_view
        assert coordinator._inventory_expiry is None


async def test_deselecting_a_system_forgets_its_descriptions(hass) -> None:
    from tests.test_local_backed import local_values, started

    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, cloud, owner, _):
        await started(hass, owner)
        inventory = source_for(owner, GATEWAY).inventory
        assert inventory.templates(GATEWAY)
        management = owner.connections.management
        management.set_binding_enabled(
            management.edit(cloud), binding_for(owner, GATEWAY), False
        )
        await eventually(lambda: not inventory.templates(GATEWAY))
        # The resources keep their own age limit (P40).
        assert inventory.resources(GATEWAY)
