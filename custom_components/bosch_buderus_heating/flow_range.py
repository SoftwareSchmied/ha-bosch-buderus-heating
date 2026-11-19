"""Whether a volume flow is below its sensor's measuring range (0.9.2).

When the flow is too low to measure, the heat pump reports a declared state
instead of a value: locally ``LOW_FLOW``/``lowflow`` (the raw value 65535),
in the cloud a value listed as below the measuring range. The flow sensor
then shows unknown with the attribute ``reported_state: low_flow``. These
diagnostic binary sensors show the same as on/off, so the time below the
range can be recorded: on while the state is reported, off while a value is
measured (a real 0 included), unknown without a current reading.

They read what the flow sensors already read and never send a request.
Disabled by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Final, Protocol, cast

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .control import track_entity_once
from .device import device_info_for_resource
from .local_only import below_measuring_range
from .observations import ValueStatus

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .observations import Observation
    from .platform_scope import DiscoveryLifecycle


@dataclass(frozen=True, slots=True)
class FlowRange:
    """One binary sensor and the flow readings it follows, in order."""

    key: str
    paths: tuple[str, ...]


# The primary flow (PC0): the heat source's flow, else the controller signal.
PRIMARY: Final = FlowRange(
    "primary_flow_below_range",
    ("/heatSources/hs1/pumpVolumeFlow", "/signals/SRC.SystemPumpVolumeFlow"),
)
# The flow of heating circuit 1 (PC1), a controller signal.
CIRCUIT_1: Final = FlowRange(
    "circuit_1_flow_below_range",
    ("/signals/SRC.HC1.FlowCtrl.PumpVolumeFlow",),
)
FLOW_RANGES: Final = (PRIMARY, CIRCUIT_1)


@dataclass(frozen=True, slots=True)
class FlowState:
    """``below``: True below the range, False with a measured value."""

    below: bool
    path: str
    source: str


class _LocalPlantView(Protocol):
    def local_only_reading(self, path: str) -> Observation | None: ...


def _local_reading(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> Observation | None:
    """The current local reading of a path only the local access reads."""
    if getattr(coordinator, "read_only_view", False) is True:
        return cast("_LocalPlantView", coordinator).local_only_reading(path)
    selection = coordinator.local_selection
    return selection.local_only_reading(path) if selection is not None else None


def _number(value: object) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def flow_state(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> FlowState | None:
    """The state of one flow reading; None without a current reading.

    A cloud resource is read as its sensor shows it (a current local reading
    first, else the cloud reading within its validity limit). A device state
    other than the low-flow one (for example an open sensor) says nothing
    about the range and gives None, like a missing reading.
    """
    if path in coordinator.resources:
        snapshot = coordinator.display_snapshot(path)
        if snapshot is None or not snapshot.available:
            return None
        value = snapshot.resource.value
        metadata = snapshot.resource.metadata
        if not _number(value):
            return None
        if value in metadata.below_range_values:
            return FlowState(True, path, "cloud")
        if value in metadata.numeric_error_values:
            return None
        return FlowState(False, path, "cloud")
    reading = _local_reading(coordinator, path)
    if reading is None:
        return None
    if below_measuring_range(reading):
        return FlowState(True, path, "local")
    if reading.status is ValueStatus.VALID and _number(reading.value):
        return FlowState(False, path, "local")
    return None


def current_state(
    coordinator: BoschBuderusDataUpdateCoordinator, flow: FlowRange
) -> FlowState | None:
    """The first flow reading of the list that has a current state."""
    for path in flow.paths:
        if (state := flow_state(coordinator, path)) is not None:
            return state
    return None


def _present(coordinator: BoschBuderusDataUpdateCoordinator, flow: FlowRange) -> bool:
    """One of the flow readings exists on this plant (with or without a
    current value)."""
    return any(
        path in coordinator.resources or _local_reading(coordinator, path) is not None
        for path in flow.paths
    )


def flow_range_unique_id(gateway_id: str, flow: FlowRange) -> str:
    return f"{gateway_id}:{flow.key}"


class BoschBuderusFlowRangeBinarySensor(
    CoordinatorEntity["BoschBuderusDataUpdateCoordinator"],
    BinarySensorEntity,
):
    """On while a volume flow is below its sensor's measuring range."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, flow: FlowRange
    ) -> None:
        super().__init__(coordinator)
        self._flow = flow
        self._attr_translation_key = flow.key
        self._attr_unique_id = flow_range_unique_id(
            coordinator.gateway.gateway_id, flow
        )

    @property
    def device_info(self) -> DeviceInfo:
        # The device of the flow reading it follows (the first one listed).
        return device_info_for_resource(self.coordinator, self._flow.paths[0])

    @property
    def available(self) -> bool:
        return current_state(self.coordinator, self._flow) is not None

    @property
    def is_on(self) -> bool | None:
        state = current_state(self.coordinator, self._flow)
        return state.below if state is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        state = current_state(self.coordinator, self._flow)
        if state is None:
            return None
        return {"derived_from": state.path, "source": state.source}

    async def async_update(self) -> None:
        """The flow readings arrive with the regular reads; never a request."""


def track_flow_range_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add each binary sensor once one of its flow readings exists, or at
    once when it is registered already."""
    registry = er.async_get(coordinator.hass)
    for flow in FLOW_RANGES:
        unique_id = flow_range_unique_id(coordinator.gateway.gateway_id, flow)

        def ready(flow: FlowRange = flow, unique_id: str = unique_id) -> bool:
            registered = (
                registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)
                is not None
            )
            return registered or _present(coordinator, flow)

        track_entity_once(
            entry,
            coordinator,
            async_add_entities,
            ready,
            partial(BoschBuderusFlowRangeBinarySensor, coordinator, flow),
        )
