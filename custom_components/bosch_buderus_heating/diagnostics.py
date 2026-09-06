"""Privacy-preserving diagnostics for Bosch/Buderus Heating."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from homeassistant.core import HomeAssistant

from . import BoschBuderusConfigEntry
from .const import CONF_BRAND, CONF_GATEWAY_IDS
from .coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from .holidays import (
    HOLIDAY_RESOURCE_PATHS,
    HOLIDAY_TIMEZONE_PATH,
    parse_holiday_state,
    parse_holiday_write_configuration,
)
from .pointt import Gateway, Resource
from .pointt.redaction import diagnostic_resource_path, resource_path_template
from .resource_catalog import (
    capability_maturity,
    entity_enabled_by_default,
    poll_group,
    resource_name,
    supports_entity,
)
from .runtime import BoschBuderusRuntimeData
from .writes import EnumWritePolicy, NumberWritePolicy, assess_control

DIAGNOSTICS_SCHEMA_VERSION = 11


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> dict[str, Any]:
    """Return schema and aggregate state without credentials or raw values."""
    del hass
    runtime = getattr(entry, "runtime_data", None)
    selected = entry.data.get(CONF_GATEWAY_IDS, [])
    base = {
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
    return {
        **base,
        "setup": {"runtime_available": True},
        "request_metrics": runtime.client.metrics.snapshot(),
        "gateways": gateway_reports,
    }


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
        fallback_timezone=coordinator.hass.config.time_zone,
    )
    current_holiday_resources = {
        path: snapshot.resource
        for path in (*HOLIDAY_RESOURCE_PATHS, HOLIDAY_TIMEZONE_PATH)
        if (snapshot := snapshots.get(path)) is not None
        and snapshot.available
        and snapshot.freshness is Freshness.FRESH
    }
    supported_without_value_count = sum(
        _supported_without_value(resource, snapshots.get(resource.path))
        for resource in resources
    )
    return {
        "label": f"gateway_{number}",
        "device_class": _gateway_class(coordinator.gateway),
        "runtime": coordinator.diagnostics_summary(),
        "discovery": _discovery_diagnostics(coordinator),
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
            is not None,
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
            )
            for resource in sorted(
                resources, key=lambda item: _path_template(item.path)
            )
        ],
    }


def _capability_diagnostics(
    resource: Resource,
    snapshot: ResourceSnapshot | None,
    metrics: dict[str, object],
    unknown_enum_values_detected: int,
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
        "name": resource_name(resource.path),
        "resource_type": _safe_token(resource.metadata.resource_type),
        "unit": _safe_unit(resource.metadata.unit),
        "poll_group": poll_group(resource).value,
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
        "available": available,
        "freshness": freshness,
        "source": source,
        "last_error_category": error_category,
        "consecutive_failures": consecutive_failures,
        "unknown_enum_values_detected": unknown_enum_values_detected,
        "calls": metrics,
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
    if isinstance(policy, EnumWritePolicy):
        result.update(
            advertised_known_options=sorted(
                policy.allowed_values.intersection(metadata.allowed_values)
            ),
            unrecognized_option_count=sum(
                value not in policy.allowed_values for value in metadata.allowed_values
            ),
            requires_all_options=policy.require_all_options,
        )
    elif isinstance(policy, NumberWritePolicy):
        result.update(
            minimum=_finite_bound(metadata.minimum),
            maximum=_finite_bound(metadata.maximum),
            policy_minimum=policy.safe_minimum,
            policy_maximum=policy.safe_maximum,
            policy_step=policy.step,
            policy_unit=policy.unit,
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


def _discovery_group(path: str) -> str:
    """Group discovery work by safe root or concrete logical circuit."""
    safe_path = diagnostic_resource_path(path)
    parts = safe_path.strip("/").split("/")
    if len(parts) >= 2 and re.fullmatch(
        r"(?:hc|dhw|hs|sc|zone)\d+", parts[1], re.IGNORECASE
    ):
        return f"/{parts[0]}/{parts[1]}"
    return f"/{parts[0]}" if parts and parts[0] else "/"


def _gateway_class(gateway: Gateway) -> str:
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
