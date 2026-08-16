"""Tests for dynamically generated Home Assistant sensors."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory
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
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    _energy_values,
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
    assert _sensor(hass, resource, value_key="total_electricity").native_value == 42.58
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
    assert _sensor(hass, resource, value_key="total_electricity").native_value == 48.5


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
    assert status_sensor.name == "Heat generator \N{EN DASH} System pressure status"
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
        runtime_data=SimpleNamespace(coordinators=(sensor.coordinator,))
    )
    added: list[BoschBuderusSensor] = []

    await async_setup_entry(hass, entry, added.extend)

    assert len(added) == 1
    assert added[0].entity_description.resource_path == safe.path


def test_unknown_scalar_capability_remains_diagnostics_only() -> None:
    unknown = Resource(
        path="/heatSources/vendorSpecificEfficiency",
        value=98.5,
        has_value=True,
    )

    assert build_sensor_descriptions({unknown.path: unknown}) == ()


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
