"""Tests for dynamically generated Home Assistant sensors."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.pointt.metrics import RequestMetrics
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusActiveFaultsSensor,
    BoschBuderusActiveNotificationsSensor,
    BoschBuderusNextHolidaySensor,
    BoschBuderusRequestMetricSensor,
    BoschBuderusSensor,
    _energy_values,
    _measurement_attributes,
    _native_scalar,
    async_setup_entry,
    build_sensor_descriptions,
)


def _sensor(
    hass: HomeAssistant,
    resource: Resource,
    *,
    value_key: str | None = None,
    available: bool = True,
) -> BoschBuderusSensor:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass,
        AsyncMock(),
        Gateway("gateway-one", device_type="MX300", firmware_version="1.2.3"),
        entry,
    )
    coordinator.resources = {resource.path: resource}
    coordinator.data = {
        resource.path: ResourceSnapshot(resource, available, datetime.now(UTC))
    }
    coordinator.last_update_success = True
    descriptions = build_sensor_descriptions(coordinator.resources)
    description = next(item for item in descriptions if item.value_key == value_key)
    return BoschBuderusSensor(coordinator, description)


def _pressure_sensors(
    hass: HomeAssistant,
    *,
    pressure_value: float = 2.0,
    range_values: dict[str, float] | None = None,
    range_available: bool = True,
) -> tuple[BoschBuderusSensor, BoschBuderusSensor | None]:
    limits = range_values or {
        "highSystemPressure": 2.5,
        "absoluteHighPressure": 3.3,
        "lowSystemPressure": 0.0,
        "shutOfPressureThreshold": 0.6,
        "highPressureThreshold": 2.7,
        "lowPressureThreshold": 0.6,
    }
    pressure = Resource(
        path="/heatSources/systemPressure",
        value=pressure_value,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="bar"),
    )
    pressure_range = Resource(
        path="/heatSources/systemPressureRange",
        values=(limits,),
        metadata=ResourceMetadata(unit="bar"),
    )
    resources = {pressure.path: pressure, pressure_range.path: pressure_range}
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass,
        AsyncMock(),
        Gateway("gateway-one", device_type="MX300"),
        entry,
    )
    coordinator.resources = resources
    now = datetime.now(UTC)
    coordinator.data = {
        pressure.path: ResourceSnapshot(pressure, True, now),
        pressure_range.path: ResourceSnapshot(pressure_range, range_available, now),
    }
    coordinator.last_update_success = True
    descriptions = build_sensor_descriptions(resources)
    pressure_sensor = BoschBuderusSensor(
        coordinator,
        next(item for item in descriptions if item.unique_key == "system_pressure"),
    )
    status_description = next(
        (item for item in descriptions if item.unique_key == "system_pressure_status"),
        None,
    )
    status_sensor = (
        BoschBuderusSensor(coordinator, status_description)
        if status_description is not None
        else None
    )
    return pressure_sensor, status_sensor


def _dew_point_sensor(
    hass: HomeAssistant,
    *,
    circuit: str = "hc1",
    room_temperature: object = 25.0,
    humidity: object = 65.0,
    temperature_available: bool = True,
    humidity_available: bool = True,
) -> BoschBuderusSensor:
    temperature = Resource(
        path=f"/heatingCircuits/{circuit}/roomtemperature",
        value=room_temperature,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )
    humidity_resource = Resource(
        path=f"/heatingCircuits/{circuit}/actualHumidity",
        value=humidity,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="%"),
    )
    resources = {
        temperature.path: temperature,
        humidity_resource.path: humidity_resource,
    }
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass,
        AsyncMock(),
        Gateway("gateway-one", device_type="MX300"),
        entry,
    )
    coordinator.resources = resources
    now = datetime.now(UTC)
    coordinator.data = {
        temperature.path: ResourceSnapshot(temperature, temperature_available, now),
        humidity_resource.path: ResourceSnapshot(
            humidity_resource, humidity_available, now
        ),
    }
    coordinator.last_update_success = True
    description = next(
        item
        for item in build_sensor_descriptions(resources)
        if item.value_kind == "dew_point"
    )
    return BoschBuderusSensor(coordinator, description)


def test_temperature_sensor_value_identity_and_device(hass: HomeAssistant) -> None:
    resource = Resource(
        path="/heatSources/actualSupplyTemperature",
        value=32.5,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )
    sensor = _sensor(hass, resource)

    assert sensor.available
    assert sensor.native_value == 32.5
    assert sensor.unique_id == "gateway-one:gateway:supply_temperature"
    assert sensor.device_info["manufacturer"] == "Bosch Thermotechnology"
    assert sensor.device_info["model"] == "MX300"


def test_optional_heat_source_power_has_safe_measurement_metadata(
    hass: HomeAssistant,
) -> None:
    resource = Resource(
        path="/heatSources/hs7/actualPower",
        value=4.2,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="kW"),
    )

    sensor = _sensor(hass, resource)

    assert sensor.native_value == 4.2
    assert sensor.entity_description.native_unit_of_measurement == UnitOfPower.KILO_WATT
    assert sensor.entity_description.device_class is SensorDeviceClass.POWER
    assert sensor.entity_description.entity_registry_enabled_default

    watts = _sensor(
        hass,
        Resource(
            path="/heatSources/hs8/actualPower",
            value=4200.0,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue", unit="W"),
        ),
    )
    assert watts.entity_description.native_unit_of_measurement == UnitOfPower.WATT


@pytest.mark.parametrize(
    ("path", "unit", "expected_unit", "device_class"),
    (
        (
            "/solarCircuits/sc1/solarYield",
            "kWh",
            UnitOfEnergy.KILO_WATT_HOUR,
            SensorDeviceClass.ENERGY,
        ),
        (
            "/dhwCircuits/dhw1/waterTotalConsumption",
            "L",
            UnitOfVolume.LITERS,
            SensorDeviceClass.WATER,
        ),
        (
            "/dhwCircuits/dhw1/volumeFlow",
            "L/min",
            UnitOfVolumeFlowRate.LITERS_PER_MINUTE,
            SensorDeviceClass.VOLUME_FLOW_RATE,
        ),
    ),
)
def test_optional_measurements_use_home_assistant_units(
    hass: HomeAssistant,
    path: str,
    unit: str,
    expected_unit: str,
    device_class: SensorDeviceClass,
) -> None:
    sensor = _sensor(
        hass,
        Resource(
            path=path,
            value=1.5,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue", unit=unit),
        ),
    )

    assert sensor.entity_description.native_unit_of_measurement == expected_unit
    assert sensor.entity_description.device_class is device_class


@pytest.mark.parametrize(
    ("path", "unit", "expected_unit", "device_class", "state_class"),
    (
        (
            "/dhwCircuits/dhw1/sensor/atmosphericPressure",
            "hPa",
            UnitOfPressure.HPA,
            SensorDeviceClass.PRESSURE,
            SensorStateClass.MEASUREMENT,
        ),
        (
            "/heatSources/electricityTotalConsumption",
            "Wh",
            UnitOfEnergy.WATT_HOUR,
            SensorDeviceClass.ENERGY,
            SensorStateClass.TOTAL_INCREASING,
        ),
        (
            "/dhwCircuits/dhw1/volumeFlow",
            "L",
            UnitOfVolume.LITERS,
            SensorDeviceClass.WATER,
            SensorStateClass.MEASUREMENT,
        ),
        (
            "/heatSources/gasTotalConsumption",
            "m³",
            UnitOfVolume.CUBIC_METERS,
            SensorDeviceClass.GAS,
            SensorStateClass.TOTAL_INCREASING,
        ),
        (
            "/dhwCircuits/dhw1/waterTotalConsumption",
            "m3",
            UnitOfVolume.CUBIC_METERS,
            SensorDeviceClass.WATER,
            SensorStateClass.TOTAL_INCREASING,
        ),
        (
            "/ventilation/zone1/filter/remainingTime",
            "min",
            UnitOfTime.MINUTES,
            SensorDeviceClass.DURATION,
            SensorStateClass.MEASUREMENT,
        ),
        (
            "/ventilation/zone1/filter/maxRunTime",
            "hours",
            UnitOfTime.HOURS,
            SensorDeviceClass.DURATION,
            SensorStateClass.MEASUREMENT,
        ),
        (
            "/devices/device1/rfTimeofConnectionLost",
            "seconds",
            UnitOfTime.SECONDS,
            SensorDeviceClass.DURATION,
            SensorStateClass.MEASUREMENT,
        ),
        (
            "/system/lowNoise/duration",
            "days",
            UnitOfTime.DAYS,
            SensorDeviceClass.DURATION,
            SensorStateClass.MEASUREMENT,
        ),
    ),
)
def test_extended_measurement_unit_mapping(
    path: str,
    unit: str,
    expected_unit: str,
    device_class: SensorDeviceClass,
    state_class: SensorStateClass,
) -> None:
    resource = Resource(
        path=path,
        value=1.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit=unit),
    )

    actual_unit, actual_class, actual_state_class, scale = _measurement_attributes(
        resource, None
    )

    assert (actual_unit, actual_class, actual_state_class, scale) == (
        expected_unit,
        device_class,
        state_class,
        1.0,
    )


@pytest.mark.parametrize(
    ("path", "device_class"),
    (
        (
            "/heatingCircuits/hc1/actualHumidity",
            SensorDeviceClass.HUMIDITY,
        ),
        (
            "/zones/zone1/averageActualHumidity",
            SensorDeviceClass.HUMIDITY,
        ),
        ("/devices/device1/actualHumidity", SensorDeviceClass.HUMIDITY),
        (
            "/ventilation/zone1/maxRelativeHumidity",
            SensorDeviceClass.HUMIDITY,
        ),
        ("/devices/device1/battery", SensorDeviceClass.BATTERY),
    ),
)
def test_known_percentage_semantics_use_specific_device_class(
    path: str,
    device_class: SensorDeviceClass,
) -> None:
    resource = Resource(
        path=path,
        value=50.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="%"),
    )

    assert _measurement_attributes(resource, None) == (
        PERCENTAGE,
        device_class,
        SensorStateClass.MEASUREMENT,
        1.0,
    )


@pytest.mark.parametrize(
    "path",
    (
        "/heatingCircuits/hc1/pumpModulation",
        "/system/silentMode/powerReduction",
        "/devices/device1/signal",
        "/unknown/actualHumidity",
    ),
)
def test_other_percentage_resources_remain_generic(path: str) -> None:
    resource = Resource(
        path=path,
        value=50.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="%"),
    )

    assert _measurement_attributes(resource, None) == (
        PERCENTAGE,
        None,
        SensorStateClass.MEASUREMENT,
        1.0,
    )


def test_semantic_percentage_class_requires_percentage_unit() -> None:
    resource = Resource(
        path="/heatingCircuits/hc1/actualHumidity",
        value=20.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )

    assert _measurement_attributes(resource, None) == (
        UnitOfTemperature.CELSIUS,
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
        1.0,
    )


def test_optional_string_boolean_uses_translated_on_off_enum(
    hass: HomeAssistant,
) -> None:
    sensor = _sensor(
        hass,
        Resource(
            path="/system/powerLimitation/active",
            value="on",
            has_value=True,
            metadata=ResourceMetadata(resource_type="stringValue"),
        ),
    )

    assert sensor.entity_description.translation_key == "on_off_state"
    assert sensor.native_value == "on"


def test_optional_auxiliary_heater_mode_uses_app_translation(
    hass: HomeAssistant,
) -> None:
    resource = Resource(
        path="/heatSources/additionalHeater/operationMode",
        value="manual",
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue"),
    )

    sensor = _sensor(hass, resource)

    assert sensor.native_value == "manual"
    assert sensor.entity_description.translation_key == (
        "auxiliary_heater_operation_mode"
    )
    assert sensor.entity_description.options == ["off", "manual", "auto"]
    assert sensor.entity_description.entity_category is EntityCategory.DIAGNOSTIC
    assert not sensor.entity_description.entity_registry_enabled_default


def test_optional_text_defrost_state_uses_on_off_translation(
    hass: HomeAssistant,
) -> None:
    resource = Resource(
        path="/heatSources/hs3/defrostActive",
        value="on",
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue"),
    )

    sensor = _sensor(hass, resource)

    assert sensor.native_value == "on"
    assert sensor.entity_description.translation_key == "on_off_state"


def test_dynamic_circuit_uses_configured_name_and_stable_device(
    hass: HomeAssistant,
) -> None:
    resource = Resource(
        path="/heatingCircuits/hc2/operationMode",
        value="auto",
        has_value=True,
    )
    sensor = _sensor(hass, resource)
    name_resource = Resource(
        path="/heatingCircuits/hc2/name", value="Obergeschoss", has_value=True
    )
    sensor.coordinator.data[name_resource.path] = ResourceSnapshot(
        name_resource, True, datetime.now(UTC)
    )
    sensor = BoschBuderusSensor(sensor.coordinator, sensor.entity_description)

    assert sensor.native_value == "auto"
    assert sensor.entity_description.device_class is SensorDeviceClass.ENUM
    assert sensor.entity_description.translation_key == "heating_circuit_operation_mode"
    assert sensor.entity_description.options == ["off", "manual", "auto"]
    assert sensor.device_info["name"] == "Bosch Thermotechnology MX300"
    assert sensor.device_info["identifiers"] == {(DOMAIN, "gateway-one")}
    assert sensor.name == "Obergeschoss \N{EN DASH} Operation mode"


def test_unavailable_resource_retains_value_but_marks_entity_unavailable(
    hass: HomeAssistant,
) -> None:
    resource = Resource(
        path="/heatingCircuits/hc1/overallStatus",
        value="summer_idle",
        has_value=True,
    )
    sensor = _sensor(hass, resource, available=False)

    assert not sensor.available
    assert sensor.native_value == "summer_idle"
    assert sensor.entity_description.device_class is SensorDeviceClass.ENUM
    assert sensor.entity_description.translation_key == "heating_circuit_overall_status"


def test_enum_includes_pointt_advertised_and_current_unknown_values(
    hass: HomeAssistant,
) -> None:
    resource = Resource(
        path="/heatingCircuits/hc1/switchProgramMode",
        value="vendor_extension",
        has_value=True,
        metadata=ResourceMetadata(allowed_values=("level", "clock")),
    )

    sensor = _sensor(hass, resource)

    assert sensor.native_value == "vendor_extension"
    assert sensor.entity_description.translation_key == (
        "heating_circuit_switch_program_mode"
    )
    assert sensor.entity_description.options == [
        "level",
        "clock",
        "vendor_extension",
    ]


def test_enum_changed_after_setup_becomes_unknown_without_log_spam(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    resource = Resource(
        path="/heatingCircuits/hc1/overallStatus",
        value="summer_idle",
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue"),
    )
    sensor = _sensor(hass, resource)
    caplog.set_level(
        logging.WARNING,
        logger="custom_components.bosch_buderus_heating.sensor",
    )

    first_unknown = Resource(
        path=resource.path,
        value="private_vendor_state_one",
        has_value=True,
        metadata=resource.metadata,
    )
    sensor.coordinator.data = {
        resource.path: ResourceSnapshot(first_unknown, True, datetime.now(UTC))
    }

    assert sensor.native_value is None
    assert sensor.native_value is None
    assert sensor.coordinator.unknown_enum_value_count(resource.path) == 1

    second_unknown = Resource(
        path=resource.path,
        value="private_vendor_state_two",
        has_value=True,
        metadata=resource.metadata,
    )
    sensor.coordinator.data = {
        resource.path: ResourceSnapshot(second_unknown, True, datetime.now(UTC))
    }

    assert sensor.native_value is None
    assert sensor.coordinator.unknown_enum_value_count(resource.path) == 2
    assert caplog.text.count("returned an undeclared enum value") == 1
    assert "/heatingCircuits/{hc}/overallStatus" in caplog.text
    assert "private_vendor_state" not in caplog.text
    assert "gateway-one" not in caplog.text


def test_gas_boiler_heat_source_type_uses_canonical_boiler_state(
    hass: HomeAssistant,
) -> None:
    resource = Resource(
        path="/heatSources/hs1/type",
        value="gas_boiler",
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue"),
    )

    sensor = _sensor(hass, resource)

    assert sensor.native_value == "boiler"
    assert sensor.entity_description.options == ["heatpump", "boiler", "hybrid"]


def test_enum_translates_single_value_list_resource(hass: HomeAssistant) -> None:
    resource = Resource(
        path="/system/variableTariff/supportStatus",
        values=("not_supported",),
    )

    sensor = _sensor(hass, resource, value_key="values")

    assert sensor.native_value == "not_supported"
    assert sensor.entity_description.device_class is SensorDeviceClass.ENUM
    assert sensor.entity_description.translation_key == "support_status"
    assert sensor.entity_description.options == ["not_supported", "supported", "active"]


def test_known_array_entities_exist_when_empty_at_startup(
    hass: HomeAssistant,
) -> None:
    demand = Resource(
        path="/heatSources/actualHeatDemand",
        metadata=ResourceMetadata(resource_type="arrayData"),
    )
    demand_sensor = _sensor(hass, demand, value_key="values")

    assert demand_sensor.native_value == "none"
    assert demand_sensor.entity_description.translation_key == "heat_demand"
    assert demand_sensor.entity_description.options == [
        "none",
        "ch",
        "dhw",
        "frost",
        "ch_dhw",
        "ch_frost",
        "dhw_frost",
        "ch_dhw_frost",
    ]

    active = Resource(
        path=demand.path,
        values=("ch", "dhw"),
        metadata=demand.metadata,
    )
    demand_sensor.coordinator.data[demand.path] = ResourceSnapshot(
        active, True, datetime.now(UTC)
    )
    assert demand_sensor.native_value == "ch_dhw"

    support = Resource(
        path="/system/variableTariff/supportStatus",
        metadata=ResourceMetadata(resource_type="arrayData"),
    )
    support_sensor = _sensor(hass, support, value_key="values")
    assert support_sensor.native_value is None
    assert support_sensor.entity_description.translation_key == "support_status"


@pytest.mark.parametrize(
    ("path", "value", "translation_key", "options"),
    [
        (
            "/heatSources/compressor/status",
            "dhw",
            "compressor_status",
            [
                "off",
                "heating",
                "cooling",
                "dhw",
                "pool",
                "pool_heat",
                "defrost",
                "alarm",
            ],
        ),
        (
            "/heatSources/Source/eHeater/status",
            "off",
            "electric_auxiliary_heater_status",
            ["off", "heating", "dhw", "pool", "pool_heat", "defrost", "alarm"],
        ),
        (
            "/system/globalSeasonOptimizer/currentMode",
            "automatic",
            "season_optimizer_mode",
            ["off", "automatic", "forced_heat", "forced_cool"],
        ),
        (
            "/heatingCircuits/hc1/currentSuWiMode",
            "cooling",
            "heating_circuit_summer_winter_mode",
            ["off", "forced", "cooling"],
        ),
        (
            "/heatingCircuits/hc1/suWiSwitchMode",
            "cooling",
            "heating_circuit_summer_winter_switch_mode",
            ["off", "automatic", "forced", "cooling"],
        ),
        (
            "/heatingCircuits/hc1/heatCoolMode",
            "cool",
            "heating_circuit_heat_cool_mode",
            ["heat", "cool", "heat_cool"],
        ),
    ],
)
def test_hidden_status_resources_are_translated_enums(
    hass: HomeAssistant,
    path: str,
    value: str,
    translation_key: str,
    options: list[str],
) -> None:
    sensor = _sensor(
        hass,
        Resource(
            path=path,
            value=value,
            has_value=True,
            metadata=ResourceMetadata(resource_type="stringValue"),
        ),
    )

    assert sensor.native_value == value
    assert sensor.entity_description.translation_key == translation_key
    assert sensor.entity_description.options == options


def test_hidden_status_pointt_camel_case_is_normalized(
    hass: HomeAssistant,
) -> None:
    data_processing = _sensor(
        hass,
        Resource(
            path="/gateway/dataProcessing/status",
            value="inProgress",
            has_value=True,
            metadata=ResourceMetadata(resource_type="stringValue"),
        ),
    )
    isrc = _sensor(
        hass,
        Resource(
            path="/system/iSRC/supportStatus",
            value="notSupportedIncompatibleController",
            has_value=True,
            metadata=ResourceMetadata(resource_type="stringValue"),
        ),
    )
    season_optimizer = _sensor(
        hass,
        Resource(
            path="/system/globalSeasonOptimizer/currentMode",
            value="forcedCool",
            has_value=True,
            metadata=ResourceMetadata(resource_type="stringValue"),
        ),
    )
    heat_cool_support = _sensor(
        hass,
        Resource(
            path="/heatingCircuits/hc1/heatCoolMode",
            value="heatCool",
            has_value=True,
            metadata=ResourceMetadata(resource_type="stringValue"),
        ),
    )

    assert data_processing.native_value == "in_progress"
    assert "inProgress" not in data_processing.entity_description.options
    assert isrc.native_value == "not_supported_incompatible_controller"
    assert "notSupportedIncompatibleController" not in isrc.entity_description.options
    assert season_optimizer.native_value == "forced_cool"
    assert "forcedCool" not in season_optimizer.entity_description.options
    assert heat_cool_support.native_value == "heat_cool"
    assert "heatCool" not in heat_cool_support.entity_description.options


@pytest.mark.parametrize(
    ("path", "value", "expected_options"),
    [
        (
            "/heatingCircuits/hc1/overallStatus",
            "ch_enabled",
            {
                "ch_enabled",
                "ch_disabled",
                "emergency_mode",
                "floor_drying",
                "summer_idle",
                "boost",
                "away",
                "holiday",
                "cooling_manual_on",
                "cooling_manual_off",
                "heating_manual_on",
                "heating_manual_off",
                "heating_auto",
            },
        ),
        (
            "/dhwCircuits/dhw1/overallStatus",
            "manual_on_eco",
            {
                "dhw_enabled",
                "dhw_disabled",
                "auto",
                "manual_off",
                "manual_on_low",
                "manual_on_eco",
                "manual_on_high",
                "extra",
                "away",
                "holiday",
                "floor_drying",
                "td",
            },
        ),
    ],
)
def test_apk_confirmed_overall_status_values_are_complete(
    hass: HomeAssistant,
    path: str,
    value: str,
    expected_options: set[str],
) -> None:
    sensor = _sensor(hass, Resource(path=path, value=value, has_value=True))

    assert sensor.native_value == value
    assert set(sensor.entity_description.options or ()) == expected_options


def test_known_multipart_entities_survive_empty_or_partial_startup() -> None:
    working_time = Resource(
        path="/heatSources/hs1/workingTime",
        values=({"total": 3600},),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    descriptions = build_sensor_descriptions({working_time.path: working_time})

    assert {item.value_key for item in descriptions} == {
        "total",
        "ch",
        "cooling",
        "dhw",
    }


def test_pointt_enum_spelling_is_normalized_only_at_ha_boundary(
    hass: HomeAssistant,
) -> None:
    resource = Resource(path="/heatSources/hs1/type", value="Heatpump", has_value=True)

    sensor = _sensor(hass, resource)

    assert resource.value == "Heatpump"
    assert sensor.native_value == "heatpump"
    assert sensor.entity_description.options == ["heatpump", "boiler", "hybrid"]


def test_energy_resources_expand_and_validate_totals(hass: HomeAssistant) -> None:
    resource = Resource(
        path="/heatSources/emon/totalConsumption",
        values=(
            {"compressor": 42.58},
            {"eheater": 0},
            {"outputProduced": 98.15},
        ),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    descriptions = build_sensor_descriptions({resource.path: resource})

    assert {item.value_key for item in descriptions} == {
        "compressor",
        "eheater",
        "environmental_energy",
        "outputProduced",
        "total_electricity",
    }
    total = _sensor(hass, resource, value_key="total_electricity")
    assert total.native_value == 42.58
    assert total.extra_state_attributes == {
        "value_source": "calculated",
        "calculation": "compressor + auxiliary_heater",
    }
    assert _sensor(hass, resource, value_key="outputProduced").native_value == 98.15
    assert _sensor(
        hass, resource, value_key="environmental_energy"
    ).native_value == pytest.approx(55.57)


def test_energy_resources_prefer_direct_total_without_duplicate(
    hass: HomeAssistant,
) -> None:
    resource = Resource(
        path="/heatSources/emon/totalConsumption",
        values=(
            {"electricity": 48.5},
            {"compressor": 42.58},
            {"eheater": 1.2},
            {"outputProduced": 98.15},
        ),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    descriptions = build_sensor_descriptions({resource.path: resource})

    assert [item.value_key for item in descriptions].count("total_electricity") == 1
    assert not any(item.value_key == "electricity" for item in descriptions)
    total = _sensor(hass, resource, value_key="total_electricity")
    assert total.native_value == 48.5
    assert total.extra_state_attributes == {"value_source": "direct"}


def test_environmental_energy_requires_complete_non_negative_balance(
    hass: HomeAssistant,
) -> None:
    incomplete = Resource(
        path="/heatSources/emon/totalConsumption",
        values=({"compressor": 12.0}, {"outputProduced": 30.0}),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    assert not any(
        item.value_key == "environmental_energy"
        for item in build_sensor_descriptions({incomplete.path: incomplete})
    )

    inconsistent = Resource(
        path="/heatSources/emon/totalConsumption",
        values=(
            {"compressor": 20.0},
            {"eheater": 2.0},
            {"outputProduced": 10.0},
        ),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    assert (
        _sensor(hass, inconsistent, value_key="environmental_energy").native_value
        is None
    )

    rounding_edge = Resource(
        path="/heatSources/emon/totalConsumption",
        values=(
            {"compressor": 0.1},
            {"eheater": 0.2},
            {"outputProduced": 0.3},
        ),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    assert (
        _sensor(hass, rounding_edge, value_key="environmental_energy").native_value == 0
    )


def test_energy_resources_accept_typed_pointt_items_and_empty_startup_shape(
    hass: HomeAssistant,
) -> None:
    typed = Resource(
        path="/heatSources/emon/chConsumption",
        values=(
            {"type": "compressor", "value": 12.5},
            {"type": "eheater", "value": 1.5},
        ),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    assert _sensor(hass, typed, value_key="total_electricity").native_value == 14

    empty = Resource(
        path="/heatSources/emon/dhwConsumption",
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    descriptions = build_sensor_descriptions({empty.path: empty})
    assert {item.value_key for item in descriptions} == {
        "compressor",
        "eheater",
        "outputProduced",
        "total_electricity",
    }


def test_nested_values_and_runtime_scaling(hass: HomeAssistant) -> None:
    pressure = Resource(
        path="/heatSources/systemPressureRange",
        values=(
            {
                "highSystemPressure": 2.5,
                "absoluteHighPressure": 3.3,
                "lowSystemPressure": 0.0,
                "shutOfPressureThreshold": 0.6,
                "highPressureThreshold": 2.7,
                "lowPressureThreshold": 0.6,
            },
        ),
        metadata=ResourceMetadata(unit="bar"),
    )
    descriptions = build_sensor_descriptions({pressure.path: pressure})
    assert {item.value_key for item in descriptions} == {
        "highSystemPressure",
        "absoluteHighPressure",
        "lowSystemPressure",
        "shutOfPressureThreshold",
        "highPressureThreshold",
        "lowPressureThreshold",
    }
    assert all(
        item.entity_category is EntityCategory.DIAGNOSTIC for item in descriptions
    )
    assert _sensor(hass, pressure, value_key="lowPressureThreshold").native_value == 0.6

    runtime = Resource(
        path="/heatSources/hs1/workingTime",
        values=({"total": 3600},),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    assert all(
        item.entity_category is EntityCategory.DIAGNOSTIC
        for item in build_sensor_descriptions({runtime.path: runtime})
    )
    assert _sensor(hass, runtime, value_key="total").native_value == 1.0


def test_system_pressure_exposes_validated_limits_and_derived_status(
    hass: HomeAssistant,
) -> None:
    pressure_sensor, status_sensor = _pressure_sensors(hass)

    assert pressure_sensor.extra_state_attributes == {
        "technical_minimum_bar": 0.0,
        "shutdown_pressure_bar": 0.6,
        "normal_minimum_bar": 0.6,
        "normal_maximum_bar": 2.5,
        "upper_pressure_limit_bar": 2.7,
        "absolute_maximum_bar": 3.3,
    }
    assert status_sensor is not None
    assert status_sensor.available
    assert status_sensor.native_value == "normal"
    assert status_sensor.name == (
        "Heat generator \N{EN DASH} System pressure status (calculated)"
    )
    assert status_sensor.entity_description.translation_key == "pressure_status"

    now = datetime.now(UTC)
    for pressure, expected in (
        (0.6, "critical_low"),
        (0.7, "normal"),
        (2.5, "high"),
        (2.69, "high"),
        (2.7, "critical_high"),
        (3.4, "critical_high"),
    ):
        resource = Resource(
            path="/heatSources/systemPressure", value=pressure, has_value=True
        )
        status_sensor.coordinator.data[resource.path] = ResourceSnapshot(
            resource, True, now
        )
        assert status_sensor.native_value == expected

    low_range = {
        "highSystemPressure": 2.5,
        "absoluteHighPressure": 3.3,
        "lowSystemPressure": 0.0,
        "shutOfPressureThreshold": 0.4,
        "highPressureThreshold": 2.7,
        "lowPressureThreshold": 0.6,
    }
    _, low_status_sensor = _pressure_sensors(
        hass, pressure_value=0.5, range_values=low_range
    )
    assert low_status_sensor is not None
    assert low_status_sensor.native_value == "low"


def test_pressure_status_requires_complete_plausible_limits(
    hass: HomeAssistant,
) -> None:
    incomplete = {
        "highSystemPressure": 2.5,
        "absoluteHighPressure": 3.3,
        "lowSystemPressure": 0.0,
        "shutOfPressureThreshold": 0.6,
        "highPressureThreshold": 2.7,
    }
    pressure_sensor, status_sensor = _pressure_sensors(hass, range_values=incomplete)
    assert pressure_sensor.extra_state_attributes is None
    assert status_sensor is None

    implausible = {**incomplete, "lowPressureThreshold": 2.8}
    pressure_sensor, status_sensor = _pressure_sensors(hass, range_values=implausible)
    assert pressure_sensor.extra_state_attributes is None
    assert status_sensor is None


def test_pressure_status_becomes_unavailable_with_range_resource(
    hass: HomeAssistant,
) -> None:
    pressure_sensor, status_sensor = _pressure_sensors(hass, range_available=False)

    assert pressure_sensor.extra_state_attributes is None
    assert status_sensor is not None
    assert not status_sensor.available
    assert status_sensor.native_value is None


def test_calculated_dew_point_uses_same_circuit_measurements(
    hass: HomeAssistant,
) -> None:
    sensor = _dew_point_sensor(hass)

    assert sensor.available
    assert sensor.native_value == pytest.approx(17.96)
    assert sensor.name == "Heating circuit 1 \N{EN DASH} Dew point (calculated)"
    assert sensor.unique_id == ("gateway-one:heatingCircuits:hc1:calculated_dew_point")
    assert sensor.entity_description.device_class is SensorDeviceClass.TEMPERATURE
    assert sensor.entity_description.state_class is SensorStateClass.MEASUREMENT
    assert (
        sensor.entity_description.native_unit_of_measurement
        is UnitOfTemperature.CELSIUS
    )
    assert sensor.entity_description.suggested_display_precision == 1
    assert sensor.extra_state_attributes == {
        "source_room_temperature_c": 25.0,
        "source_relative_humidity_percent": 65.0,
        "calculation_method": "magnus",
        "magnus_a": 17.62,
        "magnus_b_c": 243.12,
    }


def test_calculated_dew_point_uses_localized_german_name(
    hass: HomeAssistant,
) -> None:
    hass.config.language = "de"

    sensor = _dew_point_sensor(hass)

    assert sensor.name == "Heizkreis 1 \N{EN DASH} Taupunkt (berechnet)"


def test_calculated_dew_point_is_created_for_each_complete_circuit() -> None:
    resources: dict[str, Resource] = {}
    for circuit in ("hc1", "hc2"):
        temperature = Resource(
            path=f"/heatingCircuits/{circuit}/roomtemperature",
            value=21.0,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
        )
        humidity = Resource(
            path=f"/heatingCircuits/{circuit}/actualHumidity",
            value=50.0,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue", unit="%"),
        )
        resources[temperature.path] = temperature
        resources[humidity.path] = humidity

    descriptions = tuple(
        item
        for item in build_sensor_descriptions(resources)
        if item.value_kind == "dew_point"
    )

    assert [item.unique_key for item in descriptions] == [
        "heatingCircuits:hc1:calculated_dew_point",
        "heatingCircuits:hc2:calculated_dew_point",
    ]
    assert [item.secondary_resource_path for item in descriptions] == [
        "/heatingCircuits/hc1/actualHumidity",
        "/heatingCircuits/hc2/actualHumidity",
    ]


def test_calculated_dew_point_requires_both_valid_capabilities() -> None:
    temperature = Resource(
        path="/heatingCircuits/hc1/roomtemperature",
        value=25.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )
    humidity = Resource(
        path="/heatingCircuits/hc1/actualHumidity",
        value=65.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="%"),
    )
    other_circuit_humidity = Resource(
        path="/heatingCircuits/hc2/actualHumidity",
        value=55.0,
        has_value=True,
        metadata=humidity.metadata,
    )
    wrong_unit = Resource(
        path=humidity.path,
        value=65.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )

    for resources in (
        {temperature.path: temperature},
        {
            temperature.path: temperature,
            other_circuit_humidity.path: other_circuit_humidity,
        },
        {temperature.path: temperature, wrong_unit.path: wrong_unit},
    ):
        assert not any(
            item.value_kind == "dew_point"
            for item in build_sensor_descriptions(resources)
        )


def test_calculated_dew_point_follows_input_availability(
    hass: HomeAssistant,
) -> None:
    sensor = _dew_point_sensor(hass, humidity_available=False)

    assert not sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


def test_calculated_dew_point_rejects_invalid_runtime_value(
    hass: HomeAssistant,
) -> None:
    sensor = _dew_point_sensor(hass, humidity=0.0)

    assert sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


def test_entity_categories_follow_home_assistant_sections() -> None:
    resources = {
        item.path: item
        for item in (
            Resource(
                path="/system/sensors/temperatures/outdoor_t1",
                value=12.3,
                has_value=True,
            ),
            Resource(path="/system/awayMode/enabled", value="off", has_value=True),
            Resource(
                path="/heatingCircuits/hc1/maxFlowTemp",
                value=40,
                has_value=True,
            ),
            Resource(
                path="/heatSources/hs1/supplyFlowCondenserTemp",
                value=28.3,
                has_value=True,
            ),
        )
    }

    descriptions = {
        item.resource_path: item for item in build_sensor_descriptions(resources)
    }
    assert (
        descriptions["/system/sensors/temperatures/outdoor_t1"].entity_category is None
    )
    assert (
        descriptions["/system/awayMode/enabled"].entity_category
        is EntityCategory.DIAGNOSTIC
    )
    assert not descriptions["/system/awayMode/enabled"].entity_registry_enabled_default
    assert (
        descriptions["/heatingCircuits/hc1/maxFlowTemp"].entity_category
        is EntityCategory.DIAGNOSTIC
    )
    assert (
        descriptions["/heatSources/hs1/supplyFlowCondenserTemp"].entity_category
        is EntityCategory.DIAGNOSTIC
    )


def test_configured_names_are_decoded_and_empty_resources_are_skipped(
    hass: HomeAssistant,
) -> None:
    encoded_name = Resource(
        path="/dhwCircuits/dhw1/name",
        value="AFcAYQByAG0AdwBhAHMAcwBlAHI=",
        has_value=True,
    )
    assert _sensor(hass, encoded_name).native_value == "Warmwasser"

    empty_program = Resource(
        path="/heatingCircuits/hc1/switchPrograms/A",
        metadata=ResourceMetadata(resource_type="switchProgram", writable=True),
    )
    assert build_sensor_descriptions({empty_program.path: empty_program}) == ()

    empty_name = Resource(path="/heatingCircuits/hc1/name", has_value=True)
    assert build_sensor_descriptions({empty_name.path: empty_name}) == ()


def test_value_helpers_reject_unsafe_or_non_scalar_values() -> None:
    resource = Resource(
        path="/energy",
        values=(
            "ignored",
            {"boolean": True, "text": "4", "negative": -1, "nan": float("nan")},
            {"valid": 4},
        ),
    )

    assert _energy_values(resource) == {"valid": 4.0}
    assert _native_scalar(True) is None
    assert _native_scalar([1]) is None
    assert _native_scalar(32767.0) is None
    assert _native_scalar(float("inf")) is None
    assert _native_scalar("heating") == "heating"


def test_boolean_values_are_reserved_for_binary_sensors() -> None:
    resource = Resource(
        path="/system/awayMode/enabled",
        value=False,
        has_value=True,
        metadata=ResourceMetadata(resource_type="booleanValue"),
    )

    assert build_sensor_descriptions({resource.path: resource}) == ()


def test_structured_values_do_not_raise_while_rendering_labels(
    hass: HomeAssistant,
) -> None:
    resource = Resource(
        path="/heatingCircuits/hc1/switchPrograms/A",
        values=({"periods": ["06:00", "22:00"]},),
    )

    assert _sensor(hass, resource, value_key="values").native_value == (
        "{'periods': ['06:00', '22:00']}"
    )


def test_system_info_is_sanitized_opt_in_diagnostic(hass: HomeAssistant) -> None:
    resource = Resource(
        path="/system/info",
        values=(
            {
                "ProductName": "Controller",
                "Ver": "1.2.3",
                "ModuleSerialNumber": "module-123",
                "Tok": "must-never-be-exposed",
                "Unknown": "ignored",
            },
            "ignored",
        ),
        metadata=ResourceMetadata(resource_type="systeminfo"),
    )
    sensor = _sensor(hass, resource)

    assert sensor.native_value == "Controller · Version 1.2.3"
    assert sensor.entity_description.entity_category is EntityCategory.DIAGNOSTIC
    assert not sensor.entity_description.entity_registry_enabled_default
    assert sensor.extra_state_attributes == {
        "modul_1_produktname": "Controller",
        "modul_1_modul_seriennummer": "module-123",
        "modul_1_version": "1.2.3",
    }


def test_system_info_text_is_bounded_and_has_a_fallback(
    hass: HomeAssistant,
) -> None:
    long_modules = Resource(
        path="/system/info",
        values=tuple(
            {"ProductName": f"Module {index} " + "x" * 100, "Ver": "1.0"}
            for index in range(5)
        ),
        metadata=ResourceMetadata(resource_type="systeminfo"),
    )
    state = _sensor(hass, long_modules).native_value
    assert isinstance(state, str)
    assert len(state) <= 255
    assert "weitere" in state

    empty = Resource(
        path="/system/info",
        values=("ignored",),
        metadata=ResourceMetadata(resource_type="systeminfo"),
    )
    assert _sensor(hass, empty).native_value == "Keine Module erkannt"


def test_identity_values_are_available_as_disabled_diagnostics() -> None:
    resources = {
        path: Resource(path=path, value=value, has_value=True)
        for path, value in (
            ("/gateway/serialId", "serial"),
            ("/gateway/uuid", "uuid"),
            ("/system/country", "DE"),
        )
    }

    descriptions = build_sensor_descriptions(resources)

    assert {item.resource_path for item in descriptions} == set(resources)
    assert all(
        item.entity_category is EntityCategory.DIAGNOSTIC
        and not item.entity_registry_enabled_default
        for item in descriptions
    )


async def test_platform_adds_every_discovered_safe_scalar(
    hass: HomeAssistant,
) -> None:
    safe = Resource(path="/system/brand", value="Buderus", has_value=True)
    private = Resource(path="/gateway/wifi/mac", value="secret", has_value=True)
    sensor = _sensor(hass, safe)
    sensor.coordinator.resources = {safe.path: safe, private.path: private}
    entry = SimpleNamespace(
        entry_id="entry-one",
        runtime_data=SimpleNamespace(
            client=SimpleNamespace(metrics=RequestMetrics()),
            coordinators=(sensor.coordinator,),
        ),
    )
    added: list[object] = []

    await async_setup_entry(hass, entry, added.extend)

    assert len(added) == 6
    metric_sensors = [
        entity
        for entity in added
        if isinstance(entity, BoschBuderusRequestMetricSensor)
    ]
    assert len(metric_sensors) == 3
    assert all(
        not entity.entity_description.entity_registry_enabled_default
        and entity.entity_description.entity_category is EntityCategory.DIAGNOSTIC
        and entity.device_info is None
        for entity in metric_sensors
    )
    metrics_by_key = {
        entity.entity_description.key: entity for entity in metric_sensors
    }
    assert metrics_by_key["pointt_api_requests_total"].native_value == 0
    assert metrics_by_key["pointt_api_requests_last_hour"].native_value == 0
    assert metrics_by_key["pointt_api_response_time_last_hour"].native_value is None
    assert (
        metrics_by_key["pointt_api_requests_total"].extra_state_attributes[
            "requests_successful"
        ]
        == 0
    )
    assert (
        metrics_by_key["pointt_api_requests_last_hour"].extra_state_attributes[
            "requests_by_type"
        ]
        == {}
    )
    assert (
        metrics_by_key["pointt_api_response_time_last_hour"].extra_state_attributes[
            "successful_response_time_samples"
        ]
        == 0
    )
    assert any(isinstance(entity, BoschBuderusActiveFaultsSensor) for entity in added)
    assert any(
        isinstance(entity, BoschBuderusActiveNotificationsSensor) for entity in added
    )
    dynamic = next(entity for entity in added if isinstance(entity, BoschBuderusSensor))
    assert dynamic.entity_description.resource_path == safe.path


async def test_next_holiday_sensor_exposes_upcoming_period(
    hass: HomeAssistant,
) -> None:
    holiday = Resource(
        path="/holidayMode/list",
        value=[
            {
                "start": "2099-08-25T01:45:00+02:00",
                "end": "2099-09-01T01:45:00+02:00",
            }
        ],
        has_value=True,
    )
    coordinator = _sensor(
        hass, Resource(path="/system/brand", value="Buderus", has_value=True)
    ).coordinator
    coordinator.resources = {holiday.path: holiday}
    coordinator.data = {
        holiday.path: ResourceSnapshot(holiday, True, datetime.now(UTC))
    }
    entry = SimpleNamespace(
        entry_id="entry-one",
        runtime_data=SimpleNamespace(
            client=SimpleNamespace(metrics=RequestMetrics()),
            coordinators=(coordinator,),
        ),
    )
    added: list[object] = []

    await async_setup_entry(hass, entry, added.extend)

    next_holiday = next(
        entity for entity in added if isinstance(entity, BoschBuderusNextHolidaySensor)
    )
    assert next_holiday.available
    assert next_holiday.native_value == datetime.fromisoformat(
        "2099-08-25T01:45:00+02:00"
    )
    assert next_holiday.extra_state_attributes == {
        "end": "2099-09-01T01:45:00+02:00",
        "active": False,
        "all_day": False,
    }
    assert next_holiday.unique_id == "gateway-one:next_holiday"
    assert next_holiday.device_info["model"] == "MX300"


def test_unknown_scalar_capability_remains_diagnostics_only() -> None:
    unknown = Resource(
        path="/heatSources/vendorSpecificEfficiency",
        value=98.5,
        has_value=True,
    )

    assert build_sensor_descriptions({unknown.path: unknown}) == ()


def test_fault_count_sensors_expose_bounded_details(hass: HomeAssistant) -> None:
    resource = Resource(
        path="/notifications",
        values=(
            {"ccd": 6249, "fc": "12"},
            {"ccd": "W1", "fc": "WARNING"},
        ),
    )
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type="K40"), entry
    )
    coordinator.last_update_success = True
    coordinator.faults.process_resources({resource.path: resource})

    faults = BoschBuderusActiveFaultsSensor(coordinator)
    notifications = BoschBuderusActiveNotificationsSensor(coordinator)

    assert faults.available
    assert faults.native_value == 1
    assert faults.extra_state_attributes["faults"][0]["code"] == "6249"
    assert notifications.native_value == 2
    assert notifications.extra_state_attributes["severity_counts"] == {
        "fault": 1,
        "warning": 1,
    }
    assert notifications.device_info["model"] == "Heating system"


def test_default_policy_enables_user_values_and_keeps_technical_values_opt_in() -> None:
    resources = {
        item.path: item
        for item in (
            Resource(
                path="/heatSources/actualSupplyTemperature",
                value=35.0,
                has_value=True,
            ),
            Resource(
                path="/heatSources/hs1/workingTime",
                values=({"total": 3600},),
            ),
            Resource(
                path="/gateway/versionFirmware",
                value="1.2.3",
                has_value=True,
            ),
            Resource(
                path="/heatingCircuits/hc1/manualRoomSetpoint",
                value=20.0,
                has_value=True,
                metadata=ResourceMetadata(writable=True),
            ),
        )
    }

    descriptions = {
        item.resource_path: item for item in build_sensor_descriptions(resources)
    }
    assert descriptions[
        "/heatSources/actualSupplyTemperature"
    ].entity_registry_enabled_default
    assert descriptions["/heatSources/hs1/workingTime"].entity_registry_enabled_default
    assert not descriptions["/gateway/versionFirmware"].entity_registry_enabled_default
    assert not descriptions[
        "/heatingCircuits/hc1/manualRoomSetpoint"
    ].entity_registry_enabled_default
