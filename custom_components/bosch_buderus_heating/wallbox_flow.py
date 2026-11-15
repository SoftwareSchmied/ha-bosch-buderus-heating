"""The options step of wallboxes: authentication before a start.

A wallbox that requires authentication starts charging only after an
``authenticate`` command. The integration sends it only when the user
allows it here (off by default), with the optional authentication name the
vendor app keeps on the phone. The name is stored in the entry options and
never shown in diagnostics or logs.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult

from .ha_schema import validation as vol
from .options_navigation import NavigatingOptionsFlow
from .wallbox import WallboxCoordinator
from .wallbox_control import (
    OPTION_AUTHENTICATE,
    OPTION_AUTHENTICATION_NAME,
    label_valid,
)


def wallboxes_of(entry: ConfigEntry[Any]) -> tuple[WallboxCoordinator, ...]:
    """The wallboxes the entry currently runs, for the options menu."""
    runtime = getattr(entry, "runtime_data", None)
    legacy = getattr(runtime, "wallboxes", None)
    if isinstance(legacy, tuple):
        return legacy
    connections = getattr(runtime, "connections", None)
    snapshot = getattr(connections, "snapshot", None)
    if connections is None or snapshot is None:
        return ()
    found = []
    for key in tuple(snapshot.bindings):
        value = connections.display_data(key)
        if isinstance(value, WallboxCoordinator):
            found.append(value)
    return tuple(found)


class WallboxOptionsFlow(NavigatingOptionsFlow):
    _entry: ConfigEntry[Any]

    async def async_step_wallbox(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            allowed = user_input.get(OPTION_AUTHENTICATE) is True
            name = user_input.get(OPTION_AUTHENTICATION_NAME, "")
            name = name.strip() if isinstance(name, str) else ""
            if name and not label_valid(name, 64):
                errors[OPTION_AUTHENTICATION_NAME] = "wallbox_name_invalid"
            else:
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    options={
                        **self._entry.options,
                        OPTION_AUTHENTICATE: allowed,
                        OPTION_AUTHENTICATION_NAME: name,
                    },
                )
                return await self.async_return_to_menu("init", "wallbox_saved")
        options = self._entry.options
        stored_name = options.get(OPTION_AUTHENTICATION_NAME)
        return self.async_show_form(
            step_id="wallbox",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPTION_AUTHENTICATE,
                        default=options.get(OPTION_AUTHENTICATE) is True,
                    ): bool,
                    vol.Optional(
                        OPTION_AUTHENTICATION_NAME,
                        default=stored_name if isinstance(stored_name, str) else "",
                    ): str,
                }
            ),
            errors=errors,
        )
