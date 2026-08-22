"""Privacy-preserving diagnostics for Bosch/Buderus Heating."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from homeassistant.core import HomeAssistant

from . import BoschBuderusConfigEntry
from .const import CONF_BRAND, CONF_GATEWAY_IDS
from .coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from .holidays import parse_holiday_state
from .pointt import Gateway, Resource
from .resource_catalog import (
    capability_maturity,
    entity_enabled_by_default,
    poll_group,
    resource_name,
    supports_entity,
)

DIAGNOSTICS_SCHEMA_VERSION = 4


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> dict[str, Any]:
    """Return schema and aggregate state without credentials or raw values."""
    del hass
    runtime = entry.runtime_data
    gateway_reports = [
        _gateway_diagnostics(index, coordinator)
        for index, coordinator in enumerate(runtime.coordinators, start=1)
    ]
    selected = entry.data.get(CONF_GATEWAY_IDS, [])
    return {
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
    return {
        "label": f"gateway_{number}",
        "device_class": _gateway_class(coordinator.gateway),
        "runtime": coordinator.diagnostics_summary(),
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
        },
        "inventory": {
            "resource_count": len(resources),
            "writable_count": sum(item.metadata.writable for item in resources),
            "entity_supported_count": sum(supports_entity(item) for item in resources),
            "resource_types": dict(sorted(resource_types.items())),
            "polling_groups": dict(sorted(polling_groups.items())),
            "maturity_levels": dict(sorted(maturity_levels.items())),
            "current_error_categories": dict(sorted(errors.items())),
        },
        "capabilities": [
            _capability_diagnostics(
                resource,
                snapshots.get(resource.path),
                coordinator.capability_metrics(resource.path),
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
        "value_shape": _value_shape(resource),
        "values_count": len(resource.values),
        "references_count": len(resource.references),
        "allowed_values_count": len(resource.metadata.allowed_values),
        "has_minimum": resource.metadata.minimum is not None,
        "has_maximum": resource.metadata.maximum is not None,
        "available": available,
        "freshness": freshness,
        "source": source,
        "last_error_category": error_category,
        "consecutive_failures": consecutive_failures,
        "calls": metrics,
    }


def _path_template(path: str) -> str:
    """Remove installation-specific logical IDs from a PointT path."""
    for root, placeholder in (
        ("heatingCircuits", "{hc}"),
        ("dhwCircuits", "{dhw}"),
    ):
        path = re.sub(
            rf"^/{root}/[^/]+",
            f"/{root}/{placeholder}",
            path,
        )
    path = re.sub(
        r"^/heatSources/hs\d+(?=/|$)",
        "/heatSources/{hs}",
        path,
        flags=re.IGNORECASE,
    )
    path = re.sub(
        r"^/devices/[^/]+(?=/|$)",
        "/devices/{device}",
        path,
        flags=re.IGNORECASE,
    )
    return path


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
