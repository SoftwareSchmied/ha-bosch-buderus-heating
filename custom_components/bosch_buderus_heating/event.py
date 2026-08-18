"""Lifecycle events for PointT system notifications."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschBuderusConfigEntry
from .coordinator import BoschBuderusDataUpdateCoordinator
from .device import device_info_for_resource
from .faults import (
    FaultEventType,
    FaultLifecycleEvent,
    fault_severity_label,
    fault_summary,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one bounded event stream per configured gateway."""
    del hass
    async_add_entities(
        BoschBuderusNotificationEvent(coordinator)
        for coordinator in entry.runtime_data.coordinators
    )


class BoschBuderusNotificationEvent(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], EventEntity
):
    """Emit newly observed and resolved system notifications."""

    _attr_has_entity_name = True
    _attr_translation_key = "system_notifications"
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_event_types = [item.value for item in FaultEventType]
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:system_notifications"

    async def async_added_to_hass(self) -> None:
        """Subscribe after the entity can safely publish HA events."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.faults.async_add_listener(self._handle_fault_event)
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.faults.has_supported_source

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")

    def _handle_fault_event(self, event: FaultLifecycleEvent) -> None:
        fault = event.fault
        attributes: dict[str, str] = {
            "severity": fault.severity.value,
            "severity_label": fault_severity_label(
                fault.severity, self.coordinator.hass.config.language
            ),
            "component": fault.component_type or "unknown",
            "summary": fault_summary(fault, self.coordinator.hass.config.language),
            "observed_at": event.observed_at.isoformat(),
            "time_source": fault.time_source.value,
        }
        if fault.code is not None:
            attributes["code"] = fault.code
        if fault.subcode is not None:
            attributes["subcode"] = fault.subcode
        if fault.occurred_at is not None:
            attributes["occurred_at"] = fault.occurred_at.isoformat()
        self._trigger_event(event.event_type.value, attributes)
        self.async_write_ha_state()
