"""Reviewed local-only entities take their reading back from the cloud.

A reading with a reviewed local definition (``local_only.CATALOG``) has its
local-only entity whenever the local access provides it, also when the cloud
offers the path (``local_only_pair``); the cloud reading is its second
source. An earlier version could leave two registry shapes behind, which
are healed at setup, before any entity is added:

- the local-only entry was removed and a cloud entry of its own was added
  for the path: the cloud entry is removed, and Home Assistant restores the
  removed entry (entity ID, name, enablement, area) when the local-only
  entity is added again;
- the local-only entry was handed over to the cloud sensor (its unique ID
  was replaced): the entry gets its local-only unique ID back, so entity ID,
  settings and history stay, and the local-only entity writes its name,
  classes and states again when it is added.

Only this integration's own sensor entries of reviewed paths are changed.
No entity ID is ever changed, and an entry the user customised (name,
icon, area, labels, switched off or hidden) is never removed. Nothing is
healed without a configured local access.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .local_only_pair import fixed_definition, reviewed_definition
from .local_only_sensor import UNIQUE_ID_PART, local_only_unique_id
from .pointt.redaction import resource_path_template

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

RESTORED = "restored"
TAKEN_BACK = "taken_back"
CLOUD_ENTRIES_REMOVED = "cloud_entries_removed"
_COUNTS = (RESTORED, TAKEN_BACK, CLOUD_ENTRIES_REMOVED)
# Healed entities per entry and gateway since Home Assistant started.
_HEALED: HassKey[dict[tuple[str | None, str], Counter[str]]] = HassKey(
    f"{DOMAIN}.local_only_healed"
)


def cloud_unique_id(gateway_id: str, path: str) -> str:
    """The unique ID the cloud's sensor of a reviewed reading has."""
    return f"{gateway_id}:{path.strip('/').replace('/', ':')}"


@callback
def local_claims(coordinator: BoschBuderusDataUpdateCoordinator, path: str) -> bool:
    """The local-only entity of a reviewed reading takes it although its
    entry is not registered (``keeps_local_entity`` checks the rest).

    True when its removed entry waits to be restored, or for a reading new
    to this entry: no cloud entry of the path is registered and the entity
    can be added now (a current local reading or a fixed form). A cloud
    entity that was never a local-only one stays.
    """
    entry = getattr(coordinator, "config_entry", None)
    selection = getattr(coordinator, "local_selection", None)
    if entry is None or selection is None or not selection.has_configured_local_access:
        return False
    registry = er.async_get(coordinator.hass)
    gateway_id = coordinator.gateway.gateway_id
    if _removed_local_entry(registry, entry.entry_id, gateway_id, path):
        return True  # Home Assistant restores it when the entity is added
    if not selection.has_local_access:
        return False
    if (
        registry.async_get_entity_id(
            "sensor", DOMAIN, cloud_unique_id(gateway_id, path)
        )
        is not None
    ):
        return False  # an existing cloud entity stays
    return (
        fixed_definition(path) is not None
        or path in selection.local_only_observations()
    )


@callback
def heal_registered(coordinator: BoschBuderusDataUpdateCoordinator) -> None:
    """At setup, before any entity is added: give reviewed readings their
    local-only entries back and remove the cloud entries left over."""
    entry = getattr(coordinator, "config_entry", None)
    selection = getattr(coordinator, "local_selection", None)
    if entry is None or selection is None or not selection.has_configured_local_access:
        return
    registry = er.async_get(coordinator.hass)
    gateway_id = coordinator.gateway.gateway_id
    local_prefix = f"{gateway_id}{UNIQUE_ID_PART}"
    cloud_prefix = f"{gateway_id}:"
    cloud: dict[str, list[er.RegistryEntry]] = {}
    handed: dict[str, er.RegistryEntry] = {}
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            item.domain != "sensor"
            or item.platform != DOMAIN
            or not item.unique_id.startswith(cloud_prefix)
        ):
            continue
        previous = item.previous_unique_id or ""
        if previous.startswith(local_prefix) and reviewed_definition(
            handed_path := _path(previous, local_prefix)
        ):
            # Handed over to the cloud sensor by an earlier version.
            handed.setdefault(handed_path, item)
        if reviewed_definition(path := _path(item.unique_id, cloud_prefix)):
            cloud.setdefault(path, []).append(item)
    counts = coordinator.hass.data.setdefault(_HEALED, {}).setdefault(
        (entry.entry_id, gateway_id), Counter()
    )
    for path in sorted(set(cloud) | set(handed)):
        _heal(
            registry,
            counts,
            entry.entry_id,
            gateway_id,
            path,
            cloud.get(path, []),
            handed.get(path),
        )


def _heal(
    registry: er.EntityRegistry,
    counts: Counter[str],
    entry_id: str,
    gateway_id: str,
    path: str,
    cloud: list[er.RegistryEntry],
    handed: er.RegistryEntry | None,
) -> None:
    from . import _user_customised

    local_id = local_only_unique_id(gateway_id, path)
    registered = registry.async_get_entity_id("sensor", DOMAIN, local_id) is not None
    removed = _removed_local_entry(registry, entry_id, gateway_id, path)
    leftovers = [item for item in cloud if item is not handed]
    healed: str | None
    if handed is not None and not registered and not removed:
        # The same entry continues: entity ID, settings and history stay.
        registry.async_update_entity(handed.entity_id, new_unique_id=local_id)
        healed = TAKEN_BACK
    elif registered or removed:
        if handed is not None:
            leftovers.append(handed)
        # Home Assistant restores a removed entry when its entity is added.
        healed = RESTORED if not registered else None
    else:
        return  # a cloud entity that was never a local-only one stays
    dropped = 0
    for item in leftovers:
        if _user_customised(item):
            continue  # the user's entry stays (it is no longer provided)
        registry.async_remove(item.entity_id)
        dropped += 1
    counts[CLOUD_ENTRIES_REMOVED] += dropped
    if healed is not None:
        counts[healed] += 1
    if healed is not None or dropped:
        _LOGGER.info(
            "The local-only entity of %s takes its reading back from the cloud "
            "(%s; %d cloud entries removed); its entity ID and settings stay",
            resource_path_template(path),
            healed or "kept",
            dropped,
        )


def _path(unique_id: str, prefix: str) -> str:
    return "/" + unique_id.removeprefix(prefix).replace(":", "/")


def _removed_local_entry(
    registry: er.EntityRegistry, entry_id: str, gateway_id: str, path: str
) -> bool:
    """The local-only entry of this entry was removed and can be restored."""
    removed = registry.deleted_entities.get(
        ("sensor", DOMAIN, local_only_unique_id(gateway_id, path))
    )
    return removed is not None and removed.config_entry_id == entry_id


def diagnostics(coordinator: BoschBuderusDataUpdateCoordinator) -> dict[str, int]:
    """Healed entities since Home Assistant started (counts only)."""
    entry = getattr(coordinator, "config_entry", None)
    key = (
        entry.entry_id if entry is not None else None,
        coordinator.gateway.gateway_id,
    )
    counts = coordinator.hass.data.get(_HEALED, {}).get(key, Counter())
    return {name: counts[name] for name in _COUNTS}
