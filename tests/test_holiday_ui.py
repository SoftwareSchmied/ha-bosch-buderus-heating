"""Validate the direct delete action and the small frontend registration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.helpers.selector import TextSelector

from custom_components.bosch_buderus_heating.ha_schema import validation as vol
from custom_components.bosch_buderus_heating.holiday_ui import (
    DELETE_FIELD,
    HolidayDeleteSelector,
    HolidayFormSchema,
    async_setup_holiday_ui,
)


def _schema():
    return HolidayFormSchema(
        {
            vol.Required("holiday_start"): TextSelector(),
            vol.Optional(DELETE_FIELD): HolidayDeleteSelector(),
        }
    )


def test_delete_confirmation_can_open_with_an_incomplete_unsaved_draft():
    """A missing date must not force a user to save before deleting."""
    assert _schema()({DELETE_FIELD: True}) == {DELETE_FIELD: True}


@pytest.mark.parametrize("action", [False, None, "", 0])
def test_save_validation_is_not_bypassed_without_a_literal_delete_request(action):
    with pytest.raises(vol.Invalid):
        _schema()({DELETE_FIELD: action})


def test_delete_confirmation_rejects_unknown_fields():
    with pytest.raises(vol.Invalid):
        _schema()({DELETE_FIELD: True, "unknown_action": "delete"})


def test_form_schema_preserves_validation_context():
    if vol.__name__ != "probatio":
        pytest.skip("Voluptuous forms receive no validation context")
    assert _schema()({"holiday_start": "2030-08-01T08:00"}, context={}) == {
        "holiday_start": "2030-08-01T08:00"
    }


async def test_frontend_module_is_registered_once_with_a_content_version(hass):
    hass.config.components.add("frontend")
    hass.http = SimpleNamespace(async_register_static_paths=AsyncMock())
    with patch(
        "homeassistant.components.frontend.add_extra_js_url", new=Mock()
    ) as add_module:
        await async_setup_holiday_ui(hass)
        await async_setup_holiday_ui(hass)
    hass.http.async_register_static_paths.assert_awaited_once()
    path = hass.http.async_register_static_paths.await_args.args[0][0]
    assert path.url_path.startswith("/bosch_buderus_heating/holiday-dialog-")
    assert path.path.endswith("www/holiday-dialog.js")
    assert path.cache_headers is True
    add_module.assert_called_once_with(hass, path.url_path)


async def test_frontend_registration_is_optional_for_headless_instances(hass):
    await async_setup_holiday_ui(hass)
    assert "bosch_buderus_heating.holiday_ui" not in hass.data
