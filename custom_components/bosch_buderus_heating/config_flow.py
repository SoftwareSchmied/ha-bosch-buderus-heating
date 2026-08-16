"""Config flow for Bosch/Buderus Heating."""

from __future__ import annotations

import hashlib
from typing import Any, cast, override
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
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
from .data import tokens_from_data, tokens_to_data
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
    RequestTimeout,
    ServiceUnavailable,
    TokenManager,
)
from .runtime import BoschBuderusRuntimeData

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
