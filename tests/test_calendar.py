"""Tests for the read-only holiday calendar."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
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
    HOLIDAY_LIST_PATH,
)
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource


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
    assert entity.device_info["model"] == "K40"
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
