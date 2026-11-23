"""The interface for cards of the heat generator sensor (contract 1).

``check_contract`` checks the attributes of one heat generator sensor against
the published JSON schema ``docs/schemas/heat-generator-sensor.v1.json`` and
the fixed key set of contract 1. Used by the contract tests and by the golden
master of every fixture plant.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

SCHEMA_PATH = (
    Path(__file__).parents[2] / "docs" / "schemas" / "heat-generator-sensor.v1.json"
)
# Attributes Home Assistant adds to every state.
_HOME_ASSISTANT_KEYS = frozenset({"friendly_name", "icon", "entity_picture"})


@cache
def schema() -> dict[str, Any]:
    value = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _types(kinds: object) -> list[str]:
    return [kinds] if isinstance(kinds, str) else list(kinds)  # type: ignore[arg-type]


_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(
    instance: object,
    part: Mapping[str, Any],
    root: Mapping[str, Any],
    at: str = "$",
) -> None:
    """A small JSON schema check (the subset the published schema uses)."""
    if "$ref" in part:
        target: Any = root
        for name in part["$ref"].removeprefix("#/").split("/"):
            target = target[name]
        validate(instance, target, root, at)
        return
    if "type" in part:
        kinds = _types(part["type"])
        assert any(_CHECKS[kind](instance) for kind in kinds), f"{at}: type {kinds}"
    if "const" in part:
        assert instance == part["const"], f"{at}: const"
    if "enum" in part:
        assert instance in part["enum"], f"{at}: {instance!r} not in enum"
    if isinstance(instance, str):
        if "pattern" in part:
            assert re.search(part["pattern"], instance), f"{at}: pattern"
        if "maxLength" in part:
            assert len(instance) <= part["maxLength"], f"{at}: maxLength"
    number = isinstance(instance, int | float) and not isinstance(instance, bool)
    if number and "minimum" in part:
        assert instance >= part["minimum"], f"{at}: minimum"
    if isinstance(instance, dict):
        for key in part.get("required", ()):
            assert key in instance, f"{at}: {key} missing"
        properties = part.get("properties", {})
        extra = part.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                validate(value, properties[key], root, f"{at}.{key}")
            else:
                assert extra is not False, f"{at}: unexpected key {key}"
    if isinstance(instance, list):
        if "maxItems" in part:
            assert len(instance) <= part["maxItems"], f"{at}: maxItems"
        if isinstance(part.get("items"), dict):
            for index, value in enumerate(instance):
                validate(value, part["items"], root, f"{at}[{index}]")


def check_contract(attributes: Mapping[str, Any]) -> None:
    """Contract 1: every key present, every value as the schema states,
    nothing else besides what Home Assistant adds."""
    own = {
        key: value
        for key, value in attributes.items()
        if key not in _HOME_ASSISTANT_KEYS
    }
    validate(own, schema(), schema())
    assert set(own) == set(schema()["required"]), sorted(
        set(own) ^ set(schema()["required"])
    )
    components = own["components"]
    order = [
        (
            item["place_number"] if item["place_number"] is not None else -1,
            item["type_number"],
            item["product_name"] or "",
        )
        for item in components
    ]
    assert order == sorted(order), "components are sorted"
    if own["heat_generators"]:
        assert own["state_basis"] == "product_name"
    else:
        assert own["state_basis"] in ("component_type", None)
        assert own["known"] is False
        assert own["source"] is None
