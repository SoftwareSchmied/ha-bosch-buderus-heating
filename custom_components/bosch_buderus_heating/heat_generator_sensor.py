"""The heat generator sensor: one diagnostic entity per gateway.

Its state is the product name of the main heat generator; its attributes
list every heat generator module by role (see ``heat_generator``). The cloud
and the local access fill the same entity: it reads the module lists both
already read (``/system/info`` of the cloud, ``/system/basicInfo`` of the
local access) and never sends a request. Every platform follows the same
rule, iCom gateways included: only modules with a product name count, and
without any (an empty list, older firmware, a start from the stored
inventory) no entity is created, while an existing one keeps its last
state. A room controller reports no heat generator of its own and never
gets the entity. The state is written only when what it shows changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Self

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .control import track_entity_once
from .device import device_info_for_resource, is_rrc_plant
from .heat_generator import (
    HeatGenerators,
    choose,
    diagnostics,
    from_data,
    interpret,
    rows_from_cloud,
    rows_from_local,
)
from .local_api.models import SystemInfo

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .platform_scope import DiscoveryLifecycle

CLOUD_PATH = "/system/info"
LOCAL_PATH = "/system/basicInfo"
_STORED_KEY = "heat_generators"


def heat_generator_unique_id(gateway_id: str) -> str:
    """The same ID for every source and module order."""
    return f"{gateway_id}:heat_generator"


def supported(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    """A room controller never gets the entity: its module list (empty on
    the controllers seen so far) names no heat generator."""
    return not is_rrc_plant(coordinator)


class HeatGeneratorInputs:
    """The current answer of each source, worked out once per answer."""

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        self._coordinator = coordinator
        self._cloud: tuple[object, HeatGenerators | None] | None = None
        self._local: tuple[object, HeatGenerators | None] | None = None

    def cloud(self) -> HeatGenerators | None:
        snapshot = (self._coordinator.data or {}).get(CLOUD_PATH)
        if snapshot is None or not snapshot.available:
            return None
        if self._cloud is not None and self._cloud[0] is snapshot:
            return self._cloud[1]
        result = interpret(
            rows_from_cloud(snapshot.resource.values),
            "cloud",
            observed_at=snapshot.last_success,
        )
        self._cloud = (snapshot, result)
        return result

    def local(self) -> HeatGenerators | None:
        selection = getattr(self._coordinator, "local_selection", None)
        if selection is None:
            return None
        newest: Any = None
        for state in selection.local_states():
            for observation in getattr(state, "current_observations", ()) or ():
                if (
                    observation.request.path == LOCAL_PATH
                    and isinstance(observation.data, SystemInfo)
                    and observation.data.status == "completed"
                    and (newest is None or observation.observed_at > newest.observed_at)
                ):
                    newest = observation
        if newest is None:
            return None
        if self._local is not None and self._local[0] is newest:
            return self._local[1]
        result = interpret(
            rows_from_local(newest.data.modules),
            "local",
            observed_at=newest.observed_at,
        )
        self._local = (newest, result)
        return result

    def any(self) -> bool:
        return self.cloud() is not None or self.local() is not None


def heat_generator_diagnostics(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> dict[str, Any] | None:
    """The diagnostics block; ``None`` for room controllers."""
    if not supported(coordinator):
        return None
    inputs = HeatGeneratorInputs(coordinator)
    return diagnostics(inputs.cloud(), inputs.local())


@dataclass(frozen=True, slots=True)
class _Stored(ExtraStoredData):
    """The read fields of the shown modules; never a serial number."""

    data: dict[str, object] | None

    def as_dict(self) -> dict[str, Any]:
        return {_STORED_KEY: self.data}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        data = value.get(_STORED_KEY)
        return cls(data if isinstance(data, dict) else None)


class BoschBuderusHeatGeneratorSensor(
    CoordinatorEntity["BoschBuderusDataUpdateCoordinator"],
    RestoreEntity,
    SensorEntity,
):
    """The heat generators of one gateway, by role."""

    _attr_has_entity_name = True
    _attr_translation_key = "heat_generator"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:heat-pump-outline"

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = heat_generator_unique_id(coordinator.gateway.gateway_id)
        self._inputs = HeatGeneratorInputs(coordinator)
        self._shown: HeatGenerators | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/system")

    @property
    def available(self) -> bool:
        # The last known heat generators stay shown without a current answer.
        return True

    @property
    def native_value(self) -> str | None:
        return self._shown.state if self._shown is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        return self._shown.attributes() if self._shown is not None else None

    @property
    def extra_restore_state_data(self) -> _Stored:
        return _Stored(self._shown.to_data() if self._shown is not None else None)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (stored := await self.async_get_last_extra_data()) is not None:
            # Worked out again with the table of this version.
            self._shown = from_data(_Stored.from_dict(stored.as_dict()).data)
        self._refresh()

    def _refresh(self) -> bool:
        """Take the current answer; True when what is shown changed."""
        chosen, _conflict = choose(
            self._inputs.cloud(), self._inputs.local(), self._shown
        )
        if chosen is None or chosen == self._shown:
            return False
        self._shown = chosen
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._refresh():
            self.async_write_ha_state()

    async def async_update(self) -> None:
        """The module lists arrive with the regular reads; never a request."""


def track_heat_generator_sensor(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entity once a module list exists, or at once when it is
    registered already (it then keeps its last state)."""
    inputs = HeatGeneratorInputs(coordinator)
    unique_id = heat_generator_unique_id(coordinator.gateway.gateway_id)

    def ready() -> bool:
        if not supported(coordinator):
            return False
        registered = (
            er.async_get(coordinator.hass).async_get_entity_id(
                "sensor", DOMAIN, unique_id
            )
            is not None
        )
        return registered or inputs.any()

    track_entity_once(
        entry,
        coordinator,
        async_add_entities,
        ready,
        partial(BoschBuderusHeatGeneratorSensor, coordinator),
    )
