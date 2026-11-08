"""P19: functional groups never create devices; the device set stays stable.

Real managed runtime with a loopback PointT backend: heating circuit, hot water,
heat generator and a solar-circuit model case are discovered, the cloud source
is restarted, and the devices and identities stay exactly the same.
"""

from __future__ import annotations

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from tests.test_managed_pointt import Backend, binding_for, eventually, running

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")

GROUP_READINGS = {
    "/heatingCircuits/hc1/roomtemperature": 21.0,
    "/dhwCircuits/dhw1/actualTemp": 48.5,
    "/heatSources/hs1/supplyFlowCondenserTemp": 35.0,
    "/solarCircuits/sc1/collectorTemperature": 42.0,
}


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    hass.config.components.add(DOMAIN)


class GroupsBackend(Backend):
    """A plant with several functional groups and no PointT device list."""

    def resource(self, gateway, path):
        tree = {
            "/heatingCircuits": ["/heatingCircuits/hc1"],
            "/dhwCircuits": ["/dhwCircuits/dhw1"],
            "/heatSources": ["/heatSources/hs1"],
            "/solarCircuits": ["/solarCircuits/sc1"],
        }
        for reading in GROUP_READINGS:
            tree.setdefault(reading.rsplit("/", 1)[0], []).append(reading)
        if path in tree:
            return {"references": [{"id": child} for child in tree[path]]}
        if path in GROUP_READINGS:
            return {
                "id": path,
                "type": "floatValue",
                "value": GROUP_READINGS[path],
                "unitOfMeasure": "C",
                "writeable": False,
            }
        if path == "/devices/list":
            return {"id": path, "type": "deviceArray", "values": []}
        return super().resource(gateway, path)


def group_entities(hass, entry):
    registry = er.async_get(hass)
    return {
        item.unique_id: (item.entity_id, item.device_id)
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if any(part in item.unique_id for part in ("hc1", "dhw1", "hs1", "sc1"))
    }


def devices(hass, entry):
    return {
        device.id: frozenset(device.identifiers)
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
    }


async def test_groups_stay_at_the_gateway_device_across_a_source_restart(hass):
    backend = GroupsBackend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        await owner.start_platforms()
        await eventually(lambda: len(group_entities(hass, entry)) >= 8, timeout=30)
        before_entities = group_entities(hass, entry)
        before_devices = devices(hass, entry)

        # One device per PointT gateway, none for circuits or the solar group.
        assert sorted(before_devices.values()) == sorted(
            frozenset({(DOMAIN, gateway)}) for gateway in backend.gateways
        )
        for _entity_id, device_id in before_entities.values():
            assert device_id in before_devices
        identifiers = {key for items in before_devices.values() for _, key in items}
        assert not any(
            group in key
            for key in identifiers
            for group in ("hc1", "dhw1", "hs1", "sc1")
        )

        # Restart the cloud source of one gateway (as after a reload).
        manager = owner.connections.management
        heat = backend.gateways[0]
        manager.set_binding_enabled(
            manager.edit(cloud), binding_for(owner, heat), False
        )
        await eventually(
            lambda: all(
                hass.states.get(entity_id) is None
                or hass.states.get(entity_id).state == "unavailable"
                for unique_id, (entity_id, _) in group_entities(hass, entry).items()
                if unique_id.startswith(heat + ":")
            ),
            timeout=30,
        )
        manager.set_binding_enabled(manager.edit(cloud), binding_for(owner, heat), True)
        await eventually(
            lambda: all(
                hass.states.get(entity_id) is not None
                and hass.states.get(entity_id).state not in ("unavailable", "unknown")
                for unique_id, (entity_id, _) in group_entities(hass, entry).items()
                if unique_id.startswith(heat + ":")
            ),
            timeout=30,
        )

        assert devices(hass, entry) == before_devices
        assert group_entities(hass, entry) == before_entities
