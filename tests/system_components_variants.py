"""Variants of the shipped component table for tests.

An entry with the basis ``name`` must never change what the integration
does. Tests show this by running the same case with each such entry removed
one at a time, and with each one alone put back into a table without any of
them: only shown names may differ.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

from custom_components.bosch_buderus_heating.system_components import (
    TABLE_PATH,
    ComponentTable,
    parse_table,
)


def raw_table() -> dict[str, Any]:
    """A fresh copy of the shipped table as read from its file."""
    value = json.loads(TABLE_PATH.read_bytes().decode("utf-8"))
    assert isinstance(value, dict)
    return value


def name_entry_types(raw: dict[str, Any] | None = None) -> list[int]:
    """The type numbers of all components known by their name only."""
    raw = raw_table() if raw is None else raw
    return [item["type"] for item in raw["components"] if item["basis"] == "name"]


def without_name_entry(raw: dict[str, Any], type_id: int) -> dict[str, Any]:
    """A copy of the table without the name entry of this type number."""
    result = copy.deepcopy(raw)
    before = len(result["components"])
    result["components"] = [
        item
        for item in result["components"]
        if not (item["type"] == type_id and item["basis"] == "name")
    ]
    assert len(result["components"]) == before - 1, type_id
    return result


def without_name_entries(raw: dict[str, Any]) -> dict[str, Any]:
    """A copy of the table without any name entry."""
    result = copy.deepcopy(raw)
    result["components"] = [
        item for item in result["components"] if item["basis"] != "name"
    ]
    return result


def with_only_name_entry(raw: dict[str, Any], type_id: int) -> dict[str, Any]:
    """A copy of the table whose only name entry is the one of this type."""
    result = without_name_entries(raw)
    (entry,) = (
        item
        for item in raw["components"]
        if item["type"] == type_id and item["basis"] == "name"
    )
    result["components"] = sorted(
        [*result["components"], copy.deepcopy(entry)], key=lambda item: item["type"]
    )
    return result


def _parsed(raw: dict[str, Any]) -> ComponentTable:
    table = parse_table(raw)
    assert table is not None
    return table


def each_name_entry_removed() -> Iterator[tuple[int, ComponentTable]]:
    """(type number, table without its name entry) for every name entry."""
    raw = raw_table()
    for type_id in name_entry_types(raw):
        yield type_id, _parsed(without_name_entry(raw, type_id))


def each_name_entry_alone() -> Iterator[tuple[int, ComponentTable]]:
    """(type number, table with only this name entry) for every name entry."""
    raw = raw_table()
    for type_id in name_entry_types(raw):
        yield type_id, _parsed(with_only_name_entry(raw, type_id))
