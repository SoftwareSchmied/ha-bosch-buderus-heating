"""Shared read contracts for duration counters and resource collections."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .pointt import Resource
from .unit_contracts import NUMBER_TYPES

RUNTIME_PARTS = ("total", "ch", "cooling", "dhw")
_SOURCE_RUNTIME = re.compile(r"^/heatSources/[^/:]+/(workingTime|operationHours)$")
_MONITOR_COLLECTION = re.compile(r"^/dhwCircuits/[^/:]+/monitorValues$")
_TIME_UNITS = {
    **dict.fromkeys(("s", "sec", "secs", "seconds"), ("s", 1 / 3600)),
    **dict.fromkeys(("m", "min", "mins", "minutes"), ("min", 1 / 60)),
    **dict.fromkeys(("h", "hour", "hours"), ("h", 1.0)),
}


def is_monitor_collection(path: str) -> bool:
    """The DHW monitor endpoint contains individual resource references."""
    return _MONITOR_COLLECTION.fullmatch(path) is not None


def is_runtime_path(path: str) -> bool:
    """Match reviewed duration leaves without selecting unrelated counters."""
    return (
        path == "/heatSources/workingTime/totalSystem"
        or _SOURCE_RUNTIME.fullmatch(path) is not None
    )


@dataclass(frozen=True, slots=True)
class DurationReading:
    """A duration in hours or a fixed reason for its absence."""

    value: float | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeAssessment:
    """One decision for the catalog, sensor values and redacted diagnostics."""

    supported: bool
    reason: str | None = None
    source_unit: str | None = None
    unit_source: str = "unresolved"
    scale: float | None = None
    readings: tuple[tuple[str | None, DurationReading], ...] = ()

    def reading(self, key: str | None) -> DurationReading:
        """Keep existing identities unknown when their current form is absent."""
        return next(
            (reading for part, reading in self.readings if part == key),
            DurationReading(reason=self.reason or "unsupported_part"),
        )


def _duration(value: object, resource: Resource, scale: float) -> DurationReading:
    if not resource.metadata.numeric_error_states_valid:
        return DurationReading(reason="invalid_error_metadata")
    if value is None:
        return DurationReading(reason="missing_value")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return DurationReading(reason="not_numeric")
    if isinstance(value, float) and not math.isfinite(value):
        return DurationReading(reason="not_finite")
    if value in resource.metadata.numeric_error_values:
        return DurationReading(reason="device_error_value")
    if not 0 <= value <= 2**53 - 1:
        return DurationReading(reason="out_of_range")
    return DurationReading(float(value * scale))


def assess_runtime(resource: Resource) -> RuntimeAssessment:
    """Interpret duration units on every update; never reuse an old scale."""
    if not is_runtime_path(resource.path):
        return RuntimeAssessment(False, "not_runtime")
    multipart = resource.path.endswith("/workingTime")
    expected = (None, "emonValue") if multipart else tuple(sorted(NUMBER_TYPES))
    if resource.references or resource.metadata.resource_type not in expected:
        return RuntimeAssessment(False, "unsupported_schema")
    if not multipart and (
        resource.has_values
        or resource.values
        or isinstance(resource.value, dict | list)
    ):
        return RuntimeAssessment(False, "unsupported_schema")

    unit = resource.metadata.unit
    unit_source = "metadata"
    if unit is None or not unit.strip():
        if resource.path.endswith("/operationHours"):
            unit, unit_source = "h", "operation_hours_contract"
        elif multipart:
            # Retain the established unitless multipart working-time contract.
            unit, unit_source = "s", "legacy_working_time_contract"
        else:
            return RuntimeAssessment(False, "missing_unit")
    conversion = _TIME_UNITS.get(unit)
    if conversion is None:
        return RuntimeAssessment(False, "unsupported_unit")
    source_unit, scale = conversion
    readings: tuple[tuple[str | None, DurationReading], ...]
    if not multipart:
        readings = (
            (
                None,
                _duration(
                    resource.value if resource.has_value else None, resource, scale
                ),
            ),
        )
    else:
        items = (
            (resource.value, *resource.values)
            if resource.value is not None
            else resource.values
        )
        parts: list[tuple[str | None, DurationReading]] = []
        for key in RUNTIME_PARTS:
            if any(not isinstance(item, dict) for item in items):
                reading = DurationReading(reason="invalid_structure")
            elif resource.value is not None and (
                resource.has_values or resource.values
            ):
                reading = DurationReading(reason="mixed_structure")
            else:
                values = [
                    _duration(item[key], resource, scale)
                    for item in items
                    if isinstance(item, dict) and key in item
                ]
                if not values:
                    reading = DurationReading(reason="missing_value")
                elif errors := sorted({item.reason for item in values if item.reason}):
                    reading = DurationReading(reason=errors[0])
                elif len({item.value for item in values}) > 1:
                    reading = DurationReading(reason="conflicting_values")
                else:
                    reading = values[0]
            parts.append((key, reading))
        readings = tuple(parts)
    return RuntimeAssessment(
        True,
        source_unit=source_unit,
        unit_source=unit_source,
        scale=scale,
        readings=readings,
    )
