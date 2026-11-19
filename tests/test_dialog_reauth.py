"""Signing in again directly in the options dialog (Connections)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.const import CONF_REDIRECT_URL
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    Brand,
    Gateway,
)
from tests.helpers.options_menu import menu_note
from tests.test_cloud_setup import backend as backend
from tests.test_cloud_setup import fresh_tokens
from tests.test_config_flow import _redirect_from_result
from tests.test_connection_runtime import entry_for
from tests.test_managed_connections import choose_connection

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "account")


@pytest.fixture
def account(hass, backend):
    """The account check of a renewed sign-in lists through the same mocks."""
    with patch(
        "custom_components.bosch_buderus_heating.configuration_management.discover",
        backend.devices,
    ):
        yield


async def _sign_in_page(hass, entry, cloud):
    result = await choose_connection(hass, entry, cloud)
    assert "connection_reauthenticate" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_reauthenticate"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cloud_auth"
    assert result["description_placeholders"]["authorization_url"]
    return result


def _same_account(backend, entry, cloud) -> None:
    account = EntryConfiguration(entry, cloud).cloud_data()["bacon_account"]
    backend.devices.return_value = (("softener",), account)


def _redirect(result) -> dict:
    scheme = result["description_placeholders"]["redirect_scheme"]
    (brand,) = (item for item in Brand if item.redirect_uri.startswith(scheme + ":"))
    return {CONF_REDIRECT_URL: _redirect_from_result(result, brand)}


async def test_signing_in_again_stays_in_the_dialog(hass, backend) -> None:
    hass.config.language = "en"
    entry, cloud, _ = entry_for(hass)
    _same_account(backend, entry, cloud)
    before = EntryConfiguration(entry, cloud).tokens()
    result = await _sign_in_page(hass, entry, cloud)
    # No second sign-in under "Attention required" is opened.
    assert not list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _redirect(result)
    )
    backend.oauth.assert_awaited_once()
    note = await menu_note(hass, result, "reauth_successful", step="connection")
    assert note == "Sign-in was renewed successfully."
    after = EntryConfiguration(entry, cloud).tokens()
    assert after != before
    assert after.refresh_token == fresh_tokens().refresh_token


async def test_a_waiting_renewal_is_closed_after_the_dialog_sign_in(
    hass, backend
) -> None:
    entry, cloud, _ = entry_for(hass)
    _same_account(backend, entry, cloud)
    entry.async_start_reauth(hass, context={"connection_id": cloud})
    await hass.async_block_till_done()
    assert len(list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))) == 1
    result = await _sign_in_page(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _redirect(result)
    )
    assert result["step_id"] == "result_reauth_successful"
    assert not list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_another_account_keeps_the_old_sign_in(hass, backend) -> None:
    entry, cloud, _ = entry_for(hass)
    before = EntryConfiguration(entry, cloud).tokens()
    backend.gateways.return_value = (Gateway("someone-else"),)
    backend.devices.return_value = ((), "another-account")
    result = await _sign_in_page(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _redirect(result)
    )
    assert (result["type"], result.get("reason"), result.get("errors")) in (
        (FlowResultType.ABORT, "reauth_wrong_account", None),
        (FlowResultType.FORM, None, {"base": "reauth_targets_missing"}),
    )
    assert EntryConfiguration(entry, cloud).tokens() == before


async def test_a_rejected_listing_asks_for_a_new_sign_in_here(hass, backend) -> None:
    entry, cloud, _ = entry_for(hass)
    backend.gateways.side_effect = AuthenticationError()
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    assert result["step_id"] == "cloud_auth"
    assert result["errors"] == {"base": "invalid_auth"}
    assert not list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_a_local_access_has_no_sign_in(hass, backend) -> None:
    entry, _, _ = entry_for(hass)
    result = await choose_connection(hass, entry, "local")
    assert "connection_reauthenticate" not in result["menu_options"]
    flow = hass.config_entries.options._progress[result["flow_id"]]
    shown = await flow.async_step_connection_reauthenticate()
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "configuration_changed"
