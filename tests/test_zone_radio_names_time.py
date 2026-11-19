"""Radio devices of a zone controller: names after the zone, connection time.

Synthetic readings in the shape of the issue (a zone controller whose radio
devices have no name of their own). Nothing here confirms behaviour on a real
device.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.device import grouped_entity_name
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from custom_components.bosch_buderus_heating.zone_radio_devices import (
    ZoneRadioBatteryBinarySensor,
    ZoneRadioConnectionLostSensor,
    ZoneRadioSignalSensor,
    is_zone_radio_connection_lost,
    track_zone_radio_devices,
)
from custom_components.bosch_buderus_heating.zone_radio_names import (
    ZoneAssignment,
    zone_assignment,
    zone_number,
)
from custom_components.bosch_buderus_heating.zone_radio_time import (
    MAX_TEXT_LENGTH,
    PLACEHOLDER,
    connection_lost_time,
    printable_text,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_zone_radio_devices import battery, show, signal

DASH = " \N{EN DASH} "


def zone_id(device, value, kind="integerValue"):
    return parse_resource(
        {
            "id": f"/devices/{device}/zoneId",
            "type": kind,
            "value": value,
            "writeable": True,
        }
    )


def zone_name(number, text):
    return parse_resource(
        {
            "id": f"/zones/zone{number}/name",
            "type": "stringValue",
            "value": text,
            "writeable": False,
        }
    )


def lost(value, device="device1", kind="stringValue"):
    return parse_resource(
        {
            "id": f"/devices/{device}/rfTimeofConnectionLost",
            "type": kind,
            "value": value,
            "writeable": False,
        }
    )


@pytest.fixture(name="coordinator")
def radio_coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"gateway_ids": ["gateway"]})
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway"), entry
    )
    coordinator.data = {}
    coordinator.last_update_success = True
    return coordinator


def name_of(coordinator, device="device1", leaf="battery"):
    return grouped_entity_name(coordinator, f"/devices/{device}/{leaf}", "Battery low")


# Names after the zone (A) -------------------------------------------------


async def test_a_single_device_is_named_after_its_zone(hass, coordinator):
    show(coordinator, battery(), zone_id("device1", 5), zone_name(5, "Kitchen"))
    assert name_of(coordinator) == f"Kitchen{DASH}Battery low"
    entity = ZoneRadioBatteryBinarySensor(coordinator, "device1")
    assert entity.name == f"Kitchen{DASH}Battery low"
    # Identity and device stay as they were.
    assert entity.unique_id == "gateway:devices:device1:battery"


async def test_every_value_of_the_device_gets_the_zone_name(hass, coordinator):
    show(coordinator, zone_id("device1", 5), zone_name(5, "Kitchen"))
    for leaf in ("roomtemperature", "actualHumidity", "rfTimeofConnectionLost"):
        assert name_of(coordinator, leaf=leaf).startswith(f"Kitchen{DASH}")


async def test_several_devices_of_a_zone_are_told_apart_by_device_number(
    hass, coordinator
):
    show(
        coordinator,
        zone_name(5, "Kitchen"),
        zone_id("device10", 5),
        zone_id("device2", 5),
        zone_id("device7", 6),
        zone_name(6, "Bath"),
    )
    assert (
        name_of(coordinator, "device2")
        == f"Kitchen{DASH}Radio device 1{DASH}Battery low"
    )
    # Numerically, not as text: 10 follows 2.
    assert name_of(coordinator, "device10") == (
        f"Kitchen{DASH}Radio device 2{DASH}Battery low"
    )
    assert name_of(coordinator, "device7") == f"Bath{DASH}Battery low"
    hass.config.language = "de"
    assert name_of(coordinator, "device2").startswith(f"Kitchen{DASH}Funkgerät 1{DASH}")


async def test_a_zone_without_a_name_is_named_by_its_number(hass, coordinator):
    show(coordinator, zone_id("device1", 5), zone_name(5, ""))
    assert name_of(coordinator) == f"Zone 5{DASH}Battery low"
    show(coordinator, zone_name(5, "Zone"))  # the vendor's default name
    assert name_of(coordinator) == f"Zone 5{DASH}Battery low"
    coordinator.data.pop("/zones/zone5/name")
    show(
        coordinator,
        parse_resource(
            {
                "id": "/zones/zone5/averageCurrentTemperature",
                "type": "floatValue",
                "value": 20.0,
                "unitOfMeasure": "C",
                "writeable": False,
            }
        ),
    )
    assert name_of(coordinator) == f"Zone 5{DASH}Battery low"


@pytest.mark.parametrize(
    "reading",
    [
        None,  # the device names no zone at all
        zone_id("device1", 9),  # a zone the plant does not report
        zone_id("device1", True),
        zone_id("device1", -2),
        zone_id("device1", 0),
        zone_id("device1", 5.5, "floatValue"),
        zone_id("device1", "5", "stringValue"),
        Resource("/devices/device1/zoneId", value=float("nan"), has_value=True),
        Resource("/devices/device1/zoneId", value=float("inf"), has_value=True),
        parse_resource(
            {"id": "/devices/device1/zoneId", "type": "integerValue", "writeable": True}
        ),
    ],
)
async def test_a_device_without_a_valid_zone_keeps_the_generic_name(
    hass, coordinator, reading
):
    show(coordinator, zone_name(5, "Kitchen"))
    if reading is not None:
        show(coordinator, reading)
    assert name_of(coordinator) == f"Room device 1{DASH}Battery low"


async def test_a_whole_float_names_the_zone(hass, coordinator):
    show(coordinator, zone_id("device1", 5.0, "floatValue"), zone_name(5, "Kitchen"))
    assert name_of(coordinator) == f"Kitchen{DASH}Battery low"


async def test_a_room_controller_is_not_touched(hass, coordinator):
    show(coordinator, zone_id("device1", 5), zone_name(5, "Kitchen"))
    with patch(
        "custom_components.bosch_buderus_heating.device.is_rrc_plant",
        return_value=True,
    ):
        assert name_of(coordinator) == "Battery low"


async def test_the_zone_is_read_from_the_stored_readings_too(hass, coordinator):
    coordinator.resources[zone_id("device1", 5).path] = zone_id("device1", 5)
    show(coordinator, zone_name(5, "Kitchen"))
    assert name_of(coordinator) == f"Kitchen{DASH}Battery low"


def test_zone_number_and_assignment_rules():
    assert zone_number(zone_id("device1", 12)) == 12
    assert zone_number(None) is None
    resources = {
        zone_id("device3", 5).path: zone_id("device3", 5),
        zone_id("device4", 5).path: zone_id("device4", 5),
        "/zones/zone5/name": zone_name(5, "x"),
        "/devices/list": parse_resource(
            {"id": "/devices/list", "type": "stringValue", "value": "x"}
        ),
    }
    assert zone_assignment(resources, "device4") == ZoneAssignment(5, 2, 2)
    assert zone_assignment(resources, "device3") == ZoneAssignment(5, 1, 2)
    assert zone_assignment(resources, "device9") is None


# The connection-loss time (B) -------------------------------------------------

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-10T07:03Z", datetime(2026, 11, 10, 7, 3, tzinfo=UTC)),
        ("2026-11-10T07:03:09Z", datetime(2026, 11, 10, 7, 3, 9, tzinfo=UTC)),
        (
            "2026-1-5T7:3+01:00",
            datetime(2026, 1, 5, 7, 3, tzinfo=timezone(timedelta(hours=1))),
        ),
        (
            "2026-11-10T07:03-0530",
            datetime(
                2026, 11, 10, 7, 3, tzinfo=timezone(-timedelta(hours=5, minutes=30))
            ),
        ),
        (PLACEHOLDER, None),
        ("2000-00-00T00:00:00", None),
        ("2026-00-10T07:03Z", None),  # month 00
        ("2026-11-00T07:03Z", None),  # day 00
        ("2026-02-30T07:03Z", None),  # no such day
        ("2026-11-10T25:03Z", None),
        ("2026-11-10T07:61Z", None),
        ("2026-11-10T07:03+15:00", None),
        ("2000-01-01T00:00Z", None),  # the controller's null year
        ("1999-12-31T23:59Z", None),
        (
            "2026-11-10T07:03",
            datetime(2026, 11, 10, 7, 3, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
        (
            "2026-11-10T07:03:09",
            datetime(2026, 11, 10, 7, 3, 9, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
        ("2000-00-00T00:00", None),
        ("2030-01-01T00:00", None),  # the future without offset
        ("2030-01-01T00:00Z", None),  # the future
        ("2026-11-18T11:00Z", datetime(2026, 11, 18, 11, 0, tzinfo=UTC)),  # clock ahead
        ("", None),
        ("never", None),
        ("2026-11-10T07:03Z\n", None),
        ("2026-11-10 07:03Z", None),
    ],
)
def test_the_time_is_parsed_strictly(text, expected):
    with _local_zone("Europe/Berlin"):
        assert connection_lost_time(text, NOW) == expected


@contextmanager
def _local_zone(name):
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo(name))
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


def test_a_time_without_offset_follows_the_home_assistant_time_zone():
    text = "2026-11-10T07:03"
    with _local_zone("Europe/Berlin"):
        berlin = connection_lost_time(text, NOW)
    with _local_zone("America/New_York"):
        new_york = connection_lost_time(text, NOW)
    assert berlin == datetime(2026, 11, 10, 6, 3, tzinfo=UTC)
    assert new_york == datetime(2026, 11, 10, 12, 3, tzinfo=UTC)


def test_only_short_printable_text_can_be_shown():
    assert printable_text(" 2026-11-10T07:03Z ") == "2026-11-10T07:03Z"
    for bad in ("", "   ", "a" * (MAX_TEXT_LENGTH + 1), "a\x00b", "a\nb", None, 5):
        assert printable_text(bad) is None


async def test_the_placeholder_is_unknown_not_a_date(hass, coordinator):
    show(coordinator, lost(PLACEHOLDER))
    entity = ZoneRadioConnectionLostSensor(coordinator, "device1")
    assert entity.available
    assert entity.native_value is None
    assert entity.extra_state_attributes == {}
    assert entity.device_class is SensorDeviceClass.TIMESTAMP
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    assert entity.entity_registry_enabled_default
    assert entity.native_unit_of_measurement is None


async def test_a_time_with_offset_is_a_timestamp_with_its_text(hass, coordinator):
    show(coordinator, lost("2026-11-10T07:03Z"))
    entity = ZoneRadioConnectionLostSensor(coordinator, "device1")
    assert entity.native_value == datetime(2026, 11, 10, 7, 3, tzinfo=UTC)
    assert entity.extra_state_attributes == {"raw_value": "2026-11-10T07:03Z"}


async def test_a_time_without_offset_is_read_as_home_assistant_local_time(
    hass, coordinator
):
    await hass.config.async_set_time_zone("Europe/Berlin")
    show(coordinator, lost("2026-11-10T07:03"))
    entity = ZoneRadioConnectionLostSensor(coordinator, "device1")
    assert entity.available
    assert entity.native_value == datetime(2026, 11, 10, 6, 3, tzinfo=UTC)
    assert entity.extra_state_attributes == {"raw_value": "2026-11-10T07:03"}


@pytest.mark.parametrize(
    "reading",
    [
        lost("x" * 1000),
        lost("a\x00b"),
        lost(""),
        lost(5, kind="integerValue"),
        lost(None),
    ],
)
async def test_an_unusable_text_is_unavailable_and_creates_no_entity(
    hass, coordinator, reading
):
    show(coordinator, reading)
    assert not ZoneRadioConnectionLostSensor(coordinator, "device1").available
    assert not ZoneRadioConnectionLostSensor.wanted(reading)


async def test_a_stale_reading_is_unavailable(hass, coordinator):
    show(coordinator, lost("2026-11-10T07:03Z"))
    path = "/devices/device1/rfTimeofConnectionLost"
    coordinator.data[path] = ResourceSnapshot(
        coordinator.data[path].resource,
        True,
        datetime.now(UTC),
        freshness=Freshness.STALE,
    )
    assert not ZoneRadioConnectionLostSensor(coordinator, "device1").available


async def test_the_entity_keeps_the_identity_of_the_generic_sensor(hass, coordinator):
    resource = lost(PLACEHOLDER)
    # With a room controller's rules the generic sensor still exists: its
    # unique id is the one of the new entity.
    (generic,) = build_sensor_descriptions({resource.path: resource}, rrc_plant=True)
    entity = ZoneRadioConnectionLostSensor(coordinator, "device1")
    assert entity.unique_id == f"gateway:{generic.unique_key}"
    assert entity.unique_id == "gateway:devices:device1:rfTimeofConnectionLost"
    # On a zone controller the generic sensor no longer exists: no duplicate.
    assert build_sensor_descriptions({resource.path: resource}) == ()
    assert is_zone_radio_connection_lost(resource.path)
    assert not is_zone_radio_connection_lost("/devices/device1/battery")
    assert not is_zone_radio_connection_lost("/zones/zone1/rfTimeofConnectionLost")


async def test_one_connection_entity_per_device_also_when_found_later(
    hass, coordinator
):
    show(coordinator, lost(PLACEHOLDER, "device1"), battery("device2"))
    added: list = []
    unload: list = []
    entry = SimpleNamespace(async_on_unload=unload.append)
    track_zone_radio_devices(
        entry, coordinator, added.extend, ZoneRadioConnectionLostSensor
    )
    assert [entity.device for entity in added] == ["device1"]
    show(coordinator, lost("2026-11-10T07:03Z", "device2"))
    coordinator.async_set_updated_data(coordinator.data)
    assert [entity.device for entity in added] == ["device1", "device2"]
    for callback in unload:
        callback()


async def test_no_connection_entity_on_a_room_controller(hass, coordinator):
    show(coordinator, lost(PLACEHOLDER))
    added: list = []
    unload: list = []
    entry = SimpleNamespace(async_on_unload=unload.append)
    with patch(
        "custom_components.bosch_buderus_heating.zone_radio_devices.context_of",
        return_value=SimpleNamespace(rrc_plant=True),
    ):
        track_zone_radio_devices(
            entry, coordinator, added.extend, ZoneRadioConnectionLostSensor
        )
    assert added == []
    for callback in unload:
        callback()


async def test_battery_and_signal_are_unchanged_by_the_new_leaf(hass, coordinator):
    show(coordinator, battery(), signal(), lost(PLACEHOLDER))
    added: list = []
    unload: list = []
    entry = SimpleNamespace(async_on_unload=unload.append)
    track_zone_radio_devices(entry, coordinator, added.extend, ZoneRadioSignalSensor)
    assert [type(entity) for entity in added] == [ZoneRadioSignalSensor]
    for callback in unload:
        callback()
