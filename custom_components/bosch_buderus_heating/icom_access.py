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

ICOM_ACCESS_KEY: Final = "icom_access"
ICOM_ACCESS_VERSION: Final = 1
ICOM_TRANSPORT: Final = "icom_http_aes"
ICOM_DEFAULT_PORT: Final = 80
# The relationship of a stored local gateway ID with the cloud's one: the
# local ID read from the gateway equals the cloud gateway ID.
ICOM_CONTRACT: Final = "icom-gateway-id-v1"
MAX_ID_LENGTH: Final = 1024


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

    @property
    def key(self) -> bytes:
        return derive_key(self.gateway_password, self.private_password)


def icom_connection_data(
    *,
    host: str,
    gateway_password: str,
    private_password: str,
    gateway_id: str,
    verified_at: float,
    port: int = ICOM_DEFAULT_PORT,
    local_id: str | None = None,
) -> dict[str, Any]:
    """The stored fields of an iCom connection, without the common counters."""
    return {
        "endpoint": {"host": host, "port": port, "transport": ICOM_TRANSPORT},
        "credentials": {
            "gateway_password": normalize_gateway_password(gateway_password),
            "private_password": validate_private_password(private_password),
        },
        ICOM_ACCESS_KEY: {
            "version": ICOM_ACCESS_VERSION,
            "gateway_id": gateway_id,
            "local_id": local_id or gateway_id,
            "verified_at": verified_at,
        },
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
    return IcomAccess(
        host,
        port,
        gateway_password,
        private_password,
        gateway_id,
        float(cast(float, verified)),
        local_id,
    )
