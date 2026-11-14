"""A pasted address of the SingleKey ID page gets its own hint (#127)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant import config_entries

from custom_components.bosch_buderus_heating.cloud_auth_flow import is_sign_in_page
from custom_components.bosch_buderus_heating.const import CONF_REDIRECT_URL, DOMAIN
from custom_components.bosch_buderus_heating.local_setup_history import (
    cloud_sign_in_history,
)
from custom_components.bosch_buderus_heating.pointt import Brand
from tests.test_cloud_setup import backend as backend
from tests.test_cloud_setup import connect_dialog, empty_entry
from tests.test_config_flow import _redirect_from_result

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

PACKAGE = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
PAGE = "https://singlekey-id.com/redirection?f=abc123&lang=de"


@pytest.mark.parametrize(
    "address",
    [
        PAGE,
        "  https://singlekey-id.com/auth/connect/authorize?x=1 \n",
        "https://www.singlekey-id.com/redirection?f=1",
        "https://SingleKey-ID.com./redirection",
        "https://login.singlekey-id.com/",
    ],
)
def test_the_sign_in_page_is_recognised(address: str) -> None:
    assert is_sign_in_page(address)


@pytest.mark.parametrize(
    "address",
    [
        f"{Brand.BOSCH.redirect_uri}?code=x&state=y",
        f"{Brand.BUDERUS.redirect_uri}?code=x&state=y",
        "https://notsinglekey-id.com/redirection",
        "https://singlekey-id.com.example.org/redirection",
        "singlekey-id.com/redirection",
        "http://[broken",
        "",
        None,
    ],
)
def test_other_addresses_are_not_the_sign_in_page(address: object) -> None:
    assert not is_sign_in_page(address)


def _link(result) -> str:
    return result["description_placeholders"]["authorization_url"]


async def test_setup_names_the_sign_in_page_and_keeps_the_link(hass, backend) -> None:
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    first = await manager.async_configure(result["flow_id"], {"brand": "buderus"})
    result = await manager.async_configure(first["flow_id"], {CONF_REDIRECT_URL: PAGE})
    assert result["step_id"] == "auth"
    assert result["errors"] == {"base": "singlekey_page"}
    # The scheme the hint names is the one of this attempt.
    assert result["description_placeholders"]["redirect_scheme"]
    assert _link(result) == _link(first)
    backend.oauth.assert_not_awaited()
    # The address of the same attempt still works.
    result = await manager.async_configure(
        first["flow_id"],
        {CONF_REDIRECT_URL: _redirect_from_result(first, Brand.BUDERUS)},
    )
    assert result["step_id"] == "gateways"


async def test_options_sign_in_names_the_sign_in_page(hass, backend) -> None:
    entry = empty_entry(hass)
    first = await connect_dialog(hass, entry, authenticate=False)
    manager = hass.config_entries.options
    result = await manager.async_configure(first["flow_id"], {CONF_REDIRECT_URL: PAGE})
    assert result["step_id"] == "cloud_auth"
    assert result["errors"] == {"base": "singlekey_page"}
    assert _link(result) == _link(first)
    backend.oauth.assert_not_awaited()
    await hass.async_block_till_done()
    attempts = await cloud_sign_in_history(hass, entry.entry_id).async_attempts()
    assert [(a["step"], a["error"]) for a in attempts] == [
        ("redirect_submitted", "singlekey_page")
    ]
    # Codes only: the pasted address is never kept.
    assert "abc123" not in json.dumps(attempts)
    result = await manager.async_configure(
        first["flow_id"],
        {CONF_REDIRECT_URL: _redirect_from_result(first, Brand.BUDERUS)},
    )
    assert result["step_id"] == "cloud_select"


async def test_a_dialog_closed_after_the_hint_still_has_an_open_link(
    hass, backend
) -> None:
    entry = empty_entry(hass)
    first = await connect_dialog(hass, entry, authenticate=False)
    manager = hass.config_entries.options
    await manager.async_configure(first["flow_id"], {CONF_REDIRECT_URL: PAGE})
    manager.async_abort(first["flow_id"])
    await hass.async_block_till_done()
    attempts = await cloud_sign_in_history(hass, entry.entry_id).async_attempts()
    assert [(a["step"], a["error"]) for a in attempts] == [
        ("redirect_submitted", "singlekey_page"),
        ("link_created", "abandoned_after_link"),
    ]


def test_the_hint_names_the_app_scheme_in_every_language() -> None:
    for path in [PACKAGE / "strings.json", *(PACKAGE / "translations").glob("*.json")]:
        strings = json.loads(path.read_text("utf-8"))
        text = strings["config"]["error"]["singlekey_page"]
        assert "{redirect_scheme}://" in text, path.name
        assert "Failed to launch" in text, path.name
        assert strings["options"]["error"]["singlekey_page"] == text, path.name
