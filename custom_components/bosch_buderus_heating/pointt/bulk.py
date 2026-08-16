"""Bulk request path normalization and chunking."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from .const import MAX_BULK_PATHS


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
