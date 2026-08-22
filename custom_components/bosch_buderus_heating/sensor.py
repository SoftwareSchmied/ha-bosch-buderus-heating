"""Dynamic read-only sensors for discovered PointT resources."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import fsum, isfinite
from typing import Literal, TypeIs

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschBuderusConfigEntry
from .coordinator import BoschBuderusDataUpdateCoordinator, ResourceSnapshot
from .device import device_info_for_resource, grouped_entity_name
from .enum_translation import enum_value_to_ha
from .faults import (
    MAX_FAULT_ATTRIBUTES,
    ActiveFault,
    fault_severity_label,
    fault_summary,
    no_active_faults_label,
)
from .holidays import (
    HOLIDAY_PERIOD_PATHS,
    HOLIDAY_RESOURCE_PATHS,
    HolidayPeriod,
    HolidayState,
    parse_holiday_state,
)
from .pointt import Resource
from .pointt.models import JsonValue
from .resource_catalog import (
    CapabilityMaturity,
    PollGroup,
    capability_maturity,
    configured_device_name,
    entity_enabled_by_default,
    is_opt_in_diagnostic_resource,
    is_read_only_control_mirror,
    poll_group,
    resource_name,
    supports_entity,
)

type ValueKind = Literal[
    "value",
    "nested",
    "values",
    "enum_values",
    "heat_demand",
    "total_electricity",
    "environmental_energy",
    "system_info",
    "pressure_status",
]

_SYSTEM_PRESSURE_PATH = "/heatSources/systemPressure"
_SYSTEM_PRESSURE_RANGE_PATH = "/heatSources/systemPressureRange"
_ENERGY_BALANCE_EPSILON = 1e-9
_PRESSURE_STATUS_OPTIONS = (
    "critical_low",
    "low",
    "normal",
    "high",
    "critical_high",
)

_HEAT_DEMAND_OPTIONS = (
    "none",
    "ch",
    "dhw",
    "frost",
    "ch_dhw",
    "ch_frost",
    "dhw_frost",
    "ch_dhw_frost",
)

_KNOWN_MULTIPART_KEYS = ("total", "ch", "cooling", "dhw")
_SYSTEM_PRESSURE_RANGE_KEYS = (
    "highSystemPressure",
    "absoluteHighPressure",
    "lowSystemPressure",
    "shutOfPressureThreshold",
    "highPressureThreshold",
    "lowPressureThreshold",
)

_SYSTEM_INFO_FIELDS: dict[str, str] = {
    "ProductName": "produktname",
    "SwUpdateType": "software_update_typ",
    "SwIdenStr": "software_kennung",
    "SwIdenStr2": "software_kennung_2",
    "SwIdenStr3": "software_kennung_3",
    "SwIdenStr4": "software_kennung_4",
    "ModuleSerialNumber": "modul_seriennummer",
    "No": "nummer",
    "ModuleHwIdentStr": "modul_hardware_kennung",
    "HwVersion": "hardwareversion",
    "ProductSerialNumber": "produkt_seriennummer",
    "Ver": "version",
    "Ver2": "version_2",
    "Ver3": "version_3",
    "Ver4": "version_4",
    "Id": "id",
    "ProductTtn": "produkt_typnummer",
    "ModuleTtn": "modul_typnummer",
    "SwStatus": "softwarestatus",
    "ProductUuid": "produkt_uuid",
}

_SYSTEM_INFO_LABEL_FIELDS = (
    "ProductName",
    "ModuleHwIdentStr",
    "ProductTtn",
    "ModuleTtn",
    "Id",
)
_SYSTEM_INFO_VERSION_FIELDS = ("Ver", "HwVersion", "SwIdenStr")
_MAX_SENSOR_STATE_LENGTH = 255

_ENUM_OPTIONS: dict[str, tuple[str, ...]] = {
    "heat_demand": _HEAT_DEMAND_OPTIONS,
    "compressor_status": (
        "off",
        "heating",
        "cooling",
        "dhw",
        "pool",
        "pool_heat",
        "defrost",
        "alarm",
    ),
    "electric_auxiliary_heater_status": (
        "off",
        "heating",
        "dhw",
        "pool",
        "pool_heat",
        "defrost",
        "alarm",
    ),
    "data_processing_status": ("in_progress", "completed"),
    "season_optimizer_mode": ("off", "automatic", "forced_heat", "forced_cool"),
    "isrc_support_status": (
        "not_supported_incompatible_controller",
        "not_supported_pairing_enabled",
        "supported",
        "in_evaluation",
    ),
    "heating_circuit_switch_program_mode": ("level",),
    "heating_circuit_operation_mode": ("off", "manual", "auto"),
    "heating_circuit_overall_status": (
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
    ),
    "heating_circuit_summer_winter_mode": ("off", "forced", "cooling"),
    "heating_circuit_summer_winter_switch_mode": (
        "off",
        "automatic",
        "forced",
        "cooling",
    ),
    "heating_circuit_heat_cool_mode": ("heat", "cool", "heat_cool"),
    "heating_circuit_type": ("floor", "radiator", "convector"),
    "heating_circuit_control_type": (
        "room",
        "outdoor",
        "wdc",
        "wdcoptimized",
    ),
    "hot_water_temperature_level": ("off", "low", "high", "eco"),
    "hot_water_operation_mode": ("off", "low", "high", "ownprogram", "eco"),
    "hot_water_overall_status": (
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
    ),
    "on_off_state": ("off", "on", "stop", "start"),
    "heat_pump_type": ("air_water", "brine_water", "exhaust_air"),
    "heat_source_type": ("heatpump", "boiler", "hybrid"),
    "system_type": ("heatpump_single", "boiler_single", "hybrid"),
    "energy_management_status": ("not_connected", "connected", "active", "error"),
    "outdoor_temperature_source": ("appliance", "internet", "roomcontroller"),
    "support_status": ("not_supported", "supported", "active"),
    "update_status": ("idle", "running", "success", "failed"),
    "pressure_status": _PRESSURE_STATUS_OPTIONS,
}


@dataclass(frozen=True, slots=True)
class _PressureLimits:
    """Validated PointT pressure thresholds in bar."""

    technical_minimum: float
    shutdown_pressure: float
    normal_minimum: float
    normal_maximum: float
    upper_pressure_limit: float
    absolute_maximum: float

    def as_attributes(self) -> dict[str, str | float]:
        """Return stable numeric attributes for Home Assistant automations."""
        return {
            "technical_minimum_bar": self.technical_minimum,
            "shutdown_pressure_bar": self.shutdown_pressure,
            "normal_minimum_bar": self.normal_minimum,
            "normal_maximum_bar": self.normal_maximum,
            "upper_pressure_limit_bar": self.upper_pressure_limit,
            "absolute_maximum_bar": self.absolute_maximum,
        }


@dataclass(frozen=True, kw_only=True)
class BoschBuderusSensorEntityDescription(SensorEntityDescription):
    """Describe one scalar state extracted from a PointT resource."""

    resource_path: str
    value_kind: ValueKind = "value"
    value_key: str | None = None
    value_scale: float = 1.0
    unique_key: str


_LEGACY_KEYS: dict[tuple[str, str | None], str] = {
    ("/system/sensors/temperatures/outdoor_t1", None): "outdoor_temperature",
    ("/heatSources/actualSupplyTemperature", None): "supply_temperature",
    ("/heatSources/returnTemperature", None): "return_temperature",
    ("/dhwCircuits/dhw1/actualTemp", None): "hot_water_temperature",
    ("/heatSources/actualModulation", None): "modulation",
    ("/heatSources/systemPressure", None): "system_pressure",
    ("/heatingCircuits/hc1/overallStatus", None): "heating_status",
    ("/dhwCircuits/dhw1/overallStatus", None): "hot_water_status",
    ("/heatSources/emon/totalConsumption", "total_electricity"): "total_electricity",
    ("/heatSources/emon/totalConsumption", "outputProduced"): "total_heat_produced",
    ("/heatSources/emon/chConsumption", "total_electricity"): "heating_electricity",
    ("/heatSources/emon/chConsumption", "outputProduced"): "heating_heat_produced",
    ("/heatSources/emon/dhwConsumption", "total_electricity"): "hot_water_electricity",
    ("/heatSources/emon/dhwConsumption", "outputProduced"): "hot_water_heat_produced",
    ("/heatSources/emon/totalConsumption", "environmental_energy"): (
        "environmental_energy"
    ),
}

_UNSAFE_SUBKEY_TOKENS = (
    "ip",
    "mac",
    "serial",
    "ssid",
    "uuid",
    "recipeid",
    "updateid",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create entities from every safe resource found during discovery."""
    entities: list[SensorEntity] = []
    for coordinator in entry.runtime_data.coordinators:
        entities.extend(
            (
                BoschBuderusActiveFaultsSensor(coordinator),
                BoschBuderusActiveNotificationsSensor(coordinator),
            )
        )
        if any(path in coordinator.resources for path in HOLIDAY_PERIOD_PATHS):
            entities.append(BoschBuderusNextHolidaySensor(coordinator))
        entities.extend(
            BoschBuderusSensor(coordinator, description)
            for description in build_sensor_descriptions(coordinator.resources)
        )
    async_add_entities(entities)


class BoschBuderusNextHolidaySensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], SensorEntity
):
    """Show the start of the current or next configured holiday period."""

    _attr_has_entity_name = True
    _attr_translation_key = "next_holiday"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:next_holiday"

    @property
    def available(self) -> bool:
        return super().available and bool(self._available_resources)

    @property
    def native_value(self) -> datetime | None:
        period = self._next_period
        return period.start if period is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        period = self._next_period
        if period is None:
            return {}
        now = datetime.now(UTC)
        return {
            "end": period.end.isoformat(),
            "active": period.start <= now < period.end,
            "all_day": period.all_day,
        }

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")

    @property
    def _available_resources(self) -> dict[str, Resource]:
        snapshots = self.coordinator.data or {}
        return {
            path: snapshot.resource
            for path in HOLIDAY_RESOURCE_PATHS
            if (snapshot := snapshots.get(path)) is not None and snapshot.available
        }

    @property
    def _state(self) -> HolidayState:
        return parse_holiday_state(
            self._available_resources,
            fallback_timezone=self.coordinator.hass.config.time_zone,
        )

    @property
    def _next_period(self) -> HolidayPeriod | None:
        now = datetime.now(UTC)
        return next(
            (period for period in self._state.periods if period.end > now), None
        )


class _BoschBuderusFaultCountSensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], SensorEntity
):
    """Common behavior for bounded fault and notification counters."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.faults.has_supported_source

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")


class BoschBuderusActiveFaultsSensor(_BoschBuderusFaultCountSensor):
    """Count actionable and conservatively classified unknown faults."""

    _attr_translation_key = "active_faults"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:active_faults"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.faults.active_faults)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        faults = self.coordinator.faults.active_faults
        return _fault_list_attributes(faults, self.coordinator.hass.config.language)


class BoschBuderusActiveNotificationsSensor(_BoschBuderusFaultCountSensor):
    """Count all active PointT notifications, including maintenance and warnings."""

    _attr_translation_key = "active_notifications"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:active_notifications"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.faults.active)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        notifications = self.coordinator.faults.active
        counts = Counter(item.severity.value for item in notifications)
        return {
            **_fault_list_attributes(
                notifications, self.coordinator.hass.config.language
            ),
            "severity_counts": dict(sorted(counts.items())),
        }


def _fault_list_attributes(
    faults: tuple[ActiveFault, ...], language: str | None
) -> dict[str, object]:
    visible = faults[:MAX_FAULT_ATTRIBUTES]
    return {
        "faults": [
            {
                "code": fault.code,
                "subcode": fault.subcode,
                "severity": fault.severity.value,
                "severity_label": fault_severity_label(fault.severity, language),
                "component": fault.component_type or "unknown",
                "first_seen": fault.first_seen_at.isoformat(),
                "occurred_at": (
                    fault.occurred_at.isoformat() if fault.occurred_at else None
                ),
                "time_source": fault.time_source.value,
                "summary": fault_summary(fault, language),
            }
            for fault in visible
        ],
        "truncated": len(faults) > len(visible),
        "status": (
            fault_summary(faults[0], language)
            if faults
            else no_active_faults_label(language)
        ),
    }


def build_sensor_descriptions(
    resources: Mapping[str, Resource],
) -> tuple[BoschBuderusSensorEntityDescription, ...]:
    """Build stable scalar descriptions from the discovered resource shapes."""
    descriptions: list[BoschBuderusSensorEntityDescription] = []
    for resource in resources.values():
        if not supports_entity(resource) and not is_opt_in_diagnostic_resource(
            resource.path
        ):
            continue
        if resource.path == "/system/info":
            descriptions.append(_description(resource, None, "system_info"))
            continue
        if resource.path == "/heatSources/actualHeatDemand":
            descriptions.append(_description(resource, "values", "heat_demand"))
            continue
        if resource.path == "/system/variableTariff/supportStatus":
            descriptions.append(_description(resource, "values", "enum_values"))
            continue
        if resource.path.endswith("/name") and (
            not isinstance(resource.value, str)
            or configured_device_name(resource.value) is None
        ):
            continue
        if resource.metadata.resource_type == "emonValue" and "/emon/" in resource.path:
            descriptions.extend(_energy_descriptions(resource))
            continue

        known_keys = _known_value_keys(resource.path)
        nested_keys = _nested_scalar_keys(resource.value, include_booleans=False)
        if not nested_keys and resource.values:
            nested_keys = _values_scalar_keys(resource, include_booleans=False)
        nested_keys = tuple(dict.fromkeys((*known_keys, *nested_keys)))
        if nested_keys:
            descriptions.extend(
                _description(resource, key, "nested") for key in nested_keys
            )
        elif resource.values:
            descriptions.append(_description(resource, "values", "values"))
        elif (
            resource.has_value
            or resource.metadata.resource_type in {"floatValue", "stringValue"}
        ) and not _is_boolean_resource(resource):
            descriptions.append(_description(resource, None, "value"))
    pressure = resources.get(_SYSTEM_PRESSURE_PATH)
    pressure_range = resources.get(_SYSTEM_PRESSURE_RANGE_PATH)
    if (
        pressure is not None
        and _pressure_number(pressure.value) is not None
        and pressure_range is not None
        and _pressure_limits(pressure_range) is not None
    ):
        descriptions.append(_pressure_status_description())
    return tuple(descriptions)


def _pressure_status_description() -> BoschBuderusSensorEntityDescription:
    """Describe the derived status only available with validated thresholds."""
    return BoschBuderusSensorEntityDescription(
        key="system_pressure_status",
        name="Systemdruckstatus",
        resource_path=_SYSTEM_PRESSURE_PATH,
        value_kind="pressure_status",
        value_key="pressure_status",
        unique_key="system_pressure_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(_PRESSURE_STATUS_OPTIONS),
        translation_key="pressure_status",
    )


def _energy_descriptions(
    resource: Resource,
) -> list[BoschBuderusSensorEntityDescription]:
    keys = set(_energy_values(resource))
    if not keys and resource.path in {
        "/heatSources/emon/totalConsumption",
        "/heatSources/emon/chConsumption",
        "/heatSources/emon/dhwConsumption",
        "/heatSources/emon/coolingConsumption",
    }:
        keys.update(("compressor", "eheater", "outputProduced"))
    derived_keys = {"electricity", "environmental_energy", "total_electricity"}
    descriptions = [
        _description(resource, key, "nested") for key in sorted(keys - derived_keys)
    ]
    complete_electricity = {"compressor", "eheater"}.issubset(keys)
    if "/emon/" in resource.path and ("electricity" in keys or complete_electricity):
        descriptions.append(
            _description(resource, "total_electricity", "total_electricity")
        )
    if resource.path == "/heatSources/emon/totalConsumption" and {
        "compressor",
        "eheater",
        "outputProduced",
    }.issubset(keys):
        descriptions.append(
            _description(
                resource,
                "environmental_energy",
                "environmental_energy",
            )
        )
    return descriptions


def _description(
    resource: Resource,
    value_key: str | None,
    value_kind: ValueKind,
) -> BoschBuderusSensorEntityDescription:
    path = resource.path
    key = _LEGACY_KEYS.get((path, value_key)) or _semantic_key(path, value_key)
    unit, device_class, state_class, scale = _measurement_attributes(
        resource, value_key
    )
    enum_translation_key = _enum_translation_key(resource, value_key)
    enum_options = (
        _enum_options(resource, enum_translation_key)
        if enum_translation_key is not None
        else None
    )
    if enum_options is not None:
        device_class = SensorDeviceClass.ENUM
        state_class = None
    category = _entity_category(resource)
    display_subkey = None if value_key in {None, "values"} else value_key
    name = (
        _total_electricity_name(path)
        if value_kind == "total_electricity"
        else resource_name(path, "environmental_energy")
        if value_kind == "environmental_energy"
        else resource_name(path, display_subkey)
    )
    return BoschBuderusSensorEntityDescription(
        key=key,
        name=name,
        resource_path=path,
        value_kind=value_kind,
        value_key=value_key,
        value_scale=scale,
        unique_key=key,
        native_unit_of_measurement=unit,
        device_class=device_class,
        state_class=state_class,
        options=enum_options,
        translation_key=enum_translation_key,
        entity_category=category,
        entity_registry_enabled_default=(
            entity_enabled_by_default(path) and not is_opt_in_diagnostic_resource(path)
        ),
        suggested_display_precision=2 if unit == UnitOfEnergy.KILO_WATT_HOUR else None,
    )


class BoschBuderusSensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    SensorEntity,
):
    """Represent one scalar value from a discovered PointT resource."""

    _attr_has_entity_name = True
    entity_description: BoschBuderusSensorEntityDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: BoschBuderusSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        language = coordinator.hass.config.language
        display_subkey = (
            None if description.value_key in {None, "values"} else description.value_key
        )
        if description.value_kind == "total_electricity":
            display_name = resource_name(
                description.resource_path, "electricity", language=language
            )
        elif description.value_kind == "environmental_energy":
            display_name = resource_name(
                description.resource_path,
                "environmental_energy",
                language=language,
            )
        elif description.value_kind == "pressure_status":
            display_name = (
                "Systemdruckstatus"
                if language.casefold().startswith("de")
                else "System pressure status"
            )
        else:
            display_name = resource_name(
                description.resource_path, display_subkey, language=language
            )
        self._attr_name = grouped_entity_name(
            coordinator, description.resource_path, display_name
        )
        legacy = _LEGACY_KEYS.get((description.resource_path, description.value_key))
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:gateway:{legacy}"
            if legacy is not None
            else f"{coordinator.gateway.gateway_id}:{description.unique_key}"
        )

    @property
    def available(self) -> bool:
        snapshot = self._snapshot
        if not (super().available and snapshot is not None and snapshot.available):
            return False
        if self.entity_description.value_kind != "pressure_status":
            return True
        range_snapshot = self._pressure_range_snapshot
        return (
            range_snapshot is not None
            and range_snapshot.available
            and _pressure_limits(range_snapshot.resource) is not None
        )

    @property
    def native_value(self) -> str | int | float | None:
        snapshot = self._snapshot
        if snapshot is None:
            return None
        resource = snapshot.resource
        description = self.entity_description
        if description.value_kind == "pressure_status":
            range_snapshot = self._pressure_range_snapshot
            pressure = _pressure_number(resource.value)
            limits = (
                _pressure_limits(range_snapshot.resource)
                if range_snapshot is not None and range_snapshot.available
                else None
            )
            if pressure is None or limits is None:
                return None
            return _pressure_status(pressure, limits)
        if description.value_kind == "total_electricity":
            values = _energy_values(resource)
            direct = values.get("electricity")
            if direct is not None:
                return direct
            compressor = values.get("compressor")
            auxiliary = values.get("eheater")
            if compressor is None or auxiliary is None:
                return None
            return fsum((compressor, auxiliary))
        if description.value_kind == "environmental_energy":
            values = _energy_values(resource)
            produced = values.get("outputProduced")
            compressor = values.get("compressor")
            auxiliary = values.get("eheater")
            if produced is None or compressor is None or auxiliary is None:
                return None
            environmental = fsum((produced, -compressor, -auxiliary))
            if environmental < -_ENERGY_BALANCE_EPSILON:
                return None
            return max(0.0, environmental)
        if description.value_kind == "heat_demand":
            return _heat_demand_state(resource)
        if description.value_kind == "enum_values":
            for item in resource.values:
                if isinstance(item, str) and item:
                    return enum_value_to_ha(description.translation_key or "", item)
            return None
        if description.value_kind == "values":
            labels = [
                str(item) for item in resource.values if item is not None and item != ""
            ]
            return ", ".join(labels) if labels else None
        if description.value_kind == "system_info":
            return _system_info_summary(resource)
        if description.value_kind == "nested" and description.value_key is not None:
            value = _nested_value(resource, description.value_key)
        else:
            value = resource.value
        scalar = _native_scalar(value)
        if isinstance(scalar, str) and description.translation_key is not None:
            scalar = enum_value_to_ha(description.translation_key, scalar)
        if isinstance(scalar, str) and _is_name_resource(resource.path):
            return configured_device_name(scalar)
        if isinstance(scalar, (int, float)) and description.value_scale != 1.0:
            return scalar * description.value_scale
        return scalar

    @property
    def extra_state_attributes(self) -> dict[str, str | float] | None:
        description = self.entity_description
        if (
            description.resource_path == _SYSTEM_PRESSURE_PATH
            and description.value_kind == "value"
        ):
            range_snapshot = self._pressure_range_snapshot
            if range_snapshot is None or not range_snapshot.available:
                return None
            limits = _pressure_limits(range_snapshot.resource)
            return limits.as_attributes() if limits is not None else None
        if description.value_kind != "system_info":
            return None
        snapshot = self._snapshot
        if snapshot is None:
            return None
        attributes: dict[str, str | float] = {}
        module_number = 0
        for item in snapshot.resource.values:
            if not isinstance(item, dict):
                continue
            module_number += 1
            for source_key, attribute_key in _SYSTEM_INFO_FIELDS.items():
                value = item.get(source_key)
                if isinstance(value, str) and value.strip():
                    attributes[f"modul_{module_number}_{attribute_key}"] = value.strip()
        return attributes or None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(
            self.coordinator, self.entity_description.resource_path
        )

    @property
    def _snapshot(self) -> ResourceSnapshot | None:
        data = self.coordinator.data or {}
        return data.get(self.entity_description.resource_path)

    @property
    def _pressure_range_snapshot(self) -> ResourceSnapshot | None:
        data = self.coordinator.data or {}
        return data.get(_SYSTEM_PRESSURE_RANGE_PATH)


def _nested_scalar_keys(
    value: JsonValue,
    prefix: str = "",
    *,
    include_booleans: bool | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    keys: list[str] = []
    for key, child in value.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if not _safe_subkey(full_key):
            continue
        if isinstance(child, dict):
            keys.extend(
                _nested_scalar_keys(child, full_key, include_booleans=include_booleans)
            )
        elif not isinstance(child, (list, dict)) and (
            include_booleans is None or isinstance(child, bool) is include_booleans
        ):
            keys.append(full_key)
    return tuple(keys)


def _values_scalar_keys(
    resource: Resource, *, include_booleans: bool | None = None
) -> tuple[str, ...]:
    keys: list[str] = []
    for item in resource.values:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if (
                key not in keys
                and _safe_subkey(key)
                and not isinstance(value, (list, dict))
                and (
                    include_booleans is None
                    or isinstance(value, bool) is include_booleans
                )
            ):
                keys.append(key)
    return tuple(keys)


def _nested_value(resource: Resource, key: str) -> JsonValue:
    value: JsonValue = resource.value
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            value = None
            break
        value = value[part]
    if value is not None:
        return value
    for item in resource.values:
        if isinstance(item, dict) and key in item:
            return item[key]
    return None


def _native_scalar(value: JsonValue) -> str | int | float | None:
    if isinstance(value, (bool, list, dict)):
        return None
    if isinstance(value, float) and (
        not isfinite(value) or value in {32767.0, -32768.0}
    ):
        return None
    return value


def _pressure_number(value: JsonValue) -> float | None:
    """Normalize a finite, non-negative pressure value."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
    ):
        return None
    return float(value)


def _pressure_limits(resource: Resource) -> _PressureLimits | None:
    """Return limits only when all six PointT values form a plausible range."""
    keys = (
        "lowSystemPressure",
        "shutOfPressureThreshold",
        "lowPressureThreshold",
        "highSystemPressure",
        "highPressureThreshold",
        "absoluteHighPressure",
    )
    values = tuple(_pressure_number(_nested_value(resource, key)) for key in keys)
    if any(value is None for value in values):
        return None
    technical, shutdown, normal_minimum, normal_maximum, upper, absolute = values
    assert technical is not None
    assert shutdown is not None
    assert normal_minimum is not None
    assert normal_maximum is not None
    assert upper is not None
    assert absolute is not None
    if not (
        technical <= shutdown <= normal_minimum < normal_maximum <= upper <= absolute
    ):
        return None
    return _PressureLimits(
        technical,
        shutdown,
        normal_minimum,
        normal_maximum,
        upper,
        absolute,
    )


def _pressure_status(pressure: float, limits: _PressureLimits) -> str:
    """Classify current pressure while giving safety limits precedence."""
    if pressure <= limits.shutdown_pressure:
        return "critical_low"
    if pressure < limits.normal_minimum:
        return "low"
    if pressure < limits.normal_maximum:
        return "normal"
    if pressure < limits.upper_pressure_limit:
        return "high"
    return "critical_high"


def _system_info_summary(resource: Resource) -> str:
    """Build a bounded human-readable module summary for the sensor state."""
    modules: list[str] = []
    for number, item in enumerate(
        (value for value in resource.values if isinstance(value, dict)), start=1
    ):
        label = _first_nonempty_string(item, _SYSTEM_INFO_LABEL_FIELDS)
        version = _first_nonempty_string(item, _SYSTEM_INFO_VERSION_FIELDS)
        module = label or f"Modul {number}"
        if version and version != label:
            module = f"{module} · Version {version}"
        modules.append(module)
    if not modules:
        return "Keine Module erkannt"

    for included in range(len(modules), 0, -1):
        omitted = len(modules) - included
        suffix = f"; … (+{omitted} weitere)" if omitted else ""
        summary = "; ".join(modules[:included]) + suffix
        if len(summary) <= _MAX_SENSOR_STATE_LENGTH:
            return summary
    return f"{modules[0][: _MAX_SENSOR_STATE_LENGTH - 1]}…"


def _first_nonempty_string(
    item: Mapping[str, JsonValue], fields: tuple[str, ...]
) -> str | None:
    for field in fields:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_boolean_resource(resource: Resource) -> bool:
    return (
        isinstance(resource.value, bool)
        or resource.metadata.resource_type == "booleanValue"
    )


def _energy_values(resource: Resource) -> dict[str, float]:
    values: dict[str, float] = {}
    candidates: tuple[JsonValue, ...] = resource.values
    if isinstance(resource.value, dict):
        candidates = (resource.value, *candidates)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        typed_key = item.get("type")
        typed_value = item.get("value")
        if isinstance(typed_key, str) and _valid_energy_number(typed_value):
            values[typed_key] = float(typed_value)
            continue
        for key, raw_value in item.items():
            if _valid_energy_number(raw_value):
                values[key] = float(raw_value)
    return values


def _valid_energy_number(value: JsonValue) -> TypeIs[int | float]:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and isfinite(value)
        and value >= 0
    )


def _measurement_attributes(
    resource: Resource, value_key: str | None
) -> tuple[str | None, SensorDeviceClass | None, SensorStateClass | None, float]:
    path = resource.path
    unit = resource.metadata.unit
    if resource.metadata.resource_type == "emonValue" and "/emon/" in path:
        return (
            UnitOfEnergy.KILO_WATT_HOUR,
            SensorDeviceClass.ENERGY,
            SensorStateClass.TOTAL_INCREASING,
            1.0,
        )
    if path.endswith("/workingTime"):
        return (
            UnitOfTime.HOURS,
            SensorDeviceClass.DURATION,
            SensorStateClass.TOTAL_INCREASING,
            1 / 3600,
        )
    if path.endswith("/numberOfStarts"):
        return None, None, SensorStateClass.TOTAL_INCREASING, 1.0
    if unit == "C":
        return (
            UnitOfTemperature.CELSIUS,
            SensorDeviceClass.TEMPERATURE,
            SensorStateClass.MEASUREMENT,
            1.0,
        )
    if unit == "bar":
        return (
            UnitOfPressure.BAR,
            SensorDeviceClass.PRESSURE,
            SensorStateClass.MEASUREMENT,
            1.0,
        )
    if unit == "mins":
        return (
            UnitOfTime.MINUTES,
            SensorDeviceClass.DURATION,
            SensorStateClass.MEASUREMENT,
            1.0,
        )
    if unit == "%" or value_key in {"percent", "cur_percent"}:
        return PERCENTAGE, None, SensorStateClass.MEASUREMENT, 1.0
    if resource.metadata.resource_type == "floatValue":
        return None, None, SensorStateClass.MEASUREMENT, 1.0
    return None, None, None, 1.0


def _enum_translation_key(resource: Resource, value_key: str | None) -> str | None:
    """Return the state-translation group for a known textual PointT value."""
    if resource.path == "/gateway/update/status" and value_key == "status.value":
        return "update_status"
    if (
        resource.path == "/system/variableTariff/supportStatus"
        and value_key == "values"
    ):
        return "support_status"
    if resource.path == "/heatSources/actualHeatDemand" and value_key == "values":
        return "heat_demand"
    if value_key is not None or not isinstance(resource.value, str):
        return None
    path = resource.path
    tail = path.rsplit("/", 1)[-1]
    if path.startswith("/heatingCircuits/"):
        return {
            "switchProgramMode": "heating_circuit_switch_program_mode",
            "operationMode": "heating_circuit_operation_mode",
            "overallStatus": "heating_circuit_overall_status",
            "currentSuWiMode": "heating_circuit_summer_winter_mode",
            "suWiSwitchMode": "heating_circuit_summer_winter_switch_mode",
            "heatCoolMode": "heating_circuit_heat_cool_mode",
            "heatingType": "heating_circuit_type",
            "controlType": "heating_circuit_control_type",
        }.get(tail)
    if path.startswith("/dhwCircuits/"):
        return {
            "currentTemperatureLevel": "hot_water_temperature_level",
            "operationMode": "hot_water_operation_mode",
            "overallStatus": "hot_water_overall_status",
            "charge": "on_off_state",
            "reduceTempOnAlarm": "on_off_state",
            "tdMode": "on_off_state",
        }.get(tail)
    if tail == "heatPumpType":
        return "heat_pump_type"
    if path.startswith("/heatSources/") and tail == "type":
        return "heat_source_type"
    return {
        "/gateway/dataProcessing/status": "data_processing_status",
        "/system/type": "system_type",
        "/system/awayMode/enabled": "on_off_state",
        "/system/globalSeasonOptimizer/currentMode": "season_optimizer_mode",
        "/system/iSRC/supportStatus": "isrc_support_status",
        "/heatSources/chStatus": "on_off_state",
        "/heatSources/compressor/status": "compressor_status",
        "/heatSources/Source/eHeater/status": "electric_auxiliary_heater_status",
        "/heatSources/emStatus": "energy_management_status",
        "/heatSources/flameStatus": "on_off_state",
        "/system/sensors/temperatures/outdoorTemperatureSource": (
            "outdoor_temperature_source"
        ),
        "/system/variableTariff/supportStatus": "support_status",
    }.get(path)


def _known_value_keys(path: str) -> tuple[str, ...]:
    """Return stable subkeys for resources whose startup payload may be partial."""
    if path == _SYSTEM_PRESSURE_RANGE_PATH:
        return _SYSTEM_PRESSURE_RANGE_KEYS
    if re.fullmatch(r"/heatSources/[^/]+/(?:numberOfStarts|workingTime)", path):
        return _KNOWN_MULTIPART_KEYS
    return ()


def _heat_demand_state(resource: Resource) -> str:
    """Combine the active PointT demand list into one stable enum state."""
    aliases = {
        "central_heating": "ch",
        "heating": "ch",
        "domestic_hot_water": "dhw",
        "hot_water": "dhw",
        "frost_protection": "frost",
    }
    active: set[str] = set()
    for item in resource.values:
        if not isinstance(item, str):
            continue
        normalized = re.sub(r"[^a-z0-9]+", "_", item.casefold()).strip("_")
        active.add(aliases.get(normalized, normalized))
    ordered = tuple(item for item in ("ch", "dhw", "frost") if item in active)
    return "_".join(ordered) if ordered else "none"


def _enum_options(resource: Resource, translation_key: str) -> list[str]:
    """Combine known, advertised, and current values into valid enum options."""
    options = list(_ENUM_OPTIONS[translation_key])
    for candidate in (
        *resource.metadata.allowed_values,
        *resource.values,
        resource.value,
    ):
        if isinstance(candidate, str):
            normalized = enum_value_to_ha(translation_key, candidate)
            if normalized not in options:
                options.append(normalized)
    return options


def _semantic_key(path: str, subkey: str | None) -> str:
    normalized = path.strip("/").replace("/", ":")
    return f"{normalized}:{subkey}" if subkey else normalized


_PRIMARY_PATHS = frozenset(
    {
        "/system/awayMode/enabled",
        "/system/sensors/temperatures/outdoor_t1",
    }
)

_DIAGNOSTIC_TAILS = frozenset(
    {"numberOfStarts", "supplyFlowCondenserTemp", "workingTime"}
)


def _entity_category(resource: Resource) -> EntityCategory | None:
    """Classify operating values, settings, and service diagnostics."""
    path = resource.path
    tail = path.rsplit("/", 1)[-1]
    if "/emon/" in path:
        return None
    if is_read_only_control_mirror(path):
        return EntityCategory.DIAGNOSTIC
    if capability_maturity(path) is CapabilityMaturity.UNDERSTOOD:
        return EntityCategory.DIAGNOSTIC
    if path in _PRIMARY_PATHS:
        return None
    if path == "/heatSources/systemPressureRange" or tail in _DIAGNOSTIC_TAILS:
        return EntityCategory.DIAGNOSTIC
    if poll_group(resource) is PollGroup.STATIC or path.startswith(
        ("/gateway/", "/system/")
    ):
        return EntityCategory.DIAGNOSTIC
    return None


def _is_name_resource(path: str) -> bool:
    """Return whether a PointT string contains a user-facing configured name."""
    tail = path.rsplit("/", 1)[-1]
    return tail == "name" or bool(re.fullmatch(r"name[A-Z0-9]+", tail))


def _safe_subkey(key: str) -> bool:
    normalized = key.lower().replace("_", "")
    return not any(token in normalized for token in _UNSAFE_SUBKEY_TOKENS)


def _total_electricity_name(path: str) -> str:
    return resource_name(path, "electricity")
