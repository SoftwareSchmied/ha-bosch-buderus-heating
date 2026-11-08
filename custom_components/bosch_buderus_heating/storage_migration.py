"""Guarded HA storage transition, gated by the setup flow's active version."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .auth_lifecycle import entry_refresh_in_progress
from .configuration import ConfigurationError, load_configuration
from .configuration_migration import convert_legacy_configuration


@callback
def migrate_entry_storage(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    """Validate before one HA update, without yielding or touching registries.

    Earlier previews must complete the existing version-3 migration first.
    The caller must stop old runtime jobs before changing the storage contract.
    Backups, startup and transport are separate from this configuration update.
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
    hass.config_entries.async_update_entry(
        entry,
        data=proposed.to_data(),
        options=proposed.to_options(),
        version=proposed.version,
        minor_version=proposed.minor_version,
    )
