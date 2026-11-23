"""Validate the reviewed subset of schema facets used by this local API."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from ._data import SCHEMAS
from .errors import LocalResourceError, Problem


def is_number(value: object) -> bool:
    return type(value) is int or (isinstance(value, Decimal) and value.is_finite())


def same_value(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def iso_date(value: str) -> date:
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise ValueError("Invalid date")
    return date.fromisoformat(value)


def iso_datetime(value: str) -> datetime:
    if (
        re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})",
            value,
        )
        is None
    ):
        raise ValueError("Invalid datetime")
    if not value.endswith("Z") and int(value[-2:]) >= 60:
        raise ValueError("Invalid offset")
    return datetime.fromisoformat(value)


def _format(value: str, format_name: str | None) -> bool:
    try:
        if format_name == "date":
            iso_date(value)
        elif format_name == "date-time":
            iso_datetime(value)
        elif format_name == "uri":
            parsed = urlsplit(value)
            return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False
    return True


def matches(value: Any, schema: dict[str, Any], *, open_value: bool = False) -> bool:
    """Check source facets without inferring transport rights from writeable.

    With ``open_value``, the enum of a document's ``value`` is not checked
    (every other facet is): a reading of a value the pinned schema does not
    list yet.
    """
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name in {"ReadableResource", "WriteableResource"}:
            return True  # BaseResource still validates the reported 0/1 metadata.
        return matches(value, SCHEMAS[name], open_value=open_value)
    if "allOf" in schema and not all(
        matches(value, s, open_value=open_value) for s in schema["allOf"]
    ):
        return False
    if (
        "oneOf" in schema
        and sum(matches(value, s, open_value=open_value) for s in schema["oneOf"]) != 1
    ):
        return False
    if "anyOf" in schema and not any(
        matches(value, s, open_value=open_value) for s in schema["anyOf"]
    ):
        return False
    kind = schema.get("type")
    if kind is None and ("properties" in schema or "required" in schema):
        kind = "object"
    if kind not in {None, "object", "array", "string", "integer", "number", "boolean"}:
        return False
    if kind == "object":
        if (
            not isinstance(value, dict)
            or not set(schema.get("required", ())) <= value.keys()
        ):
            return False
        if (
            not schema.get("minProperties", 0)
            <= len(value)
            <= schema.get("maxProperties", len(value))
        ):
            return False
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            if key in properties:
                facets = properties[key]
                if open_value and key == "value" and "enum" in facets:
                    facets = {
                        name: item for name, item in facets.items() if name != "enum"
                    }
                if not matches(child, facets):
                    return False
            elif additional is False or (
                isinstance(additional, dict) and not matches(child, additional)
            ):
                return False
    elif kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 0) <= len(
            value
        ) <= schema.get("maxItems", len(value)):
            return False
        if not all(matches(item, schema.get("items", {})) for item in value):
            return False
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(
            value
        ) <= schema.get("maxLength", len(value)):
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False
        if not _format(value, schema.get("format")):
            return False
    elif kind == "integer":
        if type(value) is not int:
            return False
    elif kind == "number":
        if not is_number(value):
            return False
    elif kind == "boolean" and type(value) is not bool:
        return False
    if "enum" in schema and not any(same_value(value, item) for item in schema["enum"]):
        return False
    if is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
    return True


def validate(value: object, schema: dict[str, Any]) -> None:
    if not matches(value, schema):
        raise LocalResourceError(Problem.INVALID_SCHEMA)


def read_only(schema: dict[str, Any]) -> bool:
    """The response contract of a resource the gateway reports as readable
    only (never one of a setting)."""
    parts = schema.get("allOf", ())
    refs = {
        str(part.get("$ref", "")).rsplit("/", 1)[-1]
        for part in parts
        if isinstance(part, dict)
    }
    return "ReadableResource" in refs and "WriteableResource" not in refs
