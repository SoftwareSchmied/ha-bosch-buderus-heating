"""Tests for bounded discovery, privacy, polling, and naming rules."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating.discovery import (
    ROOT_RESOURCE_PATHS,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.holidays import HOLIDAY_RESOURCE_PATHS
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Resource,
    ResourceForbidden,
    ResourceMetadata,
    ResourceNotFound,
    ResourceReference,
)
from custom_components.bosch_buderus_heating.resource_catalog import (
    CapabilityMaturity,
    PollGroup,
    capability_maturity,
    configured_device_name,
    entity_enabled_by_default,
    is_opt_in_diagnostic_resource,
    is_private_resource,
    logical_device_for_path,
    poll_group,
    resource_name,
    supports_entity,
)


async def test_discovery_follows_references_once_and_stays_in_roots() -> None:
    client = AsyncMock()
    resources = {
        "/heatingCircuits": Resource(
            path="/heatingCircuits",
            references=(
                ResourceReference("/heatingCircuits/hc1"),
                ResourceReference("/outside"),
            ),
        ),
        "/heatingCircuits/hc1": Resource(
            path="/heatingCircuits/hc1",
            references=(
                ResourceReference("/heatingCircuits/hc1/operationMode"),
                ResourceReference("/heatingCircuits"),
            ),
        ),
        "/heatingCircuits/hc1/operationMode": Resource(
            path="/heatingCircuits/hc1/operationMode", value="auto", has_value=True
        ),
    }
    client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
        BatchItemResult(
            gateway_id=gateway,
            path=path,
            status=200,
            resource=resources[path],
        )
        for path in paths
    )

    result = await async_discover_resources(
        client, "gateway", roots=("/heatingCircuits",)
    )

    assert set(result) == set(resources)
    assert client.get_resources_bulk.await_count == 3


async def test_discovery_skips_missing_optional_nodes_and_validates_bounds() -> None:
    client = AsyncMock()
    client.get_resources_bulk.return_value = (
        BatchItemResult(
            gateway_id="gateway",
            path="/system",
            status=404,
            error=ResourceNotFound("/system", 404),
        ),
    )
    assert await async_discover_resources(client, "gateway", roots=("/system",)) == {}
    with pytest.raises(ValueError):
        await async_discover_resources(client, "gateway", maximum_resources=0)


def test_discovery_includes_exact_optional_holiday_paths() -> None:
    assert set(HOLIDAY_RESOURCE_PATHS).issubset(ROOT_RESOURCE_PATHS)


async def test_discovery_expands_known_opaque_energy_container() -> None:
    client = AsyncMock()
    root = Resource(
        path="/heatSources",
        references=(ResourceReference("/heatSources/emon"),),
    )
    energy_paths = (
        "/heatSources/emon/totalConsumption",
        "/heatSources/emon/chConsumption",
        "/heatSources/emon/dhwConsumption",
        "/heatSources/emon/coolingConsumption",
    )

    def response(gateway: str, paths: tuple[str, ...]):
        results = []
        for path in paths:
            if path == root.path:
                results.append(BatchItemResult(gateway, path, 200, root))
            elif path == "/heatSources/emon":
                results.append(
                    BatchItemResult(
                        gateway,
                        path,
                        403,
                        error=ResourceForbidden(path, 403),
                    )
                )
            else:
                results.append(
                    BatchItemResult(
                        gateway,
                        path,
                        200,
                        Resource(path=path, values=({"compressor": 1.0},)),
                    )
                )
        return tuple(results)

    client.get_resources_bulk.side_effect = response

    resources = await async_discover_resources(
        client, "gateway", roots=("/heatSources",)
    )

    assert set(energy_paths).issubset(resources)


async def test_discovery_probes_known_unadvertised_status_resources() -> None:
    client = AsyncMock()
    roots = (
        Resource(path="/gateway"),
        Resource(path="/system"),
        Resource(path="/heatSources"),
    )
    expected = {
        "/gateway/dataProcessing/status",
        "/system/globalSeasonOptimizer/currentMode",
        "/system/iSRC/supportStatus",
        "/heatSources/chStatus",
        "/heatSources/compressor/status",
        "/heatSources/Source/eHeater/status",
    }

    def response(gateway: str, paths: tuple[str, ...]):
        return tuple(
            BatchItemResult(
                gateway,
                path,
                200,
                next((item for item in roots if item.path == path), None)
                or Resource(path=path, value="off", has_value=True),
            )
            for path in paths
        )

    client.get_resources_bulk.side_effect = response

    resources = await async_discover_resources(
        client,
        "gateway",
        roots=tuple(item.path for item in roots),
    )

    assert expected.issubset(resources)


def test_catalog_privacy_polling_devices_and_names() -> None:
    assert is_private_resource("/gateway/wifi/mac")
    assert is_opt_in_diagnostic_resource("/gateway/serialId")
    assert is_opt_in_diagnostic_resource("/gateway/uuid")
    assert is_opt_in_diagnostic_resource("/system/country")
    assert is_opt_in_diagnostic_resource("/system/info")
    assert not is_opt_in_diagnostic_resource("/gateway/wifi/mac")
    assert not supports_entity(
        Resource(path="/gateway/serialId", value="secret", has_value=True)
    )
    writable = Resource(
        path="/heatingCircuits/hc2/operationMode",
        metadata=ResourceMetadata(writable=True),
    )
    assert poll_group(writable) is PollGroup.CONTROL
    logical = logical_device_for_path(writable.path)
    assert logical is not None and logical.logical_id == "hc2"
    assert resource_name(writable.path) == "Betriebsart"
    assert resource_name(writable.path, language="en") == "Operation mode"
    assert resource_name("/heatingCircuits/hc2/currentRoomSetpoint") == (
        "Wunschtemperatur"
    )
    assert resource_name("/heatSources/actualModulation") == "Aktuelle Modulation"
    assert resource_name("/heatSources/systemPressure") == "Systemdruck"
    assert resource_name("/heatSources/compressor/status") == "Status Kompressor"
    assert (
        resource_name("/heatSources/Source/eHeater/status", language="en")
        == "Auxiliary heater status"
    )
    assert resource_name("/system/type", language="en") == "System type"
    assert (
        resource_name(
            "/system/sensors/temperatures/outdoorTemperatureSource", language="en"
        )
        == "Outdoor temperature source"
    )
    assert (
        resource_name("/system/variableTariff/supportStatus", language="en")
        == "Support status"
    )
    assert resource_name("/heatingCircuits/hc1/temperatureLevels/comfort2") == "Heizen"
    assert resource_name("/heatingCircuits/hc1/temperatureLevels/eco") == "Absenken"
    assert resource_name("/dhwCircuits/dhw1/temperatureLevels/high") == (
        "Komfort Starttemperatur"
    )
    assert resource_name("/dhwCircuits/dhw1/temperatureLevels/low") == (
        "Eco Starttemperatur"
    )
    assert resource_name("/dhwCircuits/dhw1/temperatureLevels/eco") == (
        "Eco+ Starttemperatur"
    )
    assert resource_name("/dhwCircuits/dhw1/temperatureLevels/off") == "Aus"
    assert (
        resource_name("/heatingCircuits/hc1/switchPrograms/nameA")
        == "Name des Zeitprogramms A"
    )
    assert (
        resource_name("/gateway/update/status", "progress.cur_step")
        == "Softwareupdate Aktueller Schritt"
    )
    assert resource_name("/heatSources/hs1/workingTime", "total") == (
        "Betriebszeit Gesamt"
    )
    assert (
        resource_name("/heatSources/emon/coolingConsumption", "outputProduced")
        == "Kühlung \N{EN DASH} Erzeugte Kühlenergie"
    )
    assert (
        resource_name("/heatSources/emon/totalConsumption", "electricity")
        == "Gesamt \N{EN DASH} Stromverbrauch"
    )
    assert (
        resource_name(
            "/heatSources/emon/totalConsumption",
            "environmental_energy",
        )
        == "Gesamt \N{EN DASH} Umweltenergie (berechnet)"
    )
    assert (
        resource_name(
            "/heatSources/emon/dhwConsumption",
            "compressor",
            language="en",
        )
        == "Hot water \N{EN DASH} Heat pump electricity consumption"
    )
    assert (
        poll_group(
            Resource(
                path="/heatSources/emon/totalConsumption",
                metadata=ResourceMetadata(resource_type="emonValue"),
            )
        )
        is PollGroup.ENERGY
    )
    assert (
        poll_group(Resource(path="/heatSources/systemPressureRange"))
        is PollGroup.STATIC
    )
    assert (
        poll_group(Resource(path="/gateway/dataProcessing/status")) is PollGroup.STATIC
    )
    assert poll_group(Resource(path="/heatSources/hs1/failurelist")) is PollGroup.STATIC
    assert poll_group(Resource(path="/holidayMode/list")) is PollGroup.CONTROL
    assert not supports_entity(
        Resource(path="/holidayMode/list", value=[], has_value=True)
    )
    assert resource_name("/holidayMode/list", language="en") == "Holiday periods"
    assert not supports_entity(
        Resource(path="/heatSources/numberOfStarts", value=13, has_value=True)
    )


def test_capability_maturity_controls_entity_publication() -> None:
    assert (
        capability_maturity("/heatSources/actualSupplyTemperature")
        is CapabilityMaturity.VERIFIED
    )
    assert (
        capability_maturity("/heatingCircuits/hc7/operationMode")
        is CapabilityMaturity.WRITE_VERIFIED
    )
    assert (
        capability_maturity("/heatSources/hs2/workingTime")
        is CapabilityMaturity.UNDERSTOOD
    )
    assert (
        capability_maturity("/heatingCircuits/hc2/suWiSwitchMode")
        is CapabilityMaturity.UNDERSTOOD
    )
    assert (
        capability_maturity("/heatSources/vendorExtension")
        is CapabilityMaturity.OBSERVED
    )
    assert entity_enabled_by_default("/dhwCircuits/dhw2/actualTemp")
    assert entity_enabled_by_default("/heatSources/hs2/workingTime")
    assert entity_enabled_by_default("/heatSources/hs2/numberOfStarts")
    assert entity_enabled_by_default("/heatSources/hs2/supplyFlowCondenserTemp")
    assert entity_enabled_by_default("/heatSources/emon/coolingConsumption")
    assert entity_enabled_by_default("/heatSources/emStatus")
    assert entity_enabled_by_default("/heatSources/compressor/status")
    assert entity_enabled_by_default("/heatSources/Source/eHeater/status")
    assert not entity_enabled_by_default("/gateway/dataProcessing/status")
    assert not entity_enabled_by_default("/gateway/versionFirmware")
    assert not entity_enabled_by_default("/gateway/serialId")
    assert not entity_enabled_by_default("/system/info")
    assert not entity_enabled_by_default("/gateway/update/status")
    assert not entity_enabled_by_default("/heatingCircuits/hc2/manualRoomSetpoint")
    assert not entity_enabled_by_default("/dhwCircuits/dhw2/operationMode")
    assert not supports_entity(
        Resource(
            path="/heatSources/vendorExtension",
            value=1,
            has_value=True,
        )
    )


def test_configured_device_name_decodes_observed_pointt_format() -> None:
    assert configured_device_name("AFcAYQByAG0AdwBhAHMAcwBlAHI=") == "Warmwasser"
    assert configured_device_name("Obergeschoss") == "Obergeschoss"
    assert configured_device_name("  ") is None
    assert configured_device_name("bad\x00name") is None
