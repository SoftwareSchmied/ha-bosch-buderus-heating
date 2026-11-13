"""Back navigation and result notes in the options dialog.

Every menu below the main menu (``init``) ends with a "Back" entry
(``back``) that leads to its parent menu. A finished action (a saved
setting, a written holiday or program, a planned search) returns to its menu
with a short note instead of closing the dialog, so several things can be
done in one session. The note is the text of the action's former success
message; the menu shows it once, in its description (``{result}``).

Settings that were saved through ``async_create_entry`` are written to the
entry directly before the menu is shown; the effect is the same, the dialog
just stays open.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN

BACK = "back"


class _Missing(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class NavigatingOptionsFlow(OptionsFlow):
    """The menu a ``back`` entry leads to and the note of a finished action."""

    _back_target: str = "init"
    _result_note: str = ""

    def take_result_note(self) -> str:
        """The note of the last finished action, once; empty without one."""
        note, self._result_note = self._result_note, ""
        return note

    async def async_step_back(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Return to the parent of the menu that offered ``back``."""
        target = self._back_target
        self._result_note = ""
        result: ConfigFlowResult = await getattr(self, f"async_step_{target}")()
        return result

    async def async_return_to_menu(
        self,
        menu: str,
        reason: str | None = None,
        placeholders: Mapping[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show ``menu`` again, with the text of ``reason`` as its note."""
        self._result_note = (
            await self._note_text(reason, placeholders) if reason is not None else ""
        )
        result: ConfigFlowResult = await getattr(self, f"async_step_{menu}")()
        return result

    async def _options_text(self, key: str, fallback: str) -> str:
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "options", {DOMAIN}
        )
        return translations.get(f"component.{DOMAIN}.options.{key}", fallback)

    async def back_label(self, step_id: str) -> str:
        """The translated label of the ``back`` entry of a menu."""
        return await self._options_text(f"step.{step_id}.menu_options.{BACK}", "← Back")

    async def _note_text(
        self, reason: str, placeholders: Mapping[str, str] | None
    ) -> str:
        text = await self._options_text(f"abort.{reason}", reason)
        if placeholders:
            # A stray brace in a text: shown as it is.
            with suppress(ValueError, IndexError):
                text = text.format_map(_Missing(placeholders))
        return f"{text}\n\n"
