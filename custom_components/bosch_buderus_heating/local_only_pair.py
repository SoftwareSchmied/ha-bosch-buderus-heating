"""A reviewed local-only reading the cloud offers as well.

When the cloud starts offering the path of a reading that already has a
local-only entity with a reviewed definition (for example once the children
of a refused directory are read directly), that entity stays the entity of
the reading: unique ID, entity ID, name, classes, states and enablement
stay, and no cloud sensor is added for the path. The cloud reading becomes
its second source:

- a paired reading (the pair catalog) shows what every paired entity shows:
  the local reading first with the local values option on or while the
  cloud cannot be used, otherwise the cloud reading within its validity
  limit; the local reading stands in while there is no current cloud one;
- any other reading shows its local reading first, as before, and the cloud
  reading only while there is no local one.

Readings without a reviewed definition (generic or documented ones the
local plant view offers) keep the hand-over to the cloud's sensor (8C-c).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .coordinator import SnapshotSource
from .local_only import GENERIC, LocalOnlyDefinition, definition_for
from .local_selection import eligible_path
from .observations import from_cloud_snapshot

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .observations import Observation

LOCAL = "local"
CLOUD = "cloud"


def reviewed_definition(path: str) -> LocalOnlyDefinition | None:
    """The reviewed local definition of a path (its entity is kept)."""
    return definition_for(path)


def fixed_definition(path: str) -> LocalOnlyDefinition | None:
    """A reviewed definition whose form never depends on a reading, so its
    entity can be added before the first local reading."""
    definition = reviewed_definition(path)
    if (
        definition is None
        or definition.kind == GENERIC
        or definition.alternate is not None
        or definition.unverified
    ):
        return None
    return definition


def paired_reading(
    coordinator: BoschBuderusDataUpdateCoordinator,
    path: str,
    local: Observation | None,
) -> tuple[Observation | None, str | None]:
    """The reading a kept local-only entity shows, and its source."""
    if eligible_path(path):
        shown = coordinator.display_snapshot(path)
        if (
            shown is not None
            and shown.available
            and shown.source is not SnapshotSource.LOCAL
        ):
            return _cloud(coordinator, shown), CLOUD
        return local, LOCAL if local is not None else None
    if local is not None:
        return local, LOCAL
    shown = coordinator.displayed_cloud_snapshot(path)
    if shown is None or not shown.available:
        return None, None
    return _cloud(coordinator, shown), CLOUD


def _cloud(
    coordinator: BoschBuderusDataUpdateCoordinator, snapshot: object
) -> Observation:
    return from_cloud_snapshot(
        snapshot, connection_id="cloud", target_id=coordinator.gateway.gateway_id
    )
