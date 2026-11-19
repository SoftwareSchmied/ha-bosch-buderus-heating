"""What entities show over a short cloud blip (H-B3-2, see ``cloud_hold``).

Both helpers are for the display of an entity only. A write never uses them: it
reads the setting again from the gateway and refuses without a current reading.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .cloud_hold import GRACE_CYCLES
from .coordinator import Freshness, ResourceSnapshot

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator


def cloud_usable(coordinator: DataUpdateCoordinator[Any]) -> bool:
    """The last refresh succeeded, or its failure is still held.

    The replacement of ``CoordinatorEntity.available`` for entities that need a
    current cloud cycle, for every family (heating, air conditioner, wallbox).
    Without the hold (another kind of coordinator) it is exactly that.
    """
    if coordinator.last_update_success:
        return True
    holding = getattr(coordinator, "cloud_holding", None)
    return bool(holding()) if callable(holding) else False


def held_snapshot(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> ResourceSnapshot | None:
    """The stored snapshot; a reading whose request got no answer keeps its value.

    A current reading is returned as stored. A reading that failed without an
    answer for it (no connection, timeout, server error) and failed at most
    ``GRACE_CYCLES`` times in a row is returned with its last good value as
    current, as long as that value is within its validity limit. An explicit
    answer for the reading (an error status, invalid data) and a longer failure
    leave the stored snapshot, which is not current.
    """
    snapshot = (coordinator.data or {}).get(path)
    if (
        snapshot is None
        or (snapshot.available and snapshot.freshness is Freshness.FRESH)
        or snapshot.consecutive_failures > GRACE_CYCLES
    ):
        return snapshot
    displayed = getattr(coordinator, "displayed_cloud_snapshot", None)
    if not callable(displayed):
        return snapshot
    shown: ResourceSnapshot | None = displayed(path)
    if shown is not None and shown.available and shown.freshness is Freshness.FRESH:
        return shown
    return snapshot
