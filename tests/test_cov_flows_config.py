"""Error branches of the cloud setup, reconfigure, reauth and holiday dialogs."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.bacon.models import (
    CONF_BACON_IDS,
    BaconError,
)
from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.const import (
    CONF_BRAND,
    CONF_FAULT_NOTIFICATIONS,
    CONF_GATEWAY_IDS,
    CONF_POLLING_PROFILE,
    CONF_REDIRECT_URL,
    DOMAIN,
    PollingProfile,
)
from custom_components.bosch_buderus_heating.holidays import HOLIDAY_LIST_PATH
from custom_components.bosch_buderus_heating.legacy_holidays import (
    WDDW2_HOLIDAY_PATH,
)
from custom_components.bosch_buderus_heating.notifications import (
    gateway_notification_key,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    AuthTokens,
    Brand,
    Gateway,
    InvalidPayload,
    RateLimited,
    Resource,
    WriteNotConfirmed,
    WriteValidationError,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.helpers.options_menu import outcome
from tests.test_cloud_setup import backend as backend
from tests.test_config_flow import (
    _configure_options,
    _entry_with_writable_holiday,
    _holiday_form_input,
    _open_existing_holiday,
    _reconfigure_entry,
    _redirect_from_result,
    _start_auth_flow,
    _start_holiday_options,
    _start_reconfigure,
)
from tests.test_connection_runtime import entry_for
from tests.test_legacy_holidays import RESOURCES, flow_entry
from tests.test_managed_connections import open_reauth

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

CF = "custom_components.bosch_buderus_heating.config_flow"
TOKENS = AuthTokens("access", "refresh", expires_at=4000.0)


def _flow(hass, result, manager="flow"):
    return getattr(hass.config_entries, manager)._progress[result["flow_id"]]


async def _to_gateways(hass, gateways=None):
    """Sign in and list the systems; the selection step is shown."""
    gateways = gateways or (Gateway("gateway-one"),)
    result = await _start_auth_flow(hass)
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    with (
        patch(CF + ".OAuthClient.exchange_code", AsyncMock(return_value=TOKENS)),
        patch(CF + ".PointTClient.get_gateways", AsyncMock(return_value=gateways)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )
    assert result["step_id"] == "gateways"
    return result


# --- new cloud access: listing and pauses ---------------------------------


async def test_a_pause_without_an_end_blocks_the_listing_for_a_while(hass) -> None:
    result = await _start_auth_flow(hass)
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    listing = AsyncMock(side_effect=RateLimited(None))
    with (
        patch(CF + ".OAuthClient.exchange_code", AsyncMock(return_value=TOKENS)),
        patch(CF + ".PointTClient.get_gateways", listing),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )
        assert result["step_id"] == "retry"
        assert result["errors"] == {"base": "cloud_rate_limited"}
        # A retry during the pause asks nothing of the cloud.
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "cloud_rate_limited"}
    assert listing.await_count == 1


async def test_the_sign_in_waits_out_a_pause_before_a_new_attempt(hass) -> None:
    result = await _start_auth_flow(hass)
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    exchange = AsyncMock(side_effect=RateLimited(None))
    with patch(CF + ".OAuthClient.exchange_code", exchange):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )
        assert result["step_id"] == "auth_wait"
        assert int(result["description_placeholders"]["seconds"]) > 0
        # Pasting the link again while paused sends nothing.
        flow = _flow(hass, result)
        shown = await flow.async_step_auth({CONF_REDIRECT_URL: redirect})
    assert shown["step_id"] == "auth_wait"
    assert exchange.await_count == 1


async def test_a_listing_step_without_a_sign_in_aborts(hass) -> None:
    result = await _to_gateways(hass)
    flow = _flow(hass, result)
    flow._tokens = None
    shown = await flow.async_step_retry({})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "invalid_flow"


async def test_retry_and_expired_selection_pages_are_shown_before_asking(hass) -> None:
    result = await _to_gateways(hass)
    flow = _flow(hass, result)
    for step in ("retry", "selection_expired"):
        shown = await getattr(flow, f"async_step_{step}")()
        assert shown["type"] is FlowResultType.FORM
        assert shown["step_id"] == step
        assert not shown.get("errors")


async def test_a_selection_without_its_sign_in_aborts(hass) -> None:
    result = await _to_gateways(hass)
    flow = _flow(hass, result)
    flow._tokens = None
    shown = await flow.async_step_gateways({CONF_GATEWAY_IDS: ["gateway-one"]})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "invalid_flow"


async def test_a_selection_without_its_listing_aborts(hass) -> None:
    result = await _to_gateways(hass)
    flow = _flow(hass, result)
    flow._new_cloud_listing = None
    shown = await flow.async_step_gateways({CONF_GATEWAY_IDS: ["gateway-one"]})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "invalid_flow"


@pytest.mark.parametrize(
    "error,reason",
    [
        (BaconError("invalid_identifier"), "configuration_changed"),
        (ConfigurationError("cloud_target_owned"), "cloud_target_owned"),
        (ConfigurationError("cloud_already_connected"), "cloud_already_connected"),
        (ConfigurationError("unexpected"), "configuration_changed"),
    ],
)
async def test_registration_failures_end_the_dialog_without_an_entry(
    hass, error, reason
) -> None:
    result = await _to_gateways(hass)
    with patch(CF + ".new_cloud_configuration", side_effect=error):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_GATEWAY_IDS: ["gateway-one"]}
        )
    assert result["type"] is FlowResultType.ABORT
    assert await outcome(hass, result) == reason
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_an_expired_selection_lists_again_without_a_new_sign_in(hass) -> None:
    result = await _to_gateways(hass)
    with patch(
        CF + ".new_cloud_configuration",
        side_effect=ConfigurationError("cloud_selection_expired"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_GATEWAY_IDS: ["gateway-one"]}
        )
    assert result["step_id"] == "selection_expired"
    assert not hass.config_entries.async_entries(DOMAIN)


# --- renewed sign-in of a managed entry ------------------------------------


async def test_managed_reauth_rejected_by_the_cloud_asks_for_a_new_login(
    hass, backend
) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await open_reauth(hass, entry, cloud)
    assert result["step_id"] == "auth"
    redirect = _redirect_from_result(result, Brand.BUDERUS)
    with patch.object(
        ConfigurationManagement,
        "async_reauthenticate",
        AsyncMock(side_effect=AuthenticationError("rejected")),
    ) as renew:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: redirect}
        )
    renew.assert_awaited_once()
    assert result["step_id"] == "auth"
    assert result["errors"] == {"base": "invalid_auth"}


# --- guards of the legacy reconfigure and the shared helpers ---------------


async def test_reconfigure_retry_page_is_shown_before_asking(hass) -> None:
    entry = _reconfigure_entry(hass)
    result = await _start_reconfigure(
        hass, entry, AsyncMock(return_value=(Gateway("gateway-one"),))
    )
    flow = _flow(hass, result)
    shown = await flow.async_step_reconfigure_retry()
    assert shown["step_id"] == "reconfigure_retry"
    assert not shown.get("errors")


@pytest.mark.parametrize("field", ["gateways", "cloud_devices"])
async def test_reconfigure_refuses_systems_that_are_not_listed(hass, field) -> None:
    entry = _reconfigure_entry(hass)
    result = await _start_reconfigure(
        hass, entry, AsyncMock(return_value=(Gateway("gateway-one"),))
    )
    before = dict(entry.data)
    flow = _flow(hass, result)
    submitted = {
        CONF_BRAND: Brand.BUDERUS.value,
        CONF_GATEWAY_IDS: ["gateway-other" if field == "gateways" else "gateway-one"],
        CONF_POLLING_PROFILE: PollingProfile.STANDARD.value,
    }
    if field == "cloud_devices":
        # The cloud device list is not offered right now; another one is refused.
        flow._bacon_unavailable = True
        submitted[CONF_BACON_IDS] = ["unknown-device"]
    shown = await flow.async_step_reconfigure(submitted)
    assert shown["step_id"] == "reconfigure"
    assert shown["errors"] == {"base": "gateway_changed"}
    assert dict(entry.data) == before


async def test_reconfigure_helpers_refuse_a_missing_entry(hass) -> None:
    result = await _to_gateways(hass)
    flow = _flow(hass, result)
    assert flow._reconfigure_entry is None
    shown = await flow._async_prepare_reconfigure()
    assert shown["reason"] == "invalid_flow"
    assert flow._show_reconfigure_form()["reason"] == "invalid_flow"
    assert flow._finish_reconfigure(["gateway-one"])["reason"] == "invalid_flow"
    with pytest.raises(RuntimeError):
        flow._reconfigure_client()


async def test_a_removed_entry_is_not_updated_by_a_late_answer(hass) -> None:
    result = await _to_gateways(hass)
    flow = _flow(hass, result)
    gone = MockConfigEntry(domain=DOMAIN, data={CONF_GATEWAY_IDS: ["gateway-one"]})
    shown = flow._update_and_restart(gone, data_updates={CONF_GATEWAY_IDS: []})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "configuration_changed"
    assert gone.data[CONF_GATEWAY_IDS] == ["gateway-one"]


async def test_duplicate_detection_ignores_other_brands_and_without_a_brand(
    hass,
) -> None:
    result = await _to_gateways(hass)
    flow = _flow(hass, result)
    other = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_BRAND: Brand.BOSCH.value, CONF_GATEWAY_IDS: ["gateway-one"]},
    )
    same = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_BRAND: Brand.BUDERUS.value, CONF_GATEWAY_IDS: ["gateway-one"]},
    )
    other.add_to_hass(hass)
    assert flow._find_duplicate_entry({"gateway-one"}) is None
    same.add_to_hass(hass)
    assert flow._find_duplicate_entry({"gateway-one"}) is same
    assert flow._find_duplicate_entry({"another"}) is None
    flow._brand = None
    assert flow._find_duplicate_entry({"gateway-one"}) is None


# --- options: notifications, rescan, holidays -----------------------------


async def test_notification_choice_of_an_unknown_installation_is_refused(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_GATEWAY_IDS: ["one", "two"]})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "notifications"}
    )
    flow = _flow(hass, result, "options")
    shown = await flow.async_step_notifications(
        {CONF_FAULT_NOTIFICATIONS: [gateway_notification_key("three")]}
    )
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "notification_gateways_changed"
    assert not entry.options


async def test_a_search_cannot_be_planned_without_a_running_system(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_GATEWAY_IDS: ["one"]})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow = _flow(hass, result, "options")
    shown = await flow.async_step_rescan()
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "rescan_unavailable"


@pytest.mark.parametrize(
    "error,key",
    [
        (RateLimited(60), "write_rate_limited"),
        (AuthenticationError("expired"), "write_authentication_failed"),
        (WriteNotConfirmed("not confirmed"), "write_not_confirmed"),
        (WriteValidationError("stale"), "holiday_changed"),
        (KeyError("path"), "holiday_changed"),
        (InvalidPayload("bad"), "write_failed"),
    ],
)
async def test_holiday_mode_write_failures_are_explained_and_stay_open(
    hass, error, key
) -> None:
    entry, coordinator = flow_entry(
        hass, {WDDW2_HOLIDAY_PATH: RESOURCES[WDDW2_HOLIDAY_PATH]}
    )
    coordinator.async_write_control = AsyncMock(side_effect=error)
    result = await _start_holiday_options(hass, entry)
    assert result["step_id"] == "holiday_mode"
    shown = result["data_schema"].schema
    current = next(iter(shown)).default()
    changed = "on" if current != "on" else "off"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"holiday_mode": changed}
    )
    assert result["step_id"] == "holiday_mode"
    assert result["errors"] == {"base": key}
    coordinator.async_write_control.assert_awaited_once()


async def test_holiday_mode_step_of_an_unloaded_entry_aborts(hass) -> None:
    entry, coordinator = flow_entry(
        hass, {WDDW2_HOLIDAY_PATH: RESOURCES[WDDW2_HOLIDAY_PATH]}
    )
    coordinator.async_write_control = AsyncMock()
    result = await _start_holiday_options(hass, entry)
    entry.runtime_data = None
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"holiday_mode": "on"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert await outcome(hass, result) == "holiday_changed"
    coordinator.async_write_control.assert_not_awaited()


async def test_a_holiday_saved_after_the_entry_was_unloaded_is_refused(hass) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    values = _holiday_form_input(result)
    entry.runtime_data = None
    result = await _configure_options(hass, result["flow_id"], values)
    assert result["type"] is FlowResultType.ABORT
    assert await outcome(hass, result) == "not_loaded"
    coordinator.async_update_holiday.assert_not_awaited()


@pytest.mark.parametrize(
    "overrides",
    [
        {"holiday_assigned_to": "hc1"},
        {"holiday_assigned_to": [1]},
        {"holiday_heating_mode": 5},
    ],
)
async def test_malformed_holiday_choices_never_reach_a_write(hass, overrides) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry)
    values = {**_holiday_form_input(result), **overrides}
    flow = _flow(hass, result, "options")
    shown = await flow.async_step_holiday(values)
    assert shown["type"] is FlowResultType.FORM
    assert shown["errors"] == {"base": "write_validation_failed"}
    coordinator.async_update_holiday.assert_not_awaited()


async def test_a_cancelled_delete_of_another_holiday_is_not_applied(hass) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _open_existing_holiday(hass, entry, action="delete_holiday")
    flow = _flow(hass, result, "options")
    flow._displayed_choice = None
    shown = await flow.async_step_delete_holiday({"confirm_delete": False})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "holiday_changed"
    coordinator.async_delete_holiday.assert_not_awaited()


async def test_creating_a_holiday_needs_a_destination_that_can_create(hass) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    result = await _start_holiday_options(hass, entry)
    flow = _flow(hass, result, "options")
    # The write settings of the system are no longer offered.
    from custom_components.bosch_buderus_heating.holidays import (
        HOLIDAY_CONFIGURATION_PATH,
    )

    coordinator.data[HOLIDAY_CONFIGURATION_PATH].available = False
    shown = await flow.async_step_holiday_gateway()
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "no_writable_holidays"


async def test_a_listed_holiday_without_an_id_is_not_offered(hass) -> None:
    entry, coordinator = _entry_with_writable_holiday(hass)
    snapshot = coordinator.data[HOLIDAY_LIST_PATH]
    valid = snapshot.resource.value[0]
    coordinator.data[HOLIDAY_LIST_PATH].resource = Resource(
        path=HOLIDAY_LIST_PATH,
        value=[{k: v for k, v in valid.items() if k != "id"}, valid],
        has_value=True,
    )
    result = await _start_holiday_options(hass, entry)
    options = next(iter(result["data_schema"].schema.values())).config["options"]
    periods = [o for o in options if o["value"] not in ("create", "back")]
    assert len(periods) == 1


# --- local-only start and the follow-up dialog ------------------------------


async def _local_only(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local_only"}
    )


async def test_local_only_start_with_an_unusable_configuration_creates_nothing(
    hass,
) -> None:
    with patch(CF + ".load_configuration", side_effect=ConfigurationError("x")):
        result = await _local_only(hass)
    assert result["type"] is FlowResultType.ABORT
    assert await outcome(hass, result) == "configuration_changed"
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize("failure", ["raises", "aborted"])
async def test_the_entry_exists_even_if_the_local_setup_cannot_be_opened(
    hass, failure
) -> None:
    options = hass.config_entries.options
    if failure == "raises":
        patched = patch.object(
            type(options), "async_init", AsyncMock(side_effect=RuntimeError("down"))
        )
    else:
        patched = patch.object(
            type(options),
            "async_init",
            AsyncMock(return_value={"type": FlowResultType.ABORT, "reason": "x"}),
        )
    with patched:
        result = await _local_only(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "next_flow" not in result
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_shows_the_form_again_without_input(hass) -> None:
    entry = _reconfigure_entry(hass)
    result = await _start_reconfigure(
        hass, entry, AsyncMock(return_value=(Gateway("gateway-one"),))
    )
    flow = _flow(hass, result)
    shown = await flow.async_step_reconfigure()
    assert shown["type"] is FlowResultType.FORM
    assert shown["step_id"] == "reconfigure"


async def test_a_holiday_circuit_without_a_known_kind_keeps_its_name() -> None:
    from custom_components.bosch_buderus_heating.config_flow import (
        _holiday_circuit_label,
    )

    assert _holiday_circuit_label("custom", "en") == "custom"
    assert _holiday_circuit_label("hc2", "en") == "Heating circuit 2"
    assert _holiday_circuit_label("dhw1", "de") == "Warmwasser 1"


async def test_an_unconfirmed_update_without_a_read_back_stays_open(hass) -> None:
    from custom_components.bosch_buderus_heating.holiday_writes import (
        HolidayWriteNotConfirmed,
    )

    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.async_update_holiday.side_effect = HolidayWriteNotConfirmed(None)
    result = await _open_existing_holiday(hass, entry)
    values = {
        **_holiday_form_input(result),
        "holiday_fix_temperature": 18.0,
        "holiday_assigned_to": ["hc1", "dhw1", "vent1"],
    }
    result = await _configure_options(hass, result["flow_id"], values)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "holiday"
    assert result["errors"] == {"base": "write_not_confirmed"}
    coordinator.async_update_holiday.assert_awaited_once()
