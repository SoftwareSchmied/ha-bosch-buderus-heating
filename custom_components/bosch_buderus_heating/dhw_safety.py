"""Read-only WDDW2 safety-temperature state, based on its string value."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .pointt.bulk import normalize_resource_path
from .pointt.models import Resource

_PATH = re.compile(r"^/dhwCircuits/[^/:]+/safetyTemperature$")


def is_dhw_safety_path(path: str) -> bool:
    """Recognize only a canonical circuit leaf, never invent a circuit."""
    if _PATH.fullmatch(path) is None:
        return False
    try:
        return normalize_resource_path(path) == path
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class DhwSafetyAssessment:
    """Share schema and state decisions without exposing the raw value."""

    supported: bool
    value: bool | None = None
    reason: str | None = None


def assess_dhw_safety(resource: Resource) -> DhwSafetyAssessment:
    """Interpret only the known on/off codes, never a temperature."""
    if not is_dhw_safety_path(resource.path):
        return DhwSafetyAssessment(False, reason="not_dhw_safety")
    if (
        resource.metadata.resource_type != "stringValue"
        or resource.references
        or resource.values
        or resource.has_values
        or isinstance(resource.value, dict | list)
    ):
        return DhwSafetyAssessment(False, reason="unsupported_schema")
    unit = resource.metadata.unit
    if unit is not None and unit.strip() not in ("", "other"):
        return DhwSafetyAssessment(False, reason="unsupported_unit")
    if resource.value is None:
        return DhwSafetyAssessment(True, reason="missing_value")
    if not isinstance(resource.value, str):
        return DhwSafetyAssessment(True, reason="not_string")
    if resource.value not in ("on", "off"):
        return DhwSafetyAssessment(True, reason="unknown_state")
    return DhwSafetyAssessment(True, value=resource.value == "on")
