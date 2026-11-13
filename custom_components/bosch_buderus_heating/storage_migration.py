"""Guarded HA storage transition, gated by the setup flow's active version."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .auth_lifecycle import entry_refresh_in_progress
from .configuration import ConfigurationError, load_configuration
from .configuration_migration import convert_legacy_configuration


@callback
def migrate_entry_storage(
    hass: HomeAssistant, entry: ConfigEntry[Any], *, minor_version: int | None = None
) -> None:
    """Validate before one HA update, without yielding or touching registries.

    Earlier previews must complete the existing version-3 migration first.
    The caller must stop old runtime jobs before changing the storage contract.
    Backups, startup and transport are separate from this configuration update.

    ``minor_version`` is the format the setup flow advertises. A converted or
    older version-4 entry ends there, so HA does not migrate it at every start;
    later minor versions only add optional data, so the stored data stays.
    """
    if hass.config_entries.async_get_entry(entry.entry_id) is not entry:
        raise ConfigurationError("entry_not_current")
    if entry.state not in (
        ConfigEntryState.NOT_LOADED,
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.MIGRATION_ERROR,
    ):
        raise ConfigurationError("entry_not_stopped")
    if entry.version == 4:
        load_configuration(
            entry.entry_id,
            entry.data,
            entry.options,
            version=entry.version,
            minor_version=entry.minor_version,
        )
        if (
            minor_version is not None
            and entry.minor_version < minor_version
            # A pending rotation would refuse a changed format; next start.
            and not entry_refresh_in_progress(hass, entry.entry_id)
        ):
            load_configuration(
                entry.entry_id,
                entry.data,
                entry.options,
                version=entry.version,
                minor_version=minor_version,
            )
            hass.config_entries.async_update_entry(entry, minor_version=minor_version)
        return
    if entry_refresh_in_progress(hass, entry.entry_id):
        raise ConfigurationError("authentication_in_progress")
    proposed = convert_legacy_configuration(
        entry.entry_id,
        entry.data,
        entry.options,
        version=entry.version,
        minor_version=entry.minor_version,
    )
    if minor_version is not None and minor_version > proposed.minor_version:
        proposed = load_configuration(
            proposed.owner_id,
            proposed.to_data(),
            proposed.to_options(),
            version=proposed.version,
            minor_version=minor_version,
        )
    hass.config_entries.async_update_entry(
        entry,
        data=proposed.to_data(),
        options=proposed.to_options(),
        version=proposed.version,
        minor_version=proposed.minor_version,
    )
