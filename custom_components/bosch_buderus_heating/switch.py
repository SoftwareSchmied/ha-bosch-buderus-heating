"""Safe string-backed PointT switches."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
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
from .pointt import Resource
from .resource_catalog import resource_name
from .sensor import _semantic_key
from .writes import STRING_SWITCH_POLICIES, EnumWritePolicy, enum_policy_for_resource


@dataclass(frozen=True, kw_only=True)
class BoschBuderusSwitchEntityDescription(SwitchEntityDescription):
    """Describe one on/off string resource."""

    resource_path: str
    write_policy: EnumWritePolicy
    on_value: str
    off_value: str


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create switches only from exact current capability metadata."""
    del hass
    entities: list[BoschBuderusSwitch] = []
    for coordinator in entry.runtime_data.coordinators:
        entities.extend(
            BoschBuderusSwitch(coordinator, description)
            for description in build_switch_descriptions(coordinator.resources)
        )
    async_add_entities(entities)


def build_switch_descriptions(
    resources: Mapping[str, Resource],
) -> tuple[BoschBuderusSwitchEntityDescription, ...]:
    """Build only the three released string-backed switches."""
    descriptions: list[BoschBuderusSwitchEntityDescription] = []
    for resource in resources.values():
        policy = enum_policy_for_resource(resource)
        if policy not in STRING_SWITCH_POLICIES:
            continue
        tail = resource.path.rsplit("/", 1)[-1]
        key, name, on_value, off_value = {
            "charge": ("extra_hot_water", "Extra-Warmwasser", "start", "stop"),
            "reduceTempOnAlarm": (
                "reduce_temperature_on_alarm",
                "Temperaturabsenkung bei Störung",
                "on",
                "off",
            ),
            "enabled": ("away_mode", "Abwesenheitsmodus", "on", "off"),
        }[tail]
        descriptions.append(
            BoschBuderusSwitchEntityDescription(
                key=_semantic_key(resource.path, None),
                name=name,
                resource_path=resource.path,
                write_policy=policy,
                on_value=on_value,
                off_value=off_value,
                translation_key=key,
                entity_registry_enabled_default=True,
                entity_category=(
                    EntityCategory.CONFIG if tail == "reduceTempOnAlarm" else None
                ),
            )
        )
    return tuple(descriptions)


class BoschBuderusSwitch(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    SwitchEntity,
):
    """Represent one string-backed switch with confirmed writes."""

    _attr_has_entity_name = True
    entity_description: BoschBuderusSwitchEntityDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: BoschBuderusSwitchEntityDescription,
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
    def is_on(self) -> bool | None:
        snapshot = self._snapshot
        if snapshot is None or not isinstance(snapshot.resource.value, str):
            return None
        return snapshot.resource.value == self.entity_description.on_value

    async def async_turn_on(self, **kwargs: Any) -> None:
        del kwargs
        await self._async_set(self.entity_description.on_value)

    async def async_turn_off(self, **kwargs: Any) -> None:
        del kwargs
        await self._async_set(self.entity_description.off_value)

    async def _async_set(self, value: str) -> None:
        await async_set_control(
            self.coordinator,
            self.entity_description.resource_path,
            value,
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
