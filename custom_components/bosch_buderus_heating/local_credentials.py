"""Local credential provenance and persisted evidence, never live authorization."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from .gateway_identity import GatewayIdentity, GatewayNamespace, IdentityContext
from .local_probe import LocalEndpoint

LOCAL_ACCESS_VERSION = 1
LOCAL_STORAGE_MINOR_VERSION = 2
MAX_TOKEN_LENGTH = 8192
MAX_SCOPE_LENGTH = 2048


class LocalCredentialError(ValueError):
    """Fixed error codes without credentials, certificate or endpoint text."""


class TokenOrigin(StrEnum):
    CREATED = "created"
    IMPORTED = "imported"


def validate_local_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_TOKEN_LENGTH
        or any(not 0x21 <= ord(c) <= 0x7E for c in value)
    ):
        raise LocalCredentialError("invalid_local_token")
    return value


def _scope(value: object) -> tuple[str, ...]:
    # The official token response reports an OAuth scope string. An imported
    # token's successful GET does not supply this issuer-reported metadata.
    if not isinstance(value, str) or not 0 < len(value) <= MAX_SCOPE_LENGTH:
        raise LocalCredentialError("invalid_reported_scope")
    result = tuple(value.split(" "))
    if len(result) > 64 or any(
        re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]+", scope) is None for scope in result
    ):
        raise LocalCredentialError("invalid_reported_scope")
    if len(set(result)) != len(result) or "open_api.read" not in result:
        raise LocalCredentialError("local_read_scope_missing")
    return result


@dataclass(frozen=True, slots=True)
class LocalCredentials:
    token: str = field(repr=False)
    origin: TokenOrigin
    reported_scope: tuple[str, ...] | None = field(repr=False)

    def __post_init__(self) -> None:
        validate_local_token(self.token)
        if not isinstance(self.origin, TokenOrigin):
            raise LocalCredentialError("invalid_token_origin")
        if self.origin == TokenOrigin.IMPORTED:
            if self.reported_scope is not None:
                raise LocalCredentialError("imported_scope_unproven")
        elif (
            not isinstance(self.reported_scope, tuple)
            or not all(isinstance(s, str) for s in self.reported_scope)
            or _scope(" ".join(self.reported_scope)) != self.reported_scope
        ):
            raise LocalCredentialError("invalid_reported_scope")

    def to_data(self) -> dict[str, object]:
        """Private storage only. No inferred scope or token-revocation ID."""
        return {
            "token": self.token,
            "token_type": "Bearer",
            "origin": self.origin.value,
            "reported_scope": list(self.reported_scope)
            if self.reported_scope
            else None,
        }


def import_local_token(token: object) -> LocalCredentials:
    return LocalCredentials(validate_local_token(token), TokenOrigin.IMPORTED, None)


def created_local_token(payload: object) -> LocalCredentials:
    """Parse a successful, bounded auth response supplied by the auth transport.

    Unknown fields, including an uncontracted token_id, do not establish token
    ownership. HTTP status, TLS and request provenance belong to that caller.
    """
    if not isinstance(payload, Mapping):
        raise LocalCredentialError("invalid_token_response")
    token_type = payload.get("token_type")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise LocalCredentialError("invalid_token_type")
    return LocalCredentials(
        validate_local_token(payload.get("access_token")),
        TokenOrigin.CREATED,
        _scope(payload.get("scope")),
    )


def _fingerprint(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise LocalCredentialError("invalid_certificate_binding")
    return value


def _timestamp(value: object) -> float:
    if type(value) not in (int, float):
        raise LocalCredentialError("invalid_verification_time")
    try:
        result = float(cast(int | float, value))
    except OverflowError:
        raise LocalCredentialError("invalid_verification_time") from None
    if not math.isfinite(result) or result < 0:
        raise LocalCredentialError("invalid_verification_time")
    return result


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LocalCredentialError("invalid_local_access")
    return value


@dataclass(frozen=True, slots=True)
class StoredLocalAccess:
    """Historical evidence only; restarting does not make a read fresh again."""

    credentials: LocalCredentials = field(repr=False)
    endpoint: LocalEndpoint = field(repr=False)
    resource_sha256: str = field(repr=False)
    authentication_sha256: str | None = field(repr=False)
    gateway_id: str = field(repr=False)
    verified_at: float = field(repr=False)

    def __post_init__(self) -> None:
        _fingerprint(self.resource_sha256)
        _fingerprint(self.authentication_sha256, optional=True)
        _timestamp(self.verified_at)
        # Reuse the exact gateway-ID validation without claiming a live context.
        GatewayIdentity(
            IdentityContext(GatewayNamespace.K40_LOCAL, "storage", "storage", 0),
            self.gateway_id,
        )
        if (
            self.credentials.origin == TokenOrigin.CREATED
            and self.authentication_sha256 is None
        ):
            raise LocalCredentialError("authentication_binding_required")

    def to_data(self) -> dict[str, object]:
        """Fields for a v4.2 connection, not an exported diagnostic record."""
        return {
            "credentials": self.credentials.to_data(),
            "endpoint": {
                "host": self.endpoint.host,
                "port": 9443,
                "transport": "https_local_api",
            },
            "local_access": {
                "version": LOCAL_ACCESS_VERSION,
                "certificate_binding": {
                    "algorithm": "sha256_der_leaf",
                    "resources": self.resource_sha256,
                    "authentication": self.authentication_sha256,
                },
                "read_evidence": {
                    "method": "setup_probe_v1",
                    "gateway_id": self.gateway_id,
                    "verified_at": self.verified_at,
                },
            },
        }


def load_local_access(connection: Mapping[str, object]) -> StoredLocalAccess:
    """Read the new format; a legacy scope cannot synthesize trust or freshness."""
    if "local_access" not in connection:
        raise LocalCredentialError("local_revalidation_required")
    access = _mapping(connection["local_access"])
    if (
        type(access.get("version")) is not int
        or access["version"] != LOCAL_ACCESS_VERSION
    ):
        raise LocalCredentialError("unsupported_local_access_version")
    raw = _mapping(connection.get("credentials"))
    if raw.get("token_type") != "Bearer" or "scope" in raw or "token_id" in raw:
        raise LocalCredentialError("invalid_local_credentials")
    raw_origin = raw.get("origin")
    if not isinstance(raw_origin, str):
        raise LocalCredentialError("invalid_token_origin")
    try:
        origin = TokenOrigin(raw_origin)
    except ValueError, TypeError:
        raise LocalCredentialError("invalid_token_origin") from None
    scopes = raw.get("reported_scope")
    if scopes is not None and (
        not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes)
    ):
        raise LocalCredentialError("invalid_reported_scope")
    credentials = LocalCredentials(
        validate_local_token(raw.get("token")),
        origin,
        tuple(scopes) if isinstance(scopes, list) else None,
    )
    endpoint = _mapping(connection.get("endpoint"))
    if (
        type(endpoint.get("port")) is not int
        or endpoint["port"] != 9443
        or endpoint.get("transport") != "https_local_api"
    ):
        raise LocalCredentialError("invalid_local_endpoint")
    host = endpoint.get("host")
    if not isinstance(host, str):
        raise LocalCredentialError("invalid_local_endpoint")
    address = LocalEndpoint(host)
    if address.host != host:
        raise LocalCredentialError("invalid_local_endpoint")
    trust = _mapping(access.get("certificate_binding"))
    evidence = _mapping(access.get("read_evidence"))
    if (
        trust.get("algorithm") != "sha256_der_leaf"
        or evidence.get("method") != "setup_probe_v1"
    ):
        raise LocalCredentialError("invalid_local_evidence")
    resource = _fingerprint(trust.get("resources"))
    assert resource is not None
    gateway_id = evidence.get("gateway_id")
    if not isinstance(gateway_id, str):
        raise LocalCredentialError("invalid_local_identity")
    return StoredLocalAccess(
        credentials,
        address,
        resource,
        _fingerprint(trust.get("authentication"), optional=True),
        gateway_id,
        _timestamp(evidence.get("verified_at")),
    )
