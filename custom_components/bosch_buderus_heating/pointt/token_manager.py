"""Serialized access-token refresh and persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from time import time
from typing import Protocol

from .exceptions import RefreshTokenRejected
from .models import AuthTokens


class OAuthTokenClient(Protocol):
    """Token refresh interface required by TokenManager."""

    async def refresh(self, refresh_token: str) -> AuthTokens:
        """Refresh and rotate a token set."""
        ...


PersistTokens = Callable[[AuthTokens], Awaitable[None] | None]


class TokenManager:
    """Own one rotating refresh token and serialize every refresh."""

    def __init__(
        self,
        oauth_client: OAuthTokenClient,
        tokens: AuthTokens,
        persist: PersistTokens,
        *,
        clock: Callable[[], float] = time,
        refresh_margin: float = 60.0,
    ) -> None:
        self._oauth_client = oauth_client
        self._tokens = tokens
        self._persist = persist
        self._clock = clock
        self._refresh_margin = refresh_margin
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> AuthTokens:
        """Return the current token set without logging its contents."""
        return self._tokens

    async def get_access_token(
        self, *, force_refresh: bool = False, rejected_token: str | None = None
    ) -> str:
        """Return a valid token and refresh once under a lock when required."""
        if not force_refresh and not self._tokens.is_expired(
            now=self._clock(), margin=self._refresh_margin
        ):
            return self._tokens.access_token

        observed_tokens = self._tokens
        async with self._lock:
            already_refreshed = self._tokens is not observed_tokens or (
                rejected_token is not None
                and self._tokens.access_token != rejected_token
            )
            if (not force_refresh or already_refreshed) and not self._tokens.is_expired(
                now=self._clock(), margin=self._refresh_margin
            ):
                return self._tokens.access_token
            refresh_token = self._tokens.refresh_token
            if not refresh_token:
                raise RefreshTokenRejected("No refresh token is available")
            new_tokens = await self._oauth_client.refresh(refresh_token)
            self._tokens = new_tokens
            persisted = self._persist(new_tokens)
            if isawaitable(persisted):
                await persisted
            return new_tokens.access_token


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
