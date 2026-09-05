from __future__ import annotations

import asyncio

import pytest

from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    RefreshTokenRejected,
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
