"""RRC 2.0 entities: radio device details and settings (W6).

Owned by work package W6. ``rrc_platforms.track`` calls the tracker of each
platform listed in ``TRACKERS`` once per coordinator; see
``docs/dev/rrc-packages.md``.

The child lock and the read-only valve readings are generic entities of the
contract (``rrc_contract_devices``). This module adds what the generic
platforms cannot show: the temperature offset only while the device uses
it, the open window sensitivity as three levels, and the valve problem.
Every entity sits on its radio device (E07); the controller's own settings
on the controller. Writes go through the shared write path (metadata
checks, one PUT, read-back); everything is expected, untested on hardware.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.number import NumberMode
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory, Platform, UnitOfTemperature
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import rrc, rrc_contracts, rrc_devices
from .control import async_set_control, track_rrc_entities
from .control_presentation import control_name
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource, grouped_entity_name
from .number import BoschBuderusNumber, BoschBuderusNumberEntityDescription
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource
from .rrc_contract_devices import OFFSET_POLICY, SENSITIVITY_POLICY
from .rrc_device_settings import (
    OFFSET,
    SENSITIVITY,
    SENSITIVITY_LEVELS,
    STATUS,
    SUBFOLDERS,
    VALVE,
    offered,
    sensitivity_level,
    split,
    valve_status,
)
from .rrc_platforms import Tracker
from .sensor import _semantic_key
from .writes import ControlContext, assess_control, context_of

if TYPE_CHECKING:
    from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
else:
    # As in binary_sensor.py: older releases define it in the platform module.
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass


def _listed(devices: Resource | None, path: str) -> bool:
    """The device of a setting path is listed, with a type of that subfolder.

    A device that left the list, or whose type changed under the same ID,
    shows nothing from its former subfolder.
    """
    parts = split(path)
    if parts is None or devices is None:
        return False
    device = rrc_devices.find({rrc.DEVICE_LIST: devices}, parts.device_id)
    return device is not None and SUBFOLDERS.get(device.kind or "") == parts.subfolder


def _displayed_list(coordinator: BoschBuderusDataUpdateCoordinator) -> Resource | None:
    snapshot = coordinator.display_snapshot(rrc.DEVICE_LIST)
    return snapshot.resource if snapshot is not None and snapshot.available else None


def _fresh(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> Resource | None:
    """The current reading of a setting, as for every control."""
    snapshot = (coordinator.data or {}).get(path)
    if (
        snapshot is None
        or not snapshot.available
        or snapshot.freshness is not Freshness.FRESH
    ):
        return None
    return snapshot.resource


def _candidates(
    resources: Mapping[str, Resource], leaf: str
) -> list[tuple[str, Resource]]:
    devices = resources.get(rrc.DEVICE_LIST)
    return [
        (path, resource)
        for path, resource in resources.items()
        if (parts := split(path)) is not None
        and parts.leaf == leaf
        and _listed(devices, path)
        and offered(resource)
    ]


def _name(coordinator: BoschBuderusDataUpdateCoordinator, path: str) -> str:
    return rrc_contracts.name(path, coordinator.hass.config.language) or path


# --------------------------------------------------------------- offset


class DeviceOffsetNumber(BoschBuderusNumber):
    """CT-E04: the temperature offset of a radio device or the controller.

    A generic contract number that exists only while the device uses the
    setting; limits and step come from the metadata (S3, S4).
    """

    @property
    def available(self) -> bool:
        resource = _fresh(self.coordinator, self.entity_description.resource_path)
        return (
            super().available
            and resource is not None
            and offered(resource)
            and _listed(
                _displayed_list(self.coordinator),
                self.entity_description.resource_path,
            )
        )


def build_offsets(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[BoschBuderusNumberEntityDescription, ...]:
    descriptions: list[BoschBuderusNumberEntityDescription] = []
    for path, resource in _candidates(resources, OFFSET):
        assessment = assess_control(resource, context)
        minimum, maximum = resource.metadata.minimum, resource.metadata.maximum
        if (
            assessment.policy is not OFFSET_POLICY
            or not assessment.eligible
            or minimum is None
            or maximum is None
        ):
            continue
        descriptions.append(
            BoschBuderusNumberEntityDescription(
                key=_semantic_key(path, None),
                name=rrc_contracts.name(path, "de"),
                resource_path=path,
                write_policy=OFFSET_POLICY,
                native_min_value=minimum,
                native_max_value=maximum,
                native_step=assessment.ui_step,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                mode=NumberMode.BOX,
                icon=rrc_contracts.icon(path),
                entity_category=EntityCategory.CONFIG,
                translation_key=rrc_contracts.entity_key(path, Platform.NUMBER.value),
                entity_registry_enabled_default=OFFSET_POLICY.enabled_by_default,
            )
        )
    return tuple(descriptions)


def _create_offset(
    coordinator: BoschBuderusDataUpdateCoordinator,
    description: BoschBuderusNumberEntityDescription,
) -> DeviceOffsetNumber:
    return DeviceOffsetNumber(coordinator, description)


def track_offsets(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(entry, coordinator, add, build_offsets, _create_offset)


# ---------------------------------------------------------- sensitivity


@dataclass(frozen=True, kw_only=True)
class SettingDescription(SelectEntityDescription):
    """One setting of one listed radio device."""

    resource_path: str


class WindowSensitivitySelect(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], SelectEntity
):
    """CT-E05: the sensitivity of the open window detection of a valve.

    Three levels with exact values (high 0.5, medium 1.5, low 2.5); a level
    outside the advertised limits or step is refused before sending. Only
    a reading of exactly the written value confirms the change.
    """

    _attr_has_entity_name = True
    entity_description: SettingDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: SettingDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self.resource_path = description.resource_path
        self._attr_options = list(SENSITIVITY_LEVELS)
        self._attr_name = control_name(
            coordinator,
            description.resource_path,
            _name(coordinator, description.resource_path),
            contract=True,
        )
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:{description.key}:control"
        )

    @property
    def _resource(self) -> Resource | None:
        return _fresh(self.coordinator, self.resource_path)

    @property
    def available(self) -> bool:
        resource = self._resource
        if not super().available or resource is None or not offered(resource):
            return False
        assessment = assess_control(resource, context_of(self.coordinator))
        return (
            assessment.policy is SENSITIVITY_POLICY
            and assessment.eligible
            and _listed(_displayed_list(self.coordinator), self.resource_path)
        )

    @property
    def current_option(self) -> str | None:
        return sensitivity_level(self._resource)

    async def async_select_option(self, option: str) -> None:
        await async_set_control(
            self.coordinator,
            self.resource_path,
            SENSITIVITY_LEVELS[option],
            SENSITIVITY_POLICY,
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.resource_path)


def build_sensitivities(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[SettingDescription, ...]:
    descriptions: list[SettingDescription] = []
    for path, resource in _candidates(resources, SENSITIVITY):
        assessment = assess_control(resource, context)
        if assessment.policy is not SENSITIVITY_POLICY or not assessment.eligible:
            continue
        descriptions.append(
            SettingDescription(
                key=_semantic_key(path, None),
                resource_path=path,
                translation_key=rrc_contracts.entity_key(path, Platform.SELECT.value),
                icon=rrc_contracts.icon(path),
                entity_category=EntityCategory.CONFIG,
                entity_registry_enabled_default=SENSITIVITY_POLICY.enabled_by_default,
            )
        )
    return tuple(descriptions)


def _create_sensitivity(
    coordinator: BoschBuderusDataUpdateCoordinator, description: SettingDescription
) -> WindowSensitivitySelect:
    return WindowSensitivitySelect(coordinator, description)


def track_sensitivities(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(
        entry, coordinator, add, build_sensitivities, _create_sensitivity
    )


# -------------------------------------------------------- valve problem


@dataclass(frozen=True, kw_only=True)
class ValveDescription(BinarySensorEntityDescription):
    """The valve status of one listed radiator thermostat."""

    resource_path: str
    device_id: int


class ValveProblemBinarySensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], BinarySensorEntity
):
    """CT-E09: on while the valve reports a problem (too tight, adjustment
    range too big or too small, error position); enabled by default.

    ``etrv/status`` arrives as a string array (TC100.2) or as a list of
    objects with a ``valve`` state; both are read.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    entity_description: ValveDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: ValveDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self.resource_path = description.resource_path
        self._attr_name = grouped_entity_name(
            coordinator,
            description.resource_path,
            _name(coordinator, description.resource_path),
        )
        # E07: on the device, outside the status path (a list, whose former
        # field entries the registry cleanup removes).
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:devices:"
            f"device{description.device_id}:valve_problem"
        )

    @property
    def _resource(self) -> Resource | None:
        snapshot = self.coordinator.display_snapshot(self.resource_path)
        if snapshot is None or not snapshot.available:
            return None
        return snapshot.resource

    @property
    def available(self) -> bool:
        resource = self._resource
        return (
            super().available
            and resource is not None
            and offered(resource)
            and _listed(_displayed_list(self.coordinator), self.resource_path)
        )

    @property
    def is_on(self) -> bool | None:
        status = valve_status(self._resource)
        return status.problem if status is not None else None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """The known problem states, lower case; never an unknown word."""
        status = valve_status(self._resource)
        problems = status.problems if status is not None else ()
        return {"valve_problems": [problem.lower() for problem in problems]}

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.resource_path)


def build_valve_problems(
    resources: Mapping[str, Resource], _context: ControlContext
) -> tuple[ValveDescription, ...]:
    descriptions: list[ValveDescription] = []
    for path, _resource in _candidates(resources, STATUS):
        parts = split(path)
        if parts is None or parts.subfolder != VALVE:
            continue
        descriptions.append(
            ValveDescription(
                key=f"devices:device{parts.device_id}:valve_problem",
                resource_path=path,
                device_id=parts.device_id,
                translation_key=rrc_contracts.entity_key(
                    path, Platform.BINARY_SENSOR.value
                ),
            )
        )
    return tuple(descriptions)


def _create_valve_problem(
    coordinator: BoschBuderusDataUpdateCoordinator, description: ValveDescription
) -> ValveProblemBinarySensor:
    return ValveProblemBinarySensor(coordinator, description)


def track_valve_problems(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(
        entry, coordinator, add, build_valve_problems, _create_valve_problem
    )


TRACKERS: Mapping[Platform, Tracker] = MappingProxyType(
    {
        Platform.BINARY_SENSOR: track_valve_problems,
        Platform.NUMBER: track_offsets,
        Platform.SELECT: track_sensitivities,
    }
)
