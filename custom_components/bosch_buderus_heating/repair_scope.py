"""Stable local repair ownership for legacy entries and managed sources."""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.config_entries import ConfigEntry


def repair_id(
    prefix: str,
    entry: ConfigEntry[Any],
    connection_id: str | None = None,
    binding_id: str | None = None,
) -> str:
    identity = prefix + entry.entry_id
    if entry.version == 4:
        if connection_id is None:
            raise ValueError("A managed repair requires a connection")
        scope = connection_id + "\0" + (binding_id or "")
        identity += "_" + hashlib.sha256(scope.encode()).hexdigest()[:24]
    return identity


def repair_data(
    entry: ConfigEntry[Any],
    connection_id: str | None = None,
    binding_id: str | None = None,
) -> dict[str, str | int | float | None]:
    data: dict[str, str | int | float | None] = {"entry_id": entry.entry_id}
    if entry.version == 4:
        if connection_id is None:
            raise ValueError("A managed repair requires a connection")
        data["connection_id"] = connection_id
        if binding_id is not None:
            data["binding_id"] = binding_id
    return data
