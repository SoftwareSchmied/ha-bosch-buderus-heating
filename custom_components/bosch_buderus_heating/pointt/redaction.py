"""Redaction helpers for diagnostics, fixtures, and support output."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping

from .models import JsonValue

REDACTED = "<redacted>"

_SECRET_KEYS = {
    "access_token",
    "authorization",
    "code",
    "code_verifier",
    "id_token",
    "nonce",
    "password",
    "refresh_token",
    "state",
    "token",
}
_IDENTIFIER_KEYS = {
    "account_subject",
    "gateway_id",
    "gatewayid",
    "mac",
    "serial",
    "serial_number",
    "serialnumber",
    "uuid",
}
_PRIVATE_KEYS = {
    "custom_name",
    "customtitle",
    "email",
    "ip",
    "location",
    "name",
    "ssid",
    "timezone",
    "username",
}

_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_EMAIL_PATTERN = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_MAC_PATTERN = re.compile(r"(?i)\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b")
_IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_OAUTH_QUERY_PATTERN = re.compile(
    r"(?i)([?&](?:code|state|nonce|access_token|refresh_token)=)[^&#\s]+"
)


def anonymize_identifier(value: object, *, salt: bytes) -> str:
    """Return an installation-specific, non-reversible short identifier."""
    digest = hmac.new(salt, str(value).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"id:{digest[:12]}"


def redact_mapping(value: Mapping[str, object], *, salt: bytes) -> dict[str, JsonValue]:
    """Recursively redact credentials and personal identifiers."""
    return {key: _redact_value(key, item, salt=salt) for key, item in value.items()}


def _redact_value(key: str, value: object, *, salt: bytes) -> JsonValue:
    normalized = key.casefold()
    if normalized in _SECRET_KEYS or normalized.endswith("token"):
        return REDACTED
    if normalized in _IDENTIFIER_KEYS:
        return anonymize_identifier(value, salt=salt)
    if normalized in _PRIVATE_KEYS:
        return REDACTED
    if isinstance(value, Mapping):
        return redact_mapping(
            {str(child_key): child for child_key, child in value.items()}, salt=salt
        )
    if isinstance(value, list):
        return [_redact_value("", item, salt=salt) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return redact_text(value) if isinstance(value, str) else value
    return REDACTED


def redact_text(value: str) -> str:
    """Remove common credentials and personal data from free-form text."""
    redacted = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", value)
    redacted = _JWT_PATTERN.sub(REDACTED, redacted)
    redacted = _EMAIL_PATTERN.sub(REDACTED, redacted)
    redacted = _MAC_PATTERN.sub(REDACTED, redacted)
    redacted = _IPV4_PATTERN.sub(REDACTED, redacted)
    return _OAUTH_QUERY_PATTERN.sub(rf"\1{REDACTED}", redacted)
