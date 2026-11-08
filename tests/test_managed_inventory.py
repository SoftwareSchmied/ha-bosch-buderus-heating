"""P47 in managed sources: a temporary cloud failure starts with the inventory.

A managed entry is always loaded; a PointT source that cannot start is
retried by the connection runtime and its entities stay "not provided" until
then. With a saved inventory the source now starts at once, its entities are
unavailable, and the values return with the first working cycle. These tests
use the real HTTP client against a synthetic cloud and restart one source.
"""

import pytest

from custom_components.bosch_buderus_heating.managed_pointt import (
    ManagedPointTCoordinator,
)
from tests.test_connection_runtime import settle
from tests.test_managed_pointt import (
    TEMP,
    Backend,
    binding_for,
    eventually,
    running,
    source_for,
)

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
GATEWAY = "000123"


OUTDOOR = "/system/sensors/temperatures/outdoor_t1"


class SensorBackend(Backend):
    """Adds a read-only sensor, shown through the display path (P12)."""

    def resource(self, gateway, path):
        if path == "/system":
            return {"references": [{"id": OUTDOOR}]}
        if path == OUTDOOR:
            return {
                "id": OUTDOOR,
                "type": "floatValue",
                "value": 7.5,
                "unitOfMeasure": "C",
                "writeable": False,
            }
        return super().resource(gateway, path)


async def _restart(hass, owner, cloud, previous):
    """Stop and start one source, forcing a fresh gateway list."""
    management = owner.connections.management
    binding = binding_for(owner, GATEWAY)
    management.set_binding_enabled(management.edit(cloud), binding, False)
    await settle(hass)
    previous.source.connection._gateways = None  # no cached list
    management.set_binding_enabled(management.edit(cloud), binding, True)
    await eventually(
        lambda: (
            (item := source_for(owner, GATEWAY)) is not None and item is not previous
        )
    )
    return source_for(owner, GATEWAY)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("gateways", "gateway_list_service_unavailable"),
        (GATEWAY, "discovery_service_unavailable"),
    ],
)
async def test_source_starts_with_the_inventory_during_a_cloud_failure(
    hass, failure, reason
):
    backend = Backend()
    async with running(hass, backend) as (_, cloud, owner, _):
        await eventually(lambda: source_for(owner, GATEWAY))
        first = source_for(owner, GATEWAY)
        assert isinstance(first, ManagedPointTCoordinator)
        assert first.inventory is not None
        assert TEMP in first.inventory.resources(GATEWAY)
        assert first.startup_mode == "live"

        backend.status[failure] = 503
        backend.status[GATEWAY] = 503  # the gateway itself does not answer either
        fresh = await _restart(hass, owner, cloud, first)
        assert isinstance(fresh, ManagedPointTCoordinator)
        report = fresh.startup_inventory_diagnostics(0)
        assert fresh.startup_mode == "inventory" and report["reason"] == reason
        assert not fresh.last_update_success
        assert not fresh.data[TEMP].available
        # Published at once (the source no longer waits for a retry), so the
        # managed platforms build its entities from the stored resources.
        assert owner.connections.source_data(binding_for(owner, GATEWAY)) is fresh
        assert fresh.stored_resources()

        # The cloud answers again: values with the first working cycle,
        # then the discovery in the next one.
        backend.status.clear()
        fresh._next_update = dict.fromkeys(fresh._poll_intervals, 0)
        await fresh.async_refresh()
        assert fresh.last_update_success
        assert fresh.data[TEMP].available and fresh.data[TEMP].resource.value == 20
        await fresh.async_refresh()
        report = fresh.startup_inventory_diagnostics(0)
        assert report["discovery_result"] == "success"
        assert not fresh.stored_resources()


async def test_permanent_gateway_list_answer_does_not_use_the_inventory(hass):
    backend = Backend()
    async with running(hass, backend) as (_, cloud, owner, _):
        await eventually(lambda: source_for(owner, GATEWAY))
        first = source_for(owner, GATEWAY)
        backend.status["gateways"] = 403
        management = owner.connections.management
        binding = binding_for(owner, GATEWAY)
        management.set_binding_enabled(management.edit(cloud), binding, False)
        await settle(hass)
        first.source.connection._gateways = None
        management.set_binding_enabled(management.edit(cloud), binding, True)
        await eventually(lambda: ("gateways",) in backend.calls[-3:])
        await settle(hass)
        # As before: no source is published; the runtime retries later.
        assert source_for(owner, GATEWAY) is None


async def test_source_without_inventory_waits_as_before(hass):
    backend = Backend()
    backend.status["gateways"] = 503
    async with running(hass, backend) as (_, _, owner, harness):
        await eventually(lambda: harness.sleeps.qsize() == 2)
        assert source_for(owner, GATEWAY) is None


async def test_failed_first_poll_after_an_inventory_start_shows_nothing(hass):
    """Stored values are never shown as last good values (P12)."""
    from custom_components.bosch_buderus_heating.pointt import Gateway

    backend = Backend()
    async with running(hass, backend) as (_, cloud, owner, _):
        await eventually(lambda: source_for(owner, GATEWAY))
        first = source_for(owner, GATEWAY)
        inventory = first.inventory
        inventory.remember_gateways((Gateway("no-longer-bound"),))
        backend.status["gateways"] = 503
        backend.status[GATEWAY] = 503
        fresh = await _restart(hass, owner, cloud, first)
        # P40: an unbound gateway stays until its age limit (re-adding works).
        assert inventory.gateway("no-longer-bound") is not None
        fresh._next_update = dict.fromkeys(fresh._poll_intervals, 0)
        await fresh.async_refresh()  # still no answer
        shown = fresh.display_snapshot(TEMP)
        assert shown is not None and not shown.available
        assert fresh.source_snapshot(TEMP, "cloud") is None or not (
            fresh.source_snapshot(TEMP, "cloud").available
        )


async def test_entities_from_the_inventory_stay_unavailable_until_an_answer(hass):
    """With HA platforms loaded: entities exist, never show a stored value."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.bosch_buderus_heating.const import DOMAIN
    from tests.test_managed_platforms import NUMBER, available, native

    hass.config.components.add(DOMAIN)
    backend = SensorBackend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        await owner.start_platforms()
        await eventually(lambda: available(owner, "number", NUMBER))
        first = source_for(owner, GATEWAY)

        def gateway_states():
            return {
                item.entity_id: getattr(hass.states.get(item.entity_id), "state", None)
                for item in er.async_entries_for_config_entry(
                    er.async_get(hass), entry.entry_id
                )
                if item.unique_id.startswith(GATEWAY + ":") and item.disabled_by is None
            }

        # Fault entities have no notifications in this backend; the number has a value.
        assert {"20.0", "7.5"} <= set(gateway_states().values())
        backend.status["gateways"] = 503
        backend.status[GATEWAY] = 503
        fresh = await _restart(hass, owner, cloud, first)
        assert fresh.startup_mode == "inventory"
        await eventually(lambda: native(owner, "number", NUMBER) is not None)
        await settle(hass)
        assert native(owner, "number", NUMBER).coordinator is fresh
        assert set(gateway_states().values()) <= {"unavailable", None}

        # A first poll without any answer still shows nothing (P12).
        fresh._next_update = dict.fromkeys(fresh._poll_intervals, 0)
        await fresh.async_refresh()
        await settle(hass)
        assert set(gateway_states().values()) <= {"unavailable", None}

        backend.status.clear()
        fresh._next_update = dict.fromkeys(fresh._poll_intervals, 0)
        await fresh.async_refresh()
        await settle(hass)
        await eventually(lambda: available(owner, "number", NUMBER))
        number = native(owner, "number", NUMBER)
        assert hass.states.get(number.entity_id).state == "20.0"
        assert {"20.0", "7.5"} <= set(gateway_states().values())
