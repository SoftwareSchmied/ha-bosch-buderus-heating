"""Sign-in history failures and edge cases of the shared sign-in step, and the
recordings dialog."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.data_entry_flow import AbortFlow, FlowResultType

from custom_components.bosch_buderus_heating.cloud_auth_flow import (
    CloudAuthorizationFlow,
)
from custom_components.bosch_buderus_heating.config_flow import BoschBuderusOptionsFlow
from custom_components.bosch_buderus_heating.const import (
    CONF_GATEWAY_IDS,
    CONF_REDIRECT_URL,
    DOMAIN,
)
from custom_components.bosch_buderus_heating.pointt import AuthTokens, Brand
from custom_components.bosch_buderus_heating.recordings.manager import OPTION_IMPORT
from tests.ha_test_helpers import MockConfigEntry
from tests.test_config_flow import _redirect_from_result, _start_auth_flow

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

CF = "custom_components.bosch_buderus_heating.config_flow"
AUTH = "custom_components.bosch_buderus_heating.cloud_auth_flow"
TOKENS = AuthTokens("access", "refresh", expires_at=4000.0)


def _flow(hass, result):
    return hass.config_entries.flow._progress[result["flow_id"]]


async def test_a_failing_sign_in_history_never_stops_the_dialog(hass) -> None:
    result = await _start_auth_flow(hass)
    with patch(AUTH + ".cloud_sign_in_history", side_effect=RuntimeError("disk")):
        flow = _flow(hass, result)
        flow._reauth_entry = MockConfigEntry(domain=DOMAIN)
        flow._reauth_entry.add_to_hass(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: "not an address"}
        )
    assert result["step_id"] == "auth"
    assert result["errors"] == {"base": "invalid_redirect"}


async def test_attempts_of_a_first_setup_are_dropped_when_their_entry_is_missing(
    hass,
) -> None:
    result = await _start_auth_flow(hass)
    flow = _flow(hass, result)
    # The attempts noted before the entry existed.
    flow._auth_pending = [("2026-11-15 10:00:00", "redirect_submitted", None)]
    flow._auth_flush_pending()
    assert flow._auth_pending is None
    # A history that cannot be written does not break the end of the setup.
    flow._auth_pending = [("2026-11-15 10:00:00", "redirect_submitted", None)]
    entry = MockConfigEntry(domain=DOMAIN, unique_id="installation_" + flow.flow_id)
    entry.add_to_hass(hass)
    with patch(AUTH + ".cloud_sign_in_history", side_effect=RuntimeError("disk")):
        flow._auth_flush_pending()
    assert flow._auth_pending is None


async def test_the_shared_sign_in_needs_its_three_hooks() -> None:
    flow = CloudAuthorizationFlow()
    with pytest.raises(NotImplementedError):
        flow._auth_brand()
    with pytest.raises(NotImplementedError):
        flow._auth_service_failed(RuntimeError("x"))  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        await flow._auth_succeeded(TOKENS)
    assert await flow._auth_blocked(None) is None


async def test_a_dialog_without_its_brand_or_link_aborts(hass) -> None:
    result = await _start_auth_flow(hass)
    flow = _flow(hass, result)
    flow._oauth_flow = None
    shown = flow._show_auth_form()
    assert shown["type"] is FlowResultType.ABORT and shown["reason"] == "invalid_flow"
    # Without a link a new one is issued.
    shown = flow._show_or_start_authorization()
    assert shown["type"] is FlowResultType.FORM and shown["step_id"] == "auth"
    # The same link is shown again while it is usable.
    link = shown["description_placeholders"]["authorization_url"]
    again = flow._show_or_start_authorization()
    assert again["description_placeholders"]["authorization_url"] == link
    flow._oauth_flow = None
    flow._brand = None
    for shown in (
        flow._start_authorization(),
        flow._restart_authorization(error="invalid_auth"),
        await flow.async_step_auth({CONF_REDIRECT_URL: "x"}),
    ):
        assert shown["type"] is FlowResultType.ABORT
        assert shown["reason"] == "invalid_flow"


async def test_an_abort_while_the_access_is_checked_ends_the_attempt_with_its_reason(
    hass, caplog
) -> None:
    result = await _start_auth_flow(hass)
    flow = _flow(hass, result)
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    with (
        patch(CF + ".OAuthClient.exchange_code", AsyncMock(return_value=TOKENS)),
        patch.object(
            type(flow), "_auth_succeeded", AsyncMock(side_effect=AbortFlow("stop"))
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "stop"
    assert "failed in step access_checked: stop" in caplog.text


async def test_an_unexpected_error_in_the_exchange_is_noted_by_type_only(hass) -> None:
    result = await _start_auth_flow(hass)
    flow = _flow(hass, result)
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    with (
        patch(
            CF + ".OAuthClient.exchange_code",
            AsyncMock(side_effect=RuntimeError("secret detail")),
        ),
        pytest.raises(RuntimeError),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )
    noted = flow._auth_pending[-1]
    assert noted[1] == "code_exchanged"
    assert noted[2] == "unexpected:RuntimeError"
    assert "secret detail" not in repr(flow._auth_pending)


# --- recordings dialog --------------------------------------------------------


def _recordings_flow(hass, *, enabled):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GATEWAY_IDS: ["gateway"]},
        options={OPTION_IMPORT: True} if enabled else {},
    )
    entry.add_to_hass(hass)
    flow = BoschBuderusOptionsFlow(entry)
    flow.hass = hass
    return entry, flow


@pytest.mark.parametrize(
    "before,after,runs",
    [(False, True, 1), (True, True, 0), (True, False, 0), (False, False, 0)],
)
async def test_switching_the_import_requests_a_run_only_when_it_is_newly_on(
    hass, before, after, runs
) -> None:
    entry, flow = _recordings_flow(hass, enabled=before)
    manager = SimpleNamespace(request_run=MagicMock())
    with patch(
        "custom_components.bosch_buderus_heating.recordings_flow.managers_of",
        return_value=[manager],
    ):
        shown = await flow.async_step_recordings({OPTION_IMPORT: after})
    assert entry.options.get(OPTION_IMPORT) is after
    assert manager.request_run.call_count == runs
    assert shown["step_id"] == (
        "result_recordings_import_on" if after else "result_recordings_import_off"
    )
