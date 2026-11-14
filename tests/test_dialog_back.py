"""Every selection form under the main menu has a way back (holidays,
room controller programs, operation, token list)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.options_navigation import BACK
from tests.test_config_flow import _entry_with_writable_holiday, _start_holiday_options
from tests.test_local_auth import auth_gateway as auth_gateway
from tests.test_local_tokens import OLD, OWN, Gateway, _open_list
from tests.test_local_tokens import dialog as dialog
from tests.test_local_transport import certificates as certificates
from tests.test_rrc_flow import environment as environment
from tests.test_rrc_flow import make_flow

PACKAGE = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
__all__ = ["auth_gateway", "certificates", "dialog", "environment"]


def _first(result, field: str) -> dict:
    for key, selector in result["data_schema"].schema.items():
        if str(key) == field:
            config = selector.config
            return {"first": config["options"][0], "key": config["translation_key"]}
    raise AssertionError(field)


@pytest.mark.parametrize(
    "path",
    [PACKAGE / "strings.json", *sorted((PACKAGE / "translations").glob("*.json"))],
    ids=lambda path: path.stem,
)
def test_the_back_choice_reads_like_the_menu_entry(path: Path) -> None:
    data = json.loads(path.read_text("utf-8"))
    back = data["options"]["step"]["connections"]["menu_options"]["back"]
    assert data["selector"]["navigation"]["options"]["back"] == back
    assert data["selector"]["holiday_period"]["options"]["back"] == back


async def test_holidays_lead_back(hass, enable_custom_integrations) -> None:
    entry, _ = _entry_with_writable_holiday(hass)
    result = await _start_holiday_options(hass, entry)
    first = _first(result, "holiday_period")
    assert first["first"]["value"] == BACK and first["key"] == "holiday_period"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"holiday_period": BACK}
    )
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "init"


async def test_room_controller_programs_lead_back(hass, environment) -> None:
    flow, coordinator = make_flow(hass, environment)
    shown = await flow.async_step_rrc_programs()
    first = _first(shown, "program")
    assert first["first"]["value"] == BACK and first["key"] == "navigation"
    result = await flow.async_step_rrc_programs({"program": BACK})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "init"
    coordinator.client.put_resource_value.assert_not_awaited()


async def test_operation_leads_back_and_names_systems_once(hass, environment) -> None:
    flow, coordinator = make_flow(hass, environment)
    other = SimpleNamespace(gateway=SimpleNamespace(gateway_id="gw-two"))
    flow._control_gateways = lambda: {"a": coordinator, "b": other}
    shown = await flow.async_step_controls()
    options = shown["data_schema"].schema["installation"].config["options"]
    assert options[0]["value"] == BACK
    labels = [item["label"] for item in options[1:]]
    # Distinct names carry no extra number.
    assert labels == ["Heating system 1", "Heating system 2"]
    result = await flow.async_step_controls({"installation": BACK})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "init"


@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.parametrize("removal", [False, True])
async def test_the_token_list_leads_back_without_a_change(
    hass, dialog, removal
) -> None:
    gateway = Gateway((OWN, OLD))
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow = result["flow_id"]
    if removal:
        # The list before a removal with revocation: "Back" keeps the access.
        await hass.config_entries.options.async_configure(flow, {"token": BACK})
        await hass.config_entries.options.async_configure(
            flow, {"next_step_id": "connection_remove"}
        )
        result = await hass.config_entries.options.async_configure(
            flow, {"confirm": True, "revoke": True}
        )
        assert result["step_id"] == "local_tokens_remove"
    assert _first(result, "token")["first"]["value"] == BACK
    result = await hass.config_entries.options.async_configure(flow, {"token": BACK})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "connection"
    assert gateway.posts == []
    assert "local" in entry.data["connections"]
