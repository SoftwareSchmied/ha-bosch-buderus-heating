"""Bounded, sanitized aiohttp transport for PointT requests."""

from __future__ import annotations

import asyncio
import json
import math
import random
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic

import aiohttp

from .const import (
    DEFAULT_CONCURRENCY,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_TOTAL_TIMEOUT,
    DEFAULT_USER_AGENT,
    MAX_RESPONSE_BYTES,
    POINTT_BASE_URL,
)
from .exceptions import (
    AccessTokenRejected,
    InvalidPayload,
    RateLimited,
    RequestTimeout,
    ResourceForbidden,
    ResourceNotAcceptable,
    ResourceNotFound,
    ServiceUnavailable,
    UnexpectedHttpStatus,
)
from .metrics import RequestMetrics, bulk_resource_count, request_category
from .models import JsonValue


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry settings for idempotent reads."""

    attempts: int = 2
    base_delay: float = 0.25
    maximum_delay: float = 2.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("Retry attempts must be positive")


class PointTTransport:
    """HTTP transport with bounded concurrency and no sensitive error bodies."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str = POINTT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        concurrency: int = DEFAULT_CONCURRENCY,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
        metrics: RequestMetrics | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("Transport concurrency must be positive")
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent
        self._semaphore = asyncio.Semaphore(concurrency)
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._random_value = random_value
        self._timeout = aiohttp.ClientTimeout(
            connect=DEFAULT_CONNECT_TIMEOUT, total=DEFAULT_TOTAL_TIMEOUT
        )
        self.metrics = metrics or RequestMetrics()

    async def request_json(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        json_body: JsonValue = None,
        retryable: bool = False,
        resource_path: str | None = None,
    ) -> JsonValue:
        """Perform one JSON request and optionally retry temporary read failures."""
        attempts = self._retry_policy.attempts if retryable else 1
        last_error: RequestTimeout | ServiceUnavailable | None = None
        for attempt in range(attempts):
            try:
                return await self._request_once(
                    method,
                    path,
                    access_token,
                    json_body=json_body,
                    resource_path=resource_path,
                )
            except (RequestTimeout, ServiceUnavailable) as err:
                last_error = err
                if attempt + 1 == attempts:
                    raise
                self.metrics.record_retry()
                delay = min(
                    self._retry_policy.base_delay * (2**attempt),
                    self._retry_policy.maximum_delay,
                )
                await self._sleep(delay + delay * self._random_value())
        if last_error is not None:  # pragma: no cover - loop always raises or returns
            raise last_error
        raise RuntimeError("PointT retry loop did not execute")  # pragma: no cover

    async def _request_once(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        json_body: JsonValue,
        resource_path: str | None,
    ) -> JsonValue:
        started = monotonic()
        status: int | None = None
        outcome = "success"
        try:
            url = f"{self._base_url}/{path.lstrip('/')}"
            headers = {
                "Accept": "application/json",
                "Accept-Charset": "UTF-8",
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": self._user_agent,
            }
            try:
                async with (
                    self._semaphore,
                    self._session.request(
                        method,
                        url,
                        headers=headers,
                        json=json_body if json_body is not None else None,
                        timeout=self._timeout,
                    ) as response,
                ):
                    status = response.status
                    if response.status >= 400:
                        self._raise_http_error(
                            response.status, response.headers, resource_path
                        )
                    if response.status == 204:
                        return None
                    raw = await response.read()
            except TimeoutError as err:
                raise RequestTimeout("PointT request timed out") from err
            except aiohttp.ClientError as err:
                raise ServiceUnavailable() from err

            if len(raw) > MAX_RESPONSE_BYTES:
                raise InvalidPayload("PointT response exceeded the size limit")
            if not raw:
                return None
            try:
                parsed: object = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as err:
                raise InvalidPayload("PointT response was not valid JSON") from err
            return _validate_json(parsed)
        except AccessTokenRejected:
            outcome = "authentication_error"
            raise
        except RateLimited:
            outcome = "rate_limited"
            raise
        except RequestTimeout:
            outcome = "timeout"
            raise
        except ServiceUnavailable:
            outcome = "service_unavailable"
            raise
        except ResourceForbidden, ResourceNotFound, ResourceNotAcceptable:
            outcome = "resource_error"
            raise
        except InvalidPayload:
            outcome = "invalid_payload"
            raise
        except UnexpectedHttpStatus:
            outcome = "unexpected_http_status"
            raise
        except Exception:
            outcome = "internal_error"
            raise
        finally:
            self.metrics.record_request(
                category=request_category(path),
                method=method,
                status=status,
                outcome=outcome,
                duration_ms=(monotonic() - started) * 1000,
                bulk_size=(
                    bulk_resource_count(json_body)
                    if request_category(path) == "bulk"
                    else 0
                ),
            )

    @staticmethod
    def _raise_http_error(
        status: int, headers: Mapping[str, str], resource_path: str | None
    ) -> None:
        path = resource_path or "/"
        if status == 401:
            raise AccessTokenRejected("PointT access token was rejected")
        if status == 403:
            raise ResourceForbidden(path, status)
        if status == 404:
            raise ResourceNotFound(path, status)
        if status == 406:
            raise ResourceNotAcceptable(path, status)
        if status == 429:
            retry_after: float | None = None
            raw_retry = headers.get("Retry-After")
            if raw_retry is not None:
                with suppress(ValueError):
                    retry_after = max(0.0, float(raw_retry))
            raise RateLimited(retry_after)
        if status >= 500:
            raise ServiceUnavailable(status)
        raise UnexpectedHttpStatus(status)


def _validate_json(value: object) -> JsonValue:
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidPayload("PointT response contained a non-finite number")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_validate_json(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {str(key): _validate_json(item) for key, item in value.items()}
    raise InvalidPayload("PointT response contained an unsupported JSON value")
