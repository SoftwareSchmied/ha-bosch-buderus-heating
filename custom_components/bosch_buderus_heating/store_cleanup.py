"""Remove the private files of removed entries and accesses (STORE-5).

A removal deletes the known files of its entry or access at once. Once per
start, files of entry IDs that no longer exist are removed as well: they are
left from earlier versions or from a removal that could not finish.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .log_helpers import log_failure

_LOGGER = logging.getLogger(__name__)

# The per-entry stores: the prefix before the entry ID and what follows it.
_ENTRY_FAMILIES = (
    (f"{DOMAIN}.faults.", r"\.[0-9a-f]{16}"),
    (f"{DOMAIN}_faults_", r"_[0-9a-f]{24}"),
    (f"{DOMAIN}.inventory.", ""),
    (f"{DOMAIN}.discovery_inventory.", ""),
    (f"{DOMAIN}.module_fingerprints.", ""),
    (f"{DOMAIN}.gateway_exchange.", ""),
    (f"{DOMAIN}.auth_history.", r"(?:\.[^.]+)?"),
)
_LOCAL_SCHEDULE = re.compile(rf"{re.escape(DOMAIN)}\.local_schedule\.[0-9a-f]{{64}}")


def local_schedule_scopes(entry_id: str, data: Mapping[str, Any]) -> set[str] | None:
    """The scopes of the local schedules of every binding; None if unreadable."""
    bindings = data.get("bindings", {})
    if not isinstance(bindings, Mapping):
        return None
    scopes = set()
    for binding_id, binding in bindings.items():
        target = binding.get("target_id") if isinstance(binding, Mapping) else None
        if not isinstance(binding_id, str) or not isinstance(target, str):
            return None
        scopes.add(f"{entry_id}:{target}:{binding_id}")
    return scopes


def local_schedule_path(hass: HomeAssistant, scope: str) -> Path:
    """The file of one local schedule, named exactly as its store names it."""
    from .local_polling.storage import LocalScheduleStore

    return LocalScheduleStore(hass, scope)._path


async def async_remove_local_schedules(
    hass: HomeAssistant, scopes: Iterable[str]
) -> None:
    """Delete the schedules of removed bindings; a missing file is fine."""
    paths = [local_schedule_path(hass, scope) for scope in scopes]

    def remove() -> None:
        for path in paths:
            path.unlink(missing_ok=True)

    try:
        await hass.async_add_executor_job(remove)
    except OSError:
        _LOGGER.warning("A local schedule of a removed access could not be deleted")


def _entry_gateways(entry: ConfigEntry[Any]) -> tuple[str, ...]:
    from .entry_sources import notification_gateways

    try:
        return notification_gateways(entry)
    except ValueError, KeyError, TypeError:
        return ()  # unreadable: the next start removes them by entry ID


async def async_remove_entry_stores(
    hass: HomeAssistant, entry: ConfigEntry[Any]
) -> None:
    """The per-gateway and per-access files of a removed entry (STORE-5).

    Also for an entry that was never loaded in this process. Estimated energy
    totals are dropped only for gateways no other entry uses.
    """
    from homeassistant.helpers.storage import Store

    from .auth_diagnostics import STORAGE_VERSION, _cloud_connection_ids, _storage_key
    from .estimated_energy import estimated_energy_store
    from .faults import async_remove_fault_store
    from .notifications import async_remove_notification_store

    gateways = _entry_gateways(entry)
    for gateway_id in gateways:
        await async_remove_fault_store(hass, entry.entry_id, gateway_id)
        await async_remove_notification_store(hass, entry.entry_id, gateway_id)
    for connection_id in sorted(_cloud_connection_ids(entry)):
        await Store[dict[str, Any]](
            hass,
            STORAGE_VERSION,
            _storage_key(entry.entry_id, connection_id),
            private=True,
        ).async_remove()
    if (scopes := local_schedule_scopes(entry.entry_id, entry.data)) is not None:
        await async_remove_local_schedules(hass, scopes)
    others = {
        gateway_id
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
        for gateway_id in _entry_gateways(other)
    }
    if unused := set(gateways) - others:
        try:
            await estimated_energy_store(hass).async_forget_gateways(unused)
        except Exception as err:  # the totals are an optional cache
            log_failure(
                _LOGGER,
                logging.WARNING,
                "Estimated energy totals could not be updated",
                error=err,
            )


def _orphaned(name: str, entry_ids: set[str]) -> bool:
    for prefix, suffix in _ENTRY_FAMILIES:
        if not name.startswith(prefix) or not re.fullmatch(
            re.escape(prefix) + ".+" + suffix, name
        ):
            continue
        return not any(
            re.fullmatch(re.escape(prefix + entry_id) + suffix, name)
            for entry_id in entry_ids
        )
    return False


async def async_sweep_orphaned_stores(hass: HomeAssistant) -> None:
    """Remove the files of entries and bindings that no longer exist.

    The configured entries, also disabled and ignored ones, are known before
    any of them starts. Backups set aside as ``.corrupt`` stay.
    """
    folder = Path(hass.config.path(".storage"))

    def names() -> list[str]:
        try:
            return [path.name for path in folder.iterdir() if path.is_file()]
        except FileNotFoundError:
            return []

    try:
        found = [
            name
            for name in await hass.async_add_executor_job(names)
            if name.startswith(DOMAIN) and ".corrupt" not in name
        ]
    except OSError:
        _LOGGER.debug("The storage folder could not be listed", exc_info=True)
        return
    entries: list[ConfigEntry[Any]] = hass.config_entries.async_entries(DOMAIN)
    entry_ids = {entry.entry_id for entry in entries}
    orphans = [name for name in found if _orphaned(name, entry_ids)]
    schedules: set[str] | None = set()
    for entry in entries:
        scopes = local_schedule_scopes(entry.entry_id, entry.data)
        if scopes is None or schedules is None:
            schedules = None  # one unreadable entry: keep every schedule
            continue
        schedules.update(local_schedule_path(hass, scope).name for scope in scopes)
    if schedules is not None:
        orphans.extend(
            name
            for name in found
            if _LOCAL_SCHEDULE.fullmatch(name) and name not in schedules
        )
    if not orphans:
        return

    def remove() -> None:
        for name in orphans:
            (folder / name).unlink(missing_ok=True)

    try:
        await hass.async_add_executor_job(remove)
    except OSError:
        _LOGGER.warning("Files of removed entries could not be deleted")
        return
    _LOGGER.info("Removed %d stored files of removed entries", len(orphans))
