"""Bulk request path normalization and chunking."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import date

from .const import MAX_BULK_PATHS

# W5: the only query keys a resource read may carry, and their values: a
# record position (``entry``, ``next``) or a day (``date``, yyyy-MM-dd).
QUERY_KEYS = frozenset({"entry", "date", "next"})
MAX_QUERY_POSITION = 1_000_000
_QUERY_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def normalize_resource_path(path: str) -> str:
    """Normalize a PointT resource path and reject unsafe input."""
    normalized = path.strip()
    if normalized.startswith("/resource/"):
        normalized = normalized.removeprefix("/resource")
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if (
        normalized == "/"
        or "?" in normalized
        or "#" in normalized
        or "://" in normalized
        or any(segment in {".", ".."} for segment in normalized.split("/"))
    ):
        raise ValueError("Invalid PointT resource path")
    return normalized


def normalize_query(query: Mapping[str, str | int]) -> tuple[tuple[str, str], ...]:
    """Validate the query of a resource read; sorted, as text.

    Only ``entry``/``next`` with a non-negative integer and ``date`` with a
    real calendar day are accepted; anything else could reach other API
    parts or leak into logs.
    """
    if not query or not set(query) <= QUERY_KEYS:
        raise ValueError("Invalid PointT resource query")
    items: list[tuple[str, str]] = []
    for key in sorted(query):
        value = query[key]
        if key == "date":
            if not isinstance(value, str) or not _QUERY_DATE.fullmatch(value):
                raise ValueError("Invalid PointT resource query")
            try:
                date.fromisoformat(value)
            except ValueError as err:
                raise ValueError("Invalid PointT resource query") from err
            items.append((key, value))
        else:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_QUERY_POSITION
            ):
                raise ValueError("Invalid PointT resource query")
            items.append((key, str(value)))
    return tuple(items)


def chunk_resource_paths(
    paths: Iterable[str], *, size: int = MAX_BULK_PATHS
) -> Iterator[tuple[str, ...]]:
    """Yield normalized resource paths in bounded chunks."""
    if size < 1 or size > MAX_BULK_PATHS:
        raise ValueError(f"Bulk chunk size must be between 1 and {MAX_BULK_PATHS}")

    chunk: list[str] = []
    for path in paths:
        chunk.append(normalize_resource_path(path))
        if len(chunk) == size:
            yield tuple(chunk)
            chunk.clear()
    if chunk:
        yield tuple(chunk)
