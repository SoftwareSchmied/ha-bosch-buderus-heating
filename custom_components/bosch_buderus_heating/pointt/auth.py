"""Bosch/Buderus SingleKey ID OAuth and PKCE helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic, time
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .const import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_TOTAL_TIMEOUT,
    DEFAULT_USER_AGENT,
    OAUTH_AUTHORIZE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_SCOPES,
    OAUTH_TOKEN_URL,
)
from .exceptions import (
    AuthenticationError,
    AuthorizationCodeConsumed,
    InvalidPayload,
    OAuthRedirectError,
    OAuthStateMismatch,
    RefreshTokenRejected,
    RequestTimeout,
    ServiceUnavailable,
    UnexpectedHttpStatus,
)
from .models import AuthTokens, Brand


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class PKCEContext:
    """Fresh OAuth request correlation and PKCE values."""

    code_verifier: str = field(repr=False)
    code_challenge: str
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    created_at: float


@dataclass(frozen=True, slots=True)
class AuthorizationGrant:
    """Validated authorization code and its one-time verifier."""

    code: str = field(repr=False)
    code_verifier: str = field(repr=False)


def create_pkce_context(*, now: float | None = None) -> PKCEContext:
    """Create fresh state, nonce, verifier, and S256 challenge."""
    verifier = _base64url(secrets.token_bytes(64))
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return PKCEContext(
        code_verifier=verifier,
        code_challenge=challenge,
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        created_at=monotonic() if now is None else now,
    )


def build_authorization_url(brand: Brand, context: PKCEContext) -> str:
    """Build a brand-specific SingleKey ID authorization URL."""
    query = urlencode(
        {
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": brand.redirect_uri,
            "response_type": "code",
            "scope": " ".join(OAUTH_SCOPES),
            "state": context.state,
            "nonce": context.nonce,
            "code_challenge": context.code_challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            "style_id": brand.style_id,
        }
    )
    return f"{OAUTH_AUTHORIZE_URL}?{query}"


def parse_redirect_url(brand: Brand, redirect_url: str, *, expected_state: str) -> str:
    """Validate a full application redirect URL and return its code."""
    parsed = urlparse(redirect_url.strip())
    expected = urlparse(brand.redirect_uri)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.path != expected.path
    ):
        raise OAuthRedirectError("OAuth redirect URI did not match the selected brand")

    parameters = parse_qs(parsed.query or parsed.fragment, keep_blank_values=True)
    if "error" in parameters:
        raise OAuthRedirectError("Authorization server returned an OAuth error")
    received_state = parameters.get("state", [None])[0]
    if not secrets.compare_digest(received_state or "", expected_state):
        raise OAuthStateMismatch("OAuth redirect state did not match")
    code = parameters.get("code", [None])[0]
    if not code:
        raise OAuthRedirectError("OAuth redirect did not contain a code")
    return code


@dataclass(slots=True)
class OAuthFlow:
    """One short-lived authorization flow whose redirect is consumed once."""

    brand: Brand
    context: PKCEContext
    max_age: float = 600.0
    _consumed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def create(cls, brand: Brand, *, now: float | None = None) -> OAuthFlow:
        """Create a fresh flow for one brand."""
        return cls(brand=brand, context=create_pkce_context(now=now))

    @property
    def authorization_url(self) -> str:
        """Return the URL the user should open."""
        return build_authorization_url(self.brand, self.context)

    def consume_redirect(
        self, redirect_url: str, *, now: float | None = None
    ) -> AuthorizationGrant:
        """Validate and consume one redirect URL."""
        if self._consumed:
            raise AuthorizationCodeConsumed("OAuth redirect was already consumed")
        current = monotonic() if now is None else now
        if current - self.context.created_at > self.max_age:
            raise OAuthRedirectError("OAuth flow expired")
        code = parse_redirect_url(
            self.brand, redirect_url, expected_state=self.context.state
        )
        self._consumed = True
        return AuthorizationGrant(code=code, code_verifier=self.context.code_verifier)


class OAuthClient:
    """Async OAuth token endpoint client using an injected aiohttp session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        token_url: str = OAUTH_TOKEN_URL,
        clock: Callable[[], float] = time,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._session = session
        self._token_url = token_url
        self._clock = clock
        self._user_agent = user_agent
        self._timeout = aiohttp.ClientTimeout(
            connect=DEFAULT_CONNECT_TIMEOUT, total=DEFAULT_TOTAL_TIMEOUT
        )

    async def exchange_code(
        self, brand: Brand, grant: AuthorizationGrant
    ) -> AuthTokens:
        """Exchange a validated authorization code for tokens."""
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": OAUTH_CLIENT_ID,
                "code": grant.code,
                "code_verifier": grant.code_verifier,
                "redirect_uri": brand.redirect_uri,
            },
            refresh_request=False,
            fallback_refresh_token=None,
        )

    async def refresh(self, refresh_token: str) -> AuthTokens:
        """Rotate a refresh token and return the new token set."""
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": OAUTH_CLIENT_ID,
                "refresh_token": refresh_token,
            },
            refresh_request=True,
            fallback_refresh_token=refresh_token,
        )

    async def _token_request(
        self,
        data: Mapping[str, str],
        *,
        refresh_request: bool,
        fallback_refresh_token: str | None,
    ) -> AuthTokens:
        headers = {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        try:
            async with self._session.post(
                self._token_url,
                data=data,
                headers=headers,
                timeout=self._timeout,
            ) as response:
                if response.status in {400, 401, 403}:
                    if refresh_request:
                        raise RefreshTokenRejected("Refresh token was rejected")
                    raise AuthenticationError("Authorization code was rejected")
                if response.status >= 500:
                    raise ServiceUnavailable(response.status)
                if response.status >= 400:
                    raise UnexpectedHttpStatus(response.status)
                payload = await response.json(content_type=None)
        except TimeoutError as err:
            raise RequestTimeout("OAuth token request timed out") from err
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            raise InvalidPayload("OAuth token response was not valid JSON") from err
        except aiohttp.ClientError as err:
            raise ServiceUnavailable() from err

        if not isinstance(payload, Mapping):
            raise InvalidPayload("OAuth token response must be an object")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise InvalidPayload("OAuth token response did not contain an access token")
        refresh_token = payload.get("refresh_token", fallback_refresh_token)
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise InvalidPayload("OAuth refresh token must be a string")
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            raise InvalidPayload("OAuth token response did not contain expires_in")
        if not math.isfinite(float(expires_in)):
            raise InvalidPayload("OAuth token expiry must be finite")
        token_type = payload.get("token_type", "Bearer")
        if not isinstance(token_type, str):
            raise InvalidPayload("OAuth token type must be a string")
        scope_value = payload.get("scope", "")
        if isinstance(scope_value, str):
            scope = tuple(scope_value.split())
        elif isinstance(scope_value, list) and all(
            isinstance(item, str) for item in scope_value
        ):
            scope = tuple(scope_value)
        else:
            raise InvalidPayload("OAuth scope must be a string or string array")
        return AuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=self._clock() + float(expires_in),
            token_type=token_type,
            scope=scope,
        )
