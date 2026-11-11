"""F1: a read-only firmware update entity per cloud gateway.

The entity shows the installed gateway firmware and an update the cloud
offers, with a link to its release notes. It never installs: there is no
install feature, and no request confirms an update or changes the gateway's
automatic update setting. It is created with the first usable answer of the
firmware update list (``firmware_updates``); a gateway the list refuses or
does not name gets no entity.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschBuderusConfigEntry
from .coordinator import BoschBuderusDataUpdateCoordinator
from .device import device_info_for_resource
from .firmware_updates import FirmwareUpdateTracker, firmware_update_tracker
from .firmware_watch import firmware_values
from .platform_scope import DiscoveryLifecycle, PlatformScope, setup_platform
from .pointt.firmware_updates import FirmwareUpdates, release_url
from .resource_catalog import FIRMWARE_PATHS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_platform(entry, Platform.UPDATE, async_add_entities, _setup_entities)


def _setup_entities(
    entry: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for coordinator in entry.coordinators:
        _track_firmware_update(entry, coordinator, async_add_entities)


def _track_firmware_update(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Read the list while the gateway is shown; add the entity once the
    first usable answer arrives."""
    tracker = firmware_update_tracker(coordinator)
    entry.async_on_unload(tracker.attach())
    added = False

    @callback
    def discover() -> None:
        nonlocal added
        if added or tracker.shown() is None:
            return
        added = True
        async_add_entities([BoschBuderusFirmwareUpdate(coordinator, tracker)])

    discover()
    if not added:
        entry.async_on_unload(tracker.add_listener(discover))


class BoschBuderusFirmwareUpdate(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], UpdateEntity
):
    """Installed and offered gateway firmware; showing only."""

    _attr_has_entity_name = True
    _attr_translation_key = "firmware_update"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    # F1: no INSTALL, and no RELEASE_NOTES: the notes are only linked
    # (``release_url``), never fetched by the integration.
    _attr_supported_features = UpdateEntityFeature(0)

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        tracker: FirmwareUpdateTracker,
    ) -> None:
        super().__init__(coordinator)
        self._tracker = tracker
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:firmware_update"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._tracker.add_listener(self._tracker_updated))

    @callback
    def _tracker_updated(self) -> None:
        self.async_write_ha_state()

    @property
    def _shown(self) -> FirmwareUpdates | None:
        return self._tracker.shown()

    @property
    def available(self) -> bool:
        # Independent of the resource polling: the list has its own rounds.
        return self._shown is not None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")

    @property
    def installed_version(self) -> str | None:
        shown = self._shown
        if shown is not None and shown.current is not None and shown.current.version:
            return shown.current.version
        # Without a version in the list: the version the gateway reports.
        values = firmware_values(self.coordinator.resources)
        return next(
            (values[path] for path in FIRMWARE_PATHS if path in values),
            self.coordinator.gateway.firmware_version or None,
        )

    @property
    def latest_version(self) -> str | None:
        shown = self._shown
        offered = shown.offered_version if shown is not None else None
        return offered or self.installed_version

    @property
    def release_url(self) -> str | None:
        shown = self._shown
        if shown is None:
            return None
        return release_url(shown, self.hass.config.language)

    @property
    def auto_update(self) -> bool:
        shown = self._shown
        return bool(shown is not None and shown.auto_update_enabled)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        shown = self._shown
        return {
            "auto_update_enabled": shown.auto_update_enabled if shown else None,
            "auto_update_changeable": shown.auto_update_changeable if shown else None,
            "update_state": shown.update_state if shown else None,
        }
