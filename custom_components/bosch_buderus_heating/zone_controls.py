"""Narrow contracts for ConnectKey heating and cooling zone controls."""

from __future__ import annotations

import math
import re
from functools import lru_cache

from .i18n import localize
from .pointt import Resource
from .resource_catalog import additional_app_value

ZONE_MODE_PATTERN = r"^/zones/zone[^/:]+/(?:heating|cooling)/operationMode$"
ZONE_MANUAL_PATTERN = r"^/zones/zone[^/:]+/(?:heating|cooling)/manualRoomSetpoint$"
_ZONE_TEMPORARY = re.compile(
    r"^/zones/zone[^/:]+/(?:heating|cooling)/temporaryRoomSetpoint$"
)
ZONE_MODES = frozenset({"off", "manual", "auto"})
_ZONE_CONTROLS = (re.compile(ZONE_MODE_PATTERN), re.compile(ZONE_MANUAL_PATTERN))


@lru_cache(maxsize=4096)
def is_zone_control(path: str) -> bool:
    """Keep RRC, legacy paths and temporary schedule overrides outside."""
    return any(pattern.fullmatch(path) for pattern in _ZONE_CONTROLS)


def is_zone_temporary(path: str) -> bool:
    """Recognize the read-only override without promising its lifecycle."""
    return _ZONE_TEMPORARY.fullmatch(path) is not None


def zone_control_issue(resource: Resource) -> str | None:
    """Validate each response using the same scalar rules as its sensor."""
    if resource.has_values:
        return "unsupported_schema"
    if additional_app_value(resource) is None:
        return "invalid_zone_reading"
    if resource.path.endswith("/operationMode"):
        if resource.value not in ZONE_MODES:
            return "unknown_zone_mode"
        if resource.value not in resource.metadata.allowed_values:
            return "current_option_not_advertised"
    elif not resource.metadata.step_size_valid or (
        resource.metadata.step_size is not None
        and (
            not math.isfinite(resource.metadata.step_size)
            or resource.metadata.step_size <= 0
        )
    ):
        return "invalid_step"
    return None


def zone_control_name(path: str, language: str) -> str | None:
    """Distinguish a new control from its existing, unchanged reading."""
    if not is_zone_control(path):
        return None
    heating = "/heating/" in path
    mode = path.endswith("/operationMode")
    prefix = (
        localize(language, "Heizen", "Heating")
        if heating
        else localize(language, "Kühlen", "Cooling")
    )
    action = (
        localize(language, "Betriebsart einstellen", "Set operating mode")
        if mode
        else localize(
            language, "Manuellen Sollwert einstellen", "Set manual temperature"
        )
    )
    return f"{prefix}: {action}"
