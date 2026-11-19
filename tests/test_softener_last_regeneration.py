"""The calculated time a water softener last finished a regeneration.

Expected, untested on a real softener. The reading is the softener's
``regeneration/enabled`` (running or not); the sensor notes a change from
running to not running that the integration sees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant, State

from custom_components.bosch_buderus_heating import water_softener_history as module
from custom_components.bosch_buderus_heating.derived_inputs import Input
from custom_components.bosch_buderus_heating.pointt import Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.recordings.sensor import _KINDS
from custom_components.bosch_buderus_heating.recordings.series import Quantity
from custom_components.bosch_buderus_heating.water_softener_history import (
    SoftenerLastRegenerationSensor,
    regeneration_paths,
    track_softener_history_sensors,
)

PATH = "/swCircuits/sw1/regeneration/enabled"
T0 = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)


def reading(value: str, at: datetime) -> Input:
    resource: Resource = parse_resource(
        {
            "id": PATH,
            "type": "stringValue",
            "writeable": 0,
            "recordable": 0,
            "allowedValues": ["on", "off"],
            "value": value,
        }
    )
    return Input(PATH, value, "cloud", at, resource)


class Readings:
    def __init__(self) -> None:
        self.current: Input | None = None

    def __call__(self, coordinator: Any, path: str) -> Input | None:
        assert path == PATH
        return self.current


@pytest.fixture
def readings(monkeypatch: pytest.MonkeyPatch) -> Readings:
    fake = Readings()
    monkeypatch.setattr(module, "read", fake)
    monkeypatch.setattr(module, "grouped_entity_name", lambda c, p, name: name)
    return fake


def sensor(hass: HomeAssistant) -> SoftenerLastRegenerationSensor:
    coordinator = SimpleNamespace(
        hass=hass,
        gateway=SimpleNamespace(gateway_id="gw1"),
        async_add_listener=lambda *args: lambda: None,
    )
    return SoftenerLastRegenerationSensor(coordinator, PATH)  # type: ignore[arg-type]


def test_identity_and_defaults(hass: HomeAssistant, readings: Readings) -> None:
    entity = sensor(hass)
    assert (
        entity.unique_id
        == "gw1:swCircuits:sw1:regeneration:enabled:calculated_last_regeneration"
    )
    assert entity.device_class is SensorDeviceClass.TIMESTAMP
    assert entity.entity_registry_enabled_default is False
    assert entity.name == "Last regeneration (calculated)"
    assert entity.available is False and entity.native_value is None


def test_the_end_of_a_regeneration_is_noted(
    hass: HomeAssistant, readings: Readings
) -> None:
    entity = sensor(hass)
    readings.current = reading("off", T0)
    entity._observe()
    assert entity.native_value is None  # nothing seen to end
    readings.current = reading("on", T0 + timedelta(minutes=10))
    entity._observe()
    assert entity.native_value is None
    end = T0 + timedelta(hours=2)
    readings.current = reading("off", end)
    entity._observe()
    assert entity.native_value == end and entity.available
    assert entity.extra_state_attributes == {"derived_from": [PATH]}
    # Later readings of "off" change nothing.
    readings.current = reading("off", end + timedelta(hours=1))
    entity._observe()
    assert entity.native_value == end


def test_a_missing_or_unusable_reading_changes_nothing(
    hass: HomeAssistant, readings: Readings
) -> None:
    entity = sensor(hass)
    readings.current = reading("on", T0)
    entity._observe()
    # Stale or unavailable: no reading; an unknown value: unusable.
    readings.current = None
    entity._observe()
    readings.current = reading("maybe", T0 + timedelta(minutes=5))
    entity._observe()
    assert entity.native_value is None
    # Still known as running: the end is noted at the next usable reading.
    end = T0 + timedelta(minutes=30)
    readings.current = reading("off", end)
    entity._observe()
    assert entity.native_value == end


def test_a_start_is_not_an_end(hass: HomeAssistant, readings: Readings) -> None:
    entity = sensor(hass)
    readings.current = reading("off", T0)
    entity._observe()
    readings.current = reading("on", T0 + timedelta(hours=1))
    entity._observe()
    assert entity.native_value is None


async def test_the_value_is_restored_after_a_restart(
    hass: HomeAssistant, readings: Readings, monkeypatch: pytest.MonkeyPatch
) -> None:
    entity = sensor(hass)
    entity.hass = hass
    entity.entity_id = "sensor.softener_last_regeneration"

    async def last_state() -> State:
        return State(entity.entity_id, T0.isoformat())

    monkeypatch.setattr(entity, "async_get_last_state", last_state)
    readings.current = None
    await entity.async_added_to_hass()
    assert entity.native_value == T0

    async def broken() -> State:
        return State(entity.entity_id, "unavailable")

    other = sensor(hass)
    other.hass = hass
    other.entity_id = "sensor.other"
    monkeypatch.setattr(other, "async_get_last_state", broken)
    await other.async_added_to_hass()
    assert other.native_value is None


def test_paths_and_discovery(hass: HomeAssistant, readings: Readings) -> None:
    assert regeneration_paths(
        {
            PATH: 1,
            "/swCircuits/sw2/regeneration/enabled": 1,
            "/swCircuits/sw1/regeneration/phase": 1,
            "/heatSources/x": 1,
        }
    ) == [PATH, "/swCircuits/sw2/regeneration/enabled"]
    assert regeneration_paths(None) == []
    added: list[Any] = []
    listeners: list[Any] = []
    coordinator = SimpleNamespace(
        hass=hass,
        gateway=SimpleNamespace(gateway_id="gw1"),
        resources={},
        async_add_listener=lambda listener: (
            listeners.append(listener) or (lambda: None)
        ),
    )
    unloads: list[Any] = []
    entry = SimpleNamespace(async_on_unload=unloads.append)
    track_softener_history_sensors(entry, coordinator, added.extend)  # type: ignore[arg-type]
    assert added == []  # no softener circuit: nothing is created
    coordinator.resources = {PATH: object()}
    listeners[0]()
    listeners[0]()
    assert len(added) == 1  # once


def test_the_water_series_is_a_water_sensor_in_litres() -> None:
    device_class, unit, precision = _KINDS[Quantity.VOLUME]
    assert device_class is SensorDeviceClass.WATER
    assert unit == "L" and precision == 0
