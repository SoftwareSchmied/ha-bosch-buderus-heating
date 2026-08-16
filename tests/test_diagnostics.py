"""Tests for redacted Home Assistant diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

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
    _gateway_class,
    _path_template,
    _safe_token,
    _safe_unit,
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
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
    coordinator.resources = {item.path: item for item in (name, serial, reference)}
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
    }
    coordinator._record_capability(name.path, "not_found", SnapshotSource.BATCH)
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
    assert gateway_report["runtime"]["resources_stale"] == 1
    assert gateway_report["inventory"]["current_error_categories"] == {"http_404": 1}
    assert gateway_report["inventory"]["maturity_levels"] == {"understood": 3}
    capability = next(
        item
        for item in gateway_report["capabilities"]
        if item["path_template"] == "/heatingCircuits/{hc}/name"
    )
    assert capability["value_shape"] == "string"
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
    assert diagnostics["request_metrics"]["requests_total"] == 1
    assert diagnostics["request_metrics"]["requests_successful"] == 1
    assert diagnostics["request_metrics"]["success_rate_percent"] == 100.0


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
