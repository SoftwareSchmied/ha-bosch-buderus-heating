from __future__ import annotations

import asyncio

import pytest

from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    RateLimited,
    RefreshTokenRejected,
    RequestTimeout,
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
    with pytest.raises(type(error)):
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
