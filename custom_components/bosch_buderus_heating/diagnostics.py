"""Privacy-preserving diagnostics for Bosch/Buderus Home Comfort."""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from time import monotonic, time
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import BoschBuderusConfigEntry, rrc
from .auth_diagnostics import loaded_auth_history
from .auth_lifecycle import connection_token_manager
from .cloud_pauses import cloud_pause_store
from .configuration import CLOUD_KIND, LOCAL_KIND
from .configuration_access import EntryConfiguration
from .configuration_management import section
from .configuration_migration import legacy_cloud_connection_id
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
from .observations import (
    Observation,
    from_cloud_snapshot,
    from_local_observation,
    pair_for_path,
    pair_observations,
    pair_summary,
)
from .pointt import Gateway, Resource
from .pointt.redaction import diagnostic_resource_path, resource_path_template
from .read_contracts import assess_runtime, is_monitor_collection, is_runtime_path
from .resource_catalog import (
    PollGroup,
    capability_maturity,
    entity_enabled_by_default,
    poll_group,
    resource_name,
    supports_entity,
)
from .rrc_programs import DAY, WEEK, parse_schedule, program_names, selection_issue
from .runtime import BoschBuderusRuntimeData
from .start_counters import assess_start_counter, is_start_counter_path
from .startup_data import LICENSE_PATH, STARTUP_BUDGET_SECONDS, safe_technical_value
from .update_load import update_load_diagnostics
from .wallbox import WallboxCoordinator
from .water_softener import (
    is_water_softener,
    softener_capability,
    softener_schema_issue,
    softener_unit,
    softener_value,
)
from .writes import EnumWritePolicy, NumberWritePolicy, assess_control

DIAGNOSTICS_SCHEMA_VERSION = 44
_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .managed_entry import ManagedEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> dict[str, Any]:
    """Return schema, aggregates and reviewed metadata without private values."""
    from .managed_entry import ManagedEntry

    runtime = getattr(entry, "runtime_data", None)
    try:
        cloud_data = EntryConfiguration(entry).cloud_data()
    except ValueError:
        # Diagnostics must remain available after a downgrade or broken setup.
        cloud_data = {}
    selected = cloud_data.get(CONF_GATEWAY_IDS, [])
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
            "brand": _safe_token(cloud_data.get(CONF_BRAND)),
            "configured_gateway_count": len(selected)
            if isinstance(selected, list)
            else 0,
        },
        "update_load": update_load_diagnostics(hass),
    }
    if isinstance(runtime, ManagedEntry):
        return _managed_diagnostics(base, runtime)
    base["authentication"] = [
        {"connection": number, **_access_authentication(hass, entry.entry_id, key)}
        for number, key in enumerate(_history_connections(hass, entry), 1)
    ]
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


def _history_connections(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> list[str]:
    """Order loaded histories with the original cloud access first."""
    legacy = legacy_cloud_connection_id(entry.entry_id)
    keys = {legacy}
    connections = entry.data.get("connections")
    if isinstance(connections, dict):
        keys.update(
            key
            for key, value in connections.items()
            if isinstance(value, dict) and value.get("kind") == CLOUD_KIND
        )
    return sorted(
        (key for key in keys if loaded_auth_history(hass, entry.entry_id, key)),
        key=lambda key: (key != legacy, key),
    ) or [legacy]


def _access_authentication(
    hass: HomeAssistant, owner_id: str, connection_id: str
) -> dict[str, Any]:
    """Describe one access's token history without credentials or fingerprints."""
    manager = connection_token_manager(hass, owner_id, connection_id)
    recorder = loaded_auth_history(hass, owner_id, connection_id)
    if recorder is not None:
        return recorder.diagnostics(manager)
    report: dict[str, Any] = {"history_available": False}
    if manager is not None:
        report["current"] = {
            "access_token_remaining_s": round(manager.tokens.expires_at - time()),
            "has_refresh_token": bool(manager.tokens.refresh_token),
            "refresh_rejected": manager.refresh_rejected,
        }
    return report


def _managed_diagnostics(base: dict[str, Any], runtime: ManagedEntry) -> dict[str, Any]:
    """Use export-local numbers for relationships; never serialize configuration."""
    from .managed_cloud import CloudConnection
    from .managed_local import LocalConnection

    state = runtime.connections
    snapshot = state.snapshot
    options = snapshot.to_options()
    connections = {key: number for number, key in enumerate(snapshot.connections, 1)}
    targets = {key: number for number, key in enumerate(snapshot.targets, 1)}
    bindings = {key: number for number, key in enumerate(snapshot.bindings, 1)}
    connection_reports = []
    for key, number in connections.items():
        record = snapshot.connections[key]
        driver = state.connection_driver(key)
        report: dict[str, Any] = {
            "connection": number,
            "kind": record.kind
            if record.kind in (CLOUD_KIND, LOCAL_KIND)
            else "unsupported",
            "enabled": section(options, "connections")[key]["enabled"],
            "phase": state.connection_phase(key).value,
            # Schema 44 (B08): failed connection attempts, without messages.
            "failures": state.connection_failures(key),
            "request_metrics": {},
        }
        if record.kind == CLOUD_KIND:
            report["cloud_pause"] = cloud_pause_store(runtime.hass).diagnostics(
                (runtime.entry.entry_id, key)
            )
            report["authentication"] = _access_authentication(
                runtime.hass, runtime.entry.entry_id, key
            )
        if isinstance(driver, CloudConnection):
            report["request_metrics"] = driver.resources.metrics.snapshot()
            account = driver.additional.account
            report["bacon"] = (
                account.diagnostics() if account is not None else {"configured": False}
            )
        elif isinstance(driver, LocalConnection):
            report["local"] = [
                source.diagnostics() for source in driver.sources.values()
            ]
        connection_reports.append(report)
    sources = []
    gateways: list[dict[str, Any]] = []
    wallboxes = []
    for key, number in bindings.items():
        binding = snapshot.bindings[key]
        relationship = {
            "binding": number,
            "connection": connections[binding.connection_id],
            "target": targets[binding.target_id],
        }
        sources.append(
            {
                **relationship,
                "adapter": binding.adapter
                if binding.adapter in ("pointt", "bacon", "k40_local")
                else "unsupported",
                "enabled": section(options, "bindings")[key]["enabled"],
                "phase": state.source_phase(key).value,
                # Schema 44 (B08): failed source attempts, without messages.
                "failures": state.source_failures(key),
            }
        )
        value = state.source_data(key)
        if isinstance(value, BoschBuderusDataUpdateCoordinator):
            gateways.append(
                {**_gateway_diagnostics(len(gateways) + 1, value), **relationship}
            )
        elif isinstance(value, WallboxCoordinator):
            wallboxes.append({**value.diagnostics(), **relationship})
    base["config"].pop("brand", None)
    base["config"]["configured_gateway_count"] = sum(
        target.kind == "pointt_object" for target in snapshot.targets.values()
    )
    if any(report["technical_metadata"] for report in gateways):
        base["privacy"]["contains_raw_resource_values"] = True
        base["privacy"]["raw_resource_values_scope"] = "allowlisted_technical_metadata"
    return {
        **base,
        "setup": {"runtime_available": True},
        "request_metrics": {},
        "connections": connection_reports,
        "targets": [
            {
                "target": number,
                "kind": snapshot.targets[key].kind
                if snapshot.targets[key].kind
                in ("pointt_object", "standalone_device", "local_installation")
                else "unsupported",
                "binding_count": sum(
                    item.target_id == key for item in snapshot.bindings.values()
                ),
            }
            for key, number in targets.items()
        ],
        "sources": sources,
        "observation_pairs": _observation_pairs(runtime, targets),
        "local_plant_view": _local_plant_views(runtime, targets),
        # P49b: systems shown from their local readings alone (count only).
        "local_plant_targets": len(runtime.connections.target_views()),
        "views_without_cloud": _views_without_cloud(runtime, targets),
        "inventory": {
            "healthy": runtime.inventory.healthy,
            "retained_entity_count": len(runtime.inventory.entities),
        },
        "gateways": gateways,
        "wallboxes": wallboxes,
    }


def _add_counters(
    total: dict[str, dict[str, Any]], summary: dict[str, dict[str, Any]]
) -> None:
    """Add counter figures of several sources of the same target."""
    for name, item in summary.items():
        entry = total.setdefault(name, {"shown": {}, "switches": 0, "blocked": 0})
        for source, count in item["shown"].items():
            entry["shown"][source] = entry["shown"].get(source, 0) + count
        entry["switches"] += item["switches"]
        entry["blocked"] += item["blocked"]


def _merged_names(selections: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Catalog measurement names per cloud reason; no values, paths or IDs."""
    merged: dict[str, set[str]] = {}
    for item in selections:
        for reason, names in item.get("cloud_reason_measurements", {}).items():
            merged.setdefault(reason, set()).update(names)
    return {reason: sorted(names) for reason, names in sorted(merged.items())}


def _merged_write_checks(
    selections: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """Local read-backs after confirmed cloud writes, per catalog measurement."""
    merged: dict[str, Counter[str]] = {}
    for item in selections:
        for name, counts in item.get("write_checks", {}).items():
            merged.setdefault(name, Counter()).update(counts)
    return {
        name: dict(sorted(counts.items())) for name, counts in sorted(merged.items())
    }


def _observation_pairs(
    runtime: ManagedEntry, targets: dict[str, int]
) -> list[dict[str, Any]]:
    """Compare catalog pairs per target without values, paths or identifiers."""
    state = runtime.connections
    snapshot = state.snapshot
    reports = []
    for target_key, number in targets.items():
        cloud: list[Observation] = []
        local: list[Observation] = []
        selections: list[dict[str, Any]] = []
        counters: dict[str, dict[str, Any]] = {}
        for binding_key, binding in snapshot.bindings.items():
            if binding.target_id != target_key:
                continue
            value = state.source_data(binding_key)
            if isinstance(value, BoschBuderusDataUpdateCoordinator):
                if value.local_selection is not None:
                    selections.append(value.local_selection.summary(value.data or {}))
                if value.counter_guard is not None:
                    _add_counters(counters, value.counter_guard.summary())
                cloud.extend(
                    from_cloud_snapshot(
                        item, connection_id=binding.connection_id, target_id=target_key
                    )
                    for path, item in (value.data or {}).items()
                    if pair_for_path(path) is not None
                )
            elif binding.adapter == "k40_local":
                local_state = state.local_state(binding_key)
                if local_state is None:
                    continue
                local.extend(
                    from_local_observation(
                        item,
                        connection_id=binding.connection_id,
                        target_id=target_key,
                        current=True,
                    )
                    for item in local_state.current_observations
                    if pair_for_path(item.request.path) is not None
                )
        if cloud or local or selections:
            reports.append(
                {
                    "target": number,
                    "cloud_observations": len(cloud),
                    "local_observations": len(local),
                    **pair_summary(pair_observations(cloud, local)),
                    "local_values": {
                        "enabled": any(item["enabled"] for item in selections),
                        "eligible": sum(
                            item["eligible_entities_sources"] for item in selections
                        ),
                        "using_local": sum(item["using_local"] for item in selections),
                        "using_cloud": sum(item["using_cloud"] for item in selections),
                        "cloud_reasons": dict(
                            sorted(
                                sum(
                                    (
                                        Counter(item["cloud_reasons"])
                                        for item in selections
                                    ),
                                    Counter(),
                                ).items()
                            )
                        ),
                        "cloud_reason_measurements": _merged_names(selections),
                        "write_checks": _merged_write_checks(selections),
                    },
                    "counters": counters,
                }
            )
    return reports


def _views_without_cloud(
    runtime: ManagedEntry, targets: dict[str, int]
) -> list[dict[str, Any]]:
    """A5: plants shown without a running cloud, per target.

    The kind of view, whether its local error list is current, how many of
    its entities are shown and available, and its active fault count. No
    identifiers, paths or values.
    """
    state = runtime.connections
    snapshot = state.snapshot
    views: list[tuple[str, str, Any]] = [
        (target_id, "target_view", view)
        for target_id, view in state.target_views().items()
    ]
    for binding_key, binding in snapshot.bindings.items():
        view = state.retained_data(binding_key)
        if view is not None and state.source_data(binding_key) is None:
            views.append((binding.target_id, _view_kind(view), view))
    registry = er.async_get(runtime.hass)
    entries = er.async_entries_for_config_entry(registry, runtime.entry.entry_id)
    reports = []
    for target_id, kind, view in views:
        try:
            gateway = view.gateway.gateway_id
            shown = [
                entry
                for entry in entries
                if entry.disabled_by is None
                and entry.unique_id.startswith(f"{gateway}:")
            ]
            available = sum(
                (item := runtime.hass.states.get(entry.entity_id)) is not None
                and item.state != "unavailable"
                for entry in shown
            )
            faults = view.faults
            reports.append(
                {
                    "target": targets.get(target_id),
                    "kind": kind,
                    "local_list_decides": faults.local_authority is True,
                    "local_list_current": faults.local_list_current,
                    "entities": len(shown),
                    "entities_available": available,
                    "active_faults": len(faults.active_faults),
                }
            )
        except Exception:  # the report must stay available
            _LOGGER.debug("View without a cloud not reportable", exc_info=True)
            reports.append({"target": targets.get(target_id), "error": "unavailable"})
    return sorted(reports, key=lambda item: (item["target"] or 0, item.get("kind", "")))


def _view_kind(view: Any) -> str:
    """The kind of a view kept without a running cloud source."""
    if getattr(view, "read_only_view", False) is True:
        return "local_plant"
    if view.faults.local_authority is not True:
        # Kept for a cloud expected back (sign-in required, outage): the
        # cloud still decides the faults (P42).
        return "kept_for_cloud"
    return "dormant" if getattr(view, "inventory_view", False) else "held"


def _local_plant_views(
    runtime: ManagedEntry, targets: dict[str, int]
) -> list[dict[str, Any]]:
    """Shadow comparison (P49b): the entities the local readings alone would
    give, against the running cloud and the registry. Builds no entities and
    sends no request; unique IDs after the gateway only, never values."""
    state = runtime.connections
    snapshot = state.snapshot
    entries = er.async_entries_for_config_entry(
        er.async_get(runtime.hass), runtime.entry.entry_id
    )
    reports = []
    for target_key, number in targets.items():
        coordinators: list[BoschBuderusDataUpdateCoordinator] = []
        observations: list[Any] = []
        local_access = False
        for binding_key, binding in snapshot.bindings.items():
            if binding.target_id != target_key:
                continue
            value = state.source_data(binding_key)
            if isinstance(value, BoschBuderusDataUpdateCoordinator):
                coordinators.append(value)
            elif binding.adapter == "k40_local":
                local_state = state.local_state(binding_key)
                if local_state is not None:
                    local_access = True
                    observations.extend(local_state.current_observations)
        if not local_access or not coordinators:
            continue
        for coordinator in coordinators:
            try:
                report = _local_plant_view(coordinator, observations, entries)
            except Exception:  # the report must stay available
                _LOGGER.debug("Local plant view not available", exc_info=True)
                report = {"error": "unavailable"}
            reports.append({"target": number, **report})
    return reports


def _local_plant_view(
    coordinator: BoschBuderusDataUpdateCoordinator,
    observations: list[Any],
    entries: list[er.RegistryEntry],
) -> dict[str, Any]:
    from .local_plant_view import (
        build_local_plant_view,
        compatible_templates,
        description_shapes,
        registry_shapes,
        shadow_comparison,
    )

    cloud_paths = frozenset(coordinator.resources)
    # Built from the local readings and the cloud's path list alone, as from
    # the saved inventory without the cloud: no cloud metadata as templates.
    view = build_local_plant_view(observations, cloud_paths=cloud_paths)
    templates = {
        path: item.resource
        for path, item in (coordinator.data or {}).items()
        if item.available and item.freshness is Freshness.FRESH
    }
    registry, per_source = registry_shapes(entries, coordinator.gateway.gateway_id)
    return shadow_comparison(
        view,
        cloud=description_shapes(coordinator.resources),
        registry=registry,
        cloud_paths=cloud_paths,
        templates_compatible=compatible_templates(view, templates),
        per_source_entities=per_source,
    )


def _gateway_diagnostics(
    number: int, coordinator: BoschBuderusDataUpdateCoordinator
) -> dict[str, object]:
    resources = tuple(coordinator.resources.values())
    resource_types = Counter(
        _safe_token(item.metadata.resource_type) for item in resources
    )
    # The group each path is actually polled in (O51 included).
    polling_groups = Counter(
        coordinator.polling_group(item).value for item in resources
    )
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
        "runtime": {
            **coordinator.diagnostics_summary(),
            # O41/O45/O51: counts and a fixed interval only.
            **coordinator.cloud_rate_summary(),
        },
        "discovery": _discovery_diagnostics(coordinator),
        "discovery_recovery": coordinator.discovery_recovery.diagnostics(monotonic()),
        "rrc_programs": _rrc_program_diagnostics(coordinator),
        "startup_inventory": coordinator.startup_inventory_diagnostics(monotonic()),
        "firmware_changes": coordinator.firmware.diagnostics(monotonic()),
        "startup_data": _startup_diagnostics(coordinator),
        "start_counter_recovery": coordinator.start_counter_recovery_diagnostics(),
        "technical_metadata": [
            {"path": diagnostic_resource_path(resource.path), "value": value}
            for resource in resources
            if (value := safe_technical_value(resource)) is not None
        ],
        "faults": {
            **fault_state,
            # O94: masked like every other path (hs1 stays, IDs are hidden).
            "supported_resources": tuple(
                sorted(
                    diagnostic_resource_path(path)
                    for path in supported_fault_resources
                    if isinstance(path, str)
                )
            ),
            "resource_results": {
                diagnostic_resource_path(path): _safe_token(result)
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
                group=coordinator.polling_group(resource),
            )
            for resource in sorted(
                resources, key=lambda item: _path_template(item.path)
            )
        ],
    }


def _rrc_program_diagnostics(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> dict[str, object]:
    """#113 (RRC-02): why each RRC zone offers a program selection or not.

    Counts and stable reason tokens only; no program names or values.
    """
    snapshots = coordinator.data or {}

    def current(path: str) -> Resource | None:
        snapshot = snapshots.get(path)
        if snapshot is not None and snapshot.available:
            return snapshot.resource
        return coordinator.resources.get(path)

    programs = current(rrc.PROGRAM_LIST)
    snapshot = snapshots.get(rrc.PROGRAM_LIST)
    state = (
        "missing"
        if rrc.PROGRAM_LIST not in coordinator.resources
        else "current"
        if snapshot is not None
        and snapshot.available
        and snapshot.freshness is Freshness.FRESH
        else "not_current"
    )
    identifiers = rrc.listed_ids(programs) if programs is not None else None
    zones = [
        {
            "path": diagnostic_resource_path(path),
            "selection": reason
            if (reason := selection_issue(resource, programs)) is not None
            else "offered",
            "used": resource.metadata.used,
        }
        for path in sorted(coordinator.resources)
        if rrc.scalar_field(path) == "clockProgram"
        and (resource := current(path)) is not None
    ]
    return {
        "program_selections": len(zones),
        "program_selections_offered": sum(
            zone["selection"] == "offered" for zone in zones
        ),
        "program_list": state,
        "program_list_issue": rrc.list_issue(programs)
        if programs is not None
        else None,
        "program_count": len(identifiers) if identifiers is not None else 0,
        "program_names_readable": len(program_names(programs)),
        "zones": zones,
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
    *,
    group: PollGroup | None = None,
) -> dict[str, object]:
    """``group`` is the group the path is actually polled in, if known."""
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
        "poll_group": (group or poll_group(resource)).value,
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
            requires_all_options=policy.options_rule == "exact",
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
    masked = Counter(diagnostic_resource_path(path) for path in state.reads)
    indexes: dict[str, int] = {}
    seen: Counter[str] = Counter()
    for path in sorted(state.reads):
        if masked[shown := diagnostic_resource_path(path)] > 1:
            seen[shown] += 1
            indexes[path] = seen[shown]
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
                # O94: the same masked path more than once (for example the
                # product name of two devices) is told apart by its number.
                **(
                    {"index": index} if (index := indexes.get(path)) is not None else {}
                ),
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
