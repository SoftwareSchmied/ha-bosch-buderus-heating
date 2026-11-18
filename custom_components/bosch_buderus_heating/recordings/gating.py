"""Which plants the series without a local path are read for.

A series of ``series.CLOUD_ONLY`` is asked for only when the plant shows
what it belongs to, so that no other plant collects refusals for it:

- the water-softener series for a plant with a softener circuit
  (``/swCircuits/sw1/...``);
- the boiler series (energy of older firmware and boilers) for a plant the
  recordings are read for at all (``anchor``) but without the energy
  monitoring (``/heatSources/emon``) a heat pump has.

A path that does not answer there (404, 403, an unknown unit) yields no
entity. See ``docs/recordings.md``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Final

from .anchor import _ANCHOR, _known
from .series import (
    CLOUD_ONLY,
    REQUIRES_BOILER,
    REQUIRES_SOFTENER,
    Series,
)

if TYPE_CHECKING:
    from ..coordinator import BoschBuderusDataUpdateCoordinator

SOFTENER_CIRCUIT: Final = "/swCircuits/sw1/"
_HEAT_SOURCE: Final = "/heatSources"
_EMON: Final = "/heatSources/emon"


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


def allowed(series: Series, paths: Iterable[str]) -> bool:
    """Whether a plant with these resources is asked for the series (a
    boiler needs a reading a recording belongs to, as for any read)."""
    if series.requires is None:
        return True
    found = tuple(paths)
    if series.requires == REQUIRES_SOFTENER:
        return has_softener_circuit(found)
    if series.requires == REQUIRES_BOILER:
        return has_boiler_shape(found) and any(_ANCHOR.fullmatch(p) for p in found)
    return False


def allowed_keys(
    coordinator: BoschBuderusDataUpdateCoordinator | None,
) -> frozenset[str]:
    """The keys of the series without a local path this plant is asked for."""
    if coordinator is None:
        return frozenset()
    paths = tuple(_known(coordinator))
    return frozenset(item.key for item in CLOUD_ONLY if allowed(item, paths))


def softener_plant(coordinator: BoschBuderusDataUpdateCoordinator | None) -> bool:
    return coordinator is not None and has_softener_circuit(_known(coordinator))
