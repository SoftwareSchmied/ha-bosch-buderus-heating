"""Combined hot-water readings and operation controls for PointT circuits."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.water_heater import WaterHeaterEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import rrc
from .const import DOMAIN
from .control import HeldCapabilities, async_set_control
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness, SnapshotSource
from .device import device_info_for_resource, grouped_entity_name
from .entity_resources import fresh_resource, temperature
from .ha_compat import temperature_members
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource
from .rrc_contract_dhw import OPERATION_MODE_POLICY as RRC_OPERATION_MODE_POLICY
from .writes import (
    DHW_OPERATION_MODE_POLICY,
    EnumWritePolicy,
    assess_control,
    context_of,
)

if TYPE_CHECKING:
    from homeassistant.components.water_heater.const import WaterHeaterEntityFeature
else:
    from homeassistant.components.water_heater import WaterHeaterEntityFeature

_MODE_PATH = re.compile(r"^(/dhwCircuits/dhw\d+)/operationMode$")
# Keep the exact wire spelling. In particular, `eco` denotes Eco+, not Eco.
_OPERATIONS = {
    "Off": "off",
    "low": "low",
    "high": "high",
    "ownprogram": "ownprogram",
    "eco": "eco",
    "HCprogram": "follow_heating_program",
}
# W3: an RRC room controller may read its "Off" back as "off". Only there;
# other gateways keep their exact spelling.
_RRC_OPERATIONS = {**_OPERATIONS, "off": "off"}
_POLICIES = (DHW_OPERATION_MODE_POLICY, RRC_OPERATION_MODE_POLICY)


def track_pointt_water_heaters(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add each discovered circuit once, including later metadata recovery."""
    seen: set[str] = set()

    @callback
    def discover() -> None:
        additions = []
        for path in coordinator.data or {}:
            match = _MODE_PATH.fullmatch(path)
            if match is None or match[1] in seen:
                continue
            entity = PointTWaterHeater(coordinator, match[1])
            if not entity.available:
                if path not in coordinator.stored_resources():
                    continue
                # Started with the stored inventory (P47): add it unavailable,
                # but only if a normal start with these values would add it.
                with coordinator.stored_as_fresh():
                    if not entity.available:
                        continue
            seen.add(match[1])
            additions.append(entity)
        if additions:
            add_entities(additions)

    entry.async_on_unload(coordinator.async_add_listener(discover))
    discover()


@temperature_members
class PointTWaterHeater(
    HeldCapabilities,
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    WaterHeaterEntity,
):
    """Show the circuit's own readings without treating start levels as targets."""

    _attr_has_entity_name = True
    _attr_translation_key = "pointt_dhw"
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = 0.1
    _attr_icon = "mdi:water-boiler"

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, root: str
    ) -> None:
        super().__init__(coordinator)
        self.root = root
        self._target_not_before: datetime | None = self._mode_write_time()
        key = root.strip("/").replace("/", ":")
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{key}:water_heater"
        self._attr_name = grouped_entity_name(
            coordinator, root + "/operationMode", ""
        ).rstrip(" \N{EN DASH}")

    def _mode_write_time(self) -> datetime | None:
        mode = (self.coordinator.data or {}).get(self.root + "/operationMode")
        if mode is not None and mode.source is SnapshotSource.WRITE:
            return mode.last_attempt or mode.last_success
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        # Mode and temperature polling use separate groups. Keep the write's
        # freshness boundary even when the mode alone has been polled again.
        if (written_at := self._mode_write_time()) is not None:
            self._target_not_before = written_at
        super()._handle_coordinator_update()

    def _resource(self, suffix: str) -> Resource | None:
        resource = fresh_resource(self.coordinator, self.root + "/" + suffix)
        return resource if resource is not None and not resource.references else None

    @property
    def _operations(self) -> dict[str, str]:
        """Wire codes and their operations, by the kind of gateway (S11)."""
        if context_of(self.coordinator).rrc_plant:
            return _RRC_OPERATIONS
        return _OPERATIONS

    def _mode_reading(self) -> Resource | None:
        """The mode reading; on a room controller also one it does not use
        now (``used`` false), which stays shown and refuses every change. Not
        available or malformed flags leave no reading, so no water heater."""
        resource = self._resource("operationMode")
        if resource is not None or not context_of(self.coordinator).rrc_plant:
            return resource
        snapshot = (self.coordinator.data or {}).get(self.root + "/operationMode")
        if (
            not self.coordinator.last_update_success
            or snapshot is None
            or not snapshot.available
            or snapshot.freshness is not Freshness.FRESH
            or snapshot.resource.references
            or not snapshot.resource.has_value
            or rrc.flag_state(snapshot.resource) != "unused"
        ):
            return None
        return snapshot.resource

    @property
    def _mode(self) -> Resource | None:
        resource = self._mode_reading()
        if (
            resource is None
            or resource.metadata.resource_type != "stringValue"
            or resource.metadata.unit not in (None, "")
            or not isinstance(resource.value, str)
            or resource.value not in self._operations
        ):
            return None
        return resource

    @property
    def available(self) -> bool:
        return self._mode is not None

    @property
    def current_operation(self) -> str | None:
        mode = self._mode
        return self._operations[str(mode.value)] if mode is not None else None

    @property
    def native_current_temperature(self) -> float | None:
        # The same reading as the hot-water temperature sensor: local first
        # (P13), with the usual checks. Mode, targets and writes stay cloud.
        resource = fresh_resource(
            self.coordinator, self.root + "/actualTemp", displayed=True
        )
        return temperature(resource if resource and not resource.references else None)

    @property
    def native_target_temperature(self) -> float | None:
        # This is the reported active setpoint. The writable temperatureLevels
        # are separate start thresholds, including during automatic operation.
        snapshots = self.coordinator.data or {}
        target = snapshots.get(self.root + "/currentSetpoint")
        written_at = self._mode_write_time() or self._target_not_before
        if (
            written_at is not None
            and target is not None
            and (target.last_success is None or target.last_success < written_at)
        ):
            # Confirmation of a mode write does not confirm its new setpoint.
            # Wait for the next regular reading instead of showing the old one.
            return None
        return temperature(self._resource("currentSetpoint"))

    def _writable(self) -> tuple[EnumWritePolicy, dict[str, str]] | None:
        """The mode's write policy and each offered operation's wire code.

        The first advertised code of an operation wins, in the order of
        ``_OPERATIONS`` ("Off" before "off").
        """
        mode = self._mode
        if mode is None:
            return None
        assessment = assess_control(mode, context_of(self.coordinator))
        policy = assessment.policy
        if (
            not assessment.eligible
            or not isinstance(policy, EnumWritePolicy)
            or policy not in _POLICIES
            or assessment.current_value_issue is not None
        ):
            return None
        codes: dict[str, str] = {}
        for raw, value in self._operations.items():
            if raw in assessment.options:
                codes.setdefault(value, raw)
        return policy, codes

    @property
    def operation_list(self) -> list[str]:
        writable = self._writable()
        if writable is None:
            return []
        _policy, codes = writable
        # In the order of the operations, not of the codes.
        order = list(dict.fromkeys(_RRC_OPERATIONS.values()))
        return sorted(codes, key=order.index)

    @property
    def supported_features(self) -> WaterHeaterEntityFeature:
        return (
            WaterHeaterEntityFeature.OPERATION_MODE
            if self.operation_list
            else WaterHeaterEntityFeature(0)
        )

    def _live_capabilities(self) -> dict[str, Any]:
        # HA's base defaults would invent temperature-control bounds. This
        # entity has no direct target-temperature command or temperature step.
        return {"operation_list": self.operation_list} if self.operation_list else {}

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.root)

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        mode = self._mode
        writable = self._writable()
        if mode is None or writable is None or operation_mode not in writable[1]:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="write_validation_failed"
            )
        policy, codes = writable
        await async_set_control(
            self.coordinator,
            mode.path,
            codes[operation_mode],
            policy,
            expected=(mode,),
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="pointt_dhw_temperature_read_only",
        )
