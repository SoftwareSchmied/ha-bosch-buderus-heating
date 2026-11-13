"""Which heat generators a gateway reports, by role, from its module list.

The cloud's ``/system/info`` and the local ``/system/basicInfo`` list the
modules of a system. Only two fields of a module are read: the product name
and the hardware type. Serial numbers, tokens, versions and type numbers are
never read, kept or shown.

A module with a product name is a heat generator module (heat source, boiler,
outdoor unit, hybrid manager or other). Rows without a product name (the
gateway, the user interface) never count. The meaning of a module comes from
the versioned data table ``heat_generator_models.json`` only: a role from its
hardware type or product name, and family, construction, power and brand
family where an entry proves them. Nothing is guessed from parts of a name;
without an entry a value stays ``None``. A new model needs a new table entry
and a test, never new code.

Pure logic without Home Assistant imports.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

TABLE_PATH: Final = Path(__file__).with_name("heat_generator_models.json")
TABLE_SCHEMA: Final = 1

# The roles in their fixed order: the first module of the sorted list is the
# main heat generator (a heat source before a boiler before the rest).
ROLES: Final = ("heat_source", "boiler", "outdoor_unit", "hybrid_manager", "other")
OTHER: Final = "other"
SOURCES: Final = ("cloud", "local")

# Bounds: rows read from one answer, modules shown, text lengths.
MAX_ROWS: Final = 64
MAX_GENERATORS: Final = 16
MAX_NAME_LENGTH: Final = 255
MAX_HARDWARE_LENGTH: Final = 64
_MAX_FAMILY_LENGTH: Final = 64
_MAX_POWER_KW: Final = 10_000.0

# The fields of a cloud row that hold serial numbers or other identifiers. A
# product name or hardware type equal to one of them is never shown.
_SERIAL_FIELDS: Final = (
    "ModuleSerialNumber",
    "ProductSerialNumber",
    "No",
    "ProductUuid",
)
# A text made only of digits from this length on is a number, not a name.
_NUMBER_ONLY: Final = re.compile(r"[0-9]{8,}")
_ENTRY_FIELDS: Final = frozenset(
    {
        "match",
        "role",
        "family",
        "construction",
        "power_kw",
        "brand_family",
        "examples",
        "evidence",
    }
)
_MEANING_FIELDS: Final = ("role", "family", "construction", "power_kw", "brand_family")


def clean_text(value: object, limit: int = MAX_NAME_LENGTH) -> str | None:
    """A shown text: control characters and padding removed, spaces
    collapsed, cut to ``limit`` characters; ``None`` when nothing remains."""
    if not isinstance(value, str):
        return None
    text = "".join(
        character if character.isprintable() else " "
        for character in value
        if character != "�"
    )
    text = " ".join(text.split())
    # 0xFF bytes are padding of an unset field, never a name.
    if not text or not text.replace("\xff", "").strip():
        return None
    return text[:limit].rstrip() or None


@dataclass(frozen=True, slots=True)
class ModuleRow:
    """The two fields of one module that are read, already cleaned."""

    product_name: str | None
    hardware_type: str | None


def _identifiers(values: Iterable[object]) -> frozenset[str]:
    found: set[str] = set()
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, str | int | float):
            text = " ".join(str(value).split()).casefold()
            if text:
                found.add(text)
    return frozenset(found)


def _shown(value: object, limit: int, identifiers: frozenset[str]) -> str | None:
    text = clean_text(value, limit)
    if text is None or _NUMBER_ONLY.fullmatch(text) is not None:
        return None
    if text.casefold() in identifiers:
        return None  # a serial number in a name field is never shown
    return text


def _row(
    product_name: object, hardware_type: object, identifiers: frozenset[str]
) -> ModuleRow | None:
    name = _shown(product_name, MAX_NAME_LENGTH, identifiers)
    if name is None:
        return None  # the gateway, a user interface or an unnamed module
    return ModuleRow(name, _shown(hardware_type, MAX_HARDWARE_LENGTH, identifiers))


def rows_from_cloud(values: Iterable[object]) -> tuple[ModuleRow, ...]:
    """The modules with a product name of a cloud ``/system/info`` answer."""
    rows: list[ModuleRow] = []
    for number, item in enumerate(values):
        if number >= MAX_ROWS:
            break
        if not isinstance(item, Mapping):
            continue
        identifiers = _identifiers(item.get(key) for key in _SERIAL_FIELDS)
        row = _row(item.get("ProductName"), item.get("ModuleHwIdentStr"), identifiers)
        if row is not None:
            rows.append(row)
    return tuple(rows)


def rows_from_local(modules: Iterable[object]) -> tuple[ModuleRow, ...]:
    """The modules with a product name of a decoded local system answer."""
    rows: list[ModuleRow] = []
    for number, module in enumerate(modules):
        if number >= MAX_ROWS:
            break
        identifiers = _identifiers(
            (
                getattr(module, "serial_number", None),
                getattr(module, "product_serial", None),
            )
        )
        row = _row(
            getattr(module, "product_name", None),
            getattr(module, "hardware", None),
            identifiers,
        )
        if row is not None:
            rows.append(row)
    return tuple(rows)


# --------------------------------------------------------------- data table


@dataclass(frozen=True, slots=True)
class TableEntry:
    """One table entry: what it matches and what it proves."""

    product_name: re.Pattern[str] | None
    hardware_type: str | None
    meaning: Mapping[str, object]
    examples: tuple[ModuleRow, ...]

    @property
    def specificity(self) -> int:
        """Both conditions before a product name before a hardware type."""
        return (2 if self.product_name is not None else 0) + (
            1 if self.hardware_type is not None else 0
        )

    @property
    def names_product(self) -> bool:
        return self.product_name is not None

    def matches(self, row: ModuleRow) -> bool:
        if self.hardware_type is not None and row.hardware_type != self.hardware_type:
            return False
        return self.product_name is None or (
            row.product_name is not None
            and self.product_name.fullmatch(row.product_name) is not None
        )


@dataclass(frozen=True, slots=True)
class ModelTable:
    version: int
    constructions: frozenset[str]
    brand_families: frozenset[str]
    # Most specific first, in file order within the same specificity.
    entries: tuple[TableEntry, ...]


def _optional_text(value: object, allowed: frozenset[str] | None = None) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value or len(value) > _MAX_FAMILY_LENGTH:
        return False
    return allowed is None or value in allowed


def _power(value: object) -> bool:
    return value is None or (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 < value <= _MAX_POWER_KW
    )


def _example(value: object) -> ModuleRow:
    if not isinstance(value, Mapping) or set(value) - {"product_name", "hardware_type"}:
        raise ValueError("invalid_example")
    product_name = value.get("product_name")
    hardware_type = value.get("hardware_type")
    if clean_text(product_name) != product_name or product_name is None:
        raise ValueError("invalid_example")
    if hardware_type is not None and (
        clean_text(hardware_type, MAX_HARDWARE_LENGTH) != hardware_type
    ):
        raise ValueError("invalid_example")
    return ModuleRow(product_name, hardware_type)


def _entry(
    item: object, constructions: frozenset[str], brand_families: frozenset[str]
) -> TableEntry:
    if not isinstance(item, Mapping) or set(item) - _ENTRY_FIELDS:
        raise ValueError("invalid_entry")
    match = item.get("match")
    if (
        not isinstance(match, Mapping)
        or not match
        or set(match) - {"product_name", "hardware_type"}
    ):
        raise ValueError("invalid_match")
    pattern_text = match.get("product_name")
    hardware_type = match.get("hardware_type")
    pattern = None
    if pattern_text is not None:
        if not isinstance(pattern_text, str) or not pattern_text:
            raise ValueError("invalid_match")
        pattern = re.compile(pattern_text)
    if hardware_type is not None and (
        not isinstance(hardware_type, str)
        or clean_text(hardware_type, MAX_HARDWARE_LENGTH) != hardware_type
    ):
        raise ValueError("invalid_match")
    role = item.get("role")
    if role is not None and role not in ROLES:
        raise ValueError("invalid_role")
    if not (
        _optional_text(item.get("family"))
        and _optional_text(item.get("construction"), constructions)
        and _optional_text(item.get("brand_family"), brand_families)
        and _power(item.get("power_kw"))
    ):
        raise ValueError("invalid_meaning")
    meaning = {key: item[key] for key in _MEANING_FIELDS if item.get(key) is not None}
    if not meaning:
        raise ValueError("entry_without_meaning")
    examples = item.get("examples")
    evidence = item.get("evidence")
    if not isinstance(examples, list) or not examples:
        raise ValueError("entry_without_examples")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("entry_without_evidence")
    entry = TableEntry(
        pattern,
        hardware_type,
        meaning,
        tuple(_example(example) for example in examples),
    )
    if not all(entry.matches(example) for example in entry.examples):
        raise ValueError("example_not_matched")
    return entry


def parse_table(data: object) -> ModelTable:
    """Validate the table; any fault rejects the whole table."""
    if not isinstance(data, Mapping) or data.get("schema") != TABLE_SCHEMA:
        raise ValueError("unsupported_table")
    version = data.get("table_version")
    constructions = data.get("constructions")
    brand_families = data.get("brand_families")
    entries = data.get("entries")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
        or not isinstance(constructions, list)
        or not isinstance(brand_families, list)
        or not isinstance(entries, list)
        or not all(isinstance(item, str) and item for item in constructions)
        or not all(isinstance(item, str) and item for item in brand_families)
    ):
        raise ValueError("invalid_table")
    allowed_constructions = frozenset(constructions)
    allowed_brands = frozenset(brand_families)
    parsed = [_entry(item, allowed_constructions, allowed_brands) for item in entries]
    ordered = sorted(
        enumerate(parsed), key=lambda pair: (-pair[1].specificity, pair[0])
    )
    return ModelTable(
        version,
        allowed_constructions,
        allowed_brands,
        tuple(entry for _, entry in ordered),
    )


def load_table(path: Path = TABLE_PATH) -> ModelTable:
    return parse_table(json.loads(path.read_text(encoding="utf-8")))


# Read once on import (in the import executor), never in the event loop.
TABLE: Final = load_table()


# ------------------------------------------------------------ interpretation


@dataclass(frozen=True, slots=True)
class Generator:
    """One heat generator module and what the table proves about it."""

    role: str
    product_name: str
    hardware_type: str | None
    family: str | None
    construction: str | None
    power_kw: float | int | None
    brand_family: str | None
    # A table entry for its product proves its meaning.
    known: bool

    def attribute(self) -> dict[str, object]:
        return {
            "role": self.role,
            "product_name": self.product_name,
            "hardware_type": self.hardware_type,
            "family": self.family,
            "construction": self.construction,
            "power_kw": self.power_kw,
        }

    def sort_key(self) -> tuple[int, str, str, str]:
        return (
            ROLES.index(self.role),
            self.product_name.casefold(),
            self.product_name,
            self.hardware_type or "",
        )


def interpret_row(row: ModuleRow, table: ModelTable = TABLE) -> Generator:
    """The meaning of one module: per field, the most specific entry that
    sets it; a module no entry describes is ``other`` without meaning."""
    meaning: dict[str, object] = {}
    known = False
    for entry in table.entries:
        if not entry.matches(row):
            continue
        known = known or entry.names_product
        for key, value in entry.meaning.items():
            meaning.setdefault(key, value)
    power = meaning.get("power_kw")
    return Generator(
        role=str(meaning.get("role", OTHER)),
        product_name=row.product_name or "",
        hardware_type=row.hardware_type,
        family=_text_or_none(meaning.get("family")),
        construction=_text_or_none(meaning.get("construction")),
        power_kw=power if isinstance(power, int | float) else None,
        brand_family=_text_or_none(meaning.get("brand_family")),
        known=known,
    )


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class HeatGenerators:
    """The heat generators of one answer, in their stable order."""

    rows: tuple[ModuleRow, ...]
    generators: tuple[Generator, ...]
    source: str
    table_version: int
    # When the answer was read; not part of what is shown.
    observed_at: datetime | None = field(default=None, compare=False)

    @property
    def primary(self) -> Generator:
        return self.generators[0]

    @property
    def known(self) -> bool:
        return all(item.known for item in self.generators)

    @property
    def state(self) -> str:
        return self.primary.product_name

    @property
    def outdoor_unit(self) -> str | None:
        return next(
            (
                item.product_name
                for item in self.generators
                if item.role == "outdoor_unit"
            ),
            None,
        )

    def attributes(self) -> dict[str, object]:
        primary = self.primary
        return {
            "heat_generators": [
                item.attribute() for item in self.generators[:MAX_GENERATORS]
            ],
            "primary": {
                "family": primary.family,
                "construction": primary.construction,
                "power_kw": primary.power_kw,
                "brand_family": primary.brand_family,
            },
            "outdoor_unit": self.outdoor_unit,
            "source": self.source,
            "table_version": self.table_version,
            "known": self.known,
        }

    def same_modules(self, other: HeatGenerators) -> bool:
        return self.rows == other.rows

    def to_data(self) -> dict[str, object]:
        """What a restart keeps: the read fields and the source, no meaning
        (it is worked out again with the table of the running version)."""
        return {
            "rows": [[row.product_name, row.hardware_type] for row in self.rows],
            "source": self.source,
        }


def interpret(
    rows: Sequence[ModuleRow],
    source: str,
    *,
    observed_at: datetime | None = None,
    table: ModelTable = TABLE,
) -> HeatGenerators | None:
    """The heat generators of one answer; ``None`` without any."""
    if source not in SOURCES:
        raise ValueError("unknown_source")
    if not rows:
        return None
    ordered = tuple(sorted(rows[:MAX_ROWS], key=_row_key))
    generators = tuple(
        sorted((interpret_row(row, table) for row in ordered), key=Generator.sort_key)
    )
    return HeatGenerators(ordered, generators, source, table.version, observed_at)


def _row_key(row: ModuleRow) -> tuple[str, str]:
    return row.product_name or "", row.hardware_type or ""


def from_data(data: object, table: ModelTable = TABLE) -> HeatGenerators | None:
    """The kept heat generators of a restart; ``None`` for anything invalid."""
    if not isinstance(data, Mapping):
        return None
    source, raw_rows = data.get("source"), data.get("rows")
    if source not in SOURCES or not isinstance(raw_rows, list):
        return None
    rows: list[ModuleRow] = []
    for item in raw_rows[:MAX_ROWS]:
        if not isinstance(item, list) or len(item) != 2:
            return None
        row = _row(item[0], item[1], frozenset())
        if row is None:
            return None
        rows.append(row)
    return interpret(rows, str(source), table=table)


def choose(
    cloud: HeatGenerators | None,
    local: HeatGenerators | None,
    last: HeatGenerators | None,
) -> tuple[HeatGenerators | None, bool]:
    """The heat generators to show, and whether the two sources disagree.

    One source alone is shown as it is. When both agree, the newer answer
    names the source. When they disagree, the answer that matches the last
    shown one stays; without such an answer the newer one is shown. Without
    any answer the last shown one stays.
    """
    if cloud is None or local is None:
        return (cloud or local or last), False
    newer, older = (local, cloud) if _newer(local, cloud) else (cloud, local)
    if cloud.same_modules(local):
        return newer, False
    if last is not None:
        for candidate in (newer, older):
            if candidate.same_modules(last):
                return candidate, True
    return newer, True


def _newer(first: HeatGenerators, second: HeatGenerators) -> bool:
    if first.observed_at is None:
        return False
    if second.observed_at is None:
        return True
    return first.observed_at > second.observed_at


def diagnostics(
    cloud: HeatGenerators | None, local: HeatGenerators | None
) -> dict[str, Any]:
    """Product names, hardware types and roles only; no serial numbers."""
    chosen, conflict = choose(cloud, local, None)

    def modules(item: HeatGenerators | None) -> list[dict[str, object]]:
        if item is None:
            return []
        return [
            {
                "role": generator.role,
                "product_name": generator.product_name,
                "hardware_type": generator.hardware_type,
            }
            for generator in item.generators
        ]

    report: dict[str, Any] = {
        "table_version": TABLE.version,
        "cloud_modules": None if cloud is None else len(cloud.rows),
        "local_modules": None if local is None else len(local.rows),
        "source": None if chosen is None else chosen.source,
        "known": None if chosen is None else chosen.known,
        "conflict": conflict,
        "modules": modules(chosen),
        # Modules without a table entry for their product: report them, so
        # the table can grow.
        "unmapped_modules": [
            item
            for item, generator in zip(
                modules(chosen),
                chosen.generators if chosen is not None else (),
                strict=True,
            )
            if not generator.known
        ],
    }
    if conflict:
        other = local if chosen is cloud else cloud
        report["other_source_modules"] = modules(other)
    return report
