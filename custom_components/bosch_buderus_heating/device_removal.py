"""D42: let the user delete a device the integration no longer provides.

A heating circuit removed at the controller or an accessory gone from the
plant keeps its device until the user deletes it. Home Assistant offers
"Delete" only when the integration allows it. A device may be deleted only
when the entity registry holds no entity of this entry for it at all, and
no device connected through it holds one. A disabled entity or one only
restored as unavailable counts as present: deleting such a device would
lose the user's settings of its entities, so its entities are deleted
first. Should the plant report the device again, the next start creates it
anew.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er


def _has_entity(hass: HomeAssistant, entry_id: str, device_id: str) -> bool:
    """Any registered entity of this entry, also disabled or restored."""
    registry = er.async_get(hass)
    return any(
        entity.config_entry_id == entry_id
        for entity in er.async_entries_for_device(
            registry, device_id, include_disabled_entities=True
        )
    )


def device_in_use(hass: HomeAssistant, entry_id: str, device_id: str) -> bool:
    """Whether the device or a device connected through it has an entity."""
    if _has_entity(hass, entry_id, device_id):
        return True
    registry = dr.async_get(hass)
    return any(
        child.via_device_id == device_id
        and child.id != device_id
        and _has_entity(hass, entry_id, child.id)
        for child in dr.async_entries_for_config_entry(registry, entry_id)
    )


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry[Any], device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting a device without any registered entity (stale-devices)."""
    return not device_in_use(hass, config_entry.entry_id, device_entry.id)
