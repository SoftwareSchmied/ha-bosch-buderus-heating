"""Back navigation and result pages in the options dialog.

Every menu below the main menu (``init``) ends with a "Back" entry
(``back``) that leads to its parent menu. A finished action (a saved
setting, a written holiday or program, a planned search) shows its outcome
on a short page of its own and then returns to its menu with **OK**, so
several things can be done in one session.

The outcome is a fixed translation (``options.step.result_<reason>``), so
Home Assistant shows it in the language of the user's profile; only values
such as times or names are filled in. The same holds for the result of a
connection test.

Settings that were saved through ``async_create_entry`` are written to the
entry directly before the page is shown; the effect is the same, the dialog
just stays open.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import SelectOptionDict

from .ha_schema import validation as vol

BACK: Final = "back"
# The translation of the "Back" choice of a selection form (also in
# ``holiday_period``): a selection form has no menu entry, so its first
# choice leads back instead of closing the whole dialog with X.
NAVIGATION: Final = "navigation"
BACK_CHOICE: Final = SelectOptionDict(value=BACK, label=BACK)
# The step of the outcome of a finished action.
RESULT_PREFIX: Final = "result_"
# Every outcome a finished action can report, each with its own page text.
RESULT_REASONS: Final = frozenset(
    {
        "component_devices_off",
        "component_devices_on",
        "connection_removed",
        "connection_saved",
        "holiday_created",
        "holiday_deleted",
        "holiday_unchanged",
        "holiday_updated",
        "icom_connected",
        "local_token_access_end_failed",
        "local_token_access_ended",
        "local_token_not_removed",
        "local_token_old_revoked_removed",
        "local_token_revoke_rejected",
        "local_token_revoke_unclear",
        "local_token_revoked",
        "local_token_revoked_removed",
        "local_tokens_changed",
        "notifications_saved",
        "reauth_successful",
        "reauth_successful_targets_missing",
        "recordings_delete_kept",
        "recordings_deleted",
        "recordings_import_off",
        "recordings_import_on",
        "rescan_partial",
        "rescan_paused",
        "rescan_planned",
        "rescan_running",
        "rescan_unavailable",
        "rrc_program_saved",
        "switch_program_saved",
        "wallbox_saved",
    }
)

StepHandler = Callable[..., Awaitable[ConfigFlowResult]]
# Forms that always lead to another form: Home Assistant shows "Next"
# instead of the button that ends a dialog.
NEXT_STEPS: Final = frozenset(
    {
        "cloud_retry",
        "connection_add_cloud",
        "connection_discover_retry",
        "controls",
        "holiday_gateway",
        "holidays",
        "icom_setup",
        "local_tokens",
        "rrc_add",
        "rrc_day",
        "rrc_programs",
        "rrc_schedule",
        "switch_program_copy",
        "switch_program_day",
        "switch_program_point",
        "switch_program_point_aircon",
        "switch_program_point_value",
        "switch_program_week",
        "switch_program_week_aircon",
        "switch_program_week_aircon_inactive",
        "switch_program_week_dhw",
        "switch_program_week_inactive",
        "switch_program_week_rrc_dhw",
        "switch_programs",
    }
)


class NavigatingOptionsFlow(OptionsFlow):
    """The menu a ``back`` entry leads to and the page of a finished action."""

    _back_target: str = "init"
    # Steps that exist only while their form or menu is shown: an entry of
    # the connections menu, the page of an outcome.
    _page_steps: dict[str, StepHandler] | None = None

    def async_show_form(self, **kwargs: Any) -> ConfigFlowResult:
        """An intermediate form says "Next" (its own choice stays)."""
        if kwargs.get("step_id") in NEXT_STEPS:
            kwargs.setdefault("last_step", False)
        return super().async_show_form(**kwargs)

    def __getattr__(self, name: str) -> Any:
        """A step that exists only while its form or menu is shown."""
        steps = self.__dict__.get("_page_steps") or {}
        if name.startswith("async_step_"):
            step = steps.get(name.removeprefix("async_step_"))
            if step is not None:
                return step
        raise AttributeError(name)

    def add_page_step(self, step_id: str, handler: StepHandler) -> None:
        """Make ``async_step_<step_id>`` answer with ``handler``."""
        steps = dict(self.__dict__.get("_page_steps") or {})
        steps[step_id] = handler
        self._page_steps = steps

    async def async_step_back(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Return to the parent of the menu that offered ``back``."""
        target = self._back_target
        result: ConfigFlowResult = await getattr(self, f"async_step_{target}")()
        return result

    async def _show_menu(self, menu: str) -> ConfigFlowResult:
        result: ConfigFlowResult = await getattr(self, f"async_step_{menu}")()
        return result

    def show_page(
        self,
        step_id: str,
        menu: str,
        placeholders: Mapping[str, str] | None = None,
    ) -> ConfigFlowResult:
        """A page with a fixed text; **OK** leads to ``menu``."""

        async def leave(user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
            return await self._show_menu(menu)

        self.add_page_step(step_id, leave)
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({}),
            description_placeholders=dict(placeholders or {}),
        )

    async def async_return_to_menu(
        self,
        menu: str,
        reason: str | None = None,
        placeholders: Mapping[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show ``menu`` again; with ``reason``, its outcome page first."""
        if reason is None:
            return await self._show_menu(menu)
        if reason not in RESULT_REASONS:
            raise ValueError(f"unknown outcome {reason}")
        return self.show_page(RESULT_PREFIX + reason, menu, placeholders)
