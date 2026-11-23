"""Regression checks for measured units, metadata changes and native writes."""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from test_number import _number, _resource
from test_sensor import _dew_point_sensor, _sensor

from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
from custom_components.bosch_buderus_heating.diagnostics import _capability_diagnostics
from custom_components.bosch_buderus_heating.energy_contracts import assess_energy
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.resource_catalog import supports_entity
from custom_components.bosch_buderus_heating.sensor import _energy_values
from custom_components.bosch_buderus_heating.writes import (
    WriteService,
    number_policy_for_resource,
)


@pytest.mark.parametrize(
    ("unit", "native", "value", "expected", "device_class"),
    [
        ("degC", "°C", -10, -10, SensorDeviceClass.TEMPERATURE),
        ("degF", "°F", 14, 14, SensorDeviceClass.TEMPERATURE),
        ("KWh", "kWh", 1.5, 1.5, SensorDeviceClass.ENERGY),
        ("wh", "Wh", 1500, 1500, SensorDeviceClass.ENERGY),
        ("kBtu", "kWh", 10, 2.9307107, SensorDeviceClass.ENERGY),
        ("minutes", "min", 90, 90, SensorDeviceClass.DURATION),
        ("m", "min", 90, 90, SensorDeviceClass.DURATION),
        ("hour", "h", 1.5, 1.5, SensorDeviceClass.DURATION),
        ("sec", "s", 90, 90, SensorDeviceClass.DURATION),
        ("secs", "s", 90, 90, SensorDeviceClass.DURATION),
    ],
)
def test_scalar_units_keep_physical_meaning(
    hass: HomeAssistant,
    unit: str,
    native: str,
    value: float,
    expected: float,
    device_class: SensorDeviceClass,
) -> None:
    resource = parse_resource(
        {
            "id": "/system/sensors/temperatures/outdoor_t1",
            "type": "floatValue",
            "unitOfMeasure": unit,
            "value": value,
        }
    )
    entity = _sensor(hass, resource)
    assert entity.native_unit_of_measurement == native
    assert entity.device_class == device_class
    assert entity.native_value == pytest.approx(expected)


@pytest.mark.parametrize(
    ("unit", "raw"),
    [("Wh", 1000), ("wh", 1000), ("KWh", 1), ("kWh", 1), ("kBtu", 1 / 0.29307107)],
)
def test_structured_energy_uses_reported_unit(
    hass: HomeAssistant, unit: str, raw: float
) -> None:
    resource = parse_resource(
        {
            "id": "/heatSources/emon/totalConsumption",
            "type": "emonValue",
            "unitOfMeasure": unit,
            "values": [
                {"compressor": raw, "eheater": raw * 2, "outputProduced": raw * 8}
            ],
        }
    )
    for part, expected in (
        ("compressor", 1),
        ("eheater", 2),
        ("total_electricity", 3),
        ("environmental_energy", 5),
    ):
        entity = _sensor(hass, resource, value_key=part)
        assert entity.native_unit_of_measurement == "kWh"
        assert entity.native_value == pytest.approx(expected)


@pytest.mark.parametrize("unit", ["W", "mystery"])
def test_explicit_unknown_energy_unit_is_not_assumed_kwh(unit: str) -> None:
    resource = Resource(
        path="/heatSources/emon/totalConsumption",
        values=({"compressor": 123},),
        metadata=ResourceMetadata(resource_type="emonValue", unit=unit),
    )
    assert _energy_values(resource) == {}


@pytest.mark.parametrize("unit", ["", " ", "\t"])
def test_a_blank_energy_unit_is_a_missing_one(unit: str) -> None:
    """Strictness A4: blank is missing, as for the start counters."""
    resource = Resource(
        path="/heatSources/emon/totalConsumption",
        values=({"compressor": 123},),
        metadata=ResourceMetadata(resource_type="emonValue", unit=unit),
    )
    assert _energy_values(resource) == {"compressor": 123}
    assert assess_energy(resource).unit_source == "legacy_unitless_kwh"


def test_energy_conflicts_and_error_values_do_not_create_totals() -> None:
    resource = Resource(
        path="/heatSources/emon/totalConsumption",
        values=(
            {"compressor": 2, "eheater": 0},
            {"compressor": 3, "outputProduced": 999},
        ),
        metadata=ResourceMetadata(
            resource_type="emonValue", unit="kWh", numeric_error_values=(999,)
        ),
    )
    assert _energy_values(resource) == {"eheater": 0}


def test_energy_unit_changes_recover_same_entities(hass: HomeAssistant) -> None:
    resource = Resource(
        path="/heatSources/emon/totalConsumption",
        values=({"compressor": 2, "eheater": 0},),
        metadata=ResourceMetadata(resource_type="emonValue", unit="kWh"),
    )
    entity = _sensor(hass, resource, value_key="total_electricity")
    identity = entity.unique_id
    for unit, raw, expected in (
        ("Wh", 2000, 2),
        ("unknown", 2000, None),
        ("kWh", 2, 2),
    ):
        changed = replace(
            resource,
            values=({"compressor": raw, "eheater": 0},),
            metadata=replace(resource.metadata, unit=unit),
        )
        entity.coordinator.data[resource.path] = ResourceSnapshot(
            changed, True, datetime.now(UTC)
        )
        assert entity.native_value == expected
        assert entity.unique_id == identity


def test_scalar_unit_changes_do_not_reuse_old_scale(hass: HomeAssistant) -> None:
    resource = Resource(
        path="/solarCircuits/sc1/solarYield",
        value=2,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="kWh"),
    )
    entity = _sensor(hass, resource)
    identity = entity.unique_id
    for unit, raw, native, expected in (
        ("kBtu", 10, "kWh", 2.9307107),
        ("Wh", 2000, "Wh", 2000),
        ("unknown", 2000, None, None),
        ("kWh", 2, "kWh", 2),
    ):
        changed = replace(
            resource, value=raw, metadata=replace(resource.metadata, unit=unit)
        )
        entity.coordinator.data[resource.path] = ResourceSnapshot(
            changed, True, datetime.now(UTC)
        )
        assert entity.native_value == (
            pytest.approx(expected) if expected is not None else None
        )
        assert entity.native_unit_of_measurement == native
        assert entity.unique_id == identity


@pytest.mark.parametrize(
    ("unit", "native", "low", "high", "target"),
    [
        ("degC", "°C", -20, 30, -9.5),
        ("degF", "°F", -4, 86, 14.5),
        ("minutes", "min", 30, 120, 90),
        ("hour", "h", 1, 48, 2),
    ],
)
async def test_native_controls_preserve_bounds_and_confirm_device_values(
    hass: HomeAssistant, unit: str, native: str, low: float, high: float, target: float
) -> None:
    path = (
        "/heatingCircuits/hc1/manualRoomSetpoint"
        if unit.startswith("deg")
        else "/dhwCircuits/dhw1/chargeDuration"
    )
    resource = _resource(path, low, minimum=low, maximum=high, unit=unit)
    policy = number_policy_for_resource(resource)
    assert policy is not None
    entity = _number(hass, resource)
    assert entity.native_unit_of_measurement == native
    assert entity.native_min_value == low
    assert entity.native_max_value == high
    client = AsyncMock()
    client.put_resource_value.return_value = None
    client.get_resource.return_value = replace(resource, value=target)
    result = await WriteService(client, read_back_delay=0).async_write_number(
        "gateway-one", resource, target, policy
    )
    client.put_resource_value.assert_awaited_once_with("gateway-one", path, target)
    assert result.resource.value == target


@pytest.mark.parametrize("tail", ["highPriceDelta", "lowPriceDelta"])
async def test_temperature_differences_have_no_fahrenheit_offset(
    hass: HomeAssistant, tail: str
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    resource = Resource(
        path=f"/system/variableTariff/ch/{tail}",
        value=2,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )
    entity = _sensor(hass, resource)
    entity.entity_id = "sensor.tariff_delta"
    entity.entity_description = replace(
        entity.entity_description, entity_registry_enabled_default=True
    )
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    with patch.object(
        type(entity), "device_info", new_callable=PropertyMock, return_value=None
    ):
        await component.async_add_entities([entity])
    er.async_get(hass).async_update_entity_options(
        entity.entity_id, "sensor", {"unit_of_measurement": "°F"}
    )
    await hass.async_block_till_done()
    state = hass.states.get(entity.entity_id)
    assert state is not None
    assert state.attributes["unit_of_measurement"] == "°F"
    assert float(state.state) == pytest.approx(3.6)


@pytest.mark.parametrize(
    "value,reason",
    [
        (True, "not_numeric"),
        (None, "not_numeric"),
        ("3", "not_numeric"),
        (float("nan"), "not_finite"),
        (float("inf"), "not_finite"),
        (-1, "out_of_range"),
        (2**53, "out_of_range"),
    ],
)
def test_invalid_energy_parts_are_not_silently_zero(value: object, reason: str) -> None:
    resource = Resource(
        path="/heatSources/emon/totalConsumption",
        metadata=ResourceMetadata(resource_type="emonValue", unit="Wh"),
        values=(
            {"type": "compressor", "value": value},
            {"type": "eheater", "value": 0},
        ),
    )
    assessment = assess_energy(resource)
    assert dict(assessment.readings)["compressor"].reason == reason
    assert _energy_values(resource) == {"eheater": 0}


def test_energy_diagnostics_are_consistent_and_redacted() -> None:
    resource = parse_resource(
        {
            "id": "/heatSources/emon/totalConsumption",
            "type": "emonValue",
            "unitOfMeasure": "Wh",
            "values": [
                {"compressor": 123456.789, "eheater": 0},
                {"private-dynamic-key": 55, "outputProduced": -1},
            ],
        }
    )
    report = _capability_diagnostics(resource, None, {}, 0)["energy_contract"]
    assert report == {
        "source_unit": "Wh",
        "unit_source": "metadata",
        "output_unit": "kWh",
        "scale": 0.001,
        "rejection_reason": None,
        "valid_parts": 3,
        "invalid_parts": {"out_of_range": 1},
    }
    serialized = json.dumps(report)
    assert "123456" not in serialized
    assert "private-dynamic-key" not in serialized


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"numeric_error_states_valid": False}, "invalid_error_metadata"),
        ({"numeric_error_values": (42,)}, "device_error_value"),
    ],
)
def test_energy_error_metadata_invalidates_reading(change: dict, reason: str) -> None:
    resource = Resource(
        path="/heatSources/emon/totalConsumption",
        values=({"compressor": 42},),
        metadata=ResourceMetadata(resource_type="emonValue", **change),
    )
    assert dict(assess_energy(resource).readings)["compressor"].reason == reason


def test_unproven_part_units_and_mixed_containers_are_rejected() -> None:
    resource = parse_resource(
        {
            "id": "/heatSources/emon/totalConsumption",
            "type": "emonValue",
            "unitOfMeasure": "kWh",
            "values": [{"type": "compressor", "value": 2000, "unitOfMeasure": "Wh"}],
        }
    )
    assert _energy_values(resource) == {}
    assert (
        dict(assess_energy(resource).readings)["compressor"].reason
        == "unsupported_part_unit"
    )
    mixed = replace(resource, value={"compressor": 2}, has_value=True)
    assert assess_energy(mixed).reason == "mixed_structure"
    assert _energy_values(mixed) == {}
    assert (
        assess_energy(
            replace(resource, metadata=ResourceMetadata(resource_type="arrayData"))
        ).reason
        == "unsupported_schema"
    )


def test_repeated_identical_energy_parts_preserve_value() -> None:
    resource = Resource(
        path="/heatSources/emon/totalConsumption",
        values=({"compressor": 2}, {"compressor": 2}),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    assert _energy_values(resource) == {"compressor": 2}


def test_dew_point_converts_current_input_unit_before_calculation(
    hass: HomeAssistant,
) -> None:
    entity = _dew_point_sensor(hass, room_temperature=20.0)
    initial = entity.native_value
    path = entity.entity_description.resource_path
    snapshot = entity.coordinator.data[path]
    original = snapshot.resource
    for unit, value, valid in (
        ("degF", 68, True),
        ("unknown", 20, False),
        ("C", 20, True),
    ):
        resource = replace(
            original, value=value, metadata=replace(original.metadata, unit=unit)
        )
        entity.coordinator.data[path] = replace(snapshot, resource=resource)
        assert entity.native_value == (initial if valid else None)


@pytest.mark.parametrize(
    "path",
    [
        "/heatingCircuits/hc2/actualSupplyTemperature",
        "/zones/zone2/heating/manualRoomSetpoint",
    ],
)
def test_celsius_alias_matches_existing_read_and_control_contract(
    hass: HomeAssistant, path: str
) -> None:
    resource = _resource(path, 20, unit="degC")
    assert supports_entity(resource)
    entity = _sensor(hass, resource)
    assert entity.native_value == 20
    assert entity.native_unit_of_measurement == "°C"
    if path.startswith("/zones/"):
        assert number_policy_for_resource(resource) is not None
