"""Tests for bounded numeric controls."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.number import (
    BoschBuderusNumber,
    async_setup_entry,
    build_number_descriptions,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
)

PATH = "/heatingCircuits/hc1/manualRoomSetpoint"


def _resource(
    path: str = PATH,
    value: float = 20.0,
    *,
    minimum: float = 5.0,
    maximum: float = 30.0,
    unit: str = "C",
    writable: bool = True,
) -> Resource:
    return Resource(
        path=path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue",
            unit=unit,
            minimum=minimum,
            maximum=maximum,
            writable=writable,
        ),
    )


def _number(
    hass: HomeAssistant, resource: Resource | None = None
) -> BoschBuderusNumber:
    current = resource or _resource()
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type="K40"), entry
    )
    coordinator.resources = {current.path: current}
    coordinator.data = {
        current.path: ResourceSnapshot(current, True, datetime.now(UTC))
    }
    coordinator.last_update_success = True
    return BoschBuderusNumber(
        coordinator, build_number_descriptions(coordinator.resources)[0]
    )


def test_number_uses_live_limits_and_identity(hass: HomeAssistant) -> None:
    entity = _number(hass)

    assert entity.available
    assert entity.native_value == 20.0
    assert entity.native_min_value == 5.0
    assert entity.native_max_value == 30.0
    assert entity.native_step == 0.5
    assert entity.entity_description.entity_registry_enabled_default
    assert entity.name == "Heating circuit 1 \u2013 Manual setpoint"
    assert entity.unique_id == (
        "gateway-one:heatingCircuits:hc1:manualRoomSetpoint:control"
    )
    assert entity.device_info["identifiers"] == {(DOMAIN, "gateway-one")}


def test_all_released_numeric_controls_are_discovered() -> None:
    resources = {
        resource.path: resource
        for resource in (
            _resource(),
            _resource(
                "/heatingCircuits/hc1/maxFlowTemp",
                40.0,
                minimum=30.0,
                maximum=60.0,
            ),
            _resource("/heatingCircuits/hc1/temperatureLevels/comfort2", 21.0),
            _resource("/heatingCircuits/hc1/temperatureLevels/eco", 18.0),
            _resource(
                "/dhwCircuits/dhw1/chargeDuration",
                60,
                minimum=60,
                maximum=2880,
                unit="mins",
            ),
            _resource(
                "/dhwCircuits/dhw1/singleChargeSetpoint",
                60,
                minimum=50,
                maximum=70,
            ),
            _resource(
                "/dhwCircuits/dhw1/temperatureLevels/eco",
                42,
                minimum=30,
                maximum=45,
            ),
            _resource(
                "/dhwCircuits/dhw1/temperatureLevels/high",
                50,
                minimum=40,
                maximum=55,
            ),
            _resource(
                "/dhwCircuits/dhw1/temperatureLevels/low",
                45,
                minimum=35,
                maximum=51,
            ),
        )
    }

    descriptions = build_number_descriptions(resources)

    assert len(descriptions) == 9
    assert {item.translation_key for item in descriptions} == {
        "manual_room_setpoint",
        "heating_temperature",
        "reduced_temperature",
        "extra_hot_water_duration",
        "extra_hot_water_setpoint",
        "hot_water_eco_plus",
        "hot_water_comfort",
        "hot_water_eco",
        "maximum_supply_temperature",
    }
    assert {
        item.name
        for item in descriptions
        if item.resource_path.startswith("/dhwCircuits/")
        and "/temperatureLevels/" in item.resource_path
    } == {
        "Eco+ Starttemperatur",
        "Komfort Starttemperatur",
        "Eco Starttemperatur",
    }
    assert all(
        item.native_step == 1.0
        for item in descriptions
        if item.resource_path.startswith("/dhwCircuits/")
        and not item.resource_path.endswith("/chargeDuration")
    )
    assert all(
        item.native_step == 0.5
        for item in descriptions
        if item.resource_path.startswith("/heatingCircuits/")
        and not item.resource_path.endswith("/maxFlowTemp")
    )
    maximum_supply = next(
        item for item in descriptions if item.resource_path.endswith("/maxFlowTemp")
    )
    assert maximum_supply.native_min_value == 30.0
    assert maximum_supply.native_max_value == 60.0
    assert maximum_supply.native_step == 1.0
    assert maximum_supply.translation_key == "maximum_supply_temperature"
    assert not maximum_supply.entity_registry_enabled_default


def test_unsafe_numeric_metadata_is_not_exposed() -> None:
    invalid = (
        _resource(writable=False),
        _resource(unit="bar"),
        _resource(minimum=0),
        _resource(maximum=35),
        _resource(
            "/heatingCircuits/hc1/maxFlowTemp",
            minimum=-1,
            maximum=60,
        ),
        _resource(
            "/heatingCircuits/hc1/maxFlowTemp",
            minimum=30,
            maximum=101,
        ),
    )

    assert all(build_number_descriptions({item.path: item}) == () for item in invalid)


def test_maximum_supply_temperature_uses_each_gateways_live_range() -> None:
    resource = _resource(
        "/heatingCircuits/hc7/maxFlowTemp",
        55.0,
        minimum=20.0,
        maximum=80.0,
    )

    description = build_number_descriptions({resource.path: resource})[0]

    assert description.native_min_value == 20.0
    assert description.native_max_value == 80.0
    assert description.native_step == 1.0


def test_number_becomes_unavailable_when_stale(hass: HomeAssistant) -> None:
    entity = _number(hass)
    current = entity.coordinator.data[PATH]
    entity.coordinator.data[PATH] = ResourceSnapshot(
        current.resource,
        True,
        current.last_success,
        freshness=Freshness.STALE,
    )

    assert not entity.available


async def test_number_calls_confirmed_coordinator_write(hass: HomeAssistant) -> None:
    entity = _number(hass)
    writer = AsyncMock()
    entity.coordinator.async_write_control = writer

    await entity.async_set_native_value(20.5)

    writer.assert_awaited_once_with(PATH, 20.5, entity.entity_description.write_policy)


async def test_maximum_supply_temperature_uses_confirmed_write(
    hass: HomeAssistant,
) -> None:
    resource = _resource(
        "/heatingCircuits/hc1/maxFlowTemp",
        40.0,
        minimum=30.0,
        maximum=60.0,
    )
    entity = _number(hass, resource)
    writer = AsyncMock()
    entity.coordinator.async_write_control = writer

    assert entity.native_value == 40.0
    assert entity.native_step == 1.0
    assert entity.name == "Heating circuit 1 \N{EN DASH} Maximum supply temperature"
    assert not entity.entity_description.entity_registry_enabled_default

    await entity.async_set_native_value(41.0)

    writer.assert_awaited_once_with(
        resource.path,
        41.0,
        entity.entity_description.write_policy,
    )


async def test_platform_adds_numeric_controls(hass: HomeAssistant) -> None:
    entity = _number(hass)
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinators=(entity.coordinator,))
    )
    added: list[BoschBuderusNumber] = []

    await async_setup_entry(hass, entry, added.extend)

    assert len(added) == 1
