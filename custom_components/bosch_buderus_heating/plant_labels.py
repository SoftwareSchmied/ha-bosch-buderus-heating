"""Names of systems and devices in dialog selections, instead of identifiers.

A dialog that lists systems or devices shows the name the user knows: the
device name given in Home Assistant, or the name of the device. Only a system
without a device in this entry (not set up yet, or removed) keeps its
identifier, and two equal names get theirs added so they stay apart.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import SelectOptionDict

from .const import DOMAIN


def device_name(hass: HomeAssistant, entry_id: str, identifier: str) -> str | None:
    """The name of this entry's device with ``identifier``, if it has one."""
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry_id):
        if (DOMAIN, identifier) in device.identifiers:
            return device.name_by_user or device.name or None
    return None


def plant_labels(
    hass: HomeAssistant, entry_id: str, identifiers: Mapping[str, str]
) -> dict[str, str]:
    """A label for each choice (value: identifier of its system or device)."""
    names = {
        value: device_name(hass, entry_id, identifier) or identifier
        for value, identifier in identifiers.items()
    }
    counts = Counter(names.values())
    return {
        value: f"{name} ({identifiers[value]})"
        if counts[name] > 1 and name != identifiers[value]
        else name
        for value, name in names.items()
    }


def plant_choices(
    hass: HomeAssistant, entry_id: str, identifiers: Mapping[str, str]
) -> list[SelectOptionDict]:
    """Selection options with the labels of ``plant_labels``."""
    return [
        SelectOptionDict(value=value, label=label)
        for value, label in plant_labels(hass, entry_id, identifiers).items()
    ]
