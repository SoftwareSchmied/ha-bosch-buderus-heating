"""Home Assistant calendar of the events of an RRC 2.0 room controller.

Whole-day events from ``/events/list1`` to ``list3`` (``rrc_events``). Home
Assistant's end date is exclusive, the controller's inclusive. Dates count in
the controller's time zone (``/gateway/time/timeZone``), else in Home
Assistant's.

Writing (expected, untested on hardware): absences are created, changed and
deleted; weekday events are only deleted. Every write reads the lists first,
sends one request and reads them again until the change shows (S5 to S7).
While the outcome of one write is unknown, the next one reads the lists
again and is refused until the first shows (C36), so a creation after a
timeout is never sent twice.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Final, override

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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import rrc_events
from .const import DOMAIN
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource
from .holiday_timezones import async_resolve_holiday_timezones
from .holidays import HOLIDAY_TIMEZONE_PATH
from .i18n import localize
from .pointt import (
    AuthenticationError,
    PointTError,
    RateLimited,
    Resource,
    ResourceError,
    UnexpectedHttpStatus,
    WriteNotConfirmed,
    WriteValidationError,
)
from .rrc_contract_absence import WEEKDAY_DESCRIPTIONS, WEEKDAY_TITLES
from .rrc_events import EventFields, EventState, RrcEvent
from .rrc_write import RrcWritePending
from .writes import (
    ContractRejected,
    ControlLocked,
    RrcWriteNotConfirmed,
    WriteBusy,
)

TIMEZONE_PATH: Final = "/gateway/time/timeZone"
_FEATURES: Final = (
    CalendarEntityFeature.CREATE_EVENT
    | CalendarEntityFeature.UPDATE_EVENT
    | CalendarEntityFeature.DELETE_EVENT
)

type Operation = Callable[[], Awaitable[tuple[Resource, ...]]]


async def async_run_rrc_write(
    coordinator: BoschBuderusDataUpdateCoordinator,
    path: str,
    operation: Operation,
    *,
    require_snapshot: bool = True,
    late_check: Callable[[Resource], bool] | None = None,
) -> None:
    """Run one structured RRC write and raise only translated, value-free
    Home Assistant errors (like ``control.async_set_control``)."""
    try:
        await coordinator.async_run_rrc_operation(
            path, operation, require_snapshot=require_snapshot, late_check=late_check
        )
    except (ContractRejected, RrcWritePending) as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key=err.translation_key
        ) from err
    except ControlLocked as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="rrc_boost_locked"
        ) from err
    except WriteValidationError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="write_validation_failed"
        ) from err
    except RrcWriteNotConfirmed as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key=err.translation_key
        ) from err
    except WriteNotConfirmed as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_not_confirmed"
        ) from err
    except WriteBusy as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_busy"
        ) from err
    except AuthenticationError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_authentication_failed"
        ) from err
    except RateLimited as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_rate_limited"
        ) from err
    except PointTError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_failed"
        ) from err


async def async_read_lists(
    coordinator: BoschBuderusDataUpdateCoordinator, paths: tuple[str, ...]
) -> dict[str, Resource | None]:
    """Read the event lists in one bulk request; ``None`` for a list
    without a usable answer (P31: a controller may answer 500 at times)."""
    readings: dict[str, Resource | None] = dict.fromkeys(paths)
    results = await coordinator.client.get_resources_bulk(
        coordinator.gateway.gateway_id, paths
    )
    for item in results:
        if item.status == 429:
            raise RateLimited(retry_after=None)
        resource = item.resource
        if (
            item.path in readings
            and resource is not None
            and resource.path == item.path
        ):
            readings[item.path] = resource
    return readings


class RrcEventCalendar(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], CalendarEntity
):
    """The events of a room controller; absences can be written."""

    _attr_has_entity_name = True
    _attr_translation_key = "rrc_events"
    _attr_icon = "mdi:calendar-account"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:events:calendar"
        # The resolved controller time zone (one name; ``None`` if invalid).
        self._timezones: dict[str, tzinfo | None] = {}

    # ------------------------------------------------------------ reading
    @property
    def _known(self) -> tuple[str, ...]:
        return rrc_events.known_lists(self.coordinator.resources)

    @property
    def _state(self) -> EventState:
        data = self.coordinator.data or {}
        readings: dict[str, Resource | None] = {}
        for path in self._known:
            snapshot = data.get(path)
            readings[path] = (
                snapshot.resource
                if snapshot is not None
                and snapshot.available
                and snapshot.freshness is Freshness.FRESH
                else None
            )
        return rrc_events.parse_state(readings, self._known)

    @property
    def available(self) -> bool:
        return super().available and bool(self._state.lists)

    @property
    def supported_features(self) -> CalendarEntityFeature:
        """Writing only while every known list reads validly (no hidden
        event could overlap a new one)."""
        return _FEATURES if self._state.writable else CalendarEntityFeature(0)

    @property
    def event(self) -> CalendarEvent | None:
        timezone = self._timezone
        now = dt_util.now(timezone)
        return next(
            (
                self._calendar_event(item)
                for item in self._state.events
                if self._bounds(item, timezone)[1] > now
            ),
            None,
        )

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return the events overlapping the requested interval."""
        del hass
        timezone = self._timezone
        start_utc, end_utc = _as_utc(start_date), _as_utc(end_date)
        result: list[CalendarEvent] = []
        for item in self._state.events:
            begin, end = self._bounds(item, timezone)
            if end > start_utc and begin < end_utc:
                result.append(self._calendar_event(item))
        return result

    @staticmethod
    def _bounds(item: RrcEvent, timezone: tzinfo) -> tuple[datetime, datetime]:
        """The event from midnight of its first day to midnight after its
        last day, in the controller's time zone (daylight saving included)."""
        begin = datetime.combine(item.fields.start, time.min, timezone)
        end = datetime.combine(item.fields.end + timedelta(days=1), time.min, timezone)
        return begin, end

    def _calendar_event(self, item: RrcEvent) -> CalendarEvent:
        return CalendarEvent(
            start=item.fields.start,
            end=item.fields.end + timedelta(days=1),
            summary=self._summary(item),
            description=self._description(item),
            uid=_uid(item),
        )

    def _summary(self, item: RrcEvent) -> str:
        if (name := rrc_events.display_name(item.fields)) is not None:
            return name
        language = self.coordinator.hass.config.language
        if item.fields.kind == rrc_events.TYPE_AWAY:
            return localize(language, "Abwesenheit", "Away")
        return localize(language, *WEEKDAY_TITLES[item.fields.kind])

    def _description(self, item: RrcEvent) -> str | None:
        if item.fields.kind == rrc_events.TYPE_AWAY:
            return None
        return localize(
            self.coordinator.hass.config.language,
            *WEEKDAY_DESCRIPTIONS[item.fields.kind],
        )

    # ---------------------------------------------------------- time zone
    @property
    def _timezone(self) -> tzinfo:
        name = self._timezone_name()
        resolved = self._timezones.get(name) if name is not None else None
        return resolved or dt_util.get_default_time_zone()

    def _timezone_name(self) -> str | None:
        snapshot = (self.coordinator.data or {}).get(TIMEZONE_PATH)
        resource = (
            snapshot.resource
            if snapshot is not None
            else self.coordinator.resources.get(TIMEZONE_PATH)
        )
        if resource is None or not isinstance(resource.value, str):
            return None
        return resource.value.strip() or None

    async def _async_resolve_timezone(self) -> bool:
        """Resolve the controller's zone off the event loop's disk access;
        whether it changed."""
        name = self._timezone_name()
        if name is None or name in self._timezones:
            return False
        # The holiday helper validates the name before loading it.
        resolved = await async_resolve_holiday_timezones(
            {
                HOLIDAY_TIMEZONE_PATH: Resource(
                    HOLIDAY_TIMEZONE_PATH, value=name, has_value=True
                )
            },
            {},
        )
        self._timezones = {name: resolved.get(name)}
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_resolve_timezone()

    @callback
    def _handle_coordinator_update(self) -> None:
        name = self._timezone_name()
        if name is not None and name not in self._timezones and self.hass is not None:
            self.hass.async_create_task(
                self._async_timezone_changed(), eager_start=True
            )
        super()._handle_coordinator_update()

    async def _async_timezone_changed(self) -> None:
        if await self._async_resolve_timezone():
            self.async_write_ha_state()

    # ------------------------------------------------------------ writing
    @override
    async def async_create_event(self, **kwargs: Any) -> None:
        """Create one absence; nothing is sent twice."""

        async def operation() -> tuple[Resource, ...]:
            _check_fields(kwargs)
            start, end = _dates(kwargs[EVENT_START], kwargs[EVENT_END], self._timezone)
            fields = EventFields(
                rrc_events.encode_name(kwargs.get(EVENT_SUMMARY)),
                start,
                end,
                rrc_events.TYPE_AWAY,
            )
            rrc_events.check_dates(start, end, self._today)

            def check(state: EventState) -> frozenset[int]:
                rrc_events.check_overlap(fields, state)
                return state.identifiers()

            async def send(_before: frozenset[int]) -> None:
                await self.coordinator.client.post_resource(
                    self.coordinator.gateway.gateway_id,
                    rrc_events.EVENTS_ROOT,
                    fields.wire(),
                )

            def confirm(before: frozenset[int], state: EventState) -> bool:
                return rrc_events.created(before, fields, state) is not None

            return await self._async_structured(check, send, confirm)

        await self._async_run(operation)

    @override
    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Change the dates or the name of one absence."""
        del recurrence_id
        original = self._event_for_uid(uid)

        async def operation() -> tuple[Resource, ...]:
            if recurrence_range:
                raise ContractRejected(rrc_events.UNSUPPORTED_FIELDS)
            _check_fields(event)
            if original is None:
                raise WriteValidationError("The event is no longer listed")
            if not original.editable:
                raise ContractRejected(rrc_events.TYPE_READ_ONLY)
            start, end = _dates(event[EVENT_START], event[EVENT_END], self._timezone)
            summary = event.get(EVENT_SUMMARY)
            fields = EventFields(
                original.fields.name
                if summary == self._summary(original)
                else rrc_events.encode_name(summary),
                start,
                end,
                rrc_events.TYPE_AWAY,
            )
            rrc_events.check_dates(start, end, self._today)
            if fields == original.fields:
                return ()

            def check(state: EventState) -> None:
                _check_unchanged(original, state)
                rrc_events.check_overlap(fields, state, ignore=original.identifier)

            async def send(_prepared: None) -> None:
                await self.coordinator.client.put_resource_json(
                    self.coordinator.gateway.gateway_id, original.path, fields.wire()
                )

            def confirm(_prepared: None, state: EventState) -> bool:
                return rrc_events.changed(original, fields, state)

            return await self._async_structured(check, send, confirm)

        await self._async_run(operation)

    @override
    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete one event, absences and weekday events alike."""
        del recurrence_id
        original = self._event_for_uid(uid)

        async def operation() -> tuple[Resource, ...]:
            if recurrence_range:
                raise ContractRejected(rrc_events.UNSUPPORTED_FIELDS)
            if original is None:
                raise WriteValidationError("The event is no longer listed")

            def check(state: EventState) -> None:
                _check_unchanged(original, state)

            async def send(_prepared: None) -> None:
                await self.coordinator.client.delete_resource(
                    self.coordinator.gateway.gateway_id, original.path
                )

            def confirm(_prepared: None, state: EventState) -> bool:
                return rrc_events.deleted(original, state)

            return await self._async_structured(check, send, confirm)

        await self._async_run(operation)

    async def _async_run(self, operation: Operation) -> None:
        await async_run_rrc_write(
            self.coordinator,
            rrc_events.EVENTS_ROOT,
            operation,
            # A creation needs no reading of ``/events`` itself; the
            # preflight reads the lists.
            require_snapshot=False,
        )

    async def _async_structured[T](
        self,
        check: Callable[[EventState], T],
        send: Callable[[T], Awaitable[None]],
        confirm: Callable[[T, EventState], bool],
    ) -> tuple[Resource, ...]:
        """Fresh lists, one request, the lists again until the change shows.

        A 4xx answer to the request is the controller's refusal (for
        example a full calendar): ``rrc_event_rejected``.
        """
        coordinator = self.coordinator
        known = self._known
        refused: list[PointTError] = []

        async def read() -> tuple[dict[str, Resource | None], EventState]:
            readings = await async_read_lists(coordinator, known)
            return readings, rrc_events.parse_state(readings, known)

        async def preflight() -> tuple[T, EventState]:
            _readings, state = await read()
            if not state.writable:
                raise WriteValidationError("The events cannot be checked now")
            return check(state), state

        async def request(prepared: tuple[T, EventState]) -> None:
            try:
                await send(prepared[0])
            except (ResourceError, UnexpectedHttpStatus) as err:
                refused.append(err)
                raise

        async def confirmed(
            prepared: tuple[T, EventState],
        ) -> tuple[Resource, ...] | None:
            readings, state = await read()
            if state.unreadable or not confirm(prepared[0], state):
                return None
            return tuple(item for item in readings.values() if item is not None)

        try:
            return await coordinator.rrc_struct_writes.async_write(
                preflight=preflight,
                send=request,
                confirm=confirmed,
                guard=rrc_events.GUARD,
                pending_reason=rrc_events.PENDING,
                on_sent=lambda: coordinator.note_rrc_write_sent(rrc_events.EVENTS_ROOT),
            )
        except PointTError as err:
            if refused and err is refused[0]:
                raise ContractRejected(rrc_events.REJECTED) from err
            raise

    @property
    def _today(self) -> date:
        return dt_util.now(self._timezone).date()

    def _event_for_uid(self, uid: str) -> RrcEvent | None:
        return next((item for item in self._state.events if _uid(item) == uid), None)

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")


def _uid(item: RrcEvent) -> str:
    """C40: an ID may be reused for another event; a digest of the fields
    keeps an outdated UID from matching it. No name in clear text."""
    fields = item.fields
    digest = hashlib.sha256(
        "|".join(
            (fields.name, fields.start.isoformat(), fields.end.isoformat(), fields.kind)
        ).encode()
    ).hexdigest()[:12]
    return f"rrc-ev{item.identifier}-{digest}"


def _check_fields(event: Mapping[str, Any]) -> None:
    """Recurring events, descriptions and locations are not stored."""
    if (
        event.get(EVENT_RRULE)
        or event.get(EVENT_DESCRIPTION)
        or event.get(EVENT_LOCATION)
    ):
        raise ContractRejected(rrc_events.UNSUPPORTED_FIELDS)


def _check_unchanged(original: RrcEvent, state: EventState) -> None:
    """S5/C40: the event is still listed exactly as it was shown."""
    current = state.find(original.identifier)
    if current is None or not rrc_events.same_fields(current.fields, original.fields):
        raise WriteValidationError("The event changed; reopen the calendar")


def _dates(start: object, end: object, timezone: tzinfo) -> tuple[date, date]:
    """The inclusive dates of a whole-day event (Home Assistant's end is
    exclusive). Times are accepted only at midnight in the controller's
    time zone."""
    if type(start) is date and type(end) is date:
        first, after = start, end
    elif isinstance(start, datetime) and isinstance(end, datetime):
        begin = _local(start, timezone)
        finish = _local(end, timezone)
        if begin.time() != time.min or finish.time() != time.min:
            raise ContractRejected(rrc_events.INVALID_DATES)
        first, after = begin.date(), finish.date()
    else:
        raise ContractRejected(rrc_events.INVALID_DATES)
    last = after - timedelta(days=1)
    if last < first:
        raise ContractRejected(rrc_events.INVALID_DATES)
    return first, last


def _local(value: datetime, timezone: tzinfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
