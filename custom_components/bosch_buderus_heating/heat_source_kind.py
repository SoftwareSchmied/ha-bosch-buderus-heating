"""The kind of heat source of a heat pump: air, brine or water, from evidence.

The heat pump type the device reports (``/heatSources/hsN/heatPumpType``)
decides first. Without it only readings that exist on one kind count:

* air: a defrost signal or defrost flag, a defrost the plant reported, the
  air inlet temperature at the evaporator (TL2) or the outdoor unit's fan;
* brine: a reading of the brine circuit (collector temperatures, pump,
  pressure, flow).

Air and brine units share the controller platform and many signal names
(4-way valve, drip pan heater, crankcase heater, brine and collector flags),
so names never count, only current readings of these paths. Contradicting
evidence gives no kind. ``water`` (water-to-water) is known only from the
reported type; no reading proves it so far.
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from weakref import WeakKeyDictionary

from .derived_inputs import flag, known_paths, number, read, state

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator

AIR: Final = "air"
BRINE: Final = "brine"
WATER: Final = "water"
KINDS: Final = (AIR, BRINE, WATER)

HEAT_PUMP_TYPE: Final = re.compile(r"/heatSources/hs\d+/heatPumpType")
# By option key. The gateway reports ``air_water``, ``exhaustAir_water``
# (``exhaustair_water``) and ``liquid_water``; a liquid source is a brine
# or water circuit, shown as brine (neither defrosts). The other keys are
# the names the heat pump type sensor shows for them.
_REPORTED: Final = {
    "air_water": AIR,
    "exhaustair_water": AIR,
    "exhaust_air": AIR,
    "liquid_water": BRINE,
    "brine_water": BRINE,
    "water_water": WATER,
}
DEFROST_SIGNAL: Final = "/signals/HYBMAN.CAN.DefrostActive"
DEFROST_FLAG: Final = re.compile(r"/heatSources/hs\d+/defrostActive")
AIR_INLET: Final = "/heatSources/sensors/evaporatorTemp_tl2"
OUTDOOR_FAN: Final = re.compile(r"/heatSources/hs\d+/oduFanSpeed")
BRINE_CIRCUIT: Final = re.compile(r"/heatSources/hs\d+/brineCircuit/[A-Za-z]+")
# Plants that have reported a defrost, by coordinator; kept by the defrost
# monitor across restarts.
_DEFROSTED: WeakKeyDictionary[object, bool] = WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class SourceKind:
    """The kind (``None`` when unknown) and the paths that prove it."""

    kind: str | None
    evidence: tuple[str, ...]


def note_defrost_evidence(coordinator: object) -> None:
    with suppress(TypeError):
        _DEFROSTED[coordinator] = True


def defrost_evidence(coordinator: object) -> bool:
    try:
        return _DEFROSTED.get(coordinator, False)
    except TypeError:
        return False


def _air_evidence(coordinator: BoschBuderusDataUpdateCoordinator) -> list[str]:
    found: list[str] = []
    if defrost_evidence(coordinator):
        found.append("reported_defrost")
    if flag(read(coordinator, DEFROST_SIGNAL)) is not None:
        found.append(DEFROST_SIGNAL)
    found.extend(known_paths(coordinator, DEFROST_FLAG))
    if number(read(coordinator, AIR_INLET)) is not None:
        found.append(AIR_INLET)
    found.extend(
        path
        for path in known_paths(coordinator, OUTDOOR_FAN)
        if number(read(coordinator, path)) is not None
    )
    return found


def _brine_evidence(coordinator: BoschBuderusDataUpdateCoordinator) -> list[str]:
    return [
        path
        for path in known_paths(coordinator, BRINE_CIRCUIT)
        if number(read(coordinator, path)) is not None
    ]


def classify(coordinator: BoschBuderusDataUpdateCoordinator) -> SourceKind:
    """The heat source kind of this plant's heat pump, if evidenced."""
    reported = {
        kind: path
        for path in known_paths(coordinator, HEAT_PUMP_TYPE)
        if (kind := _REPORTED.get(state(read(coordinator, path)) or "")) is not None
    }
    if len(reported) == 1:
        kind, path = next(iter(reported.items()))
        return SourceKind(kind, (path,))
    if reported:
        return SourceKind(None, tuple(sorted(reported.values())))
    air = _air_evidence(coordinator)
    brine = _brine_evidence(coordinator)
    if air and not brine:
        return SourceKind(AIR, tuple(air))
    if brine and not air:
        return SourceKind(BRINE, tuple(brine))
    return SourceKind(None, tuple((*air, *brine)))


def evidence_paths(coordinator: BoschBuderusDataUpdateCoordinator) -> tuple[str, ...]:
    """Every reading ``classify`` may look at (entity use)."""
    return (
        *known_paths(coordinator, HEAT_PUMP_TYPE),
        DEFROST_SIGNAL,
        *known_paths(coordinator, DEFROST_FLAG),
        AIR_INLET,
        *known_paths(coordinator, OUTDOOR_FAN),
        *known_paths(coordinator, BRINE_CIRCUIT),
    )
