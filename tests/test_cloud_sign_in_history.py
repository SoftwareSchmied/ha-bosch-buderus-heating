"""The last cloud sign-in attempts of an entry: steps, codes and times only."""

import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating import cloud_auth_flow
from custom_components.bosch_buderus_heating.const import (
    CONF_BRAND,
    CONF_GATEWAY_IDS,
    CONF_REDIRECT_URL,
    DOMAIN,
)
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.local_setup_history import (
    cloud_sign_in_history,
    local_setup_history,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    Brand,
    Gateway,
    RequestTimeout,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_cloud_setup import backend as backend
from tests.test_cloud_setup import connect_dialog, empty_entry
from tests.test_config_flow import _redirect_from_result

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

ROOT = "custom_components.bosch_buderus_heating.config_flow"


def _shape(attempts):
    return [(a["step"], a["result"], a["error"]) for a in attempts]


def _own(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == cloud_auth_flow.__name__ and r.levelno == logging.INFO
    ]


async def _attempts(hass, entry_id):
    await hass.async_block_till_done()
    return await cloud_sign_in_history(hass, entry_id).async_attempts()


def _reauth_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="buderus:test",
        data={
            CONF_BRAND: Brand.BUDERUS.value,
            CONF_GATEWAY_IDS: ["gateway-one"],
            **tokens_to_data(AuthTokens("old-access", "old-refresh", 1000.0)),
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _reauth_link(hass, entry):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=dict(entry.data),
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "auth"
    return result


async def test_a_first_setup_keeps_its_attempts_in_the_new_entry(hass, backend, caplog):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"brand": "buderus"}
    )
    link = result["description_placeholders"]["authorization_url"]
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    with caplog.at_level(logging.INFO):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_REDIRECT_URL: redirect.replace("state=", "state=x")},
        )
        assert result["errors"] == {"base": "invalid_redirect"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )
        assert result["step_id"] == "gateways"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"gateway_ids": ["000123"], "bacon_device_ids": []}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    attempts = await _attempts(hass, entry.entry_id)
    assert _shape(attempts) == [
        ("redirect_submitted", "failed", "state_mismatch"),
        ("success", "succeeded", None),
    ]
    assert _own(caplog) == [
        "A cloud sign-in attempt failed in step redirect_submitted: state_mismatch"
    ]
    report = await async_get_config_entry_diagnostics(hass, entry)
    assert report["cloud_sign_in_attempts"] == attempts
    # Kept apart from the local setup attempts.
    assert report["local_setup_attempts"] == []
    text = json.dumps(report, default=str) + json.dumps(attempts) + str(_own(caplog))
    state = redirect.split("state=")[1]
    for secret in (link, redirect, state, "one-time-code", "000123", "@"):
        assert secret not in json.dumps(attempts) + str(_own(caplog)), secret
    assert "one-time-code" not in text and state not in text


async def test_a_closed_dialog_after_the_link_is_its_own_entry(hass) -> None:
    entry = _reauth_entry(hass)
    result = await _reauth_link(hass, entry)
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert _shape(await _attempts(hass, entry.entry_id)) == [
        ("link_created", "failed", "abandoned_after_link")
    ]


async def test_a_first_setup_without_an_entry_keeps_nothing(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"brand": "buderus"}
    )
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    ("visible", "expected"),
    [
        ("gateway-one", [("success", "succeeded", None)]),
        ("other", [("access_checked", "failed", "reauth_wrong_account")]),
    ],
)
async def test_a_renewed_sign_in_ends_with_its_access_check(hass, visible, expected):
    entry = _reauth_entry(hass)
    result = await _reauth_link(hass, entry)
    with (
        patch.object(hass.config_entries, "async_reload", AsyncMock()),
        patch(
            ROOT + ".OAuthClient.exchange_code",
            AsyncMock(return_value=AuthTokens("new-access", "new-refresh", 9000.0)),
        ),
        patch(
            ROOT + ".PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway(visible),)),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_REDIRECT_URL: _redirect_from_result(result, Brand.BUDERUS)},
        )
    assert result["type"] is FlowResultType.ABORT
    attempts = await _attempts(hass, entry.entry_id)
    assert _shape(attempts) == expected
    assert "new-access" not in json.dumps(attempts) and visible not in json.dumps(
        attempts
    )


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ValueError("bad"), "invalid_auth"),
        (RequestTimeout("slow"), "cannot_connect"),
    ],
)
async def test_a_refused_code_exchange_is_noted(hass, error, code):
    entry = _reauth_entry(hass)
    result = await _reauth_link(hass, entry)
    with patch(ROOT + ".OAuthClient.exchange_code", AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_REDIRECT_URL: _redirect_from_result(result, Brand.BUDERUS)},
        )
    assert _shape(await _attempts(hass, entry.entry_id)) == [
        ("code_exchanged", "failed", code)
    ]


async def test_an_unexpected_error_names_only_its_type(hass) -> None:
    entry = _reauth_entry(hass)
    result = await _reauth_link(hass, entry)
    with (
        patch(
            ROOT + ".OAuthClient.exchange_code",
            AsyncMock(side_effect=RuntimeError("secret detail")),
        ),
        pytest.raises(RuntimeError),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_REDIRECT_URL: _redirect_from_result(result, Brand.BUDERUS)},
        )
    attempts = await _attempts(hass, entry.entry_id)
    assert _shape(attempts) == [("code_exchanged", "failed", "unexpected:RuntimeError")]
    assert "secret" not in json.dumps(attempts)


async def test_adding_a_cloud_access_notes_its_attempts(hass, backend) -> None:
    entry = empty_entry(hass)
    result = await connect_dialog(hass, entry, authenticate=False)
    hass.config_entries.options.async_abort(result["flow_id"])
    result = await connect_dialog(hass, entry)
    assert result["type"] is FlowResultType.FORM
    hass.config_entries.options.async_abort(result["flow_id"])
    assert _shape(await _attempts(hass, entry.entry_id)) == [
        ("link_created", "failed", "abandoned_after_link"),
        ("access_checked", "failed", "abandoned_after_sign_in"),
    ]
    assert await local_setup_history(hass, entry.entry_id).async_attempts() == []


async def test_a_link_replaced_before_use_is_noted_at_the_next_attempt(hass):
    entry = _reauth_entry(hass)
    result = await _reauth_link(hass, entry)
    expired = iter([True])
    with patch.object(
        cloud_auth_flow.OAuthFlow,
        "is_expired",
        lambda self: next(expired, False),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["errors"] == {"base": "auth_expired"}
    assert _shape(await _attempts(hass, entry.entry_id)) == [
        ("link_created", "failed", "abandoned_after_link")
    ]


async def test_removing_the_entry_with_an_open_sign_in_keeps_no_history(
    hass,
) -> None:
    from custom_components.bosch_buderus_heating import local_setup_history as history

    entry = _reauth_entry(hass)
    await _reauth_link(hass, entry)
    # Home Assistant deletes the entry's data first and closes the open
    # sign-in afterwards: that must not bring the history back.
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    histories = hass.data.get(history._HISTORIES, {})
    assert (history.CLOUD, entry.entry_id) not in histories
