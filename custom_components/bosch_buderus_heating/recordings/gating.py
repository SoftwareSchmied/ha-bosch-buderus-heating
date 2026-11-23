"""Which plants the series without a local path are read for.

A series of ``series.CLOUD_ONLY`` is asked for only when the plant shows
what it belongs to, so that no other plant collects refusals for it:

- the water-softener series for a plant with a softener circuit
  (``/swCircuits/sw1/...``);
- the boiler series (energy of older firmware and boilers) for a plant the
  recordings are read for at all (``anchor``) but without the energy
  monitoring (``/heatSources/emon``) a heat pump has, except a heat pump
  behind an iCom gateway;
- the series of a heat pump behind an iCom gateway (and, for the
  diagnostics only, the solar yield) for an iCom gateway whose plant kind
  (``plant_inputs.current_plant``) is ``heat_pump``; without a plant kind the
  boiler series are read as before.

The heat pump's own series of a hybrid system (``series.HEAT_SOURCE_SERIES``)
are read, locally or from the cloud, for a plant with a burner share
(``burner_share``) whose heat pump reports counters of its own under its heat
source (``performance.heat_pump_root``), and only for that heat source; and
for a hybrid system (plant kind ``hybrid``) without such counters for ``hs2``,
the heat source of a hybrid system's heat pump. Never for an iCom gateway.

A path that does not answer there (404, 403, an unknown unit) yields no
entity. See ``docs/recordings.md``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Final

from .anchor import _ANCHOR, _known
from .series import (
    CLOUD_ONLY,
    HEAT_SOURCE_IDS,
    HEAT_SOURCE_SERIES,
    REQUIRES_BOILER,
    REQUIRES_ICOM_HEAT_PUMP,
    REQUIRES_SOFTENER,
    Series,
)

if TYPE_CHECKING:
    from ..coordinator import BoschBuderusDataUpdateCoordinator

SOFTENER_CIRCUIT: Final = "/swCircuits/sw1/"
_HEAT_SOURCE: Final = "/heatSources"
_EMON: Final = "/heatSources/emon"
_HEAT_SOURCE_TOTAL: Final = re.compile(r"/heatSources/hs\d+/emon/totalConsumption")
# The heat source of a hybrid system's heat pump.
HYBRID_HEAT_SOURCE: Final = "hs2"


def has_softener_circuit(paths: Iterable[str]) -> bool:
    return any(path.startswith(SOFTENER_CIRCUIT) for path in paths)


def has_boiler_shape(paths: Iterable[str]) -> bool:
    """A heat source without the energy monitoring of a heat pump."""
    heat_source = False
    for path in paths:
        if path == _EMON or path.startswith(_EMON + "/"):
            return False
        if path == _HEAT_SOURCE or path.startswith(_HEAT_SOURCE + "/"):
            heat_source = True
    return heat_source


def allowed(
    series: Series, paths: Iterable[str], *, icom_heat_pump: bool = False
) -> bool:
    """Whether a plant with these resources is asked for the series (a
    boiler or a heat pump needs a reading a recording belongs to, as for any
    read). ``icom_heat_pump``: a heat pump behind an iCom gateway."""
    if series.requires is None:
        return True
    found = tuple(paths)
    if series.requires == REQUIRES_SOFTENER:
        return has_softener_circuit(found)
    anchored = any(_ANCHOR.fullmatch(p) for p in found)
    if series.requires == REQUIRES_BOILER:
        return not icom_heat_pump and has_boiler_shape(found) and anchored
    if series.requires == REQUIRES_ICOM_HEAT_PUMP:
        return icom_heat_pump and anchored
    return False


def _icom(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    from ..icom_profile import is_icom_device_type

    gateway = getattr(coordinator, "gateway", None)
    return is_icom_device_type(getattr(gateway, "device_type", None))


def icom_heat_pump(coordinator: BoschBuderusDataUpdateCoordinator | None) -> bool:
    """An iCom gateway whose plant kind is ``heat_pump``."""
    if coordinator is None:
        return False
    from ..plant_inputs import current_plant

    if not _icom(coordinator):
        return False
    plant = current_plant(coordinator)
    return plant is not None and plant.plant_kind == "heat_pump"


def allowed_keys(
    coordinator: BoschBuderusDataUpdateCoordinator | None,
) -> frozenset[str]:
    """The keys of the series without a local path this plant is asked for."""
    if coordinator is None:
        return frozenset()
    paths = tuple(_known(coordinator))
    pump = icom_heat_pump(coordinator)
    return frozenset(
        item.key for item in CLOUD_ONLY if allowed(item, paths, icom_heat_pump=pump)
    )


def heat_pump_source(
    coordinator: BoschBuderusDataUpdateCoordinator | None,
) -> str | None:
    """The heat source (``hs2``) of a hybrid system's heat pump whose own
    recordings are read: the plant shows a burner share, and the heat pump
    reports counters of its own (heat and electricity, no burner part) under
    that heat source; else ``hs2`` for a hybrid system without such counters
    (``heat_pump_source_basis``). ``None`` for any other plant."""
    if coordinator is None:
        return None
    source = _counted_source(coordinator)
    if source is not None:
        return source
    return HYBRID_HEAT_SOURCE if hybrid_plant(coordinator) else None


def heat_pump_source_basis(
    coordinator: BoschBuderusDataUpdateCoordinator | None,
) -> str | None:
    """Why ``heat_pump_source`` names a heat source: ``counters`` (its own
    counters with a burner share) or ``plant_kind`` (a hybrid system)."""
    if coordinator is None:
        return None
    if _counted_source(coordinator) is not None:
        return "counters"
    return "plant_kind" if hybrid_plant(coordinator) else None


def hybrid_plant(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    """A hybrid system (plant kind ``hybrid``) on another gateway than iCom."""
    from ..plant_inputs import current_plant

    if _icom(coordinator):
        return False
    plant = current_plant(coordinator)
    return plant is not None and plant.hybrid


def _counted_source(coordinator: BoschBuderusDataUpdateCoordinator) -> str | None:
    """The heat source whose own counters show a hybrid system's heat pump."""
    if not any(_HEAT_SOURCE_TOTAL.fullmatch(path) for path in _known(coordinator)):
        return None
    from ..burner_share import burner_share
    from ..performance import COLLECTIONS, heat_pump_root

    root = heat_pump_root(coordinator, current=False) or heat_pump_root(coordinator)
    if root is None:
        return None
    source = root.rpartition("/")[2]
    if source not in HEAT_SOURCE_IDS:
        return None
    if not any(burner_share(coordinator, path) for path in COLLECTIONS.values()):
        return None
    return source


def heat_pump_keys(
    coordinator: BoschBuderusDataUpdateCoordinator | None,
) -> frozenset[str]:
    """The keys of the heat pump's own series this plant is asked for."""
    source = heat_pump_source(coordinator)
    if source is None:
        return frozenset()
    return frozenset(
        item.key for item in HEAT_SOURCE_SERIES if item.heat_source == source
    )


def softener_plant(coordinator: BoschBuderusDataUpdateCoordinator | None) -> bool:
    return coordinator is not None and has_softener_circuit(_known(coordinator))
