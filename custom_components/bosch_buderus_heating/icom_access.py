"""The stored local access of an iCom gateway, and its identity rule.

An iCom access is a connection of the local kind. It is told from a
K40/MX400 access by its "icom_access" record; the two passwords are kept in
"credentials" like every other secret of the entry. Nothing here opens a
connection. The module depends on no other module of the integration, so the
configuration can validate it without a cycle.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast

from .icom_crypto import (
    IcomCryptoError,
    derive_key,
    normalize_gateway_password,
    validate_private_password,
)
from .icom_identity import FINGERPRINT_PATHS, text_of

ICOM_ACCESS_KEY: Final = "icom_access"
ICOM_ACCESS_VERSION: Final = 1
ICOM_TRANSPORT: Final = "icom_http_aes"
ICOM_DEFAULT_PORT: Final = 80
# The relationship of a stored local gateway ID with the cloud's one: the
# local ID read from the gateway equals the cloud gateway ID.
ICOM_CONTRACT: Final = "icom-gateway-id-v1"
MAX_ID_LENGTH: Final = 1024
# How the gateway is recognised: by its own ID, or by values it shares with the
# cloud (when it reports no usable ID).
IDENTITY_UUID: Final = "uuid"
IDENTITY_VALUES: Final = "values"
# Neither the gateway's own ID nor its values proved it, but nothing
# contradicted the cloud either: the access is used without an identity check.
IDENTITY_UNVERIFIED: Final = "unverified"
IDENTITY_METHODS: Final = (IDENTITY_UUID, IDENTITY_VALUES, IDENTITY_UNVERIFIED)
IDENTITY_UUID_PATH: Final = "/gateway/uuid"
# Which form of the typed private password the setup proved: as typed, or
# without spaces at its start and end. Only a code for the diagnostics.
PRIVATE_PASSWORD_VARIANTS: Final = ("exact", "trimmed")


class IcomAccessError(ValueError):
    """A fixed code, never a password, address or identifier."""


def is_icom_connection(connection: Mapping[str, object]) -> bool:
    """Whether the stored record of a local connection is an iCom access."""
    return ICOM_ACCESS_KEY in connection


def comparable_id(value: object) -> str | None:
    """A gateway ID in the form used to compare local and cloud IDs.

    Letters and digits only, in lower case: a gateway number may be written
    with separators or in another case in one of the two sources. Anything
    that is no text or holds too little to identify a device gives None.
    """
    if not isinstance(value, str) or len(value) > MAX_ID_LENGTH:
        return None
    text = re.sub(r"[^0-9a-z]", "", value.lower())
    return text if len(text) >= 3 else None


# The readings of /gateway/uuid that only stand for "no ID".
_PLACEHOLDER_NUMBERS: Final = (-1, 0)
_HEX_DIGITS: Final = frozenset("0123456789abcdefABCDEF")


def _characters(text: str) -> str:
    """Which characters a reported ID holds: digits only, hex digits or others."""
    if text.isascii() and text.isdigit():
        return "digits"
    if text and set(text) <= _HEX_DIGITS:
        return "hex"
    return "other"


def describe_reported_id(value: object, *, read: bool = True) -> dict[str, object]:
    """What /gateway/uuid answered, for the debug log and the diagnostics.

    Only its kind, its length and which characters it holds (digits only, hex
    digits or others); never a character of the ID itself.
    """
    if not read:
        return {"kind": "not_offered"}
    if value is None:
        return {"kind": "none"}
    if isinstance(value, bool):
        return {"kind": "other", "type": "bool"}
    if isinstance(value, int | float):
        if value == -1:
            return {"kind": "minus_one"}
        if value in _PLACEHOLDER_NUMBERS:
            return {"kind": "placeholder"}
        return {
            "kind": "number",
            "length": len(str(abs(int(value)))) if math.isfinite(value) else 0,
            "chars": "digits",
        }
    if not isinstance(value, str):
        return {"kind": "other", "type": type(value).__name__}
    text = value.strip()
    if not text:
        return {"kind": "empty", "length": len(value)}
    if text == "-1":
        return {"kind": "minus_one"}
    return {
        # "x", "0" and the like stand for no ID.
        "kind": "placeholder" if comparable_id(value) is None else "text",
        "length": len(value),
        "chars": _characters(text),
    }


def safe_setup_check(value: object) -> dict[str, object]:
    """The stored notes of an identity check, known fields only."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    uuid = value.get("uuid")
    if isinstance(uuid, Mapping):
        result["uuid"] = {
            key: item
            for key, item in uuid.items()
            if (key in ("kind", "type", "chars") and isinstance(item, str))
            or (key == "length" and type(item) is int)
            if not isinstance(item, str) or len(item) <= 32
        }
    for key in ("uuid_matches_cloud",):
        if value.get(key) in (True, False, None) and key in value:
            result[key] = value[key]
    verdict = value.get("values_verdict")
    if verdict in ("confirmed", "contradiction", "insufficient"):
        result["values_verdict"] = verdict
    for key in ("values_matching", "values_read"):
        number = value.get(key)
        if type(number) is int and 0 <= number <= 100:
            result[key] = number
    if value.get("method") in IDENTITY_METHODS:
        result["method"] = value["method"]
    return result


def gateway_ids_match(local: object, expected: str) -> bool:
    """Whether the ID the gateway reports is the expected cloud gateway ID."""
    first, second = comparable_id(local), comparable_id(expected)
    return first is not None and first == second


@dataclass(frozen=True, slots=True)
class IcomAccess:
    """The verified, stored access of one gateway."""

    host: str = field(repr=False)
    port: int = field(repr=False)
    gateway_password: str = field(repr=False)
    private_password: str = field(repr=False)
    gateway_id: str = field(repr=False)
    verified_at: float = field(repr=False)
    # The ID the gateway itself reports (the cloud's own ID unless the setup
    # matched the cloud's reading of the same path).
    local_id: str = field(repr=False, default="")
    # How the gateway is recognised later, and (for values) the reading that
    # must stay the same.
    identity_method: str = IDENTITY_UUID
    fingerprint_path: str = field(repr=False, default="")
    fingerprint: str = field(repr=False, default="")
    private_password_variant: str = field(
        repr=False, default=PRIVATE_PASSWORD_VARIANTS[0]
    )
    # Notes of the setup's identity check (kinds, counts and a verdict only).
    setup_check: Mapping[str, object] = field(repr=False, default_factory=dict)

    @property
    def key(self) -> bytes:
        return derive_key(self.gateway_password, self.private_password)

    @property
    def identity_path(self) -> str:
        """The resource read to recognise the gateway again."""
        if self.identity_method == IDENTITY_VALUES:
            return self.fingerprint_path
        if self.identity_method == IDENTITY_UNVERIFIED:
            # Read only to see that the gateway answers with this key.
            return self.fingerprint_path or IDENTITY_UUID_PATH
        return IDENTITY_UUID_PATH

    def usable(self, reported: object) -> bool:
        """Whether a reading of the identity resource can tell gateways apart.

        An ID such as "-1" or an empty one says nothing about the gateway; it
        neither confirms nor contradicts the stored one.
        """
        if self.identity_method == IDENTITY_UNVERIFIED:
            return True
        if self.identity_method == IDENTITY_VALUES:
            return text_of(reported) is not None
        return comparable_id(reported) is not None

    def confirms(self, reported: object) -> bool:
        """Whether a reading of the identity resource is still this gateway.

        An unverified access has nothing to compare with: every answer counts.
        """
        if self.identity_method == IDENTITY_UNVERIFIED:
            return True
        if self.identity_method == IDENTITY_VALUES:
            return text_of(reported) == self.fingerprint
        return gateway_ids_match(reported, self.local_id)


def icom_connection_data(
    *,
    host: str,
    gateway_password: str,
    private_password: str,
    gateway_id: str,
    verified_at: float,
    port: int = ICOM_DEFAULT_PORT,
    local_id: str | None = None,
    fingerprint: tuple[str, str] | None = None,
    private_password_variant: str = PRIVATE_PASSWORD_VARIANTS[0],
    identity_method: str | None = None,
    setup_check: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """The stored fields of an iCom connection, without the common counters."""
    if private_password_variant not in PRIVATE_PASSWORD_VARIANTS:
        raise IcomAccessError("invalid_icom_access")
    method = identity_method or (
        IDENTITY_UUID if fingerprint is None else IDENTITY_VALUES
    )
    if method not in IDENTITY_METHODS or (
        method == IDENTITY_VALUES and fingerprint is None
    ):
        raise IcomAccessError("invalid_icom_identity")
    record: dict[str, Any] = {
        "version": ICOM_ACCESS_VERSION,
        "gateway_id": gateway_id,
        "local_id": local_id or gateway_id,
        "verified_at": verified_at,
        "private_password_variant": private_password_variant,
    }
    if method != IDENTITY_UUID:
        record["identity"] = method
    if fingerprint is not None and method != IDENTITY_UUID:
        record["fingerprint"] = {"path": fingerprint[0], "value": fingerprint[1]}
    if setup_check:
        record["setup_check"] = safe_setup_check(setup_check)
    return {
        "endpoint": {"host": host, "port": port, "transport": ICOM_TRANSPORT},
        "credentials": {
            "gateway_password": normalize_gateway_password(gateway_password),
            "private_password": validate_private_password(private_password),
        },
        ICOM_ACCESS_KEY: record,
    }


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IcomAccessError("invalid_icom_access")
    return value


def load_icom_access(connection: Mapping[str, object]) -> IcomAccess:
    """Read and check a stored iCom access."""
    if ICOM_ACCESS_KEY not in connection:
        raise IcomAccessError("icom_revalidation_required")
    record = _mapping(connection[ICOM_ACCESS_KEY])
    if type(record.get("version")) is not int or record["version"] != 1:
        raise IcomAccessError("unsupported_icom_access_version")
    endpoint = _mapping(connection.get("endpoint"))
    host, port = endpoint.get("host"), endpoint.get("port")
    if (
        endpoint.get("transport") != ICOM_TRANSPORT
        or not isinstance(host, str)
        or not 0 < len(host) <= 320
        or not host.isprintable()
        or any(character in host for character in "/\\?#@ ")
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise IcomAccessError("invalid_icom_endpoint")
    secrets = _mapping(connection.get("credentials"))
    try:
        gateway_password = normalize_gateway_password(secrets.get("gateway_password"))
        private_password = validate_private_password(secrets.get("private_password"))
    except IcomCryptoError as error:
        raise IcomAccessError(str(error)) from None
    gateway_id = record.get("gateway_id")
    if (
        not isinstance(gateway_id, str)
        or not 0 < len(gateway_id) <= MAX_ID_LENGTH
        or not gateway_id.isprintable()
        or any(character.isspace() for character in gateway_id)
        or comparable_id(gateway_id) is None
    ):
        raise IcomAccessError("invalid_icom_identity")
    local_id = record.get("local_id", gateway_id)
    if (
        not isinstance(local_id, str)
        or not 0 < len(local_id) <= MAX_ID_LENGTH
        or not local_id.isprintable()
        or any(character.isspace() for character in local_id)
        or comparable_id(local_id) is None
    ):
        raise IcomAccessError("invalid_icom_identity")
    verified = record.get("verified_at")
    if type(verified) not in (int, float) or not math.isfinite(cast(float, verified)):
        raise IcomAccessError("invalid_icom_verification_time")
    method = record.get("identity", IDENTITY_UUID)
    if method not in IDENTITY_METHODS:
        raise IcomAccessError("invalid_icom_identity")
    # Accesses saved before this code was kept were always used as typed.
    variant = record.get("private_password_variant", PRIVATE_PASSWORD_VARIANTS[0])
    if variant not in PRIVATE_PASSWORD_VARIANTS:
        raise IcomAccessError("invalid_icom_access")
    path = text = ""
    # An unverified access keeps a fingerprint only when one was read.
    if method == IDENTITY_VALUES or (
        method == IDENTITY_UNVERIFIED and record.get("fingerprint") is not None
    ):
        stored = record.get("fingerprint")
        if not isinstance(stored, Mapping):
            raise IcomAccessError("invalid_icom_identity")
        raw_path, raw_text = stored.get("path"), stored.get("value")
        if (
            raw_path not in FINGERPRINT_PATHS
            or not isinstance(raw_text, str)
            or text_of(raw_text) != raw_text
        ):
            raise IcomAccessError("invalid_icom_identity")
        path, text = raw_path, raw_text
    return IcomAccess(
        host,
        port,
        gateway_password,
        private_password,
        gateway_id,
        float(cast(float, verified)),
        local_id,
        method,
        path,
        text,
        variant,
        safe_setup_check(record.get("setup_check")),
    )
