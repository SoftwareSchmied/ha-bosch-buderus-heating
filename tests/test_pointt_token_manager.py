from __future__ import annotations

import asyncio
import contextlib

import pytest

from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    RateLimited,
    RefreshTokenRejected,
    RequestTimeout,
    ServiceUnavailable,
    SignInUnavailable,
    TokenEndpointRejection,
    TokenManager,
)
from custom_components.bosch_buderus_heating.pointt.token_manager import (
    StaticTokenProvider,
)


class FakeOAuthClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def refresh(self, refresh_token: str, **_: object) -> AuthTokens:
        self.calls.append(refresh_token)
        await asyncio.sleep(0)
        return AuthTokens("fresh", "rotated", expires_at=5000.0)


async def test_valid_token_is_returned_without_refresh() -> None:
    oauth = FakeOAuthClient()
    persisted: list[AuthTokens] = []
    manager = TokenManager(
        oauth,
        AuthTokens("current", "refresh", expires_at=2000.0),
        persisted.append,
        clock=lambda: 1000.0,
    )
    assert await manager.get_access_token() == "current"
    assert not oauth.calls
    assert not persisted


async def test_concurrent_expiry_refreshes_and_persists_once() -> None:
    oauth = FakeOAuthClient()
    persisted: list[AuthTokens] = []
    manager = TokenManager(
        oauth,
        AuthTokens("old", "refresh", expires_at=1000.0),
        persisted.append,
        clock=lambda: 1000.0,
    )
    results = await asyncio.gather(*(manager.get_access_token() for _ in range(10)))
    assert results == ["fresh"] * 10
    assert oauth.calls == ["refresh"]
    assert persisted == [manager.tokens]


async def test_async_persistence_and_forced_refresh() -> None:
    oauth = FakeOAuthClient()
    persisted: list[AuthTokens] = []

    async def persist(tokens: AuthTokens) -> None:
        persisted.append(tokens)

    manager = TokenManager(
        oauth,
        AuthTokens("current", "refresh", expires_at=5000.0),
        persist,
        clock=lambda: 1000.0,
    )
    assert await manager.get_access_token(force_refresh=True) == "fresh"
    assert persisted == [manager.tokens]


async def test_concurrent_forced_refreshes_share_one_rotation() -> None:
    oauth = FakeOAuthClient()
    manager = TokenManager(
        oauth,
        AuthTokens("old", "refresh", expires_at=5000),
        lambda tokens: None,
        clock=lambda: 1000,
    )
    results = await asyncio.gather(
        *(
            manager.get_access_token(force_refresh=True, rejected_token="old")
            for _ in range(5)
        )
    )
    assert results == ["fresh"] * 5
    assert oauth.calls == ["refresh"]
    # A late 401 still belongs to the old token, even after the first refresh ends.
    assert (
        await manager.get_access_token(force_refresh=True, rejected_token="old")
        == "fresh"
    )
    assert oauth.calls == ["refresh"]
    await manager.get_access_token(force_refresh=True, rejected_token="fresh")
    assert oauth.calls == ["refresh", "rotated"]


async def test_missing_refresh_token_is_rejected() -> None:
    manager = TokenManager(
        FakeOAuthClient(),
        AuthTokens("old", expires_at=0.0),
        lambda _tokens: None,
        clock=lambda: 1000.0,
    )
    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()


async def test_static_token_provider_normalizes_and_cannot_refresh() -> None:
    provider = StaticTokenProvider("Bearer token")
    assert await provider.get_access_token() == "token"
    with pytest.raises(RefreshTokenRejected):
        await provider.get_access_token(force_refresh=True)
    with pytest.raises(ValueError):
        StaticTokenProvider("  ")


async def test_observer_receives_successful_expiry_refresh() -> None:
    attempts = []
    manager = TokenManager(
        FakeOAuthClient(),
        AuthTokens("old", "refresh", expires_at=1030.0),
        lambda tokens: None,
        clock=lambda: 1000.0,
        observer=attempts.append,
    )
    assert await manager.get_access_token() == "fresh"
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.trigger == "expiry"
    assert attempt.outcome == "success"
    assert attempt.access_token_remaining == 30.0
    assert attempt.presented_refresh_token == "refresh"
    assert attempt.new_tokens == manager.tokens
    assert "refresh" not in repr(attempt)
    assert "rotated" not in repr(attempt)


async def test_observer_receives_rejection_and_retains_its_reason() -> None:
    rejection = TokenEndpointRejection(400, "invalid_grant", "Token is not active")

    class RejectingClient:
        calls = 0

        async def refresh(self, refresh_token: str, **_: object) -> AuthTokens:
            self.calls += 1
            raise RefreshTokenRejected(rejection=rejection)

    oauth = RejectingClient()
    attempts = []
    manager = TokenManager(
        oauth,
        AuthTokens("old", "refresh", expires_at=5000.0),
        lambda tokens: None,
        clock=lambda: 1000.0,
    )
    manager.set_observer(attempts.append)
    with pytest.raises(RefreshTokenRejected) as first:
        await manager.get_access_token(force_refresh=True, rejected_token="old")
    with pytest.raises(RefreshTokenRejected) as second:
        await manager.get_access_token()

    assert oauth.calls == 1
    assert manager.refresh_rejected is True
    assert first.value.rejection is rejection
    assert second.value.rejection is rejection
    assert "invalid_grant" in str(second.value)
    assert [(item.trigger, item.outcome) for item in attempts] == [
        ("access_token_rejected", "rejected")
    ]


@pytest.mark.parametrize(
    ("error", "outcome"),
    [(RateLimited(120), "rate_limited"), (RequestTimeout(), "failed")],
)
async def test_observer_receives_transient_failures(
    error: Exception, outcome: str
) -> None:
    class FailingClient:
        async def refresh(self, refresh_token: str, **_: object) -> AuthTokens:
            raise error

    attempts = []
    manager = TokenManager(
        FailingClient(),
        AuthTokens("old", "refresh", expires_at=0.0),
        lambda tokens: None,
        clock=lambda: 1000.0,
        observer=attempts.append,
    )
    # A temporary failure is reported as such, not as a PointT answer.
    raised = RateLimited if isinstance(error, RateLimited) else SignInUnavailable
    with pytest.raises(raised):
        await manager.get_access_token()
    assert [item.outcome for item in attempts] == [outcome]
    assert attempts[0].error is error
    assert manager.refresh_rejected is False


async def test_observer_reports_cancelled_refresh() -> None:
    entered = asyncio.Event()

    class HangingClient:
        async def refresh(self, refresh_token: str, **_: object) -> AuthTokens:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    attempts = []
    manager = TokenManager(
        HangingClient(),
        AuthTokens("old", "refresh", expires_at=0.0),
        lambda tokens: None,
        observer=attempts.append,
    )
    waiter = asyncio.create_task(manager.get_access_token())
    await entered.wait()
    assert manager._refresh_task is not None
    manager._refresh_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert [item.outcome for item in attempts] == ["cancelled"]


async def test_failing_observer_does_not_change_refresh_result() -> None:
    def broken_observer(_attempt: object) -> None:
        raise RuntimeError("diagnostics failure")

    persisted: list[AuthTokens] = []
    manager = TokenManager(
        FakeOAuthClient(),
        AuthTokens("old", "refresh", expires_at=0.0),
        persisted.append,
        observer=broken_observer,
    )
    assert await manager.get_access_token() == "fresh"
    assert persisted == [manager.tokens]


async def test_missing_refresh_token_is_reported_as_rejection() -> None:
    attempts = []
    manager = TokenManager(
        FakeOAuthClient(),
        AuthTokens("old", None, expires_at=0.0),
        lambda tokens: None,
        observer=attempts.append,
    )
    with pytest.raises(RefreshTokenRejected, match="No refresh token"):
        await manager.get_access_token()
    assert attempts[0].outcome == "rejected"
    assert attempts[0].presented_refresh_token is None


@pytest.mark.parametrize(("remaining", "refreshed"), [(601.0, False), (599.0, True)])
async def test_access_token_is_renewed_with_ten_minutes_left(
    remaining: float, refreshed: bool
) -> None:
    """H-B3-1: renew while 600 s remain; the request goes on with the old token."""
    oauth = FakeOAuthClient()
    manager = TokenManager(
        oauth,
        AuthTokens("current", "refresh", expires_at=1000.0 + remaining),
        lambda tokens: None,
        clock=lambda: 1000.0,
    )
    # The early renewal does not hold the request back.
    assert await manager.get_access_token() == "current"
    await manager.async_wait_for_refresh()
    assert manager.tokens.access_token == ("fresh" if refreshed else "current")
    assert oauth.calls == (["refresh"] if refreshed else [])
    assert await manager.get_access_token() == ("fresh" if refreshed else "current")


# --- C15/AUTH-2: a pause after a temporary refresh failure ---


class _FlakyClient:
    """Fails the given number of times, then issues fresh tokens."""

    def __init__(self, failures: int, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or ServiceUnavailable(503)
        self.calls = 0

    async def refresh(self, refresh_token: str, **_: object) -> AuthTokens:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return AuthTokens(f"fresh-{self.calls}", "rotated", expires_at=5000.0)


def _paused_manager(client, clock: list[float]) -> TokenManager:
    return TokenManager(
        client,
        AuthTokens("old", "refresh", expires_at=0.0),
        lambda tokens: None,
        clock=lambda: 1000.0,
        retry_clock=lambda: clock[0],
    )


async def test_temporary_refresh_failure_pauses_further_token_requests() -> None:
    client = _FlakyClient(failures=10)
    clock = [0.0]
    manager = _paused_manager(client, clock)
    with pytest.raises(SignInUnavailable) as caught:
        await manager.get_access_token()
    first = caught.value
    assert isinstance(first.__cause__, ServiceUnavailable)
    assert first.reason == "http_503" and first.retry_after == 30.0
    # No HTTP status: callers treat it as no answer, not as a PointT 503.
    assert first.status is None
    assert client.calls == 1
    # Every request inside the pause fails at once, without a token request,
    # also a forced refresh after a 401 of the API.
    for moment in (1.0, 10.0, 29.9):
        clock[0] = moment
        with pytest.raises(SignInUnavailable) as caught:
            await manager.get_access_token(force_refresh=True, rejected_token="old")
        assert caught.value.reason == "http_503"
        assert caught.value.retry_after == pytest.approx(30.0 - moment)
    assert client.calls == 1
    # The wait doubles after each further failure, up to ten minutes.
    clock[0] = 30.0
    waits = []
    for _ in range(6):
        with pytest.raises(SignInUnavailable) as caught:
            await manager.get_access_token()
        waits.append(caught.value.retry_after)
        clock[0] += caught.value.retry_after
    assert waits == [60.0, 120.0, 240.0, 480.0, 600.0, 600.0]
    assert client.calls == 7
    assert manager.refresh_rejected is False


async def test_successful_refresh_ends_the_pause_and_resets_the_wait() -> None:
    client = _FlakyClient(failures=2, error=RequestTimeout())
    clock = [0.0]
    manager = _paused_manager(client, clock)
    with pytest.raises(SignInUnavailable) as caught:
        await manager.get_access_token()
    assert caught.value.reason == "timeout"
    clock[0] = 30.0
    with pytest.raises(SignInUnavailable) as caught:
        await manager.get_access_token()
    assert caught.value.retry_after == 60.0
    clock[0] = 90.0
    assert await manager.get_access_token() == "fresh-3"
    assert client.calls == 3
    # A later failure starts again with the shortest wait.
    client.failures, client.calls = 4, 3
    manager._tokens = AuthTokens("fresh-3", "rotated", expires_at=0.0)
    with pytest.raises(SignInUnavailable) as caught:
        await manager.get_access_token()
    assert caught.value.retry_after == 30.0


async def test_valid_token_is_served_during_a_pause() -> None:
    client = _FlakyClient(failures=1)
    clock = [0.0]
    manager = _paused_manager(client, clock)
    with pytest.raises(SignInUnavailable):
        await manager.get_access_token(force_refresh=True)
    manager._tokens = AuthTokens("still-valid", "refresh", expires_at=5000.0)
    assert await manager.get_access_token() == "still-valid"
    assert client.calls == 1


async def test_rate_limit_and_rejection_keep_their_own_handling() -> None:
    limited = _FlakyClient(failures=1, error=RateLimited(120))
    manager = _paused_manager(limited, [0.0])
    with pytest.raises(RateLimited):
        await manager.get_access_token()
    assert manager._last_failure is None

    rejected = _FlakyClient(failures=1, error=RefreshTokenRejected())
    manager = _paused_manager(rejected, [0.0])
    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()
    assert manager.refresh_rejected and manager._last_failure is None


async def test_unexpected_error_propagates_and_pauses_nothing() -> None:
    """D: a programming error is no failure of the sign-in service."""
    client = _FlakyClient(failures=1, error=RuntimeError("bug"))
    clock = [0.0]
    manager = _paused_manager(client, clock)
    with pytest.raises(RuntimeError, match="bug"):
        await manager.get_access_token()
    assert manager._last_failure is None
    assert manager._failures == 0
    # The next call tries again at once and succeeds.
    assert await manager.get_access_token() == "fresh-2"
    assert client.calls == 2
    # A failure of the service afterwards starts with the shortest wait.
    client.failures, client.error = 3, RequestTimeout()
    manager._tokens = AuthTokens("fresh-2", "rotated", expires_at=0.0)
    with pytest.raises(SignInUnavailable) as caught:
        await manager.get_access_token()
    assert caught.value.retry_after == 30.0


async def test_failed_saving_after_a_refresh_starts_no_pause() -> None:
    def failing_persist(tokens: AuthTokens) -> None:
        raise OSError("disk full")

    client = _FlakyClient(failures=0)
    manager = TokenManager(
        client,
        AuthTokens("old", "refresh", expires_at=0.0),
        failing_persist,
        clock=lambda: 1000.0,
    )
    with pytest.raises(OSError):
        await manager.get_access_token()
    assert manager.tokens.access_token == "fresh-1"
    assert manager._last_failure is None
    assert await manager.get_access_token() == "fresh-1"


class _ShortLivedOAuth:
    """Answers each refresh with a token valid for ``lifetime`` seconds."""

    def __init__(self, clock: list[float], lifetime: float) -> None:
        self.calls = 0
        self._clock = clock
        self._lifetime = lifetime

    async def refresh(self, refresh_token: str, **_: object) -> AuthTokens:
        self.calls += 1
        now = self._clock[0]
        return AuthTokens(
            f"access-{self.calls}",
            "rotated",
            expires_at=now + self._lifetime,
            issued_at=now,
        )


@pytest.mark.parametrize("lifetime", [1.0, 60.0, 119.0])
async def test_a_short_token_lifetime_never_refreshes_before_every_request(
    lifetime: float,
) -> None:
    """D15: below the 600 s margin, the margin shrinks to half the lifetime."""
    clock = [1000.0]
    oauth = _ShortLivedOAuth(clock, lifetime)
    manager = TokenManager(
        oauth,
        AuthTokens("old", "refresh", expires_at=0.0),
        lambda _tokens: None,
        clock=lambda: clock[0],
    )
    assert await manager.get_access_token() == "access-1"
    for _ in range(5):
        assert await manager.get_access_token() == "access-1"
    assert oauth.calls == 1
    # Within the second half of the lifetime the token is renewed once.
    clock[0] += lifetime / 2
    await manager.get_access_token()
    await manager.async_wait_for_refresh()
    assert await manager.get_access_token() == "access-2"
    assert await manager.get_access_token() == "access-2"
    assert oauth.calls == 2


async def test_a_long_token_lifetime_keeps_the_full_margin() -> None:
    clock = [1000.0]
    oauth = _ShortLivedOAuth(clock, 3600.0)
    manager = TokenManager(
        oauth,
        AuthTokens("old", "refresh", expires_at=0.0),
        lambda _tokens: None,
        clock=lambda: clock[0],
    )
    assert await manager.get_access_token() == "access-1"
    clock[0] += 3600.0 - 601.0
    assert await manager.get_access_token() == "access-1"
    clock[0] += 2.0
    await manager.get_access_token()
    await manager.async_wait_for_refresh()
    assert await manager.get_access_token() == "access-2"


async def test_stored_tokens_without_issue_time_keep_the_full_margin() -> None:
    oauth = FakeOAuthClient()
    manager = TokenManager(
        oauth,
        AuthTokens("current", "refresh", expires_at=1500.0),
        lambda _tokens: None,
        clock=lambda: 1000.0,
    )
    await manager.get_access_token()
    await manager.async_wait_for_refresh()
    assert manager.tokens.access_token == "fresh"
    assert oauth.calls == ["refresh"]


class _ExpiringOAuth:
    """Rotates the refresh token on every answer; the first ``expired``
    answers give the access token no lifetime (D15)."""

    def __init__(self, clock: list[float], expired: int) -> None:
        self.presented: list[str] = []
        self._clock = clock
        self.expired = expired

    async def refresh(self, refresh_token: str, **_: object) -> AuthTokens:
        self.presented.append(refresh_token)
        count = len(self.presented)
        now = self._clock[0]
        lifetime = 0.0 if count <= self.expired else 3600.0
        return AuthTokens(
            f"access-{count}",
            f"rotated-{count}",
            expires_at=now + lifetime,
            issued_at=now,
        )


def _expiring_manager(
    oauth: _ExpiringOAuth, clock: list[float], persisted: list[AuthTokens]
) -> TokenManager:
    return TokenManager(
        oauth,
        AuthTokens("old", "refresh", expires_at=0.0),
        persisted.append,
        clock=lambda: clock[0],
        retry_clock=lambda: clock[0],
    )


async def test_an_expired_answer_keeps_the_rotation_and_refreshes_once_more() -> None:
    clock = [1000.0]
    oauth = _ExpiringOAuth(clock, expired=1)
    persisted: list[AuthTokens] = []
    manager = _expiring_manager(oauth, clock, persisted)
    assert await manager.get_access_token() == "access-2"
    # The second refresh presents the refresh token the first answer rotated.
    assert oauth.presented == ["refresh", "rotated-1"]
    assert [tokens.refresh_token for tokens in persisted] == ["rotated-1", "rotated-2"]
    assert manager.tokens.refresh_token == "rotated-2"
    assert await manager.get_access_token() == "access-2"
    assert len(oauth.presented) == 2


async def test_expired_answers_wait_like_every_temporary_failure() -> None:
    clock = [1000.0]
    oauth = _ExpiringOAuth(clock, expired=100)
    persisted: list[AuthTokens] = []
    manager = _expiring_manager(oauth, clock, persisted)
    with pytest.raises(SignInUnavailable) as caught:
        await manager.get_access_token()
    assert caught.value.reason == "invalid_answer"
    assert caught.value.retry_after == 30.0
    # Two answers, each rotation kept and saved; never a tight loop.
    assert oauth.presented == ["refresh", "rotated-1"]
    assert manager.tokens.refresh_token == "rotated-2"
    assert persisted[-1].refresh_token == "rotated-2"
    for moment in (1.0, 29.0):
        clock[0] = 1000.0 + moment
        with pytest.raises(SignInUnavailable):
            await manager.get_access_token()
    assert len(oauth.presented) == 2
    # After the wait, the next refresh uses the last rotated token; the wait
    # doubles as after any temporary failure.
    clock[0] = 1030.0
    with pytest.raises(SignInUnavailable) as caught:
        await manager.get_access_token()
    assert caught.value.retry_after == 60.0
    assert oauth.presented == ["refresh", "rotated-1", "rotated-2", "rotated-3"]
    assert manager.refresh_rejected is False
    # A token with a lifetime ends the wait.
    oauth.expired = 0
    clock[0] = 1090.0
    assert await manager.get_access_token() == "access-5"
    assert oauth.presented[-1] == "rotated-4"
    oauth.expired = 100
    manager._tokens = AuthTokens("access-5", "rotated-5", expires_at=0.0)
    with pytest.raises(SignInUnavailable) as caught:
        await manager.get_access_token()
    assert caught.value.retry_after == 30.0


async def test_an_expired_answer_respects_a_rate_limit_pause() -> None:
    clock = [1000.0]
    oauth = _ExpiringOAuth(clock, expired=1)
    manager = _expiring_manager(oauth, clock, [])
    original = oauth.refresh

    async def refresh_then_pause(refresh_token: str, **kwargs: object) -> AuthTokens:
        tokens = await original(refresh_token, **kwargs)
        manager._backoff.activate(60.0)
        return tokens

    oauth.refresh = refresh_then_pause  # type: ignore[method-assign]
    with pytest.raises(RateLimited):
        await manager.get_access_token()
    assert oauth.presented == ["refresh"]
    assert manager.tokens.refresh_token == "rotated-1"


# --- H-B3-1: an early renewal that fails does not fail the request ---


class _TimedClient:
    """The sign-in service on a simulated clock; fails while ``failing``."""

    def __init__(self, clock: list[float], error: Exception | None = None) -> None:
        self.clock = clock
        self.failing = True
        self.error = error or RequestTimeout()
        self.requests: list[float] = []

    async def refresh(self, refresh_token: str, **_: object) -> AuthTokens:
        self.requests.append(self.clock[0])
        await asyncio.sleep(0)
        if self.failing:
            raise self.error
        count = len(self.requests)
        return AuthTokens(
            f"access-{count}",
            f"rotated-{count}",
            expires_at=self.clock[0] + 3600.0,
            issued_at=self.clock[0],
        )


def _timed_manager(
    client: _TimedClient, clock: list[float], remaining: float
) -> TokenManager:
    return TokenManager(
        client,
        AuthTokens(
            "old",
            "refresh",
            expires_at=clock[0] + remaining,
            issued_at=clock[0] + remaining - 3600.0,
        ),
        lambda _tokens: None,
        clock=lambda: clock[0],
        retry_clock=lambda: clock[0],
    )


async def _settle(manager: TokenManager) -> None:
    with contextlib.suppress(SignInUnavailable):
        await manager.async_wait_for_refresh()


async def test_a_failed_early_renewal_serves_the_valid_token_and_paces_retries() -> (
    None
):
    clock = [1000.0]
    client = _TimedClient(clock)
    manager = _timed_manager(client, clock, remaining=590.0)
    # Due, but the old token works: the request goes on at once.
    assert await manager.get_access_token() == "old"
    await _settle(manager)
    assert client.requests == [1000.0]
    # Inside the wait after the failure no further token request is sent (the
    # refresh token is never presented twice in a row), and the token is served.
    for moment in (1001.0, 1010.0, 1029.9):
        clock[0] = moment
        assert await manager.get_access_token() == "old"
        await _settle(manager)
    assert client.requests == [1000.0]
    # After the wait one more attempt runs in the background.
    clock[0] = 1030.0
    assert await manager.get_access_token() == "old"
    await _settle(manager)
    assert client.requests == [1000.0, 1030.0]
    # The service answers: the next attempt renews, and the pause ends.
    client.failing = False
    clock[0] = 1100.0
    assert await manager.get_access_token() == "old"
    await _settle(manager)
    assert client.requests == [1000.0, 1030.0, 1100.0]
    assert await manager.get_access_token() == "access-3"
    assert manager.refresh_rejected is False


async def test_the_old_token_is_not_served_in_its_last_seconds() -> None:
    clock = [1000.0]
    client = _TimedClient(clock)
    manager = _timed_manager(client, clock, remaining=29.0)
    with pytest.raises(SignInUnavailable) as caught:
        await manager.get_access_token()
    assert caught.value.reason == "timeout"
    # The pause holds the request back at once, also while it is that short.
    clock[0] = 1005.0
    with pytest.raises(SignInUnavailable):
        await manager.get_access_token()
    assert client.requests == [1000.0]


async def test_a_token_the_api_refused_is_never_served_during_a_renewal() -> None:
    clock = [1000.0]
    client = _TimedClient(clock)
    manager = _timed_manager(client, clock, remaining=500.0)
    assert await manager.get_access_token() == "old"
    await _settle(manager)
    with pytest.raises(SignInUnavailable):
        await manager.get_access_token(force_refresh=True, rejected_token="old")
    clock[0] = 1040.0
    with pytest.raises(SignInUnavailable):
        await manager.get_access_token(force_refresh=True, rejected_token="old")
    assert client.requests == [1000.0, 1040.0]


async def test_a_rejected_early_renewal_stops_the_next_request() -> None:
    clock = [1000.0]
    client = _TimedClient(
        clock, error=RefreshTokenRejected(rejection=TokenEndpointRejection(400))
    )
    manager = _timed_manager(client, clock, remaining=500.0)
    assert await manager.get_access_token() == "old"
    await asyncio.sleep(0.01)
    assert manager.refresh_rejected is True
    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()


async def test_many_requests_share_one_early_renewal_without_waiting() -> None:
    """One request of the token endpoint, however many callers (the refresh
    token rotates); nobody waits for its slow answer while the token works."""
    clock = [1000.0]
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    class Slow:
        async def refresh(self, refresh_token: str, **_: object) -> AuthTokens:
            calls.append(refresh_token)
            entered.set()
            await release.wait()
            return AuthTokens(
                "new", "rotated", expires_at=clock[0] + 3600.0, issued_at=clock[0]
            )

    manager = TokenManager(
        Slow(),
        AuthTokens(
            "old", "refresh", expires_at=clock[0] + 400.0, issued_at=clock[0] - 3200.0
        ),
        lambda _tokens: None,
        clock=lambda: clock[0],
        retry_clock=lambda: clock[0],
    )
    first = await asyncio.wait_for(manager.get_access_token(), 1)
    await entered.wait()
    others = await asyncio.wait_for(
        asyncio.gather(*(manager.get_access_token() for _ in range(10))), 1
    )
    assert [first, *others] == ["old"] * 11
    assert manager.refresh_in_progress
    # The token runs out while the answer is awaited: callers wait for the
    # same renewal, none starts a second one.
    clock[0] += 390.0
    waiters = [asyncio.create_task(manager.get_access_token()) for _ in range(3)]
    await asyncio.sleep(0)
    assert not any(waiter.done() for waiter in waiters)
    release.set()
    assert await asyncio.gather(*waiters) == ["new"] * 3
    assert calls == ["refresh"]


async def test_the_retry_wait_never_ends_after_the_token_that_still_works() -> None:
    clock = [1000.0]
    client = _TimedClient(clock)
    manager = _timed_manager(client, clock, remaining=590.0)
    waits: list[float] = []
    for _ in range(8):
        await manager.get_access_token()
        await _settle(manager)
        waits.append(manager._retry_not_before - clock[0])
        # Attempts keep coming while the token works: 30 s at the least.
        assert waits[-1] >= 30.0
        clock[0] = manager._retry_not_before
        if manager.tokens.expires_at - clock[0] <= 30.0:
            break
    # Plain doubling would have waited 30, 60, 120, 240, ...: the waits shrink
    # with the lifetime left, so more attempts fall into the last minutes.
    assert waits[:3] == [30.0, 60.0, 120.0]
    assert waits[3] <= (590.0 - 210.0 - 30.0) / 2 + 0.01
    assert len(client.requests) >= 5


async def test_a_failed_early_renewal_leaves_the_refresh_token_for_the_next_try() -> (
    None
):
    """The old refresh token stays until an answer rotates it: the next
    attempt presents it again, and only after the wait."""
    clock = [1000.0]
    presented: list[str] = []

    class Recorder(_TimedClient):
        async def refresh(self, refresh_token: str, **_: object) -> AuthTokens:
            presented.append(refresh_token)
            return await super().refresh(refresh_token)

    client = Recorder(clock)
    manager = _timed_manager(client, clock, remaining=300.0)
    await manager.get_access_token()
    await _settle(manager)
    client.failing = False
    clock[0] += 31.0
    await manager.get_access_token()
    await _settle(manager)
    assert presented == ["refresh", "refresh"]
    assert manager.tokens.refresh_token == "rotated-2"


async def _settle_any(manager: TokenManager) -> None:
    with contextlib.suppress(Exception):
        await manager.async_wait_for_refresh()


async def test_an_unexpected_error_of_the_early_renewal_waits_before_the_next_try(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure that is no PointT error used to leave no wait behind: each of
    the next requests started another token request."""
    clock = [1000.0]
    client = _TimedClient(clock, error=RuntimeError("secret-detail"))
    manager = _timed_manager(client, clock, remaining=590.0)
    for _ in range(20):
        assert await manager.get_access_token() == "old"
        await _settle_any(manager)
        await asyncio.sleep(0)
    assert client.requests == [1000.0]
    # Logged once, with the kind of error only.
    warnings = [r for r in caplog.records if "early renewal" in r.getMessage()]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()
    assert "secret-detail" not in warnings[0].getMessage()
    # After the wait the next attempt runs, as for every other failure.
    clock[0] = 1030.0
    assert await manager.get_access_token() == "old"
    await _settle_any(manager)
    assert client.requests == [1000.0, 1030.0]


async def test_an_unexpected_error_of_a_waited_renewal_still_reaches_the_caller() -> (
    None
):
    """#49: a renewal somebody waits for fails as before and sets no wait."""
    clock = [1000.0]
    client = _TimedClient(clock, error=RuntimeError("secret-detail"))
    manager = _timed_manager(client, clock, remaining=-10.0)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await manager.get_access_token()
    assert client.requests == [1000.0, 1000.0]
