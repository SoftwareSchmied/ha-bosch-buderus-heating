"""Conservatively released PointT controls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschBuderusConfigEntry
from .control import async_set_control, track_control_entities
from .coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from .device import device_info_for_resource, grouped_entity_name
from .enum_translation import writable_enum_options
from .pointt import Resource
from .resource_catalog import resource_name
from .sensor import _semantic_key
from .writes import (
    AUXILIARY_HEATER_OPERATION_MODE_POLICY,
    DHW_OPERATION_MODE_POLICY,
    HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    SILENT_MODE_POLICY,
    STRING_SWITCH_POLICIES,
    EnumWritePolicy,
    assess_control,
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
    """Create controls only for verified enum capabilities."""
    del hass
    for coordinator in entry.runtime_data.coordinators:
        track_control_entities(
            entry,
            coordinator,
            async_add_entities,
            build_select_descriptions,
            BoschBuderusSelect,
        )


def build_select_descriptions(
    resources: Mapping[str, Resource],
) -> tuple[BoschBuderusSelectEntityDescription, ...]:
    """Expose only released enum capabilities with matching metadata."""
    descriptions: list[BoschBuderusSelectEntityDescription] = []
    for resource in resources.values():
        assessment = assess_control(resource)
        policy = assessment.policy
        if (
            not assessment.eligible
            or not assessment.supports_select
            or not isinstance(policy, EnumWritePolicy)
        ):
            continue
        options: tuple[str, ...]
        if policy is HEATING_CIRCUIT_OPERATION_MODE_POLICY:
            translation_key = "heating_circuit_operation_mode"
        elif policy is DHW_OPERATION_MODE_POLICY:
            translation_key = "hot_water_operation_mode"
        elif policy is SILENT_MODE_POLICY:
            translation_key = "silent_mode"
        elif policy is AUXILIARY_HEATER_OPERATION_MODE_POLICY:
            translation_key = "auxiliary_heater_operation_mode"
        else:
            translation_key = ""
        options = tuple(_option_map(resource, translation_key))
        descriptions.append(
            BoschBuderusSelectEntityDescription(
                key=_semantic_key(resource.path, None)
                + (":options" if policy in STRING_SWITCH_POLICIES else ""),
                name="Betriebsart",
                resource_path=resource.path,
                write_policy=policy,
                options=list(options),
                translation_key=translation_key or None,
                entity_registry_enabled_default=True,
            )
        )
    return tuple(descriptions)


def _option_map(resource: Resource, translation_key: str) -> dict[str, str]:
    """Retain familiar ordering and append every additional advertised code."""
    advertised = assess_control(resource).options
    preferred = {
        "heating_circuit_operation_mode": ("off", "manual", "auto"),
        "hot_water_operation_mode": ("Off", "low", "high", "ownprogram", "eco"),
        "silent_mode": ("off", "auto", "on"),
        "auxiliary_heater_operation_mode": ("off", "manual", "auto"),
    }.get(translation_key, ())
    ordered = tuple(value for value in preferred if value in advertised)
    ordered += tuple(value for value in advertised if value not in ordered)
    return writable_enum_options(translation_key, ordered)


class BoschBuderusSelect(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    SelectEntity,
):
    """Set a PointT enum through a confirmed write transaction."""

    _attr_has_entity_name = True
    entity_description: BoschBuderusSelectEntityDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: BoschBuderusSelectEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        if description.write_policy is SILENT_MODE_POLICY:
            display_name = (
                "Silent Mode"
                if coordinator.hass.config.language.casefold().startswith("de")
                else "Silent mode"
            )
        elif description.write_policy is AUXILIARY_HEATER_OPERATION_MODE_POLICY:
            display_name = (
                "Zuheizer-Betriebsart"
                if coordinator.hass.config.language.casefold().startswith("de")
                else "Auxiliary heater mode"
            )
        else:
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
    def options(self) -> list[str]:
        return list(self._options_map)

    @property
    def _options_map(self) -> dict[str, str]:
        snapshot = self._snapshot
        if snapshot is None:
            return {}
        return _option_map(
            snapshot.resource, self.entity_description.translation_key or ""
        )

    @property
    def current_option(self) -> str | None:
        snapshot = self._snapshot
        if (
            snapshot is None
            or not snapshot.resource.has_value
            or not isinstance(snapshot.resource.value, str)
        ):
            return None
        return next(
            (
                key
                for key, raw in self._options_map.items()
                if raw == snapshot.resource.value
            ),
            None,
        )

    async def async_select_option(self, option: str) -> None:
        """Set the raw PointT enum and rely on the coordinator's read-back."""
        raw = self._options_map.get(option)
        if raw is None:
            raise ServiceValidationError("The option is no longer advertised")
        await async_set_control(
            self.coordinator,
            self.entity_description.resource_path,
            raw,
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
