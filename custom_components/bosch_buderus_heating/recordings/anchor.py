"""Whether a plant has anything the recordings could belong to.

The plant's recordings are the energy counters, the starts and working time
of the heat source and two temperatures. A room controller that only shows a
heat source's modulation (``/heatSources/actualModulation``) has none of
them, and asking it for the recordings only collects refusals. The
recordings are read only for a plant with one of these readings, cloud or
local: an energy counter collection, the starts or working time of the heat
source, the refrigerant circuit or the compressor of a heat pump.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Final

from ..derived_inputs import local_only_paths

if TYPE_CHECKING:
    from ..coordinator import BoschBuderusDataUpdateCoordinator

_ANCHOR: Final = re.compile(
    r"/heatSources/(?:emon(?:/.*)?"
    r"|numberOfStarts(?:/.*)?"
    r"|workingTime(?:/.*)?"
    r"|compressor(?:/.*)?"
    r"|hs\d+/(?:refrigerant|numberOfStarts|workingTime)(?:/.*)?)"
)


def _known(coordinator: BoschBuderusDataUpdateCoordinator) -> Iterable[str]:
    for name in ("resources", "data"):
        value = getattr(coordinator, name, None)
        if isinstance(value, Mapping):
            yield from (key for key in value if isinstance(key, str))
    selection = getattr(coordinator, "local_selection", None)
    if selection is None or callable(
        getattr(selection, "local_only_observations", None)
    ):
        yield from local_only_paths(coordinator)


def has_recording_anchor(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    """Whether the plant offers a reading a recording belongs to."""
    return any(_ANCHOR.fullmatch(path) for path in _known(coordinator))
