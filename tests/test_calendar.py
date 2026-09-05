"""Tests for the PointT holiday calendar."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.calendar import (
    BoschBuderusHolidayCalendar,
    async_setup_entry,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
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
    await async_setup_entry(hass, entry, added.extend)
    assert len(added) == 1
    assert isinstance(added[0], BoschBuderusHolidayCalendar)


async def test_calendar_setup_requires_a_period_resource(
    hass: HomeAssistant,
) -> None:
    active_modes = Resource(path=HOLIDAY_ACTIVE_MODES_PATH, value=[], has_value=True)
    coordinator = _coordinator(hass, (active_modes,))
    entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinators=(coordinator,)))
    added: list[object] = []

    await async_setup_entry(hass, entry, added.extend)

    assert added == []


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
    assert events[0].uid == "pointt-7"
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
    assert created.end_date == "2030-09-07T24:00:00"
    assert created.heating_mode == "FIX_TEMPERATURE"

    await entity.async_update_event(
        "pointt-7",
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
    assert baseline == entity._period_for_uid("pointt-7").write_values
    assert baseline.start_date != updated.start_date
    assert holiday_id == 7
    assert updated.heating_mode == "ECO"
    assert updated.dhw_mode == "LOW"
    assert updated.ventilation_mode == "NOM"
    assert updated.assigned_to == ("hc1", "dhw1", "vent1")
    assert updated.thermal_disinfection == "OFF"
    assert updated.fix_temperature == 16.5

    await entity.async_delete_event(
        "pointt-7",
        recurrence_id="ordinary-event-instance",
        recurrence_range="",
    )
    coordinator.async_delete_holiday.assert_awaited_once_with(7)


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
            "pointt-7",
            event,
            recurrence_range="THIS_AND_FUTURE",
        )

    with pytest.raises(HomeAssistantError, match="Recurring"):
        await entity.async_update_event(
            "pointt-7",
            {**event, EVENT_RRULE: "FREQ=YEARLY"},
        )


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

    with pytest.raises(HomeAssistantError, match="created or confirmed"):
        await entity.async_create_event(**event)
    with pytest.raises(HomeAssistantError, match="changed or confirmed"):
        await entity.async_update_event("pointt-7", event)
    with pytest.raises(HomeAssistantError, match="deleted or confirmed"):
        await entity.async_delete_event("pointt-7")

    with pytest.raises(HomeAssistantError, match="no writable PointT ID"):
        await entity.async_delete_event("unknown")
