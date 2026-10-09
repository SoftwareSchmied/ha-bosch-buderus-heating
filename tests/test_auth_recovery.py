"""Recovery of expired authorization attempts and sign-in rate limits."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import (
    CONF_BRAND,
    CONF_GATEWAY_IDS,
    CONF_REDIRECT_URL,
    DOMAIN,
)
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    Brand,
    Gateway,
    RateLimited,
)

FLOW_MODULE = "custom_components.bosch_buderus_heating.config_flow"
AUTH_MODULE = "custom_components.bosch_buderus_heating.pointt.auth"


@pytest.fixture
def auth_clock():
    clock = Mock(return_value=1000.0)
    with (
        patch(f"{AUTH_MODULE}.monotonic", clock),
        patch(f"{FLOW_MODULE}.monotonic", clock, create=True),
    ):
        yield clock


async def _start(hass: HomeAssistant, brand: Brand = Brand.BUDERUS):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BRAND: brand.value}
    )


def _query(result):
    return parse_qs(
        urlparse(result["description_placeholders"]["authorization_url"]).query
    )


def _redirect(result, brand: Brand = Brand.BUDERUS):
    return (
        f"{brand.redirect_uri}?code=synthetic-code&state={_query(result)['state'][0]}"
    )


@pytest.mark.parametrize("brand", list(Brand))
@pytest.mark.parametrize("submit_expired", [False, True])
async def test_expired_attempt_gets_fresh_link_and_can_finish(
    hass: HomeAssistant, enable_custom_integrations, auth_clock, brand, submit_expired
):
    result = await _start(hass, brand)
    old_query = _query(result)
    old_redirect = _redirect(result, brand)
    auth_clock.return_value = 1600.001
    exchange = AsyncMock(return_value=AuthTokens("access", "refresh", 9000.0))
    with (
        patch(f"{FLOW_MODULE}.OAuthClient.exchange_code", exchange),
        patch(
            f"{FLOW_MODULE}.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("gateway-one"),)),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_REDIRECT_URL: old_redirect} if submit_expired else None,
        )
        assert result["step_id"] == "auth"
        assert result["errors"] == {"base": "auth_expired"}
        new_query = _query(result)
        for parameter in ("state", "nonce", "code_challenge"):
            assert new_query[parameter] != old_query[parameter]
        exchange.assert_not_awaited()

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: old_redirect}
        )
        assert result["errors"] == {"base": "invalid_redirect"}
        assert _query(result) == new_query
        exchange.assert_not_awaited()

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: _redirect(result, brand)}
        )
    assert result["step_id"] == "gateways"
    exchange.assert_awaited_once()


async def test_exact_expiry_boundary_still_accepts_matching_redirect(
    hass: HomeAssistant, enable_custom_integrations, auth_clock
):
    result = await _start(hass)
    auth_clock.return_value = 1600.0
    exchange = AsyncMock(return_value=AuthTokens("access", "refresh", 9000.0))
    with (
        patch(f"{FLOW_MODULE}.OAuthClient.exchange_code", exchange),
        patch(
            f"{FLOW_MODULE}.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("gateway-one"),)),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: _redirect(result)}
        )
    assert result["step_id"] == "gateways"
    exchange.assert_awaited_once()


@pytest.mark.parametrize("retry_after", [0.0, 120.0, 720.0, None])
async def test_rate_limit_waits_without_reusing_code_or_extending_deadline(
    hass: HomeAssistant, enable_custom_integrations, auth_clock, retry_after
):
    result = await _start(hass)
    old_query = _query(result)
    old_redirect = _redirect(result)
    delay = max(60.0, retry_after) if retry_after is not None else 300.0
    exchange = AsyncMock(
        side_effect=[RateLimited(retry_after), AuthTokens("access", "refresh", 9000.0)]
    )
    with (
        patch(f"{FLOW_MODULE}.OAuthClient.exchange_code", exchange),
        patch(
            f"{FLOW_MODULE}.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("gateway-one"),)),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: old_redirect}
        )
        assert result["step_id"] == "auth_wait"
        assert result["description_placeholders"] == {"seconds": str(int(delay))}
        auth_clock.return_value = 1000.0 + delay - 0.001
        for _ in range(2):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
            assert result["step_id"] == "auth_wait"
            assert result["description_placeholders"] == {"seconds": "1"}
        exchange.assert_awaited_once()

        auth_clock.return_value = 1000.0 + delay
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "auth"
        assert _query(result)["state"] != old_query["state"]
        exchange.assert_awaited_once()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: old_redirect}
        )
        assert result["errors"] == {"base": "invalid_redirect"}
        exchange.assert_awaited_once()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: _redirect(result)}
        )
    assert result["step_id"] == "gateways"
    assert exchange.await_count == 2


async def test_interrupted_exchange_can_restart_without_replaying_consumed_code(
    hass: HomeAssistant, enable_custom_integrations, auth_clock
):
    result = await _start(hass)
    original_query = _query(result)
    original_redirect = _redirect(result)
    exchange = AsyncMock(
        side_effect=[asyncio.CancelledError(), AuthTokens("access", "refresh", 9000.0)]
    )
    with (
        patch(f"{FLOW_MODULE}.OAuthClient.exchange_code", exchange),
        patch(
            f"{FLOW_MODULE}.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("gateway-one"),)),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_REDIRECT_URL: original_redirect}
            )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: original_redirect}
        )
        assert result["errors"] == {"base": "auth_restart"}
        assert _query(result)["state"] != original_query["state"]
        exchange.assert_awaited_once()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: _redirect(result)}
        )
    assert result["step_id"] == "gateways"
    assert exchange.await_count == 2


async def test_reauth_after_expiry_preserves_entry_devices_and_entities(
    hass: HomeAssistant, enable_custom_integrations, auth_clock
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="buderus:existing",
        data={
            CONF_BRAND: Brand.BUDERUS.value,
            CONF_GATEWAY_IDS: ["gateway-one"],
            **tokens_to_data(AuthTokens("old-access", "old-refresh", 1000.0)),
        },
        options={"polling_profile": "cloud_friendly"},
    )
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "gateway-one")}
    )
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "existing-temperature",
        config_entry=entry,
        device_id=device.id,
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=dict(entry.data),
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    auth_clock.return_value = 1700.0
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REDIRECT_URL: _redirect(result)}
    )
    assert result["errors"] == {"base": "auth_expired"}
    renewed = AuthTokens("new-access", "new-refresh", 9000.0)
    with (
        patch(
            f"{FLOW_MODULE}.OAuthClient.exchange_code", AsyncMock(return_value=renewed)
        ),
        patch(
            f"{FLOW_MODULE}.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("gateway-one"),)),
        ),
        patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload_entry,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: _redirect(result)}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.data == {
        CONF_BRAND: Brand.BUDERUS.value,
        CONF_GATEWAY_IDS: ["gateway-one"],
        **tokens_to_data(renewed),
    }
    assert entry.unique_id == "buderus:existing"
    assert entry.options == {"polling_profile": "cloud_friendly"}
    assert dr.async_get(hass).async_get(device.id) == device
    assert er.async_get(hass).async_get(entity.entity_id) == entity
    reload_entry.assert_awaited_once_with(entry.entry_id)
