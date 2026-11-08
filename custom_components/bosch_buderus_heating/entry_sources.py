"""Resolve current sources without treating multiple connections as one account."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

from .configuration import load_configuration
from .configuration_access import EntryConfiguration
from .coordinator import BoschBuderusDataUpdateCoordinator
from .runtime import BoschBuderusRuntimeData


def pointt_coordinators(
    entry: ConfigEntry[Any],
) -> tuple[BoschBuderusDataUpdateCoordinator, ...] | None:
    from .managed_entry import ManagedEntry

    runtime = getattr(entry, "runtime_data", None)
    if isinstance(runtime, BoschBuderusRuntimeData):
        return runtime.coordinators
    if isinstance(runtime, ManagedEntry):
        return tuple(
            source
            for key in runtime.connections.snapshot.bindings
            if isinstance(
                source := runtime.connections.source_data(key),
                BoschBuderusDataUpdateCoordinator,
            )
        )
    return None


def notification_gateways(entry: ConfigEntry[Any]) -> tuple[str, ...]:
    """Include retained PointT targets even after their last access is removed."""
    if entry.version != 4:
        values = EntryConfiguration(entry).cloud_data().get("gateway_ids", [])
        if not isinstance(values, list):
            return ()
        return tuple(dict.fromkeys(item for item in values if isinstance(item, str)))
    snapshot = load_configuration(
        entry.entry_id,
        entry.data,
        entry.options,
        version=entry.version,
        minor_version=entry.minor_version,
    )
    from .connection_runtime import plant_gateway

    return tuple(
        dict.fromkeys(
            gateway
            for target in snapshot.targets.values()
            if (gateway := plant_gateway(target.kind, target.to_data())) is not None
        )
    )
