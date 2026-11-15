"""Reviewed local-only entities take their reading back from the cloud.

A reading with a reviewed local definition (``local_only.CATALOG``) has its
local-only entity whenever the local access provides it, also when the cloud
offers the path (``local_only_pair``); the cloud reading is its second
source. At setup, before any entity is added, every cloud entry of such a
reading is brought to the same shape, whichever was set up first (the
cloud, the local access, or an earlier version's hand-over):

- a cloud entry in use (enabled, switched off by the user, or customised)
  continues as the local-only entity: it gets the local-only unique ID, so
  its entity ID, settings and recorded history stay, also when the user
  renamed the entity ID or an automation uses it. The local-only entity
  writes its name, classes and states when it is added; a repair notice
  names the entities. A removed local-only entry of the reading stays
  removed, so the entity ID in use wins;
- a cloud entry the integration created disabled is not in use: it is
  removed, and Home Assistant restores the removed local-only entry when
  the entity is added again (or the local-only entity is added as new);
- next to a registered local-only entry only such a disabled cloud entry
  is removed; one in use stays and is named in a repair notice.

An entry continues only when the local-only entity can be added now (a
current local reading or a fixed form) with the same native unit and state
class, so its long-term statistics continue; otherwise the cloud entity
stays as it is. A reading new to the entry waits for an enabled local
access, like a reading without any local entry. Only this integration's own
sensor entries of reviewed paths are changed, no entity ID is ever changed,
and nothing is healed without a configured local access.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .local_only import state_class, static_unit, unit_and_class
from .local_only_pair import fixed_definition, reviewed_definition
from .local_only_sensor import (
    UNIQUE_ID_PART,
    _cloud_sensors,
    _text,
    _unit,
    local_only_unique_id,
    registered_form,
)
from .pointt.redaction import resource_path_template

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .local_only_sensor import _CloudSensor
    from .local_selection import LocalSelection

_LOGGER = logging.getLogger(__name__)

RESTORED = "restored"
TAKEN_BACK = "taken_back"
CONTINUED = "continued"
CLOUD_ENTRIES_REMOVED = "cloud_entries_removed"
CLOUD_ENTRIES_KEPT = "cloud_entries_kept"
_COUNTS = (RESTORED, TAKEN_BACK, CONTINUED, CLOUD_ENTRIES_REMOVED, CLOUD_ENTRIES_KEPT)
# Healed entities per entry and gateway since Home Assistant started.
_HEALED: HassKey[dict[tuple[str | None, str], Counter[str]]] = HassKey(
    f"{DOMAIN}.local_only_healed"
)
CONTINUED_ISSUE = "local_only_entities_continued"
KEPT_ISSUE = "local_only_entities_kept"


def cloud_unique_id(gateway_id: str, path: str) -> str:
    """The unique ID the cloud's sensor of a reviewed reading has."""
    return f"{gateway_id}:{path.strip('/').replace('/', ':')}"


def issue_id(kind: str, entry_id: str, gateway_id: str) -> str:
    """The repair notice of one entry and gateway, without its identifier."""
    digest = hashlib.sha256(gateway_id.encode()).hexdigest()[:16]
    return f"{kind}_{entry_id}_{digest}"


@callback
def local_claims(coordinator: BoschBuderusDataUpdateCoordinator, path: str) -> bool:
    """The local-only entity of a reviewed reading takes it although its
    entry is not registered (``keeps_local_entity`` checks the rest).

    Never while a cloud entry of the path is registered: the setup heal
    (``heal_registered``) already continued or removed every cloud entry it
    could, so one still there stays the entity of the reading. Otherwise
    when the entity can be added now (a current local reading or a fixed
    form) and either its removed entry waits to be restored or the local
    access is switched on.
    """
    entry = getattr(coordinator, "config_entry", None)
    selection = getattr(coordinator, "local_selection", None)
    if entry is None or selection is None or not selection.has_configured_local_access:
        return False
    registry = er.async_get(coordinator.hass)
    gateway_id = coordinator.gateway.gateway_id
    if (
        registry.async_get_entity_id(
            "sensor", DOMAIN, cloud_unique_id(gateway_id, path)
        )
        is not None
    ):
        return False  # an existing cloud entity stays
    if not (
        selection.has_local_access
        or _removed_local_entry(registry, entry.entry_id, gateway_id, path)
    ):
        return False
    return (
        fixed_definition(path) is not None
        or path in selection.local_only_observations()
    )


@dataclass(slots=True)
class _Notices:
    """Entity IDs to name in the repair notices of one heal."""

    continued: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


@callback
def heal_registered(coordinator: BoschBuderusDataUpdateCoordinator) -> None:
    """At setup, before any entity is added: give reviewed readings their
    local-only entries, continuing the cloud entries in use."""
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
    notices = _Notices()
    sensors: dict[str, list[_CloudSensor]] | None = None
    for path in sorted(set(cloud) | set(handed)):
        if sensors is None:
            sensors = _cloud_sensors(coordinator)
        _heal(
            coordinator,
            selection,
            registry,
            entry.entry_id,
            counts,
            notices,
            path,
            cloud.get(path, []),
            handed.get(path),
            sensors.get(path, []),
        )
    # The entries in use that stay next to a registered entity, at this start.
    counts[CLOUD_ENTRIES_KEPT] = len(notices.kept)
    _notify(coordinator, entry.entry_id, gateway_id, notices)


def _heal(
    coordinator: BoschBuderusDataUpdateCoordinator,
    selection: LocalSelection,
    registry: er.EntityRegistry,
    entry_id: str,
    counts: Counter[str],
    notices: _Notices,
    path: str,
    cloud: list[er.RegistryEntry],
    handed: er.RegistryEntry | None,
    sensors: list[_CloudSensor],
) -> None:
    gateway_id = coordinator.gateway.gateway_id
    local_id = local_only_unique_id(gateway_id, path)
    entries = {item.entity_id: item for item in (*cloud, *filter(None, [handed]))}
    registered_id = registry.async_get_entity_id("sensor", DOMAIN, local_id)
    if registered_id is not None:
        # The registered local-only entity is the entity of the reading.
        for item in entries.values():
            if _in_use(item):
                # Never removed: it stays (no longer provided) and is named.
                notices.kept.append(f"{item.entity_id} → {registered_id}")
            else:
                registry.async_remove(item.entity_id)
                counts[CLOUD_ENTRIES_REMOVED] += 1
                _log(path, "removed", 1)
        return
    removed = _removed_local_entry(registry, entry_id, gateway_id, path)
    if not (removed or handed is not None or selection.has_local_access):
        return  # a reading new to the entry waits for an enabled local access
    form = _local_form(coordinator, selection, path)
    if form is None:
        return  # the local-only entity cannot be added now: the cloud stays
    in_use = [item for item in entries.values() if _in_use(item)]
    successor: er.RegistryEntry | None
    healed: str | None
    if handed is not None and not removed:
        # Handed over by an earlier version: the same entry continues.
        successor, healed = handed, TAKEN_BACK
    elif in_use:
        successor, healed = in_use[0], CONTINUED
        if not _same_statistics(successor, form, sensors):
            return  # another unit or state class: the cloud entity stays
    else:
        successor, healed = None, RESTORED if removed else None
    dropped = 0
    for item in entries.values():
        if item is successor:
            continue
        if _in_use(item):
            # Cannot happen with one cloud unique ID per path; never removed.
            continue
        registry.async_remove(item.entity_id)
        dropped += 1
    if successor is not None:
        if removed:
            # The entity ID in use wins over the removed entry's.
            registry.deleted_entities.pop(("sensor", DOMAIN, local_id), None)
            registry.async_schedule_save()
        registry.async_update_entity(successor.entity_id, new_unique_id=local_id)
        if _in_use(successor):
            notices.continued.append(successor.entity_id)
    counts[CLOUD_ENTRIES_REMOVED] += dropped
    if healed is not None:
        counts[healed] += 1
    _log(path, healed or "removed", dropped)


def _in_use(item: er.RegistryEntry) -> bool:
    """Enabled, switched off by the user, or customised: never removed."""
    from . import _user_customised

    return (
        item.disabled_by is not er.RegistryEntryDisabler.INTEGRATION
        or _user_customised(item)
    )


def _local_form(
    coordinator: BoschBuderusDataUpdateCoordinator,
    selection: LocalSelection,
    path: str,
) -> tuple[str | None, str | None] | None:
    """Native unit and state class of the local-only entity if it can be
    added now; None without a current local reading or a fixed form."""
    from .local_plant_view import local_sensor

    definition = None
    unit: str | None = None
    if (observation := selection.local_only_observations().get(path)) is not None:
        sensor, _ = local_sensor(
            observation,
            registered=partial(
                registered_form, coordinator.hass, coordinator.gateway.gateway_id
            ),
        )
        if sensor is not None:
            definition = sensor.definition
            reading = selection.local_only_reading(path)
            mapped = (
                unit_and_class(definition, reading.unit)
                if reading is not None
                else None
            )
            unit = mapped[0] if mapped is not None else static_unit(definition)[0]
    if definition is None:
        if (definition := fixed_definition(path)) is None:
            return None
        unit = static_unit(definition)[0]
    return unit, _text(state_class(definition, unit))


def _same_statistics(
    item: er.RegistryEntry,
    form: tuple[str | None, str | None],
    sensors: list[_CloudSensor],
) -> bool:
    """The cloud entry may continue as the local-only entity: the same
    native unit, and the same state class when it has one, so its long-term
    statistics continue."""
    unit, statistics = form
    stored = _text((item.capabilities or {}).get("state_class"))
    if stored is not None and stored != statistics:
        return False
    # Native units, never displayed ones; the registry's unit only for a
    # path the cloud no longer offers.
    cloud_unit = (
        sensors[0].native_unit if len(sensors) == 1 else item.unit_of_measurement
    )
    return _unit(_text(cloud_unit)) == _unit(unit)


def _log(path: str, healed: str, changed: int) -> None:
    if healed == "removed" and not changed:
        return
    _LOGGER.info(
        "The local-only entity of %s takes its reading from the cloud's entry "
        "(%s; %d cloud entries changed); no entity ID changes",
        resource_path_template(path),
        healed,
        changed,
    )


def _notify(
    coordinator: BoschBuderusDataUpdateCoordinator,
    entry_id: str,
    gateway_id: str,
    notices: _Notices,
) -> None:
    """A repair notice for each entity in use that changed or stays behind;
    without them the change would only be in the log."""
    for kind, entities in (
        (CONTINUED_ISSUE, notices.continued),
        (KEPT_ISSUE, notices.kept),
    ):
        if not entities:
            continue
        try:
            ir.async_create_issue(
                coordinator.hass,
                DOMAIN,
                issue_id(kind, entry_id, gateway_id),
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=kind,
                translation_placeholders={
                    "count": str(len(entities)),
                    "entities": ", ".join(sorted(entities)),
                },
            )
        except Exception:  # a notice never stops the setup
            _LOGGER.warning("The notice about %s could not be created", kind)


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
