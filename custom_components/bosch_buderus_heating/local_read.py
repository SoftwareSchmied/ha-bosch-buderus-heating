"""Bounded local GETs sharing gateway admission, pacing and durable pauses."""

from __future__ import annotations

import asyncio
import math
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from types import SimpleNamespace
from typing import Self

import aiohttp
from yarl import URL

from . import local_transport as transport
from .local_api.catalog import ReadRequest, response_schema
from .local_api.decoder import decode_resource
from .local_api.errors import LocalResourceError
from .local_api.json import DEFAULT_LIMITS, JsonLimits
from .local_api.models import LocalObservation
from .local_budget import (
    MINIMUM_START_GAP,
    BudgetDeferred,
    BudgetLease,
    LocalBudgetError,
    LocalReadBudget,
)
from .local_credentials import validate_local_token
from .local_keepalive import ConnectionKind, ConnectionReuse, ReuseFailure
from .local_probe import ProbeContext, TransportFailure
from .local_transport import CertificateBinding, PortRole, StaleLocalRequest

DEFAULT_FAILURE_PAUSE = 60.0
# A resolved address list is used again for this long (O48). Every new
# resolution runs the address checks; a connection failure resolves anew.
RESOLVE_REUSE_SECONDS = 60.0
# Response times of the most recent reads kept for the diagnostics (O93).
RESPONSE_SAMPLES = 256
# T6: the phases of a read in the diagnostics: the connection set-up (address
# check, certificate inspection, a new TCP/TLS connection) until the request
# headers are sent, the wait for the response headers, and reading and
# decoding the body.
RESPONSE_PHASES = ("connection_setup", "gateway_wait", "body_and_decode")
# T8: the budget manager closed, because Home Assistant stops.
BUDGET_CLOSED = "local_budget_closed"


def _budget_problem(error: LocalBudgetError) -> str:
    """The fixed code a budget problem is reported with (T8)."""
    if str(error) == BUDGET_CLOSED:
        return BUDGET_CLOSED
    return "local_budget_storage_failed"


def _phase_times(samples: deque[float]) -> dict[str, object]:
    """Number, median, 95th percentile and maximum in milliseconds."""
    ordered = sorted(samples)
    result: dict[str, object] = {"samples": len(ordered)}
    if ordered:
        result |= {
            "median_ms": round(ordered[(len(ordered) - 1) // 2], 1),
            "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 1),
            "maximum_ms": round(ordered[-1], 1),
        }
    return result


class ReadState(StrEnum):
    SUCCESS = "success"
    PATH_ERROR = "path_error"
    ACCESS_ERROR = "access_error"
    DEFERRED = "deferred"


class ReadProblem(StrEnum):
    BUSY = "busy"
    PAUSED = "paused"
    AUTH_REQUIRED = "auth_required"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    NOT_ACCEPTABLE = "not_acceptable"
    RATE_LIMITED = "rate_limited"
    SERVICE_UNAVAILABLE = "service_unavailable"
    HTTP_ERROR = "http_error"
    # A kept connection was closed before any answer (G4): read again later.
    REUSED_CONNECTION_CLOSED = "reused_connection_closed"


@dataclass(frozen=True, slots=True)
class LocalReadLimits:
    """Transport bounds, separate from future polling and history policies."""

    timeout: float = 5.0
    json: JsonLimits = DEFAULT_LIMITS

    def __post_init__(self) -> None:
        if (
            type(self.timeout) not in {int, float}
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
            or not isinstance(self.json, JsonLimits)
        ):
            raise ValueError("invalid_local_read_limits")


DEFAULT_READ_LIMITS = LocalReadLimits()


@dataclass(frozen=True, slots=True)
class ReadMetrics:
    calls: int = 0
    tls_inspections: int = 0
    http_requests: int = 0
    http_responses: int = 0
    successes: int = 0
    deferred: int = 0


@dataclass(frozen=True, slots=True)
class ReadResult:
    request: ReadRequest = field(repr=False)
    state: ReadState
    status: int | None = None
    problem: str | None = None
    observation: LocalObservation | None = field(default=None, repr=False)
    http_requests: int = 0
    retry_after_seconds: float | None = None
    response_bytes: int = 0

    @property
    def stop_access(self) -> bool:
        return self.state in {ReadState.ACCESS_ERROR, ReadState.DEFERRED}


def _failure_class(result: ReadResult) -> str | None:
    """The class of a failed read for the diagnostics; None for a success."""
    if result.state is ReadState.SUCCESS:
        return None
    problem, status = result.problem, result.status
    if problem == TransportFailure.TIMEOUT:
        return "timeout"
    if problem in {TransportFailure.TLS, TransportFailure.TLS_CHANGED}:
        return "tls"
    if status == 404:
        return "not_found"
    if status is not None and 400 <= status < 500:
        return "http_4xx"
    if status is not None and status >= 500:
        return "http_5xx"
    if problem in {
        TransportFailure.CONNECT,
        TransportFailure.DNS,
        TransportFailure.INTERFACE,
    }:
        return "connection"
    return "other"


@dataclass(slots=True)
class _Attempt:
    lease: BudgetLease
    headers: int = 0
    status: int | None = None
    # Whether the request went over a new or a kept connection (G4).
    connection: ConnectionKind | None = None
    # How long the request waited for the spacing between request headers;
    # our own wait, never part of a response time (T1).
    paced: float = 0.0
    # T6: when the request headers were sent, the response headers received
    # and the read was complete (body read and decoded, or the error answer
    # closed), all monotonic.
    sent: float | None = None
    answered: float | None = None
    done: float | None = None


class LocalReadClient:
    """Own a credentialed session with explicit full-leaf certificate binding."""

    def __init__(
        self,
        context: ProbeContext,
        token: str,
        binding: CertificateBinding,
        *,
        budget: LocalReadBudget,
        is_current: Callable[[], bool],
        limits: LocalReadLimits = DEFAULT_READ_LIMITS,
    ) -> None:
        if binding.context != context:
            raise StaleLocalRequest()
        if binding.role != PortRole.RESOURCES:
            raise ValueError("resource_certificate_binding_required")
        budget.check_scope(context)
        self._token = validate_local_token(token)
        self._context = context
        self._binding = binding
        self._budget = budget
        self._is_current = is_current
        self._limits = limits
        # One pin object per client: aiohttp keeps connections per ``ssl``
        # argument, so only connections checked against it are used again.
        self._pin = aiohttp.Fingerprint(binding.sha256)
        # The kept-alive session, and the one opening a connection per read
        # while the gateway does not keep connections reliably (G4).
        self._session: aiohttp.ClientSession | None = None
        self._fallback_session: aiohttp.ClientSession | None = None
        self._reuse = ConnectionReuse()
        self._active: set[asyncio.Task[ReadResult]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._halted: str | None = None
        # The last inspected peer whose certificate matched the binding.
        self._peer: str | None = None
        # The endpoint's checked addresses and when they were resolved.
        self._resolved: tuple[tuple[str, ...], float] | None = None
        self._metrics = ReadMetrics()
        self._durations: deque[float] = deque(maxlen=RESPONSE_SAMPLES)
        # T6: the same reads split into their phases (milliseconds).
        self._phases: dict[str, deque[float]] = {
            phase: deque(maxlen=RESPONSE_SAMPLES) for phase in RESPONSE_PHASES
        }
        self._failure_classes: Counter[str] = Counter()
        self._deferred_reasons: Counter[str] = Counter()

    @property
    def metrics(self) -> ReadMetrics:
        return self._metrics

    def health(self) -> dict[str, object]:
        """Failure classes, response times and deferral reasons (O93).

        Fixed codes and durations only: no addresses, paths or values.
        """
        durations = sorted(self._durations)
        times: dict[str, object] = {"samples": len(durations)}
        if durations:
            times |= {
                "average_ms": round(sum(durations) / len(durations), 1),
                "p95_ms": round(durations[math.ceil(0.95 * len(durations)) - 1], 1),
                "maximum_ms": round(durations[-1], 1),
            }
        return {
            "failures_by_class": dict(sorted(self._failure_classes.items())),
            "response_times": times,
            "response_phases": {
                phase: _phase_times(samples) for phase, samples in self._phases.items()
            },
            "deferred_reasons": dict(sorted(self._deferred_reasons.items())),
        }

    def connection_reuse(self) -> dict[str, object]:
        """Keep-alive counts and fallback state (G4, O56), fixed codes only."""
        return self._reuse.diagnostics(self._metrics.tls_inspections)

    def _record(self, result: ReadResult, started: float, attempt: _Attempt) -> None:
        if result.state is ReadState.DEFERRED:
            self._deferred_reasons[str(result.problem or "unknown")] += 1
            return
        # T8: one definition of the response time everywhere: from the start
        # of the read until it is complete, without our own wait for the
        # header spacing and without saving a pause afterwards.
        end = attempt.done if attempt.done is not None else monotonic()
        total = max(0.0, end - started - attempt.paced)
        if result.http_requests:
            duration = total * 1000
            self._durations.append(duration)
            if attempt.connection is not None and attempt.status is not None:
                self._reuse.timed(attempt.connection, duration)
        gateway = None
        if attempt.sent is not None and attempt.answered is not None:
            gateway = max(0.0, attempt.answered - attempt.sent)
            setup = max(0.0, attempt.sent - started - attempt.paced)
            self._phases["connection_setup"].append(setup * 1000)
            self._phases["gateway_wait"].append(gateway * 1000)
            if attempt.done is not None:
                body = max(0.0, attempt.done - attempt.answered)
                self._phases["body_and_decode"].append(body * 1000)
        if (failure := _failure_class(result)) is not None:
            self._failure_classes[failure] += 1
        # The latency brake (T1, T6, T7). A timeout on a kept connection is
        # most likely a connection the gateway dropped: it only counts for the
        # keep-alive fallback. Answers add their gateway time only, so the
        # connection set-up (also a TLS handshake in the fallback), the body,
        # decoding and other work of Home Assistant never slow the reads.
        if result.problem == TransportFailure.TIMEOUT:
            if attempt.connection is not ConnectionKind.REUSED:
                self._budget.brake("timeout")
        elif attempt.status is not None and attempt.status >= 500:
            self._budget.brake("server_error")
        elif gateway is not None:
            self._budget.observe(gateway)

    def _count(self, **increments: int) -> None:
        self._metrics = replace(
            self._metrics,
            **{
                key: getattr(self._metrics, key) + value
                for key, value in increments.items()
            },
        )

    def _current(self) -> None:
        if self._closed:
            raise StaleLocalRequest()
        transport._current(self._is_current)

    async def _headers_sent(
        self,
        session: aiohttp.ClientSession,
        trace: SimpleNamespace,
        params: aiohttp.TraceRequestHeadersSentParams,
    ) -> None:
        self._current()
        attempt = trace.trace_request_ctx
        assert isinstance(attempt, _Attempt)
        if attempt.headers:
            raise transport._RepeatBlocked()
        waiting = monotonic()
        await self._budget.start_http(
            attempt.lease, lambda: not self._closed and self._is_current()
        )
        attempt.sent = monotonic()
        attempt.paced += attempt.sent - waiting
        attempt.headers += 1
        self._count(http_requests=1)

    async def _connection_created(
        self,
        session: aiohttp.ClientSession,
        trace: SimpleNamespace,
        params: aiohttp.TraceConnectionCreateStartParams,
    ) -> None:
        self._connected(trace, ConnectionKind.NEW)

    async def _connection_reused(
        self,
        session: aiohttp.ClientSession,
        trace: SimpleNamespace,
        params: aiohttp.TraceConnectionReuseconnParams,
    ) -> None:
        self._connected(trace, ConnectionKind.REUSED)

    def _connected(self, trace: SimpleNamespace, kind: ConnectionKind) -> None:
        attempt = trace.trace_request_ctx
        assert isinstance(attempt, _Attempt)
        attempt.connection = kind
        self._reuse.connected(kind)

    def _get_session(self, keep_alive: bool = True) -> aiohttp.ClientSession:
        session = self._session if keep_alive else self._fallback_session
        if session is None:
            trace = aiohttp.TraceConfig()
            trace.on_request_headers_sent.append(self._headers_sent)
            trace.on_connection_create_start.append(self._connection_created)
            trace.on_connection_reuseconn.append(self._connection_reused)
            session = transport.create_local_session(
                trace_configs=[trace], maximum_connections=2, keep_alive=keep_alive
            )
            if keep_alive:
                self._session = session
            else:
                self._fallback_session = session
        return session

    async def _access_error(
        self,
        request: ReadRequest,
        attempt: _Attempt,
        problem: str,
        delay: float | None = None,
    ) -> ReadResult:
        if problem not in {ReadProblem.RATE_LIMITED, ReadProblem.SERVICE_UNAVAILABLE}:
            # The next read resolves the address and inspects the certificate
            # again (another address, a restarted gateway); an answered
            # request keeps the peer.
            self._peer = None
            self._resolved = None
        if problem in {ReadProblem.AUTH_REQUIRED, TransportFailure.TLS_CHANGED}:
            self._halted = problem
        else:
            await self._budget.pause(
                DEFAULT_FAILURE_PAUSE if delay is None else delay,
                problem
                if problem
                in {ReadProblem.RATE_LIMITED, ReadProblem.SERVICE_UNAVAILABLE}
                else "connection",
            )
        return ReadResult(
            request,
            ReadState.ACCESS_ERROR,
            attempt.status,
            problem,
            http_requests=attempt.headers,
            retry_after_seconds=self._budget.remaining_seconds,
        )

    async def _http_error(
        self, request: ReadRequest, attempt: _Attempt, delay: float | None
    ) -> ReadResult:
        status = attempt.status
        assert status is not None
        if status == 401:
            return await self._access_error(request, attempt, ReadProblem.AUTH_REQUIRED)
        if status == 429:
            return await self._access_error(
                request, attempt, ReadProblem.RATE_LIMITED, delay
            )
        if status >= 500:
            return await self._access_error(
                request, attempt, ReadProblem.SERVICE_UNAVAILABLE, delay
            )
        problem = {
            403: ReadProblem.FORBIDDEN,
            404: ReadProblem.NOT_FOUND,
            406: ReadProblem.NOT_ACCEPTABLE,
        }.get(status, ReadProblem.HTTP_ERROR)
        if 300 <= status < 400:
            return ReadResult(
                request,
                ReadState.PATH_ERROR,
                status,
                TransportFailure.REDIRECT,
                http_requests=attempt.headers,
            )
        return ReadResult(
            request,
            ReadState.PATH_ERROR,
            status,
            problem,
            http_requests=attempt.headers,
        )

    async def _peer_for(self, attempt: _Attempt) -> str:
        """The address to read from, inspected only when it is not known yet.

        The address checks run for every read. The separate certificate
        inspection is skipped while the inspected peer is still one of the
        endpoint's addresses: the request pins the same SHA-256 fingerprint,
        and aiohttp compares it right after the handshake, before any request
        line or header (and so the token) is written. A mismatch is reported
        as a changed certificate, as the inspection would.
        """

        def is_current() -> bool:
            return not self._closed and self._is_current()

        peer = self._peer
        if peer is not None:
            transport._current(is_current)
            peers = await self._resolve()
            await self._budget.bind_peers(peers)
            self._budget.check_lease(attempt.lease)
            if peer in peers:
                return peer
            self._peer = None
            self._resolved = None
        self._count(tls_inspections=1)
        observation = await transport._inspect(
            self._context,
            PortRole.RESOURCES,
            is_current,
            self._binding.sha256,
            budget=self._budget,
            lease=attempt.lease,
        )
        self._peer = observation.peer
        return observation.peer

    async def _resolve(self) -> tuple[str, ...]:
        """The endpoint's checked addresses, resolved at most once a minute."""
        resolved = self._resolved
        now = monotonic()
        if resolved is not None and 0 <= now - resolved[1] < RESOLVE_REUSE_SECONDS:
            return resolved[0]
        peers = await transport._resolve(self._context.endpoint)
        self._resolved = (peers, now)
        return peers

    async def _get(self, request: ReadRequest, attempt: _Attempt) -> ReadResult:
        peer = await self._peer_for(attempt)
        self._current()
        origin = URL.build(scheme="https", host=peer, port=PortRole.RESOURCES.port)
        url = URL(str(origin) + request.target, encoded=True)
        keep_alive = self._reuse.keep_alive
        async with self._get_session(keep_alive).get(
            url,
            ssl=self._pin,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept-Encoding": "identity",
            },
            server_hostname=self._context.endpoint.host.partition("%")[0],
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=self._limits.timeout),
            middlewares=(
                transport.single_attempt(
                    lambda: not self._closed and self._is_current()
                ),
            ),
            trace_request_ctx=attempt,
        ) as response:
            attempt.answered = monotonic()
            attempt.status = response.status
            self._count(http_responses=1)
            if keep_alive:
                self._reuse.answered(
                    closes="close" in response.headers.get("Connection", "").lower()
                )
            delay = (
                transport._retry_after(response.headers.get("Retry-After"))
                if response.status in {429, 503}
                else None
            )
            if response.status != 200:
                # Without reading the error body. aiohttp has already kept the
                # connection when the answer was complete; any other is closed.
                response.close()
                attempt.done = monotonic()
                result = await self._http_error(request, attempt, delay)
                # Preserve an already observed gateway pause even after a change.
                self._current()
                return result
            self._current()
            body = await transport.read_response_body(
                response,
                self._limits.json.bytes,
                lambda: not self._closed and self._is_current(),
            )
        self._current()
        observation = decode_resource(
            request,
            body,
            context=self._context.identity,
            observed_at=datetime.now(UTC),
            observed_monotonic=monotonic(),
            limits=self._limits.json,
        )
        attempt.done = monotonic()
        await asyncio.sleep(0)  # Deliver cancellation/expiry before publishing.
        self._current()
        return ReadResult(
            request,
            ReadState.SUCCESS,
            200,
            observation=observation,
            http_requests=attempt.headers,
            response_bytes=len(body),
        )

    async def read(self, request: ReadRequest) -> ReadResult:
        """Read once or report deferral immediately. Never queue or retry here."""
        self._current()
        self._count(calls=1)
        response_schema(request)  # Validate the complete request before any I/O.
        if self._halted is not None:
            return ReadResult(request, ReadState.ACCESS_ERROR, problem=self._halted)
        busy = self._budget.busy
        try:
            lease = self._budget.claim()
        except BudgetDeferred:
            lease = None
        except LocalBudgetError as error:
            return ReadResult(
                request, ReadState.ACCESS_ERROR, problem=_budget_problem(error)
            )
        if lease is None:
            self._count(deferred=1)
            problem = ReadProblem.BUSY if busy else ReadProblem.PAUSED
            self._deferred_reasons[problem.value] += 1
            return ReadResult(
                request,
                ReadState.DEFERRED,
                problem=problem,
                retry_after_seconds=max(
                    MINIMUM_START_GAP, self._budget.remaining_seconds
                ),
            )
        if self._active:
            self._reuse.overlapping()
        task = asyncio.create_task(self._execute(request, lease))
        self._active.add(task)
        task.add_done_callback(lambda done: self._finished(done, lease))
        return await task

    def _finished(self, task: asyncio.Task[ReadResult], lease: BudgetLease) -> None:
        self._active.discard(task)
        self._budget.release(lease)

    async def _execute(self, request: ReadRequest, lease: BudgetLease) -> ReadResult:
        started = monotonic()
        attempt = _Attempt(lease)
        result = await self._execute_counted(request, attempt)
        self._record(result, started, attempt)
        return result

    def _reuse_failed(self, attempt: _Attempt, failure: ReuseFailure) -> bool:
        """Count a failure of a kept connection; True when it was one."""
        if attempt.connection is not ConnectionKind.REUSED:
            return False
        self._reuse.failed(failure)
        return True

    async def _execute_counted(
        self, request: ReadRequest, attempt: _Attempt
    ) -> ReadResult:
        try:
            return await self._execute_admitted(request, attempt)
        except BudgetDeferred as error:
            self._count(deferred=1)
            return ReadResult(
                request,
                ReadState.DEFERRED,
                attempt.status,
                problem=ReadProblem.PAUSED,
                http_requests=attempt.headers,
                retry_after_seconds=error.seconds,
            )
        except LocalBudgetError as error:
            return ReadResult(
                request,
                ReadState.ACCESS_ERROR,
                attempt.status,
                problem=_budget_problem(error),
                http_requests=attempt.headers,
            )

    async def _execute_admitted(
        self, request: ReadRequest, attempt: _Attempt
    ) -> ReadResult:
        deadline = asyncio.get_running_loop().time() + self._limits.timeout
        try:
            async with asyncio.timeout_at(deadline):
                try:
                    result = await self._get(request, attempt)
                    if result.state is ReadState.SUCCESS:
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError
                        self._count(successes=1)
                    return result
                except LocalResourceError as error:
                    self._current()
                    return ReadResult(
                        request,
                        ReadState.PATH_ERROR,
                        attempt.status,
                        error.problem,
                        http_requests=attempt.headers,
                    )
        except transport.LocalTransportError as error:
            self._current()
            if error.failure in {
                TransportFailure.BODY_TOO_LARGE,
                TransportFailure.INVALID_RESPONSE,
            }:
                return ReadResult(
                    request,
                    ReadState.PATH_ERROR,
                    attempt.status,
                    error.failure,
                    http_requests=attempt.headers,
                )
            return await self._access_error(request, attempt, error.failure)
        except aiohttp.ServerFingerprintMismatch:
            self._current()
            return await self._access_error(
                request, attempt, TransportFailure.TLS_CHANGED
            )
        except aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError:
            self._current()
            return await self._access_error(request, attempt, TransportFailure.TLS)
        except TimeoutError, aiohttp.ServerTimeoutError:
            self._current()
            self._reuse_failed(attempt, ReuseFailure.TIMEOUT)
            if attempt.headers:
                return ReadResult(
                    request,
                    ReadState.PATH_ERROR,
                    attempt.status,
                    TransportFailure.TIMEOUT,
                    http_requests=attempt.headers,
                )
            return await self._access_error(request, attempt, TransportFailure.TIMEOUT)
        except aiohttp.ClientPayloadError:
            self._current()
            self._reuse_failed(attempt, ReuseFailure.INVALID_RESPONSE)
            return ReadResult(
                request,
                ReadState.PATH_ERROR,
                attempt.status,
                TransportFailure.INVALID_RESPONSE,
                http_requests=attempt.headers,
            )
        except aiohttp.ClientError, OSError, transport._RepeatBlocked:
            self._current()
            if (
                self._reuse_failed(attempt, ReuseFailure.CLOSED)
                and attempt.status is None
            ):
                # A kept connection the gateway closed or reset before any
                # answer: no sign of an unavailable gateway, so no pause and
                # no new certificate inspection. The read is not repeated
                # here; it is due again at once and opens a new connection.
                self._count(deferred=1)
                return ReadResult(
                    request,
                    ReadState.DEFERRED,
                    problem=ReadProblem.REUSED_CONNECTION_CLOSED,
                    http_requests=attempt.headers,
                    retry_after_seconds=MINIMUM_START_GAP,
                )
            return await self._access_error(request, attempt, TransportFailure.CONNECT)

    async def __aenter__(self) -> Self:
        self._current()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _close(self) -> None:
        tasks = tuple(self._active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.gather(
                *(
                    session.close()
                    for session in (self._session, self._fallback_session)
                    if session is not None
                )
            )
        finally:
            self._token = ""

    async def close(self) -> None:
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
            self._close_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        await asyncio.shield(self._close_task)
