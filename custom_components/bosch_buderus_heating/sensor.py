"""Dynamic read-only sensors for discovered PointT resources."""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import fsum, isfinite
from typing import Literal, cast

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
    UnitOfPower,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from . import BoschBuderusConfigEntry, rrc
from .bacon import purifier
from .bacon.dry_entities import track_entities as track_dry_entities
from .bacon.purifier_entities import (
    BaconPurifierSensor,
)
from .bacon.purifier_entities import (
    track_entities as track_purifier_entities,
)
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness, ResourceSnapshot
from .derived import (
    MAGNUS_A,
    MAGNUS_B_CELSIUS,
    DewPointCalculation,
    calculate_dew_point,
)
from .device import device_info_for_resource, grouped_entity_name
from .dhw_safety import is_dhw_safety_path
from .energy_contracts import assess_energy, is_energy_collection
from .enum_translation import enum_value_to_ha
from .faults import (
    MAX_FAULT_ATTRIBUTES,
    ActiveFault,
    fault_severity_label,
    fault_summary,
    no_active_faults_label,
)
from .holiday_writes import holiday_resources_from_snapshots
from .holidays import (
    HolidayPeriod,
    HolidayState,
    is_holiday_period_path,
    is_holiday_resource_path,
    parse_holiday_state,
)
from .pointt import Resource
from .pointt.metrics import RequestMetrics
from .pointt.models import JsonValue
from .pointt.redaction import resource_path_template
from .read_contracts import assess_runtime, is_monitor_collection, is_runtime_path
from .resource_catalog import (
    CapabilityMaturity,
    PollGroup,
    additional_app_value,
    capability_maturity,
    configured_device_name,
    entity_enabled_by_default,
    is_additional_app_scalar,
    is_opt_in_diagnostic_resource,
    is_read_only_control_mirror,
    poll_group,
    resource_name,
    supports_entity,
)
from .start_counters import (
    assess_start_counter,
    is_start_counter_path,
    start_counter_placeholder,
)
from .startup_data import startup_placeholder
from .switch_programs import (
    DAYS,
    assess_switch_program,
    is_dhw_switch_program_path,
    is_switch_program_path,
)
from .unit_contracts import DURATION_UNITS, ENERGY_UNITS, TEMPERATURE_UNITS
from .wallbox_sensor import DESCRIPTIONS as WALLBOX_DESCRIPTIONS
from .wallbox_sensor import WallboxSensor
from .water_softener import PHASES, softener_capability, softener_unit, softener_value

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
    "dew_point",
    "switch_program",
]

_LOGGER = logging.getLogger(__name__)
_MAX_TRACKED_UNKNOWN_ENUM_VALUES = 16

_REQUEST_METRIC_DESCRIPTIONS = (
    SensorEntityDescription(
        key="pointt_api_requests_total",
        translation_key="pointt_api_requests_total",
        icon="mdi:counter",
        native_unit_of_measurement="requests",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="pointt_api_requests_last_hour",
        translation_key="pointt_api_requests_last_hour",
        icon="mdi:cloud-sync-outline",
        native_unit_of_measurement="requests",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="pointt_api_response_time_last_hour",
        translation_key="pointt_api_response_time_last_hour",
        icon="mdi:timer-outline",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)

_SYSTEM_PRESSURE_PATH = "/heatSources/systemPressure"
_SYSTEM_PRESSURE_RANGE_PATH = "/heatSources/systemPressureRange"
_HEATING_CIRCUIT_ROOM_TEMPERATURE = re.compile(
    r"^/heatingCircuits/(?P<circuit>[^/]+)/roomtemperature$"
)
_ENERGY_BALANCE_EPSILON = 1e-9
_PRESSURE_STATUS_OPTIONS = (
    "critical_low",
    "low",
    "normal",
    "high",
    "critical_high",
)

# PointT uses percent for several unrelated measurements. Match only resources
# whose semantics are known, so modulation, signal quality, and power reduction
# do not accidentally receive a misleading Home Assistant device class.
_PERCENTAGE_DEVICE_CLASS_PATTERNS = (
    (
        re.compile(
            r"^(?:/heatingCircuits/[^/]+/actualHumidity|/zones/zone[^/]+/averageActualHumidity|/devices/(?!list$)[^/]+/actualHumidity|/ventilation/zone[^/]+/maxRelativeHumidity)$"
        ),
        SensorDeviceClass.HUMIDITY,
    ),
    (
        re.compile(r"^/devices/(?!list$)[^/]+/battery$"),
        SensorDeviceClass.BATTERY,
    ),
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
    "rrc_user_mode": ("manual", "clock"),
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
    "auxiliary_heater_operation_mode": ("off", "manual", "auto"),
    "pv_contact_state": ("inactive", "active"),
    "data_processing_status": ("in_progress", "completed"),
    "season_optimizer_mode": (
        "off",
        "heating",
        "cooling",
        "automatic",
        "forced_heat",
        "forced_cool",
    ),
    "isrc_support_status": (
        "not_supported_incompatible_controller",
        "not_supported_pairing_enabled",
        "supported",
        "in_evaluation",
    ),
    "heating_circuit_switch_program_mode": ("level", "levels", "absolute"),
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
        "wdcsimplified",
        "mpc",
        "room_flow_temperature",
        "room_power",
        "constant",
        "valve_feedback_control",
        "isrc",
        "vbh",
        "external",
    ),
    "heating_circuit_cooling_control_type": (
        "constant",
        "rtcc_with_dewpoint",
        "rtcc_no_dewpoint",
    ),
    "hot_water_temperature_level": ("off", "low", "high", "eco"),
    "hot_water_operation_mode": (
        "off",
        "low",
        "high",
        "ownprogram",
        "eco",
        "follow_heating_program",
    ),
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
    "heat_source_type": (
        "no_appliance",
        "heatpump",
        "boiler",
        "boiler_oil",
        "boiler_gas",
        "boiler_unknown",
        "hybrid",
    ),
    "system_type": (
        "heatpump_single",
        "heatpump_cascade",
        "boiler_single",
        "hybrid",
    ),
    "energy_management_status": (
        "not_connected",
        "inactive",
        "active_ch",
        "active_dhw",
        "active_em",
        "connected",
        "active",
        "error",
    ),
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
    secondary_resource_path: str | None = None


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
    "inclusiontime",
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
    if (account := entry.runtime_data.bacon) is not None:
        track_dry_entities(entry, account, async_add_entities, "sensor")
        track_purifier_entities(
            entry,
            account,
            async_add_entities,
            BaconPurifierSensor,
            purifier.SENSORS,
            sensor=True,
        )
    entities: list[SensorEntity] = []
    entities.extend(
        WallboxSensor(coordinator, description)
        for coordinator in getattr(entry.runtime_data, "wallboxes", ())
        for description in WALLBOX_DESCRIPTIONS
    )
    if entry.runtime_data.coordinators:
        entities.extend(
            BoschBuderusRequestMetricSensor(
                entry.entry_id,
                entry.runtime_data.client.metrics,
                entry.runtime_data.coordinators,
                description,
            )
            for description in _REQUEST_METRIC_DESCRIPTIONS
        )
    for coordinator in entry.runtime_data.coordinators:
        entities.extend(
            (
                BoschBuderusActiveFaultsSensor(coordinator),
                BoschBuderusActiveNotificationsSensor(coordinator),
            )
        )
        if any(is_holiday_period_path(path) for path in coordinator.resources):
            entities.append(BoschBuderusNextHolidaySensor(coordinator))
        _track_resource_sensors(entry, coordinator, async_add_entities)
    async_add_entities(entities)


def _track_resource_sensors(
    entry: BoschBuderusConfigEntry,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Keep registered metadata sensors and add recovered values exactly once."""
    resources = dict(coordinator.resources)
    for path in coordinator.startup_data.registered_paths():
        if path not in resources:
            resources[path] = startup_placeholder(path)
    descriptions_by_key = {
        item.unique_key: item for item in build_sensor_descriptions(resources)
    }
    for path in coordinator.registered_start_counter_paths:
        # Keep both registered identities after a schema change. The scalar
        # and multipart total must never share a statistic or replace each other.
        for scalar in coordinator.registered_start_counter_shapes[path]:
            placeholder = start_counter_placeholder(path, scalar=scalar)
            for item in build_sensor_descriptions({path: placeholder}):
                descriptions_by_key.setdefault(item.unique_key, item)
    descriptions = tuple(descriptions_by_key.values())
    seen = {description.unique_key for description in descriptions}
    async_add_entities([BoschBuderusSensor(coordinator, item) for item in descriptions])

    @callback
    def recover() -> None:
        recovered = {
            path: snapshot.resource
            for path, snapshot in (coordinator.data or {}).items()
            if snapshot.available
        }
        entities = []
        for description in build_sensor_descriptions(recovered):
            if description.unique_key not in seen:
                seen.add(description.unique_key)
                entities.append(BoschBuderusSensor(coordinator, description))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(recover))


class BoschBuderusRequestMetricSensor(SensorEntity):
    """Expose account-wide PointT request metrics without causing API calls."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry_id: str,
        metrics: RequestMetrics,
        coordinators: tuple[BoschBuderusDataUpdateCoordinator, ...],
        description: SensorEntityDescription,
    ) -> None:
        self.entity_description = description
        self._metrics = metrics
        self._coordinators = coordinators
        self._attr_unique_id = f"{entry_id}:request_metrics:{description.key}"

    async def async_added_to_hass(self) -> None:
        """Update locally whenever any gateway coordinator completes a cycle."""
        await super().async_added_to_hass()
        for coordinator in self._coordinators:
            self.async_on_remove(
                coordinator.async_add_listener(self.async_write_ha_state)
            )

    @property
    def native_value(self) -> int | float | None:
        snapshot = self._metrics.snapshot()
        if self.entity_description.key == "pointt_api_requests_total":
            return cast(int, snapshot["requests_total"])
        rolling = cast(dict[str, object], snapshot["rolling_60_minutes"])
        if self.entity_description.key == "pointt_api_requests_last_hour":
            return cast(int, rolling["requests_total"])
        return cast(float | None, rolling["average_successful_response_time_ms"])

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self._metrics.snapshot()
        if self.entity_description.key == "pointt_api_requests_total":
            total_keys = (
                "observation_seconds",
                "requests_successful",
                "requests_failed",
                "success_rate_percent",
                "retry_attempts",
                "fallback_requests",
                "rate_limit_events",
                "requests_by_category",
                "requests_by_method",
                "responses_by_status_class",
                "outcomes",
            )
            return {key: snapshot[key] for key in total_keys}
        rolling = cast(dict[str, object], snapshot["rolling_60_minutes"])
        if self.entity_description.key == "pointt_api_requests_last_hour":
            request_keys = (
                "requests_successful",
                "requests_failed",
                "success_rate_percent",
                "retry_attempts",
                "fallback_requests",
                "rate_limit_events",
                "requests_by_type",
                "responses_by_http_status",
                "outcomes",
                "bulk_items_total",
                "bulk_items_successful",
                "bulk_items_failed",
                "bulk_items_parse_failed",
            )
            return {key: rolling[key] for key in request_keys}
        latency_keys = (
            "successful_response_time_samples",
            "p95_successful_response_time_ms",
            "maximum_successful_response_time_ms",
            "latest_response_time_ms",
        )
        return {key: rolling[key] for key in latency_keys}


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
        return super().available and any(
            is_holiday_resource_path(path) for path in self._available_resources
        )

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
        return holiday_resources_from_snapshots(self.coordinator.data or {})

    @property
    def _state(self) -> HolidayState:
        return parse_holiday_state(
            self._available_resources,
            fallback_timezone=dt_util.get_default_time_zone(),
            timezones=self.coordinator.holiday_timezones,
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
        return super().available and self.coordinator.faults.has_known_state

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
        if is_start_counter_path(resource.path):
            descriptions.extend(
                _description(resource, key, "value" if key is None else "nested")
                for key, _ in assess_start_counter(resource).readings
            )
            continue
        if is_switch_program_path(resource.path):
            descriptions.append(_switch_program_description(resource))
            continue
        if is_runtime_path(resource.path):
            descriptions.extend(
                _description(resource, key, "value" if key is None else "nested")
                for key, _ in assess_runtime(resource).readings
            )
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
    descriptions.extend(_dew_point_descriptions(resources))
    return tuple(descriptions)


def _pressure_status_description() -> BoschBuderusSensorEntityDescription:
    """Describe the derived status only available with validated thresholds."""
    return BoschBuderusSensorEntityDescription(
        key="system_pressure_status",
        name="System pressure status (calculated)",
        resource_path=_SYSTEM_PRESSURE_PATH,
        value_kind="pressure_status",
        value_key="pressure_status",
        unique_key="system_pressure_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(_PRESSURE_STATUS_OPTIONS),
        translation_key="pressure_status",
    )


def _dew_point_descriptions(
    resources: Mapping[str, Resource],
) -> tuple[BoschBuderusSensorEntityDescription, ...]:
    """Describe one calculated dew-point sensor per complete circuit pair."""
    descriptions: list[BoschBuderusSensorEntityDescription] = []
    for temperature_path in sorted(resources):
        match = _HEATING_CIRCUIT_ROOM_TEMPERATURE.fullmatch(temperature_path)
        if match is None:
            continue
        humidity_path = f"/heatingCircuits/{match.group('circuit')}/actualHumidity"
        temperature = resources[temperature_path]
        humidity = resources.get(humidity_path)
        if (
            humidity is None
            or not supports_entity(temperature)
            or not supports_entity(humidity)
        ):
            continue
        key = f"heatingCircuits:{match.group('circuit')}:calculated_dew_point"
        descriptions.append(
            BoschBuderusSensorEntityDescription(
                key=key,
                name="Dew point (calculated)",
                resource_path=temperature_path,
                secondary_resource_path=humidity_path,
                value_kind="dew_point",
                value_key="calculated_dew_point",
                unique_key=key,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                device_class=SensorDeviceClass.TEMPERATURE,
                state_class=SensorStateClass.MEASUREMENT,
                translation_key="calculated_dew_point",
                suggested_display_precision=1,
            )
        )
    return tuple(descriptions)


def _energy_descriptions(
    resource: Resource,
) -> list[BoschBuderusSensorEntityDescription]:
    keys = set(_energy_values(resource))
    if not keys and (
        resource.path
        in {
            "/heatSources/emon/totalConsumption",
            "/heatSources/emon/chConsumption",
            "/heatSources/emon/dhwConsumption",
            "/heatSources/emon/coolingConsumption",
            "/heatSources/emon/poolConsumption",
        }
        or re.fullmatch(r"/heatSources/hs[^/]+/emon/totalConsumption", resource.path)
    ):
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


_SWITCH_PROGRAM_LEVELS = {
    "heating_switch_program": ("comfort1", "comfort2", "comfort3", "eco", "off"),
    "dhw_switch_program": ("low", "eco", "high", "off"),
}


def _switch_program_description(
    resource: Resource,
) -> BoschBuderusSensorEntityDescription:
    """Describe one read-only program: the level it currently selects."""
    translation_key = (
        "dhw_switch_program"
        if is_dhw_switch_program_path(resource.path)
        else "heating_switch_program"
    )
    options = list(
        dict.fromkeys(
            (
                *_SWITCH_PROGRAM_LEVELS[translation_key],
                *assess_switch_program(resource).levels,
            )
        )
    )
    return replace(
        _description(resource, None, "switch_program"),
        native_unit_of_measurement=None,
        device_class=SensorDeviceClass.ENUM,
        state_class=None,
        options=options,
        translation_key=translation_key,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:calendar-clock",
    )


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
    if value_kind == "environmental_energy":
        # This net total may fall when its input counters update at different times.
        # A decrease is not evidence of a new meter cycle.
        state_class = SensorStateClass.TOTAL
    enum_translation_key = _enum_translation_key(resource, value_key)
    enum_options = (
        _enum_options(resource, enum_translation_key)
        if enum_translation_key is not None
        else None
    )
    if enum_translation_key == "softener_regeneration_phase":
        enum_options = list(PHASES)
    if enum_options is not None:
        device_class = SensorDeviceClass.ENUM
        state_class = None
    category = _entity_category(resource)
    if rrc.scalar_field(path):
        category = None
        if rrc.scalar_field(path) in rrc.TEMPERATURES:
            unit, device_class, state_class, scale = (
                UnitOfTemperature.CELSIUS,
                SensorDeviceClass.TEMPERATURE,
                SensorStateClass.MEASUREMENT,
                1.0,
            )
    if is_additional_app_scalar(path) and not path.endswith("/dhwType"):
        category = None
    if softener_capability(path) is not None:
        category = None
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
        self._unknown_enum_values: set[str] = set()
        self._unknown_enum_warning_logged = False
        self._switch_timer: CALLBACK_TYPE | None = None
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
        elif description.value_kind == "dew_point":
            display_name = (
                "Taupunkt (berechnet)"
                if language.casefold().startswith("de")
                else "Dew point (calculated)"
            )
        elif description.value_kind == "pressure_status":
            display_name = (
                "Systemdruckstatus (berechnet)"
                if language.casefold().startswith("de")
                else "System pressure status (calculated)"
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
        if rrc.scalar_field(snapshot.resource.path):
            return (
                snapshot.freshness is Freshness.FRESH
                and rrc.scalar_value(snapshot.resource) is not None
            )
        if (
            softener_capability(snapshot.resource.path) is not None
            or is_additional_app_scalar(snapshot.resource.path)
            or is_runtime_path(snapshot.resource.path)
        ):
            return snapshot.freshness is Freshness.FRESH
        if self.entity_description.value_kind == "dew_point":
            secondary = self._secondary_snapshot
            return secondary is not None and secondary.available
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
        if description.value_kind == "switch_program":
            if not snapshot.available:
                return None
            now = dt_util.now()
            level = assess_switch_program(resource).active_level(
                now.weekday(), now.hour * 60 + now.minute
            )
            return None if level is None else self._validated_enum_state(level)
        if rrc.scalar_field(resource.path):
            return rrc.scalar_value(resource) if self.available else None
        if is_monitor_collection(resource.path):
            return None
        if is_runtime_path(resource.path):
            if not snapshot.available or snapshot.freshness is not Freshness.FRESH:
                return None
            return assess_runtime(resource).reading(description.value_key).value
        if is_additional_app_scalar(resource.path):
            additional_value = (
                additional_app_value(resource)
                if snapshot.available and snapshot.freshness is Freshness.FRESH
                else None
            )
            if isinstance(additional_value, str) and description.translation_key:
                return self._validated_enum_state(
                    enum_value_to_ha(description.translation_key, additional_value)
                )
            return additional_value
        if softener_capability(resource.path) is not None:
            softener_state = (
                softener_value(resource)
                if snapshot.available and snapshot.freshness is Freshness.FRESH
                else None
            )
            return None if isinstance(softener_state, bool) else softener_state
        if is_start_counter_path(resource.path):
            if not snapshot.available:
                return None
            return assess_start_counter(resource).reading(description.value_key).value
        if description.value_kind == "dew_point":
            calculation = self._dew_point_calculation
            return calculation.dew_point_celsius if calculation is not None else None
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
            return self._validated_enum_state(_heat_demand_state(resource))
        if description.value_kind == "enum_values":
            for item in resource.values:
                if isinstance(item, str) and item:
                    return self._validated_enum_state(
                        enum_value_to_ha(description.translation_key or "", item)
                    )
            return None
        if description.value_kind == "values":
            labels = [
                str(item) for item in resource.values if item is not None and item != ""
            ]
            return ", ".join(labels) if labels else None
        if description.value_kind == "system_info":
            return _system_info_summary(resource)
        if description.value_kind == "nested" and description.value_key is not None:
            value = (
                _energy_values(resource).get(description.value_key)
                if "/emon/" in resource.path
                else _nested_value(resource, description.value_key)
            )
        else:
            value = resource.value
        scalar = _native_scalar(value)
        if isinstance(scalar, str) and description.translation_key is not None:
            scalar = enum_value_to_ha(description.translation_key, scalar)
            return self._validated_enum_state(scalar)
        if isinstance(scalar, str) and _is_name_resource(resource.path):
            return configured_device_name(scalar)
        if isinstance(scalar, (int, float)):
            unit, device_class, _, scale = _measurement_attributes(
                resource, description.value_key
            )
            if description.native_unit_of_measurement is not None and (
                unit is None or device_class != description.device_class
            ):
                return None
            return scalar * scale if scale != 1.0 else scalar
        return scalar

    def _validated_enum_state(self, value: str) -> str | None:
        """Return only enum states declared when this entity was created."""
        options = self.entity_description.options
        if options is None or value in options:
            return value

        if (
            value not in self._unknown_enum_values
            and len(self._unknown_enum_values) < _MAX_TRACKED_UNKNOWN_ENUM_VALUES
        ):
            self._unknown_enum_values.add(value)
            self.coordinator.record_unknown_enum_value(
                self.entity_description.resource_path
            )
        if not self._unknown_enum_warning_logged:
            self._unknown_enum_warning_logged = True
            _LOGGER.warning(
                "PointT resource %s returned an undeclared enum value; "
                "reporting the entity as unknown. The value is omitted from logs "
                "and counted in diagnostics",
                resource_path_template(self.entity_description.resource_path),
            )
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        snapshot = self._snapshot
        if snapshot is not None and self.entity_description.value_kind in {
            "value",
            "nested",
        }:
            return _measurement_attributes(
                snapshot.resource, self.entity_description.value_key
            )[0]
        return self.entity_description.native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> Mapping[str, object] | None:
        description = self.entity_description
        if description.value_kind == "switch_program":
            return self._switch_program_attributes()
        if description.value_kind == "dew_point":
            calculation = self._dew_point_calculation
            if calculation is None:
                return None
            return {
                "source_room_temperature_c": calculation.room_temperature_celsius,
                "source_relative_humidity_percent": (
                    calculation.relative_humidity_percent
                ),
                "calculation_method": "magnus",
                "magnus_a": MAGNUS_A,
                "magnus_b_c": MAGNUS_B_CELSIUS,
            }
        if description.value_kind == "total_electricity":
            snapshot = self._snapshot
            if snapshot is None:
                return None
            values = _energy_values(snapshot.resource)
            if values.get("electricity") is not None:
                return {"value_source": "direct"}
            if (
                values.get("compressor") is not None
                and values.get("eheater") is not None
            ):
                return {
                    "value_source": "calculated",
                    "calculation": "compressor + auxiliary_heater",
                }
            return None
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
        attributes: dict[str, object] = {}
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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.entity_description.value_kind == "switch_program":
            self.async_on_remove(self._cancel_switch_timer)
            self._schedule_next_switch()

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.entity_description.value_kind == "switch_program":
            self._schedule_next_switch()
        super()._handle_coordinator_update()

    @callback
    def _schedule_next_switch(self) -> None:
        """Refresh the state at the next switch point, without a cloud request."""
        self._cancel_switch_timer()
        snapshot = self._snapshot
        if snapshot is None or not snapshot.available:
            return
        now = dt_util.now()
        minutes = assess_switch_program(snapshot.resource).next_switch_in_minutes(
            now.weekday(), now.hour * 60 + now.minute
        )
        if minutes is None:
            return
        at = now.replace(second=0, microsecond=0) + timedelta(minutes=minutes)
        self._switch_timer = async_track_point_in_time(
            self.hass, self._handle_switch_point, at
        )

    @callback
    def _handle_switch_point(self, _now: datetime) -> None:
        self._switch_timer = None
        self.async_write_ha_state()
        self._schedule_next_switch()

    @callback
    def _cancel_switch_timer(self) -> None:
        if self._switch_timer is not None:
            self._switch_timer()
            self._switch_timer = None

    def _switch_program_attributes(self) -> dict[str, object] | None:
        """Expose the weekly switch points and reported limits, never a URI."""
        snapshot = self._snapshot
        if snapshot is None or not snapshot.available:
            return None
        assessment = assess_switch_program(snapshot.resource)
        if not assessment.supported:
            return None
        schedule = assessment.schedule()
        return {
            "switch_points": {day: schedule[day] for day in DAYS if day in schedule},
            "max_switch_points": assessment.max_switch_points,
            "max_switch_points_per_day": assessment.max_switch_points_per_day,
            "time_raster_minutes": assessment.time_raster,
        }

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
    def _secondary_snapshot(self) -> ResourceSnapshot | None:
        path = self.entity_description.secondary_resource_path
        if path is None:
            return None
        return (self.coordinator.data or {}).get(path)

    @property
    def _dew_point_calculation(self) -> DewPointCalculation | None:
        primary = self._snapshot
        secondary = self._secondary_snapshot
        if (
            primary is None
            or not primary.available
            or secondary is None
            or not secondary.available
        ):
            return None
        temperature_unit = TEMPERATURE_UNITS.get(primary.resource.metadata.unit or "")
        temperature = _native_scalar(primary.resource.value)
        if (
            temperature_unit is None
            or secondary.resource.metadata.unit != "%"
            or not isinstance(temperature, int | float)
        ):
            return None
        return calculate_dew_point(
            TemperatureConverter.convert(
                temperature, temperature_unit, UnitOfTemperature.CELSIUS
            ),
            secondary.resource.value,
        )

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
    if is_dhw_safety_path(resource.path):
        return True
    if (softener := softener_capability(resource.path)) is not None:
        return softener.kind == "binary"
    return (
        isinstance(resource.value, bool)
        or resource.metadata.resource_type == "booleanValue"
    )


def _energy_values(resource: Resource) -> dict[str, float]:
    return {
        key: reading.value
        for key, reading in assess_energy(resource).readings
        if reading.value is not None
    }


def _measurement_attributes(
    resource: Resource, value_key: str | None
) -> tuple[str | None, SensorDeviceClass | None, SensorStateClass | None, float]:
    path = resource.path
    unit = resource.metadata.unit
    if (softener := softener_capability(path)) is not None:
        unit = softener_unit(resource)
        if softener.suffix == "sensor/availableWater":
            return (
                UnitOfVolume.CUBIC_METERS
                if unit in {"m3", "m³"}
                else UnitOfVolume.LITERS,
                SensorDeviceClass.VOLUME_STORAGE,
                SensorStateClass.MEASUREMENT,
                1.0,
            )
    if is_energy_collection(resource):
        return (
            UnitOfEnergy.KILO_WATT_HOUR,
            SensorDeviceClass.ENERGY,
            SensorStateClass.TOTAL_INCREASING,
            1.0,
        )
    if is_runtime_path(path):
        return (
            UnitOfTime.HOURS,
            SensorDeviceClass.DURATION,
            SensorStateClass.TOTAL_INCREASING,
            assess_runtime(resource).scale or 1.0,
        )
    if path.endswith("/numberOfStarts"):
        return None, None, SensorStateClass.TOTAL_INCREASING, 1.0
    if (temperature_unit := TEMPERATURE_UNITS.get(unit or "")) is not None:
        return (
            temperature_unit,
            SensorDeviceClass.TEMPERATURE_DELTA
            if re.fullmatch(
                r"/system/variableTariff/(?:ch|dhw)/(?:highPriceDelta|lowPriceDelta)",
                path,
            )
            else SensorDeviceClass.TEMPERATURE,
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
    if unit in {"mbar", "hPa", "kPa", "Pa"}:
        pressure_unit = {
            "mbar": UnitOfPressure.MBAR,
            "hPa": UnitOfPressure.HPA,
            "kPa": UnitOfPressure.KPA,
            "Pa": UnitOfPressure.PA,
        }[unit]
        return (
            pressure_unit,
            SensorDeviceClass.PRESSURE,
            SensorStateClass.MEASUREMENT,
            1.0,
        )
    if unit == "W":
        return (
            UnitOfPower.WATT,
            SensorDeviceClass.POWER,
            SensorStateClass.MEASUREMENT,
            1.0,
        )
    if unit == "kW":
        return (
            UnitOfPower.KILO_WATT,
            SensorDeviceClass.POWER,
            SensorStateClass.MEASUREMENT,
            1.0,
        )
    if (energy_unit := ENERGY_UNITS.get(unit or "")) is not None:
        return (
            energy_unit[0],
            SensorDeviceClass.ENERGY,
            (
                SensorStateClass.TOTAL_INCREASING
                if path.endswith(("Consumption", "/solarYield"))
                else SensorStateClass.MEASUREMENT
            ),
            energy_unit[1],
        )
    if unit in {"l", "L"}:
        return (
            UnitOfVolume.LITERS,
            SensorDeviceClass.WATER,
            (
                SensorStateClass.TOTAL_INCREASING
                if path.endswith("TotalConsumption")
                else SensorStateClass.MEASUREMENT
            ),
            1.0,
        )
    if unit in {"m3", "m³"}:
        device_class = (
            SensorDeviceClass.GAS
            if "gas" in path.casefold()
            else SensorDeviceClass.WATER
        )
        return (
            UnitOfVolume.CUBIC_METERS,
            device_class,
            SensorStateClass.TOTAL_INCREASING,
            1.0,
        )
    if unit in {"l/min", "L/min"}:
        return (
            UnitOfVolumeFlowRate.LITERS_PER_MINUTE,
            SensorDeviceClass.VOLUME_FLOW_RATE,
            SensorStateClass.MEASUREMENT,
            1.0,
        )
    if (duration_unit := DURATION_UNITS.get(unit or "")) is not None:
        return (
            duration_unit,
            SensorDeviceClass.DURATION,
            SensorStateClass.MEASUREMENT,
            1.0,
        )
    if unit == "%":
        percentage_device_class = next(
            (
                candidate
                for pattern, candidate in _PERCENTAGE_DEVICE_CLASS_PATTERNS
                if pattern.fullmatch(path)
            ),
            None,
        )
        return (
            PERCENTAGE,
            percentage_device_class,
            SensorStateClass.MEASUREMENT,
            1.0,
        )
    if value_key in {"percent", "cur_percent"}:
        return PERCENTAGE, None, SensorStateClass.MEASUREMENT, 1.0
    if resource.metadata.resource_type == "floatValue":
        return None, None, SensorStateClass.MEASUREMENT, 1.0
    return None, None, None, 1.0


def _enum_translation_key(resource: Resource, value_key: str | None) -> str | None:
    """Return the state-translation group for a known textual PointT value."""
    if rrc.scalar_field(resource.path) == "userMode":
        return "rrc_user_mode"
    if is_additional_app_scalar(resource.path) and resource.path.endswith(
        "/operationMode"
    ):
        return "heating_circuit_operation_mode"
    if (
        softener := softener_capability(resource.path)
    ) is not None and softener.kind == "phase":
        return "softener_regeneration_phase"
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
        if re.fullmatch(r"/heatingCircuits/[^/]+/cooling/controlType", path):
            return "heating_circuit_cooling_control_type"
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
    if path == "/heatSources/type" or re.fullmatch(r"/heatSources/hs[^/]+/type", path):
        return "heat_source_type"
    if re.fullmatch(r"/heatSources/hs[^/]+/defrostActive", path):
        return "on_off_state"
    if re.fullmatch(
        r"(?:/heatingCircuits/[^/]+/(?:boostMode|openWindowDetection/(?:enabled|status)|setpointOptimization)|/dhwCircuits/[^/]+/recirculation/enabled|/heatSources/hybrid/(?:reminderEnable|reminderLapsed)|/system/(?:appliance/enabled|powerGuard/active|powerLimitation/active|silentMode/enabled|variableTariff/(?:ch|dhw)/(?:highPriceEnable|lowPriceEnable|optimization))|/solarCircuits/[^/]+/maxTemperatureReached|/pool/enabled|/zones/zone[^/]+/childLock|/pv/(?:enable|surplusAvailable))",
        path,
    ):
        return "on_off_state"
    return {
        "/gateway/dataProcessing/status": "data_processing_status",
        "/system/type": "system_type",
        "/system/awayMode/enabled": "on_off_state",
        "/system/globalSeasonOptimizer/currentMode": "season_optimizer_mode",
        "/system/iSRC/supportStatus": "isrc_support_status",
        "/heatSources/chStatus": "on_off_state",
        "/heatSources/compressor/status": "compressor_status",
        "/heatSources/Source/eHeater/status": "electric_auxiliary_heater_status",
        "/heatSources/additionalHeater/operationMode": (
            "auxiliary_heater_operation_mode"
        ),
        "/heatSources/additionalHeater/primary/status": (
            "electric_auxiliary_heater_status"
        ),
        "/heatSources/currentEmergencyMode": "on_off_state",
        "/heatSources/emStatus": "energy_management_status",
        "/heatSources/flameStatus": "on_off_state",
        "/heatSources/pvContactState": "pv_contact_state",
        "/heatSources/smartFunction/active": "on_off_state",
        "/heatSources/smartFunction/enabled": "on_off_state",
        "/heatSources/standbyMode": "on_off_state",
        "/system/sensors/temperatures/outdoorTemperatureSource": (
            "outdoor_temperature_source"
        ),
        "/system/variableTariff/supportStatus": "support_status",
    }.get(path)


def _known_value_keys(path: str) -> tuple[str, ...]:
    """Return stable subkeys for resources whose startup payload may be partial."""
    if path == _SYSTEM_PRESSURE_RANGE_PATH:
        return _SYSTEM_PRESSURE_RANGE_KEYS
    if re.fullmatch(r"/heatSources/[^/]+/numberOfStarts", path):
        return (*_KNOWN_MULTIPART_KEYS, "pool")
    if re.fullmatch(r"/heatSources/[^/]+/workingTime", path):
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
    if translation_key == "softener_regeneration_phase":
        return list(PHASES)
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
