"""P20-C/P20-Q: removing and re-adding the local access keeps identities.

The local access is removed through the real enrollment journal. Shared
entities, the plant device and the optional per-source entities (P18) keep
their registry entries; re-adding the same gateway yields the same unique IDs,
so a per-source entity the user enabled stays enabled.
"""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    BoschBuderusSourceSensor,
    build_sensor_descriptions,
)
from tests.test_local_enrollment import journal as journal
from tests.test_local_selection import RETURN, TARGET, FakeRuntime
from tests.test_local_setup import registered
from tests.test_validity import _coordinator


def snapshot_of(hass, entry):
    entities = {
        item.unique_id: (item.entity_id, item.device_id, item.disabled_by, item.name)
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }
    devices = {
        device.id: (frozenset(device.identifiers), device.name_by_user)
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
    }
    return entities, devices


async def test_removing_and_re_adding_the_local_access_keeps_identities(
    hass, journal
) -> None:
    entry, management, cloud, _target = registered(hass, local=True)
    coordinator = _coordinator(
        hass,
        Resource(
            RETURN,
            value=30.0,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
        ),
    )
    coordinator.local_selection = LocalSelection(FakeRuntime([]), TARGET)
    description = next(
        item
        for item in build_sensor_descriptions(coordinator.resources)
        if item.resource_path == RETURN
    )
    shared = BoschBuderusSensor(coordinator, description)
    local = BoschBuderusSourceSensor(coordinator, description, "local")
    cloud_source = BoschBuderusSourceSensor(coordinator, description, "cloud")

    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "gateway-one")}
    )
    dr.async_get(hass).async_update_device(device.id, name_by_user="My heating")
    registry.async_get_or_create(
        "sensor", DOMAIN, shared.unique_id, config_entry=entry, device_id=device.id
    )
    enabled = registry.async_get_or_create(
        "sensor", DOMAIN, local.unique_id, config_entry=entry, device_id=device.id
    )
    registry.async_update_entity(enabled.entity_id, name="Return (LAN)")
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        cloud_source.unique_id,
        config_entry=entry,
        device_id=device.id,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    before = snapshot_of(hass, entry)

    after = await journal.remove_access(management, management.edit("local"))
    assert "local" not in after.connections
    assert cloud in after.connections
    # Nothing is deleted or moved: shared, per-source entities and the device.
    assert snapshot_of(hass, entry) == before

    # The same gateway again: the same identities, so the user's choice stays.
    again = BoschBuderusSourceSensor(coordinator, description, "local")
    assert again.unique_id == local.unique_id
    item = registry.async_get(
        registry.async_get_entity_id("sensor", DOMAIN, again.unique_id)
    )
    assert item.disabled_by is None and item.name == "Return (LAN)"
    await coordinator.async_shutdown()
