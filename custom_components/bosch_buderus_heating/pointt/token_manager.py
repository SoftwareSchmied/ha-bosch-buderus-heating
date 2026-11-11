"""Serialized access-token refresh and persistence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from inspect import isawaitable
from time import monotonic, time
from typing import Protocol

from .exceptions import (
    PointTError,
    RateLimited,
    RefreshTokenRejected,
    SignInUnavailable,
    TokenEndpointRejection,
)
from .models import AuthTokens
from .transport import RateLimitBackoff

_LOGGER = logging.getLogger(__name__)

# Refresh while at least this much lifetime remains. Requests only check the
# token when they run, so a smaller margin was often met with 30-40 s left.
REFRESH_MARGIN_SECONDS = 120.0
# After a temporary refresh failure, wait this long before the next attempt;
# the wait doubles with every further failure up to the maximum. Requests in
# between fail at once instead of each starting another token request.
REFRESH_RETRY_INITIAL_SECONDS = 30.0
REFRESH_RETRY_MAXIMUM_SECONDS = 600.0


class OAuthTokenClient(Protocol):
    """Token refresh interface required by TokenManager."""

    async def refresh(self, refresh_token: str) -> AuthTokens:
        """Refresh and rotate a token set."""
        ...


PersistTokens = Callable[[AuthTokens], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class RefreshAttempt:
    """One finished refresh attempt, reported for diagnostics only.

    Token values are carried so that an observer can correlate them locally;
    they are excluded from the representation and must never be exported.
    """

    trigger: str
    outcome: str
    started_at: float
    duration: float
    access_token_remaining: float
    presented_refresh_token: str | None = field(repr=False)
    new_tokens: AuthTokens | None = field(default=None, repr=False)
    error: BaseException | None = field(default=None, repr=False)


RefreshObserver = Callable[[RefreshAttempt], None]


class TokenManager:
    """Own one rotating refresh token and serialize every refresh."""

    def __init__(
        self,
        oauth_client: OAuthTokenClient,
        tokens: AuthTokens,
        persist: PersistTokens,
        *,
        clock: Callable[[], float] = time,
        refresh_margin: float = REFRESH_MARGIN_SECONDS,
        backoff: RateLimitBackoff | None = None,
        observer: RefreshObserver | None = None,
        retry_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._oauth_client = oauth_client
        self._tokens = tokens
        self._persist = persist
        self._clock = clock
        self._refresh_margin = refresh_margin
        self._refresh_task: asyncio.Task[str] | None = None
        self._refresh_rejected = False
        self._rejection: TokenEndpointRejection | None = None
        self._backoff = backoff if backoff is not None else RateLimitBackoff()
        self._observer = observer
        self._retry_clock = retry_clock
        self._failures = 0
        self._retry_not_before = 0.0
        self._last_failure: SignInUnavailable | None = None

    def set_observer(self, observer: RefreshObserver | None) -> None:
        """Attach the diagnostics observer for subsequent refresh attempts."""
        self._observer = observer

    @property
    def refresh_rejected(self) -> bool:
        """Return whether this manager stopped after a rejected refresh."""
        return self._refresh_rejected

    @property
    def refresh_in_progress(self) -> bool:
        """Include a shielded refresh whose original waiter has stopped."""
        return self._refresh_task is not None and not self._refresh_task.done()

    async def async_wait_for_refresh(self) -> None:
        """Finish an existing rotation without starting or cancelling one."""
        if self._refresh_task is not None:
            await self._wait_for_refresh(self._refresh_task)

    @property
    def tokens(self) -> AuthTokens:
        """Return the current token set without logging its contents."""
        return self._tokens

    async def get_access_token(
        self, *, force_refresh: bool = False, rejected_token: str | None = None
    ) -> str:
        """Share one refresh, even if a caller stops waiting for its result."""
        if self._refresh_task is not None and not self._refresh_task.done():
            return await self._wait_for_refresh(self._refresh_task)
        if self._refresh_rejected:
            raise RefreshTokenRejected(rejection=self._rejection)
        already_refreshed = (
            rejected_token is not None and self._tokens.access_token != rejected_token
        )
        if (not force_refresh or already_refreshed) and not self._tokens.is_expired(
            now=self._clock(), margin=self._margin()
        ):
            return self._tokens.access_token
        self._backoff.raise_if_active()
        self._raise_if_retry_pending()
        # Keep a strong reference and retrieve errors even when all original
        # callers are cancelled. A rotating credential must still be saved.
        trigger = "access_token_rejected" if force_refresh else "expiry"
        self._refresh_task = asyncio.create_task(
            self._refresh(trigger), name="pointt_refresh"
        )
        self._refresh_task.add_done_callback(self._refresh_finished)
        return await self._wait_for_refresh(self._refresh_task)

    def _margin(self) -> float:
        """The refresh margin, at most half of a known token lifetime (D15).

        A lifetime shorter than the margin would make every new token count
        as expired at once, so each request would start another refresh.
        """
        issued_at = self._tokens.issued_at
        if issued_at is None:
            return self._refresh_margin
        lifetime = self._tokens.expires_at - issued_at
        return min(self._refresh_margin, max(0.0, lifetime / 2))

    def _raise_if_retry_pending(self) -> None:
        """Repeat a recent temporary failure without another token request."""
        last = self._last_failure
        if last is None:
            return
        remaining = self._retry_not_before - self._retry_clock()
        if remaining > 0:
            raise SignInUnavailable(
                last.reason, retry_after=remaining, rejection=last.rejection
            )

    def _temporary_failure(self, err: Exception) -> SignInUnavailable:
        """Start or extend the retry wait and describe the failure neutrally."""
        self._failures += 1
        delay = min(
            REFRESH_RETRY_MAXIMUM_SECONDS,
            REFRESH_RETRY_INITIAL_SECONDS * 2 ** min(self._failures - 1, 16),
        )
        self._retry_not_before = self._retry_clock() + delay
        failure = SignInUnavailable(
            _failure_reason(err),
            retry_after=delay,
            rejection=getattr(err, "rejection", None),
        )
        self._last_failure = failure
        return failure

    @staticmethod
    async def _wait_for_refresh(task: asyncio.Task[str]) -> str:
        # asyncio.wait leaves the shared task running if this caller is cancelled.
        # _refresh_finished owns errors when every caller has stopped waiting.
        await asyncio.wait((task,))
        return task.result()

    @staticmethod
    def _refresh_finished(task: asyncio.Task[str]) -> None:
        if not task.cancelled():
            task.exception()

    async def _refresh(self, trigger: str) -> str:
        refresh_token = self._tokens.refresh_token
        started_at = self._clock()
        started = monotonic()
        remaining = self._tokens.expires_at - started_at
        new_tokens: AuthTokens | None = None
        try:
            if not refresh_token:
                raise RefreshTokenRejected("No refresh token is available")
            new_tokens = await self._oauth_client.refresh(refresh_token)
            # The old refresh token may already be invalid. Retain the new one
            # in memory even if the persistence callback itself fails.
            self._tokens = new_tokens
            self._failures = 0
            self._last_failure = None
            self._report(
                trigger,
                "success",
                started_at,
                started,
                remaining,
                refresh_token,
                new_tokens=new_tokens,
            )
            persisted = self._persist(new_tokens)
            if isawaitable(persisted):
                await persisted
            return new_tokens.access_token
        except RefreshTokenRejected as err:
            self._refresh_rejected = True
            self._rejection = err.rejection
            self._report(
                trigger,
                "rejected",
                started_at,
                started,
                remaining,
                refresh_token,
                error=err,
            )
            raise
        except RateLimited as err:
            self._backoff.activate(err.retry_after)
            self._report(
                trigger,
                "rate_limited",
                started_at,
                started,
                remaining,
                refresh_token,
                error=err,
            )
            raise
        except asyncio.CancelledError:
            if new_tokens is None:
                self._report(
                    trigger,
                    "cancelled",
                    started_at,
                    started,
                    remaining,
                    refresh_token,
                )
            raise
        except Exception as err:
            if new_tokens is not None:
                # Saving failed; the new tokens are already in use.
                raise
            self._report(
                trigger,
                "failed",
                started_at,
                started,
                remaining,
                refresh_token,
                error=err,
            )
            if isinstance(err, PointTError):
                # Not an answer of the PointT service: no gateway or reading
                # failed, and an unreadable token answer is no invalid value.
                raise self._temporary_failure(err) from err
            # A programming error is no failure of the sign-in service: it
            # propagates unchanged and pauses no later sign-in.
            raise

    def _report(
        self,
        trigger: str,
        outcome: str,
        started_at: float,
        started: float,
        remaining: float,
        refresh_token: str | None,
        *,
        new_tokens: AuthTokens | None = None,
        error: BaseException | None = None,
    ) -> None:
        observer = self._observer
        if observer is None:
            return
        attempt = RefreshAttempt(
            trigger=trigger,
            outcome=outcome,
            started_at=started_at,
            duration=max(0.0, monotonic() - started),
            access_token_remaining=remaining,
            presented_refresh_token=refresh_token,
            new_tokens=new_tokens,
            error=error,
        )
        try:
            observer(attempt)
        except Exception:
            # Diagnostics must never change authentication behavior.
            _LOGGER.debug("Refresh diagnostics observer failed", exc_info=True)


def _failure_reason(err: Exception) -> str:
    """A short, fixed category for logs and diagnostics."""
    status = getattr(err, "status", None)
    if isinstance(status, int):
        return f"http_{status}"
    return {
        "RequestTimeout": "timeout",
        "ServiceUnavailable": "no_connection",
        "InvalidPayload": "invalid_answer",
    }.get(type(err).__name__, "unexpected_error")


class StaticTokenProvider:
    """Access-token provider for callers that manage refresh externally."""

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token.removeprefix("Bearer ").strip()
        if not self._access_token:
            raise ValueError("Access token must not be empty")

    async def get_access_token(
        self, *, force_refresh: bool = False, rejected_token: str | None = None
    ) -> str:
        """Return the configured token."""
        if force_refresh:
            raise RefreshTokenRejected("Static access token cannot be refreshed")
        return self._access_token
