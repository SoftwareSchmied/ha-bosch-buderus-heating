"""Dynamic read-only binary sensors for discovered PointT resources."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import BoschBuderusConfigEntry
from .bacon.access import BaconAccess
from .bacon.dhw_status import track_status
from .bacon.dry_entities import track_entities as track_dry_entities
from .bacon.entity import BaconConnectivity
from .control import track_entity_once
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness, ResourceSnapshot
from .device import device_info_for_resource, grouped_entity_name
from .dhw_safety import assess_dhw_safety, is_dhw_safety_path
from .faults import (
    MAX_FAULT_ATTRIBUTES,
    fault_severity_label,
    fault_summary,
    no_active_faults_label,
)
from .holiday_writes import holiday_resources_from_snapshots
from .holidays import (
    HolidayState,
    is_holiday_resource_path,
    parse_holiday_state,
)
from .platform_scope import DiscoveryLifecycle, PlatformScope, setup_platform
from .pointt import Resource
from .resource_catalog import (
    CapabilityMaturity,
    capability_maturity,
    entity_enabled_by_default,
    resource_name,
    supports_entity,
)
from .sensor import (
    _is_boolean_resource,
    _nested_scalar_keys,
    _nested_value,
    _semantic_key,
    _values_scalar_keys,
)
from .start_counters import is_start_counter_path
from .water_softener import low_salt, softener_capability, softener_value

if TYPE_CHECKING:
    from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
else:
    # Older HA defines this class in the platform module. Newer HA retains
    # that runtime export, but types it at its defining location in const.
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass


@dataclass(frozen=True, kw_only=True)
class BoschBuderusBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe one boolean extracted from a PointT resource."""

    resource_path: str
    value_key: str | None = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_platform(entry, Platform.BINARY_SENSOR, async_add_entities, _setup_entities)


def _setup_entities(
    entry: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create entities from every safe boolean found during discovery."""
    entities: list[BinarySensorEntity] = []
    if (account := entry.bacon) is not None:
        track_dry_entities(entry, account, async_add_entities, "binary_sensor")
        _track_connectivity(entry, account, async_add_entities)
    for coordinator in entry.coordinators:
        entities.append(BoschBuderusSystemFaultBinarySensor(coordinator))
        entities.append(BoschBuderusCloudControlBinarySensor(coordinator))
        track_entity_once(
            entry,
            coordinator,
            async_add_entities,
            partial(_has_holiday_resources, coordinator),
            partial(BoschBuderusHolidayActiveBinarySensor, coordinator),
        )
    async_add_entities(entities)
    if account is not None:
        track_status(entry, account, async_add_entities)
    for coordinator in entry.coordinators:
        _track_binary_sensors(entry, coordinator, async_add_entities)


def _track_connectivity(
    entry: PlatformScope,
    account: BaconAccess,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Connectivity per device, once the account is known (P47).

    The identity derives from the signed-in account. Managed accounts are
    published only after sign-in; a legacy entry that started during an
    outage learns it with the first sign-in in the background.
    """
    added = False

    @callback
    def discover() -> None:
        nonlocal added
        if added or account.client.subject is None:
            return
        # A stopped scope (source or entry unloading) adds nothing.
        if not entry.current():
            return
        added = True
        async_add_entities(
            [BaconConnectivity(account, serial) for serial in account.selected]
        )

    discover()
    if not added:
        entry.async_on_unload(account.add_listener(discover))


def _has_holiday_resources(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    return any(is_holiday_resource_path(path) for path in coordinator.resources)


def _track_binary_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add recovered capabilities once, using the shared polling listener."""
    seen: set[str] = set()

    @callback
    def discover() -> None:
        resources = dict(coordinator.resources)
        resources.update(
            {
                path: snapshot.resource
                for path, snapshot in (coordinator.data or {}).items()
                if snapshot.available
            }
        )
        additions = []
        for description in build_binary_sensor_descriptions(resources):
            if description.key not in seen:
                seen.add(description.key)
                additions.append(BoschBuderusBinarySensor(coordinator, description))
        if additions:
            async_add_entities(additions)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class BoschBuderusHolidayActiveBinarySensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], BinarySensorEntity
):
    """Report the PointT holiday state independently from away mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "holiday_active"
    _attr_icon = "mdi:palm-tree"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:holiday_active"

    @property
    def available(self) -> bool:
        return super().available and any(
            is_holiday_resource_path(path) for path in self._available_resources
        )

    @property
    def is_on(self) -> bool | None:
        return self._state.active

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self._state
        now = datetime.now(UTC)
        return {
            "period_count": len(state.periods),
            "active_period_count": sum(
                period.start <= now < period.end for period in state.periods
            ),
            "invalid_period_count": state.invalid_period_count,
        }

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")

    @property
    def _available_resources(self) -> dict[str, Resource]:
        return holiday_resources_from_snapshots(self.coordinator.data or {})

    @property
    def _state(self) -> HolidayState:
        return parse_holiday_state(
            self._available_resources,
            fallback_timezone=dt_util.get_default_time_zone(),
            timezones=self.coordinator.holiday_timezones,
        )


class BoschBuderusCloudControlBinarySensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], BinarySensorEntity
):
    """Whether settings can be changed through the cloud now, and why not.

    Always available: it reports the state of the write path itself. Readings
    may still be shown through sensors while it is off.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "cloud_control"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:remote"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:cloud_control"
        self._status: tuple[str | None, datetime | None] | None = None
        self._cancel_end: Callable[[], None] | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")

    @property
    def available(self) -> bool:
        return True

    @property
    def _current(self) -> tuple[str | None, datetime | None]:
        # One status per state write: state and attributes never disagree.
        return self._status or self.coordinator.control_status()

    @property
    def is_on(self) -> bool:
        return self._current[0] is None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        reason, until = self._current
        return {
            "reason": reason,
            "until": until.isoformat(timespec="seconds") if until else None,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._refresh_status()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_pause_end()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_status()
        super()._handle_coordinator_update()

    @callback
    def _refresh_status(self) -> None:
        self._status = self.coordinator.control_status()
        self._cancel_pause_end()
        until = self._status[1]
        if until is not None and self.hass is not None:
            # A pause ends without a coordinator update: re-evaluate then.
            delay = max(1.0, (until - dt_util.utcnow()).total_seconds() + 1)
            self._cancel_end = async_call_later(self.hass, delay, self._pause_ended)

    @callback
    def _pause_ended(self, _now: datetime) -> None:
        self._cancel_end = None
        self._refresh_status()
        self.async_write_ha_state()

    @callback
    def _cancel_pause_end(self) -> None:
        if self._cancel_end is not None:
            self._cancel_end()
            self._cancel_end = None


class BoschBuderusSystemFaultBinarySensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], BinarySensorEntity
):
    """Report whether an actionable or unknown system fault is active."""

    _attr_has_entity_name = True
    _attr_translation_key = "system_fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert-circle"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:system_fault"

    @property
    def available(self) -> bool:
        faults = self.coordinator.faults
        if faults.local_authority is True:
            # Without a cloud (P49b) only a current local list gives a state.
            return faults.local_list_current
        return super().available and faults.has_known_state

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.faults.active_faults)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        tracker = self.coordinator.faults
        codes = sorted(
            {item.code for item in tracker.active_faults if item.code is not None}
        )
        attributes: dict[str, object] = {
            "active_fault_count": len(tracker.active_faults),
            "active_notification_count": len(tracker.active),
            "highest_severity": (
                tracker.highest_severity.value if tracker.highest_severity else None
            ),
            "codes": codes[:MAX_FAULT_ATTRIBUTES],
            "codes_truncated": len(codes) > MAX_FAULT_ATTRIBUTES,
        }
        if tracker.active_faults:
            attributes["summary"] = fault_summary(
                tracker.active_faults[0], self.coordinator.hass.config.language
            )
            attributes["highest_severity_label"] = fault_severity_label(
                tracker.active_faults[0].severity,
                self.coordinator.hass.config.language,
            )
        else:
            attributes["summary"] = no_active_faults_label(
                self.coordinator.hass.config.language
            )
        return attributes

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")


def build_binary_sensor_descriptions(
    resources: Mapping[str, Resource],
) -> tuple[BoschBuderusBinarySensorEntityDescription, ...]:
    """Build stable descriptions for all discovered safe boolean states."""
    descriptions: list[BoschBuderusBinarySensorEntityDescription] = []
    for resource in resources.values():
        if is_start_counter_path(resource.path) or not supports_entity(resource):
            continue
        if (
            softener := softener_capability(resource.path)
        ) is not None and softener.suffix == "sensor/saltLevel":
            descriptions.append(_description(resource, "low_salt"))
        keys = _nested_scalar_keys(resource.value, include_booleans=True)
        if not keys and resource.values:
            keys = _values_scalar_keys(resource, include_booleans=True)
        if keys:
            descriptions.extend(_description(resource, key) for key in keys)
        elif _is_boolean_resource(resource):
            descriptions.append(_description(resource, None))
    return tuple(descriptions)


def _description(
    resource: Resource, value_key: str | None
) -> BoschBuderusBinarySensorEntityDescription:
    return BoschBuderusBinarySensorEntityDescription(
        key=_semantic_key(resource.path, value_key),
        name=resource_name(resource.path, value_key),
        resource_path=resource.path,
        value_key=value_key,
        entity_category=(
            EntityCategory.DIAGNOSTIC
            if capability_maturity(resource.path) is CapabilityMaturity.UNDERSTOOD
            and softener_capability(resource.path) is None
            else None
        ),
        device_class=BinarySensorDeviceClass.PROBLEM
        if value_key == "low_salt"
        else None,
        entity_registry_enabled_default=entity_enabled_by_default(resource.path),
    )


class BoschBuderusBinarySensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    BinarySensorEntity,
):
    """Represent one boolean value from a discovered PointT resource."""

    _attr_has_entity_name = True
    entity_description: BoschBuderusBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: BoschBuderusBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        display_name = resource_name(
            description.resource_path,
            description.value_key,
            language=coordinator.hass.config.language,
        )
        self._attr_name = grouped_entity_name(
            coordinator, description.resource_path, display_name
        )
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{description.key}"

    @property
    def available(self) -> bool:
        snapshot = self._snapshot
        # The displayed reading decides, not the last cloud cycle: a current
        # local reading or a cloud reading within its validity limit stays
        # available while the cloud fails or pauses.
        if snapshot is None or not snapshot.available:
            return False
        if self.entity_description.value_key == "low_salt" and not super().available:
            return False
        if (
            softener_capability(snapshot.resource.path) is not None
            or is_dhw_safety_path(snapshot.resource.path)
        ) and snapshot.freshness is not Freshness.FRESH:
            return False
        if self.entity_description.value_key == "low_salt":
            threshold = self._threshold_snapshot
            return threshold is None or (
                threshold.available and threshold.freshness is Freshness.FRESH
            )
        return True

    @property
    def is_on(self) -> bool | None:
        snapshot = self._snapshot
        if snapshot is None:
            return None
        resource = snapshot.resource
        if is_dhw_safety_path(resource.path):
            if not snapshot.available or snapshot.freshness is not Freshness.FRESH:
                return None
            return assess_dhw_safety(resource).value
        if softener_capability(resource.path) is not None:
            if not snapshot.available or snapshot.freshness is not Freshness.FRESH:
                return None
            if self.entity_description.value_key == "low_salt":
                threshold = self._threshold_snapshot
                if threshold is not None and (
                    not threshold.available
                    or threshold.freshness is not Freshness.FRESH
                ):
                    return None
                return low_salt(
                    resource,
                    threshold.resource
                    if threshold is not None
                    and threshold.available
                    and threshold.freshness is Freshness.FRESH
                    else None,
                )
            result = softener_value(resource)
            return result if isinstance(result, bool) else None
        value = (
            _nested_value(resource, self.entity_description.value_key)
            if self.entity_description.value_key is not None
            else resource.value
        )
        return value if isinstance(value, bool) else None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(
            self.coordinator, self.entity_description.resource_path
        )

    @property
    def _snapshot(self) -> ResourceSnapshot | None:
        path = self.entity_description.resource_path
        if self.entity_description.value_key == "low_salt":
            # Combined with the alarm threshold: stored readings, strict rule.
            return (self.coordinator.data or {}).get(path)
        return self.coordinator.display_snapshot(path)

    @property
    def _threshold_snapshot(self) -> ResourceSnapshot | None:
        path = (
            self.entity_description.resource_path.removesuffix("saltLevel")
            + "saltLevelAlarm"
        )
        return (self.coordinator.data or {}).get(path)
