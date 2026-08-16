"""Tests for conservatively released heating controls."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    Gateway,
    PointTError,
    RateLimited,
    Resource,
    ResourceMetadata,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.select import (
    BoschBuderusOperationModeSelect,
    async_setup_entry,
    build_select_descriptions,
)
from custom_components.bosch_buderus_heating.writes import (
    HEATING_CIRCUIT_OPERATION_MODE_POLICY,
)

PATH = "/heatingCircuits/hc1/operationMode"


def _resource(**changes: object) -> Resource:
    values = {
        "path": PATH,
        "value": "manual",
        "has_value": True,
        "metadata": ResourceMetadata(
            resource_type="stringValue",
            allowed_values=("off", "manual", "auto"),
            writable=True,
        ),
    }
    values.update(changes)
    return Resource(**values)  # type: ignore[arg-type]


def _select(
    hass: HomeAssistant, resource: Resource | None = None
) -> BoschBuderusOperationModeSelect:
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
    description = build_select_descriptions(coordinator.resources)[0]
    return BoschBuderusOperationModeSelect(coordinator, description)


def test_control_shape_and_identity(hass: HomeAssistant) -> None:
    entity = _select(hass)

    assert entity.available
    assert entity.current_option == "manual"
    assert entity.options == ["off", "manual", "auto"]
    assert entity.name == "Heating circuit 1 \u2013 Operation mode"
    assert entity.unique_id == ("gateway-one:heatingCircuits:hc1:operationMode:control")
    assert entity.device_info["identifiers"] == {(DOMAIN, "gateway-one")}
    assert entity.entity_description.entity_registry_enabled_default


def test_control_requires_exact_current_capability_shape() -> None:
    invalid = (
        _resource(path="/system/operationMode"),
        _resource(metadata=ResourceMetadata(resource_type="stringValue")),
        _resource(
            metadata=ResourceMetadata(
                resource_type="floatValue",
                allowed_values=("off", "manual", "auto"),
                writable=True,
            )
        ),
        _resource(value="holiday"),
        _resource(
            metadata=ResourceMetadata(
                resource_type="stringValue",
                allowed_values=("manual", "auto"),
                writable=True,
            )
        ),
    )

    assert all(build_select_descriptions({item.path: item}) == () for item in invalid)


def test_hot_water_operation_mode_is_dynamic() -> None:
    path = "/dhwCircuits/dhw1/operationMode"
    resource = Resource(
        path=path,
        value="eco",
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue",
            allowed_values=("Off", "low", "high", "ownprogram", "eco"),
            writable=True,
        ),
    )

    description = build_select_descriptions({path: resource})[0]

    assert description.translation_key == "hot_water_operation_mode"
    assert description.options == ["off", "low", "high", "ownprogram", "eco"]


async def test_hot_water_off_uses_ha_safe_option_and_raw_pointt_write(
    hass: HomeAssistant,
) -> None:
    path = "/dhwCircuits/dhw1/operationMode"
    resource = Resource(
        path=path,
        value="Off",
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue",
            allowed_values=("Off", "low", "high", "ownprogram", "eco"),
            writable=True,
        ),
    )
    entity = _select(hass, resource)
    writer = AsyncMock()
    entity.coordinator.async_write_control = writer

    assert entity.current_option == "off"

    await entity.async_select_option("off")

    writer.assert_awaited_once_with(path, "Off", entity.entity_description.write_policy)


def test_control_becomes_unavailable_for_stale_resource(hass: HomeAssistant) -> None:
    entity = _select(hass)
    snapshot = entity.coordinator.data[PATH]
    entity.coordinator.data[PATH] = ResourceSnapshot(
        snapshot.resource,
        True,
        snapshot.last_success,
        freshness=Freshness.STALE,
    )

    assert not entity.available


async def test_select_calls_confirmed_coordinator_write(hass: HomeAssistant) -> None:
    entity = _select(hass)
    writer = AsyncMock()
    entity.coordinator.async_write_control = writer

    await entity.async_select_option("auto")

    writer.assert_awaited_once_with(PATH, "auto", HEATING_CIRCUIT_OPERATION_MODE_POLICY)


@pytest.mark.parametrize(
    ("error", "expected_type", "translation_key"),
    [
        (
            WriteValidationError(),
            ServiceValidationError,
            "write_validation_failed",
        ),
        (WriteNotConfirmed(), HomeAssistantError, "write_not_confirmed"),
        (
            AuthenticationError(),
            HomeAssistantError,
            "write_authentication_failed",
        ),
        (RateLimited(None), HomeAssistantError, "write_rate_limited"),
        (PointTError(), HomeAssistantError, "write_failed"),
    ],
)
async def test_select_translates_write_failures(
    hass: HomeAssistant,
    error: Exception,
    expected_type: type[Exception],
    translation_key: str,
) -> None:
    entity = _select(hass)
    entity.coordinator.async_write_control = AsyncMock(side_effect=error)

    with pytest.raises(expected_type) as caught:
        await entity.async_select_option("auto")
    assert caught.value.translation_key == translation_key


async def test_platform_adds_dynamic_controls(hass: HomeAssistant) -> None:
    entity = _select(hass)
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinators=(entity.coordinator,))
    )
    added: list[BoschBuderusOperationModeSelect] = []

    await async_setup_entry(hass, entry, added.extend)

    assert len(added) == 1
