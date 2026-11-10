"""Tests for the PointT holiday calendar."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.calendar.const import (
    EVENT_END,
    EVENT_LOCATION,
    EVENT_RRULE,
    EVENT_START,
    EVENT_SUMMARY,
    CalendarEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.bosch_buderus_heating.calendar import (
    BoschBuderusHolidayCalendar,
    async_setup_entry,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.holiday_writes import (
    HolidayAlreadyExists,
    HolidayWriteNotConfirmed,
)
from custom_components.bosch_buderus_heating.holidays import (
    HOLIDAY_ACTIVE_MODES_PATH,
    HOLIDAY_CONFIGURATION_PATH,
    HOLIDAY_LIST_PATH,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    WriteNotConfirmed,
)
from tests.ha_test_helpers import MockConfigEntry

CALENDAR_MODULE = "custom_components.bosch_buderus_heating.calendar"


def _uid(entity: BoschBuderusHolidayCalendar) -> str:
    """The calendar UID of the only holiday, ID 7."""
    (period,) = entity._state.periods
    return entity._uid_for_period(period)


def _coordinator(
    hass: HomeAssistant, resources: tuple[Resource, ...]
) -> BoschBuderusDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type="K40"), entry
    )
    coordinator.resources = {resource.path: resource for resource in resources}
    now = datetime.now(UTC)
    coordinator.data = {
        resource.path: ResourceSnapshot(resource, True, now) for resource in resources
    }
    coordinator.last_update_success = True
    return coordinator


async def test_calendar_exposes_multiple_overlapping_periods(
    hass: HomeAssistant,
) -> None:
    periods = Resource(
        path=HOLIDAY_LIST_PATH,
        value=[
            {
                "id": "one",
                "name": "First trip",
                "start": "2030-01-02T00:00:00Z",
                "end": "2030-01-05T00:00:00Z",
            },
            {
                "id": "two",
                "start": "2030-02-01T00:00:00Z",
                "end": "2030-02-10T00:00:00Z",
            },
        ],
        has_value=True,
    )
    coordinator = _coordinator(hass, (periods,))
    entity = BoschBuderusHolidayCalendar(coordinator)

    events = await entity.async_get_events(
        hass,
        datetime(2030, 1, 3, tzinfo=UTC),
        datetime(2030, 3, 1, tzinfo=UTC),
    )

    assert entity.available
    assert entity.supported_features == 0
    assert entity.unique_id == "gateway-one:holiday_periods"
    assert entity.device_info["model"] == "Heating system"
    assert [event.summary for event in events] == ["First trip", "Holiday period"]
    assert len({event.uid for event in events}) == 2
    assert entity.event is not None

    naive_events = await entity.async_get_events(
        hass,
        datetime(2030, 1, 1),
        datetime(2030, 1, 6),
    )
    assert len(naive_events) == 1

    entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinators=(coordinator,)))
    added: list[object] = []
    # Independent of the room controller trackers of the calendar platform.
    with patch(f"{CALENDAR_MODULE}.rrc_platforms.track"):
        await async_setup_entry(hass, entry, added.extend)
    assert len(added) == 1
    assert isinstance(added[0], BoschBuderusHolidayCalendar)


async def test_calendar_setup_requires_a_period_resource(
    hass: HomeAssistant,
) -> None:
    active_modes = Resource(path=HOLIDAY_ACTIVE_MODES_PATH, value=[], has_value=True)
    coordinator = _coordinator(hass, (active_modes,))
    unloads: list[object] = []
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinators=(coordinator,)),
        async_on_unload=unloads.append,
    )
    added: list[object] = []

    with patch(f"{CALENDAR_MODULE}.rrc_platforms.track"):
        await async_setup_entry(hass, entry, added.extend)

    assert added == []
    assert len(unloads) == 1  # waits for holiday periods found later
    for remove in unloads:
        remove()


def _writable_holiday_resources() -> tuple[Resource, ...]:
    periods = Resource(
        path=HOLIDAY_LIST_PATH,
        value=[
            {
                "id": 7,
                "startDate": "2030-08-01T08:00:00",
                "endDate": "2030-08-08T18:00:00",
                "heatingMode": "ECO",
                "dhwMode": "LOW",
                "ventilationMode": "NOM",
                "assignedTo": ["hc1", "dhw1", "vent1"],
                "name": "VGVzdA==",
                "thermalDesinfection": "OFF",
                "fixTemperature": 16.5,
            }
        ],
        has_value=True,
    )
    configuration = Resource(
        path=HOLIDAY_CONFIGURATION_PATH,
        value={
            "values": {
                "date": {"allowedValues": ["dateTime"]},
                "heatingMode": {"allowedValues": ["OFF", "ECO", "FIX_TEMPERATURE"]},
                "dhwMode": {"allowedValues": ["OFF", "LOW"]},
                "ventilationMode": {"allowedValues": ["OFF", "NOM"]},
                "assignedTo": {"allowedValues": ["hc1", "dhw1", "vent1"]},
                "thermalDesinfection": {"allowedValues": ["ON", "OFF"]},
                "fixTemperature": {"minValue": 10.0, "maxValue": 25.0},
                "name": {
                    "stringConfig": {
                        "codingType": "BASE64",
                        "charset": "UTF8",
                        "maxLength": 32,
                    }
                },
            }
        },
        has_value=True,
    )
    return periods, configuration


async def test_writable_calendar_creates_updates_and_deletes(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, _writable_holiday_resources())
    coordinator.async_create_holiday = AsyncMock()
    coordinator.async_update_holiday = AsyncMock()
    coordinator.async_delete_holiday = AsyncMock()
    entity = BoschBuderusHolidayCalendar(coordinator)

    assert entity.supported_features == (
        CalendarEntityFeature.CREATE_EVENT
        | CalendarEntityFeature.UPDATE_EVENT
        | CalendarEntityFeature.DELETE_EVENT
    )
    events = await entity.async_get_events(
        hass,
        datetime(2030, 1, 1, tzinfo=UTC),
        datetime(2031, 1, 1, tzinfo=UTC),
    )
    assert re.fullmatch(r"pointt-7-[0-9a-f]{8}", events[0].uid)
    assert events[0].uid == _uid(entity)
    assert events[0].summary == "Test"

    await entity.async_create_event(
        **{
            EVENT_START: date(2030, 9, 1),
            EVENT_END: date(2030, 9, 8),
            EVENT_SUMMARY: "New trip",
        }
    )
    created = coordinator.async_create_holiday.await_args.args[0]
    assert created.start_date == "2030-09-01T00:00:00"
    assert created.end_date == "2030-09-08T00:00:00"
    assert created.heating_mode == "FIX_TEMPERATURE"

    await entity.async_update_event(
        _uid(entity),
        {
            EVENT_START: datetime(2030, 8, 2, 9, tzinfo=UTC),
            EVENT_END: datetime(2030, 8, 9, 19, tzinfo=UTC),
            EVENT_SUMMARY: "Test",
        },
        recurrence_id="ordinary-event-instance",
        recurrence_range="",
    )
    holiday_id, updated = coordinator.async_update_holiday.await_args.args
    baseline = coordinator.async_update_holiday.await_args.kwargs["expected"]
    assert baseline == entity._period_for_uid(_uid(entity)).write_values
    assert baseline.start_date != updated.start_date
    assert holiday_id == 7
    assert updated.heating_mode == "ECO"
    assert updated.dhw_mode == "LOW"
    assert updated.ventilation_mode == "NOM"
    assert updated.assigned_to == ("hc1", "dhw1", "vent1")
    assert updated.thermal_disinfection == "OFF"
    assert updated.fix_temperature == 16.5

    await entity.async_delete_event(
        _uid(entity),
        recurrence_id="ordinary-event-instance",
        recurrence_range="",
    )
    # C40: the calendar checks the shown holiday before deleting, as the
    # options dialog does.
    coordinator.async_delete_holiday.assert_awaited_once_with(
        7, expected=entity._state.periods[0].write_values
    )


@pytest.mark.parametrize(
    ("operation", "outcome", "key"),
    [
        ("create", "duplicate", "holiday_already_exists"),
        ("create", "previous", "holiday_previous_request_found"),
        ("create", "adjusted", "holiday_write_adjusted"),
        ("update", "adjusted", "holiday_write_adjusted"),
        ("create", "unknown", "holiday_create_unconfirmed"),
        ("update", "unknown", "holiday_update_unconfirmed"),
    ],
)
async def test_calendar_explains_unconfirmed_holiday_writes(
    hass, operation, outcome, key
):
    coordinator = _coordinator(hass, _writable_holiday_resources())
    entity = BoschBuderusHolidayCalendar(coordinator)
    resource = coordinator.data[HOLIDAY_LIST_PATH].resource
    error = (
        HolidayAlreadyExists(resource, previous_request=outcome == "previous")
        if outcome in ("duplicate", "previous")
        else HolidayWriteNotConfirmed(
            resource, actual=entity._state.periods[0] if outcome == "adjusted" else None
        )
    )
    writer = AsyncMock(side_effect=error)
    setattr(coordinator, f"async_{operation}_holiday", writer)
    event = {
        EVENT_START: date(2030, 9, 1),
        EVENT_END: date(2030, 9, 8),
        EVENT_SUMMARY: "Test",
    }
    with pytest.raises(HomeAssistantError) as caught:
        if operation == "create":
            await entity.async_create_event(**event)
        else:
            await entity.async_update_event(_uid(entity), event)
    assert caught.value.translation_key == key
    writer.assert_awaited_once()


async def test_calendar_rejects_unsupported_fields(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass, _writable_holiday_resources())
    coordinator.async_create_holiday = AsyncMock()
    entity = BoschBuderusHolidayCalendar(coordinator)

    with pytest.raises(HomeAssistantError, match="descriptions or locations"):
        await entity.async_create_event(
            **{
                EVENT_START: date(2030, 9, 1),
                EVENT_END: date(2030, 9, 8),
                EVENT_SUMMARY: "New trip",
                EVENT_LOCATION: "Private",
            }
        )

    coordinator.async_create_holiday.assert_not_awaited()

    event = {
        EVENT_START: date(2030, 9, 1),
        EVENT_END: date(2030, 9, 8),
        EVENT_SUMMARY: "New trip",
    }

    with pytest.raises(HomeAssistantError, match="Recurring"):
        await entity.async_update_event(
            _uid(entity),
            event,
            recurrence_range="THIS_AND_FUTURE",
        )

    with pytest.raises(HomeAssistantError, match="Recurring"):
        await entity.async_update_event(
            _uid(entity),
            {**event, EVENT_RRULE: "FREQ=YEARLY"},
        )


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize(
    ("start", "reason"),
    [
        (datetime(2026, 3, 29, 2, 30), "holiday_time_nonexistent"),
        (datetime(2026, 10, 25, 2, 30), "holiday_time_ambiguous"),
        (datetime(2026, 9, 20, 2, 38), "holiday_time_step"),
        (datetime(2026, 9, 20, 2, 45, 1), "holiday_time_step"),
    ],
)
async def test_calendar_rejects_invalid_wall_times_without_writing(
    hass, operation, start, reason
):
    await hass.config.async_set_time_zone("Europe/Berlin")
    coordinator = _coordinator(hass, _writable_holiday_resources())
    coordinator.async_create_holiday = AsyncMock()
    coordinator.async_update_holiday = AsyncMock()
    entity = BoschBuderusHolidayCalendar(coordinator)
    event = {
        EVENT_START: start,
        EVENT_END: start.replace(hour=5, minute=0),
        EVENT_SUMMARY: "Trip",
    }
    with pytest.raises(HomeAssistantError) as caught:
        if operation == "create":
            await entity.async_create_event(**event)
        else:
            await entity.async_update_event(_uid(entity), event)
    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == reason
    coordinator.async_create_holiday.assert_not_awaited()
    coordinator.async_update_holiday.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("end_day", [27, 28])
async def test_calendar_explains_an_end_that_does_not_follow_the_start(
    hass, operation, end_day
):
    coordinator = _coordinator(hass, _writable_holiday_resources())
    coordinator.async_create_holiday = AsyncMock()
    coordinator.async_update_holiday = AsyncMock()
    entity = BoschBuderusHolidayCalendar(coordinator)
    event = {
        EVENT_START: datetime(2030, 10, 28, 8),
        EVENT_END: datetime(2030, 10, end_day, 8),
        EVENT_SUMMARY: "Trip",
    }
    with pytest.raises(HomeAssistantError) as caught:
        if operation == "create":
            await entity.async_create_event(**event)
        else:
            await entity.async_update_event(_uid(entity), event)
    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "holiday_end_not_after_start"
    coordinator.async_create_holiday.assert_not_awaited()
    coordinator.async_update_holiday.assert_not_awaited()


async def test_calendar_reports_unavailable_and_failed_writes(
    hass: HomeAssistant,
) -> None:
    read_only = Resource(path=HOLIDAY_LIST_PATH, value=[], has_value=True)
    entity = BoschBuderusHolidayCalendar(_coordinator(hass, (read_only,)))

    with pytest.raises(HomeAssistantError, match="does not currently allow"):
        await entity.async_create_event(
            **{
                EVENT_START: date(2030, 9, 1),
                EVENT_END: date(2030, 9, 8),
                EVENT_SUMMARY: "New trip",
            }
        )
    with pytest.raises(HomeAssistantError, match="does not currently allow"):
        await entity.async_delete_event("unknown")

    coordinator = _coordinator(hass, _writable_holiday_resources())
    coordinator.async_create_holiday = AsyncMock(side_effect=WriteNotConfirmed())
    coordinator.async_update_holiday = AsyncMock(side_effect=WriteNotConfirmed())
    coordinator.async_delete_holiday = AsyncMock(side_effect=WriteNotConfirmed())
    entity = BoschBuderusHolidayCalendar(coordinator)
    event = {
        EVENT_START: date(2030, 9, 1),
        EVENT_END: date(2030, 9, 8),
        EVENT_SUMMARY: "New trip",
    }

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_create_event(**event)
    assert error.value.translation_key == "holiday_create_unconfirmed"
    with pytest.raises(HomeAssistantError) as error:
        await entity.async_update_event(_uid(entity), event)
    assert error.value.translation_key == "holiday_update_unconfirmed"
    with pytest.raises(HomeAssistantError, match="deleted or confirmed"):
        await entity.async_delete_event(_uid(entity))

    with pytest.raises(HomeAssistantError, match="no writable PointT ID"):
        await entity.async_delete_event("unknown")


async def test_reused_holiday_id_does_not_match_an_outdated_event(
    hass: HomeAssistant,
) -> None:
    # C40: the app replaced holiday 7 by another one with the same ID.
    coordinator = _coordinator(hass, _writable_holiday_resources())
    coordinator.async_update_holiday = AsyncMock()
    coordinator.async_delete_holiday = AsyncMock()
    entity = BoschBuderusHolidayCalendar(coordinator)
    outdated = _uid(entity)
    snapshot = coordinator.data[HOLIDAY_LIST_PATH]
    replaced = dict(snapshot.resource.value[0])
    replaced.update(startDate="2030-12-20T08:00:00", endDate="2030-12-27T18:00:00")
    coordinator.data[HOLIDAY_LIST_PATH] = ResourceSnapshot(
        Resource(HOLIDAY_LIST_PATH, value=[replaced], has_value=True),
        True,
        datetime.now(UTC),
    )
    assert _uid(entity) != outdated and _uid(entity).startswith("pointt-7-")
    event = {
        EVENT_START: date(2030, 9, 1),
        EVENT_END: date(2030, 9, 8),
        EVENT_SUMMARY: "Old trip",
    }
    with pytest.raises(HomeAssistantError, match="cannot currently be changed"):
        await entity.async_update_event(outdated, event)
    with pytest.raises(HomeAssistantError, match="no writable PointT ID"):
        await entity.async_delete_event(outdated)
    coordinator.async_update_holiday.assert_not_awaited()
    coordinator.async_delete_holiday.assert_not_awaited()


async def test_calendar_delete_of_a_holiday_changed_meanwhile_sends_nothing(
    hass: HomeAssistant,
) -> None:
    # C40: between the last poll and the deletion, the app changed holiday 7.
    from custom_components.bosch_buderus_heating.holiday_writes import (
        HolidayWriteService,
    )

    coordinator = _coordinator(hass, _writable_holiday_resources())
    client = coordinator.client
    coordinator._holiday_write_service = HolidayWriteService(client, sleep=AsyncMock())
    live = dict(coordinator.data[HOLIDAY_LIST_PATH].resource.value[0])
    live["endDate"] = "2030-08-10T18:00:00"
    client.get_resource.return_value = Resource(
        HOLIDAY_LIST_PATH, value=[live], has_value=True
    )
    entity = BoschBuderusHolidayCalendar(coordinator)
    with pytest.raises(HomeAssistantError, match="deleted or confirmed"):
        await entity.async_delete_event(_uid(entity))
    client.delete_holiday_period.assert_not_awaited()
    client.get_resource.assert_awaited_once_with("gateway-one", HOLIDAY_LIST_PATH)

    # Unchanged, the same deletion is sent exactly once and confirmed.
    client.get_resource.reset_mock()
    current = coordinator.data[HOLIDAY_LIST_PATH].resource
    client.get_resource.side_effect = [
        current,
        Resource(HOLIDAY_LIST_PATH, value=[], has_value=True),
    ]
    await entity.async_delete_event(_uid(entity))
    client.delete_holiday_period.assert_awaited_once_with("gateway-one", 7)


async def test_calendar_reports_a_busy_gateway_without_writing(
    hass: HomeAssistant,
) -> None:
    from unittest.mock import patch

    from custom_components.bosch_buderus_heating import coordinator as module

    coordinator = _coordinator(hass, _writable_holiday_resources())
    entity = BoschBuderusHolidayCalendar(coordinator)
    uid = _uid(entity)
    event = {
        EVENT_START: date(2030, 9, 1),
        EVENT_END: date(2030, 9, 8),
        EVENT_SUMMARY: "Trip",
    }
    with patch.object(module, "WRITE_LOCK_TIMEOUT", module.timedelta(0)):
        async with coordinator._update_lock:
            for attempt in (
                entity.async_create_event(**event),
                entity.async_update_event(uid, event),
                entity.async_delete_event(uid),
            ):
                with pytest.raises(HomeAssistantError) as error:
                    await attempt
                assert error.value.translation_key == "write_busy"
    client = coordinator.client
    client.create_holiday_period.assert_not_awaited()
    client.update_holiday_period.assert_not_awaited()
    client.delete_holiday_period.assert_not_awaited()
    client.get_resource.assert_not_awaited()
