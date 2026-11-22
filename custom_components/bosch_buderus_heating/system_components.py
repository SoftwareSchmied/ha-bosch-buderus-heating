"""The component table: type numbers, places and the few roles they have.

A gateway lists the components of its system with a type number (``Id``)
and a place (``Tok``). The versioned data table ``system_components.json``
gives each type number its name and each place its key. Every entry states
its basis:

- ``name``: only the name is known. Such an entry is shown and never
  changes what the integration does.
- ``rule``: a fixed rule of the integration uses the entry.
- ``observed``: seen in the component list of a real system; the entry
  carries a short note.
- ``documented``: stated in a manufacturer document; the entry carries its
  address.

Roles and flags only come from entries with the basis ``rule``,
``observed`` or ``documented``. Nothing is derived from a name. Conditions
such as firmware or software limits live in code, not in the table.

The table is read once on import (in the import executor), never in the
event loop. A missing or faulty table gives one warning and no table; the
rest of the integration works without it.

Pure logic without Home Assistant imports.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from .heat_generator import manufacturer_source

TABLE_PATH: Final = Path(__file__).with_name("system_components.json")
TABLE_SCHEMA: Final = 1

_LOGGER = logging.getLogger(__name__)

BASIS_VALUES: Final = ("name", "rule", "observed", "documented")
BEHAVIOUR_BASES: Final = frozenset({"rule", "observed", "documented"})
ROLE_VALUES: Final = (
    "heat_source",
    "outdoor_unit",
    "hybrid_manager",
    "system_controller",
    "gateway",
)
FLAG_VALUES: Final = ("hmi800_hp_light",)
# The closed set of place keys.
PLACE_KEYS: Final = (
    "heat_generator_control",
    "basic_controller",
    "system_controller",
    "pool",
    "remote_hc1_legacy",
    "remote_hc2_legacy",
    "remote_hc3_legacy",
    "remote_hc4_legacy",
    "fresh_water",
    "solar",
    "remote_hc1",
    "remote_hc2",
    "remote_hc3",
    "remote_hc4",
    "gateway",
    "gateway_service",
    "radio",
    "ventilation",
    "hybrid_manager",
)

# A row at one of these places, or of this type, is the gateway itself.
GATEWAY_PLACES: Final = frozenset({72, 75})
GATEWAY_TYPE_IDS: Final = frozenset({17})
# The type number that marks a row without a valid type.
INVALID_TYPE_ID: Final = 0

# Type and place numbers have one to six digits.
MAX_NUMBER: Final = 999_999
_MAX_NAME_LENGTH: Final = 64
_MAX_NOTE_LENGTH: Final = 200
_NAME: Final = re.compile(r"[!-~](?:[ -~]*[!-~])?")
_SOURCE_SITE: Final = re.compile(r"[a-z0-9.-]+\.[a-z]{2,}(/[A-Za-z0-9._/-]+)?")
_DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

_TABLE_FIELDS: Final = frozenset(
    {
        "schema",
        "table_version",
        "updated",
        "documented_sources",
        "places",
        "components",
        "roles",
        "flags",
    }
)
_EVIDENCE_FIELDS: Final = frozenset({"basis", "note", "source"})
_PLACE_FIELDS: Final = frozenset({"place", "key", "name"}) | _EVIDENCE_FIELDS
_COMPONENT_FIELDS: Final = frozenset({"type", "name", "aliases"}) | _EVIDENCE_FIELDS
_ROLE_FIELDS: Final = frozenset({"type", "place", "role"}) | _EVIDENCE_FIELDS
_FLAG_FIELDS: Final = frozenset({"type", "flag"}) | _EVIDENCE_FIELDS


@dataclass(frozen=True, slots=True)
class ComponentType:
    """One type number; ``name`` is empty when the type has no name."""

    id: int
    name: str
    aliases: tuple[str, ...]
    basis: str


@dataclass(frozen=True, slots=True)
class Place:
    """One place; ``key`` is the fixed place key, ``name`` may be empty."""

    tok: int
    key: str
    name: str
    basis: str


@dataclass(frozen=True, slots=True)
class Role:
    """The role of a type at a place, or at any place when ``tok`` is None."""

    type_id: int
    tok: int | None
    role: str
    basis: str


@dataclass(frozen=True, slots=True)
class ComponentTable:
    """The validated table."""

    schema: int
    table_version: int
    updated: str
    components: Mapping[int, ComponentType]
    places: Mapping[int, Place]
    roles: tuple[Role, ...]
    flags: Mapping[int, frozenset[str]]

    def component(self, type_id: int) -> ComponentType | None:
        return self.components.get(type_id)

    def place(self, tok: int) -> Place | None:
        return self.places.get(tok)

    def role(self, type_id: int, tok: int | None) -> Role | None:
        """The role at exactly this place first, then the role at any place.

        Only entries with a basis that may change behaviour count.
        """
        fallback = None
        for item in self.roles:
            if item.type_id != type_id or item.basis not in BEHAVIOUR_BASES:
                continue
            if tok is not None and item.tok == tok:
                return item
            if item.tok is None:
                fallback = item
        return fallback

    def has_flag(self, type_id: int, flag: str) -> bool:
        return flag in self.flags.get(type_id, frozenset())


def is_gateway_row(type_id: int | None, tok: int | None) -> bool:
    """A row of the gateway itself: by its place or by its type number."""
    return tok in GATEWAY_PLACES or type_id in GATEWAY_TYPE_IDS


# ---------------------------------------------------------------- validation


def _number(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= MAX_NUMBER
    ):
        raise ValueError("invalid_number")
    return value


def _name(value: object, *, required: bool) -> str:
    if value is None and not required:
        return ""
    if (
        not isinstance(value, str)
        or len(value) > _MAX_NAME_LENGTH
        or not _NAME.fullmatch(value)
    ):
        raise ValueError("invalid_name")
    return value


def _fields(item: object, allowed: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(item, Mapping) or set(item) - allowed:
        raise ValueError("invalid_entry")
    return item


def _basis(
    item: Mapping[str, Any], sources: tuple[str, ...], allowed: tuple[str, ...]
) -> str:
    """``observed`` needs a note; ``documented`` a note and the address of
    a manufacturer page; no other basis carries an address."""
    basis = item.get("basis")
    if basis not in allowed:
        raise ValueError("invalid_basis")
    note, source = item.get("note"), item.get("source")
    if note is not None and (
        not isinstance(note, str)
        or not note.strip()
        or len(note) > _MAX_NOTE_LENGTH
        or not _NAME.fullmatch(note)
    ):
        raise ValueError("invalid_note")
    if basis in ("observed", "documented") and note is None:
        raise ValueError("entry_without_note")
    if basis == "documented":
        if not manufacturer_source(source, sources):
            raise ValueError("source_not_manufacturer")
    elif source is not None:
        raise ValueError("invalid_source")
    return str(basis)


def _places(value: object, sources: tuple[str, ...]) -> dict[int, Place]:
    if not isinstance(value, list):
        raise ValueError("invalid_places")
    places: dict[int, Place] = {}
    for raw in value:
        item = _fields(raw, _PLACE_FIELDS)
        tok = _number(item.get("place"))
        key = item.get("key")
        if key not in PLACE_KEYS:
            raise ValueError("invalid_place_key")
        basis = _basis(item, sources, BASIS_VALUES)
        name = _name(item.get("name"), required=basis == "name")
        if tok in places or any(place.key == key for place in places.values()):
            raise ValueError("duplicate_place")
        places[tok] = Place(tok, str(key), name, basis)
    return places


def _components(value: object, sources: tuple[str, ...]) -> dict[int, ComponentType]:
    if not isinstance(value, list) or not value:
        raise ValueError("invalid_components")
    components: dict[int, ComponentType] = {}
    for raw in value:
        item = _fields(raw, _COMPONENT_FIELDS)
        type_id = _number(item.get("type"))
        basis = _basis(item, sources, BASIS_VALUES)
        name = _name(item.get("name"), required=basis == "name")
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list) or ("aliases" in item and not aliases):
            raise ValueError("invalid_aliases")
        names = [name, *(_name(alias, required=True) for alias in aliases)]
        if (aliases and not name) or len(set(names)) != len(names):
            raise ValueError("invalid_aliases")
        if type_id in components:
            raise ValueError("duplicate_component")
        components[type_id] = ComponentType(type_id, name, tuple(names[1:]), basis)
    return components


def _roles(
    value: object,
    sources: tuple[str, ...],
    components: Mapping[int, ComponentType],
    places: Mapping[int, Place],
) -> tuple[Role, ...]:
    if not isinstance(value, list):
        raise ValueError("invalid_roles")
    roles: list[Role] = []
    for raw in value:
        item = _fields(raw, _ROLE_FIELDS)
        type_id = _number(item.get("type"))
        place = item.get("place")
        tok = None if place is None else _number(place)
        role = item.get("role")
        if role not in ROLE_VALUES:
            raise ValueError("invalid_role")
        basis = _basis(item, sources, tuple(sorted(BEHAVIOUR_BASES)))
        if type_id not in components or (tok is not None and tok not in places):
            raise ValueError("role_without_entry")
        if any(known.type_id == type_id and known.tok == tok for known in roles):
            raise ValueError("duplicate_role")
        roles.append(Role(type_id, tok, str(role), basis))
    return tuple(roles)


def _flags(
    value: object, sources: tuple[str, ...], components: Mapping[int, ComponentType]
) -> dict[int, frozenset[str]]:
    if not isinstance(value, list):
        raise ValueError("invalid_flags")
    flags: dict[int, set[str]] = {}
    for raw in value:
        item = _fields(raw, _FLAG_FIELDS)
        type_id = _number(item.get("type"))
        flag = item.get("flag")
        if flag not in FLAG_VALUES:
            raise ValueError("invalid_flag")
        _basis(item, sources, tuple(sorted(BEHAVIOUR_BASES)))
        if type_id not in components:
            raise ValueError("flag_without_entry")
        if flag in flags.setdefault(type_id, set()):
            raise ValueError("duplicate_flag")
        flags[type_id].add(str(flag))
    return {type_id: frozenset(names) for type_id, names in flags.items()}


def _parse(raw: object) -> ComponentTable:
    if (
        not isinstance(raw, Mapping)
        or set(raw) != _TABLE_FIELDS
        or raw.get("schema") != TABLE_SCHEMA
    ):
        raise ValueError("unsupported_table")
    version = raw["table_version"]
    updated = raw["updated"]
    sources = raw["documented_sources"]
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
        or not isinstance(updated, str)
        or not _DATE.fullmatch(updated)
        or not isinstance(sources, list)
        or not all(
            isinstance(item, str) and _SOURCE_SITE.fullmatch(item) for item in sources
        )
    ):
        raise ValueError("invalid_table")
    date.fromisoformat(updated)
    allowed = tuple(sources)
    places = _places(raw["places"], allowed)
    components = _components(raw["components"], allowed)
    roles = _roles(raw["roles"], allowed, components, places)
    flags = _flags(raw["flags"], allowed, components)
    return ComponentTable(
        schema=TABLE_SCHEMA,
        table_version=version,
        updated=updated,
        components=MappingProxyType(components),
        places=MappingProxyType(places),
        roles=roles,
        flags=MappingProxyType(flags),
    )


def parse_table(raw: object) -> ComponentTable | None:
    """The validated table, or None: any fault rejects the whole table."""
    try:
        return _parse(raw)
    except Exception:  # a faulty table never raises
        return None


def _read(path: Path) -> ComponentTable | None:
    try:
        return _parse(json.loads(path.read_bytes().decode("utf-8")))
    except Exception as err:  # a faulty table never stops the integration
        _LOGGER.warning(
            "The system component table could not be loaded (%s); "
            "component names and roles are not available",
            type(err).__name__,
        )
        return None


@functools.cache
def load_table() -> ComponentTable | None:
    """The shipped table, read once; None with one warning if it is missing
    or faulty."""
    return _read(TABLE_PATH)


# Read once on import (in the import executor), never in the event loop.
load_table()
