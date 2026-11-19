"""The outcome of an options dialog step: an end or a result page.

Since 0.9.2 a finished action shows its outcome on a page of its own
(``result_<reason>``, a fixed translation) and **OK** returns to its menu
(``options_navigation``). ``outcome`` names that outcome, or the reason of
an aborted dialog as before, so a test can check either.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.translation import async_get_translations

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.options_navigation import RESULT_PREFIX


def result_reason(result: Any) -> str | None:
    """The outcome a result page shows, None for any other step."""
    if result["type"] is not FlowResultType.FORM:
        return None
    step = str(result.get("step_id", ""))
    return step.removeprefix(RESULT_PREFIX) if step.startswith(RESULT_PREFIX) else None


async def outcome(hass: HomeAssistant, result: Any) -> str | None:
    """The abort reason, or the outcome a result page shows."""
    if result["type"] is FlowResultType.ABORT:
        return result["reason"]
    return result_reason(result)


async def page_text(hass: HomeAssistant, result: Any) -> str:
    """The text of a page of a fixed translation, filled in like the frontend."""
    translations = await async_get_translations(
        hass, hass.config.language, "options", {DOMAIN}
    )
    text = translations[
        f"component.{DOMAIN}.options.step.{result['step_id']}.description"
    ]
    return text.format(**(result.get("description_placeholders") or {}))


async def leave_page(hass: HomeAssistant, result: Any, flow: Any = None) -> Any:
    """Select **OK** on a page; the dialog shows its menu again.

    ``flow`` is a flow a test drives directly, outside the flow manager.
    """
    if flow is not None:
        return await getattr(flow, f"async_step_{result['step_id']}")({})
    return await hass.config_entries.options.async_configure(result["flow_id"], {})


async def menu_note(
    hass: HomeAssistant,
    result: Any,
    reason: str,
    *,
    step: str = "init",
    flow: Any = None,
) -> str:
    """Check the result page of ``reason`` and that **OK** leads to ``step``.

    Returns the page text; the dialog is at ``step`` afterwards.
    """
    assert result_reason(result) == reason, result
    text = await page_text(hass, result)
    menu = await leave_page(hass, result, flow)
    assert menu["type"] is FlowResultType.MENU, menu
    assert menu["step_id"] == step, menu
    return text


def connection_choice(hass: HomeAssistant, result: Any, connection_id: str) -> dict:
    """The menu choice of one access in the connections menu."""
    flow = hass.config_entries.options._progress[result["flow_id"]]
    for step, key in (flow._connection_choices or {}).items():
        if key == connection_id:
            return {"next_step_id": step}
    raise AssertionError(f"{connection_id} is not in the connections menu")
