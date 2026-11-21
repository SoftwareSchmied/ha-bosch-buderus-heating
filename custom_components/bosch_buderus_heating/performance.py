"""Live performance values of a heat pump, worked out from its readings.

* Current COP: the heat output divided by the electrical power of the
  compressor and the electric heater. The heater turns its electricity into
  heat one to one, so its power is added to the heat output as well (the
  heat output reading covers the refrigerant circuit). Only while the
  compressor runs; unknown while the heat pump defrosts, in the start-up
  phase of the compressor (``cop_start``), without a heat output (never a
  zero), while the heat pump cools, below ``MIN_POWER_KW``, above
  ``MAX_COP`` and when it stays below 1 for ten minutes (``cop_plausibility``).
  The period ratios keep ``MAX_RATIO``.
* Primary circuit spread: supply (TC1) minus return (TC0) of the heat pump,
  only while the primary pump runs. TC0 is the return of the heating
  circuit only while the circuit pump moves at least as much water as the
  primary pump; the attribute ``return_is_circuit_return`` says so.
* Brine spread: brine inlet minus brine outlet, while the brine pump runs.
* The counters since commissioning, per mode.

Every value lists its inputs with their origin (``derived_from``). The local
reading of a path comes first, then the cloud's (``derived_inputs``); a
missing input gives no value, never a guessed one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final

from .cop_plausibility import (
    COOLING_REASON,
    IMPLAUSIBLE_LOW,
    MAX_COP,
    LowCopWatch,
    cooling_operation,
)
from .cop_start import STARTING, StartPhase
from .derived import power_kilowatts
from .derived_activity import (
    compressor_running,
    defrost_paths,
    defrost_state,
    heat_side,
    heat_source_input_paths,
    heat_source_running,
)
from .derived_inputs import (
    Input,
    Trace,
    energy_part,
    first,
    flag,
    known_paths,
    number,
    read,
    resource_energy_part,
)
from .heat_source_kind import BRINE, classify

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .pointt import Resource

MODES: Final = ("total", "ch", "dhw")
COLLECTIONS: Final = {
    "total": "/heatSources/emon/totalConsumption",
    "ch": "/heatSources/emon/chConsumption",
    "dhw": "/heatSources/emon/dhwConsumption",
}
PRODUCED: Final = "outputProduced"
# A hybrid system's heat pump reports its own counters under its heat source
# (the gateway's collections count the boiler). Their modes carry this prefix
# in the history, so they never mix with the gateway's.
HP_PREFIX: Final = "hp:"
HP_MODES: Final = tuple(HP_PREFIX + mode for mode in MODES)
HEAT_PUMP_COUNTERS: Final = "heat_pump_counters"
_HEAT_SOURCE_TOTAL: Final = re.compile(r"/heatSources/hs\d+/emon/totalConsumption")
_HEAT_SOURCE_COLLECTIONS: Final = {
    "total": "totalConsumption",
    "ch": "chConsumption",
    "dhw": "dhwConsumption",
}

# The heat output of the refrigerant circuit (despite its name the thermal
# output; it matches the heat meter on a real installation).
HEAT_OUTPUT: Final = "/signals/SRC.CUHP.HP1.CompressorPower"
COMPRESSOR_POWER: Final = (
    "/heatSources/compressor/powerElecActual",
    "/heatSources/hs1/refrigerant/compressorElecPowerActual",
)
HEATER_POWER: Final = (
    "/heatSources/eHeater/powerElecActual",
    "/heatSources/eHeater/powerElecActualLowResolution",
)
# A COP needs at least this electrical power (start and stop of the
# compressor) and is not shown above this ratio (readings taken at
# different moments).
MIN_POWER_KW: Final = 0.1
MAX_RATIO: Final = 15.0
# A performance factor needs at least this much electricity in its period.
MIN_ELECTRICITY_KWH: Final = 0.1

SUPPLY: Final = ("/heatSources/additionalHeater/flowTemp", "/signals/SRC.IntFlowTemp")
RETURN: Final = ("/heatSources/returnTemperature", "/signals/SRC.CUHP.HP1.ReturnTemp")
PRIMARY_PUMP: Final = (
    "/signals/SRC.CUHP.HP1.CHPumpRelay",
    "/signals/SRC.RelayStatus.CHPump",
    "/heatSources/hs1/refrigerant/heatCarrierPumpSpeed",
    "/heatSources/hs1/pumpVolumeFlow",
    "/signals/SRC.SystemPumpVolumeFlow",
)
_RELAYS: Final = frozenset(PRIMARY_PUMP[:2])
PRIMARY_FLOW: Final = (
    "/heatSources/hs1/pumpVolumeFlow",
    "/signals/SRC.SystemPumpVolumeFlow",
)
CIRCUIT_FLOW: Final = ("/signals/SRC.HC1.FlowCtrl.PumpVolumeFlow",)

BRINE_OUTLET: Final = re.compile(
    r"(/heatSources/hs\d+)/brineCircuit/collectorInflowTemp"
)
# Into the collector = out of the heat pump (outlet), and the other way.
INLET: Final = "/brineCircuit/collectorOutflowTemp"
OUTLET: Final = "/brineCircuit/collectorInflowTemp"
BRINE_PUMP: Final = "/brineCircuit/pumpSpeed"

# Why a live value is unknown.
COMPRESSOR_OFF: Final = "compressor_off"
DEFROSTING: Final = "defrosting"
INPUTS_MISSING: Final = "inputs_missing"
NO_HEAT_OUTPUT: Final = "no_heat_output"
IMPLAUSIBLE: Final = "implausible"
PUMP_OFF: Final = "pump_off"
TOO_LITTLE_ELECTRICITY: Final = "too_little_electricity"


@dataclass(slots=True)
class LiveResult:
    """A live value (``None`` when unknown), why not, and its attributes."""

    value: float | None
    attributes: dict[str, object] = field(default_factory=dict)


def _kilowatts(reading: Input | None) -> float | None:
    if reading is None:
        return None
    error_values = (
        reading.resource.metadata.numeric_error_values
        if reading.resource is not None
        else ()
    )
    return power_kilowatts(reading.value, reading.unit, error_values)


def _known(
    coordinator: BoschBuderusDataUpdateCoordinator, paths: tuple[str, ...]
) -> bool:
    """Whether the plant offers one of the paths at all (cloud or local)."""
    pattern = re.compile("|".join(re.escape(path) for path in paths))
    return bool(known_paths(coordinator, pattern))


def cop_input_paths(coordinator: BoschBuderusDataUpdateCoordinator) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                HEAT_OUTPUT,
                *COMPRESSOR_POWER,
                *HEATER_POWER,
                *heat_source_input_paths(coordinator),
                *defrost_paths(coordinator),
            )
        )
    )


def cop_possible(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    """The plant offers a heat output and a compressor power reading."""
    return _known(coordinator, (HEAT_OUTPUT,)) and _known(coordinator, COMPRESSOR_POWER)


def _heater_kw(
    coordinator: BoschBuderusDataUpdateCoordinator, trace: Trace
) -> tuple[bool, float | None]:
    """``(offered, kW)``: a plant without a heater reading adds nothing; a
    heater reading that is offered but not current gives no COP."""
    found = first(coordinator, HEATER_POWER, _kilowatts, trace)
    if found is not None:
        return True, found[1]
    return _known(coordinator, HEATER_POWER), None


def live_cop(
    coordinator: BoschBuderusDataUpdateCoordinator,
    phase: StartPhase | None = None,
    low: LowCopWatch | None = None,
) -> LiveResult:
    """The current COP of the heat pump; ``phase`` holds it back while the
    compressor starts, ``low`` follows how long it stays implausibly low.

    Not a heating COP while the heat pump cools (``cooling``), and not shown
    when implausible (see ``cop_plausibility``)."""
    trace = Trace()
    output = _kilowatts(trace.note(read(coordinator, HEAT_OUTPUT)))
    compressor = first(coordinator, COMPRESSOR_POWER, _kilowatts, trace)
    heater_offered, heater = _heater_kw(coordinator, trace)
    attributes: dict[str, object] = {
        "heat_output_kw": output,
        "compressor_power_kw": compressor[1] if compressor else None,
        "heater_power_kw": heater,
    }
    if classify(coordinator).kind == BRINE:
        attributes["brine_pump_included"] = False

    def unknown(reason: str) -> LiveResult:
        if low is not None:
            low.reset()
        attributes["reason"] = reason
        attributes["derived_from"] = trace.attribute()
        return LiveResult(None, attributes)

    defrost = defrost_state(coordinator, trace)
    if defrost is not None and defrost.active:
        return unknown(DEFROSTING)
    running = compressor_running(coordinator, trace)
    starting = phase.observe(running) if phase is not None else False
    if running is False:
        return unknown(COMPRESSOR_OFF)
    if running is not None and cooling_operation(coordinator, trace):
        return unknown(COOLING_REASON)
    if (
        running is None
        or output is None
        or compressor is None
        or (heater_offered and heater is None)
    ):
        return unknown(INPUTS_MISSING)
    heater_kw = heater or 0.0
    electrical = compressor[1] + heater_kw
    attributes["electrical_power_kw"] = round(electrical, 3)
    side = heat_side(coordinator, trace)
    if side is not None:
        attributes["side"] = side
    if starting:
        return unknown(STARTING)
    if electrical < MIN_POWER_KW:
        return unknown(INPUTS_MISSING)
    if output + heater_kw <= 0:
        # The compressor still draws power but no heat flows (the end of a
        # run): no COP of zero.
        return unknown(NO_HEAT_OUTPUT)
    cop = (output + heater_kw) / electrical
    if cop > MAX_COP:
        return unknown(IMPLAUSIBLE)
    if low is not None and low.observe(cop):
        return unknown(IMPLAUSIBLE_LOW)
    attributes["derived_from"] = trace.attribute()
    return LiveResult(round(cop, 2), attributes)


# --------------------------------------------------------------- spreads
def _running(reading: Input | None) -> bool | None:
    """A pump relay, or a pump speed or flow above zero."""
    if reading is None:
        return None
    if reading.path in _RELAYS:
        return flag(reading)
    value = number(reading)
    return None if value is None or value < 0 else value > 0


def spread_input_paths() -> tuple[str, ...]:
    return tuple(dict.fromkeys((*SUPPLY, *RETURN, *PRIMARY_PUMP, *CIRCUIT_FLOW)))


def spread_possible(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    return (
        _known(coordinator, SUPPLY)
        and _known(coordinator, RETURN)
        and _known(coordinator, PRIMARY_PUMP)
    )


def primary_spread(coordinator: BoschBuderusDataUpdateCoordinator) -> LiveResult:
    """TC1 - TC0 while the primary pump runs."""
    trace = Trace()
    supply = first(coordinator, SUPPLY, number, trace)
    back = first(coordinator, RETURN, number, trace)
    pump = first(coordinator, PRIMARY_PUMP, _running, trace)
    attributes: dict[str, object] = {
        "supply_temperature": supply[1] if supply else None,
        "return_temperature": back[1] if back else None,
    }
    primary = first(coordinator, PRIMARY_FLOW, number, trace)
    circuit = first(coordinator, CIRCUIT_FLOW, number, trace)
    if primary is not None:
        attributes["primary_flow"] = primary[1]
    if circuit is not None:
        attributes["circuit_flow"] = circuit[1]
    # TC0 is the circuit's return only while the circuit pump (PC1) moves at
    # least as much water as the primary pump (PC0).
    attributes["return_is_circuit_return"] = (
        circuit[1] >= primary[1]
        if primary is not None and circuit is not None and primary[1] > 0
        else None
    )
    attributes["derived_from"] = trace.attribute()
    if pump is not None and pump[1] is False:
        attributes["reason"] = PUMP_OFF
        return LiveResult(None, attributes)
    if supply is None or back is None or pump is None:
        attributes["reason"] = INPUTS_MISSING
        return LiveResult(None, attributes)
    return LiveResult(round(supply[1] - back[1], 1), attributes)


def brine_roots(coordinator: BoschBuderusDataUpdateCoordinator) -> tuple[str, ...]:
    """Heat sources with both brine temperatures."""
    roots = []
    for path in known_paths(coordinator, BRINE_OUTLET):
        match = BRINE_OUTLET.fullmatch(path)
        if match is not None and _known(coordinator, (match[1] + INLET,)):
            roots.append(match[1])
    return tuple(roots)


def brine_input_paths(
    coordinator: BoschBuderusDataUpdateCoordinator, root: str
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                root + INLET,
                root + OUTLET,
                root + BRINE_PUMP,
                *heat_source_input_paths(coordinator),
            )
        )
    )


def brine_spread(
    coordinator: BoschBuderusDataUpdateCoordinator, root: str
) -> LiveResult:
    """Brine inlet minus outlet while the brine pump (else the compressor)
    runs."""
    trace = Trace()
    inlet = number(trace.note(read(coordinator, root + INLET)))
    outlet = number(trace.note(read(coordinator, root + OUTLET)))
    pump = _running(trace.note(read(coordinator, root + BRINE_PUMP)))
    running = pump if pump is not None else heat_source_running(coordinator, trace)
    attributes: dict[str, object] = {
        "inlet_temperature": inlet,
        "outlet_temperature": outlet,
        "derived_from": trace.attribute(),
    }
    if running is False:
        attributes["reason"] = PUMP_OFF
        return LiveResult(None, attributes)
    if inlet is None or outlet is None or running is None:
        attributes["reason"] = INPUTS_MISSING
        return LiveResult(None, attributes)
    return LiveResult(round(inlet - outlet, 1), attributes)


# ------------------------------------------------------------- counters
@dataclass(frozen=True, slots=True)
class CounterReading:
    """Heat and electricity since commissioning of one mode, in kWh."""

    heat: float
    electricity: float
    origin: str
    # When the collection was read; not part of the comparison.
    observed_at: datetime | None = field(default=None, compare=False)


def is_heat_pump_mode(mode: str) -> bool:
    return mode.startswith(HP_PREFIX)


def heat_pump_path(root: str, mode: str) -> str:
    """The heat pump's own collection of a mode (``mode`` without prefix)."""
    return f"{root}/emon/{_HEAT_SOURCE_COLLECTIONS[mode]}"


def _is_heat_pump_counter(reading: Input | None) -> bool:
    """Heat and electricity are reported and no burner part: not a boiler."""
    if reading is None or _counters(reading) is None:
        return False
    burner = energy_part(reading, "burner")
    return burner is None or burner <= 0


def heat_pump_root(
    coordinator: BoschBuderusDataUpdateCoordinator, *, current: bool = True
) -> str | None:
    """The heat source (``/heatSources/hs2``) whose own total counters report
    heat and electricity of a heat pump, else ``None``.

    ``current`` asks for a current reading; without it the plant offering the
    counters is enough (to find the entities of their statistics)."""
    for path in known_paths(coordinator, _HEAT_SOURCE_TOTAL):
        root = path.removesuffix("/emon/totalConsumption")
        if current:
            if _is_heat_pump_counter(read(coordinator, path)):
                return root
            continue
        resources = getattr(coordinator, "resources", None) or {}
        resource = resources.get(path)
        if resource is not None and _resource_has_counters(resource):
            return root
    return None


def heat_pump_mode_path(
    coordinator: BoschBuderusDataUpdateCoordinator, mode: str
) -> str | None:
    """The collection the heat pump's counters of a mode are read from."""
    root = heat_pump_root(coordinator)
    return heat_pump_path(root, mode) if root is not None else None


def _counters(reading: Input | None) -> tuple[float, float] | None:
    heat = energy_part(reading, PRODUCED)
    electricity = energy_part(reading, "electricity")
    if electricity is None:
        compressor = energy_part(reading, "compressor")
        heater = energy_part(reading, "eheater")
        if compressor is not None and heater is not None:
            electricity = compressor + heater
    if reading is None or heat is None or electricity is None:
        return None
    return heat, electricity


def heat_pump_lifetime(
    coordinator: BoschBuderusDataUpdateCoordinator, mode: str
) -> tuple[CounterReading, str] | None:
    """The heat pump's own counters of a mode and their path."""
    path = heat_pump_mode_path(coordinator, mode)
    if path is None:
        return None
    reading = read(coordinator, path)
    if not _is_heat_pump_counter(reading) or reading is None:
        return None
    counters = _counters(reading)
    if counters is None:
        return None
    return CounterReading(*counters, reading.origin, reading.observed_at), path


def heat_pump_environmental(
    coordinator: BoschBuderusDataUpdateCoordinator, mode: str
) -> tuple[float | None, str] | None:
    """Environmental energy of a hybrid system's heat pump: its own heat minus
    its electricity (``None`` for a negative difference, a measuring
    artifact), with the collection it comes from; ``None`` without counters."""
    found = heat_pump_lifetime(coordinator, mode)
    if found is None:
        return None
    reading, path = found
    difference = reading.heat - reading.electricity
    if difference < -1e-9:
        return None, path
    return max(0.0, difference), path


def lifetime(
    coordinator: BoschBuderusDataUpdateCoordinator, mode: str
) -> CounterReading | None:
    """The produced heat and the electricity (compressor plus heater) of a
    mode since commissioning, from the device's counters."""
    if is_heat_pump_mode(mode):
        found = heat_pump_lifetime(coordinator, mode.removeprefix(HP_PREFIX))
        return found[0] if found is not None else None
    reading = read(coordinator, COLLECTIONS[mode])
    heat = energy_part(reading, PRODUCED)
    electricity = energy_part(reading, "electricity")
    if electricity is None:
        compressor = energy_part(reading, "compressor")
        heater = energy_part(reading, "eheater")
        if compressor is not None and heater is not None:
            electricity = compressor + heater
    if reading is None or heat is None or electricity is None:
        return None
    return CounterReading(heat, electricity, reading.origin, reading.observed_at)


def _resource_has_counters(resource: Resource) -> bool:
    """Heat and electricity of a collection, and no burner part."""
    burner = resource_energy_part(resource, "burner")
    if burner is not None and burner > 0:
        return False
    return _resource_counters(resource)


def heat_pump_counter_possible(
    coordinator: BoschBuderusDataUpdateCoordinator, mode: str
) -> bool:
    """The plant offers the heat pump's own counters of a mode."""
    root = heat_pump_root(coordinator, current=False)
    if root is None:
        return False
    resources = getattr(coordinator, "resources", None) or {}
    resource = resources.get(heat_pump_path(root, mode))
    return resource is not None and _resource_has_counters(resource)


def counter_possible(coordinator: BoschBuderusDataUpdateCoordinator, mode: str) -> bool:
    """The mode's counter collection offers heat and electricity."""
    resources = getattr(coordinator, "resources", None) or {}
    resource = resources.get(COLLECTIONS[mode])
    if resource is None:
        return False
    return _resource_counters(resource)


def _resource_counters(resource: Resource) -> bool:
    if resource_energy_part(resource, PRODUCED) is None:
        return False
    return resource_energy_part(resource, "electricity") is not None or (
        resource_energy_part(resource, "compressor") is not None
        and resource_energy_part(resource, "eheater") is not None
    )


def ratio(heat: float | None, electricity: float | None) -> float | None:
    """A performance factor: heat over electricity, ``None`` below
    ``MIN_ELECTRICITY_KWH`` or above ``MAX_RATIO``."""
    if heat is None or electricity is None or electricity < MIN_ELECTRICITY_KWH:
        return None
    result = heat / electricity
    if result < 0 or result > MAX_RATIO:
        return None
    return round(result, 2)


def unknown_reason(electricity: float) -> str:
    """Why a period's performance factor has no value: too little electricity
    in the period so far (for instance just after midnight) or a ratio that
    is not plausible."""
    if electricity < MIN_ELECTRICITY_KWH:
        return TOO_LITTLE_ELECTRICITY
    return IMPLAUSIBLE
