"""Validated multipart energy readings in the existing kWh entity unit."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .pointt import Resource
from .unit_contracts import ENERGY_UNITS, energy_kwh_scale


@dataclass(frozen=True, slots=True)
class EnergyReading:
    """One part, with a bounded reason instead of an invented fallback value."""

    value: float | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class EnergyAssessment:
    """The same interpretation for sensors and redacted diagnostics."""

    source_unit: str | None
    unit_source: str
    scale: float | None
    reason: str | None
    readings: tuple[tuple[str, EnergyReading], ...]


def is_energy_collection(resource: Resource) -> bool:
    return resource.metadata.resource_type == "emonValue" and "/emon/" in resource.path


# A heat source's own collections (``/heatSources/hs2/emon/totalConsumption``).
_HEAT_SOURCE_COLLECTION = re.compile(r"/heatSources/hs\d+/emon/[^/]+")


def compressor_is_electricity(resource: Resource) -> bool:
    """The compressor part alone is the electricity of the collection.

    So it is for a heat source's own collection that reports no electric
    heater part at all: the heat pump of a hybrid system has no heater
    (#138). A reported heater part, valid or not, still counts; the
    gateway's collections (``/heatSources/emon/...``) always need it.
    """
    if _HEAT_SOURCE_COLLECTION.fullmatch(resource.path) is None:
        return False
    if not is_energy_collection(resource):
        return False
    return all(key != "eheater" for key, _ in assess_energy(resource).readings)


def _reading(value: object, resource: Resource, scale: float) -> EnergyReading:
    if not resource.metadata.numeric_error_states_valid:
        return EnergyReading(reason="invalid_error_metadata")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return EnergyReading(reason="not_numeric")
    if isinstance(value, float) and not math.isfinite(value):
        return EnergyReading(reason="not_finite")
    if not 0 <= value <= 2**53 - 1:
        return EnergyReading(reason="out_of_range")
    if value in resource.metadata.numeric_error_values:
        return EnergyReading(reason="device_error_value")
    return EnergyReading(value * scale)


# The last assessment per path, valid for that very resource object: a
# resource never changes, and the same readings are assessed again and again.
_ASSESSED: dict[str, tuple[Resource, EnergyAssessment]] = {}
_MAX_ASSESSED = 256


def assess_energy(resource: Resource) -> EnergyAssessment:
    """Respect the collection unit; retain only the established unitless contract."""
    cached = _ASSESSED.get(resource.path)
    if cached is not None and cached[0] is resource:
        return cached[1]
    assessment = _assess_energy(resource)
    if resource.path not in _ASSESSED and len(_ASSESSED) >= _MAX_ASSESSED:
        _ASSESSED.clear()
    _ASSESSED[resource.path] = (resource, assessment)
    return assessment


def _assess_energy(resource: Resource) -> EnergyAssessment:
    unit = resource.metadata.unit
    if unit is not None and not unit.strip():
        unit = None  # a blank unit is a missing one, as for the start counters
    source = "legacy_unitless_kwh" if unit is None else "metadata"
    scale = 1.0 if unit is None else energy_kwh_scale(unit)
    reason = None if scale is not None else "unsupported_unit"
    if not is_energy_collection(resource) or resource.references:
        reason = "unsupported_schema"
    if resource.value is not None and resource.values:
        reason = "mixed_structure"
    candidates = (resource.value, *resource.values)
    parts: dict[str, list[EnergyReading]] = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        entries = (
            [(item["type"], item.get("value"))]
            if isinstance(item.get("type"), str)
            else list(item.items())
        )
        # No reviewed contract allows independent units within a collection.
        part_reason = reason or (
            "unsupported_part_unit"
            if "unit" in item or "unitOfMeasure" in item
            else None
        )
        for key, value in entries:
            if key in {"type", "unit", "unitOfMeasure"}:
                continue
            reading = (
                EnergyReading(reason=part_reason)
                if part_reason
                else _reading(value, resource, scale or 1.0)
            )
            parts.setdefault(str(key), []).append(reading)
    readings: list[tuple[str, EnergyReading]] = []
    for key, items in sorted(parts.items()):
        if errors := sorted({item.reason for item in items if item.reason}):
            reading = EnergyReading(reason=errors[0])
        elif len({item.value for item in items}) > 1:
            reading = EnergyReading(reason="conflicting_values")
        else:
            reading = items[0]
        readings.append((key, reading))
    return EnergyAssessment(
        "kWh" if unit is None else unit if unit in ENERGY_UNITS else None,
        source,
        scale,
        reason,
        tuple(readings),
    )


def energy_diagnostics(resource: Resource) -> dict[str, object]:
    """Omit readings and all dynamic part names from the diagnostic export."""
    assessment = assess_energy(resource)
    return {
        "source_unit": assessment.source_unit,
        "unit_source": assessment.unit_source,
        "output_unit": "kWh",
        "scale": assessment.scale,
        "rejection_reason": assessment.reason,
        "valid_parts": sum(item.value is not None for _, item in assessment.readings),
        "invalid_parts": dict(
            sorted(
                Counter(
                    item.reason for _, item in assessment.readings if item.reason
                ).items()
            )
        ),
    }
