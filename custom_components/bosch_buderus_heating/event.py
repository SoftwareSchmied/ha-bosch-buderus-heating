"""Lifecycle events for PointT system notifications."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.event import EventEntity, EventExtraStoredData
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import BoschBuderusConfigEntry
from .coordinator import BoschBuderusDataUpdateCoordinator
from .device import device_info_for_resource
from .faults import (
    FaultEventType,
    FaultLifecycleEvent,
    fault_severity_label,
    fault_summary,
)
from .platform_scope import PlatformScope, setup_platform


@dataclass
class _NotificationEventData(EventExtraStoredData):
    """Keep the real event timestamp separate from entity availability."""

    last_event_timestamp: str | None = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_platform(entry, Platform.EVENT, async_add_entities, _setup_entities)


def _setup_entities(
    entry: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one bounded event stream per configured gateway."""
    async_add_entities(
        BoschBuderusNotificationEvent(coordinator) for coordinator in entry.coordinators
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

    @property
    def extra_restore_state_data(self) -> EventExtraStoredData:
        """Retain the timestamp even when HA stores an unavailable state."""
        stored = super().extra_restore_state_data
        return _NotificationEventData(
            stored.last_event_type, stored.last_event_attributes, self.state
        )

    async def async_get_last_state(self) -> State | None:
        """Let native event restoration recover the original timestamp."""
        state = await super().async_get_last_state()
        if state is None or state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return state
        extra = await self.async_get_last_extra_data()
        if extra is None:
            return state
        data = extra.as_dict()
        timestamp = data.get("last_event_timestamp")
        if (
            not isinstance(timestamp, str)
            or data.get("last_event_type") not in self.event_types
            or "last_event_attributes" not in data
            or not isinstance(data["last_event_attributes"], dict | type(None))
        ):
            return state
        try:
            parsed = dt_util.parse_datetime(timestamp)
        except ValueError, OverflowError:
            return state
        if parsed is None or parsed.tzinfo is None:
            return state
        # Only the restore input changes. Actual availability and event delivery
        # remain managed by HA; replaying _trigger_event would invent an event.
        return State.from_dict(state.as_dict() | {"state": timestamp})

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
