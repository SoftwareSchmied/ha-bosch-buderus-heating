"""The one SingleKey ID sign-in step shared by the setup and the options dialogs."""

from __future__ import annotations

from typing import Any, ClassVar, cast
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntryBaseFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .cloud_configuration import renew_cloud_claim
from .const import CONF_REDIRECT_URL
from .ha_schema import validation as vol
from .pointt import (
    AuthenticationError,
    AuthorizationCodeConsumed,
    AuthTokens,
    Brand,
    OAuthClient,
    OAuthFlow,
    OAuthFlowExpired,
    OAuthRedirectError,
    OAuthScopeRejected,
    OAuthStateMismatch,
    PointTError,
)

REDIRECT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REDIRECT_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)


class CloudAuthorizationFlow(ConfigEntryBaseFlow):
    """Issue, show and consume one sign-in link (FLOW-2).

    A mistyped address keeps the link that was already used in the browser;
    only an expired or consumed attempt, or a rejected code, issues a new one.
    Both dialogs share the guide text with the `redirect_scheme` placeholder.
    """

    _AUTH_STEP_ID: ClassVar[str] = "auth"
    # The abort reason when the dialog lost the sign-in state.
    _AUTH_MISSING_REASON: ClassVar[str] = "invalid_flow"
    _oauth_flow: OAuthFlow | None = None

    def _auth_brand(self) -> Brand | None:
        """The brand whose application receives the redirect."""
        raise NotImplementedError

    async def _auth_blocked(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult | None:
        """A result that replaces the step, e.g. a wait; None to continue."""
        return None

    def _auth_service_failed(self, error: PointTError) -> ConfigFlowResult:
        """The token endpoint could not be used; the code is spent either way."""
        raise NotImplementedError

    async def _auth_succeeded(self, tokens: AuthTokens) -> ConfigFlowResult:
        """Continue with the tokens of a completed sign-in."""
        raise NotImplementedError

    def _start_authorization(self) -> ConfigFlowResult:
        brand = self._auth_brand()
        if brand is None:
            return self.async_abort(reason=self._AUTH_MISSING_REASON)
        self._oauth_flow = OAuthFlow.create(brand)
        return self._show_auth_form()

    def _restart_authorization(self, *, error: str) -> ConfigFlowResult:
        brand = self._auth_brand()
        if brand is None:
            return self.async_abort(reason=self._AUTH_MISSING_REASON)
        self._oauth_flow = OAuthFlow.create(brand)
        return self._show_auth_form(error=error)

    def _show_auth_form(self, *, error: str | None = None) -> ConfigFlowResult:
        if self._oauth_flow is None:
            return self.async_abort(reason=self._AUTH_MISSING_REASON)
        if self._oauth_flow.is_expired():
            return self._restart_authorization(error="auth_expired")
        return self.async_show_form(
            step_id=self._AUTH_STEP_ID,
            data_schema=REDIRECT_SCHEMA,
            description_placeholders={
                "authorization_url": self._oauth_flow.authorization_url,
                "redirect_scheme": urlparse(self._oauth_flow.brand.redirect_uri).scheme,
            },
            errors={"base": error} if error else None,
        )

    def _show_or_start_authorization(self) -> ConfigFlowResult:
        """The current link while it is usable, otherwise a new one."""
        if self._oauth_flow is None:
            return self._start_authorization()
        return self._show_auth_form()

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept and exchange the application redirect URL."""
        if not renew_cloud_claim(cast(dict[str, Any], self.context)):
            return self.async_abort(reason="cloud_setup_taken_over")
        if (blocked := await self._auth_blocked(user_input)) is not None:
            return blocked
        brand = self._auth_brand()
        if brand is None or self._oauth_flow is None:
            return self.async_abort(reason=self._AUTH_MISSING_REASON)
        if user_input is None:
            return self._show_auth_form()
        try:
            grant = self._oauth_flow.consume_redirect(user_input[CONF_REDIRECT_URL])
        except OAuthFlowExpired:
            return self._restart_authorization(error="auth_expired")
        except AuthorizationCodeConsumed:
            return self._restart_authorization(error="auth_restart")
        except OAuthScopeRejected:
            # The attempt already switched to the full scope set and a new
            # link: the user signs in again with that link.
            return self._show_auth_form(error="auth_restart")
        except OAuthRedirectError, OAuthStateMismatch, ValueError:
            # Not the address of this attempt: the link stays valid.
            return self._show_auth_form(error="invalid_redirect")
        try:
            tokens = await OAuthClient(
                async_get_clientsession(self.hass)
            ).exchange_code(brand, grant)
        except AuthenticationError, ValueError:
            return self._restart_authorization(error="invalid_auth")
        except PointTError as error:
            return self._auth_service_failed(error)
        if not tokens.refresh_token:
            return self._restart_authorization(error="invalid_auth")
        return await self._auth_succeeded(tokens)
