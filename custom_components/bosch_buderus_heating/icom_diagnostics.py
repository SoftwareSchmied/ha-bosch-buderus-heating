"""I1/I2: the diagnostics block of an iCom gateway.

Counts, sizes, fixed tokens and the component type numbers of
``/system/info`` only: no serial number, product name, version, value or
identifier of the installation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from .legacy_holidays import SYSTEM_INFO_PATH, icom_components

BUS_PATH: Final = "/system/bus"
# The known bus values. With the last three, the system is expected to be
# possibly not supported by the cloud access (expected, untested).
KNOWN_BUSES: Final = frozenset({"EMS2_0", "EMS1_0", "CAN", "MODBUS", "No_Bus"})
POSSIBLY_UNSUPPORTED_BUSES: Final = frozenset({"EMS1_0", "CAN", "No_Bus"})
BUS_HINT: Final = "bus_possibly_unsupported_reported_values_only"


def _reading(snapshots: Mapping[str, Any], path: str) -> Any:
    snapshot = snapshots.get(path)
    if snapshot is None or not getattr(snapshot, "available", False):
        return None
    return getattr(snapshot, "resource", None)


def icom_diagnostics(
    snapshots: Mapping[str, Any],
    profile: Mapping[str, object],
    read_back_delays: tuple[float, ...],
) -> dict[str, object]:
    """The iCom block: load profile, components, bus and holiday rules."""
    info = _reading(snapshots, SYSTEM_INFO_PATH)
    components = icom_components(info)
    bus_resource = _reading(snapshots, BUS_PATH)
    bus = getattr(bus_resource, "value", None)
    return {
        "load_profile": {
            **profile,
            "read_back_delays": list(read_back_delays),
        },
        "system_info_read": info is not None,
        # Only the component type (Id) and its token (Tok).
        "system_components": [
            {"id": identifier, "token": token} for identifier, token in components.items
        ],
        "bus": (
            None
            if bus is None
            else bus
            if isinstance(bus, str) and bus in KNOWN_BUSES
            else "other"
        ),
        "bus_hint": (
            BUS_HINT
            if isinstance(bus, str) and bus in POSSIBLY_UNSUPPORTED_BUSES
            else None
        ),
        "holiday_rules": {
            "status": "expected_untested",
            "hmi800_hp_light": components.hmi800_hp_light,
            "hidden_circuits": sorted(components.hidden_circuits),
        },
    }
