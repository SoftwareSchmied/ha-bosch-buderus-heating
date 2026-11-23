"""The heat generator sensor: one diagnostic entity per gateway.

Its state is the product name of the main heat generator; its attributes
list every heat generator module by role (see ``heat_generator``). The cloud
and the local access fill the same entity: it reads the module lists both
already read (``/system/info`` of the cloud, ``/system/basicInfo`` of the
local access) and never sends a request. Every platform follows the same
rules, iCom gateways included. The attribute ``heat_source_type`` (air,
brine or water) comes from evidence readings (``heat_source_kind``), not
from the module list.

The attributes are an interface for cards (contract 1, ``contract_version``):
every key is always present, the keys of 0.9.1 to 0.9.3-beta.5 keep their
meaning, and the plant model (``plant_inputs``) adds the plant kind with its
evidence, fuel and cascade, and the components of the system with place,
type number, type name, role, basis and product name. The gateway's own
rows and rows without a valid type are left out; versions and serial
numbers never appear.

Without a module with a product name, the state is the name of the
component at the heat generator's place (``state_basis: component_type``,
for example the control unit of an iCom heat pump); otherwise it stays
unknown. The entity is created once a named module, evidence of the heat
source kind, a component at the heat generator's place or a known plant
kind (heat pump, hybrid, boiler) is there; an existing one keeps its last
state. A room controller reports no heat generator of its own and never
gets the entity. The state is written only when what it shows changes; a
restart keeps it as it was, because the plant model's stored row is read
before the first state is written.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Self
from weakref import WeakKeyDictionary

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import heat_generator
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
    unnamed_hardware_cloud,
    unnamed_hardware_local,
)
from .heat_source_kind import KINDS, SourceKind, classify
from .local_api.models import SystemInfo
from .plant_detection import EVIDENCE_CODES, HEAT_GENERATOR_PLACE, PlantResult
from .plant_inputs import (
    ShownComponents,
    components,
    current_plant,
    describe,
    existing_tracker,
)
from .system_components import (
    INVALID_TYPE_ID,
    ComponentTable,
    is_gateway_row,
    load_table,
)

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .component_rows import ComponentRow
    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .platform_scope import DiscoveryLifecycle

_LOGGER = logging.getLogger(__name__)

CLOUD_PATH = "/system/info"
LOCAL_PATH = "/system/basicInfo"
_STORED_KEY = "heat_generators"
_KIND_KEY = "heat_source_type"
# What the sensor of a gateway shows, so the diagnostics name the same answer.
_SHOWN: WeakKeyDictionary[object, HeatGenerators] = WeakKeyDictionary()

# The interface for cards (see docs/entities.md, "Interface for cards").
CONTRACT_VERSION: Final = 1
# The evidence the sensor shows. ``stored`` only tells that an input came
# from the stored row (after a restart, before the next reading); it stays in
# the diagnostics, so a restart never changes the sensor.
SENSOR_EVIDENCE: Final = tuple(code for code in EVIDENCE_CODES if code != "stored")
MAX_COMPONENTS: Final = 32
STATE_BASES: Final = ("product_name", "component_type")
# A plant kind that gives the entity even without a named module.
_KINDS_WITH_ENTITY: Final = frozenset({"heat_pump", "hybrid", "boiler"})
# The keys of 0.9.1 to 0.9.3-beta.5 and the keys of contract 1.
PREVIOUS_KEYS: Final = (
    "heat_generators",
    "primary",
    "outdoor_unit",
    "source",
    "table_version",
    "known",
    "heat_source_type",
)
CONTRACT_KEYS: Final = (
    "contract_version",
    "state_basis",
    "plant_kind",
    "plant_kind_evidence",
    "fuel",
    "cascade",
    "components",
    "components_source",
    "components_table_version",
)


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

    def unnamed(self) -> dict[str, dict[str, int]]:
        """Module rows without a product name, by hardware type."""
        result: dict[str, dict[str, int]] = {}
        snapshot = (self._coordinator.data or {}).get(CLOUD_PATH)
        if snapshot is not None and snapshot.available:
            result["cloud"] = unnamed_hardware_cloud(snapshot.resource.values)
        newest = self._newest_local()
        if newest is not None:
            result["local"] = unnamed_hardware_local(newest.data.modules)
        return result

    def _newest_local(self) -> Any:
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
        return newest

    def local(self) -> HeatGenerators | None:
        newest = self._newest_local()
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


def shown_component(row: ComponentRow) -> bool:
    """A row the sensor lists: a valid type that is not the gateway's own."""
    return (
        row.type_id is not None
        and row.type_id != INVALID_TYPE_ID
        and not is_gateway_row(row.type_id, row.tok)
    )


def component_entries(
    rows: Iterable[ComponentRow], table: ComponentTable | None
) -> tuple[Mapping[str, object], ...]:
    """The entries of ``components``, sorted by place, type number and
    product name (at most 32); none without a component table."""
    if table is None:
        return ()
    entries: list[dict[str, object]] = []
    for row in rows:
        if not shown_component(row):
            continue
        description = describe(row, table)
        entries.append(
            {
                "place": description.place,
                "place_number": row.tok,
                "type_number": row.type_id,
                "type_name": description.type_name,
                "role": description.role,
                "basis": description.basis,
                "product_name": row.product_name,
            }
        )
    entries.sort(
        key=lambda item: (
            item["place_number"] if isinstance(item["place_number"], int) else -1,
            item["type_number"] if isinstance(item["type_number"], int) else -1,
            str(item["product_name"] or ""),
        )
    )
    return tuple(entries[:MAX_COMPONENTS])


def component_state(
    rows: Iterable[ComponentRow], table: ComponentTable | None
) -> str | None:
    """The name of the component at the heat generator's place, for a plant
    without a named module: a component with a role first, then the smallest
    type number; never by the order of the answer. ``None`` without one."""
    if table is None:
        return None
    candidates: list[tuple[int, int, str]] = []
    for row in rows:
        if row.tok != HEAT_GENERATOR_PLACE or not shown_component(row):
            continue
        description = describe(row, table)
        if description.type_name and row.type_id is not None:
            candidates.append(
                (0 if description.role else 1, row.type_id, description.type_name)
            )
    return min(candidates)[2] if candidates else None


@dataclass(frozen=True, slots=True)
class PlantView:
    """What the plant model adds to the sensor (contract 1)."""

    component_state: str | None = None
    plant_kind: str | None = None
    evidence: tuple[str, ...] = ()
    fuel: str | None = None
    cascade: bool | None = None
    components: tuple[Mapping[str, object], ...] = ()
    components_source: str | None = None
    components_table_version: int | None = None


def plant_view(
    plant: PlantResult | None,
    shown: ShownComponents | None,
    table: ComponentTable | None,
) -> PlantView:
    """The plant model's part of the sensor from its result and components."""
    rows: tuple[ComponentRow, ...] = shown.rows if shown is not None else ()
    return PlantView(
        component_state=component_state(rows, table),
        plant_kind=plant.plant_kind if plant is not None else None,
        evidence=(
            tuple(code for code in plant.evidence if code in SENSOR_EVIDENCE)
            if plant is not None
            else ()
        ),
        fuel=plant.fuel if plant is not None else None,
        cascade=plant.cascade if plant is not None else None,
        components=component_entries(rows, table),
        components_source=(
            shown.source if shown is not None and table is not None else None
        ),
        components_table_version=table.table_version if table is not None else None,
    )


def contract_attributes(
    shown: HeatGenerators | None, heat_source_type: str | None, view: PlantView
) -> dict[str, object]:
    """Every attribute of contract 1; the keys of 0.9.1 to 0.9.3-beta.5 keep
    their values, a plant without a named module gets them empty."""
    if shown is not None:
        previous: dict[str, object] = shown.attributes()
        basis: str | None = "product_name"
    else:
        previous = {
            "heat_generators": [],
            "primary": {
                "family": None,
                "construction": None,
                "power_kw": None,
                "brand_family": None,
                "outdoor_power_kw": None,
            },
            "outdoor_unit": None,
            "source": None,
            "table_version": heat_generator.TABLE.version,
            "known": False,
        }
        basis = "component_type" if view.component_state is not None else None
    return {
        **previous,
        "heat_source_type": heat_source_type,
        "contract_version": CONTRACT_VERSION,
        "state_basis": basis,
        "plant_kind": view.plant_kind,
        "plant_kind_evidence": list(view.evidence),
        "fuel": view.fuel,
        "cascade": view.cascade,
        "components": [dict(item) for item in view.components],
        "components_source": view.components_source,
        "components_table_version": view.components_table_version,
    }


class PlantViews:
    """The plant model's part of one gateway's sensor, worked out once per
    component list and plant result."""

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        self._coordinator = coordinator
        self._key: tuple[object, ...] | None = None
        self._view: PlantView | None = None

    def current(self) -> PlantView | None:
        """``None`` while the plant model has no result (not started yet,
        its stored row not read yet, or not for this gateway)."""
        tracker = existing_tracker(self._coordinator)
        if tracker is None or not tracker.loaded:
            return None
        plant = current_plant(self._coordinator)
        shown = components(self._coordinator)
        if plant is None or shown is None:
            return None
        table = load_table()
        key = (plant, shown.source, shown.rows, id(table))
        if self._view is None or key != self._key:
            self._key = key
            self._view = plant_view(plant, shown, table)
        return self._view


def plant_ready(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    """A known plant kind (heat pump, hybrid, boiler) or a component at the
    heat generator's place gives the entity without a named module."""
    try:
        plant = current_plant(coordinator)
        shown = components(coordinator)
    except Exception:  # the plant model never stops the entity's discovery
        _LOGGER.debug("Plant model not available", exc_info=True)
        return False
    if plant is not None and plant.plant_kind in _KINDS_WITH_ENTITY:
        return True
    return (
        shown is not None
        and load_table() is not None
        and any(
            row.tok == HEAT_GENERATOR_PLACE and shown_component(row)
            for row in shown.rows
        )
    )


def heat_generator_diagnostics(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> dict[str, Any] | None:
    """The diagnostics block; ``None`` for room controllers.

    Never fails: an unexpected error gives a fixed note, and the rest of the
    report stays available.
    """
    try:
        if not supported(coordinator):
            return None
        inputs = HeatGeneratorInputs(coordinator)
        report = diagnostics(inputs.cloud(), inputs.local(), shown_by(coordinator))
        kind = classify(coordinator)
        report["heat_source_type"] = kind.kind
        report["heat_source_type_evidence"] = list(kind.evidence)
        report["unnamed_modules"] = inputs.unnamed()
        return report
    except Exception:  # the report must stay available
        return {"error": "unavailable"}


def shown_by(coordinator: object) -> HeatGenerators | None:
    """What the sensor of this gateway shows, if it exists."""
    try:
        return _SHOWN.get(coordinator)
    except TypeError:  # not weakly referable
        return None


@dataclass(frozen=True, slots=True)
class _Stored(ExtraStoredData):
    """The read fields of the shown modules; never a serial number. The heat
    source kind is kept as well, so it shows from the start on."""

    data: dict[str, object] | None
    kind: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {_STORED_KEY: self.data, _KIND_KEY: self.kind}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        data = value.get(_STORED_KEY)
        kind = value.get(_KIND_KEY)
        return cls(
            data if isinstance(data, dict) else None,
            kind if kind in KINDS else None,
        )


class BoschBuderusHeatGeneratorSensor(
    CoordinatorEntity["BoschBuderusDataUpdateCoordinator"],
    RestoreEntity,
    SensorEntity,
):
    """The heat generators of one gateway, by role."""

    _attr_has_entity_name = True
    _attr_translation_key = "heat_generator"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # The component list is for cards and automations; its history is not
    # recorded (the stored row of the plant model keeps its changes).
    _unrecorded_attributes = frozenset({"components"})

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = heat_generator_unique_id(coordinator.gateway.gateway_id)
        self._inputs = HeatGeneratorInputs(coordinator)
        self._shown: HeatGenerators | None = None
        # The heat source kind from evidence readings, kept while none.
        self._kind = SourceKind(None, ())
        # The plant model's part, kept while the model has no result.
        self._views = PlantViews(coordinator)
        self._view = PlantView()

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/system")

    @property
    def available(self) -> bool:
        # The last known heat generators stay shown without a current answer.
        return True

    @property
    def native_value(self) -> str | None:
        if self._shown is not None:
            return self._shown.state
        # No module with a product name: the component at the heat
        # generator's place, if any.
        return self._view.component_state

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return contract_attributes(self._shown, self._kind.kind, self._view)

    @property
    def extra_restore_state_data(self) -> _Stored:
        return _Stored(
            self._shown.to_data() if self._shown is not None else None,
            self._kind.kind,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The plant model's stored row first, so the first state written
        # after a restart is the one shown before it.
        await self._plant_loaded()
        if (stored := await self.async_get_last_extra_data()) is not None:
            restored = _Stored.from_dict(stored.as_dict())
            # Worked out again with the table of this version.
            self._shown = from_data(restored.data)
            self._remember()
            if restored.kind is not None:
                # Shown until the evidence is read again (no evidence paths).
                self._kind = SourceKind(restored.kind, ())
        self._refresh()

    async def _plant_loaded(self) -> None:
        tracker = existing_tracker(self.coordinator)
        if tracker is None:
            return
        try:
            await tracker.async_load()
        except Exception:  # the plant model never stops the entity
            _LOGGER.debug("Plant model not read", exc_info=True)

    def _refresh(self) -> bool:
        """Take the current answer; True when what is shown changed."""
        kind = classify(self.coordinator)
        changed = kind.kind is not None and kind.kind != self._kind.kind
        if kind.kind is not None:
            self._kind = kind
        chosen, _conflict = choose(
            self._inputs.cloud(), self._inputs.local(), self._shown
        )
        if chosen is not None and chosen != self._shown:
            self._shown = chosen
            self._remember()
            changed = True
        try:
            view = self._views.current()
        except Exception:  # the plant model never stops the entity
            _LOGGER.debug("Plant model not shown", exc_info=True)
            view = None
        if view is not None and view != self._view:
            # Without a result the last one stays shown.
            self._view = view
            changed = True
        return changed

    def _remember(self) -> None:
        """Let the diagnostics name what this sensor shows."""
        with suppress(TypeError):  # not weakly referable
            if self._shown is None:
                _SHOWN.pop(self.coordinator, None)
            else:
                _SHOWN[self.coordinator] = self._shown

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        with suppress(TypeError):  # not weakly referable
            _SHOWN.pop(self.coordinator, None)

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
    """Add the entity once a named module, evidence of the heat source kind,
    a component at the heat generator's place or a known plant kind is there,
    or at once when it is registered already (it then keeps its last state).
    Without a valid model table there is no entity."""
    if not heat_generator.TABLE_AVAILABLE:
        return
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
        # A heat pump without a named module, but with evidence of its
        # heat source kind, gets the entity as well (``known: false``), and
        # so does a plant the plant model knows (contract 1).
        return (
            registered
            or inputs.any()
            or classify(coordinator).kind is not None
            or plant_ready(coordinator)
        )

    track_entity_once(
        entry,
        coordinator,
        async_add_entities,
        ready,
        partial(BoschBuderusHeatGeneratorSensor, coordinator),
    )
