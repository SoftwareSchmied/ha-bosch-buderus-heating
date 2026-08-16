"""Immutable public models used by the PointT client."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from time import time

from .exceptions import PointTError

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class Brand(StrEnum):
    """Supported PointT application brands."""

    BOSCH = "bosch"
    BUDERUS = "buderus"

    @property
    def redirect_uri(self) -> str:
        """Return the observed application redirect URI."""
        if self is Brand.BOSCH:
            return "com.bosch.tt.dashtt.pointt://app/login"
        return "com.buderus.tt.dashtt://app/login"

    @property
    def style_id(self) -> str:
        """Return the observed SingleKey ID style identifier."""
        if self is Brand.BOSCH:
            return "tt_bsch"
        return "tt_bud"


@dataclass(frozen=True, slots=True)
class AuthTokens:
    """OAuth tokens with an absolute access-token expiry timestamp."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float = 0.0
    token_type: str = "Bearer"
    scope: tuple[str, ...] = ()

    def is_expired(self, *, now: float | None = None, margin: float = 60.0) -> bool:
        """Return whether the access token is expired or near expiry."""
        current = time() if now is None else now
        return self.expires_at <= current + margin


@dataclass(frozen=True, slots=True)
class Gateway:
    """Normalized PointT gateway metadata."""

    gateway_id: str
    device_type: str | None = None
    model: str | None = None
    firmware_version: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceReference:
    """A reference from one resource node to another."""

    path: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceMetadata:
    """PointT metadata associated with one resource."""

    resource_type: str | None = None
    unit: str | None = None
    allowed_values: tuple[JsonScalar, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    writable: bool = False


@dataclass(frozen=True, slots=True)
class Resource:
    """A parsed PointT resource without unknown vendor fields."""

    path: str
    value: JsonValue = None
    has_value: bool = False
    values: tuple[JsonValue, ...] = ()
    metadata: ResourceMetadata = field(default_factory=ResourceMetadata)
    references: tuple[ResourceReference, ...] = ()


@dataclass(frozen=True, slots=True)
class BatchItemResult:
    """Result of one path inside a bulk response."""

    gateway_id: str
    path: str
    status: int | None
    resource: Resource | None = None
    error: PointTError | None = field(default=None, compare=False)

    @property
    def ok(self) -> bool:
        """Return whether this item contains a successful resource."""
        return self.resource is not None and self.error is None


@dataclass(frozen=True, slots=True)
class WriteRequest:
    """A validated scalar write against one discovered resource."""

    gateway_id: str
    path: str
    value: JsonScalar


@dataclass(frozen=True, slots=True)
class WriteResult:
    """A write confirmed by a subsequent resource read."""

    request: WriteRequest
    resource: Resource
    put_timed_out: bool = False
