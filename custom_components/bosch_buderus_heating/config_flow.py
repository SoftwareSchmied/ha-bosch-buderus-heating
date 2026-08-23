"""Config flow for Bosch/Buderus Heating."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, cast, override
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_BRAND,
    CONF_GATEWAY_IDS,
    CONF_POLLING_PROFILE,
    CONF_REDIRECT_URL,
    DEFAULT_POLLING_PROFILE,
    DOMAIN,
    RATE_LIMIT_ISSUE_PREFIX,
    PollingProfile,
    polling_profile_from_options,
)
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .data import tokens_from_data, tokens_to_data
from .holiday_writes import (
    configure_holiday_values,
    holiday_resources_from_snapshots,
)
from .holidays import (
    HolidayPeriod,
    HolidayWriteConfiguration,
    holiday_period_id,
    parse_holiday_state,
    parse_holiday_write_configuration,
)
from .pointt import (
    AuthenticationError,
    AuthorizationCodeConsumed,
    AuthTokens,
    Brand,
    Gateway,
    OAuthClient,
    OAuthFlow,
    OAuthRedirectError,
    OAuthStateMismatch,
    PointTClient,
    PointTError,
    RateLimited,
    RequestTimeout,
    ServiceUnavailable,
    TokenManager,
    WriteNotConfirmed,
    WriteValidationError,
)
from .runtime import BoschBuderusRuntimeData

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

REDIRECT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REDIRECT_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)


class BoschBuderusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure a PointT account and its gateways."""

    VERSION = 3

    _brand: Brand | None = None
    _oauth_flow: OAuthFlow | None = None
    _tokens: AuthTokens | None = None
    _gateways: tuple[Gateway, ...] = ()
    _reauth_entry: ConfigEntry | None = None
    _reconfigure_entry: ConfigEntry | None = None
    _pending_polling_profile: PollingProfile = DEFAULT_POLLING_PROFILE

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the actions exposed through the integration options dialog."""
        return BoschBuderusOptionsFlow(config_entry)

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the application brand."""
        if user_input is not None:
            self._brand = Brand(user_input[CONF_BRAND])
            return self._start_authorization()
        return self.async_show_form(step_id="user", data_schema=BRAND_SCHEMA)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication for an existing entry."""
        self._reauth_entry = self._get_reauth_entry()
        self._brand = Brand(entry_data[CONF_BRAND])
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
            self._brand = Brand(self._reconfigure_entry.data[CONF_BRAND])
            self._pending_polling_profile = polling_profile_from_options(
                self._reconfigure_entry.options
            )
            return await self._async_prepare_reconfigure()

        if user_input is None:
            return self._show_reconfigure_form()

        selected = user_input.get(CONF_GATEWAY_IDS)
        if not isinstance(selected, list) or not selected:
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

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept and exchange the application redirect URL."""
        if user_input is None:
            return self._show_auth_form()
        if self._brand is None or self._oauth_flow is None:
            return self.async_abort(reason="invalid_flow")

        try:
            grant = self._oauth_flow.consume_redirect(user_input[CONF_REDIRECT_URL])
        except OAuthRedirectError, OAuthStateMismatch, AuthorizationCodeConsumed:
            return self._show_auth_form(error="invalid_redirect")

        session = async_get_clientsession(self.hass)
        try:
            self._tokens = await OAuthClient(session).exchange_code(self._brand, grant)
        except AuthenticationError, ValueError:
            return self._restart_authorization(error="invalid_auth")
        except RequestTimeout, ServiceUnavailable:
            return self._restart_authorization(error="cannot_connect")
        except PointTError:
            return self._restart_authorization(error="unknown")

        if not self._tokens.refresh_token:
            return self._restart_authorization(error="invalid_auth")
        return await self._async_discover_gateways()

    async def async_step_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Retry gateway discovery without repeating login."""
        if user_input is not None:
            return await self._async_discover_gateways()
        return self.async_show_form(step_id="retry")

    async def async_step_gateways(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one or more gateways for this entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input[CONF_GATEWAY_IDS]
            if not isinstance(selected, list) or not selected:
                errors["base"] = "select_gateway"
            elif self._brand is None or self._tokens is None:
                return self.async_abort(reason="invalid_flow")
            else:
                duplicate = self._find_duplicate_entry(set(selected))
                if duplicate is not None:
                    return self.async_abort(reason="already_configured")

                await self.async_set_unique_id(
                    _account_fingerprint(self._brand, self._gateways)
                )
                self._abort_if_unique_id_configured()
                if self._reconfigure_entry is not None:
                    ir.async_delete_issue(
                        self.hass,
                        DOMAIN,
                        f"{RATE_LIMIT_ISSUE_PREFIX}{self._reconfigure_entry.entry_id}",
                    )
                    return self.async_update_reload_and_abort(
                        self._reconfigure_entry,
                        unique_id=_account_fingerprint(self._brand, self._gateways),
                        title=f"{self._brand.value.title()} Heating",
                        data_updates={
                            CONF_BRAND: self._brand.value,
                            CONF_GATEWAY_IDS: selected,
                            **tokens_to_data(self._tokens),
                        },
                        options={
                            **self._reconfigure_entry.options,
                            CONF_POLLING_PROFILE: self._pending_polling_profile.value,
                        },
                    )
                return self.async_create_entry(
                    title=f"{self._brand.value.title()} Heating",
                    data={
                        CONF_BRAND: self._brand.value,
                        CONF_GATEWAY_IDS: selected,
                        **tokens_to_data(self._tokens),
                    },
                )

        return self.async_show_form(
            step_id="gateways",
            data_schema=_gateway_schema(self._gateways),
            errors=errors,
        )

    async def _async_discover_gateways(self) -> ConfigFlowResult:
        if self._tokens is None:
            return self.async_abort(reason="invalid_flow")
        session = async_get_clientsession(self.hass)
        try:
            self._gateways = await PointTClient(
                session, self._tokens.access_token
            ).get_gateways()
        except AuthenticationError:
            return self._restart_authorization(error="invalid_auth")
        except RequestTimeout, ServiceUnavailable:
            return self.async_show_form(
                step_id="retry", errors={"base": "cannot_connect"}
            )
        except PointTError:
            return self.async_show_form(step_id="retry", errors={"base": "unknown"})

        if not self._gateways:
            return self.async_abort(reason="no_gateways")
        if self._reauth_entry is not None:
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data_updates=tokens_to_data(self._tokens),
                reason="reauth_successful",
            )
        return await self.async_step_gateways()

    async def _async_prepare_reconfigure(self) -> ConfigFlowResult:
        """Discover the current account gateways before showing safe choices."""
        if self._reconfigure_entry is None:
            return self.async_abort(reason="invalid_flow")
        try:
            self._gateways = await self._reconfigure_client().get_gateways()
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
        if not self._gateways:
            return self.async_abort(reason="no_gateways")
        return self._show_reconfigure_form()

    def _reconfigure_client(self) -> PointTClient:
        """Reuse the live token manager or construct one for an unloaded entry."""
        if self._reconfigure_entry is None:
            raise RuntimeError("Reconfigure entry is missing")
        runtime = getattr(self._reconfigure_entry, "runtime_data", None)
        if runtime is not None:
            return cast(BoschBuderusRuntimeData, runtime).client

        session = async_get_clientsession(self.hass)

        async def persist_tokens(updated_tokens: AuthTokens) -> None:
            if self._reconfigure_entry is None:
                return
            self.hass.config_entries.async_update_entry(
                self._reconfigure_entry,
                data={
                    **self._reconfigure_entry.data,
                    **tokens_to_data(updated_tokens),
                },
            )

        manager = TokenManager(
            OAuthClient(session),
            tokens_from_data(self._reconfigure_entry.data),
            persist_tokens,
        )
        return PointTClient(session, manager)

    def _show_reconfigure_form(self, *, error: str | None = None) -> ConfigFlowResult:
        """Show current selections and explain that saving runs discovery."""
        if self._reconfigure_entry is None or self._brand is None:
            return self.async_abort(reason="invalid_flow")
        selected = self._reconfigure_entry.data.get(CONF_GATEWAY_IDS, [])
        errors = {"base": error} if error else None
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_reconfigure_schema(
                self._gateways,
                selected if isinstance(selected, list) else [],
                self._brand,
                self._pending_polling_profile,
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
        return self.async_update_reload_and_abort(
            self._reconfigure_entry,
            data_updates={CONF_GATEWAY_IDS: selected},
            options={
                **self._reconfigure_entry.options,
                CONF_POLLING_PROFILE: self._pending_polling_profile.value,
            },
        )

    def _start_authorization(self) -> ConfigFlowResult:
        if self._brand is None:
            return self.async_abort(reason="invalid_flow")
        self._oauth_flow = OAuthFlow.create(self._brand)
        return self._show_auth_form()

    def _restart_authorization(self, *, error: str) -> ConfigFlowResult:
        if self._brand is None:
            return self.async_abort(reason="invalid_flow")
        self._oauth_flow = OAuthFlow.create(self._brand)
        return self._show_auth_form(error=error)

    def _show_auth_form(self, *, error: str | None = None) -> ConfigFlowResult:
        if self._oauth_flow is None:
            return self.async_abort(reason="invalid_flow")
        errors = {"base": error} if error else None
        return self.async_show_form(
            step_id="auth",
            data_schema=REDIRECT_SCHEMA,
            description_placeholders={
                "authorization_url": self._oauth_flow.authorization_url,
                "redirect_scheme": urlparse(self._oauth_flow.brand.redirect_uri).scheme,
            },
            errors=errors,
        )

    def _find_duplicate_entry(self, selected: set[str]) -> ConfigEntry | None:
        if self._brand is None:
            return None
        for entry in self._async_current_entries():
            if entry is self._reconfigure_entry:
                continue
            if entry.data.get(CONF_BRAND) != self._brand.value:
                continue
            configured = entry.data.get(CONF_GATEWAY_IDS, [])
            if isinstance(configured, list) and selected.intersection(configured):
                return entry
        return None


@dataclass(frozen=True, slots=True)
class _HolidayChoice:
    """One currently writable holiday and its cloud-advertised capabilities."""

    coordinator: BoschBuderusDataUpdateCoordinator
    period: HolidayPeriod
    configuration: HolidayWriteConfiguration
    label: str


class BoschBuderusOptionsFlow(OptionsFlow):
    """Configure PointT-specific fields that the HA calendar cannot display."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._selected_key: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the holiday that should be configured."""
        choices = self._holiday_choices()
        if choices is None:
            return self.async_abort(reason="not_loaded")
        if not choices:
            return self.async_abort(reason="no_writable_holidays")

        if user_input is not None:
            selected = user_input.get(CONF_HOLIDAY_PERIOD)
            if isinstance(selected, str) and selected in choices:
                self._selected_key = selected
                return await self.async_step_holiday()
            return self._show_holiday_selection(choices, error="holiday_changed")
        return self._show_holiday_selection(choices)

    async def async_step_holiday(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate, write, and confirm PointT-specific holiday settings."""
        choices = self._holiday_choices()
        if choices is None:
            return self.async_abort(reason="not_loaded")
        choice = choices.get(self._selected_key or "")
        if choice is None:
            return self.async_abort(reason="holiday_changed")

        if user_input is not None:
            current = choice.period.write_values
            if current is None:
                return self.async_abort(reason="holiday_changed")
            try:
                assigned_to = user_input[CONF_HOLIDAY_ASSIGNED_TO]
                if not isinstance(assigned_to, list) or not all(
                    isinstance(item, str) for item in assigned_to
                ):
                    raise WriteValidationError("Invalid holiday assignments")
                values = configure_holiday_values(
                    choice.period,
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
                            CONF_HOLIDAY_FIX_TEMPERATURE,
                            current.fix_temperature,
                        )
                    ),
                )
                holiday_id = holiday_period_id(choice.period)
                if holiday_id is None:
                    raise WriteValidationError("Holiday ID is not writable")
                await choice.coordinator.async_update_holiday(holiday_id, values)
            except WriteNotConfirmed:
                return self._show_holiday_form(choice, error="write_not_confirmed")
            except RateLimited:
                return self._show_holiday_form(choice, error="write_rate_limited")
            except AuthenticationError:
                return self._show_holiday_form(
                    choice, error="write_authentication_failed"
                )
            except KeyError, TypeError, ValueError, WriteValidationError:
                return self._show_holiday_form(choice, error="write_validation_failed")
            except PointTError:
                return self._show_holiday_form(choice, error="write_failed")
            return self.async_abort(reason="holiday_updated")

        return self._show_holiday_form(choice)

    def _holiday_choices(self) -> dict[str, _HolidayChoice] | None:
        runtime = getattr(self._entry, "runtime_data", None)
        if not isinstance(runtime, BoschBuderusRuntimeData):
            return None
        choices: dict[str, _HolidayChoice] = {}
        multiple_gateways = len(runtime.coordinators) > 1
        for coordinator in runtime.coordinators:
            snapshots = {
                path: snapshot
                for path, snapshot in (coordinator.data or {}).items()
                if snapshot.available and snapshot.freshness is Freshness.FRESH
            }
            resources = holiday_resources_from_snapshots(snapshots)
            configuration = parse_holiday_write_configuration(resources)
            if configuration is None:
                continue
            state = parse_holiday_state(
                resources, fallback_timezone=coordinator.hass.config.time_zone
            )
            for period in state.periods:
                holiday_id = holiday_period_id(period)
                if holiday_id is None or period.write_values is None:
                    continue
                key = hashlib.sha256(
                    f"{coordinator.gateway.gateway_id}\0{holiday_id}".encode()
                ).hexdigest()[:24]
                choices[key] = _HolidayChoice(
                    coordinator=coordinator,
                    period=period,
                    configuration=configuration,
                    label=_holiday_label(
                        coordinator,
                        period,
                        multiple_gateways=multiple_gateways,
                        language=self.hass.config.language,
                    ),
                )
        return choices

    def _show_holiday_selection(
        self, choices: dict[str, _HolidayChoice], *, error: str | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOLIDAY_PERIOD): SelectSelector(
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
            errors={"base": error} if error else None,
        )

    def _show_holiday_form(
        self, choice: _HolidayChoice, *, error: str | None = None
    ) -> ConfigFlowResult:
        current = choice.period.write_values
        if current is None:
            return self.async_abort(reason="holiday_changed")
        configuration = choice.configuration
        fields: dict[vol.Marker, object] = {
            vol.Required(
                CONF_HOLIDAY_ASSIGNED_TO, default=list(current.assigned_to)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=value,
                            label=_holiday_circuit_label(
                                value, self.hass.config.language
                            ),
                        )
                        for value in configuration.assigned_to
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
        _add_mode_field(
            fields,
            CONF_HOLIDAY_HEATING_MODE,
            current.heating_mode,
            configuration.heating_modes,
            "holiday_heating_mode",
        )
        if (
            "FIX_TEMPERATURE" in configuration.heating_modes
            and configuration.fix_temperature_min is not None
            and configuration.fix_temperature_max is not None
        ):
            fields[
                vol.Required(
                    CONF_HOLIDAY_FIX_TEMPERATURE,
                    default=current.fix_temperature,
                )
            ] = NumberSelector(
                NumberSelectorConfig(
                    min=configuration.fix_temperature_min,
                    max=configuration.fix_temperature_max,
                    step=0.5,
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
        return self.async_show_form(
            step_id="holiday",
            data_schema=vol.Schema(fields),
            description_placeholders={"holiday": choice.label},
            errors={"base": error} if error else None,
        )


def _add_mode_field(
    fields: dict[vol.Marker, object],
    field: str,
    current: str | None,
    allowed: tuple[str, ...],
    translation_key: str,
) -> None:
    if not allowed:
        return
    default = (current if current in allowed else allowed[0]).casefold()
    fields[vol.Required(field, default=default)] = SelectSelector(
        SelectSelectorConfig(
            options=[value.casefold() for value in allowed],
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
    submitted_key = submitted.casefold()
    return next(
        (value for value in allowed if value.casefold() == submitted_key), submitted
    )


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


def _gateway_schema(gateways: tuple[Gateway, ...]) -> vol.Schema:
    options = [
        SelectOptionDict(value=gateway.gateway_id, label=_gateway_label(gateway))
        for gateway in gateways
    ]
    return vol.Schema(
        {
            vol.Required(CONF_GATEWAY_IDS): SelectSelector(
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
