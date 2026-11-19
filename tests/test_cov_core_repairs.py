"""Repair flows: refused foreign or tampered issue data, flows whose entry or
state is gone, and exchange steps taken out of order."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating import repairs
from custom_components.bosch_buderus_heating.const import (
    DOMAIN,
    FIRMWARE_ISSUE_PREFIX,
    GATEWAY_EXCHANGE_ISSUE_PREFIX,
    LOCAL_START_ISSUE_PREFIX,
    RATE_LIMIT_ISSUE_PREFIX,
    PollingProfile,
)
from custom_components.bosch_buderus_heating.gateway_exchange import ModuleEvidence
from custom_components.bosch_buderus_heating.repair_scope import repair_id
from custom_components.bosch_buderus_heating.repairs import (
    FirmwareCompatibilityRepairFlow,
    GatewayExchangeRepairFlow,
    InvalidRepairFlow,
    ManagedRepairFlow,
    async_create_fix_flow,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_connection_runtime import entry_for

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _scoped(prefix, entry, connection, binding=None) -> str:
    return repair_id(prefix, entry, connection, binding)


async def _flow(hass, issue_id, data):
    flow = await async_create_fix_flow(hass, issue_id, data)
    flow.hass = hass
    return flow


# ------------------------------------------------- tampered issue data


async def test_exchange_data_with_a_wrong_id_or_type_is_refused(
    hass: HomeAssistant,
) -> None:
    entry, cloud, _ = entry_for(hass)
    issue_id = _scoped(GATEWAY_EXCHANGE_ISSUE_PREFIX, entry, cloud, "b")
    good = {
        "entry_id": entry.entry_id,
        "connection_id": cloud,
        "binding_id": "b",
        "new_gateway": "gw-new",
    }
    assert isinstance(
        await async_create_fix_flow(hass, issue_id, good), GatewayExchangeRepairFlow
    )
    for change in (
        {"new_gateway": 5},
        {"binding_id": None},
        {"connection_id": 3},
        {"binding_id": "other"},  # the id belongs to another binding
    ):
        flow = await async_create_fix_flow(hass, issue_id, {**good, **change})
        assert isinstance(flow, InvalidRepairFlow), change
    flow = await _flow(hass, issue_id, {**good, "binding_id": "other"})
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"


async def test_managed_repairs_refuse_data_that_does_not_fit_the_issue(
    hass: HomeAssistant,
) -> None:
    entry, cloud, _ = entry_for(hass)
    base = {"entry_id": entry.entry_id, "connection_id": cloud}
    firmware = _scoped(FIRMWARE_ISSUE_PREFIX, entry, cloud, "b")
    rate = _scoped(RATE_LIMIT_ISSUE_PREFIX, entry, cloud)
    assert isinstance(await async_create_fix_flow(hass, rate, base), ManagedRepairFlow)
    assert isinstance(
        await async_create_fix_flow(hass, firmware, {**base, "binding_id": "b"}),
        ManagedRepairFlow,
    )
    cases = [
        # A firmware note is always per binding.
        (firmware, base),
        # A rate limit note is per connection, never per binding.
        (rate, {**base, "binding_id": "b"}),
        # The issue id does not belong to the data.
        (rate, {**base, "connection_id": "elsewhere"}),
        (firmware, {**base, "binding_id": "other"}),
        (rate, {"entry_id": entry.entry_id}),
        (rate, {**base, "connection_id": 4}),
    ]
    for issue_id, data in cases:
        flow = await async_create_fix_flow(hass, issue_id, data)
        assert isinstance(flow, InvalidRepairFlow), (issue_id, data)


async def test_local_start_data_must_match_its_issue(hass: HomeAssistant) -> None:
    entry, cloud, _ = entry_for(hass)
    issue_id = _scoped(LOCAL_START_ISSUE_PREFIX, entry, cloud, "b")
    data = {
        "entry_id": entry.entry_id,
        "connection_id": cloud,
        "binding_id": "b",
        "problem": "certificate",
    }
    assert not isinstance(
        await async_create_fix_flow(hass, issue_id, data), InvalidRepairFlow
    )
    assert isinstance(
        await async_create_fix_flow(hass, issue_id, {**data, "problem": None}),
        InvalidRepairFlow,
    )
    assert isinstance(
        await async_create_fix_flow(hass, issue_id, {**data, "binding_id": "x"}),
        InvalidRepairFlow,
    )


async def test_an_unknown_issue_of_an_old_entry_is_refused(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    flow = await async_create_fix_flow(
        hass, "something_else", {"entry_id": entry.entry_id}
    )
    assert isinstance(flow, InvalidRepairFlow)
    # No entry id at all, or a missing entry for a known prefix.
    assert isinstance(
        await async_create_fix_flow(hass, "something_else", {}), InvalidRepairFlow
    )
    assert isinstance(
        await async_create_fix_flow(hass, "something_else", {"entry_id": 5}),
        InvalidRepairFlow,
    )


# ------------------------------------------------ flows that lost ground


async def test_the_managed_repair_aborts_when_its_entry_is_gone(
    hass: HomeAssistant,
) -> None:
    flow = ManagedRepairFlow("removed", "issue", "connection", None)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"


async def test_a_binding_repair_without_a_running_source_is_no_longer_valid(
    hass: HomeAssistant,
) -> None:
    entry, cloud, _ = entry_for(hass)
    binding = next(iter(entry.data["bindings"]))
    flow = ManagedRepairFlow(entry.entry_id, "issue", cloud, binding)
    flow.hass = hass
    # The entry is not running: nothing to restart.
    result = await flow.async_step_confirm()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "configuration_changed"


async def test_a_changed_configuration_stops_the_confirmation(
    hass: HomeAssistant,
) -> None:
    entry, cloud, _ = entry_for(hass)
    flow = ManagedRepairFlow(entry.entry_id, "issue", cloud, None)
    flow.hass = hass
    result = await flow.async_step_confirm()
    assert result["type"] is FlowResultType.FORM
    # Someone else changes the same connection meanwhile.
    management = repairs.ConfigurationManagement(hass, entry)
    management.set_polling_profile(
        management.edit(cloud), PollingProfile.CLOUD_FRIENDLY
    )
    with patch.object(repairs.ConfigurationManagement, "set_polling_profile") as save:
        result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "configuration_changed"
    save.assert_not_called()


async def test_the_firmware_repair_aborts_when_its_entry_is_gone(
    hass: HomeAssistant,
) -> None:
    flow = FirmwareCompatibilityRepairFlow("removed", "issue")
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"


# ------------------------------------------- exchange steps out of order


def _exchange_flow(hass, entry_id="owner") -> GatewayExchangeRepairFlow:
    flow = GatewayExchangeRepairFlow(entry_id, "issue", "connection", "b", "gw-new")
    flow.hass = hass
    return flow


async def test_the_exchange_aborts_when_its_entry_is_gone(hass: HomeAssistant) -> None:
    flow = _exchange_flow(hass, "removed")
    result = await flow.async_step_init()
    assert result["reason"] == "entry_not_found"
    flow._check = SimpleNamespace(old="gw-old", evidence=ModuleEvidence.MATCH)
    with patch.object(repairs, "async_exchange_gateway") as exchange:
        result = await flow._exchange()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"
    exchange.assert_not_called()


@pytest.mark.parametrize("step", ["async_step_continue", "async_step_continue_match"])
async def test_a_continue_without_a_check_is_a_changed_configuration(
    hass: HomeAssistant, step: str
) -> None:
    flow = _exchange_flow(hass)
    result = await getattr(flow, step)({"confirm": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "configuration_changed"


async def test_the_unverified_step_without_a_check_never_exchanges(
    hass: HomeAssistant,
) -> None:
    flow = _exchange_flow(hass)
    with patch.object(repairs, "async_exchange_gateway") as exchange:
        result = await flow.async_step_continue_unverified(
            {"confirm": True, "confirm_unverified": True}
        )
    assert result["reason"] == "configuration_changed"
    exchange.assert_not_called()


async def test_a_matching_confirmation_step_for_other_evidence_asks_for_both(
    hass: HomeAssistant,
) -> None:
    flow = _exchange_flow(hass)
    flow._check = SimpleNamespace(old="gw-old", evidence=ModuleEvidence.UNKNOWN)
    result = await flow.async_step_continue_match({"confirm": True})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "continue_unverified"


async def test_the_issue_registry_is_untouched_by_a_refused_exchange(
    hass: HomeAssistant,
) -> None:
    entry, cloud, _ = entry_for(hass)
    issue_id = _scoped(GATEWAY_EXCHANGE_ISSUE_PREFIX, entry, cloud, "b")
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="managed_gateway_exchange",
    )
    flow = await _flow(hass, issue_id, {"entry_id": entry.entry_id, "new_gateway": 3})
    assert isinstance(flow, InvalidRepairFlow)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_a_local_connection_is_never_switched_to_the_cloud_profile(
    hass: HomeAssistant,
) -> None:
    entry, _, _ = entry_for(hass)
    local = next(
        key
        for key, value in entry.data["connections"].items()
        if value.get("kind") != "singlekey_cloud"
    )
    flow = ManagedRepairFlow(entry.entry_id, "issue", local, None)
    flow.hass = hass
    with patch.object(repairs.ConfigurationManagement, "set_polling_profile") as save:
        result = await flow.async_step_confirm()
        result_confirm = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "configuration_changed"
    assert result_confirm["reason"] == "configuration_changed"
    save.assert_not_called()


async def test_an_unexpected_exchange_failure_is_logged_without_details(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entry, _, _ = entry_for(hass)
    flow = _exchange_flow(hass, entry.entry_id)
    flow._check = SimpleNamespace(old="gw-old", evidence=ModuleEvidence.MATCH)
    with patch.object(
        repairs,
        "async_exchange_gateway",
        side_effect=RuntimeError("token=secret-value"),
    ):
        result = await flow._exchange()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "exchange_failed"
    # The message names the error class only; details stay at debug level.
    visible = [r.getMessage() for r in caplog.records if r.levelno >= 30]
    assert visible == ["The gateway exchange failed (RuntimeError)"]
