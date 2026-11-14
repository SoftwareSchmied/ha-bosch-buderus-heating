"""The options step of the plant's recordings.

Switches the import of the hourly values into the long-term statistics on
or off (off by default) and deletes the imported statistics after a
confirmation. Switching off keeps what was imported; switching on again
goes on from the last imported hour. Deleting also switches the import
off; switched on again, it imports everything the plant still has.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult

from .ha_schema import validation as vol
from .options_navigation import NavigatingOptionsFlow
from .recordings.manager import OPTION_IMPORT, import_enabled, managers_of

DELETE = "delete_statistics"
CONFIRM = "confirm"


class RecordingsOptionsFlow(NavigatingOptionsFlow):
    _entry: ConfigEntry[Any]

    def _recording_status(self) -> dict[str, str]:
        managers = managers_of(self.hass, self._entry.entry_id)
        series = sum(len(manager.available()) for manager in managers)
        imported = sum(
            ledger.importing
            for manager in managers
            for ledger in manager.ledgers.values()
        )
        return {"series": str(series), "imported": str(imported)}

    async def async_step_recordings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            if user_input.get(DELETE) is True:
                return await self.async_step_recordings_delete()
            enabled = user_input.get(OPTION_IMPORT) is True
            was = import_enabled(self._entry)
            self.hass.config_entries.async_update_entry(
                self._entry, options={**self._entry.options, OPTION_IMPORT: enabled}
            )
            if enabled and not was:
                for manager in managers_of(self.hass, self._entry.entry_id):
                    manager.request_run()
            return await self.async_return_to_menu(
                "init", "recordings_import_on" if enabled else "recordings_import_off"
            )
        return self.async_show_form(
            step_id="recordings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPTION_IMPORT, default=import_enabled(self._entry)
                    ): bool,
                    vol.Optional(DELETE, default=False): bool,
                }
            ),
            description_placeholders=self._recording_status(),
        )

    async def async_step_recordings_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None or DELETE in user_input:
            return self.async_show_form(
                step_id="recordings_delete",
                data_schema=vol.Schema({vol.Required(CONFIRM, default=False): bool}),
            )
        if user_input.get(CONFIRM) is not True:
            return await self.async_return_to_menu("init", "recordings_delete_kept")
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, OPTION_IMPORT: False}
        )
        count = 0
        for manager in managers_of(self.hass, self._entry.entry_id):
            count += await manager.async_delete_statistics()
        return await self.async_return_to_menu(
            "init", "recordings_deleted", {"count": str(count)}
        )
