"""The outcome of an options dialog step: an end or a menu with a note.

Since 0.9.0-alpha.6 a finished action returns to its menu with the text of
its former success message as a note (``options_navigation``). ``outcome``
names that message for a menu with a note, and the reason of an aborted
dialog as before, so a test can check either.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.translation import async_get_translations

from custom_components.bosch_buderus_heating.const import DOMAIN

_ABORT = f"component.{DOMAIN}.options.abort."


async def outcome(hass: HomeAssistant, result: Any) -> str | None:
    """The abort reason, or the message a menu shows as its note."""
    if result["type"] is FlowResultType.ABORT:
        return result["reason"]
    if result["type"] is not FlowResultType.MENU:
        return None
    note = str((result.get("description_placeholders") or {}).get("result", ""))
    note = note.strip()
    if not note:
        return None
    translations = await async_get_translations(
        hass, hass.config.language, "options", {DOMAIN}
    )
    best: tuple[int, str] | None = None
    for key, text in translations.items():
        if not key.startswith(_ABORT):
            continue
        fixed = text.split("{", 1)[0].strip()
        if fixed and note.startswith(fixed) and (best is None or len(fixed) > best[0]):
            best = (len(fixed), key.removeprefix(_ABORT))
    return best[1] if best is not None else note


async def menu_note(
    hass: HomeAssistant, result: Any, reason: str, *, step: str = "init"
) -> str:
    """Check that ``result`` is ``step`` with the note of ``reason``."""
    assert result["type"] is FlowResultType.MENU, result
    assert result["step_id"] == step, result
    assert await outcome(hass, result) == reason, result
    return str(result["description_placeholders"]["result"])


def connection_choice(hass: HomeAssistant, result: Any, connection_id: str) -> dict:
    """The menu choice of one access in the connections menu."""
    flow = hass.config_entries.options._progress[result["flow_id"]]
    for step, key in (flow._connection_choices or {}).items():
        if key == connection_id:
            return {"next_step_id": step}
    raise AssertionError(f"{connection_id} is not in the connections menu")
