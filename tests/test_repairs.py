"""Tests for Bosch/Buderus Heating repair flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import (
    CONF_POLLING_PROFILE,
    DOMAIN,
    FIRMWARE_ISSUE_PREFIX,
    RATE_LIMIT_ISSUE_PREFIX,
    PollingProfile,
)
from custom_components.bosch_buderus_heating.repairs import (
    FirmwareCompatibilityRepairFlow,
    InvalidRepairFlow,
    RateLimitRepairFlow,
    async_create_fix_flow,
)


async def test_rate_limit_repair_applies_cloud_friendly_profile(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, options={"keep": True})
    entry.add_to_hass(hass)
    issue_id = f"{RATE_LIMIT_ISSUE_PREFIX}{entry.entry_id}"
    flow = await async_create_fix_flow(hass, issue_id, {"entry_id": entry.entry_id})
    assert isinstance(flow, RateLimitRepairFlow)
    flow.hass = hass

    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ) as reload_entry:
        result = await flow.async_step_confirm({})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["keep"] is True
    assert entry.options[CONF_POLLING_PROFILE] == PollingProfile.CLOUD_FRIENDLY.value
    reload_entry.assert_awaited_once_with(entry.entry_id)


async def test_repair_aborts_for_missing_or_invalid_entry(
    hass: HomeAssistant,
) -> None:
    flow = await async_create_fix_flow(hass, "unknown", None)
    assert isinstance(flow, InvalidRepairFlow)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"


async def test_firmware_repair_reloads_for_fresh_capability_discovery(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    issue_id = f"{FIRMWARE_ISSUE_PREFIX}{entry.entry_id}"
    flow = await async_create_fix_flow(hass, issue_id, {"entry_id": entry.entry_id})
    assert isinstance(flow, FirmwareCompatibilityRepairFlow)
    flow.hass = hass

    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ) as reload_entry:
        result = await flow.async_step_confirm({})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    reload_entry.assert_awaited_once_with(entry.entry_id)

    flow = RateLimitRepairFlow("missing", "issue")
    flow.hass = hass
    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"
