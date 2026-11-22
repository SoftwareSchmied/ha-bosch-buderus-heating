"""The rows of a system's component list, with every field checked.

The cloud's ``/system/info`` and the local ``/system/basicInfo`` list the
components of a system, one row each. A row gives the type number of the
component (``Id``), its place (``Tok``, the place number on the bus), its
software version, product name, hardware type, update type, software
identifier and the article number of its model. Every field is checked and
kept only in a safe form; a field that does not pass is left out.

Never read: serial numbers (``No``, ``ProductSerialNumber``,
``ModuleSerialNumber``) and ``ProductUuid``. A text equal to one of them
(a serial number in a name or version field) is left out as well.

Pure logic without Home Assistant imports.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from .heat_generator import clean_text

MAX_ROWS: Final = 64
MAX_TEXT_LENGTH: Final = 64
_MAX_NUMBER: Final = 999_999
_NUMBER: Final = re.compile(r"[0-9]{1,6}")
# A version: letters, digits and the usual separators, with a digit (the
# same rule as the software version of the outdoor unit).
_VERSION: Final = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+\-]{0,63}")
_DIGIT: Final = re.compile(r"[0-9]")
# A text made only of digits from this length on is a number, not a name.
_NUMBER_ONLY: Final = re.compile(r"[0-9]{8,}")
# Technical texts of a row: letters, digits, single spaces and a few
# separators, nothing else.
_TECHNICAL: Final = re.compile(r"[0-9A-Za-z](?:[0-9A-Za-z._:+/-]| (?! )){0,63}")
_ARTICLE: Final = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,31}")
# The fields of a cloud row that hold identifiers of a single device.
IDENTIFIER_FIELDS: Final = (
    "No",
    "ProductSerialNumber",
    "ModuleSerialNumber",
    "ProductUuid",
)
# The attributes of a decoded local row that hold such identifiers.
_LOCAL_IDENTIFIERS: Final = ("serial_number", "product_serial")


@dataclass(frozen=True, slots=True)
class ComponentRow:
    """One row of a component list; every field optional and checked."""

    type_id: int | None
    tok: int | None
    version: str | None = None
    product_name: str | None = None
    hardware_type: str | None = None
    sw_update_type: str | None = None
    sw_ident: str | None = None
    article_number: str | None = None

    @property
    def key(self) -> tuple[int | None, int | None]:
        """Place and type number: what identifies the component's kind."""
        return self.tok, self.type_id

    def to_list(self) -> list[object]:
        """The stored form: place, type, then the texts."""
        return [
            self.tok,
            self.type_id,
            self.version,
            self.product_name,
            self.hardware_type,
            self.sw_update_type,
            self.sw_ident,
            self.article_number,
        ]


def component_number(value: object) -> int | None:
    """A type or place number: 1 to 6 digits, as a number or a text.

    A boolean, a fraction, an empty or negative value, or more than six
    digits is no number.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_NUMBER else None
    if isinstance(value, str) and _NUMBER.fullmatch(value.strip()):
        return int(value.strip())
    return None


def identifiers(values: Iterable[object]) -> frozenset[str]:
    """The identifiers of one row, compared without case and spacing."""
    found: set[str] = set()
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, str | int | float):
            text = " ".join(str(value).split()).casefold()
            if text:
                found.add(text)
    return frozenset(found)


def clean_version(value: object, known: frozenset[str] = frozenset()) -> str | None:
    """A version, or ``None`` for anything else (an identifier included)."""
    text = clean_text(value, MAX_TEXT_LENGTH)
    if (
        text is None
        or _VERSION.fullmatch(text) is None
        or _DIGIT.search(text) is None
        or text.casefold() in known
    ):
        return None
    return text


def clean_name(value: object, known: frozenset[str] = frozenset()) -> str | None:
    """A product name: printable, at most 64 characters, never only digits
    and never an identifier of the same row."""
    text = clean_text(value, MAX_TEXT_LENGTH)
    if text is None or _NUMBER_ONLY.fullmatch(text) is not None:
        return None
    return None if text.casefold() in known else text


def clean_token(
    value: object, known: frozenset[str] = frozenset(), *, article: bool = False
) -> str | None:
    """A technical text (hardware type, update type, software identifier,
    article number) in a safe form, or ``None``.

    A text with a control or other unprintable character (other than plain
    white space) is dropped as a whole instead of being cleaned, so that
    nothing of it is kept."""
    if isinstance(value, str) and not "".join(value.split()).isprintable():
        return None
    text = clean_text(value, MAX_TEXT_LENGTH)
    if text is None or text.casefold() in known:
        return None
    if article:
        return text if _ARTICLE.fullmatch(text) is not None else None
    if _TECHNICAL.fullmatch(text) is None or _NUMBER_ONLY.fullmatch(text) is not None:
        return None
    return text


def _article(row: Mapping[str, object], known: frozenset[str]) -> str | None:
    for key in ("ProductTtn", "ModuleTtn"):
        if (article := clean_token(row.get(key), known, article=True)) is not None:
            return article
    return None


def _from_mapping(row: Mapping[str, object]) -> ComponentRow | None:
    known = identifiers(row.get(key) for key in IDENTIFIER_FIELDS)
    type_id = component_number(row.get("Id"))
    tok = component_number(row.get("Tok"))
    if type_id is None and tok is None:
        return None
    return ComponentRow(
        type_id,
        tok,
        clean_version(row.get("Ver", row.get("version")), known),
        clean_name(row.get("ProductName"), known),
        clean_token(row.get("ModuleHwIdentStr"), known),
        clean_token(row.get("SwUpdateType"), known),
        clean_token(row.get("SwIdenStr"), known),
        _article(row, known),
    )


def _from_module(module: object) -> ComponentRow | None:
    known = identifiers(getattr(module, key, None) for key in _LOCAL_IDENTIFIERS)
    type_id = component_number(getattr(module, "id", None))
    tok = component_number(getattr(module, "token", None))
    if type_id is None and tok is None:
        return None
    return ComponentRow(
        type_id,
        tok,
        clean_version(getattr(module, "version", None), known),
        clean_name(getattr(module, "product_name", None), known),
        clean_token(getattr(module, "hardware", None), known),
    )


def parse_cloud_rows(values: Iterable[object]) -> tuple[ComponentRow, ...]:
    """The rows of a cloud ``/system/info`` answer (at most 64 are read)."""
    rows: list[ComponentRow] = []
    for number, item in enumerate(values):
        if number >= MAX_ROWS:
            break
        if isinstance(item, Mapping) and (row := _from_mapping(item)) is not None:
            rows.append(row)
    return tuple(rows)


def parse_local_rows(modules: Iterable[object]) -> tuple[ComponentRow, ...]:
    """The rows of a local ``/system/basicInfo`` answer (at most 64).

    The answer's rows use the field names of the cloud; a decoded module
    (with ``id``, ``token``, ``version``, ``hardware`` and
    ``product_name``) is read as well.
    """
    rows: list[ComponentRow] = []
    for number, item in enumerate(modules):
        if number >= MAX_ROWS:
            break
        row = _from_mapping(item) if isinstance(item, Mapping) else _from_module(item)
        if row is not None:
            rows.append(row)
    return tuple(rows)


def row_from_list(value: object) -> ComponentRow | None:
    """A stored row, checked again; ``None`` for anything invalid."""
    if not isinstance(value, list) or len(value) != 8:
        return None
    tok, type_id = component_number(value[0]), component_number(value[1])
    if (tok is None and value[0] is not None) or (
        type_id is None and value[1] is not None
    ):
        return None
    if tok is None and type_id is None:
        return None
    return ComponentRow(
        type_id,
        tok,
        clean_version(value[2]),
        clean_name(value[3]),
        clean_token(value[4]),
        clean_token(value[5]),
        clean_token(value[6]),
        clean_token(value[7], article=True),
    )


def rows_from_lists(values: object) -> tuple[ComponentRow, ...] | None:
    """Stored rows; ``None`` when the list or any row is invalid."""
    if not isinstance(values, list) or len(values) > MAX_ROWS:
        return None
    rows = [row_from_list(item) for item in values]
    if any(row is None for row in rows):
        return None
    return tuple(row for row in rows if row is not None)
