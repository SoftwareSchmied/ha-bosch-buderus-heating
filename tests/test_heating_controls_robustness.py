"""Failure and recovery checks across dialog, climate and real write service."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.coordinator import (
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    RateLimited,
    RequestTimeout,
    ResourceReference,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.writes import WriteService
from tests.test_heating_controls import MODE, TARGET, controls, scalar, setup

STATUS = "/system/globalSeasonOptimizer/currentMode"


@pytest.mark.parametrize("value", ["off", "heating", "automatic", "forcedHeat"])
def test_recognized_global_heating_context(hass, value):
    _, coordinator, entity = setup(hass)
    resource = scalar(STATUS, value, writable=False)
    coordinator.data[STATUS] = ResourceSnapshot(resource, True, datetime.now(UTC))
    assert entity.available


@pytest.mark.parametrize("failure", ["stale", "type", "unit", "references", "unknown"])
def test_invalid_global_heating_context_blocks_controls(hass, failure):
    _, coordinator, entity = setup(hass)
    resource = scalar(STATUS, "heating", writable=False)
    if failure == "type":
        resource = replace(
            resource, metadata=replace(resource.metadata, resource_type="floatValue")
        )
    elif failure == "unit":
        resource = replace(resource, metadata=replace(resource.metadata, unit="C"))
    elif failure == "references":
        resource = replace(resource, references=(ResourceReference("/system"),))
    elif failure == "unknown":
        resource = replace(resource, value="future")
    coordinator.data[STATUS] = ResourceSnapshot(
        resource,
        True,
        datetime.now(UTC),
        freshness=Freshness.STALE if failure == "stale" else Freshness.FRESH,
    )
    assert not entity.available


async def test_global_cooling_started_since_last_poll_prevents_put(hass):
    _, coordinator, entity = setup(hass)
    resource = scalar(STATUS, "heating", writable=False)
    coordinator.data[STATUS] = ResourceSnapshot(resource, True, datetime.now(UTC))
    coordinator.client.get_resource.side_effect = lambda _gateway, path: (
        replace(resource, value="cooling")
        if path == STATUS
        else coordinator.data[path].resource
    )
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    coordinator._write_service.async_write_number.assert_not_awaited()
    assert not entity.available


@pytest.mark.parametrize(
    "error",
    [RequestTimeout(), ServiceUnavailable(), RateLimited(30), AuthenticationError()],
)
async def test_uncertain_put_never_leaves_old_target_fresh(
    hass, enable_custom_integrations, error
):
    _, coordinator, entity = setup(hass)
    before = coordinator.data[TARGET]
    coordinator._write_service = WriteService(
        coordinator.client, sleep=AsyncMock(), read_back_attempts=1
    )
    coordinator.client.put_resource_value.return_value = None
    coordinator.client.get_resource.side_effect = [
        coordinator.data[MODE].resource,
        before.resource,
        error,
    ]
    with pytest.raises(HomeAssistantError):
        await entity.async_set_temperature(temperature=22)
    coordinator.client.put_resource_value.assert_awaited_once_with(
        "gateway", TARGET, 22
    )
    assert coordinator.data[TARGET].freshness is Freshness.STALE
    assert not coordinator.data[TARGET].available
    assert coordinator.data[TARGET].last_success == before.last_success
    assert entity.native_target_temperature is None
    assert not entity.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE
    assert TARGET not in controls(coordinator)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=23)
    assert coordinator.client.put_resource_value.await_count == 1


async def test_cancelled_put_invalidates_only_affected_setting(hass):
    _, coordinator, entity = setup(hass)
    coordinator._write_service.async_write_number.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await entity.async_set_temperature(temperature=22)
    assert TARGET not in controls(coordinator)
    assert MODE in controls(coordinator)


async def test_failed_guard_read_marks_the_dependency_stale(hass):
    _, coordinator, entity = setup(hass)
    coordinator.client.get_resource.side_effect = RequestTimeout()
    with pytest.raises(HomeAssistantError):
        await entity.async_set_temperature(temperature=22)
    coordinator._write_service.async_write_number.assert_not_awaited()
    assert not entity.available
    assert coordinator.data[MODE].freshness is Freshness.STALE
    assert coordinator.data[TARGET].freshness is Freshness.FRESH
