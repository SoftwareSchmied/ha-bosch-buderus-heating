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
    page = await flow.async_return_to_menu("connection", "connection_saved", {"x": "1"})
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


async def test_the_local_setup_is_offered_once(hass, supported, journal) -> None:
    """In the connections menu only; the main menu offers it just while there
    is no access to list (a new entry without one)."""
    from tests.test_local_first import empty_entry

    entry, _, _, _ = registered(hass)
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    assert "connections" in result["menu_options"]
    assert "local_setup" not in result["menu_options"]
    result = await submit(hass, manager, result, {"next_step_id": "connections"})
    assert result["menu_options"].count("local_setup") == 1
    empty, _ = empty_entry(hass, "empty-entry")
    result = await manager.async_init(empty.entry_id)
    assert result["menu_options"].count("local_setup") == 1


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


async def test_systems_of_an_access_are_listed_by_name(hass) -> None:
    from homeassistant.helpers import device_registry as dr

    from custom_components.bosch_buderus_heating.const import DOMAIN

    entry, _, cloud, _ = registered(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "000123")},
        name="Heat pump",
    )
    result = await _connections(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], connection_choice(hass, result, cloud)
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_targets"}
    )
    labels = [
        item["label"]
        for item in result["data_schema"].schema["targets"].config["options"]
    ]
    assert "Heat pump" in labels and "000123" not in labels


async def test_intermediate_forms_say_next(hass) -> None:
    from homeassistant import config_entries

    from custom_components.bosch_buderus_heating.const import DOMAIN
    from custom_components.bosch_buderus_heating.options_navigation import (
        NEXT_STEPS,
    )

    flow = NavigatingOptionsFlow()
    flow.hass = hass
    flow.handler = "x"
    flow.flow_id = "f"
    for step in NEXT_STEPS:
        assert flow.async_show_form(step_id=step)["last_step"] is False
    # A form that saves keeps the default button; an explicit choice stays.
    assert flow.async_show_form(step_id="notifications")["last_step"] is None
    assert flow.async_show_form(step_id="holidays", last_step=True)["last_step"]
    # The setup: the brand choice leads to the sign-in.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    assert result["step_id"] == "cloud" and result["last_step"] is False
    hass.config_entries.flow.async_abort(result["flow_id"])


def test_variants_of_intermediate_forms_say_next() -> None:
    """A variant of an intermediate form (its step id plus a suffix, such as
    the air conditioner's week) leads to another form as well."""
    from custom_components.bosch_buderus_heating.options_navigation import (
        NEXT_STEPS,
    )

    strings = json.loads(
        (
            Path(__file__).parents[1]
            / "custom_components/bosch_buderus_heating/strings.json"
        ).read_text(encoding="utf-8")
    )
    # Separate steps of the token list that only share its name.
    others = {
        "local_tokens_certificate",
        "local_tokens_remove",
        "local_tokens_remove_none",
    }
    variants = {
        step
        for step in strings["options"]["step"]
        if any(step.startswith(f"{base}_") for base in NEXT_STEPS)
    } - others
    assert {
        "switch_program_week_aircon",
        "switch_program_week_aircon_inactive",
        "switch_program_point_aircon",
    } <= variants
    assert variants <= NEXT_STEPS
