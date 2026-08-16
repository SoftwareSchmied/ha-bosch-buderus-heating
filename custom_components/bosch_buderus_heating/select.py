"""Conservatively released PointT controls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschBuderusConfigEntry
from .control import async_set_control
from .coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from .device import device_info_for_resource, grouped_entity_name
from .enum_translation import enum_value_to_ha, enum_value_to_pointt
from .pointt import Resource
from .resource_catalog import resource_name
from .sensor import _semantic_key
from .writes import (
    DHW_OPERATION_MODE_POLICY,
    HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    EnumWritePolicy,
    enum_policy_for_resource,
)


@dataclass(frozen=True, kw_only=True)
class BoschBuderusSelectEntityDescription(SelectEntityDescription):
    """Describe one verified-shape PointT enum control."""

    resource_path: str
    write_policy: EnumWritePolicy


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create controls only for the verified operation-mode capability."""
    del hass
    entities: list[BoschBuderusSelect] = []
    for coordinator in entry.runtime_data.coordinators:
        entities.extend(
            BoschBuderusSelect(coordinator, description)
            for description in build_select_descriptions(coordinator.resources)
        )
    async_add_entities(entities)


def build_select_descriptions(
    resources: Mapping[str, Resource],
) -> tuple[BoschBuderusSelectEntityDescription, ...]:
    """Expose only released operation-mode capabilities with matching metadata."""
    descriptions: list[BoschBuderusSelectEntityDescription] = []
    for resource in resources.values():
        policy = enum_policy_for_resource(resource)
        if policy not in {
            HEATING_CIRCUIT_OPERATION_MODE_POLICY,
            DHW_OPERATION_MODE_POLICY,
        }:
            continue
        is_heating = policy is HEATING_CIRCUIT_OPERATION_MODE_POLICY
        options = (
            ("off", "manual", "auto")
            if is_heating
            else ("off", "low", "high", "ownprogram", "eco")
        )
        descriptions.append(
            BoschBuderusSelectEntityDescription(
                key=_semantic_key(resource.path, None),
                name="Betriebsart",
                resource_path=resource.path,
                write_policy=policy,
                options=list(options),
                translation_key=(
                    "heating_circuit_operation_mode"
                    if is_heating
                    else "hot_water_operation_mode"
                ),
                entity_registry_enabled_default=True,
            )
        )
    return tuple(descriptions)


class BoschBuderusSelect(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    SelectEntity,
):
    """Set a heating-circuit mode through a confirmed write transaction."""

    _attr_has_entity_name = True
    entity_description: BoschBuderusSelectEntityDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: BoschBuderusSelectEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        display_name = resource_name(
            description.resource_path, language=coordinator.hass.config.language
        )
        self._attr_name = grouped_entity_name(
            coordinator, description.resource_path, display_name
        )
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:{description.key}:control"
        )

    @property
    def available(self) -> bool:
        snapshot = self._snapshot
        return (
            super().available
            and snapshot is not None
            and snapshot.available
            and snapshot.freshness is Freshness.FRESH
            and enum_policy_for_resource(snapshot.resource)
            is self.entity_description.write_policy
        )

    @property
    def current_option(self) -> str | None:
        snapshot = self._snapshot
        if snapshot is None or not isinstance(snapshot.resource.value, str):
            return None
        value = enum_value_to_ha(
            self.entity_description.translation_key or "", snapshot.resource.value
        )
        return value if value in self.options else None

    async def async_select_option(self, option: str) -> None:
        """Set the raw PointT enum and rely on the coordinator's read-back."""
        await async_set_control(
            self.coordinator,
            self.entity_description.resource_path,
            enum_value_to_pointt(self.entity_description.translation_key or "", option),
            self.entity_description.write_policy,
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(
            self.coordinator, self.entity_description.resource_path
        )

    @property
    def _snapshot(self) -> ResourceSnapshot | None:
        return (self.coordinator.data or {}).get(self.entity_description.resource_path)


BoschBuderusOperationModeSelect = BoschBuderusSelect
