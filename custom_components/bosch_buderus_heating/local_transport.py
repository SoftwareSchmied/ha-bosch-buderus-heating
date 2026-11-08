"""Bounded local TLS inspection and read probes, without token provisioning."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
import socket
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from types import SimpleNamespace

import aiohttp
from yarl import URL

from .ha_compat import local_header_limits
from .local_budget import MAX_LOCAL_PAUSE_SECONDS, BudgetLease, LocalReadBudget
from .local_credentials import validate_local_token
from .local_probe import (
    MAX_BODY_BYTES,
    MAX_DISCOVERY_ADDRESSES,
    LocalEndpoint,
    LocalProbeError,
    ProbeContext,
    ProbePath,
    ProbeResponse,
    TransportFailure,
    normalize_local_address,
)

# Setup budgets, not manufacturer limits or polling intervals.
REQUEST_TIMEOUT = 5.0
PROBE_TIMEOUT = 15.0
PEER_TIMEOUT = 2.0
_CLOSE_TIMEOUT = 0.25
# An open connection to the gateway is used again only within this idle time
# (G4). It stays below the short idle limits common in small HTTP servers, so
# the client gives up a connection before the gateway would close it.
KEEPALIVE_IDLE_SECONDS = 4.0


class PortRole(StrEnum):
    AUTHENTICATION = "authentication"
    RESOURCES = "resources"

    @property
    def port(self) -> int:
        return (
            LocalEndpoint.auth_port
            if self == PortRole.AUTHENTICATION
            else LocalEndpoint.resource_port
        )


class LocalTransportError(Exception):
    """Expose a fixed category, never an SSL exception or private endpoint."""

    def __init__(self, failure: TransportFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


class StaleLocalRequest(Exception):
    def __init__(self) -> None:
        super().__init__("stale_local_request")


def _current(is_current: Callable[[], bool]) -> None:
    if not is_current():
        raise StaleLocalRequest()


@dataclass(frozen=True, slots=True)
class CertificateBinding:
    """A caller-confirmed, role-specific SHA-256 of the entire leaf certificate.

    Construction does not establish first trust. The setup owner must obtain
    confirmation before supplying this binding. It is not a PKI certificate.
    """

    context: ProbeContext = field(repr=False)
    role: PortRole
    sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.role, PortRole)
            or not isinstance(self.sha256, bytes)
            or len(self.sha256) != 32
        ):
            raise ValueError("invalid_certificate_binding")


@dataclass(frozen=True, slots=True)
class CertificateObservation:
    """Unapproved TLS evidence; no HTTP, credentials or identity comparison."""

    context: ProbeContext = field(repr=False)
    role: PortRole
    sha256: bytes = field(repr=False)
    peer: str = field(repr=False)
    tls_version: str | None


@dataclass(frozen=True, slots=True)
class LocalProbeExchange:
    responses: tuple[ProbeResponse, ...]
    retry_after_seconds: float | None = None


def _inspection_context() -> ssl.SSLContext:
    # Used only to observe a certificate without sending HTTP. Credentialed
    # requests below always use aiohttp's explicit SHA-256 fingerprint check.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _interface(host: str) -> str:
    address = ipaddress.ip_address(host)
    if not isinstance(address, ipaddress.IPv6Address) or not address.scope_id:
        return host
    scope = address.scope_id
    try:
        if scope.isdecimal():
            index = int(scope)
            socket.if_indextoname(index)
        else:
            index = socket.if_nametoindex(scope)
    except OSError, ValueError, OverflowError:
        raise LocalTransportError(TransportFailure.INTERFACE) from None
    return f"{host.partition('%')[0]}%{index}"


async def _resolve(endpoint: LocalEndpoint) -> tuple[str, ...]:
    try:
        ipaddress.ip_address(endpoint.host)
    except ValueError:
        try:
            rows = await asyncio.get_running_loop().getaddrinfo(
                endpoint.host, 0, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
        except OSError:
            raise LocalTransportError(TransportFailure.DNS) from None
        peers: list[str] = []
        for family, _, _, _, sockaddr in rows[:MAX_DISCOVERY_ADDRESSES]:
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            host = str(sockaddr[0])
            if (
                family == socket.AF_INET6
                and len(sockaddr) == 4
                and sockaddr[3]
                and "%" not in host
            ):
                host = f"{host}%{sockaddr[3]}"
            try:
                host = _interface(normalize_local_address(host))
            except LocalProbeError, LocalTransportError:
                continue
            if host not in peers:
                peers.append(host)
        if not peers:
            raise LocalTransportError(TransportFailure.DNS) from None
        return tuple(peers)
    return (_interface(endpoint.host),)


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        async with asyncio.timeout(_CLOSE_TIMEOUT):
            await writer.wait_closed()
    except OSError, TimeoutError:
        writer.transport.abort()
    except BaseException:
        writer.transport.abort()
        raise


async def _inspect(
    context: ProbeContext,
    role: PortRole,
    is_current: Callable[[], bool],
    expected_pin: bytes | None = None,
    *,
    budget: LocalReadBudget | None = None,
    lease: BudgetLease | None = None,
) -> CertificateObservation:
    _current(is_current)
    peers = await _resolve(context.endpoint)
    if budget is not None:
        assert lease is not None
        await budget.bind_peers(peers)
        budget.check_lease(lease)
    tls = await asyncio.to_thread(_inspection_context)
    last_failure = TransportFailure.CONNECT
    for peer in peers:
        _current(is_current)
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(PEER_TIMEOUT):
                _, writer = await asyncio.open_connection(
                    peer,
                    role.port,
                    ssl=tls,
                    server_hostname=context.endpoint.host.partition("%")[0],
                    ssl_handshake_timeout=PEER_TIMEOUT,
                )
                session = writer.get_extra_info("ssl_object")
                if session is None:
                    raise LocalTransportError(TransportFailure.TLS)
                certificate = session.getpeercert(binary_form=True)
                if not certificate:
                    raise LocalTransportError(TransportFailure.TLS)
                digest = hashlib.sha256(certificate).digest()
                if expected_pin is not None and digest != expected_pin:
                    # Never hide a certificate change by trying another peer.
                    raise LocalTransportError(TransportFailure.TLS_CHANGED)
                observation = CertificateObservation(
                    context, role, digest, peer, session.version()
                )
        except ssl.SSLError:
            raise LocalTransportError(TransportFailure.TLS) from None
        except TimeoutError:
            last_failure = TransportFailure.TIMEOUT
            continue
        except OSError:
            last_failure = TransportFailure.CONNECT
            continue
        finally:
            if writer is not None:
                await _close(writer)
        _current(is_current)
        return observation
    raise LocalTransportError(last_failure)


async def inspect_local_certificate(
    context: ProbeContext,
    role: PortRole,
    *,
    is_current: Callable[[], bool],
    budget: LocalReadBudget | None = None,
) -> CertificateObservation:
    """Inspect one port without transmitting HTTP or accepting a trust binding."""
    if not isinstance(role, PortRole):
        raise ValueError("invalid_port_role")
    try:
        if budget is not None:
            budget.check_scope(context)
            async with budget.slot(
                is_current=is_current, active_reserve=REQUEST_TIMEOUT
            ) as lease:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    return await _inspect(
                        context, role, is_current, budget=budget, lease=lease
                    )
        async with asyncio.timeout(REQUEST_TIMEOUT):
            return await _inspect(context, role, is_current)
    except TimeoutError:
        _current(is_current)
        raise LocalTransportError(TransportFailure.TIMEOUT) from None


def _retry_after(value: str | None) -> float | None:
    """Seconds of a Retry-After header, at most ``MAX_LOCAL_PAUSE_SECONDS``.

    A date in the past means no pause (0).
    """
    if value is None or len(value) > 128:
        return None
    try:
        if value.isascii() and value.isdecimal():
            delay = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            delay = max(0.0, (date - datetime.now(UTC)).total_seconds())
    except ValueError, OverflowError, TypeError:
        return None
    return min(delay, MAX_LOCAL_PAUSE_SECONDS) if math.isfinite(delay) else None


class _RepeatBlocked(Exception):
    pass


def single_attempt(
    is_current: Callable[[], bool],
) -> Callable[
    [aiohttp.ClientRequest, aiohttp.ClientHandlerType],
    Awaitable[aiohttp.ClientResponse],
]:
    """Prevent the HTTP library from repeating a logical GET after disconnect."""
    started = False

    async def once(
        request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
    ) -> aiohttp.ClientResponse:
        nonlocal started
        _current(is_current)
        if started:
            raise _RepeatBlocked()
        started = True
        return await handler(request)

    return once


def create_local_session(
    *,
    trace_configs: list[aiohttp.TraceConfig] | None = None,
    maximum_connections: int = 1,
    keep_alive: bool = False,
) -> aiohttp.ClientSession:
    """Own local sockets and check the fingerprint on every new connection.

    With ``keep_alive`` an answered connection is kept for the next request
    for ``KEEPALIVE_IDLE_SECONDS``. aiohttp keys its pool by address, server
    name and the ``ssl`` argument, so a kept connection is only ever used
    again for a request with the same pin object; the fingerprint itself is
    checked once per connection, right after the handshake and before the
    first request line is written.
    """
    connector = (
        aiohttp.TCPConnector(
            limit=maximum_connections, keepalive_timeout=KEEPALIVE_IDLE_SECONDS
        )
        if keep_alive
        else aiohttp.TCPConnector(force_close=True, limit=maximum_connections)
    )
    return aiohttp.ClientSession(
        trust_env=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=False,
        connector=connector,
        max_line_size=4096,
        max_field_size=4096,
        trace_configs=trace_configs,
        **local_header_limits(),
    )


async def read_response_body(
    response: aiohttp.ClientResponse,
    max_bytes: int,
    is_current: Callable[[], bool],
) -> bytes:
    """Read bounded uncompressed UTF-8 JSON bytes; decoding is a separate step."""
    if (
        response.content_type != "application/json"
        or response.headers.get("Content-Encoding", "identity").lower() != "identity"
        or (response.charset is not None and response.charset.lower() != "utf-8")
    ):
        raise LocalTransportError(TransportFailure.INVALID_RESPONSE)
    if response.content_length is not None and response.content_length > max_bytes:
        raise LocalTransportError(TransportFailure.BODY_TOO_LARGE)
    body = bytearray()
    while len(body) <= max_bytes:
        _current(is_current)
        chunk = await response.content.read(min(16384, max_bytes + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
    if len(body) > max_bytes:
        raise LocalTransportError(TransportFailure.BODY_TOO_LARGE)
    _current(is_current)
    return bytes(body)


async def _read(
    session: aiohttp.ClientSession,
    context: ProbeContext,
    observation: CertificateObservation,
    pin: aiohttp.Fingerprint,
    token: str,
    path: ProbePath,
    is_current: Callable[[], bool],
    budget: LocalReadBudget | None = None,
) -> tuple[ProbeResponse, float | None]:
    url = URL.build(
        scheme="https",
        host=observation.peer,
        port=PortRole.RESOURCES.port,
        path=path.value,
    )
    async with session.get(
        url,
        ssl=pin,
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
        server_hostname=context.endpoint.host.partition("%")[0],
        allow_redirects=False,
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        middlewares=(single_attempt(is_current),),
    ) as response:
        retry_after = (
            _retry_after(response.headers.get("Retry-After"))
            if response.status in (429, 503)
            else None
        )
        if budget is not None and (response.status == 429 or response.status >= 500):
            await budget.pause(
                60 if retry_after is None else retry_after,
                "rate_limited" if response.status == 429 else "service_unavailable",
            )
        _current(is_current)
        if response.status != 200:
            response.close()
            return ProbeResponse(path, context, response.status), retry_after
        body = await read_response_body(response, MAX_BODY_BYTES, is_current)
        return ProbeResponse(path, context, response.status, body), None


async def read_local_probe(
    context: ProbeContext,
    token: str,
    binding: CertificateBinding,
    *,
    is_current: Callable[[], bool],
    budget: LocalReadBudget | None = None,
) -> LocalProbeExchange:
    """Read only the three setup resources with explicit trust and a time budget.

    It neither stores nor creates tokens. The caller still evaluates the probe,
    verifies its gateway identity and commits setup only after those checks.
    """
    if binding.context != context:
        raise StaleLocalRequest()
    if binding.role != PortRole.RESOURCES:
        raise ValueError("resource_certificate_binding_required")
    validate_local_token(token)
    _current(is_current)
    if budget is not None:
        budget.check_scope(context)
        return await _budgeted_probe(context, token, binding, is_current, budget)
    path = ProbePath.BRAND
    responses: list[ProbeResponse] = []
    retry_after: float | None = None
    failure: TransportFailure | None = None
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with asyncio.timeout(REQUEST_TIMEOUT):
                peer = await _inspect(
                    context, PortRole.RESOURCES, is_current, binding.sha256
                )
            pin = aiohttp.Fingerprint(binding.sha256)
            async with create_local_session() as session:
                for path in ProbePath:
                    _current(is_current)
                    async with asyncio.timeout(REQUEST_TIMEOUT):
                        response, retry_after = await _read(
                            session, context, peer, pin, token, path, is_current
                        )
                    responses.append(response)
                    if response.status != 200:
                        break
    except LocalTransportError as error:
        failure = error.failure
    except aiohttp.ServerFingerprintMismatch:
        failure = TransportFailure.TLS_CHANGED
    except aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError:
        failure = TransportFailure.TLS
    except TimeoutError, aiohttp.ServerTimeoutError:
        failure = TransportFailure.TIMEOUT
    except aiohttp.ClientError, OSError, _RepeatBlocked:
        failure = TransportFailure.CONNECT
    _current(is_current)
    if failure is not None:
        # A timeout during session cleanup must not produce conflicting evidence
        # for a path whose response was already collected.
        responses = [response for response in responses if response.path != path]
        responses.append(ProbeResponse(path, context, failure=failure))
    return LocalProbeExchange(tuple(responses), retry_after)


async def _budgeted_probe(
    context: ProbeContext,
    token: str,
    binding: CertificateBinding,
    is_current: Callable[[], bool],
    budget: LocalReadBudget,
) -> LocalProbeExchange:
    responses: list[ProbeResponse] = []
    retry_after = None
    path = ProbePath.BRAND
    failure = None
    try:
        async with asyncio.timeout(120):
            for path in ProbePath:
                async with budget.slot(is_current=is_current) as lease:
                    async with asyncio.timeout(REQUEST_TIMEOUT):
                        peer = await _inspect(
                            context,
                            PortRole.RESOURCES,
                            is_current,
                            binding.sha256,
                            budget=budget,
                            lease=lease,
                        )
                        trace = aiohttp.TraceConfig()

                        async def headers_sent(
                            session: aiohttp.ClientSession,
                            trace_context: SimpleNamespace,
                            params: aiohttp.TraceRequestHeadersSentParams,
                        ) -> None:
                            await budget.start_http(lease, is_current)

                        trace.on_request_headers_sent.append(headers_sent)
                        async with create_local_session(
                            trace_configs=[trace]
                        ) as session:
                            response, retry_after = await _read(
                                session,
                                context,
                                peer,
                                aiohttp.Fingerprint(binding.sha256),
                                token,
                                path,
                                is_current,
                                budget,
                            )
                        responses.append(response)
                        if response.status != 200:
                            break
    except LocalTransportError as error:
        failure = error.failure
    except aiohttp.ServerFingerprintMismatch:
        failure = TransportFailure.TLS_CHANGED
    except aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError:
        failure = TransportFailure.TLS
    except TimeoutError, aiohttp.ServerTimeoutError:
        failure = TransportFailure.TIMEOUT
    except aiohttp.ClientError, OSError, _RepeatBlocked:
        failure = TransportFailure.CONNECT
    if failure is not None:
        if failure != TransportFailure.TLS_CHANGED:
            await budget.pause(60, "connection")
        responses = [response for response in responses if response.path != path]
        responses.append(ProbeResponse(path, context, failure=failure))
    _current(is_current)
    return LocalProbeExchange(tuple(responses), retry_after)
