"""Privacy-preserving diagnostics for Bosch/Buderus Home Comfort."""

from __future__ import annotations

import math
import re
from collections import Counter
from time import monotonic, time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import BoschBuderusConfigEntry, rrc
from .auth_diagnostics import auth_diagnostics_for
from .auth_lifecycle import entry_token_manager
from .const import CONF_BRAND, CONF_GATEWAY_IDS
from .coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from .dhw_safety import assess_dhw_safety, is_dhw_safety_path
from .discovery import DiscoveryPathSource
from .energy_contracts import energy_diagnostics, is_energy_collection
from .holidays import (
    parse_holiday_state,
    parse_holiday_write_configuration,
)
from .pointt import Gateway, Resource
from .pointt.redaction import diagnostic_resource_path, resource_path_template
from .read_contracts import assess_runtime, is_monitor_collection, is_runtime_path
from .resource_catalog import (
    capability_maturity,
    entity_enabled_by_default,
    poll_group,
    resource_name,
    supports_entity,
)
from .rrc_programs import DAY, WEEK, parse_schedule
from .runtime import BoschBuderusRuntimeData
from .start_counters import assess_start_counter, is_start_counter_path
from .startup_data import LICENSE_PATH, STARTUP_BUDGET_SECONDS, safe_technical_value
from .water_softener import (
    is_water_softener,
    softener_capability,
    softener_schema_issue,
    softener_unit,
    softener_value,
)
from .writes import EnumWritePolicy, NumberWritePolicy, assess_control

DIAGNOSTICS_SCHEMA_VERSION = 28


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> dict[str, Any]:
    """Return schema, aggregates and reviewed metadata without private values."""
    runtime = getattr(entry, "runtime_data", None)
    selected = entry.data.get(CONF_GATEWAY_IDS, [])
    base: dict[str, Any] = {
        "diagnostics_schema": DIAGNOSTICS_SCHEMA_VERSION,
        "privacy": {
            "contains_raw_resource_values": False,
            "contains_credentials": False,
            "contains_stable_identifiers": False,
            "contains_user_defined_names": False,
        },
        "config": {
            "entry_version": entry.version,
            "entry_minor_version": entry.minor_version,
            "brand": _safe_token(entry.data.get(CONF_BRAND)),
            "configured_gateway_count": len(selected)
            if isinstance(selected, list)
            else 0,
        },
        "authentication": _authentication_diagnostics(hass, entry),
    }
    if not isinstance(runtime, BoschBuderusRuntimeData):
        return {
            **base,
            "setup": {"runtime_available": False},
            "request_metrics": {},
            "gateways": [],
        }
    gateway_reports = [
        _gateway_diagnostics(index, coordinator)
        for index, coordinator in enumerate(runtime.coordinators, start=1)
    ]
    if any(report["technical_metadata"] for report in gateway_reports):
        base["privacy"]["contains_raw_resource_values"] = True
        base["privacy"]["raw_resource_values_scope"] = "allowlisted_technical_metadata"
    return {
        **base,
        "setup": {"runtime_available": True},
        "request_metrics": runtime.client.metrics.snapshot(),
        "gateways": gateway_reports,
        "wallboxes": [coordinator.diagnostics() for coordinator in runtime.wallboxes],
        "bacon": runtime.bacon.diagnostics()
        if runtime.bacon
        else {"configured": False},
    }


def _authentication_diagnostics(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> dict[str, Any]:
    """Describe token refresh history without credentials or fingerprints."""
    manager = entry_token_manager(hass, entry.entry_id)
    recorder = auth_diagnostics_for(hass, entry.entry_id)
    if recorder is None:
        report: dict[str, Any] = {"history_available": False}
        if manager is not None:
            report["current"] = {
                "access_token_remaining_s": round(
                    manager.tokens.expires_at - _wall_clock(), 1
                ),
                "has_refresh_token": bool(manager.tokens.refresh_token),
                "refresh_rejected": manager.refresh_rejected,
            }
        return report
    return recorder.diagnostics(manager)


def _wall_clock() -> float:
    """Return wall-clock time; separated for tests."""
    return time()


def _gateway_diagnostics(
    number: int, coordinator: BoschBuderusDataUpdateCoordinator
) -> dict[str, object]:
    resources = tuple(coordinator.resources.values())
    resource_types = Counter(
        _safe_token(item.metadata.resource_type) for item in resources
    )
    polling_groups = Counter(poll_group(item).value for item in resources)
    maturity_levels = Counter(
        capability_maturity(item.path).value for item in resources
    )
    snapshots = coordinator.data or {}
    errors = Counter(
        _safe_token(snapshot.last_error_category)
        for snapshot in snapshots.values()
        if snapshot.last_error_category is not None
    )
    fault_state = coordinator.faults.diagnostics()
    supported_fault_resources_value = fault_state.pop("supported_resources")
    supported_fault_resources = (
        supported_fault_resources_value
        if isinstance(supported_fault_resources_value, tuple | list)
        else ()
    )
    fault_resource_results_value = fault_state.pop("resource_results")
    fault_resource_results = (
        fault_resource_results_value
        if isinstance(fault_resource_results_value, dict)
        else {}
    )
    holiday_state = parse_holiday_state(
        coordinator.resources,
        fallback_timezone=dt_util.get_default_time_zone(),
        timezones=coordinator.holiday_timezones,
    )
    current_holiday_resources = {
        path: snapshot.resource
        for path, snapshot in snapshots.items()
        if snapshot.available and snapshot.freshness is Freshness.FRESH
    }
    supported_without_value_count = sum(
        _supported_without_value(resource, snapshots.get(resource.path))
        for resource in resources
    )
    return {
        "label": f"gateway_{number}",
        "device_class": _gateway_class(coordinator.gateway),
        "device_profile": "water_softener"
        if is_water_softener(coordinator.gateway, coordinator.resources)
        else "generic",
        "softener_entities": _softener_entity_registration(coordinator),
        "runtime": coordinator.diagnostics_summary(),
        "discovery": _discovery_diagnostics(coordinator),
        "startup_data": _startup_diagnostics(coordinator),
        "start_counter_recovery": coordinator.start_counter_recovery_diagnostics(),
        "technical_metadata": [
            {"path": diagnostic_resource_path(resource.path), "value": value}
            for resource in resources
            if (value := safe_technical_value(resource)) is not None
        ],
        "faults": {
            **fault_state,
            "supported_resources": tuple(
                sorted(
                    _path_template(path)
                    for path in supported_fault_resources
                    if isinstance(path, str)
                )
            ),
            "resource_results": {
                _path_template(path): _safe_token(result)
                for path, result in fault_resource_results.items()
                if isinstance(path, str)
            },
        },
        "holidays": {
            "supported_resources": holiday_state.supported_paths,
            "valid_period_count": len(holiday_state.periods),
            "invalid_period_count": holiday_state.invalid_period_count,
            "active_status_available": holiday_state.active is not None,
            "timezone_source": holiday_state.timezone_source,
            "calendar_writes_available": parse_holiday_write_configuration(
                current_holiday_resources
            )
            is not None
            or any(
                parse_holiday_write_configuration(
                    current_holiday_resources, source_path=period.source_path
                )
                is not None
                for period in holiday_state.periods
            ),
            "formats": {
                "modern": any(
                    path.startswith("/holidayMode/")
                    for path in current_holiday_resources
                ),
                "icom": any(
                    path.startswith("/system/holidayModes/")
                    for path in current_holiday_resources
                ),
                "wddw2": "/system/holidayMode" in current_holiday_resources,
            },
        },
        "inventory": {
            "resource_count": len(resources),
            "writable_count": sum(item.metadata.writable for item in resources),
            "entity_supported_count": sum(supports_entity(item) for item in resources),
            "resource_types": dict(sorted(resource_types.items())),
            "polling_groups": dict(sorted(polling_groups.items())),
            "maturity_levels": dict(sorted(maturity_levels.items())),
            "current_error_categories": dict(sorted(errors.items())),
            "supported_without_value_count": supported_without_value_count,
        },
        "capabilities": [
            _capability_diagnostics(
                resource,
                snapshots.get(resource.path),
                coordinator.capability_metrics(resource.path),
                coordinator.unknown_enum_value_count(resource.path),
                coordinator.resource_pause_remaining_seconds(resource.path),
            )
            for resource in sorted(
                resources, key=lambda item: _path_template(item.path)
            )
        ],
    }


def _softener_entity_registration(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> list[dict[str, object]]:
    """Distinguish capability eligibility from local HA registration and state."""
    registry = er.async_get(coordinator.hass)
    entities = er.async_entries_for_config_entry(
        registry, coordinator._config_entry.entry_id
    )
    result: list[dict[str, object]] = []
    for path in sorted(coordinator.resources):
        if softener_capability(path) is None:
            continue
        stem = f"{coordinator.gateway.gateway_id}:{path.strip('/').replace('/', ':')}"
        registrations: list[dict[str, object]] = []
        for entity in entities:
            if entity.unique_id != stem and not entity.unique_id.startswith(stem + ":"):
                continue
            state = coordinator.hass.states.get(entity.entity_id)
            registrations.append(
                {
                    "platform": entity.domain,
                    "disabled": entity.disabled_by is not None,
                    "state": "not_loaded"
                    if state is None
                    else "unavailable"
                    if state.state == "unavailable"
                    else "unknown"
                    if state.state == "unknown"
                    else "available",
                }
            )
        result.append(
            {
                "path": diagnostic_resource_path(path),
                "registered": bool(registrations),
                "entities": registrations,
            }
        )
    return result


def _capability_diagnostics(
    resource: Resource,
    snapshot: ResourceSnapshot | None,
    metrics: dict[str, object],
    unknown_enum_values_detected: int,
    polling_pause_remaining_seconds: int = 0,
) -> dict[str, object]:
    available: bool | None = None
    freshness: str | None = None
    source: str | None = None
    error_category: str | None = None
    consecutive_failures = 0
    if snapshot is not None:
        available = snapshot.available
        freshness = snapshot.freshness.value
        source = snapshot.source.value
        error_category = _optional_safe_token(snapshot.last_error_category)
        consecutive_failures = max(0, snapshot.consecutive_failures)
    return {
        "path": diagnostic_resource_path(resource.path),
        "path_template": _path_template(resource.path),
        "name": resource_name(diagnostic_resource_path(resource.path)),
        "resource_type": _safe_token(resource.metadata.resource_type),
        "unit": _safe_unit(resource.metadata.unit),
        "poll_group": poll_group(resource).value,
        "polling_paused": polling_pause_remaining_seconds > 0,
        "polling_pause_remaining_seconds": polling_pause_remaining_seconds,
        "entity_supported": supports_entity(resource),
        "maturity": capability_maturity(resource.path).value,
        "entity_enabled_by_default": entity_enabled_by_default(resource.path),
        "writable": resource.metadata.writable,
        "has_value": resource.has_value,
        "supported_without_value": _supported_without_value(resource, snapshot),
        "value_shape": _value_shape(resource),
        "values_count": len(resource.values),
        "references_count": len(resource.references),
        "allowed_values_count": len(resource.metadata.allowed_values),
        "has_minimum": resource.metadata.minimum is not None,
        "has_maximum": resource.metadata.maximum is not None,
        "control": _control_diagnostics(resource),
        **(
            {
                "rrc_contract": {
                    "physical_device_verified": False,
                    "used": resource.metadata.used,
                    "available": resource.metadata.available,
                    "availability_flags_valid": (
                        resource.metadata.availability_flags_valid
                    ),
                    "reading_valid": rrc.scalar_value(resource) is not None
                    if rrc.scalar_field(resource.path)
                    else None,
                    "schedule_shape_supported": (schedule := parse_schedule(resource))
                    is not None,
                    "existing_points_editable": schedule.editable
                    if schedule is not None
                    else False,
                    "new_points_supported": False,
                }
            }
            if rrc.scalar_field(resource.path)
            or WEEK.fullmatch(resource.path)
            or DAY.fullmatch(resource.path)
            else {}
        ),
        **(
            {"energy_contract": energy_diagnostics(resource)}
            if is_energy_collection(resource)
            else {}
        ),
        **(
            {
                "duration_contract": {
                    "schema_supported": (
                        duration := assess_runtime(resource)
                    ).supported,
                    "rejection_reason": duration.reason,
                    "source_unit": duration.source_unit,
                    "unit_source": duration.unit_source,
                    "output_unit": "h",
                    "scale": duration.scale,
                    "parts": {
                        key or "scalar": {
                            "value_valid": reading.value is not None,
                            "rejection_reason": reading.reason,
                        }
                        for key, reading in duration.readings
                    },
                }
            }
            if is_runtime_path(resource.path)
            else {}
        ),
        **(
            {
                "collection_contract": {
                    "kind": "embedded_resource_collection",
                    "scalar_rejection_reason": "collection_not_scalar",
                    "child_read_policy": "advertised_reference_reads",
                }
            }
            if is_monitor_collection(resource.path)
            else {}
        ),
        **(
            {
                "water_softener": {
                    "evidence": "app_model",
                    "physical_device_verified": False,
                    "schema_rejection_reason": softener_schema_issue(resource),
                    "unit": softener_unit(resource),
                    "value_valid": softener_value(resource) is not None,
                    "write_release": "pending_device_verification",
                }
            }
            if softener_capability(resource.path) is not None
            else {}
        ),
        **(
            {"start_counter": _start_counter_diagnostics(resource, available)}
            if is_start_counter_path(resource.path)
            else {}
        ),
        **(
            {
                "dhw_safety": {
                    "schema_supported": (
                        safety_assessment := assess_dhw_safety(resource)
                    ).supported,
                    "rejection_reason": safety_assessment.reason,
                    "value_valid": safety_assessment.value is not None,
                    "evidence": "app_model",
                    "physical_device_verified": False,
                }
            }
            if is_dhw_safety_path(resource.path)
            else {}
        ),
        "available": available,
        "freshness": freshness,
        "source": source,
        "last_error_category": error_category,
        "consecutive_failures": consecutive_failures,
        "unknown_enum_values_detected": unknown_enum_values_detected,
        "calls": metrics,
    }


def _start_counter_diagnostics(
    resource: Resource, available: bool | None
) -> dict[str, object]:
    """Report the sensor's decisions, never counts or vendor state content."""
    assessment = assess_start_counter(resource)
    return {
        "schema_supported": assessment.supported,
        "schema_rejection_reason": assessment.reason,
        "error_metadata_valid": resource.metadata.numeric_error_states_valid,
        "error_marker_count": len(resource.metadata.numeric_error_values),
        "readings": {
            key or "value": (
                "not_read"
                if available is None
                else "unavailable"
                if not available
                else reading.reason or "valid"
            )
            for key, reading in assessment.readings
        },
    }


def _control_diagnostics(resource: Resource) -> dict[str, object]:
    """Explain scalar control eligibility using the entity builders' checks."""
    assessment = assess_control(resource)
    result: dict[str, object] = {
        "platform": assessment.platform,
        "eligible": assessment.eligible,
        "rejection_reason": assessment.rejection_reason,
        "enabled_by_default": assessment.enabled_by_default,
    }
    policy = assessment.policy
    metadata = resource.metadata
    if policy is not None:
        result.update(
            writable=assessment.writable,
            current_value_issue=assessment.current_value_issue,
        )
    if isinstance(policy, EnumWritePolicy):
        result.update(
            advertised_known_options=sorted(
                policy.known_values.intersection(metadata.allowed_values)
            ),
            unrecognized_option_count=sum(
                value not in policy.known_values for value in metadata.allowed_values
            ),
            requires_all_options=False,
            offered_option_count=len(assessment.options),
            switch_supported=assessment.supports_switch,
            select_supported=assessment.supports_select,
        )
    elif isinstance(policy, NumberWritePolicy):
        result.update(
            minimum=_finite_bound(metadata.minimum),
            maximum=_finite_bound(metadata.maximum),
            ui_step=assessment.ui_step,
            write_step=None,
            unit=assessment.unit
            if assessment.unit in {"C", "F", "K", "°C", "°F", "mins", "min", "s", "h"}
            else None,
        )
    return result


def _finite_bound(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _supported_without_value(
    resource: Resource, snapshot: ResourceSnapshot | None
) -> bool:
    """Return whether a supported scalar capability currently has no value."""
    if snapshot is None or not snapshot.available:
        return False
    current = snapshot.resource
    return (
        supports_entity(current)
        and current.metadata.resource_type
        in {"booleanValue", "floatValue", "stringValue"}
        and not current.values
        and (not current.has_value or current.value is None)
    )


def _path_template(path: str) -> str:
    """Remove installation-specific logical IDs from a PointT path."""
    return resource_path_template(diagnostic_resource_path(path))


def _discovery_diagnostics(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> dict[str, object]:
    """Return discovery decisions with useful but selectively redacted paths."""
    report = coordinator.discovery_diagnostics
    groups: dict[str, Counter[str]] = {}
    for path, item in report.paths.items():
        counters = groups.setdefault(_discovery_group(path), Counter())
        counters["paths_scheduled"] += 1
        counters["paths_requested"] += item.bulk_result != "not_attempted"
        counters["resources_discovered"] += item.discovered
        counters["paths_failed"] += item.failed
        catalog = item.source is DiscoveryPathSource.CATALOG
        counters["catalog_paths"] += catalog
        counters["catalog_paths_discovered"] += catalog and item.discovered
        counters["fallback_attempts"] += item.fallback_reason is not None
        counters["fallback_successes"] += item.fallback_result == "success"
        counters["fallback_failures"] += item.fallback_result not in (None, "success")
    return {
        **report.snapshot(),
        "groups": {
            path: dict(sorted(counters.items()))
            for path, counters in sorted(groups.items())
        },
        "paths": [
            {
                "path": diagnostic_resource_path(path),
                "source": item.source.value,
                "read_policy": item.read_policy,
                "bulk_result": item.bulk_result,
                "fallback_reason": item.fallback_reason,
                "fallback_result": item.fallback_result,
                "discovered": item.discovered,
            }
            for path, item in sorted(
                report.paths.items(),
                key=lambda entry: diagnostic_resource_path(entry[0]),
            )
        ],
    }


def _startup_diagnostics(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> dict[str, object]:
    """Describe pending reads locally without performing a diagnostic request."""
    state = coordinator.startup_data
    now = monotonic()
    return {
        "scope": "current_entry_session",
        "successful_reads_repeat": False,
        "initial_entry_budget_seconds": STARTUP_BUDGET_SECONDS,
        "recovery_max_batch_paths": 30,
        "budget_exhaustions": state.budget_exhaustions,
        "logical_attempts": sum(read.attempts for read in state.reads.values()),
        "http_attempts_location": "request_metrics.requests_by_type.startup_metadata",
        "omitted": [
            {
                "path": LICENSE_PATH,
                "reason": "unused_license_document",
                "requested": False,
            }
        ]
        if state.omitted_license
        else [],
        "paths": [
            {
                "path": diagnostic_resource_path(path),
                "advertised": read.advertised,
                "registered_before_setup": read.registered,
                "active": read.active,
                "result": read.result,
                "pending": read.pending,
                "attempts": read.attempts,
                "consecutive_failures": read.failures,
                "last_attempt": read.last_attempt.isoformat()
                if read.last_attempt
                else None,
                "last_success": read.last_success.isoformat()
                if read.last_success
                else None,
                "next_attempt_in_seconds": max(0, math.ceil(read.next_attempt - now))
                if read.pending
                else None,
            }
            for path, read in sorted(state.reads.items())
        ],
    }


def _discovery_group(path: str) -> str:
    """Group discovery work by safe root or concrete logical circuit."""
    safe_path = diagnostic_resource_path(path)
    parts = safe_path.strip("/").split("/")
    if len(parts) >= 2 and re.fullmatch(
        r"(?:hc|dhw|hs|sc|sw|zone)\d+", parts[1], re.IGNORECASE
    ):
        return f"/{parts[0]}/{parts[1]}"
    return f"/{parts[0]}" if parts and parts[0] else "/"


def _gateway_class(gateway: Gateway) -> str:
    if is_water_softener(gateway, {}):
        return "water_softener_gateway"
    combined = " ".join(
        value for value in (gateway.device_type, gateway.model) if value
    ).upper()
    for known in ("MX300", "MX400", "K30RF", "K30", "K40RF", "K40"):
        if known in combined:
            return known.lower()
    if "HEATPUMP" in combined or "HEAT PUMP" in combined:
        return "heat_pump_gateway"
    return "heating_gateway"


def _value_shape(resource: Resource) -> str:
    value = resource.value
    if not resource.has_value:
        return "values" if resource.values else "none"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _safe_token(value: object) -> str:
    if value is None:
        return "none"
    candidate = str(value)
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", candidate):
        return candidate
    return "other"


def _optional_safe_token(value: object | None) -> str | None:
    return None if value is None else _safe_token(value)


def _safe_unit(value: str | None) -> str:
    if value is None:
        return "none"
    return value if re.fullmatch(r"[A-Za-z0-9%./°_-]{1,16}", value) else "other"
