"""Tests for Bosch/Buderus Home Comfort repair flows."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

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
from tests.ha_test_helpers import MockConfigEntry


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
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await flow.async_step_confirm({})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["keep"] is True
    assert entry.options[CONF_POLLING_PROFILE] == PollingProfile.CLOUD_FRIENDLY.value
    reload_entry.assert_called_once_with(entry.entry_id)


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
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await flow.async_step_confirm({})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    reload_entry.assert_called_once_with(entry.entry_id)

    flow = RateLimitRepairFlow("missing", "issue")
    flow.hass = hass
    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"


@pytest.mark.parametrize(
    "prefix,translation_key",
    [
        (RATE_LIMIT_ISSUE_PREFIX, "repeated_rate_limit"),
        (FIRMWARE_ISSUE_PREFIX, "incompatible_firmware"),
    ],
)
async def test_a_repair_opened_in_home_assistant_asks_before_it_acts(
    hass: HomeAssistant,
    enable_custom_integrations,
    hass_client,
    prefix: str,
    translation_key: str,
) -> None:
    # Started like the repair dialog does; Home Assistant hands the issue
    # to the first step as input.
    entry = MockConfigEntry(domain=DOMAIN, options={"keep": True})
    entry.add_to_hass(hass)
    # Without a setup, mark the integration loaded so its repairs are found.
    hass.config.components.add(DOMAIN)
    assert await async_setup_component(hass, "repairs", {})
    issue_id = f"{prefix}{entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=translation_key,
    )
    client = await hass_client()
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        response = await client.post(
            "/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": issue_id}
        )
        assert response.status == 200
        form = await response.json()
        assert form["type"] == "form" and form["step_id"] == "confirm"
        # Nothing has changed before the confirmation.
        reload_entry.assert_not_called()
        assert CONF_POLLING_PROFILE not in entry.options
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

        response = await client.post(f"/api/repairs/issues/fix/{form['flow_id']}")
        assert response.status == 200
        assert (await response.json())["type"] == "create_entry"
    reload_entry.assert_called_once_with(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
