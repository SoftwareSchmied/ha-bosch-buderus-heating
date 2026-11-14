"""The one SingleKey ID sign-in step shared by the setup and the options dialogs."""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Final, cast
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntryBaseFlow, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .cloud_configuration import renew_cloud_claim
from .const import CONF_REDIRECT_URL, DOMAIN
from .ha_schema import validation as vol
from .local_setup_history import cloud_sign_in_history, now_text, safe_code
from .pointt import (
    AuthenticationError,
    AuthorizationCodeConsumed,
    AuthTokens,
    Brand,
    OAuthClient,
    OAuthFlow,
    OAuthFlowExpired,
    OAuthRedirectError,
    OAuthStateMismatch,
    PointTError,
    RateLimited,
    RequestTimeout,
    ServiceUnavailable,
)

_LOGGER = logging.getLogger(__name__)

# The steps of one sign-in attempt in the entry's sign-in history.
LINK_CREATED: Final = "link_created"
REDIRECT_SUBMITTED: Final = "redirect_submitted"
CODE_EXCHANGED: Final = "code_exchanged"
ACCESS_CHECKED: Final = "access_checked"
SUCCESS: Final = "success"
# Ends of a dialog after the sign-in that mean it worked.
_SUCCESS_ABORTS: Final = frozenset(
    {"reauth_successful", "reauth_successful_targets_missing"}
)
# Attempts of a first setup wait in the dialog until its entry exists.
_MAX_PENDING: Final = 10


def _service_code(error: PointTError) -> str:
    if isinstance(error, RateLimited):
        return "rate_limited"
    if isinstance(error, RequestTimeout | ServiceUnavailable):
        return "cannot_connect"
    return f"unexpected:{type(error).__name__}"


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
    # A link was shown and no address from it has been submitted since.
    _auth_link_open: bool = False
    # True between an exchanged code and the end of the access check.
    _auth_checking: bool = False
    _auth_pending: list[tuple[str, str, str | None]] | None = None

    def _auth_history_owner(self) -> str | None:
        """The entry the sign-in belongs to; None before a first setup ends."""
        for name in ("_reauth_entry", "_reconfigure_entry", "_entry"):
            entry_id = getattr(getattr(self, name, None), "entry_id", None)
            if isinstance(entry_id, str):
                return entry_id
        return None

    def _auth_note(self, step: str, error: str | None) -> None:
        """Note one sign-in attempt: codes only, never links or account data."""
        try:
            if error is not None:
                # Once per failed attempt, with nothing but the code.
                _LOGGER.info(
                    "A cloud sign-in attempt failed in step %s: %s",
                    step,
                    safe_code(error) or "unknown",
                )
            owner = self._auth_history_owner()
            if owner is not None:
                # A dialog closed because its entry was removed would bring
                # the deleted history back.
                if self.hass.config_entries.async_get_entry(owner) is not None:
                    cloud_sign_in_history(self.hass, owner).add(step, error, log=False)
                return
            pending = self._auth_pending or []
            pending.append((now_text(), step, error))
            self._auth_pending = pending[-_MAX_PENDING:]
        except Exception:  # the history only explains; it never stops a dialog
            _LOGGER.debug("Cloud sign-in attempt not noted")

    def _auth_new_link(self) -> None:
        if self._auth_link_open:
            # The link before was never used: closed, expired or replaced.
            self._auth_note(LINK_CREATED, "abandoned_after_link")
        self._auth_link_open = True
        self._auth_checking = False

    def _auth_outcome(self, error: str | None) -> None:
        """The end of the access check after an exchanged code."""
        if self._auth_checking:
            self._auth_checking = False
            self._auth_note(ACCESS_CHECKED if error else SUCCESS, error)

    def async_show_form(self, **kwargs: Any) -> ConfigFlowResult:
        errors = kwargs.get("errors")
        if self._auth_checking and errors:
            code = errors.get("base") or errors[min(errors)]
            self._auth_note(ACCESS_CHECKED, code)
        return super().async_show_form(**kwargs)

    def async_create_entry(self, **kwargs: Any) -> ConfigFlowResult:
        self._auth_outcome(None)
        return super().async_create_entry(**kwargs)

    def async_abort(self, **kwargs: Any) -> ConfigFlowResult:
        reason = kwargs.get("reason")
        self._auth_outcome(None if reason in _SUCCESS_ABORTS else str(reason))
        return super().async_abort(**kwargs)

    @callback
    def async_remove(self) -> None:
        if self._auth_link_open:
            self._auth_link_open = False
            self._auth_note(LINK_CREATED, "abandoned_after_link")
        elif self._auth_checking:
            self._auth_checking = False
            self._auth_note(ACCESS_CHECKED, "abandoned_after_sign_in")
        self._auth_flush_pending()
        super().async_remove()

    def _auth_flush_pending(self) -> None:
        """Hand the attempts of a first setup to the entry it created."""
        pending, self._auth_pending = self._auth_pending, None
        if not pending:
            return
        try:
            unique_id = "installation_" + self.flow_id
            entry = next(
                (
                    item
                    for item in self.hass.config_entries.async_entries(DOMAIN)
                    if item.unique_id == unique_id
                ),
                None,
            )
            if entry is None:
                return  # no entry, so no diagnostics to show them in
            history = cloud_sign_in_history(self.hass, entry.entry_id)
            for at, step, error in pending:
                history.add(step, error, at=at, log=False)
        except Exception:
            _LOGGER.debug("Cloud sign-in attempts not kept")

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
        self._auth_new_link()
        return self._show_auth_form()

    def _restart_authorization(self, *, error: str) -> ConfigFlowResult:
        brand = self._auth_brand()
        if brand is None:
            return self.async_abort(reason=self._AUTH_MISSING_REASON)
        # A refused access after the exchange ends that attempt first.
        self._auth_outcome(error)
        self._oauth_flow = OAuthFlow.create(brand)
        self._auth_new_link()
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
        self._auth_link_open = False
        step = REDIRECT_SUBMITTED
        try:
            try:
                grant = self._oauth_flow.consume_redirect(user_input[CONF_REDIRECT_URL])
            except OAuthFlowExpired:
                self._auth_note(step, "auth_expired")
                return self._restart_authorization(error="auth_expired")
            except AuthorizationCodeConsumed:
                self._auth_note(step, "auth_restart")
                return self._restart_authorization(error="auth_restart")
            except OAuthStateMismatch:
                self._auth_note(step, "state_mismatch")
                self._auth_link_open = True  # the link stays valid
                return self._show_auth_form(error="invalid_redirect")
            except OAuthRedirectError, ValueError:
                # Not the address of this attempt: the link stays valid.
                self._auth_note(step, "invalid_redirect")
                self._auth_link_open = True
                return self._show_auth_form(error="invalid_redirect")
            step = CODE_EXCHANGED
            try:
                tokens = await OAuthClient(
                    async_get_clientsession(self.hass)
                ).exchange_code(brand, grant)
            except AuthenticationError, ValueError:
                self._auth_note(step, "invalid_auth")
                return self._restart_authorization(error="invalid_auth")
            except PointTError as error:
                self._auth_note(step, _service_code(error))
                return self._auth_service_failed(error)
            if not tokens.refresh_token:
                self._auth_note(step, "invalid_auth")
                return self._restart_authorization(error="invalid_auth")
            step = ACCESS_CHECKED
            self._auth_checking = True
            return await self._auth_succeeded(tokens)
        except AbortFlow as abort:
            self._auth_outcome(abort.reason)
            raise
        except Exception as error:
            if step != ACCESS_CHECKED or self._auth_checking:
                self._auth_checking = False
                self._auth_note(step, f"unexpected:{type(error).__name__}")
            raise
