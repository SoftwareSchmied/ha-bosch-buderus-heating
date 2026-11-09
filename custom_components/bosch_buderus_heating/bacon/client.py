"""Bounded, authenticated read access to the Bacon claiming service."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from time import monotonic, time
from typing import Protocol

import aiohttp

from ..pointt.exceptions import AuthenticationError, PointTError, RateLimited
from ..pointt.retry_after import (
    MAX_CLOUD_PAUSE_SECONDS,
    MIN_CLOUD_PAUSE_SECONDS,
    parse_retry_after,
)
from ..pointt.transport import RateLimitBackoff
from .models import (
    CLAIM_URL,
    MAX_BYTES,
    USER_AGENT,
    BaconError,
    account_key,
    device_list,
    token_subject,
)


class TokenProvider(Protocol):
    async def get_access_token(
        self, *, force_refresh: bool = False, rejected_token: str | None = None
    ) -> str: ...


class BaconClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: TokenProvider,
        *,
        url: str = CLAIM_URL,
        clock: Callable[[], float] = monotonic,
        request_guard: Callable[[], None] = lambda: None,
        backoff: RateLimitBackoff | None = None,
        request_semaphore: asyncio.Semaphore | None = None,
        expected_account: str | None = None,
    ) -> None:
        self.session, self.tokens, self.url, self.clock = session, tokens, url, clock
        self.retry_at = 0.0
        self.subject: str | None = None
        self._guard = request_guard
        self.backoff = backoff
        self._semaphore = request_semaphore or asyncio.Semaphore(1)
        self.expected_account = expected_account

    def check_request(self) -> None:
        self._guard()
        if self.backoff is not None:
            try:
                self.backoff.raise_if_active()
            except RateLimited as err:
                # C49: a local block is no new server signal. Its own category
                # keeps it from extending the shared pause.
                raise BaconError("paused", err.retry_after or 60) from err

    async def access_token(self, *, rejected: str | None = None) -> str:
        """Share the existing serialized refresh and classify errors locally."""
        self.check_request()
        try:
            token = await self.tokens.get_access_token(
                force_refresh=rejected is not None, rejected_token=rejected
            )
        except AuthenticationError as err:
            raise BaconError("authentication") from err
        except RateLimited as err:
            delay = min(MAX_CLOUD_PAUSE_SECONDS, max(60, err.retry_after or 60))
            self.retry_at = self.clock() + delay
            raise BaconError("rate_limited", delay) from err
        except PointTError as err:
            raise BaconError("connection") from err
        self._guard()
        subject = token_subject(token)
        if (
            self.expected_account is not None
            and account_key(subject) != self.expected_account
        ):
            raise BaconError("account_changed")
        if self.subject is not None and self.subject != subject:
            raise BaconError("account_changed")
        self.subject = subject
        return token

    async def get_devices(self) -> tuple[str, ...]:
        self.check_request()
        try:
            result = await self._get_devices()
        except Exception:
            self._guard()
            raise
        self._guard()
        return result

    async def _get_devices(self) -> tuple[str, ...]:
        if self.clock() < self.retry_at:
            raise BaconError("paused", self.retry_at - self.clock())
        token = await self.access_token()
        for attempt in range(2):
            async with self._semaphore:
                self.check_request()
                if self.clock() < self.retry_at:
                    raise BaconError("paused", self.retry_at - self.clock())
                result = await self._request_devices(token, retry=attempt == 0)
            if result is not None:
                return result
            # Release the response and resource slot before serialized OAuth.
            token = await self.access_token(rejected=token)
        raise BaconError("authentication")

    async def _request_devices(
        self, token: str, *, retry: bool
    ) -> tuple[str, ...] | None:
        """Perform one bounded HTTP attempt; None requests one token refresh."""
        try:
            async with self.session.get(
                self.url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": USER_AGENT,
                },
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=False,
            ) as response:
                self._guard()
                if response.status == 401 and retry:
                    return None
                if response.status in (401, 403):
                    raise BaconError("authentication")
                if response.status == 429:
                    requested = parse_retry_after(
                        response.headers.get("Retry-After"), now=time()
                    )
                    delay = min(
                        MAX_CLOUD_PAUSE_SECONDS,
                        max(
                            MIN_CLOUD_PAUSE_SECONDS,
                            60 if requested is None else requested,
                        ),
                    )
                    self.retry_at = self.clock() + delay
                    if self.backoff is not None:
                        self.backoff.activate(delay)
                    raise BaconError("rate_limited", delay)
                if response.status != 200:
                    raise BaconError("service_unavailable")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise BaconError("invalid_device_list")
                return device_list(json.loads(body))
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BaconError("connection") from err
        except (ValueError, UnicodeError) as err:
            raise BaconError("invalid_device_list") from err
