"""Independent, read-only start counters with bounded numeric validation."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .pointt.bulk import normalize_resource_path
from .pointt.models import Resource, ResourceMetadata

GLOBAL_START_COUNTER = "/heatSources/numberOfStarts"
START_COUNTER_KEYS = ("total", "ch", "cooling", "dhw", "pool")
# C4 (revised): always offered, but disabled by default; not every system
# heats a pool.
OPTIONAL_START_COUNTER_KEY = "pool"
MAX_START_COUNT = 2**53 - 1  # Preserve exact integers in downstream numeric storage.
# Some controllers send the parts as a short list of objects in `value`
# (for example one object with total, heating and hot water). More parts than
# this are not a counter form.
MAX_START_COUNTER_PARTS = 8
_SOURCE_PATH = re.compile(r"^/heatSources/[^/:]+/numberOfStarts$")


def is_start_counter_path(path: str) -> bool:
    """Recognize exact counter leaves without inventing heat-source IDs."""
    if path != GLOBAL_START_COUNTER and _SOURCE_PATH.fullmatch(path) is None:
        return False
    try:
        return normalize_resource_path(path) == path
    except ValueError:
        return False


def registered_start_counter_path(unique_id: str, gateway_id: str) -> str | None:
    """Decode only this gateway's existing, supported scalar entity identities."""
    prefix = f"{gateway_id}:"
    if not unique_id.startswith(prefix):
        return None
    parts = unique_id.removeprefix(prefix).split(":")
    if parts == ["heatSources", "numberOfStarts"]:
        return GLOBAL_START_COUNTER
    if (
        len(parts) in (3, 4)
        and parts[0] == "heatSources"
        and parts[2] == "numberOfStarts"
        and (len(parts) == 3 or parts[3] in START_COUNTER_KEYS)
    ):
        path = f"/heatSources/{parts[1]}/numberOfStarts"
        return path if is_start_counter_path(path) else None
    return None


def start_counter_placeholder(path: str, *, scalar: bool = False) -> Resource:
    """Restore entity shape only; a registry entry is never a fresh reading."""
    return Resource(
        path=path,
        metadata=ResourceMetadata(
            resource_type=(
                "floatValue" if scalar or path == GLOBAL_START_COUNTER else "emonValue"
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class CounterReading:
    """A validated count or a fixed, privacy-safe reason for its absence."""

    value: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class StartCounterAssessment:
    """One shared decision for entity creation, values and diagnostics."""

    supported: bool
    reason: str | None = None
    readings: tuple[tuple[str | None, CounterReading], ...] = ()
    # Part keys present in at least one object of a structurally valid,
    # non-empty multipart reading; ``None`` when that is not known (scalar,
    # empty or invalid payload).
    reported_keys: frozenset[str] | None = None

    def part_reported(self, key: str | None) -> bool:
        """Whether a part occurs in the reading; unknown counts as present."""
        return self.reported_keys is None or key is None or key in self.reported_keys

    def reading(self, key: str | None) -> CounterReading:
        """Keep a previously created entity unknown after a schema change."""
        return next(
            (reading for part, reading in self.readings if part == key),
            CounterReading(reason=self.reason or "unsupported_part"),
        )


def _count(value: object, metadata: ResourceMetadata) -> CounterReading:
    if not metadata.numeric_error_states_valid:
        return CounterReading(reason="invalid_error_metadata")
    if value is None:
        return CounterReading(reason="missing_value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return CounterReading(reason="not_numeric")
    if isinstance(value, float) and not math.isfinite(value):
        return CounterReading(reason="not_finite")
    if value in metadata.numeric_error_values:
        return CounterReading(reason="device_error_value")
    if value < 0 or value > MAX_START_COUNT:
        return CounterReading(reason="out_of_range")
    if isinstance(value, float) and not value.is_integer():
        return CounterReading(reason="not_integer")
    return CounterReading(value=int(value))


def assess_start_counter(resource: Resource) -> StartCounterAssessment:
    """Validate known schemas without comparing or replacing other sources."""
    if not is_start_counter_path(resource.path):
        return StartCounterAssessment(False, "not_start_counter")
    scalar = resource.metadata.resource_type == "floatValue"
    if (
        resource.references
        or resource.metadata.resource_type not in ("floatValue", "emonValue")
        or (resource.path == GLOBAL_START_COUNTER and not scalar)
    ):
        return StartCounterAssessment(False, "unsupported_schema")
    # Some PointT gateways declare a blank, whitespace-only unit.
    # Treat whitespace as absent while still rejecting real measurement units.
    unit = resource.metadata.unit
    if unit is not None and unit.strip() not in ("", "other"):
        return StartCounterAssessment(False, "unsupported_unit")
    if scalar:
        if resource.values or resource.has_values:
            return StartCounterAssessment(False, "unsupported_schema")
        return StartCounterAssessment(
            True, readings=((None, _count(resource.value, resource.metadata)),)
        )

    # The parser normalizes an object in `values` to a one-item tuple. Retain
    # the existing object-in-`value` form too, but never manufacture totals.
    # A list in `value` holds the parts directly, bounded in length.
    raw = resource.value
    head: tuple[object, ...]
    if isinstance(raw, list):
        head = tuple(raw) if 0 < len(raw) <= MAX_START_COUNTER_PARTS else (raw,)
    elif raw is not None:
        head = (raw,)
    else:
        head = ()
    items = (*head, *resource.values)
    if any(not isinstance(item, dict) for item in items):
        return StartCounterAssessment(
            True,
            readings=tuple(
                (key, CounterReading(reason="invalid_structure"))
                for key in START_COUNTER_KEYS
            ),
        )
    readings: list[tuple[str | None, CounterReading]] = []
    for key in START_COUNTER_KEYS:
        parts = [
            _count(item[key], resource.metadata)
            for item in items
            if isinstance(item, dict) and key in item
        ]
        if not parts:
            reading = _count(None, resource.metadata)
        elif errors := sorted({part.reason for part in parts if part.reason}):
            reading = CounterReading(reason=errors[0])
        elif len({part.value for part in parts}) > 1:
            reading = CounterReading(reason="conflicting_values")
        else:
            reading = parts[0]
        readings.append((key, reading))
    reported = (
        frozenset(
            key
            for key in START_COUNTER_KEYS
            for item in items
            if isinstance(item, dict) and key in item
        )
        if items
        else None
    )
    return StartCounterAssessment(
        True, readings=tuple(readings), reported_keys=reported
    )


_KEY_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,23}")
_MAX_KEYS = 12
_MAX_ITEMS = 5


def value_shape(value: object, depth: int = 0) -> str:
    """A value-free form of one item: types, list lengths and key names
    (an unusual key becomes ``?``), never a value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list | tuple):
        if depth >= 2:
            return f"list[{len(value)}]"
        kinds = sorted({value_shape(item, depth + 1) for item in value[:_MAX_ITEMS]})
        return f"list[{len(value)}]<{'|'.join(kinds)}>"
    if isinstance(value, dict):
        keys = sorted(
            key if isinstance(key, str) and _KEY_TOKEN.fullmatch(key) else "?"
            for key in value
        )
        if depth >= 2:
            return f"dict[{len(keys)}]"
        fields = ",".join(
            f"{key}:{value_shape(value[key], depth + 1)}" if key != "?" else "?"
            for key in keys[:_MAX_KEYS]
        )
        more = ",..." if len(keys) > _MAX_KEYS else ""
        return f"dict{{{fields}{more}}}"
    return "other"


def structure_summary(resource: Resource) -> dict[str, object] | None:
    """T3: the form of an ``emonValue`` counter whose parts read as
    ``invalid_structure``, for the diagnostics; ``None`` otherwise.

    Only types, list lengths and key names; no counts or other values, so
    that the real form can be learned from a diagnostics download.
    """
    assessment = assess_start_counter(resource)
    if not any(
        reading.reason == "invalid_structure" for _key, reading in assessment.readings
    ):
        return None
    return {
        "reason": "item_not_object",
        "value": value_shape(resource.value),
        "values_count": len(resource.values),
        "values": [value_shape(item) for item in resource.values[:_MAX_ITEMS]],
    }
