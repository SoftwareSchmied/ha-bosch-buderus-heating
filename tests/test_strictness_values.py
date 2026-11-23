"""Strictness package A4 and A6: values read as tolerantly as the reported
form allows, without granting a write.

A4: a decimal number may also come as a whole number (``integerValue``),
metadata of the wrong type counts as not reported, a blank energy unit is a
missing one, the repair compares units in any reviewed spelling, the price
deltas of the variable tariff without a unit are kelvin (#81), and a path
with an unexpected schema is read on once an hour. A6: the state "na" (not
available) of the system pressure is no pressure.

Tested with constructed answers; not yet confirmed on a real gateway.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.compatibility import (
    incompatible_capabilities,
)
from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
from custom_components.bosch_buderus_heating.diagnostics import _capability_diagnostics
from custom_components.bosch_buderus_heating.discovery_inventory import (
    resource_from_data,
    resource_to_data,
)
from custom_components.bosch_buderus_heating.entity_resources import temperature
from custom_components.bosch_buderus_heating.observations import ValueStatus
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.read_contracts import assess_runtime
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    poll_group,
    supports_entity,
)
from custom_components.bosch_buderus_heating.start_counters import (
    assess_start_counter,
)
from custom_components.bosch_buderus_heating.water_softener import softener_value
from custom_components.bosch_buderus_heating.zone_radio_devices import signal_reading
from tests.test_local_api import scalar
from tests.test_observations import local
from tests.test_sensor import _pressure_sensors, _sensor

PRESSURE = "/heatSources/systemPressure"
HIGH_DELTA = "/system/variableTariff/ch/highPriceDelta"


def _resource(path: str, value: object, kind: str, unit: str | None) -> Resource:
    return Resource(
        path=path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type=kind, unit=unit),
    )


# ------------------------------------------------------------ whole numbers


@pytest.mark.parametrize(
    ("path", "unit"),
    [
        ("/heatSources/hs1/actualPower", "kW"),
        ("/heatSources/hs1/powerPercentage", "%"),
        ("/heatSources/passiveCooling/inflowTemp", "C"),
        ("/heatingCircuits/hc1/actualHumidity", "%"),
    ],
)
def test_a_whole_number_reads_where_a_decimal_one_does(path: str, unit: str) -> None:
    assert supports_entity(_resource(path, 21.0, "floatValue", unit))
    assert supports_entity(_resource(path, 21, "integerValue", unit))
    # The type check stays: a text never reads as a number.
    assert not supports_entity(_resource(path, "21", "stringValue", unit))


def test_whole_numbers_in_the_other_read_contracts() -> None:
    assert temperature(_resource("/x", 21, "integerValue", "C")) == 21
    assert temperature(_resource("/x", 21, "booleanValue", "C")) is None
    hours = assess_runtime(
        _resource("/heatSources/hs1/operationHours", 12, "integerValue", "h")
    )
    assert hours.supported
    starts = assess_start_counter(
        _resource("/heatSources/numberOfStarts", 13, "integerValue", None)
    )
    assert starts.supported
    assert starts.reading(None).value == 13
    salt = _resource("/swCircuits/sw1/sensor/saltLevel", 40, "integerValue", "%")
    assert softener_value(salt) == 40.0
    signal = _resource("/devices/device1/signal", -60, "integerValue", "dB")
    assert signal_reading(signal) == -60.0


def test_the_repair_takes_whole_numbers_and_reviewed_spellings() -> None:
    resources = {
        "/heatSources/returnTemperature": _resource(
            "/heatSources/returnTemperature", 30, "integerValue", "°C"
        ),
        "/system/sensors/temperatures/outdoor_t1": _resource(
            "/system/sensors/temperatures/outdoor_t1", 3.5, "floatValue", "degC"
        ),
        "/heatSources/systemPressure": _resource(PRESSURE, 1.5, "floatValue", "bar"),
    }
    assert incompatible_capabilities(resources) == ()
    resources[PRESSURE] = _resource(PRESSURE, 1.5, "floatValue", "kPa")
    assert incompatible_capabilities(resources) == (PRESSURE,)


# ---------------------------------------------------------- variable tariff


@pytest.mark.parametrize("unit", [None, "", " "])
def test_a_tariff_delta_without_a_unit_is_in_kelvin(
    hass: HomeAssistant, unit: str | None
) -> None:
    """#81: both deltas came as floatValue without a unit."""
    delta = _resource(HIGH_DELTA, 2.0, "floatValue", unit)
    assert supports_entity(delta)
    sensor = _sensor(hass, delta)
    assert sensor.native_unit_of_measurement == "K"
    assert sensor.device_class is SensorDeviceClass.TEMPERATURE_DELTA
    assert sensor.native_value == 2.0


def test_only_the_tariff_deltas_take_a_missing_unit() -> None:
    assert supports_entity(_resource(HIGH_DELTA, 2.0, "floatValue", "C"))
    assert supports_entity(_resource(HIGH_DELTA, 2.0, "floatValue", "K"))
    assert not supports_entity(_resource(HIGH_DELTA, 2.0, "floatValue", "%"))
    # A setpoint of the same tariff still needs its temperature unit.
    setpoint = "/system/variableTariff/ch/currentSetpoint"
    assert not supports_entity(_resource(setpoint, 21.0, "floatValue", None))
    assert supports_entity(_resource(setpoint, 21.0, "floatValue", "C"))


# ------------------------------------------------- unexpected schema, hourly


def test_a_path_with_an_unexpected_schema_is_read_once_an_hour() -> None:
    wrong = _resource("/heatSources/hs1/actualPower", 4.2, "floatValue", "C")
    assert not supports_entity(wrong)
    assert poll_group(wrong) is PollGroup.HOURLY
    right = _resource("/heatSources/hs1/actualPower", 4.2, "floatValue", "kW")
    assert poll_group(right) is PollGroup.FAST


# --------------------------------------------------------- faulty metadata


def test_faulty_metadata_reaches_the_diagnostics_and_the_store() -> None:
    resource = parse_resource(
        {
            "id": "/heatSources/returnTemperature",
            "type": "floatValue",
            "value": 30.5,
            "unitOfMeasure": "C",
            "minValue": "5",
            "writeable": "true",
        }
    )
    assert resource.metadata.metadata_issues == ("writeable", "minValue")
    assert not resource.metadata.writable
    report = _capability_diagnostics(resource, None, {}, 0)
    assert report["metadata_not_reported"] == ["writeable", "minValue"]
    assert report["writable"] is False
    stored = resource_to_data(resource)
    assert resource_from_data(stored).metadata == resource.metadata
    # A store of an earlier version has no such list.
    del stored["metadata"]["metadata_issues"]
    assert resource_from_data(stored).metadata.metadata_issues == ()
    clean = parse_resource({"id": "/x", "type": "floatValue", "value": 1})
    assert "metadata_not_reported" not in _capability_diagnostics(clean, None, {}, 0)


# --------------------------------------------------------------- A6: "na"


def _pressure(value: float, state: list[dict[str, float]]) -> Resource:
    return parse_resource(
        {
            "id": PRESSURE,
            "type": "floatValue",
            "value": value,
            "unitOfMeasure": "bar",
            "state": state,
        }
    )


def test_the_pressure_state_na_is_no_pressure(hass: HomeAssistant) -> None:
    resource = _pressure(0.0, [{"na": 0}, {"invalid": -1}])
    assert resource.metadata.numeric_error_values == (-1, 0)
    pressure_sensor, status_sensor = _pressure_sensors(hass, pressure_value=0.0)
    now = datetime.now(UTC)
    for sensor in (pressure_sensor, status_sensor):
        assert sensor is not None
        sensor.coordinator.data[PRESSURE] = ResourceSnapshot(resource, True, now)
    assert pressure_sensor.native_value is None
    # Never "critically low" for a placeholder.
    assert status_sensor is not None
    assert status_sensor.native_value is None
    valid = _pressure(1.6, [{"na": 0}])
    for sensor in (pressure_sensor, status_sensor):
        sensor.coordinator.data[PRESSURE] = ResourceSnapshot(valid, True, now)
    assert pressure_sensor.native_value == 1.6
    assert status_sensor.native_value == "normal"


def test_na_is_a_marker_of_the_pressure_only() -> None:
    other = parse_resource(
        {
            "id": "/heatSources/returnTemperature",
            "type": "floatValue",
            "value": 0.0,
            "unitOfMeasure": "C",
            "state": [{"na": 0}],
        }
    )
    assert other.metadata.numeric_error_values == ()


def test_the_local_pressure_state_na_is_a_marker() -> None:
    marker = local(scalar(PRESSURE, value=0, unitOfMeasure="bar", state=[{"na": 0}]))
    assert marker.status is ValueStatus.STATE_MARKER
    assert marker.value is None
    reading = local(scalar(PRESSURE, value=1.5, unitOfMeasure="bar", state=[{"na": 0}]))
    assert reading.status is ValueStatus.VALID
    other = local(scalar("/heatSources/returnTemperature", value=0, state=[{"na": 0}]))
    assert other.status is ValueStatus.VALID


# ------------------------------------- unexpected schema, coordinator cycles


async def test_an_unexpected_schema_is_read_on_and_recovers(
    hass: HomeAssistant,
) -> None:
    """Real coordinator cycles: before strictness A4 such a path was read
    once at the start and never again (``static``)."""
    from tests.test_cloud_polling_plan import Clock, _patched, _plant, _run

    power = "/heatSources/hs1/actualPower"
    clock = Clock()
    patches = _patched(clock)
    with patches[0], patches[1], patches[2]:
        coordinator, cloud = _plant(hass, clock)
        wrong = _resource(power, 4.2, "floatValue", "C")
        cloud.resources[power] = wrong
        coordinator.resources[power] = wrong
        coordinator.data[power] = ResourceSnapshot(wrong, True, clock.utc())
        coordinator._rebuild_poll_groups()
        coordinator._next_update = {
            group: clock.now + interval.total_seconds()
            for group, interval in coordinator._poll_intervals.items()
        }
        start = clock.now
        await _run(coordinator, clock, 62)
        assert len(cloud.reads(start, clock.now + 1)[power]) == 1
        # A later firmware answers in the expected form: read at its pace.
        right = _resource(power, 4.4, "floatValue", "kW")
        cloud.resources[power] = right
        await _run(coordinator, clock, 62)
        assert coordinator.data[power].resource.metadata.unit == "kW"
        assert coordinator.data[power].resource.value == 4.4
        assert supports_entity(coordinator.resources[power])
        # The next plan of the groups (as after a search) reads it at its pace.
        coordinator._rebuild_poll_groups()
        assert power in coordinator._paths_by_group[PollGroup.FAST]
        assert power not in coordinator._paths_by_group[PollGroup.HOURLY]
