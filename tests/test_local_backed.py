"""P40: a stopped cloud does not take the locally supplied entities with it.

These tests run the managed entry with its real platforms, the real PointT
HTTP client against a synthetic cloud and published local readings. A cloud
that needs a new sign-in, is switched off or is blocked must not stop local
readings from reaching Home Assistant states.
"""

import asyncio
import itertools
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bosch_buderus_heating.connection_runtime import ConnectionPhase
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.gateway_identity import GatewayNamespace
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    RefreshTokenRejected,
)
from tests.test_connection_runtime import settle
from tests.test_local_selection import local
from tests.test_managed_platforms import NUMBER, native
from tests.test_managed_pointt import Backend, eventually, running, source_for
from tests.test_source_isolation import paired_target

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    # These transport fixtures start after the integration's normal setup phase.
    hass.config.components.add(DOMAIN)


GATEWAY = "000123"
CURRENT = "/heatingCircuits/hc2/currentRoomSetpoint"
SENSOR = GATEWAY + ":heatingCircuits:hc2:currentRoomSetpoint"


class ReadingBackend(Backend):
    """Adds a read-only reading that a local access can supply."""

    def resource(self, gateway, path):
        if path == "/heatingCircuits/hc2":
            return {
                "references": [
                    {"id": "/heatingCircuits/hc2/manualRoomSetpoint"},
                    {"id": CURRENT},
                ]
            }
        if path == CURRENT:
            return {
                "id": CURRENT,
                "type": "floatValue",
                "value": self.values[gateway],
                "unitOfMeasure": "C",
                "writeable": False,
            }
        return super().resource(gateway, path)


def local_values(*, local_access=True):
    def configure(data, options):
        target, _ = paired_target(data, options)
        options["targets"][target]["local_values"] = True
        options["bindings"]["local"]["enabled"] = local_access

    return configure


_READ_TIMES = itertools.count(10.0)


def publish_local(owner, value):
    """A current local reading of the paired path, as the local source does."""
    runtime = owner.connections
    context = runtime._contexts["local"]
    runtime._local_states["local"] = SimpleNamespace(
        context=context.identity_context(GatewayNamespace.K40_LOCAL),
        current_observations=(local(CURRENT, value, monotonic=next(_READ_TIMES)),),
    )
    runtime._notify()


def state(hass, owner):
    entity = native(owner, "sensor", SENSOR)
    return hass.states.get(entity.entity_id).state if entity else None


async def started(hass, owner):
    await owner.start_platforms()
    await eventually(lambda: native(owner, "sensor", SENSOR))
    publish_local(owner, 23.5)
    await eventually(lambda: state(hass, owner) == "23.5")
    return native(owner, "sensor", SENSOR)


async def test_a_blocked_cloud_poll_does_not_delay_local_updates(hass) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, _, owner, _):
        await started(hass, owner)
        coordinator = source_for(owner, GATEWAY)
        backend.gates[GATEWAY] = asyncio.Event()
        backend.entered.pop(GATEWAY, None)
        coordinator._next_update = dict.fromkeys(coordinator._poll_intervals, 0)
        refresh = asyncio.create_task(coordinator.async_refresh())
        await eventually(lambda: GATEWAY in backend.entered)
        for value in (24.0, 24.5, 25.0):  # several readings while blocked
            publish_local(owner, value)
            await eventually(lambda value=value: state(hass, owner) == str(value))
        assert not refresh.done()
        backend.gates[GATEWAY].set()
        await refresh


async def test_sign_in_required_keeps_local_values(hass) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (
        _,
        cloud,
        owner,
        _,
    ):
        sensor = await started(hass, owner)
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
        # The same entity, still showing the local value.
        assert native(owner, "sensor", SENSOR) is sensor
        assert state(hass, owner) == "23.5"
        publish_local(owner, 26.0)
        await eventually(lambda: state(hass, owner) == "26.0")
        # Controls need the cloud: unavailable, and nothing is requested.
        number = native(owner, "number", NUMBER)
        assert number is None or not number.available
        calls = len(backend.calls)
        coordinator._next_update = dict.fromkeys(coordinator._poll_intervals, 0)
        await coordinator.async_refresh()
        assert len(backend.calls) == calls
        assert coordinator.control_status()[0] in {"auth_required", "cloud_stopped"}


async def test_switched_off_cloud_keeps_local_values_and_returns(hass) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (
        _,
        cloud,
        owner,
        _,
    ):
        sensor = await started(hass, owner)
        old = source_for(owner, GATEWAY)
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), False)
        await eventually(lambda: old.local_retained)
        await settle(hass)
        assert native(owner, "sensor", SENSOR) is sensor
        assert state(hass, owner) == "23.5"
        publish_local(owner, 21.0)
        await eventually(lambda: state(hass, owner) == "21.0")

        # Switched on again: the cloud takes over, the retained view ends.
        management.set_enabled(management.edit(cloud), True)
        await eventually(
            lambda: (item := source_for(owner, GATEWAY)) is not None and item is not old
        )
        await eventually(lambda: not old.local_retained)
        await eventually(lambda: native(owner, "sensor", SENSOR) is not None)
        await eventually(lambda: state(hass, owner) == "21.0")  # still local first
        assert not owner.connections._retained


async def test_without_a_local_access_the_entities_stop_as_before(hass) -> None:
    backend = ReadingBackend()
    configure = local_values(local_access=False)
    async with running(hass, backend, configure=configure) as (_, cloud, owner, _):
        await owner.start_platforms()
        await eventually(lambda: native(owner, "sensor", SENSOR))
        entity_id = native(owner, "sensor", SENSOR).entity_id
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), False)
        await settle(hass)
        await eventually(lambda: hass.states.get(entity_id).state == "unavailable")
        assert not owner.connections._retained


async def test_removed_cloud_access_keeps_the_local_readings(hass) -> None:
    """R1 (P49b): the retained view ends, the local plant view takes over."""
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (
        _,
        cloud,
        owner,
        _,
    ):
        sensor = await started(hass, owner)
        old = source_for(owner, GATEWAY)
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), False)
        await eventually(lambda: old.local_retained)
        management.remove(management.edit(cloud))
        await settle(hass)
        await eventually(lambda: not old.local_retained)
        assert not owner.connections._retained
        await eventually(lambda: owner.connections.target_views())
        await eventually(lambda: "local" in owner.connections._contexts)
        publish_local(owner, 21.5)  # the local source reads on
        await eventually(lambda: native(owner, "sensor", SENSOR) is not None)
        assert native(owner, "sensor", SENSOR).entity_id == sensor.entity_id
        await eventually(lambda: state(hass, owner) == "21.5")


async def test_sign_in_required_at_start_uses_the_inventory(hass) -> None:
    backend = ReadingBackend()
    configure = local_values()
    async with running(hass, backend, configure=configure) as (
        _,
        cloud,
        owner,
        _,
    ):
        await started(hass, owner)
        first = source_for(owner, GATEWAY)
        assert first.inventory.resources(GATEWAY)
    # Restart with a rejected sign-in: the gateway list cannot be read.
    backend.status["gateways"] = 401
    with patch(
        "custom_components.bosch_buderus_heating.pointt.auth.OAuthClient.refresh",
        AsyncMock(side_effect=RefreshTokenRejected()),
    ):
        async with running(hass, backend, configure=configure) as (
            _,
            cloud,
            owner,
            _,
        ):
            await owner.start_platforms()
            await eventually(
                lambda: (
                    owner.connections.connection_phase(cloud)
                    is ConnectionPhase.AUTH_REQUIRED
                )
            )
            await eventually(lambda: native(owner, "sensor", SENSOR))
            publish_local(owner, 22.5)
            await eventually(lambda: state(hass, owner) == "22.5")
            retained = next(iter(owner.connections._retained.values()))[0]
            assert retained.startup_mode == "inventory"
            assert retained.local_retained
            assert deepcopy(retained.update_interval) is None


# ------------------------------------------------------------- unit level


def _paired(hass, path, *, cloud_value=40.0, local_value=45.5):
    from custom_components.bosch_buderus_heating.local_selection import (
        LocalSelection,
    )
    from custom_components.bosch_buderus_heating.pointt import (
        Resource,
        ResourceMetadata,
    )
    from tests.test_local_selection import TARGET, FakeRuntime
    from tests.test_validity import _coordinator

    resource = Resource(
        path,
        value=cloud_value,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )
    coordinator = _coordinator(hass, resource)
    coordinator.local_selection = LocalSelection(
        FakeRuntime([local(path, local_value, monotonic=20.0)]), TARGET
    )
    return coordinator


async def test_a_retained_view_shows_local_values_of_a_stopped_source(hass) -> None:
    from dataclasses import replace

    from custom_components.bosch_buderus_heating.coordinator import (
        SOURCE_STOPPED_CATEGORY,
        Freshness,
    )

    coordinator = _paired(hass, CURRENT)
    coordinator.data[CURRENT] = replace(
        coordinator.data[CURRENT],
        available=False,
        freshness=Freshness.STALE,
        last_error_category=SOURCE_STOPPED_CATEGORY,
        ended=True,
    )
    assert not coordinator.display_snapshot(CURRENT).available
    coordinator.local_retained = True
    shown = coordinator.display_snapshot(CURRENT)
    assert shown.available and shown.resource.value == 45.5
    await coordinator.async_shutdown()


async def test_updating_a_local_only_sensor_starts_no_cloud_poll(hass) -> None:
    """5A: local readings arrive by themselves."""
    from custom_components.bosch_buderus_heating.local_only import definition_for
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        BoschBuderusLocalOnlySensor,
    )

    path = "/heatSources/currentSupplySetpoint"
    coordinator = _paired(hass, CURRENT)
    sensor = BoschBuderusLocalOnlySensor(coordinator, path, definition_for(path))
    with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
        await sensor.async_update()
    refresh.assert_not_called()
    await coordinator.async_shutdown()


# ----------------------------------------------------- cases from the review


def _released(owner, coordinator) -> bool:
    return (
        not coordinator.local_retained
        and coordinator._remove_local_listener is None
        and coordinator._local_updated not in owner.connections._listeners
    )


async def test_release_removes_the_local_listener(hass) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (
        _,
        cloud,
        owner,
        _,
    ):
        await started(hass, owner)
        old = source_for(owner, GATEWAY)
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), False)
        await eventually(lambda: old.local_retained)
        management.set_enabled(management.edit(cloud), True)
        await eventually(lambda: _released(owner, old))


async def test_a_rejected_configuration_ends_the_retained_view(hass) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (
        _,
        cloud,
        owner,
        _,
    ):
        await started(hass, owner)
        old = source_for(owner, GATEWAY)
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), False)
        await eventually(lambda: old.local_retained)
        owner.connections._reject_configuration()
        await eventually(lambda: _released(owner, old))
        assert (
            owner.connections.display_data(
                next(iter(owner.connections.snapshot.bindings))
            )
            is None
            or not owner.connections._retained
        )


async def test_a_deselected_system_ends_the_retained_view(hass) -> None:
    from tests.test_managed_pointt import binding_for

    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (
        _,
        cloud,
        owner,
        _,
    ):
        await started(hass, owner)
        old = source_for(owner, GATEWAY)
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), False)
        await eventually(lambda: old.local_retained)
        management.set_binding_enabled(
            management.edit(cloud), binding_for(owner, GATEWAY), False
        )
        await eventually(lambda: _released(owner, old))
        await settle(hass)
        assert native(owner, "sensor", SENSOR) is None


async def test_switching_the_local_access_off_and_on_reattaches(hass) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (
        _,
        cloud,
        owner,
        _,
    ):
        await started(hass, owner)
        old = source_for(owner, GATEWAY)
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), False)
        await eventually(lambda: old.local_retained)
        entity_id = native(owner, "sensor", SENSOR).entity_id
        management.set_binding_enabled(management.edit("local"), "local", False)
        await settle(hass)
        await eventually(lambda: hass.states.get(entity_id).state == "unavailable")
        management.set_binding_enabled(management.edit("local"), "local", True)
        await eventually(lambda: "local" in owner.connections._contexts)
        await eventually(lambda: native(owner, "sensor", SENSOR) is not None)
        publish_local(owner, 27.0)
        await eventually(lambda: state(hass, owner) == "27.0")
        assert next(iter(owner.connections._retained.values()))[0] is old


def paired_with_second_gateway(data, options):
    """The local access belongs to the second gateway of the account."""
    cloud_binding = next(
        key
        for key, binding in data["bindings"].items()
        if binding["adapter"] == "pointt" and binding["locator"]["value"] == "gateway-b"
    )
    target = data["bindings"][cloud_binding]["target_id"]
    previous = data["bindings"]["local"]["target_id"]
    if target != previous:
        identities = data["targets"][previous]["identities"]
        identity = next(
            value
            for value in identities
            if value["namespace"] == "local_system_gateway"
        )
        identities.remove(identity)
        data["targets"][target]["identities"].append(identity)
        options["targets"][previous]["read_order"].remove("local")
        options["targets"][target]["read_order"].insert(0, "local")
        data["bindings"]["local"]["target_id"] = target
    options["targets"][target]["local_values"] = True


async def test_sign_in_at_start_retains_every_gateway_with_a_local_access(
    hass,
) -> None:
    """Shared sign-in: the second gateway is retained whichever source fails first."""
    second = "gateway-b:heatingCircuits:hc2:currentRoomSetpoint"
    backend = ReadingBackend()
    configure = paired_with_second_gateway
    async with running(hass, backend, configure=configure) as (_, _, owner, _):
        await owner.start_platforms()
        await eventually(lambda: native(owner, "sensor", second))
        assert source_for(owner, "gateway-b").inventory.resources("gateway-b")
    backend.status["gateways"] = 401
    with patch(
        "custom_components.bosch_buderus_heating.pointt.auth.OAuthClient.refresh",
        AsyncMock(side_effect=RefreshTokenRejected()),
    ):
        async with running(hass, backend, configure=configure) as (
            _,
            cloud,
            owner,
            _,
        ):
            await owner.start_platforms()
            await eventually(
                lambda: (
                    owner.connections.connection_phase(cloud)
                    is ConnectionPhase.AUTH_REQUIRED
                )
            )
            await eventually(lambda: native(owner, "sensor", second))
            retained = [item[0] for item in owner.connections._retained.values()]
            assert [item.gateway.gateway_id for item in retained] == ["gateway-b"]
            calls = len(backend.calls)
            await retained[0].async_request_refresh()
            await settle(hass)
            assert len(backend.calls) == calls  # no cloud request while retained


async def test_a_retained_live_view_is_not_replaced_by_an_inventory_start(
    hass,
) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (
        _,
        cloud,
        owner,
        _,
    ):
        await started(hass, owner)
        old = source_for(owner, GATEWAY)
        old._request_reauthentication()  # the live view is retained
        await eventually(lambda: old.local_retained)
        from custom_components.bosch_buderus_heating.connection_runtime import (
            SourceContext,
        )

        runtime = owner.connections
        ((binding, _),) = runtime._retained.items()
        # A later start attempt with a current generation fails sign-in again.
        context = SourceContext(runtime, runtime._generations.capture(cloud, binding))
        poorer = SimpleNamespace(retain_for_local=lambda: True)
        assert not runtime.retain_startup(context, poorer)
        assert owner.connections._retained[binding][0] is old
