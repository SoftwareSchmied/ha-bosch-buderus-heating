"""Failure paths of the cloud steps of the options dialog (sign-in, listing)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.bacon.models import BaconError
from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    Brand,
    PointTError,
    RateLimited,
)
from tests.helpers.options_menu import outcome
from tests.test_cloud_setup import ROOT, connect_dialog, empty_entry, existing
from tests.test_cloud_setup import backend as backend
from tests.test_config_flow import _redirect_from_result
from tests.test_connection_runtime import entry_for
from tests.test_dialog_reauth import _redirect, _same_account, _sign_in_page
from tests.test_dialog_reauth import account as account
from tests.test_managed_connections import choose_connection

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "account")

MANAGEMENT = ROOT + ".configuration_management.ConfigurationManagement"
FLOW = ROOT + ".cloud_options_flow"


def _flow(hass, result):
    return hass.config_entries.options._progress[result["flow_id"]]


async def _submit_sign_in(hass, entry, cloud, error):
    """Sign in again; storing the renewed sign-in fails with ``error``."""
    result = await _sign_in_page(hass, entry, cloud)
    with patch(
        MANAGEMENT + ".async_reauthenticate", AsyncMock(side_effect=error)
    ) as renew:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _redirect(result)
        )
    assert renew.await_count == 1
    return result


@pytest.mark.parametrize("reason", ["reauth_targets_missing", "unverifiable_targets"])
async def test_renewal_without_the_systems_asks_for_another_sign_in(
    hass, backend, reason
) -> None:
    entry, cloud, _ = entry_for(hass)
    before = EntryConfiguration(entry, cloud).tokens()
    result = await _submit_sign_in(hass, entry, cloud, ConfigurationError(reason))
    assert result["step_id"] == "cloud_auth"
    assert result["errors"] == {"base": "reauth_targets_missing"}
    assert result["description_placeholders"]["authorization_url"]
    assert EntryConfiguration(entry, cloud).tokens() == before


@pytest.mark.parametrize("reason", ["reauth_wrong_account", "reauth_no_targets"])
async def test_renewal_of_another_account_or_nothing_aborts(
    hass, backend, reason
) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await _submit_sign_in(hass, entry, cloud, ConfigurationError(reason))
    assert await outcome(hass, result) == reason


async def test_renewal_with_a_changed_configuration_aborts(hass, backend) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await _submit_sign_in(hass, entry, cloud, ConfigurationError("other"))
    assert await outcome(hass, result) == "configuration_changed"


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError(),
        BaconError("authentication"),
        BaconError("account_changed"),
    ],
)
async def test_rejected_renewal_asks_for_a_new_sign_in(hass, backend, error) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await _submit_sign_in(hass, entry, cloud, error)
    assert result["step_id"] == "cloud_auth"
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.parametrize(
    "error,key",
    [
        (BaconError("rate_limited"), "cloud_rate_limited"),
        (BaconError("unavailable"), "cannot_connect"),
        (RateLimited(None), "cloud_rate_limited"),
        (PointTError("down"), "cannot_connect"),
    ],
)
async def test_unreachable_cloud_during_renewal_keeps_the_login_for_a_retry(
    hass, backend, error, key
) -> None:
    entry, cloud, _ = entry_for(hass)
    _same_account(backend, entry, cloud)
    before = EntryConfiguration(entry, cloud).tokens()
    result = await _submit_sign_in(hass, entry, cloud, error)
    assert result["step_id"] == "cloud_retry"
    assert result["errors"] == {"base": key}
    assert EntryConfiguration(entry, cloud).tokens() == before
    # The same sign-in is stored once the cloud answers; no second login.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert await outcome(hass, result) == "reauth_successful"
    assert EntryConfiguration(entry, cloud).tokens() != before
    backend.oauth.assert_awaited_once()


async def test_renewal_reports_systems_that_were_not_found(hass, backend) -> None:
    entry, cloud, _ = entry_for(hass)
    _same_account(backend, entry, cloud)
    result = await _sign_in_page(hass, entry, cloud)
    with patch(FLOW + ".not_found_bindings", return_value=["a", "b"]):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _redirect(result)
        )
    assert await outcome(hass, result) == "reauth_successful_targets_missing"
    assert result["description_placeholders"] == {"count": "2"}


async def test_renewal_of_a_vanished_access_returns_to_the_list(hass, backend) -> None:
    entry, cloud, _ = entry_for(hass)
    _same_account(backend, entry, cloud)
    result = await _sign_in_page(hass, entry, cloud)

    def vanished(self, connection_id):
        raise ConfigurationError("unknown_connection")

    with (
        patch(MANAGEMENT + ".async_reauthenticate", AsyncMock(return_value=None)),
        patch(MANAGEMENT + ".edit", vanished),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _redirect(result)
        )
    assert result["step_id"] == "result_reauth_successful"
    assert _flow(hass, result)._connection_edit is None


async def test_sign_in_page_without_its_state_aborts(hass, backend) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await _sign_in_page(hass, entry, cloud)
    flow = _flow(hass, result)
    flow._cloud_backoff = None
    shown = await flow.async_step_cloud_auth({"redirect_url": "x"})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "configuration_changed"
    shown = await flow.async_step_cloud_retry({})
    assert shown["reason"] == "configuration_changed"


async def test_sign_in_step_refuses_while_the_cloud_is_paused(hass, backend) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await _sign_in_page(hass, entry, cloud)
    flow = _flow(hass, result)
    flow._cloud_backoff.activate(300)
    shown = await flow.async_step_cloud_auth({"redirect_url": "x"})
    assert shown["step_id"] == "cloud_retry"
    assert shown["errors"] == {"base": "cloud_rate_limited"}
    # The retry page stays paused and sends nothing.
    shown = await flow.async_step_cloud_retry({})
    assert shown["step_id"] == "cloud_retry"
    assert shown["errors"] == {"base": "cloud_rate_limited"}
    backend.oauth.assert_not_awaited()


async def test_sign_in_step_aborts_when_another_flow_claimed_the_setup(
    hass, backend
) -> None:
    entry = empty_entry(hass)
    result = await connect_dialog(hass, entry, authenticate=False)
    flow = _flow(hass, result)
    with patch(
        FLOW + ".check_new_cloud",
        side_effect=ConfigurationError("cloud_setup_in_progress"),
    ):
        shown = await flow.async_step_cloud_auth({"redirect_url": "x"})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "cloud_setup_in_progress"
    with patch(FLOW + ".check_new_cloud", side_effect=ConfigurationError("other")):
        shown = await flow.async_step_cloud_auth({"redirect_url": "x"})
    assert shown["reason"] == "configuration_changed"


async def test_a_retry_of_a_replaced_dialog_aborts(hass, backend) -> None:
    entry = empty_entry(hass)
    result = await connect_dialog(hass, entry, authenticate=False)
    flow = _flow(hass, result)
    with patch(FLOW + ".renew_cloud_claim", return_value=False):
        shown = await flow.async_step_cloud_retry({})
    assert shown["reason"] == "cloud_setup_taken_over"


@pytest.mark.parametrize(
    "gateways,devices,key",
    [
        (AuthenticationError(), None, "invalid_auth"),
        (None, BaconError("authentication"), "invalid_auth"),
        (None, BaconError("account_changed"), "invalid_auth"),
        (None, BaconError("rate_limited", 120), "cannot_connect"),
        (None, BaconError("unavailable"), "cannot_connect"),
        (PointTError("down"), None, "cannot_connect"),
    ],
)
async def test_listing_failures_after_a_new_sign_in(
    hass, backend, gateways, devices, key
) -> None:
    entry = empty_entry(hass)
    before = dict(entry.data)
    backend.gateways.side_effect = gateways
    backend.devices.side_effect = devices
    result = await connect_dialog(hass, entry)
    assert result["errors"] == {"base": key}
    assert result["step_id"] == (
        "cloud_auth" if key == "invalid_auth" else "cloud_retry"
    )
    assert dict(entry.data) == before
    if isinstance(devices, BaconError) and devices.category == "rate_limited":
        # The announced pause blocks the retry without a request.
        calls = backend.devices.await_count
        shown = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert shown["errors"] == {"base": "cloud_rate_limited"}
        assert backend.devices.await_count == calls


async def test_a_new_sign_in_without_any_system_is_reported(hass, backend) -> None:
    entry = empty_entry(hass)
    backend.gateways.return_value = ()
    backend.devices.return_value = ((), "account")
    result = await connect_dialog(hass, entry)
    assert result["step_id"] == "cloud_retry"
    assert result["errors"] == {"base": "no_gateways"}


async def test_the_listing_of_a_replaced_dialog_aborts(hass, backend) -> None:
    entry = empty_entry(hass)
    result = await connect_dialog(hass, entry, authenticate=False)
    with patch(
        FLOW + ".check_new_cloud",
        side_effect=[None, ConfigurationError("cloud_already_connected")],
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
        )
    assert await outcome(hass, result) == "cloud_already_connected"


async def test_selection_of_a_new_access_needs_its_discovery(hass, backend) -> None:
    entry = empty_entry(hass)
    result = await connect_dialog(hass, entry)
    assert result["step_id"] == "cloud_select"
    flow = _flow(hass, result)
    flow._cloud_discovery = None
    shown = await flow.async_step_cloud_select({"gateway_ids": ["000123"]})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "configuration_changed"


@pytest.mark.parametrize("changed_on_call", [1, 2])
async def test_selection_after_a_changed_ownership_registers_nothing(
    hass, backend, changed_on_call
) -> None:
    entry = empty_entry(hass)
    result = await connect_dialog(hass, entry)
    flow = _flow(hass, result)
    before = dict(entry.data)
    real = ConfigurationManagement.current
    calls = {"n": 0}

    def current(self):
        calls["n"] += 1
        if calls["n"] == changed_on_call:
            return SimpleNamespace(to_data=lambda: {"changed": True})
        return real(self)

    with (
        patch(MANAGEMENT + ".current", current),
        patch(FLOW + ".verify_local_first_merges", AsyncMock(return_value={})),
    ):
        shown = await flow.async_step_cloud_select({"gateway_ids": ["000123"]})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "configuration_changed"
    assert dict(entry.data) == before


@pytest.mark.parametrize(
    "error,key",
    [
        (BaconError("invalid_identifier"), "invalid_auth"),
        (RateLimited(None), "cloud_rate_limited"),
        (PointTError("down"), "cannot_connect"),
    ],
)
async def test_failed_registration_keeps_the_selection_open(
    hass, backend, error, key
) -> None:
    entry = empty_entry(hass)
    result = await connect_dialog(hass, entry)
    before = dict(entry.data)
    with patch(FLOW + ".verify_local_first_merges", AsyncMock(side_effect=error)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"gateway_ids": ["000123"]}
        )
    assert result["step_id"] == "cloud_select"
    assert result["errors"] == {"base": key}
    assert dict(entry.data) == before


async def test_a_rejected_token_during_registration_asks_for_a_new_sign_in(
    hass, backend
) -> None:
    entry = empty_entry(hass)
    result = await connect_dialog(hass, entry)
    with patch(
        FLOW + ".verify_local_first_merges",
        AsyncMock(side_effect=AuthenticationError()),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"gateway_ids": ["000123"]}
        )
    assert result["step_id"] == "cloud_auth"
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.parametrize("error", [BaconError("unavailable"), PointTError("down")])
async def test_connection_listing_failures_offer_a_retry(hass, backend, error) -> None:
    entry, cloud, _ = existing(hass)
    backend.devices.side_effect = error
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    assert result["step_id"] == "connection_discover_retry"
    assert result["errors"] == {"base": "cannot_connect"}
    # A retry asks again: the cloud answers now.
    backend.devices.side_effect = None
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "connection_discover"


async def test_connection_listing_asked_to_wait_shows_the_end_of_the_pause(
    hass, backend
) -> None:
    entry, cloud, _ = existing(hass)
    backend.gateways.side_effect = RateLimited(retry_after=600)
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    assert result["step_id"] == "connection_discover_retry"
    assert result["errors"] == {"base": "cloud_rate_limited_until"}
    assert result["description_placeholders"]["until"]


@pytest.mark.parametrize("seconds", [None, 0])
async def test_connection_listing_pause_without_an_end(hass, backend, seconds) -> None:
    entry, cloud, _ = existing(hass)
    backend.gateways.side_effect = RateLimited(retry_after=seconds)
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    assert result["step_id"] == "connection_discover_retry"
    assert result["errors"] == {"base": "cloud_rate_limited"}


async def test_connection_selection_failure_unrelated_to_the_plants_aborts(
    hass, backend
) -> None:
    entry, cloud, _ = existing(hass)
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    with patch(
        FLOW + ".verify_local_first_merges",
        AsyncMock(side_effect=ConfigurationError("unexpected")),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"gateway_ids": ["new-gateway"]}
        )
    assert await outcome(hass, result) == "configuration_changed"


async def test_connection_listing_without_an_edit_aborts(hass, backend) -> None:
    entry, cloud, _ = existing(hass)
    result = await choose_connection(hass, entry, cloud)
    flow = _flow(hass, result)
    flow._connection_edit = None
    shown = await flow.async_step_connection_discover()
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "configuration_changed"


async def test_an_empty_selection_of_a_new_access_asks_for_a_system(
    hass, backend
) -> None:
    entry = empty_entry(hass)
    result = await connect_dialog(hass, entry)
    before = dict(entry.data)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"gateway_ids": []}
    )
    assert result["step_id"] == "cloud_select"
    assert result["errors"] == {"base": "select_gateway"}
    assert dict(entry.data) == before


async def test_a_blocked_selection_with_a_local_first_reason_stays_open(
    hass, backend
) -> None:
    entry, cloud, _ = existing(hass)
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    before = dict(entry.data)
    with patch(
        FLOW + ".verify_local_first_merges",
        AsyncMock(side_effect=ConfigurationError("cloud_local_first_unverified")),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"gateway_ids": ["new-gateway"]}
        )
    assert result["step_id"] == "connection_discover"
    assert result["errors"] == {"base": "cloud_local_first_unverified"}
    assert dict(entry.data) == before


async def test_a_damaged_configuration_offers_no_second_cloud(hass, backend) -> None:
    entry = empty_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow = _flow(hass, result)
    assert flow.cloud_add_available() is True
    with patch(MANAGEMENT + ".current", side_effect=ConfigurationError("unreadable")):
        assert flow.cloud_add_available() is False
