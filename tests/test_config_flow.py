"""Tests for the Bosch/Buderus Home Comfort config flow."""

from __future__ import annotations

import base64
import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.config_flow import (
    CONF_HOLIDAY_ASSIGNED_TO,
    CONF_HOLIDAY_DHW_MODE,
    CONF_HOLIDAY_FIX_TEMPERATURE,
    CONF_HOLIDAY_HEATING_MODE,
    CONF_HOLIDAY_PERIOD,
    CONF_HOLIDAY_THERMAL_DISINFECTION,
    CONF_HOLIDAY_VENTILATION_MODE,
)
from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
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
from custom_components.bosch_buderus_heating.ha_schema import validation as vol
from custom_components.bosch_buderus_heating.holiday_writes import (
    HolidayAlreadyExists,
    HolidayChangedError,
    HolidayWriteNotConfirmed,
    _merge_holiday_changes,
)
from custom_components.bosch_buderus_heating.holidays import (
    HOLIDAY_CONFIGURATION_PATH,
    HOLIDAY_LIST_PATH,
    parse_holiday_state,
)
from custom_components.bosch_buderus_heating.i18n import dialog_config
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
from custom_components.bosch_buderus_heating.writes import WriteBusy
from tests.ha_test_helpers import MockConfigEntry
from tests.helpers.options_menu import outcome as flow_outcome


def _field_list(schema):
    """Serialize a form like the installed HA release sends it to the frontend."""
    from homeassistant.helpers import config_validation as cv

    try:
        from probatio import to_field_list
    except ModuleNotFoundError:  # Home Assistant before Probatio
        from voluptuous_serialize import convert

        return convert(schema, custom_serializer=cv.custom_serializer)
    return to_field_list(schema, custom_serializer=cv.custom_serializer)


async def _configure_options(hass, flow_id, user_input, *, action="save_holiday"):
    """Submit the native form or its direct delete action."""
    if action == "delete_holiday":
        user_input = {**user_input, "holiday_delete": True}
    return await hass.config_entries.options.async_configure(flow_id, user_input)


async def _open_new_holiday(hass, entry):
    result = await _start_holiday_options(hass, entry)
    return await _configure_options(
        hass, result["flow_id"], {CONF_HOLIDAY_PERIOD: "create"}
    )


def _new_holiday_input(result, **overrides):
    return {
        **_holiday_form_input(result),
        "holiday_name": "Trip",
        "holiday_start": "2030-08-01T08:00",
        "holiday_end": "2030-08-08T08:00",
        **overrides,
    }


async def _start_holiday_options(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["controls", "notifications", "holidays"]
    return await _configure_options(
        hass, result["flow_id"], {"next_step_id": "holidays"}
    )


async def _open_existing_holiday(
    hass, entry, *, gateway_id="gateway-one", action="holiday"
):
    result = await _start_holiday_options(hass, entry)
    selection = hashlib.sha256(f"{gateway_id}\x007".encode()).hexdigest()[:24]
    result = await _configure_options(
        hass, result["flow_id"], {CONF_HOLIDAY_PERIOD: selection}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "holiday"
    assert "menu_options" not in result
    if action == "delete_holiday":
        return await _configure_options(
            hass,
            result["flow_id"],
            _holiday_form_input(result),
            action="delete_holiday",
        )
    return result


def _holiday_form_input(result):
    values = {}
    for marker in result["data_schema"].schema:
        if marker.description and "suggested_value" in marker.description:
            values[marker.schema] = marker.description["suggested_value"]
        elif marker.default is not vol.UNDEFINED:
            values[marker.schema] = marker.default()
    return values


@pytest.mark.parametrize(
    ("operation", "outcome", "reason"),
    [
        ("create", "adjusted", "holiday_create_adjusted"),
        ("update", "adjusted", "holiday_update_adjusted"),
        ("create", "unknown", "holiday_create_unconfirmed"),
        ("create", "generic_unknown", "holiday_create_unconfirmed"),
        ("create", "duplicate", "holiday_already_exists"),
        ("create", "previous", "holiday_previous_request_found"),
    ],
)
async def test_holiday_write_feedback_explains_actual_state_and_closes_creation(
    hass, enable_custom_integrations, operation, outcome, reason
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = (
        await _open_new_holiday(hass, entry)
        if operation == "create"
        else await _open_existing_holiday(hass, entry)
    )
    values = (
        _new_holiday_input(result)
        if operation == "create"
        else _holiday_form_input(result)
    )
    values.update(holiday_start="2030-08-01T00:00", holiday_end="2030-08-09T00:00")

    async def fail(*args, **kwargs):
        desired = args[0] if operation == "create" else args[1]
        assert desired.start_date == "2030-08-01T00:00:00"
        assert desired.end_date == "2030-08-09T00:00:00"
        if outcome == "generic_unknown":
            raise WriteNotConfirmed()
        if outcome == "unknown":
            raise HolidayWriteNotConfirmed(None)
        readback = Resource(
            path=HOLIDAY_LIST_PATH,
            value=[
                {
                    "id": 7,
                    **replace(desired, end_date="2030-08-08T23:45:00").as_payload(),
                }
            ],
            has_value=True,
        )
        if outcome in ("duplicate", "previous"):
            raise HolidayAlreadyExists(readback, previous_request=outcome == "previous")
        actual = parse_holiday_state(
            {HOLIDAY_LIST_PATH: readback}, fallback_timezone=UTC
        ).periods[0]
        requested = parse_holiday_state(
            {
                HOLIDAY_LIST_PATH: replace(
                    readback, value=[{"id": 7, **desired.as_payload()}]
                )
            },
            fallback_timezone=UTC,
        ).periods[0]
        raise HolidayWriteNotConfirmed(readback, actual=actual, requested=requested)

    writer = AsyncMock(side_effect=fail)
    setattr(coordinator, f"async_{operation}_holiday", writer)
    result = await _configure_options(hass, result["flow_id"], values)
    assert result["type"] is FlowResultType.ABORT
    assert await flow_outcome(hass, result) == reason
    writer.assert_awaited_once()
    assert not hass.config_entries.options.async_progress()
    if outcome == "adjusted":
        assert result["description_placeholders"] == {
            "requested": "2030-08-01 00:00 / 2030-08-09 00:00",
            "actual": "2030-08-01 00:00 / 2030-08-08 23:45",
        }


async def _start_auth_flow(
    hass: HomeAssistant, brand: Brand = Brand.BUDERUS
) -> dict[str, object]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    # P49b step 7: the first step chooses the accesses.
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["cloud", "cloud_local", "local_only"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cloud"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BRAND: brand.value}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "auth"
    return result


@pytest.mark.parametrize("date_only", [False, True])
async def test_create_and_edit_share_the_same_native_form_fields(
    hass, enable_custom_integrations, date_only
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    if date_only:
        coordinator.data[HOLIDAY_CONFIGURATION_PATH].resource.value["values"]["date"][
            "allowedValues"
        ] = ["date"]
    existing = await _open_existing_holiday(hass, entry)
    creating = await _open_new_holiday(hass, entry)
    existing_fields = {
        marker.schema: selector.serialize()
        for marker, selector in existing["data_schema"].schema.items()
    }
    new_fields = {
        marker.schema: selector.serialize()
        for marker, selector in creating["data_schema"].schema.items()
    }
    assert existing_fields.pop("holiday_delete") == {
        "selector": {"bosch-buderus-holiday-delete": dialog_config("holiday")}
    }
    if not date_only:
        for field in ("holiday_start", "holiday_end"):
            assert existing_fields[field]["selector"][
                "bosch-buderus-holiday-datetime"
            ] == {"default_time": "", **dialog_config("holiday")}
            assert new_fields[field]["selector"]["bosch-buderus-holiday-datetime"] == {
                "default_time": "08:00",
                **dialog_config("holiday"),
            }
            existing_fields[field] = new_fields[field]
    assert existing_fields == new_fields
    assert "holiday_delete" not in new_fields
    assert "holiday_heating_mode" in new_fields
    assert "holiday_dhw_mode" in new_fields
    assert _holiday_form_input(creating)["holiday_start"] == ""
    assert _holiday_form_input(creating)["holiday_end"] == ""
    if not date_only:
        for field in ("holiday_start", "holiday_end"):
            assert "bosch-buderus-holiday-datetime" in new_fields[field]["selector"]
            assert len(_holiday_form_input(existing)[field]) == 16


@pytest.mark.parametrize("creating", [False, True])
@pytest.mark.parametrize("date_only", [False, True])
@pytest.mark.parametrize("same_end", [False, True])
async def test_end_before_or_equal_to_start_is_explained_without_writing(
    hass, enable_custom_integrations, creating, date_only, same_end
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_create_holiday = AsyncMock()
    if date_only:
        coordinator.data[HOLIDAY_CONFIGURATION_PATH].resource.value["values"]["date"][
            "allowedValues"
        ] = ["date"]
    result = (
        await _open_new_holiday(hass, entry)
        if creating
        else await _open_existing_holiday(hass, entry)
    )
    values = {
        **_holiday_form_input(result),
        "holiday_name": "My holiday",
        "holiday_start": "2030-10-28",
        "holiday_end": "2030-10-28" if same_end else "2030-10-27",
        "holiday_fix_temperature": 18.5,
    }
    if not date_only:
        values.update(
            holiday_start=values["holiday_start"] + "T08:00",
            holiday_end=values["holiday_end"] + "T08:00",
        )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], values
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"holiday_end": "holiday_end_not_after_start"}
    assert _holiday_form_input(result) == values
    coordinator.async_create_holiday.assert_not_awaited()
    coordinator.async_update_holiday.assert_not_awaited()
    coordinator.async_delete_holiday.assert_not_awaited()


@pytest.mark.parametrize("creating", [False, True])
async def test_common_form_saves_directly_without_an_action_menu(
    hass, enable_custom_integrations, creating
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_create_holiday = AsyncMock()
    result = (
        await _open_new_holiday(hass, entry)
        if creating
        else await _open_existing_holiday(hass, entry)
    )
    values = _new_holiday_input(
        result, holiday_name="Unsaved holiday", holiday_start="2030-08-01T10:15"
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], values
    )
    assert await flow_outcome(hass, result) == (
        "holiday_created" if creating else "holiday_updated"
    )
    writer = (
        coordinator.async_create_holiday
        if creating
        else coordinator.async_update_holiday
    )
    writer.assert_awaited_once()
    written = writer.await_args.args[0 if creating else 1]
    assert written.start_date == "2030-08-01T10:15:00"


async def test_cancel_delete_keeps_the_original_baseline_after_polling(
    hass, enable_custom_integrations
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    draft = {**_holiday_form_input(result), "holiday_dhw_mode": "low"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**draft, "holiday_delete": True}
    )
    original = coordinator.data[HOLIDAY_LIST_PATH].resource.value[0]
    coordinator.data[HOLIDAY_LIST_PATH].resource = Resource(
        path=HOLIDAY_LIST_PATH,
        value=[{**original, "fixTemperature": 19.0}],
        has_value=True,
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm_delete": False}
    )
    assert _holiday_form_input(result) == draft
    result = await _configure_options(hass, result["flow_id"], draft)
    assert await flow_outcome(hass, result) == "holiday_updated"
    call = coordinator.async_update_holiday.await_args
    baseline = call.kwargs["expected"]
    assert baseline.fix_temperature == 17.0
    merged = _merge_holiday_changes(
        baseline, call.args[1], replace(baseline, fix_temperature=19.0)
    )
    assert merged.fix_temperature == 19.0
    assert merged.dhw_mode == "LOW"


@pytest.mark.parametrize("creating", [False, True])
async def test_save_button_rechecks_capabilities_before_any_write(
    hass, enable_custom_integrations, creating
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_create_holiday = AsyncMock()
    result = (
        await _open_new_holiday(hass, entry)
        if creating
        else await _open_existing_holiday(hass, entry)
    )
    draft = _new_holiday_input(result, holiday_heating_mode="eco")
    coordinator.data[HOLIDAY_CONFIGURATION_PATH].resource.value["values"][
        "heatingMode"
    ]["allowedValues"] = ["OFF"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], draft)
    assert result["errors"] == {"base": "write_validation_failed"}
    coordinator.async_create_holiday.assert_not_awaited()
    coordinator.async_update_holiday.assert_not_awaited()


async def test_hiding_legacy_seconds_does_not_rewrite_an_unchanged_timestamp(
    hass, enable_custom_integrations
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    original = "2030-08-01T08:38:37"
    coordinator.data[HOLIDAY_LIST_PATH].resource.value[0]["startDate"] = original
    result = await _open_existing_holiday(hass, entry)
    draft = _holiday_form_input(result)
    assert draft["holiday_start"] == "2030-08-01T08:38"
    draft["holiday_fix_temperature"] = 18.5
    result = await _configure_options(hass, result["flow_id"], draft)
    assert await flow_outcome(hass, result) == "holiday_updated"
    assert coordinator.async_update_holiday.await_args.args[1].start_date == original


async def test_editing_requires_the_saved_clock_instead_of_defaulting_to_eight(
    hass, enable_custom_integrations
):
    from homeassistant.data_entry_flow import InvalidData

    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    draft = _holiday_form_input(result)
    assert draft.pop("holiday_end") == "2030-08-08T18:00"
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], draft)
    coordinator.async_update_holiday.assert_not_awaited()


@pytest.mark.parametrize("draft_date", ["", "2030-10-28T08:00", "not a date"])
async def test_inline_delete_restores_invalid_draft_without_writing(
    hass, enable_custom_integrations, draft_date
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    draft = {
        **_holiday_form_input(result),
        "holiday_name": "Unsaved holiday",
        "holiday_start": draft_date,
        "holiday_end": "2030-10-27T08:00",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**draft, "holiday_delete": True}
    )
    assert result["step_id"] == "delete_holiday"
    coordinator.async_update_holiday.assert_not_awaited()
    coordinator.async_delete_holiday.assert_not_awaited()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm_delete": False}
    )
    assert result["step_id"] == "holiday"
    assert _holiday_form_input(result) == draft
    coordinator.async_update_holiday.assert_not_awaited()
    coordinator.async_delete_holiday.assert_not_awaited()


@pytest.mark.parametrize("value", ["", "2030-08-01", "invalid"])
async def test_datetime_input_requires_an_explicit_date_and_time(
    hass, enable_custom_integrations, value
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    draft = {**_holiday_form_input(result), "holiday_start": value}
    result = await hass.config_entries.options.async_configure(result["flow_id"], draft)
    assert result["errors"] == {"base": "invalid_holiday_details"}
    assert _holiday_form_input(result)["holiday_start"] == value
    coordinator.async_update_holiday.assert_not_awaited()


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
    assert result["title"] == "Buderus Home Comfort"
    entry = result["result"]
    assert entry.version == 4
    cloud = next(iter(entry.data["connections"]))
    data = EntryConfiguration(entry, cloud).cloud_data()
    assert data[CONF_BRAND] == Brand.BUDERUS.value
    assert data[CONF_GATEWAY_IDS] == ["gateway-one"]
    assert data[CONF_ACCESS_TOKEN] == "access"
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


@pytest.fixture
def scope_fallback():
    """Each test starts with the reduced scope set and leaves no fallback."""
    from custom_components.bosch_buderus_heating.pointt import auth

    auth._ScopeFallback.active = False
    yield
    auth._ScopeFallback.active = False


def _scope_rejection(result: dict[str, object], brand: Brand) -> str:
    placeholders = result["description_placeholders"]
    assert isinstance(placeholders, dict)
    state = parse_qs(urlparse(placeholders["authorization_url"]).query)["state"][0]
    return f"{brand.redirect_uri}?error=invalid_scope&state={state}"


async def test_a_rejected_scope_set_asks_to_open_the_new_link(
    hass: HomeAssistant, enable_custom_integrations: None, scope_fallback
) -> None:
    """The dialog names the new link, not a wrong address (C, scope rejection)."""
    first = await _start_auth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        first["flow_id"],
        {CONF_REDIRECT_URL: _scope_rejection(first, Brand.BUDERUS)},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "auth"
    assert result["errors"] == {"base": "auth_restart"}
    link = result["description_placeholders"]["authorization_url"]
    assert link != first["description_placeholders"]["authorization_url"]
    assert "pointt.gateway.claiming" in parse_qs(urlparse(link).query)["scope"][0]
    # The new link completes the sign-in.
    exchange = AsyncMock(return_value=AuthTokens("access", "refresh", 4000.0))
    with (
        patch(
            "custom_components.bosch_buderus_heating.config_flow.OAuthClient.exchange_code",
            exchange,
        ),
        patch(
            "custom_components.bosch_buderus_heating.config_flow.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("gateway-one"),)),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_REDIRECT_URL: _redirect_from_result(result, Brand.BUDERUS)},
        )
    assert result["step_id"] == "gateways"
    exchange.assert_awaited_once()


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
    assert await flow_outcome(hass, result) == "no_gateways"


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
        version=3,
        data={
            CONF_BRAND: Brand.BUDERUS.value,
            CONF_GATEWAY_IDS: ["gateway-one"],
            **tokens_to_data(AuthTokens("existing", "refresh", 4000.0)),
        },
    )
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
    # Another entry can be registered while this authenticated dialog is open.
    existing.add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_GATEWAY_IDS: ["gateway-one"]}
    )
    assert result["type"] is FlowResultType.ABORT
    assert await flow_outcome(hass, result) == "cloud_already_connected"


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


@pytest.mark.parametrize("visible_gateway", ["gateway-one", "another-account"])
async def test_reauthentication_updates_tokens(
    hass: HomeAssistant, enable_custom_integrations: None, visible_gateway: str
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
        patch.object(hass.config_entries, "async_reload", AsyncMock()),
        patch(
            "custom_components.bosch_buderus_heating.config_flow.OAuthClient.exchange_code",
            AsyncMock(return_value=renewed),
        ),
        patch(
            "custom_components.bosch_buderus_heating.config_flow.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway(visible_gateway),)),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )

    assert result["type"] is FlowResultType.ABORT
    if visible_gateway == "gateway-one":
        assert await flow_outcome(hass, result) == "reauth_successful"
        assert entry.data[CONF_ACCESS_TOKEN] == "new-access"
    else:
        assert await flow_outcome(hass, result) == "reauth_wrong_account"
        assert entry.data[CONF_ACCESS_TOKEN] == "old-access"


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
        holiday_timezones={},
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
        async_delete_holiday=AsyncMock(),
    )
    entry.runtime_data = BoschBuderusRuntimeData(
        client=AsyncMock(),
        token_manager=AsyncMock(),
        gateways=(coordinator.gateway,),
        coordinators=(coordinator,),
    )
    return entry, coordinator


async def test_existing_holiday_edits_name_dates_and_settings_together(
    hass, enable_custom_integrations
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    values = _holiday_form_input(result)
    assert values["holiday_name"] == "Urlaub"
    assert values["holiday_start"] == "2030-08-01T08:00"
    assert values["holiday_end"] == "2030-08-08T18:00"
    assert list(values)[:3] == ["holiday_name", "holiday_start", "holiday_end"]
    coordinator.async_update_holiday.assert_not_awaited()
    coordinator.async_delete_holiday.assert_not_awaited()
    values.update(
        holiday_name="Urlaub im Grünen",
        holiday_start="2030-08-02T09:15",
        holiday_end="2030-08-10T18:45",
        holiday_fix_temperature=18.5,
    )
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_updated"
    coordinator.async_update_holiday.assert_awaited_once()
    holiday_id, written = coordinator.async_update_holiday.await_args.args
    baseline = coordinator.async_update_holiday.await_args.kwargs["expected"]
    assert holiday_id == 7
    assert written.start_date == "2030-08-02T09:15:00"
    assert written.end_date == "2030-08-10T18:45:00"
    assert base64.b64decode(written.name).decode() == "Urlaub im Grünen"
    assert written.fix_temperature == 18.5
    assert written.assigned_to == baseline.assigned_to
    assert written.dhw_mode == baseline.dhw_mode
    assert written.ventilation_mode == baseline.ventilation_mode
    assert written.thermal_disinfection == baseline.thermal_disinfection


@pytest.mark.parametrize("date_only", [False, True])
async def test_existing_holiday_preserves_midnight_and_exclusive_end_dates(
    hass, enable_custom_integrations, date_only
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    original = coordinator.data[HOLIDAY_LIST_PATH].resource.value[0]
    original["startDate"] = "2030-08-01" if date_only else "2030-08-01T08:00:00"
    original["endDate"] = "2030-08-08" if date_only else "2030-08-09T00:00:00"
    if date_only:
        coordinator.data[HOLIDAY_CONFIGURATION_PATH].resource.value["values"]["date"][
            "allowedValues"
        ] = ["date"]
    result = await _open_existing_holiday(hass, entry)
    values = _holiday_form_input(result)
    assert values["holiday_end"] == ("2030-08-09" if date_only else "2030-08-09T00:00")
    values[CONF_HOLIDAY_FIX_TEMPERATURE] = 18.0
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_updated"
    written = coordinator.async_update_holiday.await_args.args[1]
    assert written.start_date == original["startDate"]
    assert written.end_date == original["endDate"]

    result = await _open_existing_holiday(hass, entry)
    values = _holiday_form_input(result)
    values["holiday_end"] = "2030-08-10" if date_only else "2030-08-10T00:00"
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_updated"
    written = coordinator.async_update_holiday.await_args.args[1]
    assert written.end_date == ("2030-08-09" if date_only else "2030-08-10T00:00:00")


@pytest.mark.parametrize(
    ("changes", "errors"),
    [
        (
            {"holiday_start": "2030-08-01T22:38"},
            {"holiday_start": "holiday_time_step"},
        ),
        (
            {"holiday_end": "2030-08-08T18:45:01"},
            {"holiday_end": "holiday_time_step"},
        ),
        (
            {"holiday_end": "2030-07-01T18:00"},
            {"holiday_end": "holiday_end_not_after_start"},
        ),
        ({"holiday_name": "x" * 33}, {"base": "invalid_holiday_details"}),
        ({"holiday_name": "   "}, {"base": "invalid_holiday_details"}),
        (
            {"holiday_start": "2030-03-31T02:30"},
            {"base": "holiday_time_nonexistent"},
        ),
        (
            {"holiday_start": "2030-10-27T02:30", "holiday_end": "2030-11-01T12:00"},
            {"base": "holiday_time_ambiguous"},
        ),
    ],
)
async def test_existing_holiday_keeps_input_after_invalid_name_or_dates(
    hass, enable_custom_integrations, changes, errors
):
    await hass.config.async_set_time_zone("Europe/Berlin")
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    values = {**_holiday_form_input(result), "holiday_fix_temperature": 18.5, **changes}
    result = await _configure_options(hass, result["flow_id"], values)
    assert result["step_id"] == "holiday"
    assert result["errors"] == errors
    assert _holiday_form_input(result) == values
    coordinator.async_update_holiday.assert_not_awaited()
    values.update(
        holiday_name="Corrected holiday",
        holiday_start="2030-08-01T09:15",
        holiday_end="2030-08-08T18:45",
    )
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_updated"
    coordinator.async_update_holiday.assert_awaited_once()


async def test_existing_holiday_retry_keeps_baseline_and_unrelated_app_changes(
    hass, enable_custom_integrations
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    values = _holiday_form_input(result)
    values["holiday_start"] = "2030-08-01"
    values["holiday_start"] = values["holiday_start"].split("T")[0] + "T" + "22:38:00"
    original = coordinator.data[HOLIDAY_LIST_PATH].resource.value[0]
    coordinator.data[HOLIDAY_LIST_PATH].resource = Resource(
        path=HOLIDAY_LIST_PATH,
        value=[{**original, "fixTemperature": 19.0}],
        has_value=True,
    )
    result = await _configure_options(hass, result["flow_id"], values)
    assert result["errors"] == {"holiday_start": "holiday_time_step"}
    values = {**_holiday_form_input(result), "holiday_start": "2030-08-02T09:00"}
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_updated"
    call = coordinator.async_update_holiday.await_args
    baseline = call.kwargs["expected"]
    assert baseline.fix_temperature == 17.0
    merged = _merge_holiday_changes(
        baseline, call.args[1], replace(baseline, fix_temperature=19.0)
    )
    assert merged.fix_temperature == 19.0
    assert merged.start_date == "2030-08-02T09:00:00"


async def test_settings_only_edit_preserves_an_existing_non_quarter_hour_time(
    hass, enable_custom_integrations
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.data[HOLIDAY_LIST_PATH].resource.value[0]["startDate"] = (
        "2030-08-01T08:38:00"
    )
    result = await _open_existing_holiday(hass, entry)
    values = {**_holiday_form_input(result), "holiday_fix_temperature": 18.5}
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_updated"
    written = coordinator.async_update_holiday.await_args.args[1]
    assert written.start_date == "2030-08-01T08:38:00"
    assert written.fix_temperature == 18.5


async def test_existing_holiday_offset_inputs_use_the_installation_timezone(
    hass, enable_custom_integrations
):
    await hass.config.async_set_time_zone("Europe/Berlin")
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    values = {
        **_holiday_form_input(result),
        "holiday_start": "2030-08-02T07:15:00+00:00",
        "holiday_end": "2030-08-10T16:45:00+00:00",
    }
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_updated"
    written = coordinator.async_update_holiday.await_args.args[1]
    assert written.start_date == "2030-08-02T09:15:00"
    assert written.end_date == "2030-08-10T18:45:00"


async def test_existing_holiday_without_name_storage_still_allows_date_changes(
    hass, enable_custom_integrations
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    del coordinator.data[HOLIDAY_CONFIGURATION_PATH].resource.value["values"]["name"]
    result = await _open_existing_holiday(hass, entry)
    values = _holiday_form_input(result)
    assert "holiday_name" not in values
    values["holiday_start"] = "2030-08-02"
    values["holiday_start"] = values["holiday_start"].split("T")[0] + "T" + "09:00:00"
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_updated"
    call = coordinator.async_update_holiday.await_args
    assert call.args[1].name == call.kwargs["expected"].name


async def test_holiday_delete_requires_confirmation_and_can_return_to_editing(
    hass, enable_custom_integrations
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry, action="delete_holiday")
    assert result["step_id"] == "delete_holiday"
    assert "Urlaub" in result["description_placeholders"]["holiday"]
    assert "2030-08-01 08:00" in result["description_placeholders"]["holiday"]
    assert "2030-08-08 18:00" in result["description_placeholders"]["holiday"]
    assert _holiday_form_input(result) == {"confirm_delete": False}
    coordinator.async_delete_holiday.assert_not_awaited()
    result = await _configure_options(
        hass, result["flow_id"], {"confirm_delete": False}
    )
    assert result["type"] is FlowResultType.FORM
    assert "holiday_delete" not in _holiday_form_input(result)
    coordinator.async_delete_holiday.assert_not_awaited()
    assert result["step_id"] == "holiday"
    coordinator.async_update_holiday.assert_not_awaited()


async def test_delete_from_edit_preserves_draft_on_cancel_and_never_saves_it_first(
    hass, enable_custom_integrations
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    draft = _holiday_form_input(result)
    assert "holiday_delete" not in draft
    draft.update(
        holiday_name="Noch nicht gespeichert",
        holiday_end="2030-08-10T09:15",
        holiday_fix_temperature=18.5,
    )
    result = await _configure_options(
        hass, result["flow_id"], draft, action="delete_holiday"
    )
    assert result["step_id"] == "delete_holiday"
    assert "Urlaub" in result["description_placeholders"]["holiday"]
    assert "2030-08-08 18:00" in result["description_placeholders"]["holiday"]
    assert "Noch nicht gespeichert" not in result["description_placeholders"]["holiday"]
    coordinator.async_update_holiday.assert_not_awaited()
    coordinator.async_delete_holiday.assert_not_awaited()
    result = await _configure_options(
        hass, result["flow_id"], {"confirm_delete": False}
    )
    assert result["step_id"] == "holiday"
    assert _holiday_form_input(result) == draft
    coordinator.async_update_holiday.assert_not_awaited()
    coordinator.async_delete_holiday.assert_not_awaited()
    result = await _configure_options(hass, result["flow_id"], draft)
    assert await flow_outcome(hass, result) == "holiday_updated"
    call = coordinator.async_update_holiday.await_args
    assert call.args[1].end_date == "2030-08-10T09:15:00"
    assert call.args[1].fix_temperature == 18.5
    assert call.kwargs["expected"].end_date == "2030-08-08T18:00:00"
    assert call.kwargs["expected"].fix_temperature == 17.0
    coordinator.async_delete_holiday.assert_not_awaited()


async def test_delete_from_edit_confirms_saved_values_not_the_unsaved_draft(
    hass, enable_custom_integrations
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    draft = _holiday_form_input(result)
    draft["holiday_name"] = "Unsent name"
    original = coordinator.data[HOLIDAY_LIST_PATH].resource.value[0]
    updated = {**original, "startDate": "2030-08-02T08:00:00"}
    coordinator.data[HOLIDAY_LIST_PATH].resource = Resource(
        path=HOLIDAY_LIST_PATH, value=[updated], has_value=True
    )
    result = await _configure_options(
        hass, result["flow_id"], draft, action="delete_holiday"
    )
    assert result["step_id"] == "delete_holiday"
    assert "2030-08-02 08:00" in result["description_placeholders"]["holiday"]
    assert "Unsent name" not in result["description_placeholders"]["holiday"]
    result = await _configure_options(hass, result["flow_id"], {"confirm_delete": True})
    assert await flow_outcome(hass, result) == "holiday_deleted"
    coordinator.async_update_holiday.assert_not_awaited()
    coordinator.async_delete_holiday.assert_awaited_once()
    assert coordinator.async_delete_holiday.await_args.kwargs[
        "expected"
    ].start_date == ("2030-08-02T08:00:00")


async def test_new_holiday_has_no_delete_option(hass, enable_custom_integrations):
    from homeassistant.data_entry_flow import InvalidData

    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_create_holiday = AsyncMock()
    result = await _open_new_holiday(hass, entry)
    values = _new_holiday_input(result)
    assert "holiday_delete" not in values
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**values, "holiday_delete": True}
        )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], values
    )
    assert await flow_outcome(hass, result) == "holiday_created"
    # Back in the main menu, not in a menu of the holiday.
    assert result["step_id"] == "init"
    coordinator.async_create_holiday.assert_awaited_once()
    coordinator.async_update_holiday.assert_not_awaited()
    coordinator.async_delete_holiday.assert_not_awaited()


@pytest.mark.parametrize("action", ["holiday", "delete_holiday"])
async def test_existing_holiday_uses_the_selected_gateway(
    hass, enable_custom_integrations, action
):
    entry, first = _entry_with_writable_holiday(hass)
    second = SimpleNamespace(
        hass=hass,
        holiday_timezones={},
        gateway=Gateway("gateway-two", model="Second installation"),
        data=deepcopy(first.data),
        async_update_holiday=AsyncMock(),
        async_delete_holiday=AsyncMock(),
    )
    entry.runtime_data.coordinators = (first, second)
    result = await _open_existing_holiday(
        hass, entry, gateway_id="gateway-two", action=action
    )
    assert "Second installation" in result["description_placeholders"]["holiday"]
    values = (
        {"confirm_delete": True}
        if action == "delete_holiday"
        else {**_holiday_form_input(result), "holiday_name": "Second trip"}
    )
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == (
        "holiday_deleted" if action == "delete_holiday" else "holiday_updated"
    )
    write = (
        second.async_delete_holiday
        if action == "delete_holiday"
        else second.async_update_holiday
    )
    write.assert_awaited_once()
    assert write.await_args.args[0] == 7
    assert write.await_args.kwargs["expected"].name == "VXJsYXVi"
    first.async_delete_holiday.assert_not_awaited()
    first.async_update_holiday.assert_not_awaited()


@pytest.mark.parametrize(
    "change", ["removed", "dates", "settings", "stale", "unloaded"]
)
async def test_delete_stops_when_confirmation_is_outdated(
    hass, enable_custom_integrations, change
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry, action="delete_holiday")
    original = coordinator.data[HOLIDAY_LIST_PATH].resource.value[0]
    if change == "unloaded":
        entry.runtime_data = None
    elif change == "stale":
        coordinator.data[HOLIDAY_LIST_PATH].freshness = Freshness.STALE
    else:
        updated = (
            {**original, "startDate": "2030-08-02T08:00:00"}
            if change == "dates"
            else {**original, "fixTemperature": 19.0}
        )
        coordinator.data[HOLIDAY_LIST_PATH].resource = Resource(
            path=HOLIDAY_LIST_PATH,
            value=[] if change == "removed" else [updated],
            has_value=True,
        )
    result = await _configure_options(hass, result["flow_id"], {"confirm_delete": True})
    assert await flow_outcome(hass, result) == "holiday_changed"
    coordinator.async_delete_holiday.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (WriteNotConfirmed("missing"), "delete_not_confirmed"),
        (RateLimited(60), "write_rate_limited"),
        (AuthenticationError("expired"), "write_authentication_failed"),
        (WriteValidationError("stale"), "holiday_changed"),
        (InvalidPayload("bad"), "delete_failed"),
        (WriteBusy("busy"), "write_busy"),
        (HolidayChangedError("changed"), "holiday_changed"),
    ],
)
async def test_holiday_delete_reports_failure_without_retrying(
    hass, enable_custom_integrations, error, expected
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_delete_holiday.side_effect = error
    result = await _open_existing_holiday(hass, entry, action="delete_holiday")
    result = await _configure_options(hass, result["flow_id"], {"confirm_delete": True})
    if isinstance(error, HolidayChangedError):
        assert await flow_outcome(hass, result) == expected
    else:
        assert result["step_id"] == "delete_holiday"
        assert result["errors"] == {"base": expected}
        assert _holiday_form_input(result) == {"confirm_delete": False}
    coordinator.async_delete_holiday.assert_awaited_once()
    coordinator.async_update_holiday.assert_not_awaited()


async def test_options_flow_configures_only_advertised_holiday_fields(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    form_values = _holiday_form_input(result)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "holiday"
    field_order = [marker.schema for marker in result["data_schema"].schema]
    assert field_order.index(CONF_HOLIDAY_FIX_TEMPERATURE) < field_order.index(
        CONF_HOLIDAY_DHW_MODE
    )
    result = await _configure_options(
        hass,
        result["flow_id"],
        {
            **form_values,
            CONF_HOLIDAY_ASSIGNED_TO: ["hc1", "vent1"],
            CONF_HOLIDAY_HEATING_MODE: "eco",
            CONF_HOLIDAY_DHW_MODE: "low",
            CONF_HOLIDAY_VENTILATION_MODE: "dem",
            CONF_HOLIDAY_THERMAL_DISINFECTION: "off",
            CONF_HOLIDAY_FIX_TEMPERATURE: 18.5,
        },
    )

    assert result["type"] is FlowResultType.MENU
    assert await flow_outcome(hass, result) == "holiday_updated"
    holiday_id, values = coordinator.async_update_holiday.await_args.args
    baseline = coordinator.async_update_holiday.await_args.kwargs["expected"]
    assert baseline is not None
    assert baseline.fix_temperature != values.fix_temperature
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

    result = await _start_holiday_options(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert await flow_outcome(hass, result) == "no_writable_holidays"


async def test_options_flow_requires_a_loaded_entry(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _reconfigure_entry(hass)

    result = await _start_holiday_options(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert await flow_outcome(hass, result) == "not_loaded"


async def test_options_flow_rejects_a_changed_selection(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _start_holiday_options(hass, entry)
    selection = hashlib.sha256(b"gateway-one\x007").hexdigest()[:24]
    original = coordinator.data[HOLIDAY_LIST_PATH].resource.value[0]
    coordinator.data[HOLIDAY_LIST_PATH].resource = Resource(
        path=HOLIDAY_LIST_PATH,
        value=[{**original, "id": 8}],
        has_value=True,
    )

    result = await _configure_options(
        hass, result["flow_id"], {CONF_HOLIDAY_PERIOD: selection}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "holidays"
    assert result["errors"] == {"base": "holiday_changed"}


async def test_options_flow_stops_if_selected_holiday_disappears(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    form_values = _holiday_form_input(result)
    coordinator.data[HOLIDAY_LIST_PATH].resource = Resource(
        path=HOLIDAY_LIST_PATH, value=[], has_value=True
    )

    result = await _configure_options(
        hass,
        result["flow_id"],
        {
            **form_values,
            CONF_HOLIDAY_ASSIGNED_TO: ["hc1"],
            CONF_HOLIDAY_HEATING_MODE: "off",
            CONF_HOLIDAY_DHW_MODE: "off",
            CONF_HOLIDAY_VENTILATION_MODE: "off",
            CONF_HOLIDAY_THERMAL_DISINFECTION: "off",
            CONF_HOLIDAY_FIX_TEMPERATURE: 17.0,
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert await flow_outcome(hass, result) == "holiday_changed"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (WriteNotConfirmed("not confirmed"), "write_not_confirmed"),
        (RateLimited(60), "write_rate_limited"),
        (AuthenticationError("expired"), "write_authentication_failed"),
        (WriteValidationError("changed"), "write_validation_failed"),
        (InvalidPayload("bad"), "write_failed"),
        (WriteBusy("busy"), "write_busy"),
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
    result = await _open_existing_holiday(hass, entry)
    form_values = _holiday_form_input(result)

    result = await _configure_options(
        hass,
        result["flow_id"],
        {
            **form_values,
            CONF_HOLIDAY_ASSIGNED_TO: ["hc1", "dhw1", "vent1"],
            CONF_HOLIDAY_HEATING_MODE: "fix_temperature",
            CONF_HOLIDAY_DHW_MODE: "off",
            CONF_HOLIDAY_VENTILATION_MODE: "nom",
            CONF_HOLIDAY_THERMAL_DISINFECTION: "on",
            CONF_HOLIDAY_FIX_TEMPERATURE: 18.0,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "holiday"
    assert result["errors"] == {"base": expected}


async def test_saving_the_holiday_as_shown_sends_nothing(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """UI-1 option 3: an unchanged save, e.g. by Enter, writes nothing."""
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    result = await _configure_options(
        hass, result["flow_id"], _holiday_form_input(result)
    )
    assert result["type"] is FlowResultType.MENU
    assert await flow_outcome(hass, result) == "holiday_unchanged"
    coordinator.async_update_holiday.assert_not_awaited()
    coordinator.async_delete_holiday.assert_not_awaited()


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
    assert await flow_outcome(hass, result) == "reconfigure_successful"
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
        assert await flow_outcome(hass, result) == expected
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
    assert await flow_outcome(hass, result) == "no_gateways"


async def test_reconfigure_brand_change_signs_in_and_updates_entry(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _reconfigure_entry(hass)
    hass.config_entries.async_update_entry(entry, title="My installation")
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
    assert await flow_outcome(hass, result) == "reconfigure_successful"
    assert entry.data[CONF_BRAND] == Brand.BOSCH.value
    assert entry.data[CONF_ACCESS_TOKEN] == "bosch-access"
    assert entry.data[CONF_GATEWAY_IDS] == ["bosch-gateway"]
    assert entry.title == "My installation"


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


async def test_holiday_form_preserves_its_original_baseline_during_polling(
    hass, enable_custom_integrations
):
    from custom_components.bosch_buderus_heating.holiday_writes import (
        _merge_holiday_changes,
    )

    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    form_values = _holiday_form_input(result)
    old = coordinator.data[HOLIDAY_LIST_PATH].resource.value[0]
    coordinator.data[HOLIDAY_LIST_PATH].resource = Resource(
        path=HOLIDAY_LIST_PATH, value=[{**old, "fixTemperature": 19.0}], has_value=True
    )
    result = await _configure_options(
        hass,
        result["flow_id"],
        {
            **form_values,
            CONF_HOLIDAY_ASSIGNED_TO: ["hc1", "dhw1", "vent1"],
            CONF_HOLIDAY_HEATING_MODE: "fix_temperature",
            CONF_HOLIDAY_DHW_MODE: "low",
            CONF_HOLIDAY_VENTILATION_MODE: "nom",
            CONF_HOLIDAY_THERMAL_DISINFECTION: "on",
            CONF_HOLIDAY_FIX_TEMPERATURE: 17.0,
        },
    )
    assert await flow_outcome(hass, result) == "holiday_updated"
    call = coordinator.async_update_holiday.await_args
    baseline = call.kwargs["expected"]
    assert baseline.fix_temperature == 17.0
    from dataclasses import replace

    merged = _merge_holiday_changes(
        baseline, call.args[1], replace(baseline, fix_temperature=19.0)
    )
    assert merged.fix_temperature == 19.0 and merged.dhw_mode == "LOW"


async def test_new_holiday_requires_explicit_offered_modes(
    hass, enable_custom_integrations
):
    from homeassistant.data_entry_flow import InvalidData

    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_create_holiday = AsyncMock()
    config = coordinator.data[HOLIDAY_CONFIGURATION_PATH].resource.value["values"]
    config["heatingMode"]["allowedValues"] = ["ECO", "NewVendorMode"]
    config["dhwMode"]["allowedValues"] = ["LOW"]
    config["ventilationMode"]["allowedValues"] = ["NOM"]
    config["thermalDesinfection"]["allowedValues"] = ["OFF"]
    result = await _open_new_holiday(hass, entry)
    values = _new_holiday_input(result)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], values)
    coordinator.async_create_holiday.assert_not_awaited()
    values.update(
        holiday_heating_mode="NewVendorMode",
        holiday_dhw_mode="low",
        holiday_ventilation_mode="nom",
        holiday_thermal_disinfection="off",
        holiday_fix_temperature=18.0,
    )
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_created"
    written = coordinator.async_create_holiday.await_args.args[0]
    assert written.heating_mode == "NewVendorMode"
    assert written.dhw_mode == "LOW"
    assert written.ventilation_mode == "NOM"
    assert written.thermal_disinfection == "OFF"
    coordinator.async_update_holiday.assert_not_awaited()


@pytest.mark.parametrize("field", ["holiday_start", "holiday_end"])
@pytest.mark.parametrize("clock", ["22:38:00", "22:45:01"])
async def test_new_holiday_time_error_can_be_corrected_without_losing_input(
    hass, enable_custom_integrations, field, clock
):
    await hass.config.async_set_time_zone("Europe/Berlin")
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_create_holiday = AsyncMock()
    result = await _open_new_holiday(hass, entry)
    values = _new_holiday_input(
        result,
        holiday_name="Urlaub im Grünen",
        holiday_start="2026-09-20T22:45",
        holiday_end="2026-09-24T15:00",
    )
    values[field] = values[field][:10] + "T" + clock
    result = await _configure_options(hass, result["flow_id"], values)
    assert result["step_id"] == "new_holiday"
    assert result["errors"] == {field: "holiday_time_step"}
    expected = {
        **values,
        field: values[field][:10]
        + "T"
        + (clock[:5] if clock.endswith(":00") else clock),
    }
    assert _holiday_form_input(result) == expected
    coordinator.async_create_holiday.assert_not_awaited()
    values[field] = values[field][:10] + "T22:45"
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_created"
    coordinator.async_create_holiday.assert_awaited_once()
    written = coordinator.async_create_holiday.await_args.args[0]
    assert written.start_date == values["holiday_start"] + ":00"
    assert written.end_date == values["holiday_end"] + ":00"


@pytest.mark.parametrize(
    ("system_language", "profile_language", "expected"),
    [
        ("en", "de", "Neue Urlaubszeit"),
        ("de", "en", "New holiday"),
        ("de", "eo", "New holiday"),
    ],
)
async def test_new_holiday_selector_uses_profile_translations(
    hass, enable_custom_integrations, system_language, profile_language, expected
):
    from homeassistant.helpers.translation import async_get_translations

    hass.config.language = system_language
    entry, _ = _entry_with_writable_holiday(hass)
    result = await _start_holiday_options(hass, entry)
    selector = _field_list(result["data_schema"])[0]["selector"]["select"]
    translations = await async_get_translations(
        hass, profile_language, "selector", {DOMAIN}
    )
    # The frontend looks up the option value in the user's selector translations.
    translation_prefix = (
        f"component.{DOMAIN}.selector.{selector['translation_key']}.options."
    )
    labels = {
        option["value"]: translations.get(
            translation_prefix + option["value"], option["label"]
        )
        for option in selector["options"]
    }
    assert labels["create"] == expected
    existing = hashlib.sha256(b"gateway-one\x007").hexdigest()[:24]
    assert labels[existing].startswith("Urlaub ")
    assert len(labels) == 2
    result = await _configure_options(
        hass, result["flow_id"], {CONF_HOLIDAY_PERIOD: "create"}
    )
    assert result["step_id"] == "new_holiday"


@pytest.mark.parametrize("remove_selected", [False, True])
async def test_new_holiday_routes_to_explicit_gateway(
    hass, enable_custom_integrations, remove_selected
):
    from copy import deepcopy

    entry, first = _entry_with_writable_holiday(hass)
    first.async_create_holiday = AsyncMock()
    second = SimpleNamespace(
        hass=hass,
        holiday_timezones={},
        gateway=Gateway("gateway-two", model="MX400"),
        data=deepcopy(first.data),
        async_create_holiday=AsyncMock(),
        async_update_holiday=AsyncMock(),
    )
    entry.runtime_data.coordinators = (first, second)
    entry.runtime_data.gateways = (first.gateway, second.gateway)
    result = await _start_holiday_options(hass, entry)
    result = await _configure_options(
        hass, result["flow_id"], {CONF_HOLIDAY_PERIOD: "create"}
    )
    assert result["step_id"] == "holiday_gateway"
    options = next(iter(result["data_schema"].schema.values())).config["options"]
    selected = "create:" + hashlib.sha256(b"gateway-two").hexdigest()[:24]
    assert len(options) == 2
    assert next(
        option["label"] for option in options if option["value"] == selected
    ).startswith("MX400")
    if remove_selected:
        entry.runtime_data.coordinators = (first,)
    result = await _configure_options(
        hass, result["flow_id"], {"holiday_gateway": selected}
    )
    if remove_selected:
        assert result["step_id"] == "holiday_gateway"
        assert result["errors"] == {"base": "holiday_changed"}
        first.async_create_holiday.assert_not_awaited()
        second.async_create_holiday.assert_not_awaited()
        return
    assert result["step_id"] == "new_holiday"
    values = _new_holiday_input(
        result, holiday_assigned_to=["hc1"], holiday_heating_mode="eco"
    )
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_created"
    first.async_create_holiday.assert_not_awaited()
    second.async_create_holiday.assert_awaited_once()


@pytest.mark.parametrize("date_mode", ["date", "dateTime"])
@pytest.mark.parametrize("language", ["de", "en", "fr"])
async def test_new_holiday_dates_start_empty_and_remain_required(
    hass, enable_custom_integrations, date_mode, language
):
    hass.config.language = language
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_create_holiday = AsyncMock()
    coordinator.data[HOLIDAY_CONFIGURATION_PATH].resource.value["values"]["date"][
        "allowedValues"
    ] = [date_mode]
    result = await _open_new_holiday(hass, entry)
    schema = result["data_schema"]
    serialized = {field["name"]: field for field in _field_list(schema)}
    for field in ("holiday_start", "holiday_end"):
        marker = next(key for key in schema.schema if key.schema == field)
        assert isinstance(marker, vol.Required)
        assert marker.description == {"suggested_value": ""}
        assert serialized[field]["required"] is True
        if date_mode == "date":
            assert "date" in serialized[field]["selector"]
        else:
            assert serialized[field]["selector"]["bosch-buderus-holiday-datetime"] == {
                "default_time": "08:00",
                **dialog_config("holiday"),
            }
        assert f"{field}_time" not in serialized
    assert "holiday_heating_mode" in serialized
    assert "holiday_dhw_mode" in serialized
    with pytest.raises(vol.Invalid):
        schema({})
    values = _new_holiday_input(
        result,
        holiday_start="2026-09-20" if date_mode == "date" else "2026-09-20T08:00",
        holiday_end="2026-09-24" if date_mode == "date" else "2026-09-24T08:00",
    )
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_created"
    coordinator.async_create_holiday.assert_awaited_once()
    written = coordinator.async_create_holiday.await_args.args[0]
    assert written.start_date == (
        "2026-09-20" if date_mode == "date" else "2026-09-20T08:00:00"
    )
    assert written.end_date == (
        "2026-09-23" if date_mode == "date" else "2026-09-24T08:00:00"
    )


@pytest.mark.parametrize(
    ("start", "end", "expected_error", "suggested_start"),
    [
        (
            "2026-03-29T02:30:00",
            "2026-03-29T05:00:00",
            "holiday_time_nonexistent",
            "2026-03-29 02:30:00",
        ),
        (
            "2026-10-25T02:30:00",
            "2026-10-25T05:00:00",
            "holiday_time_ambiguous",
            "2026-10-25 02:30:00",
        ),
        (
            "2026-09-24T15:00:00",
            "2026-09-20T22:00:00",
            "write_validation_failed",
            "2026-09-24 15:00:00",
        ),
    ],
)
async def test_new_holiday_error_preserves_name_and_canonical_dates(
    hass, enable_custom_integrations, start, end, expected_error, suggested_start
):
    await hass.config.async_set_time_zone("Europe/Berlin")
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_create_holiday = AsyncMock()
    result = await _open_new_holiday(hass, entry)
    values = _new_holiday_input(
        result,
        holiday_name="Urlaub im Grünen",
        holiday_start=start[:10] + "T" + start[11:],
        holiday_end=end[:10] + "T" + end[11:],
    )
    result = await _configure_options(hass, result["flow_id"], values)
    assert result["step_id"] == "new_holiday"
    expected = (
        {"holiday_end": "holiday_end_not_after_start"}
        if expected_error == "write_validation_failed"
        else {"base": expected_error}
    )
    assert result["errors"] == expected
    suggestions = _holiday_form_input(result)
    assert suggestions["holiday_name"] == "Urlaub im Grünen"
    assert suggestions["holiday_start"] == suggested_start[:16].replace(" ", "T")
    coordinator.async_create_holiday.assert_not_awaited()


@pytest.mark.parametrize("clock", ["00:00:00", "09:15:00"])
@pytest.mark.parametrize("timezone", ["Europe/Berlin", "America/New_York"])
async def test_new_holiday_clock_override_is_not_replaced_by_eight(
    hass, enable_custom_integrations, clock, timezone
):
    await hass.config.async_set_time_zone(timezone)
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_create_holiday = AsyncMock()
    result = await _open_new_holiday(hass, entry)
    values = _new_holiday_input(
        result,
        holiday_start="2030-08-01" + "T" + clock[:5],
        holiday_end="2030-08-08" + "T" + clock[:5],
    )
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_created"
    written = coordinator.async_create_holiday.await_args.args[0]
    assert written.start_date == "2030-08-01T" + clock
    assert written.end_date == "2030-08-08T" + clock


async def test_new_holiday_malformed_datetime_never_reaches_a_write(
    hass, enable_custom_integrations
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_create_holiday = AsyncMock()
    result = await _start_holiday_options(hass, entry)
    result = await _configure_options(
        hass,
        result["flow_id"],
        {CONF_HOLIDAY_PERIOD: "create"},
    )
    result = await _configure_options(
        hass,
        result["flow_id"],
        _new_holiday_input(result, holiday_start="2026-09-20T00:00:00 22:00:00"),
    )
    assert result["errors"] == {"base": "invalid_holiday_details"}
    coordinator.async_create_holiday.assert_not_awaited()


async def test_new_holiday_date_only_gateway_rejects_reversed_dates(
    hass, enable_custom_integrations
):
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_create_holiday = AsyncMock()
    config = coordinator.data[HOLIDAY_CONFIGURATION_PATH].resource.value["values"]
    config["date"]["allowedValues"] = ["date"]
    config["fixTemperature"] = {"minValue": 18, "maxValue": 26}
    result = await _open_new_holiday(hass, entry)
    temperature = next(
        marker
        for marker in result["data_schema"].schema
        if marker.schema == CONF_HOLIDAY_FIX_TEMPERATURE
    )
    assert temperature.default is vol.UNDEFINED
    values = _new_holiday_input(
        result,
        holiday_name="Dates",
        holiday_start="2030-08-03",
        holiday_end="2030-08-01",
        holiday_fix_temperature=19.0,
    )
    result = await _configure_options(hass, result["flow_id"], values)
    assert result["errors"] == {"holiday_end": "holiday_end_not_after_start"}
    coordinator.async_create_holiday.assert_not_awaited()
    values.update(holiday_start="2030-08-01", holiday_end="2030-08-03")
    result = await _configure_options(hass, result["flow_id"], values)
    assert await flow_outcome(hass, result) == "holiday_created"
    written = coordinator.async_create_holiday.await_args.args[0]
    assert written.start_date == "2030-08-01" and written.end_date == "2030-08-02"
    assert written.fix_temperature == 19.0
