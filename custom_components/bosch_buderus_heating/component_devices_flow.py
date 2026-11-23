"""The options step of the optional component devices.

Lists the components the entry's systems report and switches one device per
component on or off (off by default). Switching on creates the devices at
once; switching off removes them again. Neither reloads the entry.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult

from .component_devices import OPTION, enable, enabled, listing, remove_all
from .ha_schema import validation as vol
from .options_navigation import NavigatingOptionsFlow


class ComponentDevicesOptionsFlow(NavigatingOptionsFlow):
    _entry: ConfigEntry[Any]

    async def async_step_components(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            on = user_input.get(OPTION) is True
            # Saved first, so a running update follows the new setting.
            self.hass.config_entries.async_update_entry(
                self._entry, options={**self._entry.options, OPTION: on}
            )
            if on:
                enable(self.hass, self._entry)
                return await self.async_return_to_menu("init", "component_devices_on")
            removed = remove_all(self.hass, self._entry)
            return await self.async_return_to_menu(
                "init", "component_devices_off", {"count": str(removed)}
            )
        return self.async_show_form(
            step_id="components",
            data_schema=vol.Schema(
                {vol.Required(OPTION, default=enabled(self._entry)): bool}
            ),
            description_placeholders={"components": listing(self.hass, self._entry)},
        )
