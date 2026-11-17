"""Per-family availability evidence for diagnostics, without identifiers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import dhw, dry, purifier, rac
from .live_report import age_bucket
from .models import Device

_EVENTS = {
    "last_sign_of_life": "alive_at",
    "last_information": "info_received_at",
    "last_sensor_message": "sensor_received_at",
    "last_state_read_answer": "state_answered_at",
}


def _families(device: Device) -> list[str]:
    names = []
    if rac.supported(device):
        names.append("midea_climate")
    for prefix, module in (
        ("dehumidifier", dry),
        ("air_purifier", purifier),
        ("water_heater", dhw),
    ):
        name = module.family(device)
        if name is not None:
            names.append(f"{prefix}_{name}")
    return names or ["other"]


def report(
    devices: dict[str, Device],
    now: float,
    available: Callable[[str], bool],
) -> dict[str, Any]:
    """Aggregate online state and event ages per family (age classes only)."""
    result: dict[str, dict[str, Any]] = {}
    for serial, device in devices.items():
        for name in _families(device):
            row = result.setdefault(
                name,
                {
                    "devices": 0,
                    "online_true": 0,
                    "online_false": 0,
                    "online_unknown": 0,
                    "available": 0,
                    **{event: {} for event in _EVENTS},
                },
            )
            row["devices"] += 1
            key = (
                "online_unknown"
                if device.online is None
                else "online_true"
                if device.online
                else "online_false"
            )
            row[key] += 1
            row["available"] += bool(available(serial))
            for event, attribute in _EVENTS.items():
                stamp = getattr(device, attribute)
                # Zero is the unset default of the received-at fields.
                bucket = age_bucket(now - stamp if stamp else None)
                row[event][bucket] = row[event].get(bucket, 0) + 1
    return result
