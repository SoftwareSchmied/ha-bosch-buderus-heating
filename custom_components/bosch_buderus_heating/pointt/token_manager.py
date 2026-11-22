"""Serialized access-token refresh and persistence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from inspect import isawaitable
from time import monotonic, time
from typing import Final, Protocol

from .const import TOKEN_TOTAL_TIMEOUT
from .exceptions import (
    InvalidPayload,
    PointTError,
    RateLimited,
    RefreshTokenRejected,
    SignInUnavailable,
    TokenEndpointRejection,
)
from .models import AuthTokens
from .transport import RateLimitBackoff

_LOGGER = logging.getLogger(__name__)

# Renew while at least this much lifetime remains. Requests only check the
# token when they run, so a small margin was often met with 30-40 s left. With
# this ample margin a renewal that fails or answers slowly (the sign-in service
# needed up to 29 s, and one request timed out) happens while the current
# access token still works: the request that finds the renewal due goes on
# with it and the renewal runs in the background (H-B3-1).
REFRESH_MARGIN_SECONDS = 600.0
# Below this lifetime the access token is no longer served while a renewal is
# pending: a request waits for the renewal (or fails with its error) as before.
USABLE_MARGIN_SECONDS = 30.0
# After a temporary refresh failure, wait this long before the next attempt;
# the wait doubles with every further failure up to the maximum. Requests in
# between fail at once instead of each starting another token request.
REFRESH_RETRY_INITIAL_SECONDS = 30.0
REFRESH_RETRY_MAXIMUM_SECONDS = 600.0
# X39: an early renewal waits longer for the answer, as the current access
# token still works meanwhile: the remaining lifetime less a reserve for one
# more try, never less than the usual token timeout and at most five minutes.
# Giving up on an answer that is merely late can lose the rotated token; a
# shorter wait would only make that more likely.
EARLY_REFRESH_TIMEOUT_MAXIMUM_SECONDS = 300.0
EARLY_REFRESH_TIMEOUT_RESERVE_SECONDS = 120.0
# X39: a request that never left (no connection) is safe to repeat soon.
NOT_SENT_RETRY_DELAY_SECONDS = 8.0
NOT_SENT_RETRIES = 2
# X39: after a lost answer, one more try with the same token shows whether
# the service had used it: an answer renews, ``invalid_grant`` means that the
# answer with the new token was lost and a new sign-in is needed.
ANSWER_LOST_RETRY_DELAY_SECONDS = 12.0
ANSWER_LOST_RETRIES = 1
# Fixed codes of a failed attempt without an answer (diagnostics, X39).
NOT_SENT: Final = "not_sent"
ANSWER_LOST: Final = "answer_lost"


class OAuthTokenClient(Protocol):
    """Token refresh interface required by TokenManager."""

    async def refresh(
        self, refresh_token: str, *, answer_timeout: float | None = None
    ) -> AuthTokens:
        """Refresh and rotate a token set; ``answer_timeout`` bounds the answer."""
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
    # X39: the wait for the answer, which try of this renewal it was (0 the
    # first), the kind of a failure without an answer (``not_sent`` or
    # ``answer_lost``), the delay of the immediate next try, and whether a
    # rejection followed a lost answer with the same refresh token.
    answer_timeout: float | None = None
    retry: int = 0
    failure_case: str | None = None
    retry_in: float | None = None
    after_lost_answer: bool = False


RefreshObserver = Callable[[RefreshAttempt], None]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class _RetryBudget:
    """The immediate retries left within one renewal (X39)."""

    not_sent: int = NOT_SENT_RETRIES
    answer_lost: int = ANSWER_LOST_RETRIES
    tries: int = 0

    def take(self, case: str | None) -> float | None:
        """The delay of the next try for this failure, or None for none."""
        if case == NOT_SENT and self.not_sent > 0:
            self.not_sent -= 1
            return NOT_SENT_RETRY_DELAY_SECONDS
        if case == ANSWER_LOST and self.answer_lost > 0:
            self.answer_lost -= 1
            return ANSWER_LOST_RETRY_DELAY_SECONDS
        return None


@dataclass(frozen=True, slots=True)
class _AttemptDetails:
    """The X39 fields of one reported attempt."""

    answer_timeout: float
    retry: int
    failure_case: str | None = None
    retry_in: float | None = None
    after_lost_answer: bool = False


class _RetrySoon(Exception):
    """Internal: try the same renewal again after ``delay`` seconds."""

    def __init__(self, error: PointTError, delay: float) -> None:
        super().__init__(type(error).__name__)
        self.error = error
        self.delay = delay


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
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._oauth_client = oauth_client
        self._tokens = tokens
        self._persist = persist
        self._clock = clock
        self._refresh_margin = refresh_margin
        self._refresh_task: asyncio.Task[str] | None = None
        # Whether the running renewal started early, while the access token
        # still worked: then requests do not wait for it.
        self._refresh_early = False
        self._refresh_rejected = False
        self._rejection: TokenEndpointRejection | None = None
        self._backoff = backoff if backoff is not None else RateLimitBackoff()
        self._observer = observer
        self._retry_clock = retry_clock
        self._failures = 0
        self._retry_not_before = 0.0
        self._last_failure: SignInUnavailable | None = None
        self._sleep = sleep
        # X39: the refresh token whose request may have reached the service
        # without an answer, and whether its rejection followed such a loss.
        self._answer_lost_token: str | None = None
        self._rejected_after_lost_answer = False

    def set_observer(self, observer: RefreshObserver | None) -> None:
        """Attach the diagnostics observer for subsequent refresh attempts."""
        self._observer = observer

    @property
    def refresh_rejected(self) -> bool:
        """Return whether this manager stopped after a rejected refresh."""
        return self._refresh_rejected

    @property
    def rejected_after_lost_answer(self) -> bool:
        """Whether the rejection followed a lost answer with the same token.

        Then the service most likely replaced the token in the answer that
        never arrived (X39); only a new sign-in helps.
        """
        return self._refresh_rejected and self._rejected_after_lost_answer

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
        in_flight = self._refresh_task is not None and not self._refresh_task.done()
        if (
            in_flight
            and self._refresh_early
            and self._current_token_usable(force_refresh)
        ):
            # An early renewal is running: the current access token still
            # works, so this request does not wait for the slow answer.
            return self._tokens.access_token
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
        early = self._current_token_usable(force_refresh)
        try:
            self._backoff.raise_if_active()
            self._raise_if_retry_pending()
        except RateLimited, SignInUnavailable:
            if early:
                # The pause holds the renewal back; the current access token
                # is served until the renewal may start again.
                return self._tokens.access_token
            raise
        # Keep a strong reference and retrieve errors even when all original
        # callers are cancelled. A rotating credential must still be saved.
        trigger = "access_token_rejected" if force_refresh else "expiry"
        self._refresh_early = early
        self._refresh_task = asyncio.create_task(
            self._refresh(trigger), name="pointt_refresh"
        )
        self._refresh_task.add_done_callback(self._refresh_finished)
        if early:
            # Not awaited: a failure is kept for the retry wait and the
            # diagnostics (``_refresh_once``), a rejection stops the next call.
            return self._tokens.access_token
        return await self._wait_for_refresh(self._refresh_task)

    def _current_token_usable(self, force_refresh: bool) -> bool:
        """Whether the access token can be used while a renewal is pending.

        Never after the API refused it (``force_refresh``), and not in the last
        seconds of its lifetime.
        """
        if force_refresh:
            return False
        return not self._tokens.is_expired(
            now=self._clock(), margin=min(USABLE_MARGIN_SECONDS, self._margin())
        )

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
        # While the access token still works, the next attempts come before it
        # ends, however often the renewal failed (H-B3-1): at most after half
        # of the time it can still be used. Never shorter than the first wait,
        # so the sign-in service is not asked in a tight loop.
        usable_for = self._tokens.expires_at - self._clock() - USABLE_MARGIN_SECONDS
        if usable_for > 0:
            delay = min(delay, max(REFRESH_RETRY_INITIAL_SECONDS, usable_for / 2))
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
        """Refresh; an access token expired on arrival is refreshed once more.

        D15: such an answer may rotate the refresh token, so the new token
        set is kept and saved. The next refresh uses it right away, once; if
        that answer is expired as well, the attempt counts as a temporary
        failure and the usual retry wait (30 s doubling up to 10 min) holds
        every further refresh. There is never a tight refresh loop.
        """
        tokens = await self._refresh_with_retries(trigger)
        if not _expired_on_arrival(tokens):
            return tokens.access_token
        self._backoff.raise_if_active()
        tokens = await self._refresh_with_retries("expired_on_arrival")
        if not _expired_on_arrival(tokens):
            return tokens.access_token
        raise self._temporary_failure(
            InvalidPayload("OAuth access token was expired on arrival")
        )

    async def _refresh_with_retries(self, trigger: str) -> AuthTokens:
        """One renewal with its few immediate retries (X39).

        The retries run inside the one shared renewal task, so no request and
        no scheduled renewal starts a second token request meanwhile. Only
        after them does the usual retry wait begin.
        """
        budget = _RetryBudget()
        while True:
            try:
                return await self._refresh_once(trigger, budget)
            except _RetrySoon as retry:
                await self._sleep(retry.delay)
                budget.tries += 1

    def _answer_timeout(self) -> float:
        """How long this try waits for the answer of the sign-in service (X39).

        An early renewal, while the access token still works, waits up to the
        remaining usable time less a reserve for one more try (90 s to 5 min).
        Otherwise requests are waiting, so the usual 90 s apply.
        """
        if not (self._refresh_early and self._current_token_usable(False)):
            return TOKEN_TOTAL_TIMEOUT
        remaining = self._tokens.expires_at - self._clock()
        return min(
            EARLY_REFRESH_TIMEOUT_MAXIMUM_SECONDS,
            max(TOKEN_TOTAL_TIMEOUT, remaining - EARLY_REFRESH_TIMEOUT_RESERVE_SECONDS),
        )

    async def _refresh_once(self, trigger: str, budget: _RetryBudget) -> AuthTokens:
        refresh_token = self._tokens.refresh_token
        started_at = self._clock()
        started = monotonic()
        remaining = self._tokens.expires_at - started_at
        timeout = self._answer_timeout()
        new_tokens: AuthTokens | None = None
        details = _AttemptDetails(answer_timeout=timeout, retry=budget.tries)
        try:
            if not refresh_token:
                raise RefreshTokenRejected("No refresh token is available")
            new_tokens = await self._oauth_client.refresh(
                refresh_token, answer_timeout=timeout
            )
            # The old refresh token may already be invalid. Retain the new one
            # in memory even if the persistence callback itself fails.
            self._tokens = new_tokens
            self._answer_lost_token = None
            if not _expired_on_arrival(new_tokens):
                # An expired token ends no retry wait (D15).
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
                details=details,
            )
            persisted = self._persist(new_tokens)
            if isawaitable(persisted):
                await persisted
            return new_tokens
        except RefreshTokenRejected as err:
            self._refresh_rejected = True
            self._rejection = err.rejection
            self._rejected_after_lost_answer = (
                refresh_token is not None and refresh_token == self._answer_lost_token
            )
            self._report(
                trigger,
                "rejected",
                started_at,
                started,
                remaining,
                refresh_token,
                error=err,
                details=replace(
                    details, after_lost_answer=self._rejected_after_lost_answer
                ),
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
                details=details,
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
                    details=details,
                )
            raise
        except Exception as err:
            if new_tokens is not None:
                # Saving failed; the new tokens are already in use.
                raise
            case = _failure_case(err)
            retry_in = None
            if case is not None and self._backoff.remaining <= 0:
                retry_in = budget.take(case)
            # The first lost answer of a refresh token is a warning; repeats
            # with the same token, after the usual wait, are logged for debug.
            first_loss = (
                case == ANSWER_LOST and refresh_token != self._answer_lost_token
            )
            if case == ANSWER_LOST:
                self._answer_lost_token = refresh_token
            self._report(
                trigger,
                "failed",
                started_at,
                started,
                remaining,
                refresh_token,
                error=err,
                details=replace(details, failure_case=case, retry_in=retry_in),
            )
            if case == ANSWER_LOST:
                _log_lost_answer(
                    started_at, started, remaining, retry_in, warn=first_loss
                )
            elif case == NOT_SENT:
                _LOGGER.debug(
                    "The token renewal could not reach the sign-in service (%s); "
                    "the request was not sent. %s",
                    type(err).__name__,
                    f"Next try in {retry_in:.0f} s"
                    if retry_in is not None
                    else "The usual retry wait applies",
                )
            if isinstance(err, PointTError):
                if retry_in is not None:
                    raise _RetrySoon(err, retry_in) from err
                # Not an answer of the PointT service: no gateway or reading
                # failed, and an unreadable token answer is no invalid value.
                raise self._temporary_failure(err) from err
            if self._refresh_early:
                # Nobody awaits an early renewal, so nobody sees this error
                # and the next request would start another renewal at once:
                # the usual retry wait applies, and the cause is logged once
                # (the wait keeps it from repeating).
                self._temporary_failure(err)
                _LOGGER.warning(
                    "The early renewal of the access token failed unexpectedly "
                    "(%s); the next try waits",
                    type(err).__name__,
                )
            # A programming error is no failure of the sign-in service: it
            # propagates unchanged to a caller that waits for it.
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
        details: _AttemptDetails,
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
            answer_timeout=details.answer_timeout,
            retry=details.retry,
            failure_case=details.failure_case,
            retry_in=details.retry_in,
            after_lost_answer=details.after_lost_answer,
        )
        try:
            observer(attempt)
        except Exception:
            # Diagnostics must never change authentication behavior.
            _LOGGER.debug("Refresh diagnostics observer failed", exc_info=True)


def _failure_case(err: BaseException) -> str | None:
    """X39: a failure without an answer, by whether the request was sent."""
    sent = getattr(err, "request_sent", None)
    if sent is False:
        return NOT_SENT
    if sent is True:
        return ANSWER_LOST
    return None


def _log_lost_answer(
    started_at: float,
    started: float,
    remaining: float,
    retry_in: float | None,
    *,
    warn: bool,
) -> None:
    """X39: say at once that an answer of the sign-in service was lost.

    Never with a token: only the time, the wait and the remaining lifetime.
    """
    waited = max(0.0, monotonic() - started)
    _LOGGER.log(
        logging.WARNING if warn else logging.DEBUG,
        "The sign-in service did not answer the token renewal started at %s "
        "(no answer after %.0f s, access token valid for %.0f s more). The "
        "request may have reached it; if it already replaced the refresh token "
        "in the lost answer, signing in again is required. %s",
        datetime.fromtimestamp(started_at).astimezone().isoformat(timespec="seconds"),
        waited,
        max(0.0, remaining - waited),
        f"One more try with the same token follows in {retry_in:.0f} s"
        if retry_in is not None
        else "The next try follows after the usual wait",
    )


def _expired_on_arrival(tokens: AuthTokens) -> bool:
    """The answer gave the access token no lifetime (D15)."""
    return tokens.issued_at is not None and tokens.expires_at <= tokens.issued_at


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
