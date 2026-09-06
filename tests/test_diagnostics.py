"""Tests for redacted Home Assistant diagnostics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import (
    CONF_ACCESS_TOKEN,
    CONF_BRAND,
    CONF_GATEWAY_IDS,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.diagnostics import (
    _discovery_diagnostics,
    _gateway_class,
    _path_template,
    _safe_token,
    _safe_unit,
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.discovery import DiscoveryPathSource
from custom_components.bosch_buderus_heating.holidays import HOLIDAY_LIST_PATH
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    PointTClient,
    Resource,
    ResourceMetadata,
    ResourceReference,
)
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData


async def test_diagnostics_contains_schema_and_metrics_but_no_private_data(
    hass: HomeAssistant,
) -> None:
    gateway_id = "private-gateway-id"
    access_token = "private-access-token"
    refresh_token = "private-refresh-token"
    configured_name = "Private living room"
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_BRAND: "buderus",
            CONF_GATEWAY_IDS: [gateway_id],
            CONF_ACCESS_TOKEN: access_token,
            CONF_REFRESH_TOKEN: refresh_token,
        },
    )
    entry.add_to_hass(hass)
    client = PointTClient(AsyncMock(), access_token)
    gateway = Gateway(
        gateway_id,
        device_type="K40RF",
        model="private-model-details",
        firmware_version="private-firmware",
    )
    coordinator = BoschBuderusDataUpdateCoordinator(hass, client, gateway, entry)
    coordinator.discovery_diagnostics.reset()
    coordinator.discovery_diagnostics.scheduled(
        "/heatingCircuits/hc2", DiscoveryPathSource.REFERENCE
    )
    coordinator.discovery_diagnostics.bulk_result(
        BatchItemResult(
            gateway_id,
            "/heatingCircuits/hc2",
            200,
            resource=Resource("/heatingCircuits/hc2"),
        )
    )
    coordinator.discovery_diagnostics.completed = True
    coordinator.discovery_diagnostics.stop_reason = "complete"
    name = Resource(
        path="/heatingCircuits/private-circuit/name",
        value=configured_name,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue",
            allowed_values=("private-option",),
            writable=True,
        ),
    )
    serial = Resource(
        path="/gateway/serialId",
        value="private-serial-number",
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue"),
    )
    reference = Resource(
        path="/heatingCircuits",
        references=(ResourceReference("/heatingCircuits/private-circuit"),),
    )
    holiday = Resource(
        path=HOLIDAY_LIST_PATH,
        value={
            "name": "Private holiday name",
            "start": "2026-08-20",
            "end": "2026-08-25",
        },
        has_value=True,
    )
    hybrid_without_value = Resource(
        path="/heatSources/hybrid/activeHeatSource",
        metadata=ResourceMetadata(resource_type="stringValue"),
    )
    coordinator.resources = {
        item.path: item
        for item in (name, serial, reference, holiday, hybrid_without_value)
    }
    now = datetime.now(UTC)
    coordinator.data = {
        name.path: ResourceSnapshot(
            name,
            False,
            now,
            last_attempt=now,
            source=SnapshotSource.BATCH,
            freshness=Freshness.STALE,
            last_error_category="http_404",
            consecutive_failures=2,
        ),
        serial.path: ResourceSnapshot(serial, True, now),
        reference.path: ResourceSnapshot(reference, True, now),
        holiday.path: ResourceSnapshot(holiday, True, now),
        hybrid_without_value.path: ResourceSnapshot(hybrid_without_value, True, now),
    }
    coordinator._record_capability(name.path, "not_found", SnapshotSource.BATCH)
    coordinator.record_unknown_enum_value(hybrid_without_value.path)
    notification = Resource(path="/notifications", values=({"ccd": 6249, "fc": "12"},))
    coordinator.faults.process_resources({notification.path: notification})
    coordinator.faults.record_results(
        (
            BatchItemResult(
                gateway_id,
                "/devices/private-device/errors",
                404,
            ),
        )
    )
    client.metrics.record_request(
        category="bulk",
        method="POST",
        status=200,
        outcome="success",
        duration_ms=10,
        bulk_size=3,
    )
    entry.runtime_data = BoschBuderusRuntimeData(
        client=client,
        token_manager=AsyncMock(),
        gateways=(gateway,),
        coordinators=(coordinator,),
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    rendered = repr(diagnostics)

    assert diagnostics["diagnostics_schema"] == 12

    for private in (
        gateway_id,
        access_token,
        refresh_token,
        configured_name,
        "private-option",
        "private-serial-number",
        "private-model-details",
        "private-firmware",
        "private-circuit",
        "private-device",
        "Private holiday name",
        "2026-08-20",
        "2026-08-25",
    ):
        assert private not in rendered
    assert diagnostics["privacy"] == {
        "contains_raw_resource_values": False,
        "contains_credentials": False,
        "contains_stable_identifiers": False,
        "contains_user_defined_names": False,
    }
    gateway_report = diagnostics["gateways"][0]
    assert gateway_report["device_class"] == "k40rf"
    assert gateway_report["discovery"]["completed"]
    assert gateway_report["discovery"]["advertised_references"] == 1
    assert gateway_report["discovery"]["groups"] == {
        "/heatingCircuits/hc2": {
            "fallback_attempts": 0,
            "fallback_failures": 0,
            "fallback_successes": 0,
            "paths_requested": 1,
            "paths_failed": 0,
            "paths_scheduled": 1,
            "resources_discovered": 1,
        }
    }
    assert gateway_report["discovery"]["paths"] == [
        {
            "path": "/heatingCircuits/hc2",
            "source": "reference",
            "bulk_result": "success",
            "fallback_reason": None,
            "fallback_result": None,
            "discovered": True,
        }
    ]
    assert gateway_report["runtime"]["resources_stale"] == 1
    assert gateway_report["faults"]["active_faults"] == 1
    assert gateway_report["faults"]["codes"] == ("6249",)
    assert gateway_report["faults"]["resource_results"] == {
        "/devices/{device}/errors": "404",
        "/notifications": "success",
    }
    assert gateway_report["holidays"] == {
        "supported_resources": ("/holidayMode/list",),
        "valid_period_count": 1,
        "invalid_period_count": 0,
        "active_status_available": True,
        "timezone_source": "home_assistant",
        "calendar_writes_available": False,
    }
    assert gateway_report["inventory"]["current_error_categories"] == {"http_404": 1}
    assert gateway_report["inventory"]["maturity_levels"] == {"understood": 5}
    assert gateway_report["inventory"]["supported_without_value_count"] == 1
    capability = next(
        item
        for item in gateway_report["capabilities"]
        if item["path_template"] == "/heatingCircuits/{hc}/name"
    )
    assert capability["value_shape"] == "string"
    assert capability["path"] == "/heatingCircuits/{hc}/name"
    assert capability["allowed_values_count"] == 1
    assert capability["last_error_category"] == "http_404"
    assert capability["maturity"] == "understood"
    assert not capability["entity_enabled_by_default"]
    assert capability["name"] == "Name"
    assert capability["calls"] == {
        "attempts_total": 1,
        "successful": 0,
        "failed": 1,
        "success_rate_percent": 0.0,
        "results": {"not_found": 1},
        "attempts_by_source": {"batch": 1},
        "last_result": "not_found",
    }
    hybrid_capability = next(
        item
        for item in gateway_report["capabilities"]
        if item["path_template"] == "/heatSources/hybrid/activeHeatSource"
    )
    assert hybrid_capability["supported_without_value"]
    assert hybrid_capability["unknown_enum_values_detected"] == 1
    assert diagnostics["request_metrics"]["requests_total"] == 1
    assert diagnostics["request_metrics"]["requests_successful"] == 1
    assert diagnostics["request_metrics"]["success_rate_percent"] == 100.0


async def test_diagnostic_names_do_not_leak_dynamic_container_identifiers(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    client = PointTClient(AsyncMock(), "synthetic-token")
    gateway = Gateway("synthetic-gateway")
    coordinator = BoschBuderusDataUpdateCoordinator(hass, client, gateway, entry)
    marker = "SYNTHETICPRIVATE123456"
    roots = (
        "devices",
        "heatingCircuits",
        "dhwCircuits",
        "heatSources",
        "solarCircuits",
        "ventilation",
        "zones",
    )
    coordinator.resources = {
        f"/{root}/{marker}": Resource(f"/{root}/{marker}") for root in roots
    }
    entry.runtime_data = BoschBuderusRuntimeData(
        client, AsyncMock(), (gateway,), (coordinator,)
    )
    report = await async_get_config_entry_diagnostics(hass, entry)
    assert marker.casefold() not in json.dumps(report).casefold()
    assert len(report["gateways"][0]["capabilities"]) == len(roots)


async def test_diagnostics_are_available_before_runtime_setup(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_BRAND: "buderus",
            CONF_GATEWAY_IDS: ["private-gateway-id"],
            CONF_ACCESS_TOKEN: "private-access-token",
            CONF_REFRESH_TOKEN: "private-refresh-token",
        },
    )
    entry.add_to_hass(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["setup"] == {"runtime_available": False}
    assert diagnostics["request_metrics"] == {}
    assert diagnostics["gateways"] == []
    assert "private-gateway-id" not in repr(diagnostics)
    assert "private-access-token" not in repr(diagnostics)


def test_diagnostics_normalizers_never_echo_unknown_private_strings() -> None:
    assert _path_template("/heatingCircuits/hc2/currentRoomSetpoint") == (
        "/heatingCircuits/{hc}/currentRoomSetpoint"
    )
    assert _path_template("/dhwCircuits/dhw7/actualTemp") == (
        "/dhwCircuits/{dhw}/actualTemp"
    )
    assert _path_template("/heatSources/hs3/type") == "/heatSources/{hs}/type"
    assert _path_template("/heatSources/actualHeatDemand") == (
        "/heatSources/actualHeatDemand"
    )
    assert _path_template("/heatSources/emon/totalConsumption") == (
        "/heatSources/emon/totalConsumption"
    )
    assert _path_template("/devices/private-device/errors") == (
        "/devices/{device}/errors"
    )
    assert _gateway_class(Gateway("secret", model="unknown-private")) == (
        "heating_gateway"
    )
    assert _gateway_class(Gateway("secret", device_type="heat pump")) == (
        "heat_pump_gateway"
    )
    assert _safe_token("safe_token-1") == "safe_token-1"
    assert _safe_token("private value with spaces") == "other"
    assert _safe_unit("bar") == "bar"
    assert _safe_unit("private unit with spaces") == "other"


@pytest.mark.parametrize(
    "root",
    [
        "heatingCircuits",
        "dhwCircuits",
        "heatSources",
        "solarCircuits",
        "ventilation",
        "zones",
    ],
)
def test_diagnostics_templates_also_hide_unrecognized_identifiers(root: str) -> None:
    assert "private-id" not in _path_template(f"/{root}/private-id/status")


async def test_discovery_diagnostics_counts_reads_and_failures_per_circuit(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway"), entry
    )
    report = coordinator.discovery_diagnostics
    first = "/heatingCircuits/hc1/operationMode"
    second = "/heatingCircuits/hc2/operationMode"
    pending = "/heatingCircuits/hc2/boostMode"
    for path in (first, second, pending):
        report.scheduled(path, DiscoveryPathSource.REFERENCE)
    report.bulk_started((first, second))
    for path in (first, second):
        report.bulk_result(BatchItemResult("gateway", path, 502))
        report.fallback_started(path, "gateway_5xx")
    report.fallback_finished(first, "http_404", discovered=False)
    report.fallback_finished(second, "success", discovered=True)

    result = _discovery_diagnostics(coordinator)

    assert result["attempts_scope"] == "logical_resource_reads"
    assert result["bulk_calls"] == 1
    assert result["paths_requested"] == 2
    assert result["paths_failed"] == 1
    assert result["fallback_attempts"] == 2
    assert result["groups"]["/heatingCircuits/hc1"] == {
        "paths_scheduled": 1,
        "paths_requested": 1,
        "paths_failed": 1,
        "resources_discovered": 0,
        "fallback_attempts": 1,
        "fallback_successes": 0,
        "fallback_failures": 1,
    }
    assert result["groups"]["/heatingCircuits/hc2"] == {
        "paths_scheduled": 2,
        "paths_requested": 1,
        "paths_failed": 0,
        "resources_discovered": 1,
        "fallback_attempts": 1,
        "fallback_successes": 1,
        "fallback_failures": 0,
    }
