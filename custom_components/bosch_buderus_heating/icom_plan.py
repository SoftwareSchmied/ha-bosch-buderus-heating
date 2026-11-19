"""Which resources a local iCom access reads, and from which circuits.

An iCom gateway offers the same resource tree as the cloud passes on to it.
The local access reads only the readings the cloud pair catalog lists
(observations.PAIR_CATALOG): they replace the cloud's value of the same path
when the type and unit agree and the user chose local values (or the cloud is
not usable). Any other path would become a local-only entity, which an iCom
access never creates. The circuits of the installation come from the
directories of the gateway, so no number is assumed.

Pure logic: no Home Assistant import, no clock, no request.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from .local_api.catalog import ReadRequest, request_for_path
from .local_api.errors import LocalResourceError
from .observations import PAIR_CATALOG

UUID_PATH: Final = "/gateway/uuid"
# The directories that name the circuits, with the leaf name of their members.
DIRECTORIES: Final[Mapping[str, tuple[str, str]]] = {
    "hc": ("/heatingCircuits", "hc"),
    "dhw": ("/dhwCircuits", "dhw"),
    "hs": ("/heatSources", "hs"),
}
# A defensive bound per kind of circuit, not a device limit.
MAX_CIRCUITS: Final = 16
_ID = re.compile(r"[a-z]+[0-9]{1,3}")


def circuit_ids(document: object, directory: str, prefix: str) -> tuple[str, ...]:
    """The circuit IDs a directory lists (for example "hc1", "hc2").

    Only direct members named like the prefix and a number count; every other
    entry (settings of the directory itself, sub-directories) is ignored.
    """
    if not isinstance(document, Mapping):
        return ()
    references = document.get("references")
    if not isinstance(references, list):
        return ()
    found: list[str] = []
    for item in references:
        identifier = item.get("id") if isinstance(item, Mapping) else None
        if not isinstance(identifier, str) or not identifier.startswith(
            directory + "/"
        ):
            continue
        name = identifier[len(directory) + 1 :]
        if (
            "/" in name
            or not _ID.fullmatch(name)
            or not name.startswith(prefix)
            or name in found
        ):
            continue
        found.append(name)
    return tuple(sorted(found, key=_number))[:MAX_CIRCUITS]


def _number(name: str) -> int:
    return int(re.sub(r"[^0-9]", "", name) or 0)


@dataclass(frozen=True, slots=True)
class PathSet:
    """The concrete readings for the circuits found."""

    requests: tuple[ReadRequest, ...]


def readings(
    heating: Iterable[str] = (),
    hot_water: Iterable[str] = (),
    sources: Iterable[str] = (),
) -> PathSet:
    """The requests of all pair paths for the given circuits.

    A path with a circuit placeholder is expanded for each circuit of its
    kind; with no circuit of a kind the placeholder paths are left out. A path
    the request catalog does not know is skipped (it cannot be decoded).
    """
    circuits: dict[str, tuple[str, ...]] = {
        "{hc}": tuple(heating),
        "{dhw}": tuple(hot_water),
        "{hs}": tuple(sources),
    }
    paths: dict[str, None] = {}
    for pair in PAIR_CATALOG:
        variants = [pair.template]
        for placeholder, ids in circuits.items():
            if placeholder in pair.template:
                variants = [
                    template.replace(placeholder, identifier)
                    for template in variants
                    for identifier in ids
                ]
        for path in variants:
            paths.setdefault(path, None)
    requests: list[ReadRequest] = []
    for path in paths:
        try:
            requests.append(request_for_path(path))
        except LocalResourceError:
            continue
    return PathSet(tuple(requests))


def document_value(document: Mapping[str, Any]) -> object:
    """The "value" of a document, None when it has none."""
    return document.get("value")
