"""System health: the coarse state of the accesses, without any identifier."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .configuration import CLOUD_KIND
from .connection_status import access_status
from .const import DOMAIN
from .managed_entry import ManagedEntry


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register the info callback."""
    register.async_register_info(system_health_info)


def _describe(states: Counter[str]) -> str:
    if not states:
        return "none"
    return ", ".join(f"{state}: {count}" for state, count in sorted(states.items()))


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """State of the cloud and LAN accesses and the last successful update."""
    cloud: Counter[str] = Counter()
    local: Counter[str] = Counter()
    latest: datetime | None = None
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime = getattr(entry, "runtime_data", None)
        if not isinstance(runtime, ManagedEntry):
            continue
        for key, connection in runtime.connections.snapshot.connections.items():
            status = access_status(entry, key)
            (cloud if connection.kind == CLOUD_KIND else local)[status.state] += 1
            if status.last_success is not None and (
                latest is None or status.last_success > latest
            ):
                latest = status.last_success
    return {
        "cloud_accesses": _describe(cloud),
        "local_accesses": _describe(local),
        "last_successful_update": latest.isoformat() if latest else "none yet",
    }
