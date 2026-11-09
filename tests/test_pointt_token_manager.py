from __future__ import annotations

import asyncio

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

    async def refresh(self, refresh_token: str) -> AuthTokens:
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

        async def refresh(self, refresh_token: str) -> AuthTokens:
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
        async def refresh(self, refresh_token: str) -> AuthTokens:
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
        async def refresh(self, refresh_token: str) -> AuthTokens:
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


@pytest.mark.parametrize(("remaining", "refreshed"), [(121.0, False), (119.0, True)])
async def test_access_token_is_renewed_with_two_minutes_left(
    remaining: float, refreshed: bool
) -> None:
    """O94: renew while 120 s remain, not first with 30-40 s left."""
    oauth = FakeOAuthClient()
    manager = TokenManager(
        oauth,
        AuthTokens("current", "refresh", expires_at=1000.0 + remaining),
        lambda tokens: None,
        clock=lambda: 1000.0,
    )
    token = await manager.get_access_token()
    assert token == ("fresh" if refreshed else "current")
    assert oauth.calls == (["refresh"] if refreshed else [])


# --- C15/AUTH-2: a pause after a temporary refresh failure ---


class _FlakyClient:
    """Fails the given number of times, then issues fresh tokens."""

    def __init__(self, failures: int, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or ServiceUnavailable(503)
        self.calls = 0

    async def refresh(self, refresh_token: str) -> AuthTokens:
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


async def test_unexpected_error_is_kept_but_still_paused() -> None:
    client = _FlakyClient(failures=1, error=RuntimeError("bug"))
    clock = [0.0]
    manager = _paused_manager(client, clock)
    with pytest.raises(RuntimeError):
        await manager.get_access_token()
    with pytest.raises(SignInUnavailable) as caught:
        await manager.get_access_token()
    assert caught.value.reason == "unexpected_error"
    assert client.calls == 1


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
