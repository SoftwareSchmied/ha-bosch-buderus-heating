"""The deviation of the gateway's clock from Home Assistant's, as a sensor (0.9.2).

A calculated value (``derived_from``): the time the gateway reports
(``/gateway/DateTime``) minus the time of Home Assistant at the moment that
reading was received. The value is shown in seconds, positive when the
gateway runs ahead. It never sends a request and uses only the reading of
the regular cycle.

A reading without a zone is taken as local time of the installation, the
time zone of Home Assistant; a reading with a zone keeps it. If the gateway
reports another convention, the deviation shows a multiple of whole hours
instead. A reading that is not a time, or a deviation of more than a year,
gives no value. Diagnostic, disabled by default.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, tzinfo
from functools import partial
from typing import TYPE_CHECKING, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .control import track_entity_once
from .coordinator import Freshness
from .device import device_info_for_resource

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .platform_scope import DiscoveryLifecycle

CLOCK_PATH: Final = "/gateway/DateTime"
# The spelling older documents give; a gateway reports one of them.
CLOCK_PATHS: Final = (CLOCK_PATH, "/gateway/dateTime")
_MAX_TEXT: Final = 64
MAX_DEVIATION: Final = timedelta(days=366)


def clock_path(paths: Iterable[str]) -> str | None:
    """The path of the gateway time among ``paths``, if there is one."""
    available = set(paths)
    return next((path for path in CLOCK_PATHS if path in available), None)


def parse_clock(value: object, zone: tzinfo | None) -> datetime | None:
    """The reported time as an aware time, or None for anything else.

    ``zone`` is the time zone a reading without one is taken in.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_TEXT:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if zone is None:
            return None
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def deviation_seconds(
    value: object, received_at: datetime | None, zone: tzinfo | None
) -> int | None:
    """Gateway time minus Home Assistant time, in whole seconds; None when
    the reading is not a time, was never received, or lies beyond a year."""
    if received_at is None or received_at.tzinfo is None:
        return None
    reported = parse_clock(value, zone)
    if reported is None:
        return None
    difference = reported - received_at
    if abs(difference) > MAX_DEVIATION:
        return None
    return round(difference.total_seconds())


class GatewayClockDeviationSensor(
    CoordinatorEntity["BoschBuderusDataUpdateCoordinator"], SensorEntity
):
    """The calculated deviation of the gateway clock, in seconds."""

    _attr_has_entity_name = True
    _attr_translation_key = "gateway_clock_deviation"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:gateway:DateTime:clock_deviation"
        )

    def _path(self) -> str:
        return clock_path(self.coordinator.data or {}) or CLOCK_PATH

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self._path())

    async def async_update(self) -> None:
        """Read with the regular cycle; never a request of its own."""

    def _deviation(self) -> int | None:
        path = self._path()
        snapshot = (self.coordinator.data or {}).get(path)
        if (
            snapshot is None
            or not snapshot.available
            or snapshot.freshness is not Freshness.FRESH
            or snapshot.resource.path != path
        ):
            return None
        return deviation_seconds(
            snapshot.resource.value,
            snapshot.last_success,
            dt_util.get_default_time_zone(),
        )

    @property
    def available(self) -> bool:
        return super().available and self._deviation() is not None

    @property
    def native_value(self) -> int | None:
        return self._deviation()

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"derived_from": {self._path(): "cloud"}}


def track_clock_deviation_sensor(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensor once the gateway reported its time."""
    registry = er.async_get(coordinator.hass)
    unique_id = f"{coordinator.gateway.gateway_id}:gateway:DateTime:clock_deviation"

    def ready() -> bool:
        return (
            clock_path(coordinator.resources) is not None
            or registry.async_get_entity_id("sensor", DOMAIN, unique_id) is not None
        )

    track_entity_once(
        entry,
        coordinator,
        async_add_entities,
        ready,
        partial(GatewayClockDeviationSensor, coordinator),
    )
