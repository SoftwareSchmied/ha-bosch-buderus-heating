"""Sensors of the opt-in wallbox history (last session, monthly energy).

They are added only once the history option is on and the first answer has
been read, and never removed again; with the option off they are unavailable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .held_readings import cloud_usable
from .wallbox import WallboxCoordinator
from .wallbox_history import Kind, history_enabled
from .wallbox_sensor import wallbox_device_info

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .platform_scope import PlatformScope


@dataclass(frozen=True, kw_only=True)
class WallboxHistoryDescription(SensorEntityDescription):
    """Which history part a sensor reads and how."""

    kind: Kind
    read: Callable[[WallboxCoordinator], float | int | datetime | None]


def _session(
    coordinator: WallboxCoordinator, name: str
) -> float | int | datetime | None:
    session = coordinator.history.session
    return getattr(session, name) if session is not None else None


def _totals(coordinator: WallboxCoordinator, name: str) -> float | None:
    totals = coordinator.history.totals
    return getattr(totals, name) if totals is not None else None


HISTORY_DESCRIPTIONS = (
    WallboxHistoryDescription(
        key="history_last_session_start",
        translation_key="wallbox_history_last_session_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        kind="chargelog",
        read=lambda coordinator: _session(coordinator, "start"),
    ),
    WallboxHistoryDescription(
        key="history_last_session_end",
        translation_key="wallbox_history_last_session_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        kind="chargelog",
        read=lambda coordinator: _session(coordinator, "end"),
    ),
    WallboxHistoryDescription(
        key="history_last_session_duration",
        translation_key="wallbox_history_last_session_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        kind="chargelog",
        read=lambda coordinator: _session(coordinator, "duration_seconds"),
    ),
    WallboxHistoryDescription(
        key="history_last_session_energy",
        translation_key="wallbox_history_last_session_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        kind="chargelog",
        read=lambda coordinator: _session(coordinator, "energy_kwh"),
    ),
    WallboxHistoryDescription(
        key="history_month_grid_energy",
        translation_key="wallbox_history_month_grid_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        kind="energyhistory",
        read=lambda coordinator: _totals(coordinator, "grid_kwh"),
    ),
    WallboxHistoryDescription(
        key="history_month_solar_energy",
        translation_key="wallbox_history_month_solar_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        kind="energyhistory",
        read=lambda coordinator: _totals(coordinator, "solar_kwh"),
    ),
)


class WallboxHistorySensor(CoordinatorEntity[WallboxCoordinator], SensorEntity):
    """A value of the opt-in history; unavailable while the option is off."""

    _attr_has_entity_name = True
    entity_description: WallboxHistoryDescription

    def __init__(
        self, coordinator: WallboxCoordinator, description: WallboxHistoryDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:wallbox:cp0:{description.key}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return wallbox_device_info(self.coordinator)

    @property
    def available(self) -> bool:
        # With the option off the coordinator has dropped the history.
        return cloud_usable(self.coordinator) and self.coordinator.history.fresh(
            self.entity_description.kind
        )

    @property
    def native_value(self) -> float | int | datetime | None:
        return self.entity_description.read(self.coordinator)


def track_wallbox_history_sensors(
    scope: PlatformScope, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the history sensors of a wallbox once the option is on and the
    first answer is in. Without the option nothing is created."""
    for coordinator in scope.wallboxes:
        added: list[bool] = []

        @callback
        def discover(
            coordinator: WallboxCoordinator = coordinator, added: list[bool] = added
        ) -> None:
            entry = coordinator.config_entry
            if (
                added
                or not history_enabled(entry.options if entry is not None else None)
                or not coordinator.history.received
            ):
                return
            added.append(True)
            async_add_entities(
                WallboxHistorySensor(coordinator, description)
                for description in HISTORY_DESCRIPTIONS
            )

        scope.async_on_unload(coordinator.async_add_listener(discover))
        discover()
