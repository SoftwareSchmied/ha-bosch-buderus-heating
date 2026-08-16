"""Tests for capability-based firmware compatibility checks."""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.compatibility import (
    incompatible_capabilities,
    sync_firmware_compatibility_issue,
)
from custom_components.bosch_buderus_heating.const import (
    DOMAIN,
    FIRMWARE_ISSUE_PREFIX,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata


def _resource(path: str, resource_type: str, unit: str | None = None) -> Resource:
    return Resource(
        path=path,
        value=1.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type=resource_type, unit=unit),
    )


def test_compatibility_checks_only_known_live_schemas() -> None:
    compatible = _resource("/dhwCircuits/dhw1/temperatureLevels/eco", "floatValue", "C")
    changed_type = _resource(
        "/dhwCircuits/dhw1/temperatureLevels/high", "stringValue", "C"
    )
    changed_unit = _resource("/heatSources/systemPressure", "floatValue", "kPa")
    unknown = _resource("/vendor/newCapability", "newType")

    assert incompatible_capabilities(
        {item.path: item for item in (compatible, changed_type, changed_unit, unknown)}
    ) == (changed_type.path, changed_unit.path)


def test_firmware_issue_is_created_and_removed_from_aggregate_result(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    issue_id = f"{FIRMWARE_ISSUE_PREFIX}{entry.entry_id}"
    changed = _resource("/heatSources/systemPressure", "floatValue", "kPa")
    coordinator = SimpleNamespace(resources={changed.path: changed})

    sync_firmware_compatibility_issue(hass, entry, (coordinator,))  # type: ignore[arg-type]

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "incompatible_firmware"
    assert issue.translation_placeholders == {"conflict_count": "1"}

    coordinator.resources = {changed.path: _resource(changed.path, "floatValue", "bar")}
    sync_firmware_compatibility_issue(hass, entry, (coordinator,))  # type: ignore[arg-type]

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
