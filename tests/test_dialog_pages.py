"""Outcome pages and menus with fixed translations (profile language)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating import connection_flow, rescan
from custom_components.bosch_buderus_heating.connection_status import TEST_CODES
from custom_components.bosch_buderus_heating.options_navigation import (
    RESULT_PREFIX,
    RESULT_REASONS,
    NavigatingOptionsFlow,
)
from tests.helpers.options_menu import connection_choice
from tests.test_local_enrollment import journal as journal
from tests.test_local_flow import submit, supported
from tests.test_local_setup import registered

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")

PACKAGE = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
FILES = [PACKAGE / "strings.json", *sorted((PACKAGE / "translations").glob("*.json"))]
__all__ = ["journal", "supported"]


def _steps(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))["options"]["step"]


@pytest.mark.parametrize("path", FILES, ids=lambda path: path.stem)
def test_every_outcome_has_a_page_in_every_language(path: Path) -> None:
    steps = _steps(path)
    for reason in RESULT_REASONS:
        page = steps[RESULT_PREFIX + reason]
        assert page["title"].strip() and page["description"].strip(), reason
        assert set(page) == {"title", "description"}, reason
    for code in TEST_CODES:
        page = steps[f"connection_test_{code}"]
        assert page["title"].strip() and page["description"].strip(), code
    # No menu carries a built note any more.
    for name, step in steps.items():
        assert "{result}" not in json.dumps(step, ensure_ascii=False), name
    # The connections menu: one translated entry per listed access.
    menu = steps["connections"]["menu_options"]
    for number in range(1, connection_flow.MAX_LISTED + 1):
        key = f"connection_open_{number}"
        assert menu[key] == "{" + key + "}"
    assert list(menu)[-2:] == ["local_setup", "back"]


def test_every_reported_outcome_is_known() -> None:
    """Every literal outcome in the code has its page; rescan states too."""
    found: set[str] = set()
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in ("async_return_to_menu", "_token_note", "_remove_access"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
                    elif isinstance(arg, ast.IfExp):
                        found.update(
                            item.value
                            for item in (arg.body, arg.orelse)
                            if isinstance(item, ast.Constant)
                        )
    found -= {"init", "connections", "connection"}
    states = {
        rescan.PLANNED,
        rescan.PARTIAL,
        rescan.PAUSED,
        rescan.RUNNING,
        rescan.UNAVAILABLE,
    }
    found |= {f"rescan_{state}" for state in states}
    assert found, "no outcome found"
    assert found <= RESULT_REASONS, sorted(found - RESULT_REASONS)


async def test_an_unknown_outcome_is_refused(hass) -> None:
    flow = NavigatingOptionsFlow()
    flow.hass = hass
    with pytest.raises(ValueError, match="unknown outcome"):
        await flow.async_return_to_menu("init", "no_such_outcome")


async def test_ok_on_a_page_leads_to_its_menu(hass) -> None:
    calls: list[str] = []

    class Flow(NavigatingOptionsFlow):
        async def async_step_connection(self, user_input=None):
            calls.append("connection")
            return {"type": FlowResultType.MENU, "step_id": "connection"}

    flow = Flow()
    flow.hass = hass
    flow.handler = "x"
    flow.flow_id = "f"
    page = await flow.async_return_to_menu(
        "connection", "connection_saved", {"x": "1"}
    )
    assert page["type"] is FlowResultType.FORM
    assert page["step_id"] == "result_connection_saved"
    assert page["description_placeholders"] == {"x": "1"}
    assert not page["data_schema"].schema
    assert calls == []
    shown = await flow.async_step_result_connection_saved({})
    assert shown["step_id"] == "connection" and calls == ["connection"]
    # Without an outcome the menu comes at once.
    await flow.async_return_to_menu("connection")
    assert calls == ["connection", "connection"]
    # Other names stay unknown.
    assert not hasattr(flow, "async_step_result_holiday_created")
    with pytest.raises(AttributeError):
        flow.no_such_attribute  # noqa: B018


async def _connections(hass, entry):
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    return await submit(hass, manager, result, {"next_step_id": "connections"})


async def test_a_local_access_is_added_from_the_connections_menu(
    hass, supported, journal
) -> None:
    entry, _, _, target = registered(hass)
    result = await _connections(hass, entry)
    assert result["step_id"] == "connections"
    options = result["menu_options"]
    assert isinstance(options, list) and options[-2:] == ["local_setup", "back"]
    # Each access has its label as the placeholder of its translated entry.
    for step in options[:-2]:
        assert step.startswith("connection_open_")
        assert result["description_placeholders"][step]
    manager = hass.config_entries.options
    result = await submit(hass, manager, result, {"next_step_id": "local_setup"})
    assert result["step_id"] == "local_target"
    choices = next(iter(result["data_schema"].schema.values())).config["options"]
    # Not bound to one access: every plant and a new one without the cloud.
    values = [item["value"] for item in choices]
    assert target in values and values[-1] == "new_plant"


async def test_without_local_support_the_menu_offers_no_local_setup(hass) -> None:
    entry, _, cloud, _ = registered(hass)
    result = await _connections(hass, entry)
    assert "local_setup" not in result["menu_options"]
    assert result["menu_options"][-1] == "back"
    # An entry still opens its access.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], connection_choice(hass, result, cloud)
    )
    assert result["step_id"] == "connection"


async def test_a_long_list_falls_back_to_built_labels(hass, monkeypatch) -> None:
    hass.config.language = "de"
    monkeypatch.setattr(connection_flow, "MAX_LISTED", 0)
    entry, _, cloud, _ = registered(hass)
    result = await _connections(hass, entry)
    options = result["menu_options"]
    assert isinstance(options, dict)
    assert options["back"] == "← Zurück"
    assert all(label for label in options.values())
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], connection_choice(hass, result, cloud)
    )
    assert result["step_id"] == "connection"
