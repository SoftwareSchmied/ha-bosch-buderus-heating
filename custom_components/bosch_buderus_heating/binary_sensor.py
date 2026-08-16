"""Dynamic read-only binary sensors for discovered PointT resources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
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
    entities: list[BoschBuderusBinarySensor] = []
    for coordinator in entry.runtime_data.coordinators:
        entities.extend(
            BoschBuderusBinarySensor(coordinator, description)
            for description in build_binary_sensor_descriptions(coordinator.resources)
        )
    async_add_entities(entities)


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
