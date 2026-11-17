"""What a heating circuit is doing right now, worked out from its readings.

The same rule gives the ``hvac_action`` of the climate entities and the
calculated activity sensor of each heating circuit. Only the five standard
actions are used:

* ``off``: the circuit's mode is off, or its heating/cooling status is off
  (summer);
* ``defrosting``: the heat pump defrosts (``defrost_state``);
* ``idle``: the heat source does not run, runs for hot water or the pool,
  or the circuit pump is off;
* ``heating`` / ``cooling``: the heat source runs for the circuit side and
  the circuit pump runs; cooling when a cooling signal or the cooling season
  says so.

The local reading of an input comes first, then the cloud reading (see
``derived_inputs``). A missing input that the rule needs gives no activity
at all, never a guessed one. Verified on a real heat pump with one heating
circuit; boilers, iCom gateways and room controllers follow the same rule
untested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .derived_inputs import (
    Input,
    Trace,
    first,
    flag,
    known_paths,
    number,
    read,
    state,
)
from .heat_source_kind import (
    AIR,
    DEFROST_FLAG,
    DEFROST_SIGNAL,
    classify,
    note_defrost_evidence,
)

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator

HEATING: Final = "heating"
COOLING: Final = "cooling"
DEFROSTING: Final = "defrosting"
IDLE: Final = "idle"
OFF: Final = "off"
ACTIVITIES: Final = (HEATING, COOLING, DEFROSTING, IDLE, OFF)

# Defrost sources in their order: the hybrid manager's defrost signal, the
# refrigerant circuit status of each heat pump (local), the compressor status
# (cloud) and each heat source's defrost flag.
REFRIGERANT_STATUS: Final = re.compile(r"/heatSources/hs\d+/refrigerant/status")
COMPRESSOR_STATUS: Final = "/heatSources/compressor/status"
_DEFROST_STATES: Final = frozenset({"defrost", "passive_defrost"})

# Whether the heat source runs: the compressor (its signals, the refrigerant
# circuit status, the compressor status) or the heat source's own status.
COMPRESSOR_SIGNALS: Final = (
    "/signals/SRC.CUHP.HMI.CompressorActive",
    "/signals/SRC.CUHP.HP1.CompressorStatus",
)
HEAT_SOURCE_ACTIVE: Final = "/heatSources/flameStatus"
_RUNNING_STATES: Final = frozenset(
    {"startup", "heating", "cooling", "dhw", "pool", "pool_heat", *_DEFROST_STATES}
)
_STOPPED_STATES: Final = frozenset({"idle", "off", "shutoff"})

# Which side the heat goes to: the 3-way valve VW1 (off = heating side), its
# relay, the refrigerant circuit and compressor status, and the central
# heating status of a boiler.
DHW_VALVE: Final = "/signals/SRC.CUHP.HP1.DHWValve"
DHW_VALVE_RELAY: Final = "/signals/SRC.RelayStatus.3WayValve"
CENTRAL_HEATING_STATUS: Final = "/heatSources/chStatus"
COOLING_SIGNAL: Final = "/signals/SRC.CUHP.Cooling.Active"
CIRCUIT: Final = "circuit"
HOT_WATER: Final = "dhw"
POOL: Final = "pool"
OTHER: Final = "other"
_STATE_SIDES: Final = {
    "heating": CIRCUIT,
    "cooling": CIRCUIT,
    "dhw": HOT_WATER,
    "pool": POOL,
    "pool_heat": POOL,
}
_CIRCUIT_NUMBER = re.compile(r"/heatingCircuits/hc(\d+)")


@dataclass(frozen=True, slots=True)
class Derivation[T]:
    """A calculated value, ``None`` without its inputs, and its inputs."""

    value: T | None
    derived_from: dict[str, str]


@dataclass(frozen=True, slots=True)
class DefrostReading:
    """Whether the plant defrosts, from which source, and when it was read."""

    active: bool
    source: Input


def defrost_paths(coordinator: BoschBuderusDataUpdateCoordinator) -> tuple[str, ...]:
    """Every defrost source of this plant in its order."""
    return (
        DEFROST_SIGNAL,
        *known_paths(coordinator, REFRIGERANT_STATUS),
        COMPRESSOR_STATUS,
        *known_paths(coordinator, DEFROST_FLAG),
    )


def defrost_capable(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    """Whether this plant defrosts at all, from evidence only.

    An air heat pump (``heat_source_kind``) does. A brine or water unit
    shares the controller platform and its signal names (4-way valve, drip
    pan heater) with the air units and never does. Without any evidence of
    the kind, a status that actually reports a defrost proves it; the
    general status lists declare a defrost state on every unit.
    """
    kind = classify(coordinator).kind
    if kind is not None:
        return kind == AIR
    for path in (*known_paths(coordinator, REFRIGERANT_STATUS), COMPRESSOR_STATUS):
        if _defrost_of(read(coordinator, path)) is True:
            note_defrost_evidence(coordinator)
            return True
    return False


def _defrost_of(reading: Input | None) -> bool | None:
    if reading is None:
        return None
    if reading.path == DEFROST_SIGNAL or DEFROST_FLAG.fullmatch(reading.path):
        return flag(reading)
    key = state(reading)
    if key is None:
        return None
    if reading.path == COMPRESSOR_STATUS:
        return key == "defrost"
    return key in _DEFROST_STATES


def defrost_state(
    coordinator: BoschBuderusDataUpdateCoordinator, trace: Trace | None = None
) -> DefrostReading | None:
    """The first defrost source with a current reading.

    ``None`` for a plant without evidence that it defrosts at all
    (``defrost_capable``). With several heat pumps a source counts as
    defrosting when any of them does; it counts as not defrosting only when
    every one has a reading.
    """
    if not defrost_capable(coordinator):
        return None
    groups: list[tuple[str, ...]] = [
        (DEFROST_SIGNAL,),
        known_paths(coordinator, REFRIGERANT_STATUS),
        (COMPRESSOR_STATUS,),
        known_paths(coordinator, DEFROST_FLAG),
    ]
    for group in groups:
        states = [
            (_defrost_of(item), item)
            for item in (read(coordinator, path) for path in group)
        ]
        active = [item for result, item in states if result is True and item]
        if active:
            note_defrost_evidence(coordinator)
            if trace is not None:
                trace.note(active[0])
            return DefrostReading(True, active[0])
        known = [item for result, item in states if result is False and item]
        if states and len(known) == len(states):
            if trace is not None:
                for item in known:
                    trace.note(item)
            return DefrostReading(False, known[0])
    return None


def _running_of(reading: Input | None) -> bool | None:
    if reading is None:
        return None
    if reading.path in COMPRESSOR_SIGNALS:
        return flag(reading)
    key = state(reading)
    if reading.path == HEAT_SOURCE_ACTIVE:
        if key in ("on", "start"):
            return True
        return False if key in ("off", "stop") else None
    if key in _RUNNING_STATES:
        return True
    return False if key in _STOPPED_STATES else None


def compressor_running(
    coordinator: BoschBuderusDataUpdateCoordinator, trace: Trace | None = None
) -> bool | None:
    """Whether the compressor runs; ``None`` without a reading."""
    paths = (
        *COMPRESSOR_SIGNALS,
        *known_paths(coordinator, REFRIGERANT_STATUS),
        COMPRESSOR_STATUS,
    )
    found = first(coordinator, paths, _running_of, trace)
    return found[1] if found is not None else None


def heat_source_running(
    coordinator: BoschBuderusDataUpdateCoordinator, trace: Trace | None = None
) -> bool | None:
    """Whether the compressor or the burner runs: running when either
    runs (a hybrid's boiler heats while the compressor stands), stopped
    only when every source with a reading stands; ``None`` without one."""
    compressor = compressor_running(coordinator, trace)
    if compressor is True:
        return True
    burner = first(coordinator, (HEAT_SOURCE_ACTIVE,), _running_of, trace)
    if burner is not None and burner[1]:
        return True
    if compressor is False or burner is not None:
        return False
    return None


def _side_of(reading: Input | None) -> str | None:
    if reading is None:
        return None
    if reading.path in (DHW_VALVE, DHW_VALVE_RELAY):
        on = flag(reading)
        return None if on is None else HOT_WATER if on else CIRCUIT
    key = state(reading)
    if reading.path == CENTRAL_HEATING_STATUS:
        # Expected, untested: on while the boiler heats the circuits.
        if key == "on":
            return CIRCUIT
        return OTHER if key == "off" else None
    return _STATE_SIDES.get(key or "")


def heat_side(
    coordinator: BoschBuderusDataUpdateCoordinator, trace: Trace | None = None
) -> str | None:
    """Where the running heat source delivers to."""
    paths = (
        DHW_VALVE,
        DHW_VALVE_RELAY,
        *known_paths(coordinator, REFRIGERANT_STATUS),
        COMPRESSOR_STATUS,
        CENTRAL_HEATING_STATUS,
    )
    found = first(coordinator, paths, _side_of, trace)
    if found is None:
        return None
    side = found[1]
    # The valve and the refrigerant circuit's status arrive one after the
    # other: at the end of a hot water charge the valve is already back on
    # the heating side while the status still says hot water or has stopped.
    # Contradicting readings give no heat to the circuit.
    from_valve = found[0].path in (DHW_VALVE, DHW_VALVE_RELAY)
    if (
        side == CIRCUIT
        and from_valve
        and _status_contradicts_circuit(coordinator, trace)
    ):
        return OTHER
    return side


def _status_contradicts_circuit(
    coordinator: BoschBuderusDataUpdateCoordinator, trace: Trace | None
) -> bool:
    for path in (*known_paths(coordinator, REFRIGERANT_STATUS), COMPRESSOR_STATUS):
        reading = read(coordinator, path)
        key = state(reading)
        if key is None:
            continue
        if key in _STOPPED_STATES or _STATE_SIDES.get(key, CIRCUIT) != CIRCUIT:
            if trace is not None:
                trace.note(reading)
            return True
    return False


def _cooling(
    coordinator: BoschBuderusDataUpdateCoordinator, season: str | None, trace: Trace
) -> bool:
    if season == "cooling":
        return True
    if flag(trace.note(read(coordinator, COOLING_SIGNAL))) is True:
        return True
    paths = (*known_paths(coordinator, REFRIGERANT_STATUS), COMPRESSOR_STATUS)
    for path in paths:
        reading = read(coordinator, path)
        if state(reading) == "cooling":
            trace.note(reading)
            return True
    return False


def circuit_pump_paths(root: str) -> tuple[str, ...]:
    """The circuit pump's readings in their order."""
    match = _CIRCUIT_NUMBER.fullmatch(root)
    relay = (
        (f"/signals/SRC.CUHP.CH.HC{match.group(1)}PumpRelay",)
        if match is not None
        else ()
    )
    return (root + "/pumpStatus", *relay, root + "/pumpModulation")


def circuit_has_pump(coordinator: BoschBuderusDataUpdateCoordinator, root: str) -> bool:
    """Whether the plant offers a pump value of the circuit at all (cloud or
    local): without one the activity cannot be told while the heat source
    runs for heating."""
    pattern = re.compile("|".join(re.escape(path) for path in circuit_pump_paths(root)))
    return bool(known_paths(coordinator, pattern))


_CIRCUIT_MODE = re.compile(r"/heatingCircuits/(hc\d+)/operationMode")


def activity_inputs_missing(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> dict[str, list[str]]:
    """Per heating circuit the inputs of its activity the plant does not
    offer (today only ``pump``); circuits with all inputs are left out. For
    the diagnostics: it tells why a circuit has no activity sensor."""
    missing: dict[str, list[str]] = {}
    for path in known_paths(coordinator, _CIRCUIT_MODE):
        circuit = _CIRCUIT_MODE.fullmatch(path)
        if circuit is None:
            continue
        root = path.removesuffix("/operationMode")
        if not circuit_has_pump(coordinator, root):
            missing[circuit[1]] = ["pump"]
    return missing


def _pump_of(reading: Input | None) -> bool | None:
    if reading is None:
        return None
    if reading.path.endswith("/pumpModulation"):
        value = number(reading)
        return None if value is None or value < 0 else value > 0
    return flag(reading)


def circuit_pump(
    coordinator: BoschBuderusDataUpdateCoordinator,
    root: str,
    trace: Trace | None = None,
) -> bool | None:
    found = first(coordinator, circuit_pump_paths(root), _pump_of, trace)
    return found[1] if found is not None else None


def heat_source_input_paths(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> tuple[str, ...]:
    """Every reading whether and where the heat source runs may combine."""
    return tuple(
        dict.fromkeys(
            (
                *defrost_paths(coordinator),
                *COMPRESSOR_SIGNALS,
                HEAT_SOURCE_ACTIVE,
                DHW_VALVE,
                DHW_VALVE_RELAY,
                CENTRAL_HEATING_STATUS,
                COOLING_SIGNAL,
            )
        )
    )


def circuit_input_paths(
    coordinator: BoschBuderusDataUpdateCoordinator, root: str
) -> tuple[str, ...]:
    """Every reading the activity of a circuit may combine (entity use)."""
    return tuple(
        dict.fromkeys(
            (
                root + "/operationMode",
                root + "/currentSuWiMode",
                *heat_source_input_paths(coordinator),
                *circuit_pump_paths(root),
            )
        )
    )


def circuit_activity(
    coordinator: BoschBuderusDataUpdateCoordinator, root: str
) -> Derivation[str]:
    """The activity of one heating circuit (``root``: ``/heatingCircuits/hc1``)."""
    trace = Trace()

    def result(value: str | None) -> Derivation[str]:
        return Derivation(value, trace.attribute() if value is not None else {})

    mode = state(trace.note(read(coordinator, root + "/operationMode")))
    if mode is None:
        return result(None)
    if mode == "off":
        return result(OFF)
    season = state(trace.note(read(coordinator, root + "/currentSuWiMode")))
    if season == "off":
        return result(OFF)
    defrost = defrost_state(coordinator, trace)
    if defrost is None and defrost_capable(coordinator):
        # A defrosting heat pump whose defrost is not readable now: no guess.
        return result(None)
    if defrost is not None and defrost.active:
        return result(DEFROSTING)
    running = heat_source_running(coordinator, trace)
    if running is None:
        return result(None)
    if not running:
        return result(IDLE)
    side = heat_side(coordinator, trace)
    if side is None:
        return result(None)
    if side != CIRCUIT:
        return result(IDLE)
    pump = circuit_pump(coordinator, root, trace)
    if pump is None:
        return result(None)
    if not pump:
        return result(IDLE)
    return result(COOLING if _cooling(coordinator, season, trace) else HEATING)


def plant_defrosting(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    """Whether the plant's heat pump defrosts right now (room controllers)."""
    reading = defrost_state(coordinator)
    return reading is not None and reading.active
