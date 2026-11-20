"""RRC 2.0 entities: automatic absence, presence sensor, solar yield, hot
water system and hydraulic balancing (readings).

``rrc_platforms.track`` calls the tracker of each platform listed in
``TRACKERS`` once per coordinator. The two switches of the contract
(``rrc_contract_extras``) are generic ones; this module adds the read only
entities. Each exists only when its resource is in the expected shape and
not reported unavailable (P41), and only for a recognised room controller
(S11). Units are taken from the metadata, never guessed (P10). All of them
are expected, untested on a CT200; the paths and types were seen on a
TC100.2. ``/system/autoAway/users/*`` is never read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Final

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    EntityCategory,
    Platform,
    UnitOfEnergy,
    UnitOfTime,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .control import track_rrc_entities
from .coordinator import BoschBuderusDataUpdateCoordinator
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource
from .rrc_contract_extras import (
    COMING_HOME,
    COMING_HOME_PREDICTION,
    HOT_WATER_SYSTEM,
    HYDRAULIC_BALANCING,
    PRESENCE_SENSITIVITY,
    SOLAR_YIELD,
)
from .rrc_platforms import Tracker
from .rrc_system_entities import (
    RrcSystemProblem,
    RrcSystemSensor,
    SystemProblemDescription,
    SystemSensorDescription,
    _usable,
    flag,
    sensor_value,
)
from .write_policies import ControlContext

_MINUTES: Final = frozenset({"min", "mins", "minute", "minutes"})
_KWH: Final = frozenset({"kwh"})

# (path or path pattern, description); a pattern matches several circuits.
SENSORS: Final[tuple[tuple[str, SystemSensorDescription], ...]] = (
    (
        COMING_HOME_PREDICTION,
        SystemSensorDescription(
            key="rrc_coming_home_prediction",
            translation_key="rrc_coming_home_prediction",
            resource_path="",
            kind="measurement",
            units=_MINUTES,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            device_class=SensorDeviceClass.DURATION,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            icon="mdi:map-marker-clock-outline",
        ),
    ),
    (
        PRESENCE_SENSITIVITY,
        SystemSensorDescription(
            key="rrc_presence_sensitivity",
            translation_key="rrc_presence_sensitivity",
            resource_path="",
            kind="text",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            icon="mdi:motion-sensor",
        ),
    ),
    (
        SOLAR_YIELD,
        SystemSensorDescription(
            key="rrc_solar_total_yield",
            translation_key="rrc_solar_total_yield",
            resource_path="",
            kind="measurement",
            units=_KWH,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=0,
            icon="mdi:solar-power",
        ),
    ),
    (
        HOT_WATER_SYSTEM,
        SystemSensorDescription(
            key="rrc_hot_water_system",
            translation_key="rrc_hot_water_system",
            resource_path="",
            kind="text",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            icon="mdi:water-boiler",
        ),
    ),
)
FLAGS: Final[tuple[tuple[str, SystemProblemDescription], ...]] = (
    (
        COMING_HOME,
        SystemProblemDescription(
            key="rrc_coming_home",
            translation_key="rrc_coming_home",
            resource_path="",
            role="flag",
            icon="mdi:home-import-outline",
        ),
    ),
    (
        HYDRAULIC_BALANCING,
        SystemProblemDescription(
            key="rrc_hydraulic_balancing",
            translation_key="rrc_hydraulic_balancing",
            resource_path="",
            role="flag",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            icon="mdi:valve",
        ),
    ),
)


def _matches(spec: str, path: str) -> bool:
    return (
        re.fullmatch(spec, path) is not None if spec.startswith("^") else spec == path
    )


def _own[D: (SystemSensorDescription, SystemProblemDescription)](
    description: D, path: str
) -> D:
    """The description for one concrete path, with a key of its own."""
    return replace(description, key=f"{description.key}:{path}", resource_path=path)


def build_sensors(
    resources: Mapping[str, Resource], _context: ControlContext
) -> tuple[SystemSensorDescription, ...]:
    """The sensors whose resource exists in the expected shape."""
    found: list[SystemSensorDescription] = []
    for path in sorted(resources):
        resource = resources[path]
        for spec, description in SENSORS:
            if not _matches(spec, path) or not _usable(resource):
                continue
            own = _own(description, path)
            if sensor_value(own, resource) is not None:
                found.append(own)
    return tuple(found)


def build_flags(
    resources: Mapping[str, Resource], _context: ControlContext
) -> tuple[SystemProblemDescription, ...]:
    """The flags whose resource reads ``"true"`` or ``"false"``."""
    found: list[SystemProblemDescription] = []
    for path in sorted(resources):
        resource = resources[path]
        for spec, description in FLAGS:
            if (
                _matches(spec, path)
                and _usable(resource)
                and flag(resource.value) is not None
            ):
                found.append(_own(description, path))
    return tuple(found)


def _track_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(
        entry, coordinator, async_add_entities, build_sensors, RrcSystemSensor
    )


def _track_flags(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(
        entry, coordinator, async_add_entities, build_flags, RrcSystemProblem
    )


TRACKERS: Mapping[Platform, Tracker] = MappingProxyType(
    {Platform.SENSOR: _track_sensors, Platform.BINARY_SENSOR: _track_flags}
)
