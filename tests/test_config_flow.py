"""Tests for the Bosch/Buderus Heating config flow."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.config_flow import (
    CONF_HOLIDAY_ASSIGNED_TO,
    CONF_HOLIDAY_DHW_MODE,
    CONF_HOLIDAY_FIX_TEMPERATURE,
    CONF_HOLIDAY_HEATING_MODE,
    CONF_HOLIDAY_PERIOD,
    CONF_HOLIDAY_THERMAL_DISINFECTION,
    CONF_HOLIDAY_VENTILATION_MODE,
)
from custom_components.bosch_buderus_heating.const import (
    CONF_ACCESS_TOKEN,
    CONF_BRAND,
    CONF_GATEWAY_IDS,
    CONF_POLLING_PROFILE,
    CONF_REDIRECT_URL,
    DOMAIN,
    PollingProfile,
    polling_profile_from_options,
)
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.holidays import (
    HOLIDAY_CONFIGURATION_PATH,
    HOLIDAY_LIST_PATH,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    AuthTokens,
    Brand,
    Gateway,
    InvalidPayload,
    RateLimited,
    RequestTimeout,
    Resource,
    ServiceUnavailable,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData


async def _start_auth_flow(
    hass: HomeAssistant, brand: Brand = Brand.BUDERUS
) -> dict[str, object]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BRAND: brand.value}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "auth"
    return result


def _redirect_from_result(result: dict[str, object], brand: Brand) -> str:
    placeholders = result["description_placeholders"]
    assert isinstance(placeholders, dict)
    authorization_url = placeholders["authorization_url"]
    assert isinstance(authorization_url, str)
    state = parse_qs(urlparse(authorization_url).query)["state"][0]
    return f"{brand.redirect_uri}?code=one-time-code&state={state}"


async def test_user_flow_creates_entry_after_gateway_selection(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    result = await _start_auth_flow(hass)
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    tokens = AuthTokens("access", "refresh", expires_at=4000.0)
    gateways = (
        Gateway("gateway-one", model="Logatherm"),
        Gateway("gateway-two", device_type="heatpump"),
    )

    with (
        patch(
            "custom_components.bosch_buderus_heating.config_flow.OAuthClient.exchange_code",
            AsyncMock(return_value=tokens),
        ),
        patch(
            "custom_components.bosch_buderus_heating.config_flow.PointTClient.get_gateways",
            AsyncMock(return_value=gateways),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateways"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GATEWAY_IDS: ["gateway-one"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Buderus Heating"
    assert result["data"][CONF_BRAND] == Brand.BUDERUS.value
    assert result["data"][CONF_GATEWAY_IDS] == ["gateway-one"]
    assert result["data"][CONF_ACCESS_TOKEN] == "access"
    assert "gateway-one" not in result["result"].unique_id


async def test_invalid_redirect_stays_in_auth_step(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    result = await _start_auth_flow(hass, Brand.BOSCH)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_REDIRECT_URL: (
                f"{Brand.BOSCH.redirect_uri}?code=code&state=wrong-state"
            )
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "auth"
    assert result["errors"] == {"base": "invalid_redirect"}


async def test_gateway_discovery_can_retry_without_new_login(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    result = await _start_auth_flow(hass)
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    tokens = AuthTokens("access", "refresh", expires_at=4000.0)
    get_gateways = AsyncMock(
        side_effect=[ServiceUnavailable(), (Gateway("gateway-one"),)]
    )

    with (
        patch(
            "custom_components.bosch_buderus_heating.config_flow.OAuthClient.exchange_code",
            AsyncMock(return_value=tokens),
        ),
        patch(
            "custom_components.bosch_buderus_heating.config_flow.PointTClient.get_gateways",
            get_gateways,
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )
        assert result["step_id"] == "retry"
        assert result["errors"] == {"base": "cannot_connect"}

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateways"
    assert get_gateways.await_count == 2


async def test_flow_aborts_when_account_has_no_gateways(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    result = await _start_auth_flow(hass)
    redirect = _redirect_from_result(result, Brand.BUDERUS)

    with (
        patch(
            "custom_components.bosch_buderus_heating.config_flow.OAuthClient.exchange_code",
            AsyncMock(return_value=AuthTokens("access", "refresh", 4000.0)),
        ),
        patch(
            "custom_components.bosch_buderus_heating.config_flow.PointTClient.get_gateways",
            AsyncMock(return_value=()),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_gateways"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthenticationError("rejected"), "invalid_auth"),
        (RequestTimeout("timeout"), "cannot_connect"),
        (InvalidPayload("bad response"), "unknown"),
    ],
)
async def test_token_exchange_errors_restart_authorization(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    error: Exception,
    expected: str,
) -> None:
    result = await _start_auth_flow(hass)
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    with patch(
        "custom_components.bosch_buderus_heating.config_flow.OAuthClient.exchange_code",
        AsyncMock(side_effect=error),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )

    assert result["step_id"] == "auth"
    assert result["errors"] == {"base": expected}


async def test_token_exchange_requires_refresh_token(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    result = await _start_auth_flow(hass)
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    with patch(
        "custom_components.bosch_buderus_heating.config_flow.OAuthClient.exchange_code",
        AsyncMock(return_value=AuthTokens("access", expires_at=4000.0)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_gateway_selection_validates_input_and_existing_entries(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BRAND: Brand.BUDERUS.value,
            CONF_GATEWAY_IDS: ["gateway-one"],
        },
    )
    existing.add_to_hass(hass)
    result = await _start_auth_flow(hass)
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    with (
        patch(
            "custom_components.bosch_buderus_heating.config_flow.OAuthClient.exchange_code",
            AsyncMock(return_value=AuthTokens("access", "refresh", expires_at=4000.0)),
        ),
        patch(
            "custom_components.bosch_buderus_heating.config_flow.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("gateway-one"),)),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )

    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_GATEWAY_IDS: []}
    )
    assert result["errors"] == {"base": "select_gateway"}
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_GATEWAY_IDS: ["gateway-one"]}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("error", "step", "expected"),
    [
        (AuthenticationError("rejected"), "auth", "invalid_auth"),
        (InvalidPayload("bad response"), "retry", "unknown"),
    ],
)
async def test_gateway_discovery_maps_additional_errors(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    error: Exception,
    step: str,
    expected: str,
) -> None:
    result = await _start_auth_flow(hass)
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    with (
        patch(
            "custom_components.bosch_buderus_heating.config_flow.OAuthClient.exchange_code",
            AsyncMock(return_value=AuthTokens("access", "refresh", expires_at=4000.0)),
        ),
        patch(
            "custom_components.bosch_buderus_heating.config_flow.PointTClient.get_gateways",
            AsyncMock(side_effect=error),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )
    assert result["step_id"] == step
    assert result["errors"] == {"base": expected}


async def test_reauthentication_updates_tokens(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    original = AuthTokens("old-access", "old-refresh", expires_at=1000.0)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="buderus:test",
        data={
            CONF_BRAND: Brand.BUDERUS.value,
            CONF_GATEWAY_IDS: ["gateway-one"],
            **tokens_to_data(original),
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=dict(entry.data),
    )
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "auth"

    redirect = _redirect_from_result(result, Brand.BUDERUS)
    renewed = AuthTokens("new-access", "new-refresh", expires_at=9000.0)
    with (
        patch(
            "custom_components.bosch_buderus_heating.config_flow.OAuthClient.exchange_code",
            AsyncMock(return_value=renewed),
        ),
        patch(
            "custom_components.bosch_buderus_heating.config_flow.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("gateway-one"),)),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == "new-access"


def _reconfigure_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="buderus:test",
        data={
            CONF_BRAND: Brand.BUDERUS.value,
            CONF_GATEWAY_IDS: ["gateway-one"],
            **tokens_to_data(
                AuthTokens("access", "refresh", expires_at=4_000_000_000.0)
            ),
        },
    )
    entry.add_to_hass(hass)
    return entry


def _entry_with_writable_holiday(hass: HomeAssistant) -> tuple[MockConfigEntry, object]:
    entry = _reconfigure_entry(hass)
    coordinator = SimpleNamespace(
        hass=hass,
        gateway=Gateway("gateway-one", model="K40"),
        data={
            HOLIDAY_LIST_PATH: SimpleNamespace(
                resource=Resource(
                    path=HOLIDAY_LIST_PATH,
                    value=[
                        {
                            "id": 7,
                            "startDate": "2030-08-01T08:00:00",
                            "endDate": "2030-08-08T18:00:00",
                            "heatingMode": "FIX_TEMPERATURE",
                            "dhwMode": "OFF",
                            "ventilationMode": "NOM",
                            "assignedTo": ["hc1", "dhw1", "vent1"],
                            "name": "VXJsYXVi",
                            "thermalDesinfection": "ON",
                            "fixTemperature": 17.0,
                        }
                    ],
                    has_value=True,
                ),
                available=True,
                freshness=Freshness.FRESH,
            ),
            HOLIDAY_CONFIGURATION_PATH: SimpleNamespace(
                resource=Resource(
                    path=HOLIDAY_CONFIGURATION_PATH,
                    value={
                        "values": {
                            "date": {"allowedValues": ["dateTime"]},
                            "heatingMode": {
                                "allowedValues": [
                                    "SATURDAY",
                                    "FIX_TEMPERATURE",
                                    "OFF",
                                    "ECO",
                                ]
                            },
                            "dhwMode": {
                                "allowedValues": [
                                    "SATURDAY",
                                    "OFF",
                                    "ECO",
                                    "LOW",
                                    "HIGH",
                                    "OFF_TD",
                                ]
                            },
                            "ventilationMode": {
                                "allowedValues": [
                                    "SATURDAY",
                                    "OFF",
                                    "MIN",
                                    "RED",
                                    "NOM",
                                    "MAX",
                                    "DEM",
                                ]
                            },
                            "assignedTo": {"allowedValues": ["hc1", "dhw1", "vent1"]},
                            "thermalDesinfection": {"allowedValues": ["ON", "OFF"]},
                            "fixTemperature": {
                                "minValue": 10.0,
                                "maxValue": 25.0,
                            },
                            "name": {
                                "stringConfig": {
                                    "codingType": "BASE64",
                                    "charset": "UTF8",
                                    "maxLength": 32,
                                }
                            },
                        }
                    },
                    has_value=True,
                ),
                available=True,
                freshness=Freshness.FRESH,
            ),
        },
        async_update_holiday=AsyncMock(),
    )
    entry.runtime_data = BoschBuderusRuntimeData(
        client=AsyncMock(),
        token_manager=AsyncMock(),
        gateways=(coordinator.gateway,),
        coordinators=(coordinator,),
    )
    return entry, coordinator


async def test_options_flow_configures_only_advertised_holiday_fields(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    selection = hashlib.sha256(b"gateway-one\x007").hexdigest()[:24]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HOLIDAY_PERIOD: selection}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "holiday"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_HOLIDAY_ASSIGNED_TO: ["hc1", "vent1"],
            CONF_HOLIDAY_HEATING_MODE: "eco",
            CONF_HOLIDAY_DHW_MODE: "low",
            CONF_HOLIDAY_VENTILATION_MODE: "dem",
            CONF_HOLIDAY_THERMAL_DISINFECTION: "off",
            CONF_HOLIDAY_FIX_TEMPERATURE: 18.5,
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "holiday_updated"
    holiday_id, values = coordinator.async_update_holiday.await_args.args
    assert holiday_id == 7
    assert values.assigned_to == ("hc1", "vent1")
    assert values.heating_mode == "ECO"
    assert values.dhw_mode == "LOW"
    assert values.ventilation_mode == "DEM"
    assert values.thermal_disinfection == "OFF"
    assert values.fix_temperature == 18.5
    assert values.start_date == "2030-08-01T08:00:00"
    assert values.end_date == "2030-08-08T18:00:00"
    assert values.name == "VXJsYXVi"


async def test_options_flow_rejects_stale_or_missing_holidays(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.data[HOLIDAY_LIST_PATH].freshness = Freshness.STALE

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_writable_holidays"


async def test_options_flow_requires_a_loaded_entry(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _reconfigure_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"


async def test_options_flow_rejects_a_changed_selection(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    selection = hashlib.sha256(b"gateway-one\x007").hexdigest()[:24]
    original = coordinator.data[HOLIDAY_LIST_PATH].resource.value[0]
    coordinator.data[HOLIDAY_LIST_PATH].resource = Resource(
        path=HOLIDAY_LIST_PATH,
        value=[{**original, "id": 8}],
        has_value=True,
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HOLIDAY_PERIOD: selection}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "holiday_changed"}


async def test_options_flow_stops_if_selected_holiday_disappears(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    selection = hashlib.sha256(b"gateway-one\x007").hexdigest()[:24]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HOLIDAY_PERIOD: selection}
    )
    coordinator.data[HOLIDAY_LIST_PATH].resource = Resource(
        path=HOLIDAY_LIST_PATH, value=[], has_value=True
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_HOLIDAY_ASSIGNED_TO: ["hc1"],
            CONF_HOLIDAY_HEATING_MODE: "off",
            CONF_HOLIDAY_DHW_MODE: "off",
            CONF_HOLIDAY_VENTILATION_MODE: "off",
            CONF_HOLIDAY_THERMAL_DISINFECTION: "off",
            CONF_HOLIDAY_FIX_TEMPERATURE: 17.0,
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "holiday_changed"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (WriteNotConfirmed("not confirmed"), "write_not_confirmed"),
        (RateLimited(60), "write_rate_limited"),
        (AuthenticationError("expired"), "write_authentication_failed"),
        (WriteValidationError("changed"), "write_validation_failed"),
        (InvalidPayload("bad"), "write_failed"),
    ],
)
async def test_options_flow_reports_holiday_write_errors(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    error: Exception,
    expected: str,
) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_update_holiday.side_effect = error
    result = await hass.config_entries.options.async_init(entry.entry_id)
    selection = hashlib.sha256(b"gateway-one\x007").hexdigest()[:24]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HOLIDAY_PERIOD: selection}
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_HOLIDAY_ASSIGNED_TO: ["hc1", "dhw1", "vent1"],
            CONF_HOLIDAY_HEATING_MODE: "fix_temperature",
            CONF_HOLIDAY_DHW_MODE: "off",
            CONF_HOLIDAY_VENTILATION_MODE: "nom",
            CONF_HOLIDAY_THERMAL_DISINFECTION: "on",
            CONF_HOLIDAY_FIX_TEMPERATURE: 17.0,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "holiday"
    assert result["errors"] == {"base": expected}


async def _start_reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry, get_gateways: AsyncMock
) -> dict[str, object]:
    with patch(
        "custom_components.bosch_buderus_heating.config_flow.PointTClient.get_gateways",
        get_gateways,
    ):
        return await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
            data=dict(entry.data),
        )


async def test_reconfigure_saves_profile_and_forces_rediscovery(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _reconfigure_entry(hass)
    gateways = (Gateway("gateway-one", model="K40"), Gateway("gateway-two"))
    result = await _start_reconfigure(hass, entry, AsyncMock(return_value=gateways))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_BRAND: Brand.BUDERUS.value,
                CONF_GATEWAY_IDS: ["gateway-one", "gateway-two"],
                CONF_POLLING_PROFILE: PollingProfile.CLOUD_FRIENDLY.value,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_GATEWAY_IDS] == ["gateway-one", "gateway-two"]
    assert entry.options[CONF_POLLING_PROFILE] == PollingProfile.CLOUD_FRIENDLY.value
    reload_entry.assert_called_once_with(entry.entry_id)


async def test_reconfigure_validates_gateway_selection(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _reconfigure_entry(hass)
    result = await _start_reconfigure(
        hass, entry, AsyncMock(return_value=(Gateway("gateway-one"),))
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BRAND: Brand.BUDERUS.value,
            CONF_GATEWAY_IDS: [],
            CONF_POLLING_PROFILE: PollingProfile.STANDARD.value,
        },
    )
    assert result["errors"] == {"base": "select_gateway"}


@pytest.mark.parametrize(
    ("error", "step", "expected"),
    [
        (AuthenticationError("expired"), None, "reauth_required"),
        (ServiceUnavailable(), "reconfigure_retry", "cannot_connect"),
        (InvalidPayload("bad"), "reconfigure_retry", "unknown"),
    ],
)
async def test_reconfigure_discovery_errors(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    error: Exception,
    step: str | None,
    expected: str,
) -> None:
    result = await _start_reconfigure(
        hass, _reconfigure_entry(hass), AsyncMock(side_effect=error)
    )
    if step is None:
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == expected
    else:
        assert result["step_id"] == step
        assert result["errors"] == {"base": expected}


async def test_reconfigure_retry_and_no_gateways(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _reconfigure_entry(hass)
    get_gateways = AsyncMock(
        side_effect=[ServiceUnavailable(), (Gateway("gateway-one"),)]
    )
    with patch(
        "custom_components.bosch_buderus_heating.config_flow.PointTClient.get_gateways",
        get_gateways,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
            data=dict(entry.data),
        )
        assert result["step_id"] == "reconfigure_retry"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "reconfigure"

    result = await _start_reconfigure(
        hass, _reconfigure_entry(hass), AsyncMock(return_value=())
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_gateways"


async def test_reconfigure_brand_change_signs_in_and_updates_entry(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _reconfigure_entry(hass)
    result = await _start_reconfigure(
        hass, entry, AsyncMock(return_value=(Gateway("gateway-one"),))
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BRAND: Brand.BOSCH.value,
            CONF_GATEWAY_IDS: ["gateway-one"],
            CONF_POLLING_PROFILE: PollingProfile.CLOUD_FRIENDLY.value,
        },
    )
    assert result["step_id"] == "auth"
    redirect = _redirect_from_result(result, Brand.BOSCH)
    tokens = AuthTokens("bosch-access", "bosch-refresh", expires_at=9000.0)
    with (
        patch(
            "custom_components.bosch_buderus_heating.config_flow.OAuthClient.exchange_code",
            AsyncMock(return_value=tokens),
        ),
        patch(
            "custom_components.bosch_buderus_heating.config_flow."
            "PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("bosch-gateway", model="K40"),)),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )
    assert result["step_id"] == "gateways"

    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_GATEWAY_IDS: ["bosch-gateway"]}
        )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_BRAND] == Brand.BOSCH.value
    assert entry.data[CONF_ACCESS_TOKEN] == "bosch-access"
    assert entry.data[CONF_GATEWAY_IDS] == ["bosch-gateway"]


def test_polling_profile_tolerates_legacy_and_invalid_options() -> None:
    assert polling_profile_from_options({}) is PollingProfile.STANDARD
    assert (
        polling_profile_from_options({CONF_POLLING_PROFILE: "invalid"})
        is PollingProfile.STANDARD
    )
    assert (
        polling_profile_from_options(
            {CONF_POLLING_PROFILE: PollingProfile.CLOUD_FRIENDLY.value}
        )
        is PollingProfile.CLOUD_FRIENDLY
    )
