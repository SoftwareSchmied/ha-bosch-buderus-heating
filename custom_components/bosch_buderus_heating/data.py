"""Config-entry serialization helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_SCOPE,
    CONF_TOKEN_TYPE,
)
from .pointt import AuthTokens


def tokens_to_data(tokens: AuthTokens) -> dict[str, Any]:
    """Serialize tokens into JSON-compatible config-entry data."""
    return {
        CONF_ACCESS_TOKEN: tokens.access_token,
        CONF_REFRESH_TOKEN: tokens.refresh_token,
        CONF_EXPIRES_AT: tokens.expires_at,
        CONF_TOKEN_TYPE: tokens.token_type,
        CONF_SCOPE: list(tokens.scope),
    }


def tokens_from_data(data: Mapping[str, Any]) -> AuthTokens:
    """Restore and validate tokens from config-entry data."""
    access_token = data.get(CONF_ACCESS_TOKEN)
    refresh_token = data.get(CONF_REFRESH_TOKEN)
    expires_at = data.get(CONF_EXPIRES_AT)
    token_type = data.get(CONF_TOKEN_TYPE, "Bearer")
    scope = data.get(CONF_SCOPE, [])

    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Config entry does not contain an access token")
    if refresh_token is not None and not isinstance(refresh_token, str):
        raise ValueError("Config entry refresh token is invalid")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ValueError("Config entry token expiry is invalid")
    if not isinstance(token_type, str):
        raise ValueError("Config entry token type is invalid")
    if not isinstance(scope, list) or not all(isinstance(item, str) for item in scope):
        raise ValueError("Config entry token scope is invalid")

    return AuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=float(expires_at),
        token_type=token_type,
        scope=tuple(scope),
    )
