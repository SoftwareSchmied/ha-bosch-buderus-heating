"""Local-only sensors keep their registry entry while a reading is missing.

When Home Assistant stops, the local access withdraws its readings; after
the start it reads its resources one after the other, and a timeout can
pause it for a minute. A sensor without a current reading must keep the
device class (and with it the display precision Home Assistant derives from
it) and the unit it was registered with; it only becomes available again
when its reading arrives.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.bosch_buderus_heating.local_only_sensor import (
    BoschBuderusLocalOnlySensor,
)
from custom_components.bosch_buderus_heating.local_plant_view import local_sensor
from tests.ha_test_helpers import add_without_device
from tests.test_local_only import FLOW, SIGNAL, coordinator_with, number

READINGS = {
    SIGNAL: (number(SIGNAL, 28.5), "°C", SensorDeviceClass.TEMPERATURE),
    FLOW: (
        number(FLOW, 1180, unit="l/h", kind="integerValue"),
        "L/h",
        SensorDeviceClass.VOLUME_FLOW_RATE,
    ),
}


def _sensor(hass, path, observations):
    observation = READINGS[path][0]
    definition = local_sensor(observation)[0].definition
    return BoschBuderusLocalOnlySensor(
        coordinator_with(hass, observations), path, definition
    )


def _stored(entry: er.RegistryEntry) -> tuple[object, ...]:
    return (
        entry.original_device_class,
        entry.unit_of_measurement,
        entry.capabilities,
        dict(entry.options),
    )


@pytest.mark.parametrize("path", [SIGNAL, FLOW])
async def test_a_missing_reading_keeps_the_registry_entry(hass, path) -> None:
    observation, unit, device_class = READINGS[path]
    assert await async_setup_component(hass, "sensor", {})
    registry = er.async_get(hass)
    sensor = _sensor(hass, path, [observation])
    # Enabled, as on an installation that shows the reading.
    registry.async_get_or_create("sensor", "sensor", sensor.unique_id)
    await add_without_device(hass, sensor)
    await hass.async_block_till_done()
    entity_id = sensor.entity_id
    entry = registry.async_get(entity_id)
    assert entry is not None and entry.original_device_class == device_class
    assert entry.options["sensor"].get("suggested_display_precision") is not None
    before = _stored(entry)
    assert hass.states.get(entity_id).attributes["unit_of_measurement"] == unit

    # Home Assistant stops or the local access pauses: the reading is gone.
    sensor.coordinator.local_selection._runtime.observations.clear()
    sensor.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    assert _stored(registry.async_get(entity_id)) == before

    # A restart: the new entity is registered before its first reading.
    await sensor.async_remove()
    await hass.async_block_till_done()
    later = _sensor(hass, path, [])
    await add_without_device(hass, later)
    await hass.async_block_till_done()
    assert later.entity_id == entity_id
    state = hass.states.get(entity_id)
    assert state.state == "unavailable"
    assert state.attributes["unit_of_measurement"] == unit
    assert state.attributes["device_class"] == device_class
    assert _stored(registry.async_get(entity_id)) == before

    # The reading arrives: available again, nothing in the registry changes.
    later.coordinator.local_selection._runtime.observations.append(observation)
    later.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state != "unavailable"
    assert _stored(registry.async_get(entity_id)) == before


async def test_the_class_is_held_with_the_unit(hass) -> None:
    sensor = _sensor(hass, SIGNAL, [READINGS[SIGNAL][0]])
    temperature = (21.5, "°C", SensorDeviceClass.TEMPERATURE)
    with patch.object(sensor, "_shape", return_value=temperature):
        assert sensor.native_unit_of_measurement == "°C"
        assert sensor.device_class is SensorDeviceClass.TEMPERATURE
    with patch.object(sensor, "_shape", return_value=None):
        assert not sensor.available
        assert sensor.native_unit_of_measurement == "°C"
        assert sensor.device_class is SensorDeviceClass.TEMPERATURE
    # A current number without a known unit shows neither unit nor class,
    # but the held pair stays for the next gap.
    with patch.object(sensor, "_shape", return_value=(22.0, None, None)):
        assert sensor.device_class is None
    with patch.object(sensor, "_shape", return_value=None):
        assert sensor.native_unit_of_measurement == "°C"
        assert sensor.device_class is SensorDeviceClass.TEMPERATURE
    # Another unit with its own class replaces the held one.
    with patch.object(sensor, "_shape", return_value=(5, "s", None)):
        assert sensor.device_class is None
    with patch.object(sensor, "_shape", return_value=None):
        assert sensor.device_class is None


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("temperature", SensorDeviceClass.TEMPERATURE),
        (None, None),
        ("no_such_class", None),
    ],
)
async def test_before_the_first_reading_the_registered_class_counts(
    hass, stored, expected
) -> None:
    sensor = _sensor(hass, SIGNAL, [])
    sensor.registry_entry = SimpleNamespace(
        original_device_class=stored,
        options={"bosch_buderus_heating": {"native_unit": "°C"}},
        capabilities={"state_class": "measurement"},
    )
    assert not sensor.available
    assert sensor.device_class == expected
    assert sensor.native_unit_of_measurement == "°C"
