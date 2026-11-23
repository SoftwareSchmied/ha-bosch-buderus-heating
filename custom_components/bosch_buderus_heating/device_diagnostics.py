"""Diagnostics of one device: entity kinds and states, never readings or names.

An optional component device (``component_devices``) adds the block
``component``: its place and type number, the names of the component table,
role, basis and the software version shown at the device.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.device_registry import DeviceEntry

# The most entities listed for one device; the counts still cover all.
MAX_LISTED_ENTITIES = 500
# Fixed translation keys only; anything else (a user-given or unexpected
# value) is not copied.
_SAFE_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.")


def _safe_key(value: object) -> str | None:
    if (
        isinstance(value, str)
        and 0 < len(value) <= 80
        and set(value) <= _SAFE_KEY_CHARS
    ):
        return value
    return None


def _state_kind(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if state is None:
        return "not_loaded"
    if state.state == STATE_UNAVAILABLE:
        return "unavailable"
    if state.state == STATE_UNKNOWN:
        return "unknown"
    return "value"


def device_report(
    hass: HomeAssistant, entry: ConfigEntry[Any], device: DeviceEntry
) -> dict[str, Any]:
    """Return the entities of one device with their kind and state class only."""
    registry = er.async_get(hass)
    entities = [
        item
        for item in er.async_entries_for_device(
            registry, device.id, include_disabled_entities=True
        )
        if item.config_entry_id == entry.entry_id
    ]
    listed: list[dict[str, Any]] = []
    states: Counter[str] = Counter()
    domains: Counter[str] = Counter()
    disabled = 0
    for item in sorted(entities, key=lambda value: value.unique_id):
        kind = "disabled" if item.disabled_by else _state_kind(hass, item.entity_id)
        states[kind] += 1
        domains[item.domain] += 1
        disabled += 1 if item.disabled_by else 0
        if len(listed) < MAX_LISTED_ENTITIES:
            listed.append(
                {
                    "domain": item.domain,
                    "translation_key": _safe_key(item.translation_key),
                    "entity_category": item.entity_category.value
                    if item.entity_category
                    else None,
                    "disabled_by": item.disabled_by.value if item.disabled_by else None,
                    "state": kind,
                }
            )
    report: dict[str, Any] = {
        "device": {
            "has_parent_device": device.via_device_id is not None,
            "disabled": device.disabled_by is not None,
            "entity_count": len(entities),
            "disabled_entity_count": disabled,
        },
        "entities_by_domain": dict(sorted(domains.items())),
        "entities_by_state": dict(sorted(states.items())),
        "entities": listed,
        "entities_truncated": len(entities) > MAX_LISTED_ENTITIES,
    }
    from .component_devices import device_report as component_report

    if (component := component_report(hass, entry, device)) is not None:
        report["component"] = component
    return report
