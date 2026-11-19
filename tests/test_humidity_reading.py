"""Humidity placeholders of zones, radio devices and heating circuits (0.9.3).

A resource without a sensor behind it reports a value that is no humidity.
It is shown as unknown, and a sensor first seen with it starts disabled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating import humidity_reading
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.diagnostics import (
    _capability_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import Gateway
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from tests.ha_test_helpers import MockConfigEntry

ZONE = "/zones/zone1/averageActualHumidity"
DEVICE = "/devices/device1/actualHumidity"
CIRCUIT = "/heatingCircuits/hc1/actualHumidity"
PATHS = (ZONE, DEVICE, CIRCUIT)


def _humidity(path: str, value: object, **fields: object):
    return parse_resource(
        {
            "id": path,
            "type": "floatValue",
            "value": value,
            "unitOfMeasure": "%",
            "writeable": 0,
            **fields,
        }
    )


def _sensor(hass, resource) -> BoschBuderusSensor:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type="MX400"), entry
    )
    coordinator.resources = {resource.path: resource}
    coordinator.update_interval = None
    coordinator.data = {
        resource.path: ResourceSnapshot(resource, True, datetime.now(UTC))
    }
    coordinator.last_update_success = True
    description = build_sensor_descriptions(coordinator.resources)[0]
    return BoschBuderusSensor(coordinator, description)


def _cap(resource):
    return _capability_diagnostics(resource, None, {}, 0)


def _description(resource):
    return build_sensor_descriptions({resource.path: resource})[0]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("value", [0, 0.0, -1, -1.0, 255, 255.0, 100.5, 32767, -3276.8])
def test_a_placeholder_shows_unknown(hass, path, value):
    resource = _humidity(path, value)
    assert resource.has_value
    assert _sensor(hass, resource).native_value is None


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("value", [1, 35.5, 48, 100, 100.0])
def test_a_real_humidity_is_shown(hass, path, value):
    assert _sensor(hass, _humidity(path, value)).native_value == value


def test_a_declared_error_marker_is_unknown_and_not_enabled(hass):
    resource = _humidity(ZONE, 50.0, state=[{"invalid": 50.0}])
    assert 50.0 in resource.metadata.numeric_error_values
    assert _sensor(hass, resource).native_value is None
    assert humidity_reading.humidity_state(resource) == "error_marker"
    assert _description(resource).entity_registry_enabled_default is False


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("value", [0, -1, 255])
def test_a_new_sensor_with_a_placeholder_starts_disabled(path, value):
    assert _description(_humidity(path, value)).entity_registry_enabled_default is False


@pytest.mark.parametrize("path", PATHS)
def test_a_new_sensor_with_a_real_reading_keeps_its_default(path):
    assert _description(_humidity(path, 45.0)).entity_registry_enabled_default is True


def test_the_unique_id_and_the_name_do_not_change():
    placeholder = _description(_humidity(ZONE, -1))
    real = _description(_humidity(ZONE, 45.0))
    assert (placeholder.key, placeholder.name) == (real.key, real.name)
    assert placeholder.unique_key == real.unique_key


def test_other_percentages_are_not_touched():
    resource = parse_resource(
        {
            "id": "/heatingCircuits/hc1/pumpModulation",
            "type": "floatValue",
            "value": 0,
            "unitOfMeasure": "%",
            "writeable": 0,
        }
    )
    assert humidity_reading.humidity_state(resource) is None
    assert not humidity_reading.is_placeholder(resource)


def test_missing_and_text_readings_are_no_placeholder():
    empty = parse_resource(
        {"id": ZONE, "type": "floatValue", "unitOfMeasure": "%", "writeable": 0}
    )
    assert humidity_reading.humidity_state(empty) == "missing"
    assert not humidity_reading.is_placeholder(empty)
    text = _humidity(ZONE, "n/a")
    assert humidity_reading.humidity_state(text) == "not_numeric"
    assert not humidity_reading.is_placeholder(text)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, False), (float("nan"), False), (float("inf"), False), ("50", False)],
)
def test_only_finite_numbers_are_humidity(value, expected):
    assert humidity_reading.plausible_humidity(value) is expected


def test_the_diagnostics_name_the_kind_never_the_value():
    for value, kind in ((-1, "implausible"), (45.0, "valid")):
        resource = _humidity(ZONE, value)
        entry = _cap(resource)
        assert entry["humidity_reading"] == kind
        assert str(value) not in {str(item) for item in entry.values()}
    assert "humidity_reading" not in _cap(
        parse_resource(
            {"id": "/system/x", "type": "floatValue", "value": 1.0, "writeable": 0}
        )
    )
