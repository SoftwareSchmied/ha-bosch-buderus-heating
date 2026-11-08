"""Wallbox readings with stable IDs and no inferred energy accumulation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .device import registered_device
from .wallbox import STATES, Section, WallboxCoordinator


@dataclass(frozen=True, kw_only=True)
class WallboxSensorDescription(SensorEntityDescription):
    """Locate a reading in its independently refreshed section."""

    section: Section = "telemetry"


DESCRIPTIONS = (
    WallboxSensorDescription(
        key="state",
        translation_key="wallbox_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(STATES.values()),
        icon="mdi:ev-station",
    ),
    WallboxSensorDescription(
        key="power",
        translation_key="wallbox_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    WallboxSensorDescription(
        key="energy",
        translation_key="wallbox_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # The meter's reset scope has not been established on hardware.
    ),
    WallboxSensorDescription(
        key="duration",
        translation_key="wallbox_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    WallboxSensorDescription(
        key="current_limit",
        translation_key="wallbox_current_limit",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
    ),
    WallboxSensorDescription(
        key="charging_start",
        translation_key="wallbox_charging_start",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    WallboxSensorDescription(
        key="role",
        section="conf",
        translation_key="wallbox_role",
        device_class=SensorDeviceClass.ENUM,
        options=["standalone", "leader", "follower"],
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    WallboxSensorDescription(
        key="hardware_current_limit",
        section="info",
        translation_key="wallbox_hardware_current_limit",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


class WallboxSensor(CoordinatorEntity[WallboxCoordinator], SensorEntity):
    """Expose only a currently valid interpretation of an allowlisted field."""

    _attr_has_entity_name = True
    entity_description: WallboxSensorDescription

    def __init__(
        self, coordinator: WallboxCoordinator, description: WallboxSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:wallbox:cp0:{description.key}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        gateway = self.coordinator.gateway
        product = self.coordinator.reading("info", "product")
        software = self.coordinator.reading("info", "software")
        info = DeviceInfo(
            identifiers={(DOMAIN, gateway.gateway_id)},
            manufacturer="Bosch/Buderus",
            model=product if isinstance(product, str) else gateway.model,
            sw_version=software
            if isinstance(software, str)
            else gateway.firmware_version,
        )
        if not registered_device(self.coordinator.hass, gateway.gateway_id):
            # C8: a new device is named without the brand.
            info["name"] = "Wallbox"
        return info

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.section_fresh(
            self.entity_description.section
        )

    @property
    def native_value(self) -> str | float | int | datetime | None:
        return self.coordinator.reading(
            self.entity_description.section, self.entity_description.key
        )
