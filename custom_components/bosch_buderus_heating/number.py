"""Safe numeric PointT controls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
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
from .writes import NumberWritePolicy, number_policy_for_resource


@dataclass(frozen=True, kw_only=True)
class BoschBuderusNumberEntityDescription(NumberEntityDescription):
    """Describe one bounded numeric PointT control."""

    resource_path: str
    write_policy: NumberWritePolicy


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create numeric controls only from matching live metadata."""
    del hass
    entities: list[BoschBuderusNumber] = []
    for coordinator in entry.runtime_data.coordinators:
        entities.extend(
            BoschBuderusNumber(coordinator, description)
            for description in build_number_descriptions(coordinator.resources)
        )
    async_add_entities(entities)


def build_number_descriptions(
    resources: Mapping[str, Resource],
) -> tuple[BoschBuderusNumberEntityDescription, ...]:
    """Build controls using the gateway's own bounds within safe envelopes."""
    descriptions: list[BoschBuderusNumberEntityDescription] = []
    for resource in resources.values():
        policy = number_policy_for_resource(resource)
        minimum, maximum = resource.metadata.minimum, resource.metadata.maximum
        if policy is None or minimum is None or maximum is None:
            continue
        key, name = _number_identity(resource.path)
        descriptions.append(
            BoschBuderusNumberEntityDescription(
                key=_semantic_key(resource.path, None),
                name=name,
                resource_path=resource.path,
                write_policy=policy,
                native_min_value=minimum,
                native_max_value=maximum,
                native_step=policy.step,
                native_unit_of_measurement=(
                    UnitOfTemperature.CELSIUS
                    if policy.unit == "C"
                    else UnitOfTime.MINUTES
                ),
                mode=(
                    NumberMode.BOX
                    if resource.path.endswith("/chargeDuration")
                    else NumberMode.SLIDER
                ),
                entity_category=EntityCategory.CONFIG,
                translation_key=key,
                entity_registry_enabled_default=not resource.path.endswith(
                    "/maxFlowTemp"
                ),
            )
        )
    return tuple(descriptions)


class BoschBuderusNumber(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    NumberEntity,
):
    """Represent one numeric setting confirmed by PointT read-back."""

    _attr_has_entity_name = True
    entity_description: BoschBuderusNumberEntityDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: BoschBuderusNumberEntityDescription,
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
            and number_policy_for_resource(snapshot.resource) is not None
        )

    @property
    def native_value(self) -> float | None:
        snapshot = self._snapshot
        if snapshot is None or isinstance(snapshot.resource.value, bool):
            return None
        value = snapshot.resource.value
        return float(value) if isinstance(value, int | float) else None

    async def async_set_native_value(self, value: float) -> None:
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


def _number_identity(path: str) -> tuple[str, str]:
    tail = path.rsplit("/", 1)[-1]
    return {
        "manualRoomSetpoint": ("manual_room_setpoint", "Manueller Sollwert"),
        "maxFlowTemp": (
            "maximum_supply_temperature",
            "Maximale Vorlauftemperatur",
        ),
        "comfort2": ("heating_temperature", "Heizen"),
        "eco": (
            ("hot_water_eco_plus", "Eco+ Starttemperatur")
            if path.startswith("/dhwCircuits/")
            else ("reduced_temperature", "Absenken")
        ),
        "chargeDuration": ("extra_hot_water_duration", "Dauer Extra-Warmwasser"),
        "singleChargeSetpoint": (
            "extra_hot_water_setpoint",
            "Solltemperatur Extra-Warmwasser",
        ),
        "high": ("hot_water_comfort", "Komfort Starttemperatur"),
        "low": ("hot_water_eco", "Eco Starttemperatur"),
    }[tail]
