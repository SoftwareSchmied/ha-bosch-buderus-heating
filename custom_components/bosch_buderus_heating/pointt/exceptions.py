"""Stable exception hierarchy for the PointT client."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TokenEndpointRejection:
    """Sanitized reason returned by the OAuth token endpoint.

    The fields never contain tokens, codes or raw response bodies. The OAuth
    error code and its description are reduced to a short, allowlisted form.
    """

    http_status: int
    oauth_error: str | None = None
    oauth_error_description: str | None = None
    body_format: str = "empty"
    content_type: str | None = None

    def summary(self) -> str:
        """Return a compact, log-safe description."""
        parts = [f"HTTP {self.http_status}"]
        if self.oauth_error is not None:
            detail = self.oauth_error
            if self.oauth_error_description:
                detail = f"{detail}: {self.oauth_error_description}"
            parts.append(detail)
        else:
            parts.append(f"no OAuth error code, {self.body_format} body")
        return ", ".join(parts)


class PointTError(Exception):
    """Base class for client errors."""


class SourceStopped(PointTError):
    """The request belongs to a source that no longer accepts work."""

    def __init__(self) -> None:
        super().__init__("PointT source is no longer active")


class AuthenticationError(PointTError):
    """Base class for authentication failures."""


class AccessTokenRejected(AuthenticationError):
    """The API rejected the current access token."""


class RefreshTokenRejected(AuthenticationError):
    """The authorization server rejected the refresh token."""

    def __init__(
        self,
        message: str = "Refresh token was rejected",
        *,
        rejection: TokenEndpointRejection | None = None,
    ) -> None:
        self.rejection = rejection
        if rejection is not None:
            message = f"{message} ({rejection.summary()})"
        super().__init__(message)


class OAuthStateMismatch(AuthenticationError):
    """The OAuth redirect state did not match the current flow."""


class OAuthRedirectError(AuthenticationError):
    """The OAuth redirect was invalid or contained an authorization error."""


class OAuthFlowExpired(OAuthRedirectError):
    """The authorization attempt expired before its redirect was accepted."""


class OAuthScopeRejected(OAuthRedirectError):
    """The sign-in service rejected the requested scopes of this attempt.

    The flow has already switched to the full scope set and a fresh link; the
    user only needs to sign in again with that link.
    """


class AuthorizationCodeConsumed(AuthenticationError):
    """The authorization redirect was already consumed."""


class TransportError(PointTError):
    """Base class for HTTP and network failures."""


class RequestTimeout(TransportError):
    """A request exceeded its configured timeout."""


class ServiceUnavailable(TransportError):
    """The PointT service returned a temporary failure."""

    def __init__(self, status: int | None = None) -> None:
        self.status = status
        message = "PointT service is unavailable"
        if status is not None:
            message = f"PointT service is unavailable (HTTP {status})"
        super().__init__(message)


class SignInUnavailable(ServiceUnavailable):
    """The sign-in service could not renew the access token for now.

    Raised for temporary token endpoint failures: no answer, a server error,
    an unreadable answer or a refusal without a definite OAuth error. It is
    no answer of the PointT service itself and carries no HTTP status, so
    callers treat it like a missing connection. Until ``retry_after`` seconds
    have passed, the token manager repeats it without contacting the service.
    """

    def __init__(
        self,
        reason: str = "unavailable",
        *,
        retry_after: float | None = None,
        rejection: TokenEndpointRejection | None = None,
    ) -> None:
        super().__init__()
        self.reason = reason
        self.retry_after = retry_after
        self.rejection = rejection
        self.args = (f"Sign-in service is temporarily unavailable ({reason})",)


class RateLimited(TransportError):
    """The cloud rejected requests due to rate limiting."""

    def __init__(self, retry_after: float | None) -> None:
        self.retry_after = retry_after
        super().__init__("PointT request rate limited")


class UnexpectedHttpStatus(TransportError):
    """An otherwise unmapped HTTP status was returned."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Unexpected PointT HTTP status {status}")


class RefreshRefused(UnexpectedHttpStatus):
    """The token endpoint refused a refresh without a definite OAuth error.

    An HTML page of a firewall, an empty body or an unrelated error code is
    no reliable sign of an invalid sign-in. The refresh is retried later.
    """

    def __init__(self, status: int, *, rejection: TokenEndpointRejection) -> None:
        super().__init__(status)
        self.rejection = rejection
        self.args = (f"Token refresh was refused ({rejection.summary()})",)


class ResourceError(PointTError):
    """Base class for failures scoped to one resource path."""

    def __init__(self, path: str, status: int) -> None:
        self.path = path
        self.status = status
        super().__init__(f"Resource request failed with HTTP {status}")


class ResourceForbidden(ResourceError):
    """The resource is not allowed for this installation."""


class ResourceNotFound(ResourceError):
    """The resource does not exist for this installation."""


class ResourceNotAcceptable(ResourceError):
    """The resource or request form was not accepted."""


class ProtocolError(PointTError):
    """Base class for malformed server responses."""


class InvalidPayload(ProtocolError):
    """A response body did not match its required envelope."""


class ResourceOmitted(InvalidPayload):
    """A bulk response did not contain a requested path: no answer for it."""


class InvalidBatchEnvelope(ProtocolError):
    """A bulk response did not contain a readable gateway envelope."""


class WriteError(PointTError):
    """Base class for safe write-transaction failures."""


class WriteValidationError(WriteError):
    """A requested write did not match the current capability metadata."""


class WriteRejected(WriteError):
    """The PointT service rejected or returned an invalid write response."""


class WriteNotConfirmed(WriteError):
    """The requested value could not be confirmed by a read-back."""
