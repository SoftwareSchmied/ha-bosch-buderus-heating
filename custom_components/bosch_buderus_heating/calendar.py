"""Home Assistant calendar for PointT holiday periods."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, tzinfo
from typing import Any, override

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.components.calendar.const import (
    EVENT_DESCRIPTION,
    EVENT_END,
    EVENT_LOCATION,
    EVENT_RRULE,
    EVENT_START,
    EVENT_SUMMARY,
    CalendarEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschBuderusConfigEntry
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource
from .holiday_writes import create_holiday_values, update_holiday_values
from .holidays import (
    HOLIDAY_PERIOD_PATHS,
    HOLIDAY_RESOURCE_PATHS,
    HOLIDAY_TIMEZONE_PATH,
    HolidayPeriod,
    HolidayState,
    holiday_period_id,
    holiday_timezone,
    parse_holiday_state,
    parse_holiday_write_configuration,
)
from .pointt import PointTError, Resource


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one holiday calendar for every supporting gateway."""
    del hass
    async_add_entities(
        BoschBuderusHolidayCalendar(coordinator)
        for coordinator in entry.runtime_data.coordinators
        if any(path in coordinator.resources for path in HOLIDAY_PERIOD_PATHS)
    )


class BoschBuderusHolidayCalendar(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], CalendarEntity
):
    """Expose valid PointT holiday periods and capability-gated writes."""

    _attr_has_entity_name = True
    _attr_translation_key = "holiday_periods"
    _attr_icon = "mdi:calendar-account"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:holiday_periods"

    @property
    def available(self) -> bool:
        return super().available and any(
            path in self._available_resources for path in HOLIDAY_PERIOD_PATHS
        )

    @property
    def supported_features(self) -> CalendarEntityFeature:
        """Expose writes only while all required capabilities are current."""
        if parse_holiday_write_configuration(self._available_resources) is None:
            return CalendarEntityFeature(0)
        return (
            CalendarEntityFeature.CREATE_EVENT
            | CalendarEntityFeature.UPDATE_EVENT
            | CalendarEntityFeature.DELETE_EVENT
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

    @override
    async def async_create_event(self, **kwargs: Any) -> None:
        """Create a PointT holiday from a Home Assistant calendar event."""
        self._validate_event_fields(kwargs)
        configuration = parse_holiday_write_configuration(self._available_resources)
        if configuration is None:
            raise HomeAssistantError(
                "This heating system does not currently allow holiday changes"
            )
        try:
            values = create_holiday_values(
                kwargs[EVENT_START],
                kwargs[EVENT_END],
                kwargs[EVENT_SUMMARY],
                configuration,
                self._timezone,
            )
            await self.coordinator.async_create_holiday(values)
        except (KeyError, PointTError, TypeError, ValueError) as err:
            raise HomeAssistantError(
                "The holiday could not be created or confirmed"
            ) from err

    @override
    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Update dates and name while preserving all PointT mode settings."""
        self._validate_recurrence(recurrence_id, recurrence_range)
        self._validate_event_fields(event)
        configuration = parse_holiday_write_configuration(self._available_resources)
        period = self._period_for_uid(uid)
        holiday_id = holiday_period_id(period) if period is not None else None
        if configuration is None or period is None or holiday_id is None:
            raise HomeAssistantError("This holiday cannot currently be changed safely")
        try:
            values = update_holiday_values(
                period,
                event[EVENT_START],
                event[EVENT_END],
                event[EVENT_SUMMARY],
                configuration,
                self._timezone,
                change_name=event[EVENT_SUMMARY] != self._summary_for_period(period),
            )
            await self.coordinator.async_update_holiday(
                holiday_id, values, expected=period.write_values
            )
        except (KeyError, PointTError, TypeError, ValueError) as err:
            raise HomeAssistantError(
                "The holiday could not be changed or confirmed"
            ) from err

    @override
    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete one PointT holiday by its validated numeric identifier."""
        self._validate_recurrence(recurrence_id, recurrence_range)
        if parse_holiday_write_configuration(self._available_resources) is None:
            raise HomeAssistantError(
                "This heating system does not currently allow holiday changes"
            )
        period = self._period_for_uid(uid)
        holiday_id = holiday_period_id(period) if period is not None else None
        if holiday_id is None:
            raise HomeAssistantError("This holiday has no writable PointT ID")
        try:
            await self.coordinator.async_delete_holiday(holiday_id)
        except PointTError as err:
            raise HomeAssistantError(
                "The holiday could not be deleted or confirmed"
            ) from err

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")

    @property
    def _available_resources(self) -> dict[str, Resource]:
        snapshots = self.coordinator.data or {}
        return {
            path: snapshot.resource
            for path in (*HOLIDAY_RESOURCE_PATHS, HOLIDAY_TIMEZONE_PATH)
            if (snapshot := snapshots.get(path)) is not None
            and snapshot.available
            and snapshot.freshness is Freshness.FRESH
        }

    @property
    def _state(self) -> HolidayState:
        return parse_holiday_state(
            self._available_resources,
            fallback_timezone=self.coordinator.hass.config.time_zone,
        )

    @property
    def _timezone(self) -> tzinfo:
        return holiday_timezone(
            self._available_resources,
            self.coordinator.hass.config.time_zone,
        )

    def _period_for_uid(self, uid: str) -> HolidayPeriod | None:
        return next(
            (
                period
                for period in self._state.periods
                if self._uid_for_period(period) == uid
            ),
            None,
        )

    @staticmethod
    def _validate_recurrence(
        recurrence_id: str | None, recurrence_range: str | None
    ) -> None:
        # Home Assistant may provide this ID for ordinary calendar events.
        del recurrence_id

        # The HA frontend sends an empty string for "this event", including
        # ordinary non-recurring events edited or dragged in the calendar.
        if recurrence_range:
            raise HomeAssistantError("Recurring holidays are not supported")

    @staticmethod
    def _validate_event_fields(event: dict[str, Any]) -> None:
        if event.get(EVENT_RRULE):
            raise HomeAssistantError("Recurring holidays are not supported")
        if event.get(EVENT_DESCRIPTION) or event.get(EVENT_LOCATION):
            raise HomeAssistantError(
                "PointT holidays do not support descriptions or locations"
            )

    def _event_from_period(self, period: HolidayPeriod) -> CalendarEvent:
        return CalendarEvent(
            start=period.start.date() if period.all_day else period.start,
            end=period.end.date() if period.all_day else period.end,
            summary=self._summary_for_period(period),
            uid=self._uid_for_period(period),
        )

    def _summary_for_period(self, period: HolidayPeriod) -> str:
        if period.name is not None:
            return period.name
        language = self.coordinator.hass.config.language.casefold()
        return "Urlaubszeit" if language.startswith("de") else "Holiday period"

    @staticmethod
    def _uid_for_period(period: HolidayPeriod) -> str:
        if (holiday_id := holiday_period_id(period)) is not None:
            return f"pointt-{holiday_id}"
        fingerprint = "|".join(
            (
                period.start.isoformat(),
                period.end.isoformat(),
                period.identifier or "",
                period.name or "",
            )
        )
        return f"read-only-{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
