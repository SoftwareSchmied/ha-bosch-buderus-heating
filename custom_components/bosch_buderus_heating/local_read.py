"""Bounded local GETs sharing gateway admission, pacing and durable pauses."""

from __future__ import annotations

import asyncio
import math
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
from .local_probe import ProbeContext, TransportFailure
from .local_transport import CertificateBinding, PortRole, StaleLocalRequest

DEFAULT_FAILURE_PAUSE = 60.0


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


@dataclass(slots=True)
class _Attempt:
    lease: BudgetLease
    headers: int = 0
    status: int | None = None


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
        self._session: aiohttp.ClientSession | None = None
        self._active: set[asyncio.Task[ReadResult]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._halted: str | None = None
        self._metrics = ReadMetrics()

    @property
    def metrics(self) -> ReadMetrics:
        return self._metrics

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
        await self._budget.start_http(
            attempt.lease, lambda: not self._closed and self._is_current()
        )
        attempt.headers += 1
        self._count(http_requests=1)

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            trace = aiohttp.TraceConfig()
            trace.on_request_headers_sent.append(self._headers_sent)
            self._session = transport.create_local_session(
                trace_configs=[trace], maximum_connections=2
            )
        return self._session

    async def _access_error(
        self,
        request: ReadRequest,
        attempt: _Attempt,
        problem: str,
        delay: float | None = None,
    ) -> ReadResult:
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

    async def _get(self, request: ReadRequest, attempt: _Attempt) -> ReadResult:
        self._count(tls_inspections=1)
        peer = await transport._inspect(
            self._context,
            PortRole.RESOURCES,
            lambda: not self._closed and self._is_current(),
            self._binding.sha256,
            budget=self._budget,
            lease=attempt.lease,
        )
        self._current()
        origin = URL.build(scheme="https", host=peer.peer, port=PortRole.RESOURCES.port)
        url = URL(str(origin) + request.target, encoded=True)
        async with self._get_session().get(
            url,
            ssl=aiohttp.Fingerprint(self._binding.sha256),
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
            attempt.status = response.status
            self._count(http_responses=1)
            delay = (
                transport._retry_after(response.headers.get("Retry-After"))
                if response.status in {429, 503}
                else None
            )
            if response.status != 200:
                response.close()
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
        except LocalBudgetError:
            return ReadResult(
                request, ReadState.ACCESS_ERROR, problem="local_budget_storage_failed"
            )
        if lease is None:
            self._count(deferred=1)
            return ReadResult(
                request,
                ReadState.DEFERRED,
                problem=ReadProblem.BUSY if busy else ReadProblem.PAUSED,
                retry_after_seconds=max(
                    MINIMUM_START_GAP, self._budget.remaining_seconds
                ),
            )
        task = asyncio.create_task(self._execute(request, lease))
        self._active.add(task)
        task.add_done_callback(lambda done: self._finished(done, lease))
        return await task

    def _finished(self, task: asyncio.Task[ReadResult], lease: BudgetLease) -> None:
        self._active.discard(task)
        self._budget.release(lease)

    async def _execute(self, request: ReadRequest, lease: BudgetLease) -> ReadResult:
        attempt = _Attempt(lease)
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
        except LocalBudgetError:
            return ReadResult(
                request,
                ReadState.ACCESS_ERROR,
                attempt.status,
                problem="local_budget_storage_failed",
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
            return ReadResult(
                request,
                ReadState.PATH_ERROR,
                attempt.status,
                TransportFailure.INVALID_RESPONSE,
                http_requests=attempt.headers,
            )
        except aiohttp.ClientError, OSError, transport._RepeatBlocked:
            self._current()
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
            if self._session is not None:
                await self._session.close()
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
