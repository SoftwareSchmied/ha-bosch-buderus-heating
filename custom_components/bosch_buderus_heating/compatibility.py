"""Capability-based PointT schema compatibility checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, FIRMWARE_ISSUE_PREFIX
from .coordinator import BoschBuderusDataUpdateCoordinator
from .pointt import Resource


@dataclass(frozen=True, slots=True)
class _SchemaRule:
    pattern: re.Pattern[str]
    resource_type: str
    unit: str | None = None


_SCHEMA_RULES = (
    _SchemaRule(re.compile(r"^/heatingCircuits/[^/]+/operationMode$"), "stringValue"),
    _SchemaRule(re.compile(r"^/dhwCircuits/[^/]+/operationMode$"), "stringValue"),
    _SchemaRule(
        re.compile(r"^/dhwCircuits/[^/]+/temperatureLevels/(?:eco|high|low)$"),
        "floatValue",
        "C",
    ),
    _SchemaRule(
        re.compile(r"^/heatSources/(?:actualSupplyTemperature|returnTemperature)$"),
        "floatValue",
        "C",
    ),
    _SchemaRule(re.compile(r"^/heatSources/systemPressure$"), "floatValue", "bar"),
    _SchemaRule(
        re.compile(r"^/heatSources/emon/(?:total|ch|dhw|cooling)Consumption$"),
        "emonValue",
    ),
    _SchemaRule(
        re.compile(r"^/system/sensors/temperatures/outdoor_t1$"),
        "floatValue",
        "C",
    ),
)


def incompatible_capabilities(resources: dict[str, Resource]) -> tuple[str, ...]:
    """Return known paths whose live schema no longer matches safe expectations."""
    conflicts: list[str] = []
    for path, resource in resources.items():
        for rule in _SCHEMA_RULES:
            if not rule.pattern.fullmatch(path):
                continue
            metadata = resource.metadata
            if metadata.resource_type != rule.resource_type or (
                rule.unit is not None and metadata.unit != rule.unit
            ):
                conflicts.append(path)
            break
    return tuple(sorted(conflicts))


def sync_firmware_compatibility_issue(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    coordinators: tuple[BoschBuderusDataUpdateCoordinator, ...],
) -> None:
    """Create one repair only when a known PointT capability changed shape."""
    issue_id = f"{FIRMWARE_ISSUE_PREFIX}{entry.entry_id}"
    conflicts = sum(
        len(incompatible_capabilities(coordinator.resources))
        for coordinator in coordinators
    )
    if conflicts == 0:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="incompatible_firmware",
        translation_placeholders={"conflict_count": str(conflicts)},
    )
