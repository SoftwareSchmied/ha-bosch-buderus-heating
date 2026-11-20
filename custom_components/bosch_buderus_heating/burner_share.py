"""Heat that does not come from the heat pump: the burner share.

A hybrid system (a heat pump with a boiler) reports heat of the boiler too.
The performance factors divide the produced heat by the electricity of the
heat pump, and the environmental energy is the produced heat minus that
electricity; neither is valid for a boiler's heat. It is not known which
collections of such a plant contain the boiler's heat and which do not, so
a value that could be wrong is not shown: it is unknown, with the attribute
`reason: burner_share`.

The share counts as present when the energy collection reports a burner
part above zero, or when a hybrid manager is detected (its signals, the
hybrid control resources, or a system or heat source type that names a
hybrid system). A plant with a heat pump only is not affected.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from .derived_inputs import energy_part, local_only_paths, read
from .local_only import hybrid_evidence

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator

BURNER_SHARE: Final = "burner_share"
BURNER_PART: Final = "burner"


def burner_reported(values: Mapping[str, float]) -> bool:
    """The collection reports burner energy above zero."""
    burner = values.get(BURNER_PART)
    return burner is not None and burner > 0


def hybrid_detected(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    """A hybrid manager or a hybrid system type is reported."""
    readings: list[tuple[str, object]] = []
    data = getattr(coordinator, "data", None) or {}
    for path, resource in (getattr(coordinator, "resources", None) or {}).items():
        current = data[path].resource if path in data else resource
        readings.append((path, current.value if current.has_value else None))
    for path in local_only_paths(coordinator):
        reading = read(coordinator, path)
        readings.append((path, reading.value if reading is not None else None))
    return bool(hybrid_evidence(readings))


def burner_share(
    coordinator: BoschBuderusDataUpdateCoordinator, collection: str
) -> bool:
    """Whether a factor or value of the collection has a burner share."""
    burner = energy_part(read(coordinator, collection), BURNER_PART)
    if burner is not None and burner > 0:
        return True
    return hybrid_detected(coordinator)
