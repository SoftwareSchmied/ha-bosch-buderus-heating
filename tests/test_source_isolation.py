"""Source selection must not retire an unchanged cloud or local reader."""

import asyncio
from copy import deepcopy

import pytest
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import managed_local
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.managed_cloud import CloudDriverFactory
from custom_components.bosch_buderus_heating.pointt import PointTClient
from tests import test_managed_local as local_scenarios
from tests.test_connection_runtime import settle
from tests.test_managed_platforms import NUMBER, available, native
from tests.test_managed_pointt import TEMP, Backend, eventually, running, source_for
from tests.test_pointt_transport_and_client import serve

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
prepared = local_scenarios.prepared


def paired_target(data, options):
    """Match explicit gateway identity rather than dictionary insertion order."""
    cloud_binding = next(
        key
        for key, binding in data["bindings"].items()
        if binding["adapter"] == "pointt" and binding["locator"]["value"] == "000123"
    )
    target = data["bindings"][cloud_binding]["target_id"]
    previous = data["bindings"]["local"]["target_id"]
    if target != previous:
        identities = data["targets"][previous]["identities"]
        local_identity = next(
            value
            for value in identities
            if value["namespace"] == "local_system_gateway"
        )
        identities.remove(local_identity)
        data["targets"][target]["identities"].append(local_identity)
        options["targets"][previous]["read_order"].remove("local")
        options["targets"][target]["read_order"].insert(0, "local")
        data["bindings"]["local"]["target_id"] = target
    return target, cloud_binding


@pytest.fixture
async def combined(hass, monkeypatch, prepared):
    """Use real source drivers, the local scheduler and loopback PointT HTTP."""
    p = prepared
    data, options = deepcopy(dict(p.entry.data)), deepcopy(dict(p.entry.options))
    p.target, p.binding = paired_target(data, options)
    data["connections"][p.cloud]["credentials"]["expires_at"] = 10**12
    for key, binding in data["bindings"].items():
        if binding["adapter"] == "bacon":
            options["bindings"][key]["enabled"] = False
    hass.config_entries.async_update_entry(p.entry, data=data, options=options)
    p.runtime._factories["singlekey_cloud"] = CloudDriverFactory(
        hass, p.entry, lambda: p.runtime
    )
    p.backend = Backend()
    async with serve(p.backend.routes) as url:
        for module in ("managed_cloud", "managed_pointt"):
            monkeypatch.setattr(
                "custom_components.bosch_buderus_heating." + module + ".PointTClient",
                lambda *args, **kwargs: PointTClient(*args, **kwargs, base_url=url),
            )
        p.runtime.start()
        try:
            await eventually(
                lambda: (
                    p.runtime.source_data(p.binding) is not None
                    and p.runtime.local_state("local") is not None
                    and p.runtime.local_state("local").current_observations
                )
            )
            yield p
        finally:
            for gate in p.backend.gates.values():
                gate.set()
            for gate in (p.control.gate, p.control.hold):
                if gate:
                    gate.set()
            await p.runtime.close()


@pytest.mark.parametrize("change", ["remove_cloud", "remove_local", "read_order"])
async def test_selection_change_preserves_healthy_pending_reads_and_publication(
    hass, combined, change
):
    p = combined
    local_connection = p.runtime.connection_driver("local")
    local = local_connection.sources["local"]
    cloud_connection = p.runtime.connection_driver(p.cloud)
    cloud = p.runtime.source_data(p.binding)
    old_local = p.runtime.local_state("local")
    original_observation = old_local.current_observations[0]
    local_notifications, cloud_notifications, configuration_notifications = [], [], []

    @callback
    def observe():
        state = p.runtime.local_state("local")
        if state and state.current_observations:
            local_notifications.append(state.current_observations[0])
        configuration_notifications.append(
            p.runtime.snapshot.to_options()["targets"][p.target]["read_order"]
        )

    remove = p.runtime.subscribe(observe)
    remove_cloud = cloud.async_add_listener(
        lambda: cloud_notifications.append(cloud.data[TEMP].resource.value)
    )
    p.control.hold = asyncio.Event()
    p.clock.advance(60)
    local.service.wake()
    await eventually(lambda: len(p.clients[0].calls) == 2)
    p.backend.gates["000123"] = asyncio.Event()
    p.backend.entered.pop("000123", None)
    p.backend.values["000123"] = 22.0
    cloud._next_update = dict.fromkeys(cloud._poll_intervals, 0)
    refresh = asyncio.create_task(cloud.async_refresh())
    try:
        await eventually(lambda: "000123" in p.backend.entered)
        manager = p.runtime.management
        if change == "read_order":
            options = deepcopy(dict(p.entry.options))
            options["targets"][p.target]["read_order"].reverse()
            hass.config_entries.async_update_entry(p.entry, options=options)
        else:
            connection = p.cloud if change == "remove_cloud" else "local"
            manager.remove(manager.edit(connection))
        await settle(hass)
        assert (
            configuration_notifications[-1]
            == p.entry.options["targets"][p.target]["read_order"]
        )
        assert p.target in p.entry.data["targets"]
        if change != "remove_local":
            assert p.runtime.connection_driver("local") is local_connection
            assert local_connection.sources["local"] is local
            assert local.context.configuration_current
            assert p.runtime.local_state("local").current_observations == (
                original_observation,
            )
            assert not p.clients[0].closed and len(p.probes) == 1
        else:
            assert p.clients[0].closed
            assert not local.context.publish_local(old_local)
            assert p.runtime.local_state("local") is None
        if change != "remove_cloud":
            assert p.runtime.connection_driver(p.cloud) is cloud_connection
            assert p.runtime.source_data(p.binding) is cloud
            assert cloud.source.current() and not refresh.done()
        else:
            assert not cloud.source.context.publish(cloud)
            assert p.runtime.source_data(p.binding) is None
        p.control.hold.set()
        p.backend.gates["000123"].set()
        await asyncio.gather(refresh, return_exceptions=True)
        if change != "remove_local":
            await eventually(
                lambda: (
                    local_notifications
                    and local_notifications[-1] is not original_observation
                )
            )
            assert p.runtime.connection_driver("local").sources["local"] is local
            assert len(p.clients) == 1 and len(p.probes) == 1
        if change != "remove_cloud":
            assert not refresh.cancelled() and refresh.exception() is None
            assert cloud.last_update_success and cloud_notifications[-1] == 22
            assert p.runtime.source_data(p.binding) is cloud
    finally:
        p.control.hold.set()
        p.backend.gates["000123"].set()
        if not refresh.done():
            refresh.cancel()
        await asyncio.gather(refresh, return_exceptions=True)
        remove()
        remove_cloud()


@pytest.mark.parametrize(
    "change", ["identity", "local_address", "local_token", "cloud_login"]
)
async def test_real_context_changes_reject_old_publications(
    hass, monkeypatch, combined, change
):
    p = combined
    local = p.runtime.connection_driver("local").sources["local"]
    old_state = p.runtime.local_state("local")
    cloud = p.runtime.source_data(p.binding)
    p.control.gate = asyncio.Event()
    p.backend.gates["000123"] = asyncio.Event()
    data = deepcopy(dict(p.entry.data))
    if change == "identity":
        data["targets"][p.target]["identities"][0]["evidence"] = "reverified_identity"
    elif change == "local_address":
        data["connections"]["local"]["endpoint"]["host"] = "replacement.example.invalid"
    elif change == "local_token":
        data["connections"]["local"]["credentials"]["token"] = (
            "synthetic-replacement-token"
        )
        original_probe = managed_local.read_local_probe

        async def replacement_probe(context, token, pin, **kwargs):
            assert token == "synthetic-replacement-token"
            return await original_probe(context, "synthetic-local-token", pin, **kwargs)

        monkeypatch.setattr(managed_local, "read_local_probe", replacement_probe)
    else:
        data["connections"][p.cloud]["auth_epoch"] += 1
    hass.config_entries.async_update_entry(p.entry, data=data)
    # Publication itself must observe a changed entry before its HA listener runs.
    if change != "cloud_login":
        assert not local.context.publish_local(old_state)
    else:
        assert local.context.publish_local(old_state)
    if change in ("identity", "cloud_login"):
        assert not cloud.source.context.publish(cloud)
    else:
        assert cloud.source.context.publish(cloud)
    await settle(hass)
    if change != "cloud_login":
        await eventually(lambda: len(p.probes) == 2)
        assert p.runtime.local_state("local").current_observations == ()
        assert not local.context.configuration_current
    if change in ("identity", "cloud_login"):
        assert p.runtime.source_data(p.binding) is None
        assert not cloud.source.current()


@pytest.mark.parametrize("change", ["remove_local", "read_order"])
async def test_native_cloud_entity_keeps_instance_and_availability(hass, change):
    hass.config.components.add(DOMAIN)
    backend = Backend()
    async with running(hass, backend, configure=paired_target) as (entry, _, owner, _):
        await owner.start_platforms()
        await eventually(lambda: available(owner, "number", NUMBER))
        original = native(owner, "number", NUMBER)
        coordinator = source_for(owner, "000123")
        registry = er.async_get(hass)
        registry.async_update_entity(original.entity_id, name="Living room")
        states = []
        remove = hass.bus.async_listen(
            "state_changed",
            lambda event: (
                states.append(event.data["new_state"])
                if event.data["entity_id"] == original.entity_id
                else None
            ),
        )
        try:
            if change == "remove_local":
                manager = owner.connections.management
                manager.remove(manager.edit("local"))
            else:
                options = deepcopy(dict(entry.options))
                target = entry.data["bindings"]["local"]["target_id"]
                options["targets"][target]["read_order"].reverse()
                hass.config_entries.async_update_entry(entry, options=options)
            await settle(hass)
            assert native(owner, "number", NUMBER) is original and original.available
            assert source_for(owner, "000123") is coordinator
            backend.values["000123"] = 22
            coordinator._next_update = dict.fromkeys(coordinator._poll_intervals, 0)
            await coordinator.async_refresh()
            await settle(hass)
            assert hass.states.get(original.entity_id).state == "22.0"
            assert states and all(
                state and state.state != "unavailable" for state in states
            )
            assert registry.async_get(original.entity_id).name == "Living room"
        finally:
            remove()
