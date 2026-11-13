"""Safe numeric PointT controls."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschBuderusConfigEntry, rrc, rrc_contracts, rrc_platforms
from .bacon import purifier
from .bacon.purifier_entities import (
    BaconPurifierNumber,
)
from .bacon.purifier_entities import (
    track_entities as track_purifier_entities,
)
from .control import HeldCapabilities, async_set_control, track_control_entities
from .control_presentation import control_category, control_icon, control_name
from .coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from .device import device_info_for_resource
from .platform_scope import PlatformScope, setup_platform
from .pointt import Resource
from .resource_catalog import resource_name
from .sensor import _semantic_key
from .unit_contracts import DURATION_UNITS, TEMPERATURE_UNITS
from .writes import (
    ControlContext,
    NumberWritePolicy,
    assess_control,
    context_of,
    number_policy_for_resource,
)
from .zone_controls import zone_control_name


def _native_unit(unit: str | None) -> str | None:
    return {
        **TEMPERATURE_UNITS,
        **DURATION_UNITS,
        "%": PERCENTAGE,
    }.get(unit or "")


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
    setup_platform(entry, Platform.NUMBER, async_add_entities, _setup_entities)


def _setup_entities(
    entry: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create numeric controls only from matching live metadata."""
    if (account := entry.bacon) is not None:
        track_purifier_entities(
            entry, account, async_add_entities, BaconPurifierNumber, purifier.NUMBERS
        )
    for coordinator in entry.coordinators:
        track_control_entities(
            entry,
            coordinator,
            async_add_entities,
            build_number_descriptions,
            BoschBuderusNumber,
        )
        rrc_platforms.track(Platform.NUMBER, entry, coordinator, async_add_entities)


def build_number_descriptions(
    resources: Mapping[str, Resource],
    context: ControlContext | None = None,
) -> tuple[BoschBuderusNumberEntityDescription, ...]:
    """Build controls using the gateway's own bounds within safe envelopes.

    On an RRC controller its contracts' numbers join them (S11); a contract
    policy without an entity of its own (``create_entity``) adds none.
    """
    descriptions: list[BoschBuderusNumberEntityDescription] = []
    for resource in resources.values():
        policy = number_policy_for_resource(resource, context)
        minimum, maximum = resource.metadata.minimum, resource.metadata.maximum
        if (
            policy is None
            or minimum is None
            or maximum is None
            or not policy.create_entity
        ):
            continue
        contract = rrc_contracts.owns(policy)
        if contract:
            key = rrc_contracts.entity_key(resource.path, Platform.NUMBER.value)
            name = rrc_contracts.name(resource.path, "de") or resource_name(
                resource.path
            )
        else:
            key, name = _number_identity(resource.path)
        assessment = assess_control(resource, context)
        descriptions.append(
            BoschBuderusNumberEntityDescription(
                key=_semantic_key(resource.path, None),
                name=name,
                resource_path=resource.path,
                write_policy=policy,
                native_min_value=minimum,
                native_max_value=maximum,
                native_step=assessment.ui_step,
                native_unit_of_measurement=_policy_unit(policy, assessment.unit),
                mode=NumberMode.BOX,
                icon=control_icon(resource.path, numeric=True, contract=contract),
                entity_category=control_category(
                    resource.path, numeric=True, contract=contract
                ),
                translation_key=key,
                entity_registry_enabled_default=policy.enabled_by_default,
            )
        )
    return tuple(descriptions)


def _policy_unit(policy: NumberWritePolicy, unit: str | None) -> str | None:
    """The unit shown: a contract's override ("" for none), else the
    resource's."""
    if policy.ui_unit_override is not None:
        return policy.ui_unit_override or None
    return _native_unit(unit)


class BoschBuderusNumber(
    HeldCapabilities,
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    NumberEntity,
):
    """Represent one numeric setting confirmed by PointT read-back."""

    _attr_has_entity_name = True
    entity_description: BoschBuderusNumberEntityDescription
    # O30: the unit of the last reading, kept while there is none.
    _held_unit: str | None = None

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: BoschBuderusNumberEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        contract = rrc_contracts.owns(description.write_policy)
        display_name = (
            (
                contract
                and rrc_contracts.name(
                    description.resource_path, coordinator.hass.config.language
                )
            )
            or rrc.name(
                description.resource_path,
                coordinator.hass.config.language,
                control=True,
            )
            or zone_control_name(
                description.resource_path, coordinator.hass.config.language
            )
            or resource_name(
                description.resource_path, language=coordinator.hass.config.language
            )
        )
        self._attr_name = control_name(
            coordinator, description.resource_path, display_name, contract=contract
        )
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:{description.key}:control"
        )

    def _policy(self, resource: Resource) -> NumberWritePolicy | None:
        return number_policy_for_resource(resource, context_of(self.coordinator))

    @property
    def available(self) -> bool:
        snapshot = self._snapshot
        return (
            super().available
            and snapshot is not None
            and snapshot.available
            and snapshot.freshness is Freshness.FRESH
            and self._policy(snapshot.resource) is not None
        )

    @property
    def native_min_value(self) -> float:
        snapshot = self._snapshot
        if snapshot is not None and self._policy(snapshot.resource):
            minimum = snapshot.resource.metadata.minimum
            if minimum is not None:
                return minimum
        return super().native_min_value

    @property
    def native_max_value(self) -> float:
        snapshot = self._snapshot
        if snapshot is not None and self._policy(snapshot.resource):
            maximum = snapshot.resource.metadata.maximum
            if maximum is not None:
                return maximum
        return super().native_max_value

    @property
    def native_value(self) -> float | None:
        snapshot = self._snapshot
        if (
            snapshot is None
            or not snapshot.resource.has_value
            or isinstance(snapshot.resource.value, bool)
        ):
            return None
        value = snapshot.resource.value
        return (
            float(value)
            if isinstance(value, int | float) and math.isfinite(value)
            else None
        )

    @property
    def native_step(self) -> float | None:
        snapshot = self._snapshot
        if snapshot is not None:
            step = assess_control(
                snapshot.resource, context_of(self.coordinator)
            ).ui_step
            if step is not None:
                return step
        return super().native_step

    @property
    def native_unit_of_measurement(self) -> str | None:
        snapshot = self._snapshot
        if snapshot is not None:
            unit = _policy_unit(
                self.entity_description.write_policy,
                assess_control(snapshot.resource, context_of(self.coordinator)).unit,
            )
            self._held_unit = unit
            return unit
        if self._held_unit is not None:
            return self._held_unit
        return super().native_unit_of_measurement

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
        "manualTemperatureHeating": ("manual_room_setpoint", "Manueller Heizsollwert"),
        "saltLevelAlarm": ("softener_salt_threshold", "Salz-Warnschwelle"),
        "singleRegAppoint": ("softener_regeneration_delay", "Regeneration planen"),
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
        "setpointTemp": ("pool_setpoint_temperature", "Pool-Solltemperatur"),
    }[tail]
