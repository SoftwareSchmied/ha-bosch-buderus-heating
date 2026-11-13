"""Strict account, topic and event contracts for the Bacon foundation."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

CLAIM_URL = "https://claiming.euc1.bacon.bosch-tt-cw.com/v1/users/self/devices"
BROKER = "broker.euc1.bacon.bosch-tt-cw.com"
MAX_BYTES = 128 * 1024
MAX_DEVICES = 128
CONF_BACON_IDS = "bacon_device_ids"
CONF_BACON_ACCOUNT = "bacon_account"
USER_AGENT = "DashApp/3.7.0 (iOS-Release)"


class BaconError(Exception):
    """Only reviewed error categories are exposed to diagnostics."""

    def __init__(self, category: str, retry_after: float = 60) -> None:
        super().__init__(category)
        self.category = category
        self.retry_after = retry_after


def identifier(value: object) -> str:
    """Reject topic injection and ambiguous separators before any request."""
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) is None
    ):
        raise BaconError("invalid_identifier")
    return value


def token_subject(token: str) -> str:
    """Extract routing information; only the server authenticates the JWT."""
    try:
        if len(token) > 32768:
            raise ValueError
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "==="))
        return identifier(payload["sub"])
    except (ValueError, KeyError, TypeError, BaconError) as err:
        raise BaconError("authentication") from err


def account_key(subject: str) -> str:
    return hashlib.sha256(identifier(subject).encode()).hexdigest()


def device_key(subject: str, serial: str) -> str:
    return (
        "bacon:"
        + hashlib.sha256(
            (identifier(subject) + "/" + identifier(serial)).encode()
        ).hexdigest()
    )


def device_list(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, list) or len(payload) > MAX_DEVICES:
        raise BaconError("invalid_device_list")
    result = tuple(identifier(item) for item in payload)
    if len(result) != len(set(result)):
        raise BaconError("invalid_device_list")
    return result


def object_payload(payload: bytes | str) -> dict[str, Any]:
    if len(payload) > MAX_BYTES:
        raise BaconError("invalid_payload")
    try:
        result = json.loads(payload)
        # Limit nesting and reject nonfinite values without leaking raw data.
        pending = [(result, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 16 or (isinstance(value, float) and not math.isfinite(value)):
                raise ValueError
            if isinstance(value, dict):
                pending.extend((v, depth + 1) for v in value.values())
            elif isinstance(value, list):
                pending.extend((v, depth + 1) for v in value)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError) as err:
        raise BaconError("invalid_payload") from err


def topic(subject: str, serial: str, suffix: str) -> str:
    return f"users/{identifier(subject)}/devices/{identifier(serial)}/{suffix}"


@dataclass(frozen=True, slots=True)
class Shadow:
    version: int
    timestamp: int
    reported: dict[str, Any] = field(repr=False)
    desired: dict[str, Any] = field(repr=False)


def shadow(payload: dict[str, Any]) -> Shadow:
    version, timestamp, state = (
        payload.get("version"),
        payload.get("timestamp"),
        payload.get("state"),
    )
    if (
        type(version) is not int
        or version < 0
        or type(timestamp) is not int
        or timestamp < 0
        or not isinstance(state, dict)
        or not isinstance(state.get("reported", {}), dict)
        or not isinstance(state.get("desired", {}), dict)
    ):
        raise BaconError("invalid_shadow")
    return Shadow(
        version, timestamp, state.get("reported", {}), state.get("desired", {})
    )


@dataclass(slots=True)
class Device:
    serial: str = field(repr=False)
    online: bool | None = None
    error_present: bool | None = None
    info_timestamp: int = -1
    info_received_at: float = 0
    # Last device-originated, non-retained message on any channel.
    alive_at: float | None = None
    model: str | None = None
    state: Shadow | None = field(default=None, repr=False)
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    sensor: dict[str, Any] = field(default_factory=dict, repr=False)
    sensor_timestamp: int = -1
    sensor_received_at: float = 0
    sensor_fields_received_at: dict[str, float] = field(
        default_factory=dict, repr=False
    )
    sensor_generation: int = 0
    metadata_generation: int = 0
    state_received_at: float = 0
    state_fields_received_at: dict[str, float] = field(default_factory=dict, repr=False)
    state_fields_version: dict[str, int] = field(default_factory=dict, repr=False)
    state_snapshot_version: int = -1
    # Counts accepted reported-field observations to separate device reports.
    state_reports: int = 0
    received_at: float = 0
    generation: int = 0
    state_generation: int = 0
    uncertain: bool = False
