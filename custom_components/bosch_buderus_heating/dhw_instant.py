"""A hot water temperature of exactly 0.0 is a missing sensor, not a reading.

A controller on an instantaneous (combination) water heater answers the
hot water temperature as 0.0 and still flags it available. A storage
system behind a room controller does the same while its sensor is not
connected. A tank is never at 0 degrees, so the 0 is never shown as 0 °C,
whatever the hot water system reports.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator

_ACTUAL = re.compile(r"^/dhwCircuits/[^/]+/actualTemp$")


def is_instant_zero(
    _coordinator: BoschBuderusDataUpdateCoordinator, path: str, value: object
) -> bool:
    """The hot water temperature of a circuit that reads exactly zero."""
    return (
        _ACTUAL.fullmatch(path) is not None
        and not isinstance(value, bool)
        and isinstance(value, int | float)
        and value == 0
    )
