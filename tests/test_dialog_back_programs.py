"""The schedule selection of heating and hot water leads back as well."""

from __future__ import annotations

from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.options_navigation import BACK
from tests.test_switch_program_flow import environment as environment
from tests.test_switch_program_flow import make_flow

__all__ = ["environment"]


async def test_schedules_lead_back(hass, environment) -> None:
    flow, gateway = make_flow(hass, environment)
    shown = await flow.async_step_switch_programs()
    selector = shown["data_schema"].schema["program"].config
    assert selector["options"][0]["value"] == BACK
    assert selector["translation_key"] == "navigation"
    result = await flow.async_step_switch_programs({"program": BACK})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "init"
    assert gateway.raw_puts == []
