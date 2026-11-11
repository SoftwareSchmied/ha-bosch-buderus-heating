"""Bosch/Buderus SingleKey ID OAuth and PKCE helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic, time
from urllib.parse import ParseResult, parse_qs, urlencode, urlparse

import aiohttp

from .const import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_TOTAL_TIMEOUT,
    DEFAULT_USER_AGENT,
    OAUTH_AUTHORIZE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_SCOPES,
    OAUTH_SCOPES_REDUCED,
    OAUTH_TOKEN_URL,
)
from .exceptions import (
    AuthenticationError,
    AuthorizationCodeConsumed,
    InvalidPayload,
    OAuthFlowExpired,
    OAuthRedirectError,
    OAuthScopeRejected,
    OAuthStateMismatch,
    RateLimited,
    RefreshRefused,
    RefreshTokenRejected,
    RequestTimeout,
    ServiceUnavailable,
    TokenEndpointRejection,
    UnexpectedHttpStatus,
)
from .models import AuthTokens, Brand
from .retry_after import parse_retry_after


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


def build_authorization_url(
    brand: Brand,
    context: PKCEContext,
    *,
    scopes: tuple[str, ...] = OAUTH_SCOPES_REDUCED,
) -> str:
    """Build a brand-specific SingleKey ID authorization URL."""
    query = urlencode(
        {
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": brand.redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": context.state,
            "nonce": context.nonce,
            "code_challenge": context.code_challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            "style_id": brand.style_id,
        }
    )
    return f"{OAUTH_AUTHORIZE_URL}?{query}"


def _split_redirect(redirect_url: str) -> ParseResult:
    try:
        return urlparse(redirect_url.strip())
    except ValueError as err:
        # For example an unpaired "[" in the host part ("Invalid IPv6 URL").
        raise OAuthRedirectError("OAuth redirect URI could not be parsed") from err


def _redirect_parameters(brand: Brand, redirect_url: str) -> dict[str, list[str]]:
    parsed = _split_redirect(redirect_url)
    expected = urlparse(brand.redirect_uri)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.path != expected.path
    ):
        raise OAuthRedirectError("OAuth redirect URI did not match the selected brand")
    return parse_qs(parsed.query or parsed.fragment, keep_blank_values=True)


def parse_redirect_url(brand: Brand, redirect_url: str, *, expected_state: str) -> str:
    """Validate a full application redirect URL and return its code."""
    parameters = _redirect_parameters(brand, redirect_url)
    if "error" in parameters:
        raise OAuthRedirectError("Authorization server returned an OAuth error")
    received_state = parameters.get("state", [None])[0]
    if not secrets.compare_digest(received_state or "", expected_state):
        raise OAuthStateMismatch("OAuth redirect state did not match")
    code = parameters.get("code", [None])[0]
    if not code:
        raise OAuthRedirectError("OAuth redirect did not contain a code")
    return code


class _ScopeFallback:
    """Process-wide memory that the sign-in service rejected the reduced set.

    The reduced set is tried first. After one ``invalid_scope`` answer every
    later attempt of this process asks for the full set at once.
    """

    active = False


def initial_scopes() -> tuple[str, ...]:
    """Return the scope set a new authorization attempt requests."""
    return OAUTH_SCOPES if _ScopeFallback.active else OAUTH_SCOPES_REDUCED


@dataclass(slots=True)
class OAuthFlow:
    """One short-lived authorization flow whose redirect is consumed once."""

    brand: Brand
    context: PKCEContext
    max_age: float = 600.0
    scopes: tuple[str, ...] = field(default_factory=initial_scopes)
    _consumed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def create(cls, brand: Brand, *, now: float | None = None) -> OAuthFlow:
        """Create a fresh flow for one brand."""
        return cls(brand=brand, context=create_pkce_context(now=now))

    @property
    def authorization_url(self) -> str:
        """Return the URL the user should open."""
        return build_authorization_url(self.brand, self.context, scopes=self.scopes)

    def consume_redirect(
        self, redirect_url: str, *, now: float | None = None
    ) -> AuthorizationGrant:
        """Validate and consume one redirect URL."""
        if self._consumed:
            raise AuthorizationCodeConsumed("OAuth redirect was already consumed")
        if self.is_expired(now=now):
            raise OAuthFlowExpired("OAuth flow expired")
        self._fall_back_on_scope_rejection(redirect_url, now=now)
        code = parse_redirect_url(
            self.brand, redirect_url, expected_state=self.context.state
        )
        self._consumed = True
        return AuthorizationGrant(code=code, code_verifier=self.context.code_verifier)

    def _fall_back_on_scope_rejection(
        self, redirect_url: str, *, now: float | None
    ) -> None:
        """Retry once with the full set when the reduced set was rejected.

        Only an ``invalid_scope`` answer of this very attempt (matching state)
        switches. The flow then shows a fresh link with the full set; later
        attempts of this process start with it.
        """
        if self.scopes == OAUTH_SCOPES:
            return
        parameters = _redirect_parameters(self.brand, redirect_url)
        state = parameters.get("state", [None])[0]
        if parameters.get("error", [None])[0] != "invalid_scope" or (
            not secrets.compare_digest(state or "", self.context.state)
        ):
            return
        _ScopeFallback.active = True
        self.scopes = OAUTH_SCOPES
        self.context = create_pkce_context(now=now)
        raise OAuthScopeRejected("Sign-in service rejected the reduced scope set")

    def is_expired(self, *, now: float | None = None) -> bool:
        """Return whether this attempt needs a new authorization link."""
        current = monotonic() if now is None else now
        return current - self.context.created_at > self.max_age


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
                # A redirect would resend the refresh token or the code
                # verifier to the redirect target, possibly another host.
                allow_redirects=False,
            ) as response:
                if response.status in {400, 401, 403}:
                    if refresh_request:
                        rejection = await _read_rejection(response)
                        if rejection.oauth_error in _DEFINITE_REFRESH_REJECTIONS:
                            raise RefreshTokenRejected(rejection=rejection)
                        raise RefreshRefused(response.status, rejection=rejection)
                    raise AuthenticationError("Authorization code was rejected")
                if response.status == 429:
                    raise RateLimited(
                        parse_retry_after(
                            response.headers.get("Retry-After"), now=self._clock()
                        )
                    )
                if response.status >= 500:
                    raise ServiceUnavailable(response.status)
                if not 200 <= response.status < 300:
                    raise UnexpectedHttpStatus(response.status)
                raw = await _read_bounded(response, _MAX_TOKEN_RESPONSE)
        except TimeoutError as err:
            raise RequestTimeout("OAuth token request timed out") from err
        except aiohttp.ClientError as err:
            raise ServiceUnavailable() from err
        if raw is None:
            raise InvalidPayload("OAuth token response exceeded the size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as err:
            raise InvalidPayload("OAuth token response was not valid JSON") from err

        if not isinstance(payload, Mapping):
            raise InvalidPayload("OAuth token response must be an object")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise InvalidPayload("OAuth token response did not contain an access token")
        refresh_token = payload.get("refresh_token", fallback_refresh_token)
        if "refresh_token" in payload and (
            not isinstance(refresh_token, str) or not refresh_token.strip()
        ):
            raise InvalidPayload("OAuth refresh token must be a non-empty string")
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            raise InvalidPayload("OAuth token response did not contain expires_in")
        if not math.isfinite(float(expires_in)):
            raise InvalidPayload("OAuth token expiry must be finite")
        if float(expires_in) <= 0:
            # An already expired token would be refreshed before every
            # request (D15).
            raise InvalidPayload("OAuth token expiry must be positive")
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
        issued_at = self._clock()
        return AuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=issued_at + float(expires_in),
            token_type=token_type,
            scope=scope,
            issued_at=issued_at,
            refresh_expires_in=_optional_duration(payload.get("refresh_expires_in")),
            refresh_token_returned="refresh_token" in payload,
        )


_MAX_REJECTION_BODY = 4096
_MAX_TOKEN_RESPONSE = 64 * 1024
# Only these OAuth answers say that the refresh token itself is unusable. Any
# other refusal (an HTML page, an empty body, another code) is retried later.
_DEFINITE_REFRESH_REJECTIONS = frozenset(
    {"invalid_grant", "invalid_client", "unauthorized_client", "unsupported_grant_type"}
)
_OAUTH_ERROR = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_CONTENT_TYPE = re.compile(r"[a-z0-9.+-]{1,32}/[a-z0-9.+-]{1,64}")
# Long opaque runs could be tokens, codes or identifiers; never keep them.
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9_\-+/=.]{24,}")
_DESCRIPTION_DISALLOWED = re.compile(r"[^A-Za-z0-9 .,:;()'\[\]_-]")


async def _read_rejection(response: aiohttp.ClientResponse) -> TokenEndpointRejection:
    """Summarize a rejected token response without retaining its body."""
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
    content_type = content_type.strip().lower()
    safe_type = content_type if _CONTENT_TYPE.fullmatch(content_type) else None
    if content_type and safe_type is None:
        safe_type = "other"
    try:
        raw = await _read_bounded(response, _MAX_REJECTION_BODY, truncate=True)
    except aiohttp.ClientError, TimeoutError:
        return TokenEndpointRejection(
            response.status, body_format="unreadable", content_type=safe_type
        )
    if raw is None or not raw.strip():
        return TokenEndpointRejection(
            response.status, body_format="empty", content_type=safe_type
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        return TokenEndpointRejection(
            response.status, body_format="non_json", content_type=safe_type
        )
    if not isinstance(payload, Mapping):
        return TokenEndpointRejection(
            response.status, body_format="json_non_object", content_type=safe_type
        )
    error = payload.get("error")
    safe_error = None
    if isinstance(error, str):
        safe_error = error if _OAUTH_ERROR.fullmatch(error) else "other"
    return TokenEndpointRejection(
        response.status,
        oauth_error=safe_error,
        oauth_error_description=_safe_description(payload.get("error_description")),
        body_format="json",
        content_type=safe_type,
    )


async def _read_bounded(
    response: aiohttp.ClientResponse, limit: int, *, truncate: bool = False
) -> bytes | None:
    """Read until the end of the body, also across chunks, up to ``limit``.

    A single ``read(n)`` only returns what is buffered, so a body sent in
    several chunks would be cut short. Returns None when the body is longer
    than ``limit``, unless ``truncate`` keeps its first ``limit`` bytes.
    """
    body = bytearray()
    while chunk := await response.content.read(limit + 1 - len(body)):
        body.extend(chunk)
        if len(body) > limit:
            return bytes(body[:limit]) if truncate else None
    return bytes(body)


def _safe_description(value: object) -> str | None:
    """Keep a short human-readable reason; drop anything resembling a secret."""
    if not isinstance(value, str):
        return None
    text = _OPAQUE_RUN.sub("[redacted]", value)
    if "@" in text:
        # E-mail addresses or account names are not needed to classify the reason.
        text = re.sub(r"\S*@\S*", "[redacted]", text)
    text = _DESCRIPTION_DISALLOWED.sub(" ", text)
    text = " ".join(text.split())[:120].strip()
    return text or None


def _optional_duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    duration = float(value)
    return duration if math.isfinite(duration) and duration >= 0 else None
