"""RRC 2.0 entities: heating circuit settings (W4, CT-G01, CT-G02).

The selections and the switch of ``rrc_contract_circuit`` are generic
controls. The numbers are added here: before a write they read the setting,
the circuit's control strategy and, for a limit, the limits it is ordered
against freshly (S5) and send nothing when one of those changed; the
contract then keeps the limits in order and refuses outside
weather-compensated control. ``operatingSeason``
becomes an enum sensor. Nothing is confirmed on a real controller yet.

``rrc_platforms.track`` calls the tracker of each platform listed in
``TRACKERS`` once per coordinator; see ``docs/dev/rrc-packages.md``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from homeassistant.components.number import NumberMode
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import Platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import rrc_contracts
from .control import async_set_control, track_rrc_entities
from .control_presentation import control_category, control_icon
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource, grouped_entity_name
from .entity_resources import fresh_resource
from .i18n import localize
from .number import (
    BoschBuderusNumber,
    BoschBuderusNumberEntityDescription,
    _policy_unit,
)
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource
from .rrc_contract_circuit import (
    CONTROL_LEAF,
    NUMBER_POLICIES,
    OPERATING_SEASON,
    OPERATING_SEASON_NAMES,
    OPERATING_SEASONS,
    guard_paths,
)
from .rrc_platforms import Tracker
from .sensor import _semantic_key
from .writes import (
    ControlContext,
    assess_control,
    number_policy_for_resource,
)

_SEASON = re.compile(OPERATING_SEASON)


def build_number_descriptions(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[BoschBuderusNumberEntityDescription, ...]:
    """The circuit numbers of an RRC controller; the generic builder leaves
    them out (``create_entity``). Limits and steps come from the metadata."""
    descriptions: list[BoschBuderusNumberEntityDescription] = []
    for resource in resources.values():
        policy = number_policy_for_resource(resource, context)
        minimum, maximum = resource.metadata.minimum, resource.metadata.maximum
        if (
            policy is None
            or policy not in NUMBER_POLICIES
            or minimum is None
            or maximum is None
        ):
            continue
        path = resource.path
        assessment = assess_control(resource, context)
        descriptions.append(
            BoschBuderusNumberEntityDescription(
                key=_semantic_key(path, None),
                name=rrc_contracts.name(path, "de") or path.rsplit("/", 1)[-1],
                resource_path=path,
                write_policy=policy,
                native_min_value=minimum,
                native_max_value=maximum,
                native_step=assessment.ui_step,
                native_unit_of_measurement=_policy_unit(policy, assessment.unit),
                mode=NumberMode.BOX,
                icon=control_icon(path, numeric=True, contract=True),
                entity_category=control_category(path, numeric=True, contract=True),
                translation_key=rrc_contracts.entity_key(path, Platform.NUMBER.value),
                entity_registry_enabled_default=policy.enabled_by_default,
            )
        )
    return tuple(descriptions)


class RrcCircuitNumber(BoschBuderusNumber):
    """A circuit setting written after a fresh read of itself, of the
    circuit's control strategy and, for a heating curve or supply limit, of
    the limits it is ordered against (S5)."""

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: BoschBuderusNumberEntityDescription,
    ) -> None:
        super().__init__(coordinator, description)
        # O51: these stay read as usual while this setting is shown.
        self.extra_resource_paths: tuple[str, ...] = guard_paths(
            description.resource_path
        )

    def _expected(self, path: str) -> Resource | None:
        """VER-05: the control strategy counts only with a current reading,
        as in the dialog; a stale one would refuse every write as "changed"
        until the next poll, and the rule treats a missing one as no lock.
        The setting and its limits stay strict: without a current reading
        of the other limit the order cannot be checked and nothing is sent."""
        if path.endswith(f"/{CONTROL_LEAF}"):
            return fresh_resource(self.coordinator, path)
        snapshot = (self.coordinator.data or {}).get(path)
        return snapshot.resource if snapshot is not None else None

    async def async_set_native_value(self, value: float) -> None:
        path = self.entity_description.resource_path
        # The coordinator compares these with its readings and the gateway's
        # before it writes; a reading the plant does not have is left out.
        expected = tuple(
            resource
            for item in (path, *self.extra_resource_paths)
            if (resource := self._expected(item)) is not None
        )
        await async_set_control(
            self.coordinator,
            path,
            value,
            self.entity_description.write_policy,
            expected=expected,
        )


@dataclass(frozen=True, kw_only=True)
class RrcCircuitSensorDescription(SensorEntityDescription):
    """The operating season of one heating circuit."""

    resource_path: str


def _offered(resource: Resource) -> bool:
    """P41: an unavailable reading not marked as used gives no entity."""
    metadata = resource.metadata
    return (
        metadata.resource_type == "stringValue"
        and not resource.references
        and metadata.availability_flags_valid
        and not (metadata.available is False and metadata.used is not True)
    )


def build_sensor_descriptions(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[RrcCircuitSensorDescription, ...]:
    """CT-G01: the operating season, a known state (heating, cooling, idle)."""
    del context
    return tuple(
        RrcCircuitSensorDescription(
            key=f"{_semantic_key(path, None)}:season",
            name=rrc_contracts.name(path, "de") or OPERATING_SEASON_NAMES[0],
            resource_path=path,
            translation_key=rrc_contracts.entity_key(path, Platform.SENSOR.value),
            device_class=SensorDeviceClass.ENUM,
            options=list(OPERATING_SEASONS),
            icon=rrc_contracts.icon(path),
        )
        for path, resource in sorted(resources.items())
        if _SEASON.fullmatch(path) and _offered(resource)
    )


class RrcOperatingSeasonSensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], SensorEntity
):
    """Whether the circuit heats, cools or idles; another reading shows as
    unknown."""

    _attr_has_entity_name = True
    entity_description: RrcCircuitSensorDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: RrcCircuitSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        language = coordinator.hass.config.language
        path = description.resource_path
        name = rrc_contracts.name(path, language) or localize(
            language, *OPERATING_SEASON_NAMES
        )
        self._attr_name = grouped_entity_name(coordinator, path, name)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{description.key}"

    @property
    def available(self) -> bool:
        snapshot = (self.coordinator.data or {}).get(
            self.entity_description.resource_path
        )
        return (
            super().available
            and snapshot is not None
            and snapshot.available
            and snapshot.freshness is Freshness.FRESH
        )

    @property
    def native_value(self) -> str | None:
        resource = fresh_resource(
            self.coordinator, self.entity_description.resource_path
        )
        value = resource.value if resource is not None else None
        return value if isinstance(value, str) and value in OPERATING_SEASONS else None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(
            self.coordinator, self.entity_description.resource_path
        )


def _track_numbers(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(
        entry,
        coordinator,
        async_add_entities,
        build_number_descriptions,
        RrcCircuitNumber,
    )


def _track_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(
        entry,
        coordinator,
        async_add_entities,
        build_sensor_descriptions,
        RrcOperatingSeasonSensor,
    )


TRACKERS: Final[Mapping[Platform, Tracker]] = MappingProxyType(
    {Platform.NUMBER: _track_numbers, Platform.SENSOR: _track_sensors}
)
