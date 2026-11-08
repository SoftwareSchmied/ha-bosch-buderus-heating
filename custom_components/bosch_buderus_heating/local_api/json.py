"""Bounded UTF-8 JSON shared with the local setup probe."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .errors import LocalResourceError, Problem


@dataclass(frozen=True, slots=True)
class JsonLimits:
    """Defensive parser limits, not a vendor capacity or polling promise."""

    bytes: int = 256 * 1024
    depth: int = 32
    nodes: int = 8192

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (self.bytes, self.depth, self.nodes)
        ):
            raise ValueError("Invalid parser limits")


DEFAULT_LIMITS = JsonLimits()


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def _invalid(_: str) -> object:
    raise ValueError("Nonfinite number")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite number")
    return number


def _decimal(value: str) -> Decimal:
    number = Decimal(value)
    if not number.is_finite() or abs(number.adjusted()) > 308:
        raise ValueError("Number outside parser limits")
    return number


def load_json(
    body: bytes, *, exact_numbers: bool = True, limits: JsonLimits = DEFAULT_LIMITS
) -> object:
    """Reject duplicate keys, nonfinite numbers and excessive structures."""
    if not isinstance(body, bytes):
        raise LocalResourceError(Problem.INVALID_JSON)
    if len(body) > limits.bytes:
        raise LocalResourceError(Problem.BODY_TOO_LARGE)
    try:
        payload: object = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=_invalid,
            parse_float=_decimal if exact_numbers else _float,
        )
    except ValueError, RecursionError, InvalidOperation:
        raise LocalResourceError(Problem.INVALID_JSON) from None
    pending = [(payload, 0)]
    remaining = limits.nodes
    while pending:
        item, depth = pending.pop()
        remaining -= 1
        if remaining < 0 or depth > limits.depth:
            raise LocalResourceError(Problem.INVALID_JSON)
        if isinstance(item, (dict, list)):
            if len(item) > remaining:
                raise LocalResourceError(Problem.INVALID_JSON)
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)
    return payload
