"""The gateway's token list and the revocation of an old token (options).

Both run only on an explicit request in the dialog of one local access, with
the token and the certificate binding of that access. The list is shown and
then forgotten; it never reaches the configuration, the diagnostics or the
log. Only entries with this integration's own client name can be chosen for
a revocation, and a revocation needs a confirmation of its own. Nothing here
runs when an access or the whole entry is removed without that choice.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

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
from .local_credentials import load_local_access
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


def _created(value: float) -> str:
    """A creation time in the local time zone of Home Assistant."""
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


class LocalTokenFlow(ConnectionOptionsFlow):
    """Show the gateway's tokens; revoke one of this integration on request."""

    _token_access: _TokenAccess | None = None
    _token_choices: dict[str, GatewayToken] | None = None
    _token_selected: GatewayToken | None = None
    _token_observation: CertificateObservation | None = None
    _token_removal = False
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
            key=lambda token: token.created_at,
        )
        others = sorted(
            (token for token in tokens or () if not token.own),
            key=lambda token: token.created_at,
        )
        choices: dict[str, GatewayToken] = {}
        options: list[SelectOptionDict] = []
        labels: dict[str, int] = {}
        for number, token in enumerate(own, 1):
            label = _created(token.created_at)
            labels[label] = labels.get(label, 0) + 1
            if labels[label] > 1:
                label = f"{label} ({labels[label]})"
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
                "own": _lines(_created(token.created_at) for token in own),
                "others": _lines(
                    f"{markdown_literal(token.client_name) or '?'}: "
                    f"{_created(token.created_at)}"
                    for token in others
                ),
                "confirmed": _created(access.verified_at) if access else "?",
            },
        )

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
        return self.async_show_form(
            step_id="local_token_revoke",
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): BooleanSelector()}
            ),
            errors={"base": error} if error else {},
            description_placeholders={
                "created": _created(selected.created_at),
                "host": markdown_literal(access.context.endpoint.host),
            },
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
                return await self._remove_access(
                    access.edit, "local_token_revoked_removed"
                )
            if outcome.state == RevokeState.REVOKED:
                return await self._token_note(access.edit, "local_token_revoked")
            # The access's own token is gone: the access has ended, which is
            # not a failure. It is switched off until it is set up again.
            edit = access.edit
            try:
                self._management().set_enabled(edit, False)
                edit = self._management().edit(edit.connection_id)
            except ValueError, KeyError:
                pass
            return await self._token_note(edit, "local_token_access_ended")
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
