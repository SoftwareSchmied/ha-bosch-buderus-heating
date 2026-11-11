"""D42: let the user delete a device the integration no longer provides.

A heating circuit removed at the controller or an accessory gone from the
plant keeps its device until the user deletes it. Home Assistant offers
"Delete" only when the integration allows it. A device counts as current
while one of its entities of this entry has a live state, or while a
current device is connected through it; everything else may be deleted.
Should the plant report the device again, the next start creates it anew.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_RESTORED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er


def _has_live_entity(hass: HomeAssistant, entry_id: str, device_id: str) -> bool:
    """An entity of this entry that a platform currently provides."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_device(
        registry, device_id, include_disabled_entities=True
    ):
        if entity.config_entry_id != entry_id:
            continue
        state = hass.states.get(entity.entity_id)
        # A registered entity no platform provided is restored as
        # unavailable with the "restored" attribute.
        if state is not None and not state.attributes.get(ATTR_RESTORED):
            return True
    return False


def device_in_use(hass: HomeAssistant, entry_id: str, device_id: str) -> bool:
    """Whether the device or a device connected through it is current."""
    if _has_live_entity(hass, entry_id, device_id):
        return True
    registry = dr.async_get(hass)
    return any(
        child.via_device_id == device_id
        and child.id != device_id
        and _has_live_entity(hass, entry_id, child.id)
        for child in dr.async_entries_for_config_entry(registry, entry_id)
    )


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry[Any], device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting a device without a current entity (stale-devices)."""
    return not device_in_use(hass, config_entry.entry_id, device_entry.id)
