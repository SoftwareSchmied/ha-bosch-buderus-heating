"""Shared field declarations in Bacon device metadata."""

from __future__ import annotations

from typing import Any

from .models import Device

# The Celsius spellings every device family accepts.
CELSIUS = ("C", "degC", "Celsius", "°C")


def metadata(device: Device, section: str, key: str) -> dict[str, Any] | None:
    """Distinguish an empty field declaration from an absent field."""
    value: Any = device.metadata
    for part in ("shadows", "state") if section == "state" else ("topics", "sensor"):
        value = value.get(part) if isinstance(value, dict) else None
    value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, dict) else None


def writable(meta: dict[str, Any] | None) -> bool:
    """Read-only is the wire contract's default."""
    return meta is not None and meta.get("ro") is False
