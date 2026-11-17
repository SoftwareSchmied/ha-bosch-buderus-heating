"""The environmental energy of an energy collection.

It is the heat produced minus the electricity put in (compressor and
electric auxiliary heater), per collection: total, heating, hot water and
pool. The plant reports no such value. A collection without both
electricity parts, or with a negative difference, has no value; a negative
difference is a measuring artifact (the parts are read at different times),
so it is never shown and never taken for a counter reset. Cooling has no
environmental energy: its collection has no auxiliary heater and its
produced heat is not taken from the environment.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import fsum
from typing import Final

ENERGY_PREFIX: Final = "/heatSources/emon/"
# The collections that have an environmental energy entity.
COLLECTIONS: Final = frozenset(
    {
        "/heatSources/emon/totalConsumption",
        "/heatSources/emon/chConsumption",
        "/heatSources/emon/dhwConsumption",
        "/heatSources/emon/poolConsumption",
    }
)
# The total's entity predates the others and is enabled by default; the
# entities of the modes are created disabled.
ENABLED_BY_DEFAULT: Final = frozenset({"/heatSources/emon/totalConsumption"})
NO_ENVIRONMENTAL_ENERGY_PATHS: Final = frozenset(
    {"/heatSources/emon/coolingConsumption"}
)
BALANCE_EPSILON: Final = 1e-9


def has_environmental_energy(path: str) -> bool:
    return path in COLLECTIONS


def environmental_energy(values: Mapping[str, float]) -> float | None:
    """The difference of the reported parts, or None (incomplete, negative)."""
    produced = values.get("outputProduced")
    compressor = values.get("compressor")
    auxiliary = values.get("eheater")
    if produced is None or compressor is None or auxiliary is None:
        return None
    environmental = fsum((produced, -compressor, -auxiliary))
    if environmental < -BALANCE_EPSILON:
        return None
    return max(0.0, environmental)
