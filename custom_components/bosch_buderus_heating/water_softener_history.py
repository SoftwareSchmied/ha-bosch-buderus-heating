"""When a water softener last finished a regeneration (calculated).

The softener reports whether a regeneration is running
(``regeneration/enabled``), not when the last one ended. This sensor notes
the moment the integration sees that reading change from running to not
running and keeps it across restarts. It sends no request of its own and is
disabled by default.

The time is the one of the reading that showed the end, so it is as exact as
the polling of that reading. A regeneration that starts and ends while Home
Assistant is not running, or between two readings, is not noticed.
Expected, untested on a real softener.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Final

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .derived_inputs import read
from .device import device_info_for_resource, grouped_entity_name
from .i18n import localize
from .platform_scope import DiscoveryLifecycle
from .water_softener import softener_value

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator

KEY: Final = "calculated_last_regeneration"
_RUNNING: Final = re.compile(r"/swCircuits/[^/]+/regeneration/enabled")


def regeneration_paths(paths: object) -> list[str]:
    """The running-state readings of the softener circuits in a path set."""
    if not hasattr(paths, "__iter__"):
        return []
    return sorted(
        path for path in paths if isinstance(path, str) and _RUNNING.fullmatch(path)
    )


class SoftenerLastRegenerationSensor(
    CoordinatorEntity["BoschBuderusDataUpdateCoordinator"],
    RestoreEntity,
    SensorEntity,
):
    """The time a regeneration was last seen to end."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = frozenset({"derived_from"})

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, path: str
    ) -> None:
        super().__init__(coordinator)
        self.resource_path = path
        self._attr_translation_key = KEY
        key = path.strip("/").replace("/", ":")
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{key}:{KEY}"
        self._attr_name = grouped_entity_name(
            coordinator,
            path,
            localize(
                coordinator.hass.config.language,
                "Letzte Regeneration (berechnet)",
                "Last regeneration (calculated)",
            ),
        )
        self._value: datetime | None = None
        # The last reading of the running state (None: none seen yet).
        self._running: bool | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.resource_path)

    @property
    def available(self) -> bool:
        return self._value is not None

    @property
    def native_value(self) -> datetime | None:
        return self._value

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        if self._value is None:
            return None
        return {"derived_from": [self.resource_path]}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            parsed = dt_util.parse_datetime(last.state)
            if parsed is not None and parsed.tzinfo is not None:
                self._value = parsed
        self._observe()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._observe()
        super()._handle_coordinator_update()

    def _observe(self) -> None:
        """Note an end: a running reading followed by a not-running one. A
        reading that is missing, stale or unusable changes nothing."""
        reading = read(self.coordinator, self.resource_path)
        if reading is None or reading.resource is None:
            return
        running = softener_value(reading.resource)
        if not isinstance(running, bool):
            return
        if self._running is True and running is False:
            self._value = reading.observed_at or dt_util.utcnow()
        self._running = running

    async def async_update(self) -> None:
        """Calculated from a reading the integration reads anyway."""


def track_softener_history_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensor of each softener circuit once its reading exists."""
    seen: set[str] = set()

    @callback
    def discover() -> None:
        additions = []
        for path in regeneration_paths(getattr(coordinator, "resources", ())):
            if path not in seen:
                seen.add(path)
                additions.append(SoftenerLastRegenerationSensor(coordinator, path))
        if additions:
            async_add_entities(additions)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))
