"""Tests for brand-aware Home Assistant device information."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import CONF_BRAND, DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.device import (
    device_info_for_resource,
    grouped_entity_name,
)
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource


def _coordinator(
    hass: HomeAssistant,
    *,
    brand: str | None = None,
    gateway: Gateway | None = None,
    resources: tuple[Resource, ...] = (),
) -> BoschBuderusDataUpdateCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BRAND: brand} if brand is not None else {},
    )
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass,
        AsyncMock(),
        gateway or Gateway("gateway-one", device_type="k40"),
        entry,
    )
    coordinator.resources = {resource.path: resource for resource in resources}
    coordinator.data = {
        resource.path: ResourceSnapshot(resource, True, datetime.now(UTC))
        for resource in resources
    }
    return coordinator


def test_gateway_uses_pointt_brand_and_readable_model(hass: HomeAssistant) -> None:
    coordinator = _coordinator(
        hass,
        brand="bosch",
        gateway=Gateway("gateway-one", device_type="k40", firmware_version="1.2.3"),
        resources=(
            Resource("/system/brand", "Buderus", True),
            Resource("/gateway/serialId", "serial-123", True),
            Resource("/gateway/versionFirmware", "2.0.0", True),
            Resource("/gateway/versionHardware", "3.0", True),
            Resource(
                "/system/info",
                values=(
                    {
                        "ProductTtn": "product-type-1",
                        "Tok": "must-never-be-used",
                    },
                ),
            ),
        ),
    )

    info = device_info_for_resource(coordinator, "/system/brand")

    assert info["identifiers"] == {(DOMAIN, "gateway-one")}
    assert info["manufacturer"] == "Buderus"
    assert info["model"] == "K40"
    assert info["name"] == "Buderus K40"
    assert info["serial_number"] == "serial-123"
    assert info["sw_version"] == "2.0.0"
    assert info["hw_version"] == "3.0"
    assert info["model_id"] == "product-type-1"


def test_configured_brand_is_fallback_when_brand_resource_is_missing(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(
        hass,
        brand="bosch",
        gateway=Gateway("gateway-one", model="mx300"),
    )

    info = device_info_for_resource(coordinator, "/heatSources/overallStatus")

    assert info["manufacturer"] == "Bosch"
    assert info["model"] == "MX300"
    assert info["name"] == "Bosch MX300"


def test_logical_entity_uses_configured_group_on_shared_gateway(
    hass: HomeAssistant,
) -> None:
    resources = (
        Resource("/gateway/brand", "Buderus", True),
        Resource("/heatingCircuits/hc2/name", "Obergeschoss", True),
        Resource("/heatingCircuits/hc2/operationMode", "auto", True),
    )
    coordinator = _coordinator(hass, resources=resources)

    info = device_info_for_resource(coordinator, "/heatingCircuits/hc2/operationMode")
    name = grouped_entity_name(
        coordinator, "/heatingCircuits/hc2/operationMode", "Operation mode"
    )

    assert info["identifiers"] == {(DOMAIN, "gateway-one")}
    assert info["manufacturer"] == "Buderus"
    assert info["model"] == "K40"
    assert info["name"] == "Buderus K40"
    assert name == "Obergeschoss \N{EN DASH} Operation mode"


def test_single_heat_source_is_identified_as_heat_pump(hass: HomeAssistant) -> None:
    resources = (
        Resource("/system/brand", "Buderus", True),
        Resource("/heatSources/hs1/heatPumpType", "air_water", True),
        Resource("/heatSources/hs1/workingTime", 42, True),
    )
    coordinator = _coordinator(hass, resources=resources)

    info = device_info_for_resource(coordinator, "/heatSources/hs1/workingTime")
    name = grouped_entity_name(
        coordinator, "/heatSources/hs1/workingTime", "Operating time Total"
    )

    assert info["identifiers"] == {(DOMAIN, "gateway-one")}
    assert info["manufacturer"] == "Buderus"
    assert info["model"] == "K40"
    assert info["name"] == "Buderus K40"
    assert name == "Heat pump \N{EN DASH} Operating time Total"


def test_multiple_heat_pumps_receive_unambiguous_names(hass: HomeAssistant) -> None:
    resources = (
        Resource("/heatSources/hs1/type", "Heatpump", True),
        Resource("/heatSources/hs2/type", "Heatpump", True),
        Resource("/heatSources/hs2/workingTime", 21, True),
    )
    coordinator = _coordinator(hass, brand="buderus", resources=resources)

    name = grouped_entity_name(
        coordinator, "/heatSources/hs2/workingTime", "Operating time Total"
    )

    assert name == "Heat pump 2 \N{EN DASH} Operating time Total"


def test_unknown_brand_preserves_neutral_legacy_fallback(hass: HomeAssistant) -> None:
    coordinator = _coordinator(
        hass,
        resources=(Resource("/system/brand", "Other", True),),
    )

    info = device_info_for_resource(coordinator, "/system/brand")

    assert info["manufacturer"] == "Bosch Thermotechnology"


def test_unknown_heat_source_and_descriptive_model_keep_generic_names(
    hass: HomeAssistant,
) -> None:
    resource = Resource("/heatSources/hs1/workingTime", 42, True)
    coordinator = _coordinator(
        hass,
        brand="buderus",
        gateway=Gateway("gateway-one", model="Compress 7000i AW"),
        resources=(resource,),
    )

    gateway_info = device_info_for_resource(coordinator, "/system/overallStatus")
    source_name = grouped_entity_name(coordinator, resource.path, "Operating time")

    assert gateway_info["model"] == "Compress 7000i AW"
    assert source_name == "Heat generator 1 \N{EN DASH} Operating time"


def test_central_resources_receive_clear_groups(hass: HomeAssistant) -> None:
    resources = (
        Resource("/heatSources/hs1/type", "Heatpump", True),
        Resource("/heatSources/actualModulation", 30, True),
    )
    coordinator = _coordinator(hass, brand="buderus", resources=resources)

    assert (
        grouped_entity_name(
            coordinator, "/heatSources/actualModulation", "Current modulation"
        )
        == "Heat pump \N{EN DASH} Current modulation"
    )
    assert (
        grouped_entity_name(coordinator, "/system/brand", "Brand")
        == "System \N{EN DASH} Brand"
    )
    assert (
        grouped_entity_name(coordinator, "/gateway/brand", "Brand")
        == "Gateway \N{EN DASH} Brand"
    )


def test_dynamic_groups_follow_german_system_language(hass: HomeAssistant) -> None:
    hass.config.language = "de"
    coordinator = _coordinator(
        hass,
        resources=(Resource("/heatingCircuits/hc1/operationMode", "auto", True),),
    )

    assert (
        grouped_entity_name(
            coordinator, "/heatingCircuits/hc1/operationMode", "Betriebsart"
        )
        == "Heizkreis 1 \N{EN DASH} Betriebsart"
    )
