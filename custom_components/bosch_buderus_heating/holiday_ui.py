"""Small native-form extension for the holiday delete action."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, override

from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import BooleanSelector, TextSelector

from .ha_schema import validation as vol
from .log_helpers import log_failure

DELETE_FIELD = "holiday_delete"
_REGISTERED = "bosch_buderus_heating.holiday_ui"
_LOGGER = logging.getLogger(__name__)


class HolidayDeleteSelector(BooleanSelector):
    """Render a button while keeping the action a server-validated boolean."""

    selector_type = "bosch-buderus-holiday-delete"


class HolidayDateTimeSelector(TextSelector):
    """Keep date and minute-only clock together, including an empty new date."""

    selector_type = "bosch-buderus-holiday-datetime"

    def __init__(self, *, creating: bool) -> None:
        super().__init__()
        self._creating = creating

    @override
    def serialize(self) -> dict[str, Any]:
        return {
            "selector": {
                self.selector_type: {"default_time": "08:00" if self._creating else ""}
            }
        }


class HolidayFormSchema(vol.Schema):
    """Allow opening confirmation without validating an unsaved draft."""

    @override
    def __call__(self, data: Any, *, context: Any = None) -> Any:
        if (
            isinstance(data, dict)
            and data.get(DELETE_FIELD) is True
            and DELETE_FIELD in self.schema
        ):
            # This action only opens confirmation. Nothing from the draft is
            # written. Keep known inputs so cancellation can restore them.
            if any(key not in self.schema for key in data):
                raise vol.Invalid("Unknown holiday field")
            return dict(data)
        if context is not None:
            return super().__call__(data, context=context)
        return super().__call__(data)


async def async_setup_holiday_ui(hass: HomeAssistant) -> None:
    """Register the selector once, without adding a panel or a cloud API."""
    await _async_register_module(hass, _REGISTERED, "holiday-dialog")


async def async_setup_controls_ui(hass: HomeAssistant) -> None:
    """Load the controls selector through the same supported frontend hook."""
    await _async_register_module(
        hass, "bosch_buderus_heating.controls_ui", "controls-dialog"
    )


async def _async_register_module(hass: HomeAssistant, key: str, name: str) -> None:
    if key in hass.data or "frontend" not in hass.config.components:
        return

    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import (  # type: ignore[attr-defined,unused-ignore]
        StaticPathConfig,
    )

    source = Path(__file__).parent / "www" / f"{name}.js"
    try:
        content = await hass.async_add_executor_job(source.read_bytes)
        version = hashlib.sha256(content).hexdigest()[:16]
        url = f"/bosch_buderus_heating/{name}-{version}.js"
        await hass.http.async_register_static_paths(
            [StaticPathConfig(url, str(source), cache_headers=True)]
        )
        add_extra_js_url(hass, url)
    except (OSError, ValueError, RuntimeError) as error:
        # Only one optional dialog form is affected; the appliances, their
        # entities and the remaining options must still start.
        log_failure(
            _LOGGER,
            logging.WARNING,
            "The dialog module %s could not be loaded, so its form cannot be "
            "shown; everything else starts normally",
            name,
            error=error,
        )
        return
    hass.data[key] = url
