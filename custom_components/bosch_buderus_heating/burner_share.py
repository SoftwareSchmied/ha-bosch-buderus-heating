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
hybrid system; the system type ``eHybrid`` is a heat pump with an
additional heater and does not count). A boiler type of the first heat
source with the outdoor status ``NOT_CONNECTED`` of the hybrid control is
never a hybrid system,
whatever else is present. The plant model adds two pieces of evidence: a hybrid
manager at the hybrid place of the component list under its fixed rule, and
a heat source that reports a heat pump next to one that reports a boiler.
They only add to the evidence above; without a result of the plant model
(before its stored row is read) everything is as before. A plant with a
heat pump only is not affected.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from .derived_inputs import energy_part, local_only_paths, read
from .local_only import (
    hybrid_detected_from,
    hybrid_path_counts,
    hybrid_path_needs_value,
)

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator

BURNER_SHARE: Final = "burner_share"
BURNER_PART: Final = "burner"


def burner_reported(values: Mapping[str, float]) -> bool:
    """The collection reports burner energy above zero."""
    burner = values.get(BURNER_PART)
    return burner is not None and burner > 0


def hybrid_detected(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    """A hybrid manager or a hybrid system type is reported.

    Within one update pass worked out once: every performance factor asks,
    and neither the cloud nor the local readings change during the pass.
    """
    from .local_selection import LocalSelection

    selection = getattr(coordinator, "local_selection", None)
    if isinstance(selection, LocalSelection):
        return selection._cached(
            ("hybrid_detected", id(coordinator)),
            lambda: _hybrid_detected(coordinator),
        )
    return _hybrid_detected(coordinator)


def _hybrid_detected(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    # Only the paths that can show a hybrid system are looked at, and only
    # the types need their value (the rest of the paths count by their name).
    readings: list[tuple[str, object]] = []
    data = getattr(coordinator, "data", None) or {}
    for path, resource in (getattr(coordinator, "resources", None) or {}).items():
        if not hybrid_path_counts(path):
            continue
        if not hybrid_path_needs_value(path):
            readings.append((path, None))
            continue
        current = data[path].resource if path in data else resource
        readings.append((path, current.value if current.has_value else None))
    for path in local_only_paths(coordinator):
        if not hybrid_path_counts(path):
            continue
        if not hybrid_path_needs_value(path):
            readings.append((path, None))
            continue
        reading = read(coordinator, path)
        readings.append((path, reading.value if reading is not None else None))
    return hybrid_detected_from(readings, _plant_evidence(coordinator))


def _plant_evidence(coordinator: object) -> tuple[str, ...]:
    """The evidence of the plant model's result; none without a result."""
    from .plant_inputs import current_plant

    try:
        plant = current_plant(coordinator)
    except Exception:  # the plant model never changes the rule by failing
        return ()
    return plant.evidence if plant is not None else ()


def burner_share(
    coordinator: BoschBuderusDataUpdateCoordinator, collection: str
) -> bool:
    """Whether a factor or value of the collection has a burner share."""
    burner = energy_part(read(coordinator, collection), BURNER_PART)
    if burner is not None and burner > 0:
        return True
    return hybrid_detected(coordinator)
