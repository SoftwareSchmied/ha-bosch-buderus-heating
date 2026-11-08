"""One bounded token request; provisioning does not authorize appliance writes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from types import SimpleNamespace

import aiohttp
from yarl import URL

from .ha_compat import local_header_limits
from .local_api.json import JsonLimits, load_json
from .local_budget import BudgetDeferred, BudgetLease, LocalReadBudget
from .local_credentials import (
    LocalCredentials,
    created_local_token,
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
                aiohttp.ClientSession(
                    trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    auto_decompress=False,
                    connector=aiohttp.TCPConnector(force_close=True, limit=1),
                    max_line_size=4096,
                    max_field_size=4096,
                    trace_configs=[trace],
                    **local_header_limits(),
                ) as session,
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
