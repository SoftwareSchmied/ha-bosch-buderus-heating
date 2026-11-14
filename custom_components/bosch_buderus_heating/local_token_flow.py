"""The gateway's token list and the revocation of an old token (options).

Both run only on an explicit request in the dialog of one local access, with
the token and the certificate binding of that access. The list is shown and
then forgotten; it never reaches the configuration, the diagnostics or the
log. Only entries with this integration's own client name can be chosen for
a revocation, and a revocation needs a confirmation of its own. The gateway
does not say which token an access uses, so revoking any of them also needs
the second confirmation that a local access may end. Nothing here runs when
an access or the whole entry is removed without that choice.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any, cast

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .configuration import LOCAL_KIND, ConfigurationError
from .configuration_management import ConnectionEdit
from .connection_flow import ConnectionOptionsFlow
from .gateway_identity import GatewayNamespace, IdentityContext
from .ha_schema import validation as vol
from .i18n import localize
from .local_address import saved_local_accesses
from .local_auth import (
    AuthProblem,
    GatewayToken,
    RevokeOutcome,
    RevokeState,
    list_gateway_tokens,
    revoke_gateway_token,
)
from .local_budget import (
    BudgetDeferred,
    LocalBudgetError,
    LocalReadBudget,
    local_budget_manager,
)
from .local_credentials import TokenOrigin, load_local_access
from .local_probe import ProbeContext, TransportFailure
from .local_transport import (
    CertificateBinding,
    CertificateObservation,
    LocalTransportError,
    PortRole,
    StaleLocalRequest,
    inspect_local_certificate,
)
from .markdown_text import markdown_literal

# Lines of one group shown in the description; the rest is counted.
MAX_SHOWN_TOKENS = 32

# The gateway does not say which token an access uses. The access's stored
# setup time is taken after its token was created, so the own token created
# last at or before that time is the likely current one. The tolerance covers
# a small clock difference between the gateway and Home Assistant. It is only
# a mark for the list: every own token needs the second confirmation, since
# the clocks, another entry or another Home Assistant may use any of them.
USED_TOKEN_TOLERANCE = 120.0

_PROBLEM_ERRORS = {
    AuthProblem.UNAUTHORIZED: "local_tokens_rejected",
    AuthProblem.FORBIDDEN: "local_tokens_forbidden",
    AuthProblem.NOT_FOUND: "local_tokens_unsupported",
    AuthProblem.INVALID_RESPONSE: "local_tokens_invalid",
    AuthProblem.RATE_LIMITED: "local_rate_limited",
    AuthProblem.SERVICE_UNAVAILABLE: "local_service_unavailable",
    AuthProblem.BUDGET_BUSY: "local_busy",
    AuthProblem.CONTEXT_CHANGED: "local_configuration_changed",
    AuthProblem.CONNECTION: "local_connection_failed",
    AuthProblem.DNS: "local_connection_failed",
    AuthProblem.TIMEOUT: "local_timeout",
    AuthProblem.CERTIFICATE_CHANGED: "local_certificate_changed",
    AuthProblem.TLS: "local_certificate_changed",
    AuthProblem.INTERFACE: "local_interface_required",
}
_TRANSPORT_ERRORS = {
    TransportFailure.TLS: "local_certificate_changed",
    TransportFailure.TLS_CHANGED: "local_certificate_changed",
    TransportFailure.TIMEOUT: "local_timeout",
    TransportFailure.INTERFACE: "local_interface_required",
}


def _problem_error(problem: AuthProblem | None) -> str:
    if problem is None:
        return "local_unexpected"
    return _PROBLEM_ERRORS.get(problem, "local_unexpected")


def _created(value: float | None) -> str:
    """A creation time in the local time zone of Home Assistant."""
    if value is None:
        return "?"
    try:
        moment = dt_util.as_local(dt_util.utc_from_timestamp(value))
    except OverflowError, OSError, ValueError:
        return "?"
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _lines(entries: Iterable[str]) -> str:
    shown = list(entries)
    if not shown:
        return "—"
    rest = len(shown) - MAX_SHOWN_TOKENS
    lines = [f"- {line}" for line in shown[:MAX_SHOWN_TOKENS]]
    if rest > 0:
        lines.append(f"- … (+{rest})")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _TokenAccess:
    """The stored local access the dialog works with; never kept afterwards."""

    edit: ConnectionEdit = field(repr=False)
    context: ProbeContext = field(repr=False)
    token: str = field(repr=False)
    resource_sha256: bytes = field(repr=False)
    binding: CertificateBinding | None = field(repr=False)
    gateway_id: str = field(repr=False)
    target_id: str | None = field(repr=False)
    verified_at: float = field(repr=False)
    # Only a token this integration created can be among the listed own ones.
    created: bool = field(repr=False)


def likely_used_tokens(
    own: Iterable[GatewayToken], verified_at: float, *, created: bool
) -> tuple[GatewayToken, ...]:
    """The own tokens an access most likely uses (several if equally old)."""
    if not created:
        return ()
    candidates = [
        token
        for token in own
        if token.created_at is not None
        and token.created_at <= verified_at + USED_TOKEN_TOLERANCE
    ]
    if not candidates:
        return ()
    newest = max(cast(float, token.created_at) for token in candidates)
    return tuple(token for token in candidates if token.created_at == newest)


@dataclass(frozen=True, slots=True)
class TokenUse:
    """Which own tokens an access most likely uses; a mark, never a proof."""

    # Most likely used by this access.
    this: tuple[GatewayToken, ...] = ()
    # Most likely used by another saved local access to the same gateway.
    others: tuple[GatewayToken, ...] = ()


def token_use(
    own: Iterable[GatewayToken],
    verified_at: float,
    *,
    created: bool,
    other_accesses: Iterable[tuple[float, bool]] = (),
) -> TokenUse:
    """Mark the own tokens this access and other accesses most likely use.

    ``other_accesses``: setup time and whether the token was created by this
    integration, for every other saved local access to the same gateway.
    Nothing is marked for this access while an own token is newer than its
    setup and no other access explains it (another Home Assistant, or the
    clocks differ): the mark would point at an older token.
    """
    tokens = tuple(own)
    others: list[GatewayToken] = []
    for other_verified, other_created in other_accesses:
        for token in likely_used_tokens(tokens, other_verified, created=other_created):
            if token not in others:
                others.append(token)
    unexplained = any(
        token.created_at is not None
        and token.created_at > verified_at + USED_TOKEN_TOLERANCE
        and token not in others
        for token in tokens
    )
    this = (
        () if unexplained else likely_used_tokens(tokens, verified_at, created=created)
    )
    return TokenUse(this, tuple(token for token in others if token not in this))


class LocalTokenFlow(ConnectionOptionsFlow):
    """Show the gateway's tokens; revoke one of this integration on request."""

    _token_access: _TokenAccess | None = None
    _token_choices: dict[str, GatewayToken] | None = None
    _token_selected: GatewayToken | None = None
    _token_observation: CertificateObservation | None = None
    _token_use: TokenUse = TokenUse()
    _token_removal = False
    _token_remove_none = False
    _token_closed = False

    def _token_load(self, edit: ConnectionEdit | None) -> _TokenAccess:
        if edit is None:
            raise ConfigurationError("configuration_changed")
        management = self._management()
        if management.edit(edit.connection_id) != edit:
            raise ConfigurationError("configuration_changed")
        snapshot = management.current()
        connection = snapshot.connections[edit.connection_id]
        if connection.kind != LOCAL_KIND:
            raise ConfigurationError("unsupported_connection")
        access = load_local_access(connection.to_data())
        context = ProbeContext(
            IdentityContext(
                GatewayNamespace.K40_LOCAL, snapshot.owner_id, edit.connection_id, 0
            ),
            access.endpoint,
        )
        targets = {
            binding.target_id
            for binding in snapshot.bindings.values()
            if binding.connection_id == edit.connection_id
        }
        return _TokenAccess(
            edit,
            context,
            access.credentials.token,
            bytes.fromhex(access.resource_sha256),
            CertificateBinding(
                context,
                PortRole.AUTHENTICATION,
                bytes.fromhex(access.authentication_sha256),
            )
            if access.authentication_sha256
            else None,
            access.gateway_id,
            next(iter(targets)) if len(targets) == 1 else None,
            access.verified_at,
            access.credentials.origin == TokenOrigin.CREATED,
        )

    def _token_current(self) -> bool:
        """The dialog is open and the access is still the one it started with."""
        access = self._token_access
        if self._token_closed or access is None:
            return False
        try:
            current = self._token_load(access.edit)
        except ValueError, KeyError:
            return False
        return (
            current.token == access.token
            and current.context.endpoint == access.context.endpoint
            and current.resource_sha256 == access.resource_sha256
        )

    async def _token_budget(self, access: _TokenAccess) -> LocalReadBudget:
        return await local_budget_manager(self.hass).budget(
            access.context, target_id=access.target_id, gateway_id=access.gateway_id
        )

    def _token_reset(self) -> None:
        self._token_access = self._token_choices = self._token_selected = None
        self._token_observation = None
        self._token_use = TokenUse()
        self._token_remove_none = False

    async def async_step_local_tokens(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The token list, read once on explicit request (menu entry)."""
        if user_input is None:
            return await self._tokens_open(removal=False)
        return await self._tokens_submit(user_input, removal=False)

    async def async_step_local_tokens_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The token list before removing the access, after the user's choice."""
        if user_input is None:
            return self.async_abort(reason="configuration_changed")
        return await self._tokens_submit(user_input, removal=True)

    async def async_step_local_tokens_remove_none(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Nothing to revoke: the menu with the explicit choice."""
        if not (self._token_remove_none and self._token_removal):
            return self.async_abort(reason="configuration_changed")
        return self._tokens_remove_none_menu()

    def _tokens_remove_none_menu(self) -> ConfigFlowResult:
        self._token_choices = None
        self._token_remove_none = True
        return self.async_show_menu(
            step_id="local_tokens_remove_none",
            menu_options=[
                "local_tokens_remove_without",
                "local_tokens_remove_cancel",
            ],
        )

    async def async_step_local_tokens_remove_without(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The explicit choice to remove the access without revoking."""
        access = self._token_access
        if not (self._token_remove_none and self._token_removal and access):
            return self.async_abort(reason="configuration_changed")
        self._token_reset()
        return await self._remove_access(access.edit, "connection_removed")

    async def async_step_local_tokens_remove_cancel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The access stays; back to its menu."""
        if not (self._token_remove_none and self._token_removal):
            return self.async_abort(reason="configuration_changed")
        self._token_reset()
        return await self.async_step_connection()

    async def async_step_connection_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (
            user_input is not None
            and user_input.get("confirm") is True
            and user_input.get("revoke") is True
        ):
            return await self._tokens_open(removal=True)
        return await super().async_step_connection_remove(user_input)

    async def _tokens_open(self, *, removal: bool) -> ConfigFlowResult:
        self._token_reset()
        self._token_removal = removal
        try:
            access = self._token_load(self._connection_edit)
        except ConfigurationError:
            return self.async_abort(reason="configuration_changed")
        except ValueError, KeyError:
            # The stored access has no usable token.
            return self.async_abort(reason="local_tokens_unavailable")
        self._token_access = access
        if access.binding is not None:
            return await self._tokens_list()
        # A token entered by hand was never used on the sign-in port, whose
        # certificate was therefore not confirmed. The same certificate as on
        # the confirmed data port counts; another one needs a confirmation.
        try:
            observation = await inspect_local_certificate(
                access.context,
                PortRole.AUTHENTICATION,
                is_current=self._token_current,
                budget=await self._token_budget(access),
            )
        except LocalTransportError as err:
            return self._tokens_form(
                None, _TRANSPORT_ERRORS.get(err.failure, "local_connection_failed")
            )
        except StaleLocalRequest:
            return self.async_abort(reason="configuration_changed")
        except BudgetDeferred:
            return self._tokens_form(None, "local_busy")
        except LocalBudgetError:
            return self._tokens_form(None, "local_storage_failed")
        if observation.sha256 == access.resource_sha256:
            self._token_access = replace(
                access,
                binding=CertificateBinding(
                    access.context, PortRole.AUTHENTICATION, observation.sha256
                ),
            )
            return await self._tokens_list()
        self._token_observation = observation
        return self._tokens_certificate_form()

    def _tokens_certificate_form(self) -> ConfigFlowResult:
        observation = self._token_observation
        access = self._token_access
        assert observation is not None and access is not None
        return self.async_show_form(
            step_id="local_tokens_certificate",
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): BooleanSelector()}
            ),
            description_placeholders={
                "host": markdown_literal(access.context.endpoint.host),
                "port": str(PortRole.AUTHENTICATION.port),
                "fingerprint": observation.sha256.hex(),
            },
        )

    async def async_step_local_tokens_certificate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        observation = self._token_observation
        access = self._token_access
        if observation is None or access is None:
            return self.async_abort(reason="configuration_changed")
        if user_input is None:
            return self._tokens_certificate_form()
        if user_input.get("confirm") is not True:
            self._token_reset()
            return await self.async_step_connection()
        if observation.context != access.context or not self._token_current():
            return self.async_abort(reason="configuration_changed")
        # Trusted for this dialog only; nothing is saved.
        self._token_access = replace(
            access,
            binding=CertificateBinding(
                access.context, PortRole.AUTHENTICATION, observation.sha256
            ),
        )
        self._token_observation = None
        return await self._tokens_list()

    async def _tokens_list(self) -> ConfigFlowResult:
        access = self._token_access
        assert access is not None and access.binding is not None
        try:
            budget = await self._token_budget(access)
        except LocalBudgetError:
            return self._tokens_form(None, "local_storage_failed")
        outcome = await list_gateway_tokens(
            access.context,
            access.token,
            access.binding,
            is_current=self._token_current,
            budget=budget,
        )
        if outcome.tokens is None:
            if outcome.problem == AuthProblem.CONTEXT_CHANGED:
                return self.async_abort(reason="configuration_changed")
            return self._tokens_form(None, _problem_error(outcome.problem))
        return self._tokens_form(outcome.tokens)

    def _tokens_form(
        self, tokens: tuple[GatewayToken, ...] | None, error: str | None = None
    ) -> ConfigFlowResult:
        access = self._token_access
        own = sorted(
            (token for token in tokens or () if token.own),
            key=lambda token: token.created_at or 0.0,
        )
        others = sorted(
            (token for token in tokens or () if not token.own),
            key=lambda token: (not token.readable, token.created_at or 0.0),
        )
        unreadable = localize(self.hass.config.language, "nicht lesbar", "not readable")
        if self._token_removal and not own:
            # Nothing of this integration can be revoked, or the list could
            # not be read: the access is removed only after an explicit
            # choice, never silently.
            return self._tokens_remove_none_menu()
        use = (
            token_use(
                own,
                access.verified_at,
                created=access.created,
                other_accesses=self._token_other_accesses(access),
            )
            if access
            else TokenUse()
        )
        self._token_use = use
        language = self.hass.config.language
        markers = {
            **{
                token: localize(
                    language,
                    "von einem anderen Zugang genutzt",
                    "used by another access",
                )
                for token in use.others
            },
            **{
                token: localize(
                    language, "von diesem Zugang genutzt", "used by this access"
                )
                for token in use.this
            },
        }
        choices: dict[str, GatewayToken] = {}
        options: list[SelectOptionDict] = []
        labels: dict[str, int] = {}
        for number, token in enumerate(own, 1):
            label = _created(token.created_at)
            labels[label] = labels.get(label, 0) + 1
            if labels[label] > 1:
                label = f"{label} ({labels[label]})"
            if token in markers:
                label = f"{label} ({markers[token]})"
            choices[f"token_{number}"] = token
            options.append(SelectOptionDict(value=f"token_{number}", label=label))
        self._token_choices = choices
        schema: dict[Any, Any] = {}
        if options:
            schema[vol.Optional("token")] = SelectSelector(
                SelectSelectorConfig(options=options)
            )
        return self.async_show_form(
            step_id="local_tokens_remove" if self._token_removal else "local_tokens",
            data_schema=vol.Schema(schema),
            errors={"base": error} if error else {},
            description_placeholders={
                "count": "?" if tokens is None else str(len(tokens)),
                "own": _lines(
                    f"{_created(token.created_at)} ({markers[token]})"
                    if token in markers
                    else _created(token.created_at)
                    for token in own
                ),
                "others": _lines(
                    f"{markdown_literal(token.client_name) or '?'}: "
                    f"{_created(token.created_at)}"
                    if token.readable
                    else unreadable
                    for token in others
                ),
                "confirmed": _created(access.verified_at) if access else "?",
                "note": self._tokens_entered_note(access, tokens),
            },
        )

    def _token_other_accesses(self, access: _TokenAccess) -> list[tuple[float, bool]]:
        """Setup time and origin of the other saved accesses to this gateway,
        in any entry of this integration; switched off ones count too."""
        this = (self._entry.entry_id, access.edit.connection_id)
        try:
            return [
                (item.verified_at, item.credentials.origin == TokenOrigin.CREATED)
                for entry_id, connection_id, item in saved_local_accesses(self.hass)
                if (entry_id, connection_id) != this
                and item.gateway_id == access.gateway_id
                and item.credentials.token != access.token
            ]
        except Exception:  # only the marks are missing; nothing is sent
            return []

    def _tokens_entered_note(
        self, access: _TokenAccess | None, tokens: tuple[GatewayToken, ...] | None
    ) -> str:
        """A token entered by hand: why nothing is marked, after a read list."""
        if access is None or access.created or tokens is None:
            return ""
        note = localize(
            self.hass.config.language,
            "Dieser Zugang nutzt einen von Hand eingegebenen Token. Welcher "
            "Eintrag der Liste das ist, lässt sich nicht erkennen, deshalb ist "
            "keiner markiert. Er kann unter „Andere Programme“ stehen, deren "
            "Tokens nie zum Widerruf angeboten werden.",
            "This access uses a token that was entered by hand. Which entry of "
            "the list it is cannot be identified, so none is marked. It may be "
            "listed under other apps, whose tokens are never offered for "
            "revocation.",
        )
        return f"\n\n{note}"

    async def _tokens_submit(
        self, user_input: dict[str, Any], *, removal: bool
    ) -> ConfigFlowResult:
        choices = self._token_choices
        if (
            self._token_access is None
            or choices is None
            or removal != self._token_removal
        ):
            return self.async_abort(reason="configuration_changed")
        choice = user_input.get("token")
        if not choice:
            access = self._token_access
            self._token_reset()
            if removal:
                return await self._remove_access(access.edit, "connection_removed")
            return await self.async_step_connection()
        selected = choices.get(choice)
        # Only an offered entry of this integration; anything else is refused.
        if selected is None or not selected.own:
            self._token_reset()
            return self.async_abort(reason="configuration_changed")
        self._token_selected = selected
        return self._token_confirm_form()

    def _token_confirm_form(self, error: str | None = None) -> ConfigFlowResult:
        selected = self._token_selected
        access = self._token_access
        assert selected is not None and access is not None
        fields: dict[Any, Any] = {
            vol.Required("confirm", default=False): BooleanSelector()
        }
        # Which token an access uses is never certain: revoking any own token
        # may end a local access and needs a second, separate confirmation.
        fields[vol.Optional("confirm_end", default=False)] = BooleanSelector()
        return self.async_show_form(
            step_id="local_token_revoke",
            data_schema=vol.Schema(fields),
            errors={"base": error} if error else {},
            description_placeholders={
                "created": _created(selected.created_at),
                "host": markdown_literal(access.context.endpoint.host),
                "use": self._token_use_text(selected),
            },
        )

    def _token_use_text(self, selected: GatewayToken) -> str:
        language = self.hass.config.language
        if selected in self._token_use.this:
            return localize(
                language,
                "Es ist sehr wahrscheinlich der Token, den dieser Zugang nutzt: "
                "Widerrufst du ihn, endet dieser lokale Zugang.",
                "It is most likely the token this access uses: revoking it ends "
                "this local access.",
            )
        if selected in self._token_use.others:
            return localize(
                language,
                "Es ist sehr wahrscheinlich der Token eines anderen lokalen "
                "Zugangs zu diesem Gateway: Widerrufst du ihn, endet jener "
                "Zugang.",
                "It is most likely the token of another local access to this "
                "gateway: revoking it ends that access.",
            )
        return localize(
            language,
            "Welchen Token ein Zugang nutzt, lässt sich nicht sicher erkennen, "
            "etwa wenn ein anderes Home Assistant dieses Gateway nutzt. "
            "Widerrufst du einen genutzten Token, endet der lokale Zugang, der "
            "ihn nutzt.",
            "Which token an access uses cannot be told for certain, for example "
            "when another Home Assistant uses this gateway. Revoking a token in "
            "use ends the local access that uses it.",
        )

    async def async_step_local_token_revoke(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        selected = self._token_selected
        access = self._token_access
        if selected is None or access is None or access.binding is None:
            return self.async_abort(reason="configuration_changed")
        if user_input is None:
            return self._token_confirm_form()
        if user_input.get("confirm") is not True:
            # Without the explicit confirmation nothing is sent.
            self._token_reset()
            return await self.async_step_connection()
        if user_input.get("confirm_end") is not True:
            # Without the second confirmation nothing is sent.
            return self._token_confirm_form("local_token_end_unconfirmed")
        if not self._token_current():
            self._token_reset()
            return self.async_abort(reason="configuration_changed")
        try:
            budget = await self._token_budget(access)
        except LocalBudgetError:
            return self._token_confirm_form("local_storage_failed")
        outcome = await revoke_gateway_token(
            access.context,
            access.token,
            access.binding,
            selected,
            is_current=self._token_current,
            budget=budget,
        )
        return await self._token_finished(access, outcome)

    async def _token_finished(
        self, access: _TokenAccess, outcome: RevokeOutcome
    ) -> ConfigFlowResult:
        removal = self._token_removal
        if outcome.state == RevokeState.NOT_SENT:
            if outcome.problem == AuthProblem.CONTEXT_CHANGED:
                self._token_reset()
                return self.async_abort(reason="configuration_changed")
            # Nothing reached the gateway; submitting again is a new request.
            return self._token_confirm_form(_problem_error(outcome.problem))
        self._token_reset()
        if outcome.state in (RevokeState.REVOKED, RevokeState.ACCESS_ENDED):
            if removal:
                # Revoked while the access still reads: the chosen token was
                # not the one it used, which stays on the gateway.
                return await self._remove_access(
                    access.edit,
                    "local_token_revoked_removed"
                    if outcome.state == RevokeState.ACCESS_ENDED
                    else "local_token_old_revoked_removed",
                )
            if outcome.state == RevokeState.REVOKED:
                return await self._token_note(access.edit, "local_token_revoked")
            return await self._token_end_access(access)
        if outcome.state == RevokeState.LIST_CHANGED:
            return await self._token_note(access.edit, "local_tokens_changed")
        if removal:
            return await self._token_note(access.edit, "local_token_not_removed")
        return await self._token_note(
            access.edit,
            "local_token_revoke_rejected"
            if outcome.state == RevokeState.REJECTED
            else "local_token_revoke_unclear",
        )

    async def _token_end_access(self, access: _TokenAccess) -> ConfigFlowResult:
        """The access's own token is gone: the access has ended, which is not
        a failure. It is switched off until it is set up again; if that
        fails, the note says so."""
        management = self._management()
        try:
            edit = management.edit(access.edit.connection_id)
            stored = load_local_access(
                management.current().connections[edit.connection_id].to_data()
            )
            if stored.credentials.token != access.token:
                # Set up again meanwhile: the new token is not affected.
                return await self._token_note(edit, "local_token_revoked")
            management.set_enabled(edit, False)
            edit = management.edit(edit.connection_id)
        except ValueError, KeyError:
            return await self._token_note(access.edit, "local_token_access_end_failed")
        return await self._token_note(edit, "local_token_access_ended")

    async def _token_note(self, edit: ConnectionEdit, reason: str) -> ConfigFlowResult:
        """Back to the access's menu with the outcome as its note."""
        try:
            self._connection_edit = self._management().edit(edit.connection_id)
        except ValueError, KeyError:
            self._connection_edit = None
            return await self.async_return_to_menu("connections", reason)
        return await self.async_return_to_menu("connection", reason)

    @callback
    def async_remove(self) -> None:
        self._token_closed = True
        self._token_reset()
        super().async_remove()
