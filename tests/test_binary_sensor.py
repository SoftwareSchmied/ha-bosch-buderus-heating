"""Tests for dynamically generated PointT binary sensors."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.binary_sensor import (
    BoschBuderusBinarySensor,
    BoschBuderusHolidayActiveBinarySensor,
    BoschBuderusSystemFaultBinarySensor,
    async_setup_entry,
    build_binary_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.holidays import (
    HOLIDAY_ACTIVE_MODES_PATH,
    HOLIDAY_LIST_PATH,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
)


def _binary_sensor(
    hass: HomeAssistant,
    resource: Resource,
    *,
    value_key: str | None = None,
    available: bool = True,
) -> BoschBuderusBinarySensor:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass,
        AsyncMock(),
        Gateway("gateway-one", device_type="MX300"),
        entry,
    )
    coordinator.resources = {resource.path: resource}
    coordinator.data = {
        resource.path: ResourceSnapshot(resource, available, datetime.now(UTC))
    }
    coordinator.last_update_success = True
    description = next(
        item
        for item in build_binary_sensor_descriptions(coordinator.resources)
        if item.value_key == value_key
    )
    return BoschBuderusBinarySensor(coordinator, description)


def test_boolean_resource_is_binary_sensor_not_regular_sensor(
    hass: HomeAssistant,
) -> None:
    resource = Resource(
        path="/system/awayMode/enabled",
        value=False,
        has_value=True,
        metadata=ResourceMetadata(resource_type="booleanValue", writable=True),
    )
    sensor = _binary_sensor(hass, resource)

    assert sensor.available
    assert sensor.is_on is False
    assert not sensor.entity_description.entity_registry_enabled_default
    assert sensor.unique_id == "gateway-one:system:awayMode:enabled"
    assert sensor.device_info["model"] == "MX300"


def test_nested_booleans_are_split_and_follow_logical_device(
    hass: HomeAssistant,
) -> None:
    resource = Resource(
        path="/heatSources/actualHeatDemand",
        value={"active": True, "details": {"blocked": False}, "level": 2},
        has_value=True,
    )

    descriptions = build_binary_sensor_descriptions({resource.path: resource})

    assert {item.value_key for item in descriptions} == {
        "active",
        "details.blocked",
    }
    sensor = _binary_sensor(hass, resource, value_key="details.blocked")
    assert sensor.is_on is False
    assert sensor.entity_description.entity_registry_enabled_default
    assert sensor.device_info["identifiers"] == {(DOMAIN, "gateway-one")}
    assert (
        sensor.name == "Heat generator \N{EN DASH} Current heat demand Details blocked"
    )


def test_unknown_boolean_capability_remains_diagnostics_only() -> None:
    resource = Resource(
        path="/heatingCircuits/hc3/vendorStatus",
        value=True,
        has_value=True,
    )

    assert build_binary_sensor_descriptions({resource.path: resource}) == ()


async def test_platform_adds_safe_booleans_only(hass: HomeAssistant) -> None:
    safe = Resource(path="/dhwCircuits/dhw1/charge", value=True, has_value=True)
    private = Resource(path="/gateway/wifi/enabled", value=True, has_value=True)
    sensor = _binary_sensor(hass, safe)
    sensor.coordinator.resources = {safe.path: safe, private.path: private}
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinators=(sensor.coordinator,))
    )
    added: list[object] = []

    await async_setup_entry(hass, entry, added.extend)

    assert len(added) == 2
    assert isinstance(added[0], BoschBuderusSystemFaultBinarySensor)
    assert isinstance(added[1], BoschBuderusBinarySensor)
    assert added[1].entity_description.resource_path == safe.path


def test_unavailable_or_non_boolean_value_has_no_binary_state(
    hass: HomeAssistant,
) -> None:
    resource = Resource(
        path="/system/awayMode/enabled",
        value=None,
        metadata=ResourceMetadata(resource_type="booleanValue"),
    )
    sensor = _binary_sensor(hass, resource, available=False)

    assert not sensor.available
    assert sensor.is_on is None


def test_system_fault_binary_sensor_uses_fault_tracker(
    hass: HomeAssistant,
) -> None:
    resource = Resource(path="/notifications", values=({"ccd": 6249, "fc": "12"},))
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type="K40"), entry
    )
    coordinator.last_update_success = True
    coordinator.faults.process_resources({resource.path: resource})
    sensor = BoschBuderusSystemFaultBinarySensor(coordinator)

    assert sensor.available
    assert sensor.is_on
    assert sensor.extra_state_attributes["codes"] == ["6249"]
    assert sensor.extra_state_attributes["active_fault_count"] == 1
    assert sensor.device_info["model"] == "K40"


async def test_platform_adds_read_only_holiday_status(hass: HomeAssistant) -> None:
    period = Resource(
        path=HOLIDAY_LIST_PATH,
        value={"start": "2026-08-01", "end": "2026-08-31"},
        has_value=True,
    )
    active = Resource(
        path=HOLIDAY_ACTIVE_MODES_PATH,
        values=({"mode": "eco"},),
    )
    coordinator = _binary_sensor(
        hass, Resource(path="/system/awayMode/enabled", value=False, has_value=True)
    ).coordinator
    coordinator.resources = {item.path: item for item in (period, active)}
    now = datetime.now(UTC)
    coordinator.data = {
        item.path: ResourceSnapshot(item, True, now) for item in (period, active)
    }
    entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinators=(coordinator,)))
    added: list[object] = []

    await async_setup_entry(hass, entry, added.extend)

    holiday = next(
        item
        for item in added
        if isinstance(item, BoschBuderusHolidayActiveBinarySensor)
    )
    assert holiday.is_on
    assert holiday.extra_state_attributes["period_count"] == 1
    assert holiday.unique_id == "gateway-one:holiday_active"
