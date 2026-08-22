"""Read-only Home Assistant calendar for PointT holiday periods."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschBuderusConfigEntry
from .coordinator import BoschBuderusDataUpdateCoordinator
from .device import device_info_for_resource
from .holidays import (
    HOLIDAY_PERIOD_PATHS,
    HOLIDAY_RESOURCE_PATHS,
    HolidayPeriod,
    HolidayState,
    parse_holiday_state,
)
from .pointt import Resource


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one read-only holiday calendar for every supporting gateway."""
    del hass
    async_add_entities(
        BoschBuderusHolidayCalendar(coordinator)
        for coordinator in entry.runtime_data.coordinators
        if any(path in coordinator.resources for path in HOLIDAY_PERIOD_PATHS)
    )


class BoschBuderusHolidayCalendar(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], CalendarEntity
):
    """Expose all valid PointT holiday periods without write features."""

    _attr_has_entity_name = True
    _attr_translation_key = "holiday_periods"
    _attr_icon = "mdi:calendar-account"
    _attr_supported_features = 0

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:holiday_periods"

    @property
    def available(self) -> bool:
        return super().available and any(
            path in self._available_resources for path in HOLIDAY_PERIOD_PATHS
        )

    @property
    def event(self) -> CalendarEvent | None:
        now = datetime.now(UTC)
        period = next((item for item in self._state.periods if item.end > now), None)
        return self._event_from_period(period) if period is not None else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return holiday periods overlapping the requested interval."""
        del hass
        start_utc = _as_utc(start_date)
        end_utc = _as_utc(end_date)
        return [
            self._event_from_period(period)
            for period in self._state.periods
            if _as_utc(period.end) > start_utc and _as_utc(period.start) < end_utc
        ]

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")

    @property
    def _available_resources(self) -> dict[str, Resource]:
        snapshots = self.coordinator.data or {}
        return {
            path: snapshot.resource
            for path in HOLIDAY_RESOURCE_PATHS
            if (snapshot := snapshots.get(path)) is not None and snapshot.available
        }

    @property
    def _state(self) -> HolidayState:
        return parse_holiday_state(
            self._available_resources,
            fallback_timezone=self.coordinator.hass.config.time_zone,
        )

    def _event_from_period(self, period: HolidayPeriod) -> CalendarEvent:
        language = self.coordinator.hass.config.language.casefold()
        summary = period.name or (
            "Urlaubszeit" if language.startswith("de") else "Holiday period"
        )
        fingerprint = "|".join(
            (
                period.start.isoformat(),
                period.end.isoformat(),
                period.identifier or "",
                period.name or "",
            )
        )
        return CalendarEvent(
            start=period.start.date() if period.all_day else period.start,
            end=period.end.date() if period.all_day else period.end,
            summary=summary,
            uid=hashlib.sha256(fingerprint.encode()).hexdigest()[:24],
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
