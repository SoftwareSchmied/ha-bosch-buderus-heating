"""Dynamic read-only binary sensors for discovered PointT resources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschBuderusConfigEntry
from .coordinator import BoschBuderusDataUpdateCoordinator, ResourceSnapshot
from .device import device_info_for_resource, grouped_entity_name
from .faults import (
    MAX_FAULT_ATTRIBUTES,
    fault_severity_label,
    fault_summary,
    no_active_faults_label,
)
from .holidays import HOLIDAY_RESOURCE_PATHS, HolidayState, parse_holiday_state
from .pointt import Resource
from .resource_catalog import (
    CapabilityMaturity,
    capability_maturity,
    entity_enabled_by_default,
    resource_name,
    supports_entity,
)
from .sensor import (
    _is_boolean_resource,
    _nested_scalar_keys,
    _nested_value,
    _semantic_key,
    _values_scalar_keys,
)


@dataclass(frozen=True, kw_only=True)
class BoschBuderusBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe one boolean extracted from a PointT resource."""

    resource_path: str
    value_key: str | None = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create entities from every safe boolean found during discovery."""
    entities: list[BinarySensorEntity] = []
    for coordinator in entry.runtime_data.coordinators:
        entities.append(BoschBuderusSystemFaultBinarySensor(coordinator))
        if any(path in coordinator.resources for path in HOLIDAY_RESOURCE_PATHS):
            entities.append(BoschBuderusHolidayActiveBinarySensor(coordinator))
        entities.extend(
            BoschBuderusBinarySensor(coordinator, description)
            for description in build_binary_sensor_descriptions(coordinator.resources)
        )
    async_add_entities(entities)


class BoschBuderusHolidayActiveBinarySensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], BinarySensorEntity
):
    """Report the PointT holiday state independently from away mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "holiday_active"
    _attr_icon = "mdi:palm-tree"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:holiday_active"

    @property
    def available(self) -> bool:
        return super().available and bool(self._available_resources)

    @property
    def is_on(self) -> bool | None:
        return self._state.active

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self._state
        now = datetime.now(UTC)
        return {
            "period_count": len(state.periods),
            "active_period_count": sum(
                period.start <= now < period.end for period in state.periods
            ),
            "invalid_period_count": state.invalid_period_count,
        }

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")

    @property
    def _available_resources(self) -> dict[str, Resource]:
        snapshots = self.coordinator.data or {}
        return {
            path: snapshot.resource
            for path in HOLIDAY_RESOURCE_PATHS
            if (snapshot := snapshots.get(path)) is not None and snapshot.available
        }

    @property
    def _state(self) -> HolidayState:
        return parse_holiday_state(
            self._available_resources,
            fallback_timezone=self.coordinator.hass.config.time_zone,
        )


class BoschBuderusSystemFaultBinarySensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], BinarySensorEntity
):
    """Report whether an actionable or unknown system fault is active."""

    _attr_has_entity_name = True
    _attr_translation_key = "system_fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert-circle"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:system_fault"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.faults.has_known_state

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.faults.active_faults)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        tracker = self.coordinator.faults
        codes = sorted(
            {item.code for item in tracker.active_faults if item.code is not None}
        )
        attributes: dict[str, object] = {
            "active_fault_count": len(tracker.active_faults),
            "active_notification_count": len(tracker.active),
            "highest_severity": (
                tracker.highest_severity.value if tracker.highest_severity else None
            ),
            "codes": codes[:MAX_FAULT_ATTRIBUTES],
            "codes_truncated": len(codes) > MAX_FAULT_ATTRIBUTES,
        }
        if tracker.active_faults:
            attributes["summary"] = fault_summary(
                tracker.active_faults[0], self.coordinator.hass.config.language
            )
            attributes["highest_severity_label"] = fault_severity_label(
                tracker.active_faults[0].severity,
                self.coordinator.hass.config.language,
            )
        else:
            attributes["summary"] = no_active_faults_label(
                self.coordinator.hass.config.language
            )
        return attributes

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")


def build_binary_sensor_descriptions(
    resources: Mapping[str, Resource],
) -> tuple[BoschBuderusBinarySensorEntityDescription, ...]:
    """Build stable descriptions for all discovered safe boolean states."""
    descriptions: list[BoschBuderusBinarySensorEntityDescription] = []
    for resource in resources.values():
        if not supports_entity(resource):
            continue
        keys = _nested_scalar_keys(resource.value, include_booleans=True)
        if not keys and resource.values:
            keys = _values_scalar_keys(resource, include_booleans=True)
        if keys:
            descriptions.extend(_description(resource, key) for key in keys)
        elif _is_boolean_resource(resource):
            descriptions.append(_description(resource, None))
    return tuple(descriptions)


def _description(
    resource: Resource, value_key: str | None
) -> BoschBuderusBinarySensorEntityDescription:
    return BoschBuderusBinarySensorEntityDescription(
        key=_semantic_key(resource.path, value_key),
        name=resource_name(resource.path, value_key),
        resource_path=resource.path,
        value_key=value_key,
        entity_category=(
            EntityCategory.DIAGNOSTIC
            if capability_maturity(resource.path) is CapabilityMaturity.UNDERSTOOD
            else None
        ),
        entity_registry_enabled_default=entity_enabled_by_default(resource.path),
    )


class BoschBuderusBinarySensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    BinarySensorEntity,
):
    """Represent one boolean value from a discovered PointT resource."""

    _attr_has_entity_name = True
    entity_description: BoschBuderusBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: BoschBuderusBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        display_name = resource_name(
            description.resource_path,
            description.value_key,
            language=coordinator.hass.config.language,
        )
        self._attr_name = grouped_entity_name(
            coordinator, description.resource_path, display_name
        )
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{description.key}"

    @property
    def available(self) -> bool:
        snapshot = self._snapshot
        return super().available and snapshot is not None and snapshot.available

    @property
    def is_on(self) -> bool | None:
        snapshot = self._snapshot
        if snapshot is None:
            return None
        resource = snapshot.resource
        value = (
            _nested_value(resource, self.entity_description.value_key)
            if self.entity_description.value_key is not None
            else resource.value
        )
        return value if isinstance(value, bool) else None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(
            self.coordinator, self.entity_description.resource_path
        )

    @property
    def _snapshot(self) -> ResourceSnapshot | None:
        return (self.coordinator.data or {}).get(self.entity_description.resource_path)
