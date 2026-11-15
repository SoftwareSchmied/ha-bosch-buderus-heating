"""Battery and radio signal of the radio room devices of a zone controller.

Synthetic readings in the shape of the zone controller fixture; nothing
here confirms behaviour on a real device.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.pointt import Gateway
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from custom_components.bosch_buderus_heating.zone_radio_devices import (
    ZoneRadioBatteryBinarySensor,
    ZoneRadioSignalSensor,
    track_zone_radio_devices,
)
from tests.ha_test_helpers import MockConfigEntry

BATTERY_VALUES = ["ok", "low", "critical", "empty", "unknown"]


def battery(device="device1", value="ok", **extra):
    return parse_resource(
        {
            "id": f"/devices/{device}/battery",
            "type": "stringValue",
            "value": value,
            "allowedValues": BATTERY_VALUES,
            "writeable": False,
            **extra,
        }
    )


def signal(device="device1", value=-60.0, unit="db", **extra):
    return parse_resource(
        {
            "id": f"/devices/{device}/signal",
            "type": "floatValue",
            "value": value,
            "unitOfMeasure": unit,
            "writeable": False,
            **extra,
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


def show(coordinator, *resources):
    for resource in resources:
        coordinator.data[resource.path] = ResourceSnapshot(
            resource, True, datetime.now(UTC)
        )


@pytest.mark.parametrize(
    ("value", "expected", "reported"),
    [
        ("ok", False, "ok"),
        ("low", True, "low"),
        ("unknown", None, "unknown"),
        ("critical", None, "critical"),
        ("not offered", None, None),
    ],
)
async def test_battery_is_on_only_for_low_and_unknown_texts_stay_unknown(
    hass, coordinator, value, expected, reported
):
    show(coordinator, battery(value=value))
    entity = ZoneRadioBatteryBinarySensor(coordinator, "device1")
    assert entity.available
    assert entity.is_on is expected
    assert entity.extra_state_attributes == (
        {"reported_state": reported} if reported else {}
    )
    assert entity.device_class is BinarySensorDeviceClass.BATTERY
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    assert entity.entity_registry_enabled_default
    assert entity.unique_id == "gateway:devices:device1:battery"


@pytest.mark.parametrize(
    "invalid",
    [
        {"type": "floatValue", "value": 20},
        {"value": None},
        {"available": False},
        {"used": False},
    ],
)
async def test_invalid_battery_reading_is_unavailable(hass, coordinator, invalid):
    resource = parse_resource(
        {
            "id": "/devices/device1/battery",
            "type": "stringValue",
            "value": "ok",
            "writeable": False,
            **invalid,
        }
    )
    show(coordinator, resource)
    entity = ZoneRadioBatteryBinarySensor(coordinator, "device1")
    assert not entity.available
    assert entity.is_on is None


async def test_stale_battery_reading_is_unavailable(hass, coordinator):
    show(coordinator, battery(value="low"))
    path = "/devices/device1/battery"
    coordinator.data[path] = replace(coordinator.data[path], freshness=Freshness.STALE)
    assert not ZoneRadioBatteryBinarySensor(coordinator, "device1").available


async def test_signal_in_db_is_a_disabled_diagnostic(hass, coordinator):
    show(coordinator, signal(value=-71.5))
    entity = ZoneRadioSignalSensor(coordinator, "device1")
    assert entity.native_value == -71.5
    assert entity.native_unit_of_measurement == "dB"
    assert entity.device_class is SensorDeviceClass.SIGNAL_STRENGTH
    assert entity.state_class is SensorStateClass.MEASUREMENT
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    assert not entity.entity_registry_enabled_default
    assert entity.unique_id == "gateway:devices:device1:signal"


@pytest.mark.parametrize(
    "resource",
    [
        signal(unit="%"),
        signal(state=[{"invalid": -60.0}]),
        signal(value=500),
        signal(value=True),
    ],
)
async def test_signal_outside_its_contract_is_unavailable(hass, coordinator, resource):
    show(coordinator, resource)
    entity = ZoneRadioSignalSensor(coordinator, "device1")
    assert not entity.available
    assert entity.native_value is None


def test_generic_sensors_do_not_duplicate_the_values():
    for resource in (battery(), signal()):
        assert not build_sensor_descriptions({resource.path: resource})


@pytest.mark.parametrize(
    ("entity_class", "leaf"),
    [(ZoneRadioBatteryBinarySensor, "battery"), (ZoneRadioSignalSensor, "signal")],
)
async def test_one_entity_per_reporting_device_also_when_found_later(
    hass, coordinator, entity_class, leaf
):
    show(coordinator, battery("device1"), signal("device1"), battery("device3"))
    added = []
    unload = []
    entry = SimpleNamespace(async_on_unload=unload.append)
    track_zone_radio_devices(entry, coordinator, added.extend, entity_class)
    expected = {"device1", "device3"} if leaf == "battery" else {"device1"}
    assert {entity.device for entity in added} == expected
    show(coordinator, signal("device3"), battery("device4"))
    coordinator.async_set_updated_data(coordinator.data)
    expected |= {"device4"} if leaf == "battery" else {"device3"}
    assert {entity.device for entity in added} == expected
    assert len(added) == len(expected)
    for callback in unload:
        callback()


async def test_no_entities_on_a_room_controller(hass, coordinator):
    show(coordinator, battery(), signal())
    added = []
    entry = SimpleNamespace(async_on_unload=lambda _: None)
    with patch(
        "custom_components.bosch_buderus_heating.zone_radio_devices.context_of",
        return_value=SimpleNamespace(rrc_plant=True),
    ):
        track_zone_radio_devices(
            entry, coordinator, added.extend, ZoneRadioBatteryBinarySensor
        )
    assert added == []


async def test_names_are_translated_and_grouped_by_device(hass, coordinator):
    hass.config.language = "de"
    show(coordinator, battery())
    entity = ZoneRadioBatteryBinarySensor(coordinator, "device1")
    assert entity.name.endswith("Batterie schwach")
    hass.config.language = "fr"
    entity = ZoneRadioSignalSensor(coordinator, "device1")
    assert entity.name.endswith("Signal radio")
