"""RRC 2.0 entities: system status, maintenance, sensors, energy.

Owned by work package W5. ``rrc_platforms.track`` calls the tracker of each
platform listed in ``TRACKERS`` once per coordinator; see
``docs/dev/rrc-packages.md``.

CT-F02 to CT-F04: read-only entities of the system leaves of an RRC 2.0 room
controller, each only when its resource exists in the expected shape and is
not reported unavailable (P41). Units are taken from the metadata, never
guessed (P10): the pressure needs ``bar``, the humidity ``%``, a temperature
``C``. All of them are expected, untested on a CT200; the paths and types
were seen on a TC100.2. The sensor tracker also starts the energy history
import (``rrc_energy_statistics``), once the controller is recognised.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    Platform,
    UnitOfPressure,
    UnitOfTemperature,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import rrc_energy_statistics
from .control import track_rrc_entities
from .coordinator import BoschBuderusDataUpdateCoordinator
from .device import device_info_for_resource
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource
from .rrc_contract_system import (
    BLOCKING_ERROR,
    CAUSE_CODE,
    DISPLAY_CODE,
    INDOOR_AIR,
    INDOOR_BOARD,
    INDOOR_CHIP,
    INDOOR_HUMIDITY,
    LOCKING_ERROR,
    MAINTENANCE_REQUEST,
    REFILL_NEEDED,
    SYSTEM_PRESSURE,
    UPDATE_STATE,
)
from .rrc_platforms import Tracker
from .write_policies import ControlContext

if TYPE_CHECKING:
    from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
else:
    # As in binary_sensor.py: older releases define it in the platform module.
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass

PRESSURE_RANGE: Final = "/heatSources/systemPressureRange"
_TRUE: Final = frozenset({"true", "on"})
_FALSE: Final = frozenset({"false", "off"})
_MAX_TEXT = 32

type ValueKind = Literal["text", "code", "measurement", "pressure_status"]


@dataclass(frozen=True, kw_only=True)
class SystemSensorDescription(SensorEntityDescription):
    """One read-only system sensor of an RRC controller."""

    resource_path: str
    kind: ValueKind
    # The metadata units this sensor accepts (casefolded); empty: any.
    units: frozenset[str] = frozenset()
    role: str = "value"
    # A ``"true"``/``"false"`` reading belongs to a problem flag instead.
    flags_elsewhere: bool = False


@dataclass(frozen=True, kw_only=True)
class SystemProblemDescription(BinarySensorEntityDescription):
    """One problem flag of an RRC controller (``"true"``/``"false"``)."""

    resource_path: str
    role: str = "problem"


def _sensor(
    path: str,
    key: str,
    kind: ValueKind,
    *,
    units: frozenset[str] = frozenset(),
    enabled: bool = True,
    category: EntityCategory | None = EntityCategory.DIAGNOSTIC,
    **extra: object,
) -> SystemSensorDescription:
    return SystemSensorDescription(
        key=key,
        translation_key=key,
        resource_path=path,
        kind=kind,
        units=units,
        entity_registry_enabled_default=enabled,
        entity_category=category,
        **extra,  # type: ignore[arg-type]
    )


_CELSIUS = frozenset({"c", "°c"})
PRESSURE: Final = _sensor(
    SYSTEM_PRESSURE,
    "rrc_system_pressure",
    "measurement",
    units=frozenset({"bar"}),
    category=None,
    native_unit_of_measurement=UnitOfPressure.BAR,
    device_class=SensorDeviceClass.PRESSURE,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
)
SENSORS: Final[tuple[SystemSensorDescription, ...]] = (
    _sensor(DISPLAY_CODE, "rrc_display_code", "text", icon="mdi:alert-circle-outline"),
    _sensor(CAUSE_CODE, "rrc_cause_code", "code", icon="mdi:numeric"),
    _sensor(
        MAINTENANCE_REQUEST,
        "rrc_maintenance_request",
        "text",
        enabled=False,
        icon="mdi:wrench-clock",
        flags_elsewhere=True,
    ),
    PRESSURE,
    _sensor(
        INDOOR_HUMIDITY,
        "rrc_indoor_humidity",
        "measurement",
        units=frozenset({"%"}),
        category=None,
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    *(
        _sensor(
            path,
            key,
            "measurement",
            units=_CELSIUS,
            enabled=False,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
        )
        for path, key in (
            (INDOOR_AIR, "rrc_indoor_air_temperature"),
            (INDOOR_CHIP, "rrc_indoor_chip_temperature"),
            (INDOOR_BOARD, "rrc_indoor_board_temperature"),
        )
    ),
    _sensor(
        UPDATE_STATE,
        "rrc_update_state",
        "text",
        enabled=False,
        icon="mdi:update",
    ),
)
# The derived pressure status, only with the plant's own pressure limits.
PRESSURE_STATUS: Final = _sensor(
    SYSTEM_PRESSURE,
    "pressure_status",
    "pressure_status",
    units=frozenset({"bar"}),
    category=None,
    device_class=SensorDeviceClass.ENUM,
    role="status",
)


def _problem(path: str, key: str) -> SystemProblemDescription:
    return SystemProblemDescription(
        key=key,
        translation_key=key,
        resource_path=path,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )


PROBLEMS: Final[tuple[SystemProblemDescription, ...]] = (
    _problem(LOCKING_ERROR, "rrc_locking_error"),
    _problem(BLOCKING_ERROR, "rrc_blocking_error"),
    _problem(MAINTENANCE_REQUEST, "rrc_maintenance_due"),
    _problem(REFILL_NEEDED, "rrc_refill_needed"),
)


# ------------------------------------------------------------------ values
def flag(value: object) -> bool | None:
    """``"true"``/``"false"`` (also a JSON boolean); anything else None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _unit_fits(description: SystemSensorDescription, resource: Resource) -> bool:
    if not description.units:
        return True
    unit = resource.metadata.unit
    return isinstance(unit, str) and unit.strip().casefold() in description.units


def sensor_value(
    description: SystemSensorDescription, resource: Resource
) -> str | float | int | None:
    """The state of a sensor for one reading; None for an unexpected shape."""
    value = resource.value
    if description.kind == "text":
        if not isinstance(value, str) or (
            description.flags_elsewhere and flag(value) is not None
        ):
            return None
        text = value.strip()
        return text[:_MAX_TEXT] if text else None
    if description.kind == "code":
        number = _number(value)
        if number is None or number < 0 or not number.is_integer():
            return None
        return int(number)
    number = _number(value)
    if number is None or not _unit_fits(description, resource):
        return None
    return number


def _usable(resource: Resource) -> bool:
    """P41: a resource the controller reports unavailable gets no entity."""
    return resource.metadata.available is not False


def _pressure_limits(resources: Mapping[str, Resource]) -> object | None:
    """The plant's validated pressure limits (as the generic status)."""
    from .sensor import _pressure_limits as limits

    pressure_range = resources.get(PRESSURE_RANGE)
    return limits(pressure_range) if pressure_range is not None else None


def build_sensors(
    resources: Mapping[str, Resource], _context: ControlContext
) -> tuple[SystemSensorDescription, ...]:
    """The system sensors whose resource exists in the expected shape."""
    descriptions: list[SystemSensorDescription] = []
    for description in SENSORS:
        resource = resources.get(description.resource_path)
        if (
            resource is None
            or not _usable(resource)
            or sensor_value(description, resource) is None
        ):
            continue
        descriptions.append(description)
    pressure = resources.get(SYSTEM_PRESSURE)
    if (
        pressure is not None
        and _usable(pressure)
        and sensor_value(PRESSURE, pressure) is not None
        and _pressure_limits(resources) is not None
    ):
        descriptions.append(PRESSURE_STATUS)
    return tuple(descriptions)


def build_problems(
    resources: Mapping[str, Resource], _context: ControlContext
) -> tuple[SystemProblemDescription, ...]:
    """The problem flags whose resource reads ``"true"`` or ``"false"``."""
    return tuple(
        description
        for description in PROBLEMS
        if (resource := resources.get(description.resource_path)) is not None
        and _usable(resource)
        and flag(resource.value) is not None
    )


# ---------------------------------------------------------------- entities
class _SystemEntity(CoordinatorEntity[BoschBuderusDataUpdateCoordinator]):
    """A read-only entity of one system resource."""

    _attr_has_entity_name = True
    resource_path: str

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        path: str,
        role: str,
    ) -> None:
        super().__init__(coordinator)
        self.resource_path = path
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:"
            f"{path.lstrip('/').replace('/', ':')}:{role}"
        )

    def _resource(self, path: str | None = None) -> Resource | None:
        snapshot = self.coordinator.display_snapshot(path or self.resource_path)
        if snapshot is None or not snapshot.available:
            return None
        resource = snapshot.resource
        return resource if _usable(resource) else None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.resource_path)


class RrcSystemSensor(_SystemEntity, SensorEntity):
    """A system sensor of an RRC controller."""

    entity_description: SystemSensorDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: SystemSensorDescription,
    ) -> None:
        super().__init__(coordinator, description.resource_path, description.role)
        self.entity_description = description
        if description.kind == "pressure_status":
            from .sensor import _PRESSURE_STATUS_OPTIONS

            self._attr_options = list(_PRESSURE_STATUS_OPTIONS)

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def native_value(self) -> str | float | int | None:
        resource = self._resource()
        if resource is None:
            return None
        description = self.entity_description
        if description.kind != "pressure_status":
            return sensor_value(description, resource)
        from .sensor import _pressure_limits, _pressure_status

        pressure = sensor_value(PRESSURE, resource)
        range_resource = self._resource(PRESSURE_RANGE)
        limits = (
            _pressure_limits(range_resource) if range_resource is not None else None
        )
        if not isinstance(pressure, float) or limits is None:
            return None
        return _pressure_status(pressure, limits)


class RrcSystemProblem(_SystemEntity, BinarySensorEntity):
    """A problem flag of an RRC controller; unknown values stay unknown."""

    entity_description: SystemProblemDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: SystemProblemDescription,
    ) -> None:
        super().__init__(coordinator, description.resource_path, description.role)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return self._resource() is not None

    @property
    def is_on(self) -> bool | None:
        resource = self._resource()
        return flag(resource.value) if resource is not None else None


# ---------------------------------------------------------------- trackers
def _track_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    started = False

    def build(
        resources: Mapping[str, Resource], context: ControlContext
    ) -> tuple[SystemSensorDescription, ...]:
        nonlocal started
        if not started:
            # CT-G05: only for a recognised controller (S11); once.
            started = True
            rrc_energy_statistics.track(entry.async_on_unload, coordinator)
        return build_sensors(resources, context)

    track_rrc_entities(entry, coordinator, async_add_entities, build, RrcSystemSensor)


def _track_problems(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(
        entry, coordinator, async_add_entities, build_problems, RrcSystemProblem
    )


TRACKERS: Mapping[Platform, Tracker] = MappingProxyType(
    {Platform.SENSOR: _track_sensors, Platform.BINARY_SENSOR: _track_problems}
)
