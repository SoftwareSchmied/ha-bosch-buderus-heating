"""A hot water circuit without a tank reports 0 as its hot water temperature.

A controller on an instantaneous (combination) water heater answers the
hot water temperature as 0.0 and still flags it available. The 0 is the
missing sensor, not a reading, so it must never be shown as 0 °C.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator

INSTANT = "instant"
_ACTUAL = re.compile(r"^(/dhwCircuits/[^/]+)/actualTemp$")


def is_instant_zero(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str, value: object
) -> bool:
    """The hot water temperature of an instantaneous circuit that reads zero."""
    match = _ACTUAL.fullmatch(path)
    if (
        match is None
        or isinstance(value, bool)
        or not isinstance(value, int | float)
        or value != 0
    ):
        return False
    snapshot = (coordinator.data or {}).get(match.group(1) + "/hotWaterSystem")
    system = snapshot.resource.value if snapshot is not None else None
    return isinstance(system, str) and system.strip().casefold() == INSTANT
