"""Bounded requests to the gateway's sign-in service (port 9442).

One token request during setup, and, on explicit request in the options
dialog, the token list and the revocation of one listed token of this
integration. None of them authorizes appliance writes.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import SimpleNamespace
from typing import cast

import aiohttp
from yarl import URL

from .local_api.json import JsonLimits, load_json
from .local_budget import (
    SETUP_WAIT_LIMIT,
    BudgetDeferred,
    BudgetLease,
    LocalBudgetError,
    LocalReadBudget,
)
from .local_credentials import (
    LocalCredentialError,
    LocalCredentials,
    created_local_token,
    load_local_access,
)
from .local_probe import (
    ProbeContext,
    TransportFailure,
)
from .local_transport import (
    PROBE_TIMEOUT,
    REQUEST_TIMEOUT,
    CertificateBinding,
    LocalTransportError,
    PortRole,
    StaleLocalRequest,
    _current,
    _inspect,
    _retry_after,
    create_local_session,
    read_response_body,
)

MAX_AUTH_BODY = 16 * 1024
CLIENT_NAME = "Home Assistant Home Comfort"


class AuthState(StrEnum):
    NOT_SENT = "not_sent"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"
    RECEIVED = "received"


class AuthProblem(StrEnum):
    INVALID_CREDENTIALS = "invalid_credentials"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    PHYSICAL_CONFIRMATION_REQUIRED = "physical_confirmation_required"
    MEDIA_TYPE = "unsupported_media_type"
    TOKEN_DATABASE_FULL = "token_database_full"
    RATE_LIMITED = "rate_limited"
    BUDGET_BUSY = "local_budget_deferred"
    RESPONSE_UNCERTAIN = "token_creation_uncertain"
    CONTEXT_CHANGED = "context_changed"
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    CERTIFICATE_CHANGED = "certificate_changed"
    TLS = "tls"
    DNS = "dns"
    INTERFACE = "interface"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INVALID_RESPONSE = "invalid_response"


_REJECTIONS = {
    400: AuthProblem.INVALID_CREDENTIALS,
    401: AuthProblem.UNAUTHORIZED,
    403: AuthProblem.FORBIDDEN,
    404: AuthProblem.NOT_FOUND,
    412: AuthProblem.PHYSICAL_CONFIRMATION_REQUIRED,
    415: AuthProblem.MEDIA_TYPE,
    429: AuthProblem.RATE_LIMITED,
    507: AuthProblem.TOKEN_DATABASE_FULL,
}


@dataclass(frozen=True, slots=True)
class LabelCredentials:
    """Short-lived input only. It is never part of the enrollment journal."""

    login: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.login, self.password):
            if (
                not isinstance(value, str)
                or not 0 < len(value) <= 128
                or any(not 0x21 <= ord(c) <= 0x7E for c in value)
            ):
                raise ValueError("invalid_label_credentials")
        if not self.password.replace("-", ""):
            raise ValueError("invalid_label_credentials")

    def form(self) -> dict[str, str]:
        return {
            "grant_type": "password",
            "username": self.login,
            "password": self.password.replace("-", ""),
            "client_name": CLIENT_NAME,
        }


_STATUS_OF_PROBLEM = {problem: status for status, problem in _REJECTIONS.items()}


def rejection_status(problem: AuthProblem | None) -> int | None:
    """The HTTP status the sign-in service answered a problem with, if any."""
    return _STATUS_OF_PROBLEM.get(problem) if problem is not None else None


@dataclass(frozen=True, slots=True)
class AuthOutcome:
    state: AuthState
    credentials: LocalCredentials | None = field(default=None, repr=False)
    problem: AuthProblem | None = None
    retry_after_seconds: float | None = None


async def _response(response: aiohttp.ClientResponse) -> AuthOutcome:
    delay = (
        _retry_after(response.headers.get("Retry-After"))
        if response.status in (429, 503)
        else None
    )
    if response.status in _REJECTIONS:
        response.close()
        return AuthOutcome(
            AuthState.REJECTED,
            problem=_REJECTIONS[response.status],
            retry_after_seconds=delay,
        )
    uncertain = AuthOutcome(
        AuthState.UNCERTAIN,
        problem=AuthProblem.RESPONSE_UNCERTAIN,
        retry_after_seconds=delay,
    )
    if (
        response.status != 200
        or response.content_type != "application/json"
        or response.headers.get("Content-Encoding", "identity").lower() != "identity"
        or (response.charset is not None and response.charset.lower() != "utf-8")
        or (
            response.content_length is not None
            and response.content_length > MAX_AUTH_BODY
        )
    ):
        response.close()
        return uncertain
    body = bytearray()
    while len(body) <= MAX_AUTH_BODY:
        chunk = await response.content.read(min(4096, MAX_AUTH_BODY + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
    if len(body) > MAX_AUTH_BODY:
        return uncertain
    try:
        payload = load_json(
            bytes(body), exact_numbers=False, limits=JsonLimits(bytes=MAX_AUTH_BODY)
        )
        credentials = created_local_token(payload)
    except ValueError, RecursionError:
        return uncertain
    return AuthOutcome(AuthState.RECEIVED, credentials)


async def _request_local_token(
    context: ProbeContext,
    label: LabelCredentials,
    binding: CertificateBinding,
    *,
    is_current: Callable[[], bool],
    on_response: Callable[[AuthOutcome], Awaitable[None]] | None = None,
    budget: LocalReadBudget | None = None,
    lease: BudgetLease | None = None,
) -> AuthOutcome:
    """Send at most one POST after explicit trust on the authentication port.

    The enrollment owner must persist an uncertain attempt before calling this
    primitive. Cancellation propagates and leaves that attempt unresolved.
    A received token is retained even if the UI becomes inactive meanwhile;
    it is not a fresh identity proof or permission to update configuration.
    """
    if binding.context != context or binding.role != PortRole.AUTHENTICATION:
        raise ValueError("authentication_binding_required")
    started = False
    attempted = False
    outcome: AuthOutcome | None = None

    async def once(
        request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
    ) -> aiohttp.ClientResponse:
        nonlocal attempted
        _current(is_current)
        if attempted:
            raise StaleLocalRequest()
        attempted = True
        return await handler(request)

    async def headers_sent(
        session: aiohttp.ClientSession,
        trace_context: SimpleNamespace,
        params: aiohttp.TraceRequestHeadersSentParams,
    ) -> None:
        nonlocal started
        _current(is_current)
        if budget is not None:
            assert lease is not None
            await budget.start_http(lease, is_current)
        started = True

    trace = aiohttp.TraceConfig()
    trace.on_request_headers_sent.append(headers_sent)
    problem = AuthProblem.CONNECTION
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            _current(is_current)
            async with asyncio.timeout(REQUEST_TIMEOUT):
                peer = await _inspect(
                    context,
                    PortRole.AUTHENTICATION,
                    is_current,
                    binding.sha256,
                    budget=budget,
                    lease=lease,
                )
            async with (
                # The token POST carries the label credentials, so it shares
                # every session hardening of the resource reads.
                create_local_session(trace_configs=[trace]) as session,
                session.post(
                    URL.build(
                        scheme="https",
                        host=peer.peer,
                        port=PortRole.AUTHENTICATION.port,
                        path="/auth/token",
                    ),
                    data=label.form(),
                    headers={"Accept-Encoding": "identity"},
                    ssl=aiohttp.Fingerprint(binding.sha256),
                    server_hostname=context.endpoint.host.partition("%")[0],
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    middlewares=(once,),
                ) as response,
            ):
                outcome = await _response(response)
                if on_response is not None:
                    # Persist before asynchronous session cleanup can be cancelled.
                    await on_response(outcome)
                elif budget is not None and (
                    outcome.retry_after_seconds is not None
                    or outcome.problem == AuthProblem.RATE_LIMITED
                ):
                    await budget.pause(
                        outcome.retry_after_seconds
                        if outcome.retry_after_seconds is not None
                        else 60,
                        "rate_limited",
                    )
        return outcome
    except StaleLocalRequest:
        problem = AuthProblem.CONTEXT_CHANGED
    except LocalTransportError as err:
        problem = {
            TransportFailure.TLS_CHANGED: AuthProblem.CERTIFICATE_CHANGED,
            TransportFailure.TLS: AuthProblem.TLS,
            TransportFailure.TIMEOUT: AuthProblem.TIMEOUT,
            TransportFailure.DNS: AuthProblem.DNS,
            TransportFailure.INTERFACE: AuthProblem.INTERFACE,
        }.get(err.failure, AuthProblem.CONNECTION)
    except TimeoutError:
        problem = AuthProblem.TIMEOUT
    except aiohttp.ServerFingerprintMismatch:
        problem = AuthProblem.CERTIFICATE_CHANGED
    except aiohttp.ClientError, OSError:
        problem = AuthProblem.CONNECTION
    # Never throw away a valid response just because session cleanup failed.
    return outcome or AuthOutcome(
        AuthState.UNCERTAIN if started else AuthState.NOT_SENT,
        problem=AuthProblem.RESPONSE_UNCERTAIN if started else problem,
    )


async def request_local_token(
    context: ProbeContext,
    label: LabelCredentials,
    binding: CertificateBinding,
    *,
    is_current: Callable[[], bool],
    on_response: Callable[[AuthOutcome], Awaitable[None]] | None = None,
    budget: LocalReadBudget | None = None,
) -> AuthOutcome:
    if budget is None:
        return await _request_local_token(
            context, label, binding, is_current=is_current, on_response=on_response
        )
    budget.check_scope(context)
    try:
        async with budget.slot(
            is_current=is_current, active_reserve=PROBE_TIMEOUT
        ) as lease:
            return await _request_local_token(
                context,
                label,
                binding,
                is_current=is_current,
                on_response=on_response,
                budget=budget,
                lease=lease,
            )
    except BudgetDeferred:
        problem = AuthProblem.BUDGET_BUSY
    except StaleLocalRequest:
        problem = AuthProblem.CONTEXT_CHANGED
    except TimeoutError:
        problem = AuthProblem.TIMEOUT
    result = AuthOutcome(AuthState.NOT_SENT, problem=problem)
    if on_response is not None:
        await on_response(result)
    return result


# The token list and the revocation of a token, as the official local API
# documentation describes them: both on the sign-in port with the access's own
# token. They run only on an explicit request in the options dialog.
MAX_TOKEN_LIST_BODY = 64 * 1024
MAX_LISTED_TOKENS = 256
MAX_TOKEN_ID_LENGTH = 128
MAX_CLIENT_NAME_LENGTH = 256
# The list, the revocation and its read-back share one admission and this
# overall time; the revocation is only sent while the rest still covers it
# and its read-back.
TOKEN_REVOKE_DEADLINE = SETUP_WAIT_LIMIT
_TOKEN_LIST_PATH = "/auth/token"
_TOKEN_REVOKE_PATH = "/auth/revoke"
_LIST_REJECTIONS = {
    401: AuthProblem.UNAUTHORIZED,
    403: AuthProblem.FORBIDDEN,
    404: AuthProblem.NOT_FOUND,
    405: AuthProblem.NOT_FOUND,
    429: AuthProblem.RATE_LIMITED,
    503: AuthProblem.SERVICE_UNAVAILABLE,
}


def token_management_available(connection: Mapping[str, object]) -> bool:
    """Whether a stored local access holds a token that can ask for the list."""
    try:
        load_local_access(connection)
    except LocalCredentialError, ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class GatewayToken:
    """One entry of the gateway's token list; it never holds a token value.

    The client name is untrusted text from the gateway: it is shown literally,
    and only an exact match with this integration's own name makes an entry
    revocable. The token ID is used for the revocation only, never shown.
    """

    token_id: str = field(repr=False)
    client_name: str = field(repr=False)
    created_at: float

    @property
    def own(self) -> bool:
        return self.client_name == CLIENT_NAME


def parse_token_list(payload: object) -> tuple[GatewayToken, ...]:
    """The bounded entries of a token list answer, or ValueError.

    Further fields of an entry are ignored and never kept. A missing or
    repeated ID makes the whole answer unusable, so no entry can be confused
    with another.
    """
    if not isinstance(payload, list) or len(payload) > MAX_LISTED_TOKENS:
        raise ValueError("invalid_token_list")
    tokens: list[GatewayToken] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            raise ValueError("invalid_token_list")
        token_id = item.get("token_id")
        if type(token_id) is int and token_id >= 0:
            token_id = str(token_id)
        if (
            not isinstance(token_id, str)
            or not 0 < len(token_id) <= MAX_TOKEN_ID_LENGTH
            or any(not 0x21 <= ord(c) <= 0x7E for c in token_id)
            or token_id in seen
        ):
            raise ValueError("invalid_token_list")
        client_name = item.get("client_name")
        if (
            not isinstance(client_name, str)
            or len(client_name) > MAX_CLIENT_NAME_LENGTH
        ):
            raise ValueError("invalid_token_list")
        created_at = item.get("created_at")
        if type(created_at) not in (int, float):
            raise ValueError("invalid_token_list")
        try:
            created = float(cast(int | float, created_at))
        except OverflowError:
            # A JSON integer too large for a float is no creation time.
            raise ValueError("invalid_token_list") from None
        if not math.isfinite(created) or created < 0:
            raise ValueError("invalid_token_list")
        seen.add(token_id)
        tokens.append(GatewayToken(token_id, client_name, created))
    return tuple(tokens)


@dataclass(frozen=True, slots=True)
class TokenListOutcome:
    """The listed tokens, or why there is no list (a fixed problem)."""

    tokens: tuple[GatewayToken, ...] | None = field(default=None, repr=False)
    problem: AuthProblem | None = None
    retry_after_seconds: float | None = None


class RevokeState(StrEnum):
    # Nothing reached the gateway; the problem says why.
    NOT_SENT = "not_sent"
    # The fresh list no longer holds the chosen entry unchanged: nothing sent.
    LIST_CHANGED = "list_changed"
    # The read-back no longer lists the entry.
    REVOKED = "revoked"
    # Right after the revocation the gateway refuses the access's own token:
    # the revoked entry was the token of this access.
    ACCESS_ENDED = "access_ended"
    # The gateway refused the revocation (or asked for a pause); it is listed.
    REJECTED = "rejected"
    # Sent, but neither confirmed nor refused. Never repeated automatically.
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class RevokeOutcome:
    state: RevokeState
    problem: AuthProblem | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class _Reply:
    sent: bool
    status: int | None = None
    body: bytes | None = field(default=None, repr=False)
    retry_after_seconds: float | None = None
    problem: AuthProblem | None = None


async def _bearer_exchange(
    context: ProbeContext,
    token: str,
    binding: CertificateBinding,
    *,
    path: str,
    form: dict[str, str] | None,
    is_current: Callable[[], bool],
    budget: LocalReadBudget | None,
    lease: BudgetLease | None,
) -> _Reply:
    """One request on the sign-in port; the HTTP library never repeats it.

    The certificate is checked before anything is sent; ``sent`` tells
    whether the request headers may have reached the gateway.
    """
    started = False
    attempted = False
    reply: _Reply | None = None

    async def once(
        request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
    ) -> aiohttp.ClientResponse:
        nonlocal attempted
        _current(is_current)
        if attempted:
            raise StaleLocalRequest()
        attempted = True
        return await handler(request)

    async def headers_sent(
        session: aiohttp.ClientSession,
        trace_context: SimpleNamespace,
        params: aiohttp.TraceRequestHeadersSentParams,
    ) -> None:
        nonlocal started
        _current(is_current)
        if budget is not None:
            assert lease is not None
            await budget.start_http(lease, is_current)
        started = True

    trace = aiohttp.TraceConfig()
    trace.on_request_headers_sent.append(headers_sent)
    problem = AuthProblem.CONNECTION
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            _current(is_current)
            async with asyncio.timeout(REQUEST_TIMEOUT):
                peer = await _inspect(
                    context,
                    PortRole.AUTHENTICATION,
                    is_current,
                    binding.sha256,
                    budget=budget,
                    lease=lease,
                )
            async with (
                create_local_session(trace_configs=[trace]) as session,
                session.request(
                    "GET" if form is None else "POST",
                    URL.build(
                        scheme="https",
                        host=peer.peer,
                        port=PortRole.AUTHENTICATION.port,
                        path=path,
                    ),
                    data=form,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept-Encoding": "identity",
                    },
                    ssl=aiohttp.Fingerprint(binding.sha256),
                    server_hostname=context.endpoint.host.partition("%")[0],
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    middlewares=(once,),
                ) as response,
            ):
                status = response.status
                delay = (
                    _retry_after(response.headers.get("Retry-After"))
                    if status in (429, 503)
                    else None
                )
                if budget is not None and (status == 429 or status >= 500):
                    await budget.pause(
                        60 if delay is None else delay,
                        "rate_limited" if status == 429 else "service_unavailable",
                    )
                body = None
                if status == 200 and form is None:
                    body = await read_response_body(
                        response, MAX_TOKEN_LIST_BODY, is_current
                    )
                else:
                    response.close()
                reply = _Reply(True, status, body, delay)
    except StaleLocalRequest:
        problem = AuthProblem.CONTEXT_CHANGED
    except BudgetDeferred, LocalBudgetError:
        problem = AuthProblem.BUDGET_BUSY
    except LocalTransportError as err:
        problem = {
            TransportFailure.TLS_CHANGED: AuthProblem.CERTIFICATE_CHANGED,
            TransportFailure.TLS: AuthProblem.TLS,
            TransportFailure.TIMEOUT: AuthProblem.TIMEOUT,
            TransportFailure.DNS: AuthProblem.DNS,
            TransportFailure.INTERFACE: AuthProblem.INTERFACE,
            TransportFailure.BODY_TOO_LARGE: AuthProblem.INVALID_RESPONSE,
            TransportFailure.INVALID_RESPONSE: AuthProblem.INVALID_RESPONSE,
        }.get(err.failure, AuthProblem.CONNECTION)
    except TimeoutError:
        problem = AuthProblem.TIMEOUT
    except aiohttp.ServerFingerprintMismatch:
        problem = AuthProblem.CERTIFICATE_CHANGED
    except aiohttp.ClientError, OSError:
        problem = AuthProblem.CONNECTION
    # A collected answer stands even if the session cleanup failed.
    return reply or _Reply(started, problem=problem)


def _token_list(reply: _Reply) -> TokenListOutcome:
    if reply.status is None:
        return TokenListOutcome(problem=reply.problem or AuthProblem.CONNECTION)
    if reply.status == 200 and reply.body is not None:
        try:
            tokens = parse_token_list(
                load_json(
                    reply.body,
                    exact_numbers=False,
                    limits=JsonLimits(bytes=MAX_TOKEN_LIST_BODY),
                )
            )
        except ValueError, RecursionError:
            return TokenListOutcome(problem=AuthProblem.INVALID_RESPONSE)
        return TokenListOutcome(tokens)
    return TokenListOutcome(
        problem=_LIST_REJECTIONS.get(reply.status, AuthProblem.INVALID_RESPONSE),
        retry_after_seconds=reply.retry_after_seconds,
    )


async def _list(
    context: ProbeContext,
    token: str,
    binding: CertificateBinding,
    is_current: Callable[[], bool],
    budget: LocalReadBudget | None,
    lease: BudgetLease | None,
) -> TokenListOutcome:
    return _token_list(
        await _bearer_exchange(
            context,
            token,
            binding,
            path=_TOKEN_LIST_PATH,
            form=None,
            is_current=is_current,
            budget=budget,
            lease=lease,
        )
    )


def _check_binding(context: ProbeContext, binding: CertificateBinding) -> None:
    if binding.context != context or binding.role != PortRole.AUTHENTICATION:
        raise ValueError("authentication_binding_required")


async def list_gateway_tokens(
    context: ProbeContext,
    token: str,
    binding: CertificateBinding,
    *,
    is_current: Callable[[], bool],
    budget: LocalReadBudget | None = None,
) -> TokenListOutcome:
    """Read the gateway's token list once (GET, nothing changes).

    Only with a confirmed certificate of the sign-in port; the answer is
    bounded and never kept beyond the caller's dialog.
    """
    _check_binding(context, binding)
    if budget is None:
        return await _list(context, token, binding, is_current, None, None)
    budget.check_scope(context)
    try:
        async with budget.slot(
            is_current=is_current, active_reserve=PROBE_TIMEOUT
        ) as lease:
            return await _list(context, token, binding, is_current, budget, lease)
    except BudgetDeferred as err:
        return TokenListOutcome(
            problem=AuthProblem.BUDGET_BUSY, retry_after_seconds=err.seconds
        )
    except LocalBudgetError:
        return TokenListOutcome(problem=AuthProblem.BUDGET_BUSY)
    except StaleLocalRequest:
        return TokenListOutcome(problem=AuthProblem.CONTEXT_CHANGED)
    except TimeoutError:
        return TokenListOutcome(problem=AuthProblem.TIMEOUT)


async def _revoke(
    context: ProbeContext,
    token: str,
    binding: CertificateBinding,
    selected: GatewayToken,
    is_current: Callable[[], bool],
    budget: LocalReadBudget | None,
    lease: BudgetLease | None,
    deadline: float,
) -> RevokeOutcome:
    loop = asyncio.get_running_loop()
    # Always a fresh list right before the revocation, never a shown one.
    listed = await _list(context, token, binding, is_current, budget, lease)
    if listed.tokens is None:
        return RevokeOutcome(
            RevokeState.NOT_SENT, listed.problem, listed.retry_after_seconds
        )
    entry = next((t for t in listed.tokens if t.token_id == selected.token_id), None)
    if entry is None or entry != selected or not entry.own:
        return RevokeOutcome(RevokeState.LIST_CHANGED)
    if deadline - loop.time() < 2 * PROBE_TIMEOUT:
        # The rest of the time no longer covers the revocation and its
        # read-back: nothing is sent.
        return RevokeOutcome(RevokeState.NOT_SENT, AuthProblem.TIMEOUT)
    reply = await _bearer_exchange(
        context,
        token,
        binding,
        path=_TOKEN_REVOKE_PATH,
        form={"token_id": entry.token_id, "usage_type": "private"},
        is_current=is_current,
        budget=budget,
        lease=lease,
    )
    if not reply.sent:
        return RevokeOutcome(
            RevokeState.NOT_SENT, reply.problem or AuthProblem.CONNECTION
        )
    if reply.status in (429, 503):
        # The gateway asked for a pause instead of handling the request.
        return RevokeOutcome(
            RevokeState.REJECTED,
            _LIST_REJECTIONS[reply.status],
            reply.retry_after_seconds,
        )
    accepted = reply.status is not None and 200 <= reply.status < 300
    # Read back once; whatever it shows, the revocation is never repeated.
    after = await _list(context, token, binding, is_current, budget, lease)
    if after.tokens is not None:
        if all(t.token_id != entry.token_id for t in after.tokens):
            return RevokeOutcome(RevokeState.REVOKED)
        if reply.status is not None and not accepted:
            return RevokeOutcome(RevokeState.REJECTED)
        return RevokeOutcome(RevokeState.UNCERTAIN)
    if after.problem == AuthProblem.UNAUTHORIZED and (accepted or reply.status is None):
        return RevokeOutcome(RevokeState.ACCESS_ENDED)
    return RevokeOutcome(RevokeState.UNCERTAIN, after.problem)


async def revoke_gateway_token(
    context: ProbeContext,
    token: str,
    binding: CertificateBinding,
    selected: GatewayToken,
    *,
    is_current: Callable[[], bool],
    budget: LocalReadBudget | None = None,
) -> RevokeOutcome:
    """Revoke one listed token of this integration after the user confirmed.

    Reads the list again first and sends nothing unless the chosen entry is
    still there unchanged and carries this integration's client name. Then
    exactly one POST, without any retry, and one read-back of the list.
    """
    _check_binding(context, binding)
    if not selected.own:
        raise ValueError("foreign_token")
    deadline = asyncio.get_running_loop().time() + TOKEN_REVOKE_DEADLINE
    if budget is None:
        return await _revoke(
            context, token, binding, selected, is_current, None, None, deadline
        )
    budget.check_scope(context)
    try:
        async with budget.slot(
            is_current=is_current, active_reserve=3 * PROBE_TIMEOUT
        ) as lease:
            return await _revoke(
                context, token, binding, selected, is_current, budget, lease, deadline
            )
    except BudgetDeferred as err:
        return RevokeOutcome(RevokeState.NOT_SENT, AuthProblem.BUDGET_BUSY, err.seconds)
    except LocalBudgetError:
        # Only the admission raises this, before anything is sent.
        return RevokeOutcome(RevokeState.NOT_SENT, AuthProblem.BUDGET_BUSY)
    except StaleLocalRequest:
        return RevokeOutcome(RevokeState.NOT_SENT, AuthProblem.CONTEXT_CHANGED)
    except TimeoutError:
        return RevokeOutcome(RevokeState.NOT_SENT, AuthProblem.TIMEOUT)
