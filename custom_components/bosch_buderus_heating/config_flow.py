"""Config flow for Bosch/Buderus Home Comfort."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from time import monotonic
from typing import Any, cast, override

from homeassistant.components import persistent_notification
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    FlowType,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util import dt as dt_util

from .auth_lifecycle import entry_auth
from .bacon import flow as bacon_flow
from .bacon.models import CONF_BACON_ACCOUNT, CONF_BACON_IDS, BaconError
from .cloud_auth_flow import CloudAuthorizationFlow
from .cloud_configuration import (
    CLAIM_ABORTS,
    CloudDiscovery,
    claim_cloud_setup,
    ensure_active_cloud_flow,
    new_cloud_configuration,
    renew_cloud_claim,
)
from .cloud_options_flow import CloudOptionsFlow
from .configuration import CLOUD_KIND, ConfigurationError, load_configuration
from .configuration_access import CredentialContext, EntryConfiguration
from .configuration_management import (
    ConfigurationManagement,
    ConnectionEdit,
    not_found_bindings,
)
from .const import (
    CONF_BRAND,
    CONF_FAULT_NOTIFICATIONS,
    CONF_GATEWAY_IDS,
    CONF_POLLING_PROFILE,
    DEFAULT_POLLING_PROFILE,
    DOMAIN,
    RATE_LIMIT_ISSUE_PREFIX,
    PollingProfile,
)
from .controls_flow import ControlsOptionsFlow
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .data import tokens_to_data
from .entry_sources import notification_gateways, pointt_coordinators
from .enum_translation import writable_enum_options
from .ha_schema import validation as vol
from .holiday_ui import (
    DELETE_FIELD,
    HolidayDateTimeSelector,
    HolidayDeleteSelector,
    HolidayFormSchema,
)
from .holiday_writes import (
    HolidayAlreadyExists,
    HolidayChangedError,
    HolidayTimeError,
    HolidayTimeRangeError,
    HolidayTimeStepError,
    HolidayWriteNotConfirmed,
    configure_holiday_values,
    create_holiday_values,
    holiday_resources_from_snapshots,
    update_holiday_values,
)
from .holidays import (
    HOLIDAY_CREATE_PATH,
    HolidayPeriod,
    HolidayWriteConfiguration,
    HolidayWriteValues,
    holiday_period_id,
    holiday_timezone,
    icom_write_values,
    parse_holiday_state,
    parse_holiday_write_configuration,
)
from .legacy_holidays import WDDW2_HOLIDAY_PATH
from .local_credentials import LOCAL_STORAGE_MINOR_VERSION
from .local_flow import LocalSetupFlow
from .notifications import (
    gateway_notification_key,
    installation_name,
    notification_id,
    notification_policy,
)
from .pointt import (
    AuthenticationError,
    AuthTokens,
    Brand,
    Gateway,
    OAuthClient,
    PointTClient,
    PointTError,
    RateLimited,
    RequestTimeout,
    Resource,
    ServiceUnavailable,
    WriteNotConfirmed,
    WriteValidationError,
)
from .pointt.retry_after import bounded_cloud_pause
from .rrc_flow import RrcScheduleOptionsFlow
from .runtime import BoschBuderusRuntimeData
from .writes import WDDW2_HOLIDAY_POLICY, WriteBusy, assess_control

CONF_HOLIDAY_PERIOD = "holiday_period"
CONF_HOLIDAY_ASSIGNED_TO = "holiday_assigned_to"
CONF_HOLIDAY_HEATING_MODE = "holiday_heating_mode"
CONF_HOLIDAY_DHW_MODE = "holiday_dhw_mode"
CONF_HOLIDAY_VENTILATION_MODE = "holiday_ventilation_mode"
CONF_HOLIDAY_THERMAL_DISINFECTION = "holiday_thermal_disinfection"
CONF_HOLIDAY_FIX_TEMPERATURE = "holiday_fix_temperature"

BRAND_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BRAND): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=Brand.BOSCH.value, label="Bosch"),
                    SelectOptionDict(value=Brand.BUDERUS.value, label="Buderus"),
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
    }
)

_LOGGER = logging.getLogger(__name__)

# P49b step 7: the marker that opens an options flow at the local setup.
LOCAL_FIRST_START = "local_first_start"


class BoschBuderusConfigFlow(
    LocalSetupFlow, CloudAuthorizationFlow, ConfigFlow, domain=DOMAIN
):
    """Configure a PointT account and its gateways."""

    VERSION = 4
    MINOR_VERSION = 2

    _new_cloud_listing: CloudDiscovery | None = None
    _brand: Brand | None = None
    _tokens: AuthTokens | None = None
    _gateways: tuple[Gateway, ...] = ()
    _bacon_ids: tuple[str, ...] = ()
    _bacon_account: str = ""
    _selected_bacon: list[str] | None = None
    _bacon_retry_at: float = 0
    _bacon_unavailable: bool = False
    _reauth_entry: ConfigEntry | None = None
    _reconfigure_entry: ConfigEntry | None = None
    _pending_polling_profile: PollingProfile = DEFAULT_POLLING_PROFILE
    _auth_retry_at: float = 0.0
    _discovery_retry_at: float = 0.0
    _managed_edit: ConnectionEdit | None = None
    _configuration_context: (
        tuple[CredentialContext, dict[str, Any], PollingProfile] | None
    ) = None

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the actions exposed through the integration options dialog."""
        return BoschBuderusOptionsFlow(config_entry)

    # P49b step 7: continue with the local setup of the new entry.
    _then_local = False

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the accesses: cloud, cloud and local, or local only."""
        return self.async_show_menu(
            step_id="user", menu_options=["cloud", "cloud_local", "local_only"]
        )

    async def async_step_cloud_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The cloud first, then the local access of the same entry."""
        self._then_local = True
        return await self.async_step_cloud()

    async def async_step_local_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """An entry without a cloud; its plant is set up locally next.

        Not checked for local support here: the component is only set up once
        the entry is added; the local setup itself checks it then.
        """
        data = {
            "revision": 0,
            "connections": {},
            "targets": {},
            "bindings": {},
            "legacy_extra": {},
        }
        options: dict[str, Any] = {"connections": {}, "targets": {}, "bindings": {}}
        try:
            load_configuration(
                self.flow_id,
                data,
                options,
                minor_version=LOCAL_STORAGE_MINOR_VERSION,
            )
        except ConfigurationError:
            return self.async_abort(reason="configuration_changed")
        await self.async_set_unique_id("installation_" + self.flow_id)
        self._then_local = True
        german = self.hass.config.language.startswith("de")
        return self.async_create_entry(
            title="Home Comfort (lokal)" if german else "Home Comfort (local)",
            data=data,
            options=options,
        )

    @override
    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        """Open the local setup of the new entry right away (P49b step 7)."""
        entry = result.get("result")
        if not self._then_local or not isinstance(entry, ConfigEntry):
            return result
        from .managed_entry import SETUP_ISSUE_PREFIX

        try:
            local = await self.hass.config_entries.options.async_init(
                entry.entry_id,
                # Marks the prepared flow for as long as it exists.
                context=cast(Any, {LOCAL_FIRST_START: True}),
                data={LOCAL_FIRST_START: True},
            )
            if local.get("type") in (FlowResultType.FORM, FlowResultType.MENU):
                result["next_flow"] = (FlowType.OPTIONS_FLOW, local["flow_id"])
                # The setup continues there: no repair note meanwhile.
                ir.async_delete_issue(
                    self.hass, DOMAIN, SETUP_ISSUE_PREFIX + entry.entry_id
                )
        except Exception:  # the entry exists; its options offer the setup
            _LOGGER.debug("The local setup could not be opened", exc_info=True)
        return result

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the application brand."""
        if self.VERSION == 4:
            try:
                claim_cloud_setup(
                    self.hass, self.flow_id, cast(dict[str, Any], self.context)
                )
            except ConfigurationError as error:
                return self.async_abort(
                    reason=str(error)
                    if str(error) in CLAIM_ABORTS
                    else "configuration_changed"
                )
        if user_input is not None:
            self._brand = Brand(user_input[CONF_BRAND])
            return self._start_authorization()
        return self.async_show_form(step_id="cloud", data_schema=BRAND_SCHEMA)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication for an existing entry."""
        self._reauth_entry = self._get_reauth_entry()
        connection_id = self.context.get("connection_id")
        if self._reauth_entry.version == 4:
            if not isinstance(connection_id, str):
                return self.async_abort(reason="connection_required")
            try:
                management = ConfigurationManagement(self.hass, self._reauth_entry)
                self._managed_edit = management.edit(connection_id)
                if management.current().connections[connection_id].kind != CLOUD_KIND:
                    return self.async_abort(reason="connection_settings_required")
            except ConfigurationError:
                return self.async_abort(reason="configuration_changed")
        configuration = EntryConfiguration(
            self._reauth_entry,
            connection_id if isinstance(connection_id, str) else None,
        )
        if self._managed_edit is None:
            self._configuration_context = configuration.dialog_context()
        self._brand = Brand(configuration.cloud_data()[CONF_BRAND])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm that the user wants to sign in again."""
        if user_input is not None:
            return self._start_authorization()
        return self.async_show_form(step_id="reauth_confirm")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update gateway selection, brand, and safe polling cadence."""
        if self._reconfigure_entry is None:
            self._reconfigure_entry = self._get_reconfigure_entry()
            if self._reconfigure_entry.version == 4:
                return self.async_abort(reason="connection_settings_required")
            configuration = EntryConfiguration(self._reconfigure_entry)
            self._configuration_context = configuration.dialog_context()
            self._brand = Brand(configuration.cloud_data()[CONF_BRAND])
            self._pending_polling_profile = configuration.polling_profile()
            return await self._async_prepare_reconfigure()

        if user_input is None:
            return self._show_reconfigure_form()

        selected = user_input.get(CONF_GATEWAY_IDS, [])
        try:
            self._selected_bacon = self._select_bacon_devices(user_input)
        except ValueError:
            return self._show_reconfigure_form(error="gateway_changed")
        if not isinstance(selected, list) or (
            not selected and not self._selected_bacon
        ):
            return self._show_reconfigure_form(error="select_gateway")
        known_ids = {gateway.gateway_id for gateway in self._gateways}
        if not all(item in known_ids for item in selected):
            return self._show_reconfigure_form(error="gateway_changed")

        brand = Brand(user_input[CONF_BRAND])
        self._pending_polling_profile = PollingProfile(user_input[CONF_POLLING_PROFILE])
        if brand is not self._brand:
            self._brand = brand
            return self._start_authorization()

        return self._finish_reconfigure(selected)

    async def async_step_reconfigure_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Retry gateway discovery for reconfiguration."""
        if user_input is not None:
            return await self._async_prepare_reconfigure()
        return self.async_show_form(step_id="reconfigure_retry")

    @override
    def _auth_brand(self) -> Brand | None:
        return self._brand

    @override
    async def _auth_blocked(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult | None:
        if self._auth_retry_at > monotonic():
            return self._show_auth_wait()
        return None

    @override
    def _auth_service_failed(self, error: PointTError) -> ConfigFlowResult:
        if isinstance(error, RateLimited):
            delay = error.retry_after
            if delay is None or not math.isfinite(delay):
                delay = 300.0
            self._auth_retry_at = monotonic() + max(60.0, delay)
            return self._show_auth_wait()
        if isinstance(error, RequestTimeout | ServiceUnavailable):
            return self._restart_authorization(error="cannot_connect")
        return self._restart_authorization(error="unknown")

    @override
    async def _auth_succeeded(self, tokens: AuthTokens) -> ConfigFlowResult:
        self._tokens = tokens
        return await self._async_discover_gateways()

    async def async_step_auth_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait out the server limit before issuing a fresh sign-in link."""
        if not renew_cloud_claim(cast(dict[str, Any], self.context)):
            return self.async_abort(reason="cloud_setup_taken_over")
        if self._auth_retry_at > monotonic():
            return self._show_auth_wait()
        self._auth_retry_at = 0.0
        return self._start_authorization()

    async def async_step_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Retry gateway discovery without repeating login."""
        if not renew_cloud_claim(cast(dict[str, Any], self.context)):
            return self.async_abort(reason="cloud_setup_taken_over")
        if user_input is not None:
            return await self._async_discover_gateways()
        return self.async_show_form(step_id="retry")

    async def async_step_selection_expired(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List the devices again after an expired selection, keeping the sign-in."""
        if not renew_cloud_claim(cast(dict[str, Any], self.context)):
            return self.async_abort(reason="cloud_setup_taken_over")
        if user_input is not None:
            return await self._async_discover_gateways()
        return self.async_show_form(step_id="selection_expired")

    async def async_step_gateways(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one or more gateways for this entry."""
        if not renew_cloud_claim(cast(dict[str, Any], self.context)):
            return self.async_abort(reason="cloud_setup_taken_over")
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input.get(CONF_GATEWAY_IDS, [])
            try:
                self._selected_bacon = self._select_bacon_devices(user_input)
            except ValueError:
                self._selected_bacon = None
            if (
                not isinstance(selected, list)
                or self._selected_bacon is None
                or (not selected and not self._selected_bacon)
                or not all(
                    isinstance(item, str)
                    and item in {g.gateway_id for g in self._gateways}
                    for item in selected
                )
            ):
                errors["base"] = "select_gateway"
            elif self._brand is None or self._tokens is None:
                return self.async_abort(reason="invalid_flow")
            else:
                if self.VERSION == 4 and self._reconfigure_entry is None:
                    return await self._create_managed_cloud(selected)
                duplicate = self._find_duplicate_entry(set(selected))
                if duplicate is not None:
                    return self.async_abort(reason="already_configured")

                await self.async_set_unique_id(self._account_identity())
                self._abort_if_unique_id_configured()
                if self._reconfigure_entry is not None:
                    ir.async_delete_issue(
                        self.hass,
                        DOMAIN,
                        f"{RATE_LIMIT_ISSUE_PREFIX}{self._reconfigure_entry.entry_id}",
                    )
                    return self._update_and_restart(
                        self._reconfigure_entry,
                        unique_id=self._account_identity(),
                        title=self._reconfigure_entry.title,
                        data_updates={
                            CONF_BRAND: self._brand.value,
                            CONF_GATEWAY_IDS: selected,
                            **bacon_flow.config_data(
                                self._selected_bacon, self._bacon_account
                            ),
                            **tokens_to_data(self._tokens),
                        },
                        options={
                            CONF_POLLING_PROFILE: self._pending_polling_profile.value,
                        },
                    )
                return self.async_create_entry(
                    title=f"{self._brand.value.title()} Home Comfort",
                    data={
                        CONF_BRAND: self._brand.value,
                        CONF_GATEWAY_IDS: selected,
                        **bacon_flow.config_data(
                            self._selected_bacon, self._bacon_account
                        ),
                        **tokens_to_data(self._tokens),
                    },
                )

        return self.async_show_form(
            step_id="gateways",
            data_schema=bacon_flow.extend_schema(
                _gateway_schema(self._gateways),
                self._bacon_ids,
                has_gateways=bool(self._gateways),
            ),
            errors=errors
            or (
                {"base": "bacon_selection_unavailable"}
                if self._bacon_unavailable
                else {}
            ),
        )

    async def _create_managed_cloud(self, selected: list[str]) -> ConfigFlowResult:
        if (
            self._new_cloud_listing is None
            or self._tokens is None
            or self._brand is None
        ):
            return self.async_abort(reason="invalid_flow")
        await self.async_set_unique_id("installation_" + self.flow_id)
        try:
            proposed = new_cloud_configuration(
                self.hass,
                self.flow_id,
                self.flow_id,
                self._brand,
                self._tokens,
                self._new_cloud_listing,
                set(selected),
                set(self._selected_bacon or []),
            )
        except ConfigurationError as error:
            if str(error) == "cloud_selection_expired":
                # A slow selection lists the devices again, without a new sign-in.
                self._new_cloud_listing = None
                return self.async_show_form(step_id="selection_expired")
            return self.async_abort(
                reason=str(error)
                if str(error)
                in {
                    *CLAIM_ABORTS,
                    "cloud_target_owned",
                    "cloud_local_first_other_entry",
                }
                else "configuration_changed"
            )
        except BaconError:
            return self.async_abort(reason="configuration_changed")
        return self.async_create_entry(
            title=f"{self._brand.value.title()} Home Comfort",
            data=proposed.to_data(),
            options=proposed.to_options(),
        )

    def _select_bacon_devices(self, user_input: dict[str, Any]) -> list[str]:
        if not self._bacon_unavailable:
            return bacon_flow.selected_devices(user_input, self._bacon_ids)
        previous = self._reconfigure_entry
        selected = (
            list(EntryConfiguration(previous).cloud_data().get(CONF_BACON_IDS, []))
            if previous
            else []
        )
        if CONF_BACON_IDS in user_input and user_input[CONF_BACON_IDS] != selected:
            raise ValueError("Cloud device selection is unavailable")
        return selected

    async def _discover_bacon(self, tokens: AuthTokens) -> None:
        """Keep a cloud outage separate from verified PointT discovery."""
        self._bacon_unavailable = False
        backing_off = monotonic() < self._bacon_retry_at
        try:
            if backing_off:
                raise BaconError(
                    "service_unavailable", self._bacon_retry_at - monotonic()
                )
            self._bacon_ids, self._bacon_account = await bacon_flow.discover(
                async_get_clientsession(self.hass), tokens
            )
        except BaconError as err:
            if err.category in {"authentication", "account_changed"}:
                raise
            if not backing_off:
                self._bacon_retry_at = monotonic() + max(60, err.retry_after)
            self._bacon_ids = ()
            self._bacon_account = ""
            self._bacon_unavailable = True
            previous = self._reconfigure_entry
            if not self._gateways:
                raise
            if self._reauth_entry and EntryConfiguration(
                self._reauth_entry
            ).cloud_data().get(CONF_BACON_IDS):
                visible = {gateway.gateway_id for gateway in self._gateways}
                if not visible.intersection(
                    EntryConfiguration(self._reauth_entry)
                    .cloud_data()
                    .get(CONF_GATEWAY_IDS, [])
                ):
                    raise
            if previous and EntryConfiguration(previous).cloud_data().get(
                CONF_BACON_IDS
            ):
                # Do not transfer hidden cloud selections to another account/brand.
                visible = {gateway.gateway_id for gateway in self._gateways}
                if (
                    self._brand is None
                    or EntryConfiguration(previous).cloud_data().get(CONF_BRAND)
                    != self._brand.value
                    or not visible.intersection(
                        EntryConfiguration(previous)
                        .cloud_data()
                        .get(CONF_GATEWAY_IDS, [])
                    )
                ):
                    raise
                self._bacon_account = (
                    EntryConfiguration(previous)
                    .cloud_data()
                    .get(CONF_BACON_ACCOUNT, "")
                )

    async def _async_discover_gateways(self) -> ConfigFlowResult:
        if self._tokens is None:
            return self.async_abort(reason="invalid_flow")
        if self._managed_edit is not None and self._reauth_entry is not None:
            return await self._async_finish_managed_reauth()
        if self.VERSION == 4 and monotonic() < self._discovery_retry_at:
            return self.async_show_form(
                step_id="retry", errors={"base": "cloud_rate_limited"}
            )
        if monotonic() < self._bacon_retry_at and not self._gateways:
            return self.async_show_form(
                step_id="retry", errors={"base": "bacon_unavailable"}
            )
        session = async_get_clientsession(self.hass)
        try:
            self._gateways = await PointTClient(
                session, self._tokens.access_token
            ).get_gateways()
            await self._discover_bacon(self._tokens)
        except BaconError as err:
            if err.category in {"authentication", "account_changed"}:
                return self._restart_authorization(error="invalid_auth")
            return self.async_show_form(
                step_id="retry", errors={"base": "bacon_unavailable"}
            )
        except AuthenticationError:
            return self._restart_authorization(error="invalid_auth")
        except RateLimited as error:
            if self.VERSION == 4:
                delay = error.retry_after
                if delay is None or not math.isfinite(delay):
                    delay = 300.0
                self._discovery_retry_at = monotonic() + bounded_cloud_pause(delay)
                return self.async_show_form(
                    step_id="retry", errors={"base": "cloud_rate_limited"}
                )
            return self.async_show_form(step_id="retry", errors={"base": "unknown"})
        except RequestTimeout, ServiceUnavailable:
            return self.async_show_form(
                step_id="retry", errors={"base": "cannot_connect"}
            )
        except PointTError:
            return self.async_show_form(step_id="retry", errors={"base": "unknown"})

        if not self._gateways and not self._bacon_ids:
            return self.async_abort(reason="no_gateways")
        if self._reauth_entry is not None:
            selected = set(
                EntryConfiguration(self._reauth_entry)
                .cloud_data()
                .get(CONF_GATEWAY_IDS, [])
            )
            visible = {gateway.gateway_id for gateway in self._gateways}
            bacon_matches = (
                not self._bacon_unavailable
                and bool(
                    EntryConfiguration(self._reauth_entry)
                    .cloud_data()
                    .get(CONF_BACON_IDS)
                )
                and EntryConfiguration(self._reauth_entry)
                .cloud_data()
                .get(CONF_BACON_ACCOUNT)
                == self._bacon_account
            )
            if (not selected or selected.isdisjoint(visible)) and not bacon_matches:
                return self.async_abort(reason="reauth_wrong_account")
            return self._update_and_restart(
                self._reauth_entry,
                data_updates=tokens_to_data(self._tokens),
                reason="reauth_successful",
            )
        if self.VERSION == 4:
            self._new_cloud_listing = CloudDiscovery(
                self._gateways, self._bacon_ids, self._bacon_account
            )
        return await self.async_step_gateways()

    async def _async_finish_managed_reauth(self) -> ConfigFlowResult:
        assert (
            self._reauth_entry is not None
            and self._managed_edit is not None
            and self._tokens is not None
        )
        try:
            await ConfigurationManagement(
                self.hass, self._reauth_entry
            ).async_reauthenticate(
                self._managed_edit,
                self._tokens,
                request_guard=lambda: ensure_active_cloud_flow(self.hass, self.flow_id),
            )
        except ConfigurationError as error:
            if str(error) in ("reauth_wrong_account", "reauth_no_targets"):
                return self.async_abort(reason=str(error))
            if str(error) in ("reauth_targets_missing", "unverifiable_targets"):
                return self.async_show_form(
                    step_id="retry", errors={"base": "reauth_targets_missing"}
                )
            return self.async_abort(reason="configuration_changed")
        except AuthenticationError:
            return self._restart_authorization(error="invalid_auth")
        except BaconError as error:
            if error.category in {"authentication", "account_changed"}:
                return self._restart_authorization(error="invalid_auth")
            return self.async_show_form(
                step_id="retry", errors={"base": "cannot_connect"}
            )
        except PointTError:
            return self.async_show_form(
                step_id="retry", errors={"base": "cannot_connect"}
            )
        from .managed_entry import ManagedEntry

        runtime = getattr(self._reauth_entry, "runtime_data", None)
        if isinstance(runtime, ManagedEntry):
            self.hass.loop.call_soon(runtime.schedule_reauthentication)
        if missing := not_found_bindings(
            self.hass, self._reauth_entry, self._managed_edit.connection_id
        ):
            # Renewed; a repair note names the systems that were not found.
            return self.async_abort(
                reason="reauth_successful_targets_missing",
                description_placeholders={"count": str(len(missing))},
            )
        return self.async_abort(reason="reauth_successful")

    async def _async_prepare_reconfigure(self) -> ConfigFlowResult:
        """Discover the current account gateways before showing safe choices."""
        if self._reconfigure_entry is None:
            return self.async_abort(reason="invalid_flow")
        if monotonic() < self._bacon_retry_at and not self._gateways:
            return self.async_show_form(
                step_id="reconfigure_retry", errors={"base": "bacon_unavailable"}
            )
        try:
            self._gateways = await self._reconfigure_client().get_gateways()
            await self._discover_bacon(
                EntryConfiguration(self._reconfigure_entry).tokens()
            )
        except BaconError as err:
            if err.category in {"authentication", "account_changed"}:
                return self.async_abort(reason="reauth_required")
            return self.async_show_form(
                step_id="reconfigure_retry", errors={"base": "bacon_unavailable"}
            )
        except AuthenticationError:
            return self.async_abort(reason="reauth_required")
        except RequestTimeout, ServiceUnavailable:
            return self.async_show_form(
                step_id="reconfigure_retry", errors={"base": "cannot_connect"}
            )
        except PointTError:
            return self.async_show_form(
                step_id="reconfigure_retry", errors={"base": "unknown"}
            )
        if not self._gateways and not self._bacon_ids:
            return self.async_abort(reason="no_gateways")
        return self._show_reconfigure_form()

    def _reconfigure_client(self) -> PointTClient:
        """Reuse the live token manager or construct one for an unloaded entry."""
        if self._reconfigure_entry is None:
            raise RuntimeError("Reconfigure entry is missing")
        runtime = getattr(self._reconfigure_entry, "runtime_data", None)
        session = async_get_clientsession(self.hass)
        manager, backoff = entry_auth(
            self.hass, self._reconfigure_entry, OAuthClient(session)
        )
        if (
            isinstance(runtime, BoschBuderusRuntimeData)
            and runtime.token_manager is manager
        ):
            return runtime.client
        return PointTClient(session, manager, backoff=backoff)

    def _show_reconfigure_form(self, *, error: str | None = None) -> ConfigFlowResult:
        """Show current selections and explain that saving runs discovery."""
        if self._reconfigure_entry is None or self._brand is None:
            return self.async_abort(reason="invalid_flow")
        selected = (
            EntryConfiguration(self._reconfigure_entry)
            .cloud_data()
            .get(CONF_GATEWAY_IDS, [])
        )
        errors = (
            {"base": error}
            if error
            else (
                {"base": "bacon_selection_unavailable"}
                if self._bacon_unavailable
                else None
            )
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=bacon_flow.extend_schema(
                _reconfigure_schema(
                    self._gateways,
                    selected if isinstance(selected, list) else [],
                    self._brand,
                    self._pending_polling_profile,
                ),
                self._bacon_ids,
                selected=list(
                    EntryConfiguration(self._reconfigure_entry)
                    .cloud_data()
                    .get(CONF_BACON_IDS, [])
                ),
                has_gateways=bool(self._gateways),
            ),
            errors=errors,
        )

    def _finish_reconfigure(self, selected: list[str]) -> ConfigFlowResult:
        """Save current-brand choices and reload to run full discovery."""
        if self._reconfigure_entry is None:
            return self.async_abort(reason="invalid_flow")
        if self._pending_polling_profile is PollingProfile.CLOUD_FRIENDLY:
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                f"{RATE_LIMIT_ISSUE_PREFIX}{self._reconfigure_entry.entry_id}",
            )
        return self._update_and_restart(
            self._reconfigure_entry,
            data_updates={
                CONF_GATEWAY_IDS: selected,
                **bacon_flow.config_data(
                    self._selected_bacon or [], self._bacon_account
                ),
            },
            options={
                CONF_POLLING_PROFILE: self._pending_polling_profile.value,
            },
        )

    @callback
    def _update_and_restart(
        self,
        entry: ConfigEntry,
        *,
        data_updates: dict[str, Any],
        options: dict[str, Any] | None = None,
        reason: str | None = None,
        unique_id: str | None = None,
        title: str | None = None,
    ) -> ConfigFlowResult:
        """Use the loaded entry's listener, or explicitly retry an unloaded entry."""
        if self.hass.config_entries.async_get_entry(entry.entry_id) is not entry:
            return self.async_abort(reason="configuration_changed")
        try:
            configuration = EntryConfiguration(entry)
            if self._configuration_context is not None and (
                configuration.dialog_context() != self._configuration_context
            ):
                return self.async_abort(reason="configuration_changed")
            updated_data = configuration.legacy_update(data_updates)
        except ValueError:
            return self.async_abort(reason="configuration_changed")
        updated_options = {**entry.options, **(options or {})}
        changed = updated_data != entry.data or updated_options != entry.options
        arguments: dict[str, Any] = {"data": updated_data, "options": updated_options}
        if reason is not None:
            arguments["reason"] = reason
        if unique_id is not None:
            arguments["unique_id"] = unique_id
        if title is not None:
            arguments["title"] = title
        result = self.async_update_and_abort(entry, **arguments)
        if (
            not changed
            or entry.state is not ConfigEntryState.LOADED
            or not entry.update_listeners
        ):
            # Unchanged reconfiguration still requests rediscovery. Entries
            # waiting for setup/reauthentication do not have a live listener.
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return result

    def _show_auth_wait(self) -> ConfigFlowResult:
        """Display the remaining wait without starting another cloud request."""
        seconds = max(0, math.ceil(self._auth_retry_at - monotonic()))
        return self.async_show_form(
            step_id="auth_wait",
            description_placeholders={"seconds": str(seconds)},
        )

    def _find_duplicate_entry(self, selected: set[str]) -> ConfigEntry | None:
        if self._brand is None:
            return None
        for entry in self._async_current_entries():
            if entry is self._reconfigure_entry:
                continue
            if (
                EntryConfiguration(entry).cloud_data().get(CONF_BRAND)
                != self._brand.value
            ):
                continue
            if (
                self._bacon_account
                and EntryConfiguration(entry).cloud_data().get(CONF_BACON_ACCOUNT)
                == self._bacon_account
            ):
                return entry
            configured = (
                EntryConfiguration(entry).cloud_data().get(CONF_GATEWAY_IDS, [])
            )
            if isinstance(configured, list) and selected.intersection(configured):
                return entry
        return None

    def _account_identity(self) -> str:
        assert self._brand is not None
        if self._gateways:
            return _account_fingerprint(self._brand, self._gateways)
        return f"{self._brand.value}:bacon:{self._bacon_account}"


@dataclass(frozen=True, slots=True)
class _HolidayChoice:
    """One currently writable holiday and its cloud-advertised capabilities."""

    coordinator: BoschBuderusDataUpdateCoordinator
    period: HolidayPeriod
    configuration: HolidayWriteConfiguration
    label: str


class BoschBuderusOptionsFlow(
    LocalSetupFlow, ControlsOptionsFlow, RrcScheduleOptionsFlow, CloudOptionsFlow
):
    """Manage holidays and installation-specific notification settings."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._selected_key: str | None = None
        self._displayed_choice: _HolidayChoice | None = None
        self._holiday_draft: dict[str, Any] | None = None
        self._delete_choice: _HolidayChoice | None = None
        self._delete_return_input: dict[str, Any] | None = None
        self._notification_gateway_keys: tuple[str, ...] | None = None
        self._holiday_mode_source: BoschBuderusDataUpdateCoordinator | None = None
        self._holiday_mode_displayed: Resource | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep local notification settings accessible even when offline."""
        if (
            user_input is not None
            and user_input.get(LOCAL_FIRST_START) is True
            and self._entry.version == 4
        ):
            # Continued from the setup of this entry (P49b step 7).
            return await self.async_step_local_setup()
        prepared = [
            flow
            for flow in self.hass.config_entries.options.async_progress_by_handler(
                self._entry.entry_id
            )
            if flow["context"].get(LOCAL_FIRST_START) is True
            and flow["flow_id"] != self.flow_id
        ]
        if user_input is None and prepared:
            # The frontend of the tested Home Assistant releases does not
            # continue the options flow named in `next_flow`: after the setup
            # it starts a new one (a POST to the options flow endpoint rather
            # than a GET of the prepared flow). This one takes over and the
            # prepared one ends quietly: it is handed off, not abandoned.
            for flow in prepared:
                cast(dict[str, Any], flow["context"])["handed_off"] = True
                with suppress(UnknownFlow):
                    self.hass.config_entries.options.async_abort(flow["flow_id"])
            return await self.async_step_local_setup()
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                *(["connections"] if self._entry.version == 4 else []),
                *(
                    ["local_setup"]
                    if self._entry.version == 4 and self.local_available()
                    else []
                ),
                *(
                    ["connection_add_cloud"]
                    if self._entry.version == 4 and self.cloud_add_available()
                    else []
                ),
                *(["rescan"] if self._entry.version == 4 else []),
                "controls",
                "notifications",
                "holidays",
                *(["rrc_programs"] if self._rrc_choices() else []),
            ],
        )

    async def async_step_rescan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A6: search the system again, through the cloud and the local access.

        Only plans the search: it runs with the next regular update and within
        the existing limits, and is refused while paused or already running.
        """
        from .managed_entry import ManagedEntry
        from .rescan import request_rescan

        owner = getattr(self._entry, "runtime_data", None)
        if not isinstance(owner, ManagedEntry):
            return self.async_abort(reason="rescan_unavailable")
        if user_input is None:
            return self.async_show_form(step_id="rescan", data_schema=vol.Schema({}))
        return self.async_abort(reason=f"rescan_{request_rescan(owner)}")

    async def async_step_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select notifications per installation without querying PointT."""
        gateway_ids = notification_gateways(self._entry)
        if not gateway_ids:
            return self.async_abort(reason="no_notification_gateways")
        current_keys = tuple(gateway_notification_key(item) for item in gateway_ids)
        if self._notification_gateway_keys is None:
            self._notification_gateway_keys = current_keys
        if current_keys != self._notification_gateway_keys:
            return self.async_abort(reason="notification_gateways_changed")
        if user_input is not None:
            if len(gateway_ids) == 1:
                enabled_keys = (
                    set(current_keys) if user_input[CONF_FAULT_NOTIFICATIONS] else set()
                )
            else:
                enabled_keys = set(user_input[CONF_FAULT_NOTIFICATIONS])
                if not enabled_keys.issubset(current_keys):
                    return self.async_abort(reason="notification_gateways_changed")
            options: dict[str, Any] = {}
            for gateway_id, key in zip(gateway_ids, current_keys, strict=True):
                was_enabled, reset = notification_policy(
                    self._entry.options, gateway_id
                )
                enabled = key in enabled_keys
                options[key] = {
                    "enabled": enabled,
                    "reset": reset + int(enabled and not was_enabled),
                }
                if not enabled:
                    persistent_notification.async_dismiss(
                        self.hass, notification_id(self._entry.entry_id, gateway_id)
                    )
            return self.async_create_entry(
                title="",
                data={**self._entry.options, CONF_FAULT_NOTIFICATIONS: options},
            )
        selected = [
            key
            for gateway_id, key in zip(gateway_ids, current_keys, strict=True)
            if notification_policy(self._entry.options, gateway_id)[0]
        ]
        if len(gateway_ids) == 1:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_FAULT_NOTIFICATIONS, default=bool(selected)
                    ): BooleanSelector()
                }
            )
        else:
            names = [
                installation_name(self.hass, self._entry.entry_id, gateway_id, index)
                for index, gateway_id in enumerate(gateway_ids, 1)
            ]
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_FAULT_NOTIFICATIONS, default=selected
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=key,
                                    label=f"{name} ({index})"
                                    if names.count(name) > 1
                                    else name,
                                )
                                for index, (key, name) in enumerate(
                                    zip(current_keys, names, strict=True), 1
                                )
                            ],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            )
        return self.async_show_form(step_id="notifications", data_schema=schema)

    async def async_step_holidays(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the holiday that should be configured."""
        choices = self._holiday_choices()
        if choices is None:
            return self.async_abort(reason="not_loaded")
        mode_choices = self._holiday_mode_choices()
        if not choices and not mode_choices:
            return self.async_abort(reason="no_writable_holidays")
        if not choices and len(mode_choices) == 1 and user_input is None:
            self._selected_key = next(iter(mode_choices))
            return await self.async_step_holiday_mode()
        if user_input is not None:
            selected = user_input.get(CONF_HOLIDAY_PERIOD)
            if isinstance(selected, str) and selected in mode_choices:
                self._selected_key = selected
                return await self.async_step_holiday_mode()
            if selected == "create":
                return await self.async_step_holiday_gateway()
            if isinstance(selected, str) and selected in choices:
                self._selected_key = selected
                self._displayed_choice = None
                self._holiday_draft = None
                self._delete_choice = None
                self._delete_return_input = None
                return await self.async_step_holiday()
            return self._show_holiday_selection(choices, error="holiday_changed")
        return self._show_holiday_selection(choices)

    async def async_step_delete_holiday(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delete only the period whose details the user has confirmed."""
        choice = (self._holiday_choices() or {}).get(self._selected_key or "")
        if choice is None or (self._selected_key or "").startswith("create:"):
            return self.async_abort(reason="holiday_changed")
        if user_input is not None and not user_input.get("confirm_delete"):
            displayed = self._displayed_choice
            if (
                displayed is None
                or displayed.coordinator is not choice.coordinator
                or holiday_period_id(displayed.period)
                != holiday_period_id(choice.period)
            ):
                return self.async_abort(reason="holiday_changed")
            draft = self._delete_return_input
            self._delete_return_input = None
            self._delete_choice = None
            return self._show_holiday_form(displayed, suggested_values=draft)
        if user_input is None:
            self._delete_return_input = dict(self._holiday_draft or {})
        errors: dict[str, str] | None = None
        if user_input is not None:
            displayed = self._delete_choice
            if (
                displayed is None
                or displayed.coordinator is not choice.coordinator
                or displayed.period.write_values != choice.period.write_values
                or holiday_period_id(displayed.period)
                != holiday_period_id(choice.period)
            ):
                return self.async_abort(reason="holiday_changed")
            holiday_id = holiday_period_id(displayed.period)
            if holiday_id is None or displayed.period.write_values is None:
                return self.async_abort(reason="holiday_changed")
            try:
                await choice.coordinator.async_delete_holiday(
                    holiday_id, expected=displayed.period.write_values
                )
            except HolidayChangedError:
                return self.async_abort(reason="holiday_changed")
            except WriteNotConfirmed:
                errors = {"base": "delete_not_confirmed"}
            except RateLimited:
                errors = {"base": "write_rate_limited"}
            except AuthenticationError:
                errors = {"base": "write_authentication_failed"}
            except WriteValidationError:
                errors = {"base": "holiday_changed"}
            except WriteBusy:
                errors = {"base": "write_busy"}
            except PointTError:
                errors = {"base": "delete_failed"}
            else:
                return self.async_abort(reason="holiday_deleted")
        self._delete_choice = choice
        return self.async_show_form(
            step_id="delete_holiday",
            data_schema=vol.Schema(
                {vol.Required("confirm_delete", default=False): BooleanSelector()}
            ),
            description_placeholders={"holiday": choice.label},
            errors=errors,
            last_step=True,
        )

    async def async_step_holiday_gateway(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the destination when several gateways can create holidays."""
        choices = {
            key: choice
            for key, choice in (self._holiday_choices() or {}).items()
            if key.startswith("create:")
        }
        if not choices:
            return self.async_abort(reason="no_writable_holidays")
        selected = (user_input or {}).get("holiday_gateway")
        if user_input is None and len(choices) == 1:
            selected = next(iter(choices))
        if isinstance(selected, str) and selected in choices:
            self._selected_key = selected
            self._displayed_choice = None
            self._holiday_draft = None
            return await self.async_step_new_holiday()
        return self.async_show_form(
            step_id="holiday_gateway",
            data_schema=vol.Schema(
                {
                    vol.Required("holiday_gateway"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=key, label=choice.label)
                                for key, choice in choices.items()
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors={"base": "holiday_changed"} if user_input is not None else None,
        )

    def _holiday_mode_choices(self) -> dict[str, BoschBuderusDataUpdateCoordinator]:
        """WDDW2 provides one mode and cannot supply calendar placeholders."""
        coordinators = pointt_coordinators(self._entry)
        if coordinators is None:
            return {}
        choices = {}
        for coordinator in coordinators:
            resource = holiday_resources_from_snapshots(coordinator.data or {}).get(
                WDDW2_HOLIDAY_PATH
            )
            if resource is not None and assess_control(resource).supports_switch:
                key = (
                    "mode:"
                    + hashlib.sha256(
                        coordinator.gateway.gateway_id.encode()
                    ).hexdigest()[:24]
                )
                choices[key] = coordinator
        return choices

    async def async_step_holiday_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the single WDDW2 mode in the existing native options dialog."""
        coordinator = self._holiday_mode_choices().get(self._selected_key or "")
        if coordinator is None:
            return self.async_abort(reason="holiday_changed")
        if user_input is not None and coordinator is not self._holiday_mode_source:
            return self.async_abort(reason="holiday_changed")
        self._holiday_mode_source = coordinator
        resource = holiday_resources_from_snapshots(coordinator.data or {})[
            WDDW2_HOLIDAY_PATH
        ]
        errors = None
        if user_input is not None:
            displayed = self._holiday_mode_displayed
            if displayed is None:
                return self.async_abort(reason="holiday_changed")
            if user_input["holiday_mode"] == displayed.value:
                # FLOW-4: an unchanged OK sends nothing, even if the system
                # changed meanwhile; a change is written against the value shown.
                return self.async_abort(reason="holiday_unchanged")
            try:
                await coordinator.async_write_control(
                    WDDW2_HOLIDAY_PATH,
                    user_input["holiday_mode"],
                    WDDW2_HOLIDAY_POLICY,
                    expected=(displayed,),
                )
            except RateLimited:
                errors = {"base": "write_rate_limited"}
            except AuthenticationError:
                errors = {"base": "write_authentication_failed"}
            except WriteNotConfirmed:
                errors = {"base": "write_not_confirmed"}
            except KeyError, WriteValidationError:
                errors = {"base": "holiday_changed"}
            except WriteBusy:
                errors = {"base": "write_busy"}
            except PointTError:
                errors = {"base": "write_failed"}
            else:
                return self.async_abort(reason="holiday_updated")
            resource = holiday_resources_from_snapshots(coordinator.data or {}).get(
                WDDW2_HOLIDAY_PATH, resource
            )
        self._holiday_mode_displayed = resource
        return self.async_show_form(
            step_id="holiday_mode",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "holiday_mode", default=resource.value
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=["on", "off"],
                            translation_key="holiday_mode",
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
            last_step=True,
        )

    async def async_step_legacy_holiday(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_holiday(user_input)

    async def async_step_new_legacy_holiday(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_new_holiday(user_input)

    async def async_step_new_holiday(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use the same fields and validation when creating a holiday."""
        return await self._async_process_holiday(user_input)

    async def async_step_holiday(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save directly, or open confirmation from the inline delete button."""
        if user_input is not None and user_input.get(DELETE_FIELD) is True:
            self._holiday_draft = {
                key: value for key, value in user_input.items() if key != DELETE_FIELD
            }
            return await self.async_step_delete_holiday()
        return await self._async_process_holiday(user_input)

    async def _async_process_holiday(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Validate the common form and save through the confirmed writer."""
        choices = self._holiday_choices()
        if choices is None:
            return self.async_abort(reason="not_loaded")
        choice = choices.get(self._selected_key or "")
        if choice is None:
            return self.async_abort(reason="holiday_changed")
        creating = (self._selected_key or "").startswith("create:")
        if creating:
            configuration = choice.configuration
            choice = replace(
                choice,
                period=replace(
                    choice.period,
                    write_values=HolidayWriteValues(
                        start_date="",
                        end_date="",
                        heating_mode=configuration.heating_mode,
                        dhw_mode=configuration.dhw_mode,
                        ventilation_mode=configuration.ventilation_mode,
                        assigned_to=configuration.assigned_to,
                        name=None,
                        thermal_disinfection=configuration.thermal_disinfection,
                        fix_temperature=configuration.fix_temperature,
                        source_path=configuration.source_path,
                    ),
                ),
            )

        errors: dict[str, str] | None = None
        suggested_values = dict(user_input) if user_input is not None else None
        if user_input is not None:
            displayed = self._displayed_choice
            if (
                displayed is None
                or displayed.coordinator is not choice.coordinator
                or holiday_period_id(displayed.period)
                != holiday_period_id(choice.period)
            ):
                return self.async_abort(reason="holiday_changed")
            current = displayed.period.write_values
            if current is None:
                return self.async_abort(reason="holiday_changed")
            validation_error = "invalid_holiday_details"
            try:
                assert suggested_values is not None
                timezone = holiday_timezone(
                    holiday_resources_from_snapshots(choice.coordinator.data or {}),
                    dt_util.get_default_time_zone(),
                    timezones=choice.coordinator.holiday_timezones,
                )
                date_only = choice.configuration.date_time_mode == "date"
                start: date | datetime
                end: date | datetime
                if date_only:
                    start = date.fromisoformat(user_input["holiday_start"])
                    end = date.fromisoformat(user_input["holiday_end"])
                else:
                    start = _holiday_datetime_input(user_input["holiday_start"])
                    end = _holiday_datetime_input(user_input["holiday_end"])
                    # Compare the same local minute shown in the browser. Keep
                    # invalid seconds intact until validation rejects them.
                    for field, value in (
                        ("holiday_start", start),
                        ("holiday_end", end),
                    ):
                        if value.tzinfo is not None:
                            value = value.astimezone(timezone).replace(tzinfo=None)
                        suggested_values[field] = value.isoformat(
                            timespec="minutes"
                            if value.second == value.microsecond == 0
                            else "auto"
                        )
                summary = user_input.get("holiday_name", displayed.period.name or "")
                if creating:
                    values = create_holiday_values(
                        start,
                        end,
                        summary,
                        choice.configuration,
                        timezone,
                        validate_defaults=False,
                    )
                else:
                    values = current
                    original_dates = _holiday_date_suggestions(displayed)
                    name_changed = summary != (displayed.period.name or "")
                    if name_changed or any(
                        suggested_values[field] != original_dates[field]
                        for field in original_dates
                    ):
                        values = update_holiday_values(
                            displayed.period,
                            start,
                            end,
                            summary,
                            choice.configuration,
                            timezone,
                            change_name=name_changed,
                        )
                    # An unchanged displayed endpoint preserves the complete
                    # wire value, including legacy seconds and inclusive dates.
                    for field, wire_field in (
                        ("holiday_start", "start_date"),
                        ("holiday_end", "end_date"),
                    ):
                        fields = [field]
                        if all(
                            suggested_values[key] == original_dates[key]
                            for key in fields
                        ):
                            values = replace(
                                values, **{wire_field: getattr(current, wire_field)}
                            )

                validation_error = "write_validation_failed"
                assigned_to = user_input[CONF_HOLIDAY_ASSIGNED_TO]
                if not isinstance(assigned_to, list) or not all(
                    isinstance(item, str) for item in assigned_to
                ):
                    raise WriteValidationError("Invalid holiday assignments")
                values = configure_holiday_values(
                    replace(displayed.period, write_values=values),
                    choice.configuration,
                    assigned_to=assigned_to,
                    heating_mode=_holiday_mode_from_form(
                        user_input.get(CONF_HOLIDAY_HEATING_MODE),
                        current.heating_mode,
                        choice.configuration.heating_modes,
                    ),
                    dhw_mode=_holiday_mode_from_form(
                        user_input.get(CONF_HOLIDAY_DHW_MODE),
                        current.dhw_mode,
                        choice.configuration.dhw_modes,
                    ),
                    ventilation_mode=_holiday_mode_from_form(
                        user_input.get(CONF_HOLIDAY_VENTILATION_MODE),
                        current.ventilation_mode,
                        choice.configuration.ventilation_modes,
                    ),
                    thermal_disinfection=_holiday_mode_from_form(
                        user_input.get(CONF_HOLIDAY_THERMAL_DISINFECTION),
                        current.thermal_disinfection,
                        choice.configuration.thermal_disinfection_modes,
                    ),
                    fix_temperature=float(
                        user_input.get(
                            CONF_HOLIDAY_FIX_TEMPERATURE, current.fix_temperature
                        )
                    ),
                )
                holiday_id = holiday_period_id(choice.period)
                if holiday_id is None:
                    raise WriteValidationError("Holiday ID is not writable")
                if creating:
                    await choice.coordinator.async_create_holiday(values)
                elif values == current:
                    # Saving the values as shown sends nothing: such a write
                    # could only undo a change made meanwhile in the app.
                    return self.async_abort(reason="holiday_unchanged")
                else:
                    await choice.coordinator.async_update_holiday(
                        holiday_id, values, expected=current
                    )
            except HolidayTimeRangeError as err:
                errors = {"holiday_end": err.translation_key}
            except HolidayTimeStepError as err:
                errors = {err.field: err.translation_key}
            except HolidayTimeError as err:
                errors = {"base": err.translation_key}
            except HolidayAlreadyExists as err:
                return self.async_abort(
                    reason="holiday_previous_request_found"
                    if err.previous_request
                    else "holiday_already_exists"
                )
            except HolidayWriteNotConfirmed as err:
                if err.actual is not None and err.requested is not None:
                    return self.async_abort(
                        reason="holiday_create_adjusted"
                        if creating
                        else "holiday_update_adjusted",
                        description_placeholders={
                            "requested": _holiday_timespan_label(err.requested),
                            "actual": _holiday_timespan_label(err.actual),
                        },
                    )
                if creating:
                    return self.async_abort(reason="holiday_create_unconfirmed")
                errors = {"base": "write_not_confirmed"}
            except WriteNotConfirmed:
                if creating:
                    return self.async_abort(reason="holiday_create_unconfirmed")
                errors = {"base": "write_not_confirmed"}
            except RateLimited:
                errors = {"base": "write_rate_limited"}
            except AuthenticationError:
                errors = {"base": "write_authentication_failed"}
            except KeyError, TypeError, ValueError, WriteValidationError:
                errors = {"base": validation_error}
            except WriteBusy:
                errors = {"base": "write_busy"}
            except PointTError:
                errors = {"base": "write_failed"}
            else:
                return self.async_abort(
                    reason="holiday_created" if creating else "holiday_updated"
                )
            # A correction must still compare with the values originally shown,
            # even if polling has since received changes from the appliance app.
            choice = replace(choice, period=displayed.period, label=displayed.label)

        return self._show_holiday_form(
            choice, errors=errors, suggested_values=suggested_values
        )

    def _holiday_choices(self) -> dict[str, _HolidayChoice] | None:
        coordinators = pointt_coordinators(self._entry)
        if coordinators is None:
            return None
        choices: dict[str, _HolidayChoice] = {}
        multiple_gateways = len(coordinators) > 1
        for coordinator in coordinators:
            snapshots = {
                path: snapshot
                for path, snapshot in (coordinator.data or {}).items()
                if snapshot.available and snapshot.freshness is Freshness.FRESH
            }
            resources = holiday_resources_from_snapshots(snapshots)
            configuration = parse_holiday_write_configuration(resources)
            state = parse_holiday_state(
                resources,
                fallback_timezone=dt_util.get_default_time_zone(),
                timezones=coordinator.holiday_timezones,
            )
            for period in state.periods:
                holiday_id = holiday_period_id(period)
                period_configuration = parse_holiday_write_configuration(
                    resources, source_path=period.source_path
                )
                if (
                    holiday_id is None
                    or period.write_values is None
                    or period_configuration is None
                ):
                    continue
                seed = f"{coordinator.gateway.gateway_id}\0{holiday_id}"
                if period.source_path != HOLIDAY_CREATE_PATH:
                    seed += f"\0{period.source_path}"
                key = hashlib.sha256(seed.encode()).hexdigest()[:24]
                choices[key] = _HolidayChoice(
                    coordinator=coordinator,
                    period=period,
                    configuration=period_configuration,
                    label=_holiday_label(
                        coordinator,
                        period,
                        multiple_gateways=multiple_gateways,
                        language=self.hass.config.language,
                    ),
                )
            if configuration is None:
                continue
            seed = coordinator.gateway.gateway_id
            if configuration.source_path != HOLIDAY_CREATE_PATH:
                seed += f"\0{configuration.source_path}"
            create_key = "create:" + hashlib.sha256(seed.encode()).hexdigest()[:24]
            choices[create_key] = _HolidayChoice(
                coordinator=coordinator,
                period=HolidayPeriod(
                    datetime.now(UTC),
                    datetime.now(UTC),
                    identifier=configuration.source_path.rsplit("hm", 1)[-1]
                    if configuration.source_path != HOLIDAY_CREATE_PATH
                    else "0",
                    source_path=configuration.source_path,
                    write_values=icom_write_values(
                        resources, configuration.source_path
                    ),
                ),
                configuration=configuration,
                label=_gateway_label(coordinator.gateway),
            )
        return choices

    def _show_holiday_selection(
        self, choices: dict[str, _HolidayChoice], *, error: str | None = None
    ) -> ConfigFlowResult:
        mode_choices = self._holiday_mode_choices()
        return self.async_show_form(
            step_id="holidays",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOLIDAY_PERIOD): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=key, label=choice.label)
                                for key, choice in choices.items()
                                if not key.startswith("create:")
                            ]
                            + (
                                [SelectOptionDict(value="create", label="New holiday")]
                                if any(key.startswith("create:") for key in choices)
                                else []
                            )
                            + [
                                SelectOptionDict(
                                    value=key, label=_gateway_label(coordinator.gateway)
                                )
                                for key, coordinator in mode_choices.items()
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="holiday_period",
                        )
                    )
                }
            ),
            errors={"base": error} if error else None,
        )

    def _show_holiday_form(
        self,
        choice: _HolidayChoice,
        *,
        errors: dict[str, str] | None = None,
        suggested_values: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        current = choice.period.write_values
        if current is None:
            return self.async_abort(reason="holiday_changed")
        self._displayed_choice = choice
        configuration = choice.configuration
        fields: dict[vol.Marker, object] = {}
        suggestions: dict[str, Any] = {}
        creating = (self._selected_key or "").startswith("create:")
        if (
            creating and configuration.source_path == HOLIDAY_CREATE_PATH
        ) or configuration.name_codec is not None:
            fields[vol.Required("holiday_name")] = TextSelector()
            suggestions["holiday_name"] = "" if creating else choice.period.name or ""
        for field in ("holiday_start", "holiday_end"):
            fields[vol.Required(field)] = (
                DateSelector()
                if configuration.date_time_mode == "date"
                else HolidayDateTimeSelector(creating=creating)
            )
        if creating:
            suggestions.update(holiday_start="", holiday_end="")
        else:
            suggestions.update(_holiday_date_suggestions(choice))
        fields[
            vol.Required(CONF_HOLIDAY_ASSIGNED_TO, default=list(current.assigned_to))
        ] = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=value,
                        label=_holiday_circuit_label(value, self.hass.config.language),
                    )
                    for value in configuration.assigned_to
                ],
                multiple=True,
                mode=SelectSelectorMode.LIST,
            )
        )
        _add_mode_field(
            fields,
            CONF_HOLIDAY_HEATING_MODE,
            current.heating_mode,
            configuration.heating_modes,
            "holiday_heating_mode",
        )
        if (
            configuration.fix_temperature_min is not None
            and configuration.fix_temperature_max is not None
        ):
            temperature_default = (
                current.fix_temperature
                if configuration.fix_temperature_min
                <= current.fix_temperature
                <= configuration.fix_temperature_max
                else vol.UNDEFINED
            )
            fields[
                vol.Required(
                    CONF_HOLIDAY_FIX_TEMPERATURE,
                    default=temperature_default,
                )
            ] = NumberSelector(
                NumberSelectorConfig(
                    min=configuration.fix_temperature_min,
                    max=configuration.fix_temperature_max,
                    step=configuration.fix_temperature_step or 0.5,
                    unit_of_measurement="°C",
                    mode=NumberSelectorMode.BOX,
                )
            )
        _add_mode_field(
            fields,
            CONF_HOLIDAY_DHW_MODE,
            current.dhw_mode,
            configuration.dhw_modes,
            "holiday_dhw_mode",
        )
        _add_mode_field(
            fields,
            CONF_HOLIDAY_VENTILATION_MODE,
            current.ventilation_mode,
            configuration.ventilation_modes,
            "holiday_ventilation_mode",
        )
        _add_mode_field(
            fields,
            CONF_HOLIDAY_THERMAL_DISINFECTION,
            current.thermal_disinfection,
            configuration.thermal_disinfection_modes,
            "holiday_thermal_disinfection",
        )
        if not creating and configuration.can_delete:
            fields[vol.Optional(DELETE_FIELD)] = HolidayDeleteSelector()
        schema = self.add_suggested_values_to_schema(
            vol.Schema(fields), {**suggestions, **(suggested_values or {})}
        )
        return self.async_show_form(
            step_id=("new_legacy_holiday" if creating else "legacy_holiday")
            if configuration.source_path != HOLIDAY_CREATE_PATH
            else ("new_holiday" if creating else "holiday"),
            data_schema=HolidayFormSchema(schema.schema),
            description_placeholders=None if creating else {"holiday": choice.label},
            errors=errors,
            last_step=True,
        )


def _holiday_date_suggestions(choice: _HolidayChoice) -> dict[str, str]:
    """Use selector-compatible local dates, including exclusive end dates."""
    suggestions: dict[str, str] = {}
    for field, value in (
        ("holiday_start", choice.period.start),
        ("holiday_end", choice.period.end),
    ):
        suggestions[field] = (
            value.date().isoformat()
            if choice.configuration.date_time_mode == "date"
            else value.replace(tzinfo=None).isoformat(timespec="minutes")
        )
    return suggestions


def _holiday_datetime_input(value: str) -> datetime:
    """Require both a date and an explicit clock in the shared input."""
    if not isinstance(value, str) or ("T" not in value and " " not in value):
        raise ValueError("A holiday date and time are required")
    return datetime.fromisoformat(value)


def _add_mode_field(
    fields: dict[vol.Marker, object],
    field: str,
    current: str | None,
    allowed: tuple[str, ...],
    translation_key: str,
) -> None:
    if not allowed:
        return
    options = writable_enum_options("holiday_mode", allowed)
    default = next((key for key, raw in options.items() if raw == current), None)
    marker = (
        vol.Required(field, default=default)
        if default is not None
        else vol.Required(field)
    )
    fields[marker] = SelectSelector(
        SelectSelectorConfig(
            options=list(options),
            mode=SelectSelectorMode.DROPDOWN,
            translation_key=translation_key,
        )
    )


def _holiday_mode_from_form(
    submitted: Any,
    current: str | None,
    allowed: tuple[str, ...],
) -> str | None:
    """Map a translated selector key back to its PointT API value."""
    if submitted is None:
        return current
    if not isinstance(submitted, str):
        raise WriteValidationError("Invalid holiday mode")
    return writable_enum_options("holiday_mode", allowed).get(submitted, submitted)


def _holiday_timespan_label(period: HolidayPeriod) -> str:
    """Display the exact exclusive period without exposing it in diagnostics."""
    return f"{period.start:%Y-%m-%d %H:%M} / {period.end:%Y-%m-%d %H:%M}"


def _holiday_label(
    coordinator: BoschBuderusDataUpdateCoordinator,
    period: HolidayPeriod,
    *,
    multiple_gateways: bool,
    language: str,
) -> str:
    holiday_id = holiday_period_id(period)
    fallback = "Urlaub" if language.casefold().startswith("de") else "Holiday"
    name = period.name or f"{fallback} {holiday_id}"
    if period.all_day:
        timespan = f"{period.start:%Y-%m-%d} / {period.end:%Y-%m-%d}"
    else:
        timespan = f"{period.start:%Y-%m-%d %H:%M} / {period.end:%Y-%m-%d %H:%M}"
    gateway = f"{_gateway_label(coordinator.gateway)} · " if multiple_gateways else ""
    return f"{gateway}{name} · {timespan}"


def _holiday_circuit_label(value: str, language: str) -> str:
    match = re.fullmatch(r"(hc|dhw|vent)(\d+)", value.casefold())
    if match is None:
        return value
    german = language.casefold().startswith("de")
    names = (
        {"hc": "Heizkreis", "dhw": "Warmwasser", "vent": "Lüftung"}
        if german
        else {
            "hc": "Heating circuit",
            "dhw": "Hot water",
            "vent": "Ventilation",
        }
    )
    return f"{names[match.group(1)]} {match.group(2)}"


def _gateway_schema(
    gateways: tuple[Gateway, ...], *, selected: list[str] | None = None
) -> vol.Schema:
    options = [
        SelectOptionDict(value=gateway.gateway_id, label=_gateway_label(gateway))
        for gateway in gateways
    ]
    return vol.Schema(
        {
            (
                vol.Required(CONF_GATEWAY_IDS, default=selected)
                if selected is not None
                else vol.Required(CONF_GATEWAY_IDS)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
    )


def _reconfigure_schema(
    gateways: tuple[Gateway, ...],
    selected: list[str],
    brand: Brand,
    polling_profile: PollingProfile,
) -> vol.Schema:
    """Build a form with only bounded, cloud-safe polling choices."""
    options = [
        SelectOptionDict(value=gateway.gateway_id, label=_gateway_label(gateway))
        for gateway in gateways
    ]
    return vol.Schema(
        {
            vol.Required(CONF_BRAND, default=brand.value): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=Brand.BOSCH.value, label="Bosch"),
                        SelectOptionDict(value=Brand.BUDERUS.value, label="Buderus"),
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(CONF_GATEWAY_IDS, default=selected): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_POLLING_PROFILE, default=polling_profile.value
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        PollingProfile.STANDARD.value,
                        PollingProfile.CLOUD_FRIENDLY.value,
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="polling_profile",
                )
            ),
        }
    )


def _gateway_label(gateway: Gateway) -> str:
    name = gateway.model or gateway.device_type or "Gateway"
    suffix = gateway.gateway_id[-4:]
    return f"{name} (…{suffix})"


def _account_fingerprint(brand: Brand, gateways: tuple[Gateway, ...]) -> str:
    gateway_ids = "\0".join(sorted(item.gateway_id for item in gateways))
    digest = hashlib.sha256(f"{brand.value}\0{gateway_ids}".encode()).hexdigest()[:24]
    return f"{brand.value}:{digest}"
