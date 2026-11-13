"""Regressions for dynamic holiday zones and event-loop-safe rendering."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import aiozoneinfo
import pytest
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating.binary_sensor import (
    BoschBuderusHolidayActiveBinarySensor,
)
from custom_components.bosch_buderus_heating.calendar import BoschBuderusHolidayCalendar
from custom_components.bosch_buderus_heating.config_flow import BoschBuderusOptionsFlow
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.diagnostics import _gateway_diagnostics
from custom_components.bosch_buderus_heating.holiday_timezones import (
    async_resolve_holiday_timezones,
)
from custom_components.bosch_buderus_heating.holidays import (
    HOLIDAY_CONFIGURATION_PATH,
    HOLIDAY_LIST_PATH,
    HOLIDAY_TIMEZONE_PATH,
    holiday_timezone,
    parse_confirmed_holiday_list,
    parse_holiday_state,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    Resource,
)
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from custom_components.bosch_buderus_heating.sensor import BoschBuderusNextHolidaySensor
from tests.ha_test_helpers import MockConfigEntry


def _resources(gateway_zone="Europe/Berlin", period_zone=None):
    row = {
        "id": 7,
        "startDate": "2030-03-30T08:00:00",
        "endDate": "2030-04-01T08:00:00",
        "assignedTo": ["hc1"],
        "fixTemperature": 17.0,
        "heatingMode": "FIX_TEMPERATURE",
        "dhwMode": "OFF",
    }
    if period_zone is not None:
        row["timeZone"] = period_zone
    return {
        HOLIDAY_LIST_PATH: Resource(HOLIDAY_LIST_PATH, value=[row], has_value=True),
        HOLIDAY_TIMEZONE_PATH: Resource(
            HOLIDAY_TIMEZONE_PATH, value=gateway_zone, has_value=True
        ),
    }


async def test_cold_gateway_and_period_zones_load_only_in_worker_threads():
    resources = _resources(period_zone="Asia/Kathmandu")
    main_thread = threading.get_ident()
    original = aiozoneinfo.get_time_zone
    loaded = []

    def load(name):
        assert threading.get_ident() != main_thread
        loaded.append(name)
        return original(name)

    with (
        patch.object(
            aiozoneinfo.CachedZoneInfo, "get_cached_zone_info", return_value=None
        ),
        patch.object(aiozoneinfo, "get_time_zone", side_effect=load),
    ):
        zones = await async_resolve_holiday_timezones(resources, {})
    assert loaded == ["Asia/Kathmandu", "Europe/Berlin"]

    with patch("zoneinfo.ZoneInfo", side_effect=AssertionError("sync zone lookup")):
        for _ in range(3):
            state = parse_holiday_state(resources, timezones=zones)
            assert state.timezone_source == "gateway"
            assert state.periods[0].start.utcoffset() == timedelta(hours=5, minutes=45)
            assert (
                holiday_timezone(resources, UTC, timezones=zones)
                is zones["Europe/Berlin"]
            )
            assert (
                parse_confirmed_holiday_list(
                    resources[HOLIDAY_LIST_PATH],
                    resources,
                    fallback_timezone=UTC,
                    timezones=zones,
                )
                == state.periods
            )


@pytest.mark.parametrize(
    "bad_zone", ["Unknown/Zone", "../etc/passwd", "/etc/passwd", "A//B"]
)
async def test_invalid_names_fall_back_and_are_not_retried_on_every_poll(bad_zone):
    resources = _resources(gateway_zone=bad_zone, period_zone=bad_zone)
    with patch.object(
        dt_util, "async_get_time_zone", wraps=dt_util.async_get_time_zone
    ) as resolve:
        zones = await async_resolve_holiday_timezones(resources, {})
        for _ in range(3):
            zones = await async_resolve_holiday_timezones(resources, zones)
            state = parse_holiday_state(
                resources, fallback_timezone=UTC, timezones=zones
            )
            assert state.timezone_source == "home_assistant"
            assert state.periods[0].start.utcoffset() == timedelta()
        resolve.assert_awaited_once_with(bad_zone)
    assert zones == {bad_zone: None}


@pytest.mark.parametrize(
    "zone,offset",
    [
        ("UTC+05:45", 345),
        ("-0330", -210),
        ("+23:59", 1439),
        ("UTC+24:00", 0),
        ("+01:60", 0),
        ("", 0),
        ("x" * 256, 0),
        ("Europe/Berlin\x00", 0),
    ],
)
async def test_fixed_offsets_and_unusable_input_need_no_zone_database(zone, offset):
    resources = _resources(gateway_zone=zone, period_zone=zone)
    with patch.object(
        dt_util, "async_get_time_zone", side_effect=AssertionError("lookup")
    ):
        zones = await async_resolve_holiday_timezones(resources, {})
        state = parse_holiday_state(resources, timezones=zones)
    assert state.periods[0].start.utcoffset() == timedelta(minutes=offset)
    assert zones == {}


async def test_changed_names_are_resolved_and_obsolete_names_are_dropped():
    first = _resources(period_zone="Unknown/Zone")
    zones = await async_resolve_holiday_timezones(first, {})
    second = _resources("America/New_York", "Asia/Tokyo")
    with patch.object(
        dt_util, "async_get_time_zone", wraps=dt_util.async_get_time_zone
    ) as resolve:
        updated = await async_resolve_holiday_timezones(second, zones)
        assert resolve.await_count == 2
        await async_resolve_holiday_timezones(second, updated)
        assert resolve.await_count == 2
    assert set(updated) == {"America/New_York", "Asia/Tokyo"}
    assert set(zones) == {"Europe/Berlin", "Unknown/Zone"}
    assert await async_resolve_holiday_timezones({}, updated) == {}


async def test_cancellation_does_not_publish_a_partially_resolved_mapping():
    previous = {"UTC": UTC}
    with (
        patch.object(
            dt_util, "async_get_time_zone", side_effect=[UTC, asyncio.CancelledError()]
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await async_resolve_holiday_timezones(
            _resources(period_zone="Asia/Tokyo"), previous
        )
    assert previous == {"UTC": UTC}


async def test_resolution_uses_the_same_period_and_depth_limits_as_parsing():
    resources = _resources()
    resources[HOLIDAY_LIST_PATH] = Resource(
        HOLIDAY_LIST_PATH,
        value=[
            {"id": i, "start": "2030-01-01", "end": "2030-01-02", "tz": f"Unknown/{i}"}
            for i in range(70)
        ],
        has_value=True,
    )
    resources[HOLIDAY_CONFIGURATION_PATH] = Resource(
        HOLIDAY_CONFIGURATION_PATH,
        value={"start": "2030-01-01", "tz": "Must/NotLoad"},
        has_value=True,
    )
    with patch.object(dt_util, "async_get_time_zone", return_value=None) as resolve:
        zones = await async_resolve_holiday_timezones(resources, {})
    assert len(zones) == resolve.await_count == 65
    assert "Unknown/64" not in zones and "Must/NotLoad" not in zones
    nested = {"start": "2030-01-01", "end": "2030-01-02", "tz": "Must/NotLoad"}
    for _ in range(7):
        nested = {"nested": nested}
    resources = {
        HOLIDAY_LIST_PATH: Resource(HOLIDAY_LIST_PATH, value=nested, has_value=True)
    }
    assert await async_resolve_holiday_timezones(resources, {}) == {}


async def test_discovery_polling_and_entities_share_the_gateway_zone(hass):
    await hass.config.async_set_time_zone("UTC")
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.get_resources_bulk.return_value = ()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway-one"), entry
    )
    resources = _resources()
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.async_discover_resources",
        return_value=resources,
    ):
        await coordinator.async_refresh()
    entities = (
        BoschBuderusHolidayCalendar(coordinator),
        BoschBuderusHolidayActiveBinarySensor(coordinator),
        BoschBuderusNextHolidaySensor(coordinator),
    )
    with patch(
        "zoneinfo.ZoneInfo", side_effect=AssertionError("rendering opened a zone")
    ):
        for entity in entities:
            period = entity._state.periods[0]
            assert period.start.utcoffset() == timedelta(hours=1)
            assert period.end.utcoffset() == timedelta(hours=2)
            assert entity.available
    changed = _resources("Asia/Tokyo")
    client.get_resources_bulk.return_value = tuple(
        BatchItemResult("gateway-one", path, 200, resource=resource)
        for path, resource in changed.items()
    )
    coordinator._next_update = dict.fromkeys(coordinator._next_update, 0)
    await coordinator.async_refresh()
    for entity in entities:
        assert entity._state.periods[0].start.utcoffset() == timedelta(hours=9)
    assert set(coordinator.holiday_timezones) == {"Asia/Tokyo"}
    coordinator.data = {HOLIDAY_TIMEZONE_PATH: coordinator.data[HOLIDAY_TIMEZONE_PATH]}
    assert not entities[1].available and not entities[2].available
    await coordinator.async_shutdown()


async def test_home_assistant_fallback_changes_without_gateway_refresh(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one"), entry
    )
    resource = _resources()[HOLIDAY_LIST_PATH]
    coordinator.data = {
        HOLIDAY_LIST_PATH: ResourceSnapshot(resource, True, datetime.now(UTC))
    }
    entity = BoschBuderusHolidayCalendar(coordinator)
    for name, offset in (("UTC", 0), ("Asia/Kathmandu", 345)):
        await hass.config.async_set_time_zone(name)
        with patch(
            "zoneinfo.ZoneInfo", side_effect=AssertionError("sync fallback lookup")
        ):
            assert entity._state.periods[0].start.utcoffset() == timedelta(
                minutes=offset
            )
            assert entity._timezone is dt_util.get_default_time_zone()


async def test_options_and_diagnostics_use_prepared_period_zones_without_io(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one"), entry
    )
    resources = _resources(period_zone="Asia/Kathmandu")
    resources[HOLIDAY_CONFIGURATION_PATH] = Resource(
        HOLIDAY_CONFIGURATION_PATH,
        value={
            "values": {
                "date": {"allowedValues": ["dateTime"]},
                "assignedTo": {"allowedValues": ["hc1"]},
                "heatingMode": {"allowedValues": ["FIX_TEMPERATURE", "OFF"]},
                "dhwMode": {"allowedValues": ["OFF"]},
            }
        },
        has_value=True,
    )
    coordinator.resources = resources
    coordinator.data = {
        path: ResourceSnapshot(resource, True, datetime.now(UTC))
        for path, resource in resources.items()
    }
    await coordinator._async_prepare_holiday_timezones()
    entry.runtime_data = BoschBuderusRuntimeData(
        AsyncMock(), AsyncMock(), (coordinator.gateway,), (coordinator,)
    )
    flow = BoschBuderusOptionsFlow(entry)
    flow.hass = hass
    with patch(
        "zoneinfo.ZoneInfo", side_effect=AssertionError("callback timezone I/O")
    ):
        choices = flow._holiday_choices()
        existing = [
            choice for key, choice in choices.items() if not key.startswith("create:")
        ]
        assert len(existing) == 1
        assert existing[0].period.start.utcoffset() == timedelta(hours=5, minutes=45)
        report = _gateway_diagnostics(1, coordinator)
        assert report["holidays"]["timezone_source"] == "gateway"
        assert report["holidays"]["valid_period_count"] == 1
        assert "Asia/Kathmandu" not in str(report)
        assert "Europe/Berlin" not in str(report)
