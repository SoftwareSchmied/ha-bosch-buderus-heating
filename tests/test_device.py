"""Tests for brand-aware Home Assistant device information."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

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
from custom_components.bosch_buderus_heating.resource_catalog import resource_name
from tests.ha_test_helpers import MockConfigEntry


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
    assert info["model"] == "Heating system"
    assert info["name"] == "Heating"
    assert info["serial_number"] == "serial-123"
    assert info["sw_version"] == "2.0.0"
    assert info["hw_version"] == "3.0"
    assert info.get("model_id") is None


def test_new_function_groups_retain_registered_device_and_user_choices(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    entry = coordinator.config_entry
    assert entry is not None
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    original = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        **device_info_for_resource(coordinator, "/gateway"),
    )
    devices.async_update_device(original.id, name_by_user="My heating system")
    saved = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatingCircuits:hc1:roomtemperature",
        config_entry=entry,
        device_id=original.id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    saved = entities.async_update_entity(
        saved.entity_id, name="My room", hidden_by=er.RegistryEntryHider.USER
    )
    saved = entities.async_update_entity_options(
        saved.entity_id, "sensor", {"suggested_display_precision": 2}
    )

    paths = (
        "/heatingCircuits/hc1/roomtemperature",
        "/heatingCircuits/hc3/roomtemperature",
        "/dhwCircuits/dhw1/actualTemp",
        "/solarCircuits/sc1/solarYield",
        "/heatSources/hs1/workingTime",
        "/devices/device2/productName",
        "/futureGroup/new1/value",
    )
    for _ in range(2):
        coordinator.resources = {path: Resource(path) for path in paths}
        for path in reversed(paths):
            registered = devices.async_get_or_create(
                config_entry_id=entry.entry_id,
                **device_info_for_resource(coordinator, path),
            )
            assert registered.id == original.id
            assert registered.name_by_user == "My heating system"
        restored = entities.async_get_or_create(
            "sensor",
            DOMAIN,
            saved.unique_id,
            config_entry=entry,
            device_id=registered.id,
            original_name="Heating circuit 1 room temperature",
        )
        assert restored.entity_id == saved.entity_id
        assert restored.device_id == saved.device_id
        assert restored.name == saved.name
        assert restored.disabled_by == saved.disabled_by
        assert restored.hidden_by == saved.hidden_by
        assert restored.options == saved.options

    assert len(dr.async_entries_for_config_entry(devices, entry.entry_id)) == 1
    assert len(er.async_entries_for_config_entry(entities, entry.entry_id)) == 1
    assert not coordinator.client.mock_calls


def test_system_info_distinguishes_controller_from_gateway(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(
        hass,
        brand="buderus",
        gateway=Gateway("gateway-one", device_type="K40"),
        resources=(
            Resource("/gateway/versionHardware", "K40RF_v1", True),
            Resource(
                "/system/info",
                values=(
                    {
                        "ProductName": "Logatherm WLW176i-12 TP70",
                        "ProductTtn": "heat-pump-type",
                    },
                    {"ModuleHwIdentStr": "HMI_module_02_800-20"},
                    {
                        "ModuleHwIdentStr": "MX400",
                        "ModuleTtn": "controller-type",
                    },
                    {"ModuleHwIdentStr": "MX400"},
                ),
            ),
        ),
    )

    info = device_info_for_resource(coordinator, "/gateway")

    assert info["model"] == "MX400"
    assert info["name"] == "MX400"
    assert info["model_id"] == "controller-type"
    assert info["hw_version"] == "K40RF_v1"


def test_ambiguous_controller_info_does_not_guess(hass: HomeAssistant) -> None:
    coordinator = _coordinator(
        hass,
        brand="bosch",
        gateway=Gateway("gateway-one", device_type="K40"),
        resources=(
            Resource(
                "/system/info",
                values=(
                    {"ModuleHwIdentStr": "MX300"},
                    {"ModuleHwIdentStr": "MX400"},
                ),
            ),
        ),
    )

    info = device_info_for_resource(coordinator, "/gateway")

    assert info["model"] == "Heating system"
    assert info["name"] == "Heating"


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
    assert info["name"] == "MX300"


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
    assert info["model"] == "Heating system"
    assert info["name"] == "Heating"
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
    assert info["model"] == "Heating system"
    assert info["name"] == "Heating"
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


@pytest.mark.parametrize(
    ("path", "expected"),
    (
        (
            "/solarCircuits/sc2/solarYield",
            "Solar circuit 2 \N{EN DASH} Solar yield",
        ),
        (
            "/ventilation/zone1/operationMode",
            "Ventilation zone 1 \N{EN DASH} Operation mode",
        ),
        (
            "/zones/zone3/currentRoomSetpoint",
            "Zone 3 \N{EN DASH} Target temperature",
        ),
        (
            "/devices/device7/battery",
            "Room device 7 \N{EN DASH} Battery level",
        ),
        ("/pool/currentTemp", "Pool \N{EN DASH} Current temperature"),
        (
            "/pv/surplusAvailable",
            "Photovoltaics \N{EN DASH} PV surplus available",
        ),
    ),
)
def test_optional_families_receive_clear_groups(
    hass: HomeAssistant, path: str, expected: str
) -> None:
    coordinator = _coordinator(hass, brand="buderus", resources=(Resource(path),))

    assert (
        grouped_entity_name(coordinator, path, resource_name(path, language="en"))
        == expected
    )


@pytest.mark.parametrize(
    ("product", "module", "expected"),
    [
        ("\xff\xff\xff\xff", None, None),
        ("\ufffd\ufffd\ufffd", None, None),
        ("\x00\x00\x00", None, None),
        (" \x07\x1b ", None, None),
        ("SN-42\x00\xff\xff", None, "SN-42"),
        ("\x00 SN 7 \ufffd", None, "SN 7"),
        ("\xff\xff", "M-9\x00", "M-9"),  # filler only: the module's serial
    ],
)
def test_serial_numbers_without_control_or_filler_characters(
    hass: HomeAssistant, product, module, expected
) -> None:
    row = {"ProductSerialNumber": product}
    if module is not None:
        row["ModuleSerialNumber"] = module
    coordinator = _coordinator(
        hass,
        resources=(
            Resource("/system/brand", "Buderus", True),
            Resource("/system/info", values=(row,)),
        ),
    )
    info = device_info_for_resource(coordinator, "/system/brand")
    assert info.get("serial_number") == expected


def test_a_filler_gateway_serial_falls_back_to_the_system_info(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(
        hass,
        resources=(
            Resource("/system/brand", "Buderus", True),
            Resource("/gateway/serialId", "\xff\xff\x00", True),
            Resource("/system/info", values=({"ProductSerialNumber": "P-1"},)),
        ),
    )
    assert (
        device_info_for_resource(coordinator, "/system/brand")["serial_number"] == "P-1"
    )
