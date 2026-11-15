"""Calculated sensors: circuit activity, net heat, run time per start, overshoot.

Each one is named "(calculated)" and lists its inputs with their origin in
the attribute ``derived_from``. None of them sends a request: they combine
readings the integration reads anyway (``derived_inputs``).

* Activity of each heating circuit (``derived_activity``), enabled.
* Net heat per mode and in total: the produced-heat counters fall while the
  heat pump defrosts (the heat taken back from the water). The net counter
  holds during such a dip and counts again only above the highest reading
  so far, so it is a ``total_increasing`` counter. A drop of more than
  ``RESET_DROP_KWH`` is a replaced or reset counter: the net counter keeps
  its total and continues from the new reading. Saved like the estimated
  energy, never lower after a restart. Disabled by default.
* Average run time per start in heating: the heating operating hours
  divided by the heating starts of a heat source.
* Supply above setpoint: the supply temperature minus its setpoint, while
  the heat source runs for heating (heat source) or the circuit heats
  (heating circuit with its own supply sensor).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from functools import partial
from math import isfinite
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import derived_defrost
from .derived_activity import (
    ACTIVITIES,
    CIRCUIT,
    HEATING,
    circuit_activity,
    circuit_input_paths,
    defrost_state,
    heat_side,
    heat_source_input_paths,
    heat_source_running,
)
from .derived_inputs import (
    Trace,
    energy_part,
    first,
    number,
    read,
    resource_energy_part,
    runtime_hours,
    starts,
)
from .derived_store import derived_store, stored_number
from .device import device_info_for_resource, grouped_entity_name
from .i18n import localize
from .performance import SUPPLY as HEAT_PUMP_SUPPLY
from .read_contracts import assess_runtime
from .start_counters import assess_start_counter
from .writes import context_of

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .platform_scope import DiscoveryLifecycle

_MODE_PATH: Final = re.compile(r"^(/heatingCircuits/hc\d+)/operationMode$")
_HEAT_SOURCE: Final = re.compile(r"^(/heatSources/hs\d+)/workingTime$")
NET_COLLECTIONS: Final = {
    "/heatSources/emon/totalConsumption": "total",
    "/heatSources/emon/chConsumption": "ch",
    "/heatSources/emon/dhwConsumption": "dhw",
    "/heatSources/emon/poolConsumption": "pool",
}
PRODUCED: Final = "outputProduced"
RESET_DROP_KWH: Final = 10.0
SUPPLY: Final = "/heatSources/actualSupplyTemperature"
SUPPLY_SETPOINT: Final = "/heatSources/currentSupplySetpoint"


def _key(path: str) -> str:
    return path.strip("/").replace("/", ":")


class _CalculatedSensor(
    CoordinatorEntity["BoschBuderusDataUpdateCoordinator"], RestoreSensor
):
    """A calculated value of one coordinator; ``_derive`` works it out."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        path: str,
        key: str,
        name: str,
        inputs: tuple[str, ...],
    ) -> None:
        super().__init__(coordinator)
        self.resource_path = path
        self.extra_resource_paths = inputs
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{_key(path)}:{key}"
        self._attr_name = grouped_entity_name(coordinator, path, name)
        self._value: Any = None
        self._attributes: dict[str, object] | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.resource_path)

    @property
    def available(self) -> bool:
        return self._value is not None

    @property
    def native_value(self) -> Any:
        return self._value

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        return self._attributes

    def _derive(self) -> tuple[Any, dict[str, object] | None]:
        raise NotImplementedError

    def _refresh(self) -> None:
        self._value, self._attributes = self._derive()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh()
        super()._handle_coordinator_update()

    async def async_update(self) -> None:
        """Calculated from readings the integration reads anyway."""


class CircuitActivitySensor(_CalculatedSensor):
    """What the heating circuit does: heating, cooling, defrosting, idle, off."""

    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, root: str
    ) -> None:
        super().__init__(
            coordinator,
            root,
            "calculated_activity",
            localize(
                coordinator.hass.config.language,
                "Aktivität (berechnet)",
                "Activity (calculated)",
            ),
            circuit_input_paths(coordinator, root),
        )
        self._root = root
        self._attr_options = list(ACTIVITIES)

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self._root + "/operationMode")

    @property
    def available(self) -> bool:
        """While the circuit's mode is current; the activity may be unknown."""
        return read(self.coordinator, self._root + "/operationMode") is not None

    def _derive(self) -> tuple[Any, dict[str, object] | None]:
        result = circuit_activity(self.coordinator, self._root)
        if result.value is None:
            return None, None
        return result.value, {"derived_from": result.derived_from}


class RunTimePerStartSensor(_CalculatedSensor):
    """Heating operating hours of a heat source per heating start."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, source: str
    ) -> None:
        self._hours = source + "/workingTime"
        self._starts = source + "/numberOfStarts"
        super().__init__(
            coordinator,
            source,
            "calculated_run_time_per_start_heating",
            localize(
                coordinator.hass.config.language,
                "Mittlere Laufzeit je Start Heizen (berechnet)",
                "Average run time per start, heating (calculated)",
            ),
            (self._hours, self._starts),
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self._hours)

    def _derive(self) -> tuple[Any, dict[str, object] | None]:
        trace = Trace()
        hours = runtime_hours(trace.note(read(self.coordinator, self._hours)), "ch")
        count = starts(trace.note(read(self.coordinator, self._starts)), "ch")
        if hours is None or count is None or count <= 0:
            return None, None
        return round(hours * 60 / count, 1), {
            "operating_hours": round(hours, 2),
            "starts": int(count),
            "derived_from": trace.attribute(),
        }


class SupplyOvershootSensor(_CalculatedSensor):
    """Supply temperature minus its setpoint while heating."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE_DELTA
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_suggested_display_precision = 1
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        supply: str,
        setpoint: str,
        root: str | None,
        preferred: tuple[str, ...] = (),
    ) -> None:
        self._supply = supply
        # Supply paths read first (the heat pump's own supply TC1).
        self._supplies = (*preferred, supply)
        self._setpoint = setpoint
        self._root = root
        inputs = (
            circuit_input_paths(coordinator, root)
            if root is not None
            else heat_source_input_paths(coordinator)
        )
        super().__init__(
            coordinator,
            root or "/heatSources",
            "calculated_supply_overshoot",
            localize(
                coordinator.hass.config.language,
                "Vorlauf über Sollwert (berechnet)",
                "Supply above setpoint (calculated)",
            ),
            tuple(dict.fromkeys((*self._supplies, setpoint, *inputs))),
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self._supply)

    def _heating(self, trace: Trace) -> bool:
        if self._root is not None:
            result = circuit_activity(self.coordinator, self._root)
            trace.used.update(result.derived_from)
            return result.value == HEATING
        defrost = defrost_state(self.coordinator, trace)
        if defrost is not None and defrost.active:
            return False
        return (
            heat_source_running(self.coordinator, trace) is True
            and heat_side(self.coordinator, trace) == CIRCUIT
        )

    def _derive(self) -> tuple[Any, dict[str, object] | None]:
        trace = Trace()
        found = first(self.coordinator, self._supplies, number, trace)
        supply = found[1] if found is not None else None
        setpoint = number(trace.note(read(self.coordinator, self._setpoint)))
        if supply is None or setpoint is None or not self._heating(trace):
            return None, None
        return round(supply - setpoint, 1), {
            "supply_temperature": supply,
            "setpoint": setpoint,
            "derived_from": trace.attribute(),
        }


class NetHeatCounterSensor(_CalculatedSensor):
    """The produced heat without the defrost dips, never decreasing."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, path: str
    ) -> None:
        super().__init__(
            coordinator,
            path,
            "calculated_net_heat",
            _net_name(coordinator.hass.config.language, NET_COLLECTIONS[path]),
            (path,),
        )
        self._store = derived_store(coordinator.hass)
        self._total: float | None = None
        self._reference: float | None = None
        self._origin: str | None = None
        self._ready = False

    @property
    def available(self) -> bool:
        return self._total is not None

    @property
    def native_value(self) -> float | None:
        return None if self._total is None else round(self._total, 3)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        if self._total is None:
            return None
        attributes: dict[str, object] = {}
        if self._reference is not None:
            attributes["highest_reading"] = round(self._reference, 3)
        if self._origin is not None:
            attributes["derived_from"] = {self.resource_path: self._origin}
        return attributes

    def _row_key(self) -> str:
        return str(self._attr_unique_id)

    async def async_added_to_hass(self) -> None:
        await self._store.async_load()
        row = self._store.get(self._row_key())
        total = stored_number(row, "total")
        reference = stored_number(row, "reference")
        restored = await self.async_get_last_sensor_data()
        if (
            restored is not None
            and restored.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
        ):
            value = _restored(restored.native_value)
            if value is not None and (total is None or value > total):
                # The restored state is newer than the saved total: the
                # reading it was counted up to is as much newer, so that
                # part is not counted again.
                if total is not None and reference is not None:
                    reference += value - total
                total = value
        self._total, self._reference = total, reference
        self._ready = True
        await super().async_added_to_hass()

    def _derive(self) -> tuple[Any, dict[str, object] | None]:
        if not self._ready:
            return None, None
        reading = read(self.coordinator, self.resource_path)
        value = energy_part(reading, PRODUCED)
        if value is None or reading is None:
            return None, None
        self._origin = reading.origin
        self._count(value)
        return None, None

    def _count(self, value: float) -> None:
        total, reference = self._total, self._reference
        if total is None or reference is None:
            # The first reading: the counter starts at the device's value,
            # or continues a restored total from this reading on.
            total = value if total is None else total
            reference = value
        elif value > reference:
            total += value - reference
            reference = value
        elif value < reference - RESET_DROP_KWH:
            reference = value  # a replaced or reset counter
        if (total, reference) != (self._total, self._reference):
            self._total, self._reference = total, reference
            self._store.set(
                self._row_key(),
                {"total": round(total, 4), "reference": round(reference, 4)},
            )

    def _refresh(self) -> None:
        self._derive()


def _net_name(language: str, mode: str) -> str:
    if mode == "ch":
        return localize(
            language, "Netto-Wärme Heizen (berechnet)", "Net heat heating (calculated)"
        )
    if mode == "dhw":
        return localize(
            language,
            "Netto-Wärme Warmwasser (berechnet)",
            "Net heat hot water (calculated)",
        )
    if mode == "pool":
        return localize(
            language, "Netto-Wärme Pool (berechnet)", "Net heat pool (calculated)"
        )
    return localize(
        language, "Netto-Wärme gesamt (berechnet)", "Net heat total (calculated)"
    )


def _restored(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    number_value = float(value)
    return number_value if isfinite(number_value) and number_value >= 0 else None


type _Factory = Callable[[], Entity]


def _candidates(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> list[tuple[str, _Factory]]:
    """Each calculated sensor whose inputs exist, by a stable key."""
    found: list[tuple[str, _Factory]] = []
    resources = getattr(coordinator, "resources", None) or {}
    data = getattr(coordinator, "data", None) or {}
    if not data:
        return found
    rrc_plant = context_of(coordinator).rrc_plant
    for path in sorted(data):
        match = _MODE_PATH.fullmatch(path)
        if match is None or rrc_plant:
            continue
        root = match[1]
        found.append(
            (f"activity:{root}", partial(CircuitActivitySensor, coordinator, root))
        )
        supply = root + "/actualSupplyTemperature"
        number_text = root.removeprefix("/heatingCircuits/hc")
        setpoint = f"/signals/SC.HC{number_text}.FlowTempSetp"
        if supply in resources and read(coordinator, setpoint) is not None:
            found.append(
                (
                    f"overshoot:{root}",
                    partial(SupplyOvershootSensor, coordinator, supply, setpoint, root),
                )
            )
    for path, resource in sorted(resources.items()):
        if (
            path in NET_COLLECTIONS
            and resource_energy_part(resource, PRODUCED) is not None
        ):
            found.append(
                (f"net:{path}", partial(NetHeatCounterSensor, coordinator, path))
            )
        match = _HEAT_SOURCE.fullmatch(path)
        counter = resources.get(match[1] + "/numberOfStarts") if match else None
        if (
            match is not None
            and counter is not None
            and assess_runtime(resource).reading("ch").value is not None
            and assess_start_counter(counter).part_reported("ch")
        ):
            found.append(
                (
                    f"runtime:{match[1]}",
                    partial(RunTimePerStartSensor, coordinator, match[1]),
                )
            )
    if SUPPLY in resources and (
        SUPPLY_SETPOINT in resources or read(coordinator, SUPPLY_SETPOINT) is not None
    ):
        found.append(
            (
                "overshoot:/heatSources",
                partial(
                    SupplyOvershootSensor,
                    coordinator,
                    SUPPLY,
                    SUPPLY_SETPOINT,
                    None,
                    HEAT_PUMP_SUPPLY,
                ),
            )
        )
    return found


def track_calculated_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add each calculated sensor once its inputs exist, and the defrost
    sensors once the plant shows that it defrosts."""
    seen: set[str] = set()

    @callback
    def discover() -> None:
        additions = []
        for key, create in _candidates(coordinator):
            if key not in seen:
                seen.add(key)
                additions.append(create())
        if additions:
            async_add_entities(additions)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))
    derived_defrost.track_defrost_entities(
        entry, coordinator, async_add_entities, derived_defrost.sensors
    )
