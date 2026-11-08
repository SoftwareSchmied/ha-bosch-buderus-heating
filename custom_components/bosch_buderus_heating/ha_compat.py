"""Bridge the Home Assistant releases this integration supports.

The oldest supported release is Home Assistant 2026.5.0 (its config flows
can continue directly into an options flow). Each helper prefers the current
API and only falls back where an older release does not offer it yet.
"""

from __future__ import annotations

import inspect
from functools import cache
from typing import Any

import aiohttp
from homeassistant.helpers import device_registry as dr

MAX_LOCAL_HEADERS = 64

# Home Assistant 2026.11 serves climate and water heater temperatures as
# native members; the old names stop working in 2027.11.
_TEMPERATURE_MEMBERS = (
    ("native_current_temperature", "current_temperature"),
    ("native_target_temperature", "target_temperature"),
    ("native_target_temperature_high", "target_temperature_high"),
    ("native_target_temperature_low", "target_temperature_low"),
    ("_attr_native_temperature_unit", "_attr_temperature_unit"),
)


def temperature_members[T: type](cls: T) -> T:
    """Serve the native temperature members of ``cls`` under their old names
    on releases before 2026.11; newer releases use the native ones directly.

    Only members defined on ``cls`` itself are copied: decorate every class
    that defines them, and set the values through these members rather than
    by assigning ``_attr_native_*`` per instance."""
    if not hasattr(cls, "native_temperature_unit"):
        for native, legacy in _TEMPERATURE_MEMBERS:
            if native in vars(cls):
                setattr(cls, legacy, vars(cls)[native])
    return cls


def temperature_unit(entity: Any) -> str:
    """The unit an entity reports its temperatures in, on every release."""
    unit = getattr(entity, "native_temperature_unit", None)
    return str(unit if unit is not None else entity.temperature_unit)


def device_by_identifier(
    registry: dr.DeviceRegistry, identifier: tuple[str, str], config_entry_id: str
) -> dr.DeviceEntry | None:
    """Return the device of one config entry that carries ``identifier``."""
    lookup = getattr(registry, "async_get_device_by_identifier", None)
    if lookup is not None:
        device: dr.DeviceEntry | None = lookup(identifier, config_entry_id)
        return device
    # Older releases keep identifiers unique across config entries, so the
    # global lookup is exact; ownership still has to be checked.
    device = registry.async_get_device(identifiers={identifier})
    if device is None or config_entry_id not in device.config_entries:
        return None
    return device


def identifier_owned_elsewhere(
    registry: dr.DeviceRegistry, identifier: tuple[str, str], config_entry_id: str
) -> bool:
    """Return whether another config entry blocks ``identifier`` globally.

    Current releases scope identifiers to their config entry, so nothing blocks
    them. Older releases reject a second device with the same identifier.
    """
    if hasattr(registry, "async_get_device_by_identifier"):
        return False
    device = registry.async_get_device(identifiers={identifier})
    return device is not None and config_entry_id not in device.config_entries


@cache
def local_header_limits() -> dict[str, Any]:
    """Return the response header count limit when aiohttp supports it.

    aiohttp 3.13 (Home Assistant 2026.3) has no ``max_headers`` option; the
    line and field size limits still bound the header block there.
    """
    parameters = inspect.signature(aiohttp.ClientSession).parameters
    if "max_headers" in parameters:
        return {"max_headers": MAX_LOCAL_HEADERS}
    return {}
