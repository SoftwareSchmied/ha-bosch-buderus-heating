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
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import BoschBuderusConfigEntry, rrc_platforms
from .const import DOMAIN
from .control import track_entity_once
from .coordinator import BoschBuderusDataUpdateCoordinator
from .device import device_info_for_resource
from .holiday_writes import (
    HolidayAlreadyExists,
    HolidayTimeError,
    HolidayTimeRangeError,
    HolidayTimeStepError,
    HolidayWriteNotConfirmed,
    create_holiday_values,
    holiday_resources_from_snapshots,
    update_holiday_values,
)
from .holidays import (
    HolidayPeriod,
    HolidayState,
    holiday_period_id,
    holiday_timezone,
    is_holiday_period_path,
    parse_holiday_state,
    parse_holiday_write_configuration,
)
from .i18n import localize
from .platform_scope import DiscoveryLifecycle, PlatformScope, setup_platform
from .pointt import PointTError, Resource, WriteNotConfirmed
from .writes import WriteBusy


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_platform(entry, Platform.CALENDAR, async_add_entities, _setup_entities)


def _setup_entities(
    entry: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one holiday calendar for every supporting gateway.

    A gateway whose holiday resources are found later, for example by the
    search for parts missing at start, gets its calendar then, exactly once.
    """
    for coordinator in entry.coordinators:
        _track_holiday_calendar(entry, coordinator, async_add_entities)
        rrc_platforms.track(Platform.CALENDAR, entry, coordinator, async_add_entities)


def _track_holiday_calendar(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    track_entity_once(
        entry,
        coordinator,
        async_add_entities,
        lambda: any(is_holiday_period_path(path) for path in coordinator.resources),
        lambda: BoschBuderusHolidayCalendar(coordinator),
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
        # The periods are shown like any other reading: within their validity
        # limit also while a cloud cycle fails. Changes need current readings
        # (``supported_features``).
        return any(is_holiday_period_path(path) for path in self._shown_resources)

    @property
    def supported_features(self) -> CalendarEntityFeature:
        """Expose writes only while all required capabilities are current."""
        resources = self._available_resources
        result = CalendarEntityFeature(0)
        if not super().available:
            return result
        if parse_holiday_write_configuration(resources) is not None:
            result |= CalendarEntityFeature.CREATE_EVENT
        configurations = [
            configuration
            for period in self._state.periods
            if (
                configuration := parse_holiday_write_configuration(
                    resources, source_path=period.source_path
                )
            )
            is not None
        ]
        # Modern calendars have a collection-wide contract even when empty.
        modern = parse_holiday_write_configuration(
            resources, source_path="/holidayMode"
        )
        if modern is not None or configurations:
            result |= CalendarEntityFeature.UPDATE_EVENT
        if modern is not None or any(item.can_delete for item in configurations):
            result |= CalendarEntityFeature.DELETE_EVENT
        return result

    @property
    def event(self) -> CalendarEvent | None:
        now = datetime.now(UTC)
        period = next(
            (item for item in self._shown_state.periods if item.end > now), None
        )
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
            for period in self._shown_state.periods
            if _as_utc(period.end) > start_utc and _as_utc(period.start) < end_utc
        ]

    @override
    async def async_create_event(self, **kwargs: Any) -> None:
        """Create a PointT holiday from a Home Assistant calendar event."""
        self._validate_event_fields(kwargs)
        configuration = parse_holiday_write_configuration(self._available_resources)
        if configuration is None:
            raise _error("holiday_changes_unavailable")
        try:
            values = create_holiday_values(
                kwargs[EVENT_START],
                kwargs[EVENT_END],
                kwargs[EVENT_SUMMARY],
                configuration,
                self._timezone,
            )
            await self.coordinator.async_create_holiday(values)
        except HolidayAlreadyExists as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="holiday_previous_request_found"
                if err.previous_request
                else "holiday_already_exists",
            ) from err
        except WriteNotConfirmed as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="holiday_write_adjusted"
                if isinstance(err, HolidayWriteNotConfirmed) and err.actual is not None
                else "holiday_create_unconfirmed",
            ) from err
        except (HolidayTimeError, HolidayTimeRangeError, HolidayTimeStepError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key=err.translation_key
            ) from err
        except WriteBusy as err:
            raise _busy() from err
        except (KeyError, PointTError, TypeError, ValueError) as err:
            raise _error("holiday_create_failed") from err

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
        period = self._period_for_uid(uid)
        configuration = (
            parse_holiday_write_configuration(
                self._available_resources, source_path=period.source_path
            )
            if period is not None
            else None
        )
        holiday_id = holiday_period_id(period) if period is not None else None
        if configuration is None or period is None or holiday_id is None:
            raise _error("holiday_change_unsafe")
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
        except WriteNotConfirmed as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="holiday_write_adjusted"
                if isinstance(err, HolidayWriteNotConfirmed) and err.actual is not None
                else "holiday_update_unconfirmed",
            ) from err
        except (HolidayTimeError, HolidayTimeRangeError, HolidayTimeStepError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key=err.translation_key
            ) from err
        except WriteBusy as err:
            raise _busy() from err
        except (KeyError, PointTError, TypeError, ValueError) as err:
            raise _error("holiday_update_failed") from err

    @override
    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete one PointT holiday by its validated numeric identifier."""
        self._validate_recurrence(recurrence_id, recurrence_range)
        period = self._period_for_uid(uid)
        configuration = (
            parse_holiday_write_configuration(
                self._available_resources, source_path=period.source_path
            )
            if period is not None
            else parse_holiday_write_configuration(self._available_resources)
        )
        if configuration is None or not configuration.can_delete:
            raise _error("holiday_changes_unavailable")
        holiday_id = holiday_period_id(period) if period is not None else None
        if holiday_id is None:
            raise _error("holiday_not_deletable")
        try:
            # C40: like the options dialog, delete only the holiday this
            # event showed; the gateway's current entry is compared first.
            await self.coordinator.async_delete_holiday(
                holiday_id,
                expected=period.write_values if period is not None else None,
            )
        except WriteBusy as err:
            raise _busy() from err
        except PointTError as err:
            raise _error("holiday_delete_failed") from err

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")

    @property
    def _available_resources(self) -> dict[str, Resource]:
        """Current readings: they decide every change."""
        return holiday_resources_from_snapshots(self.coordinator.data or {})

    @property
    def _shown_resources(self) -> dict[str, Resource]:
        """The readings shown, within their validity limit."""
        return self.coordinator.displayed_holiday_resources()

    @property
    def _state(self) -> HolidayState:
        return parse_holiday_state(
            self._available_resources,
            fallback_timezone=dt_util.get_default_time_zone(),
            timezones=self.coordinator.holiday_timezones,
        )

    @property
    def _shown_state(self) -> HolidayState:
        return parse_holiday_state(
            self._shown_resources,
            fallback_timezone=dt_util.get_default_time_zone(),
            timezones=self.coordinator.holiday_timezones,
        )

    @property
    def _timezone(self) -> tzinfo:
        return holiday_timezone(
            self._available_resources,
            dt_util.get_default_time_zone(),
            timezones=self.coordinator.holiday_timezones,
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
            raise _error("holiday_recurring_unsupported")

    @staticmethod
    def _validate_event_fields(event: dict[str, Any]) -> None:
        if event.get(EVENT_RRULE):
            raise _error("holiday_recurring_unsupported")
        if event.get(EVENT_DESCRIPTION) or event.get(EVENT_LOCATION):
            raise _error("holiday_fields_unsupported")

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
        return localize(
            self.coordinator.hass.config.language, "Urlaubszeit", "Holiday period"
        )

    @staticmethod
    def _uid_for_period(period: HolidayPeriod) -> str:
        if (holiday_id := holiday_period_id(period)) is not None:
            if period.source_path != "/holidayMode":
                return f"icom-hm{holiday_id}"
            # C40: the numeric ID may be reused for another holiday. A short
            # digest of the timespan keeps an outdated UID from matching it.
            timespan = f"{period.start.isoformat()}|{period.end.isoformat()}"
            digest = hashlib.sha256(timespan.encode()).hexdigest()[:8]
            return f"pointt-{holiday_id}-{digest}"
        fingerprint = "|".join(
            (
                period.start.isoformat(),
                period.end.isoformat(),
                period.identifier or "",
                period.name or "",
            )
        )
        return f"read-only-{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}"


def _busy() -> HomeAssistantError:
    return _error("write_busy")


def _error(key: str) -> HomeAssistantError:
    """A translated message; the text follows the user's language."""
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=key)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
