"""The readings the calculated values combine: one current reading per path.

A path the cloud also offers is read as its entity shows it: the local
reading first while it is current, otherwise the cloud reading within its
validity limit. A path only the local access offers is read from the local
access alone. An expired, failed or missing reading is no input at all, so a
calculation never works with a value that is not current.

``Trace`` records which inputs a calculation used and where each came from;
the entities show it as the attribute ``derived_from``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import TYPE_CHECKING, Protocol, cast

from .coordinator import Freshness, SnapshotSource
from .energy_contracts import assess_energy, is_energy_collection
from .local_only import definition_for, flag_state, native_value, state_key
from .read_contracts import assess_runtime, is_runtime_path
from .start_counters import assess_start_counter, is_start_counter_path

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .local_only import LocalOnlyDefinition
    from .observations import Observation
    from .pointt import Resource

LOCAL = "local"
CLOUD = "cloud"
# PointT markers of a missing number, besides the declared error values.
_SENTINELS = frozenset({32767.0, -32768.0, 3276.7, -3276.8})


@dataclass(frozen=True, slots=True)
class Input:
    """One current reading: its value, where it came from and when."""

    path: str
    value: object
    origin: str
    observed_at: datetime | None
    # The cloud entity's resource, for counters with several parts.
    resource: Resource | None = None
    # The reported unit, for readings whose unit may vary (power).
    unit: str | None = None


@dataclass(slots=True)
class Trace:
    """The inputs one calculation used, by path, with their origin."""

    used: dict[str, str] = field(default_factory=dict)

    def note(self, reading: Input | None) -> Input | None:
        if reading is not None:
            self.used[reading.path] = reading.origin
        return reading

    def attribute(self) -> dict[str, str]:
        """``derived_from``: path -> ``local`` or ``cloud``, sorted."""
        return dict(sorted(self.used.items()))


class _LocalPlantView(Protocol):
    def local_plant_sensors(self) -> Mapping[str, LocalOnlyDefinition]: ...

    def local_only_reading(self, path: str) -> Observation | None: ...


def _plant_view(coordinator: object) -> _LocalPlantView | None:
    if getattr(coordinator, "read_only_view", False) is not True:
        return None
    return cast("_LocalPlantView", coordinator)


def _local_only_observation(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> Observation | None:
    if (plant := _plant_view(coordinator)) is not None:
        return plant.local_only_reading(path)
    selection = getattr(coordinator, "local_selection", None)
    return selection.local_only_reading(path) if selection is not None else None


def _data(coordinator: object) -> Mapping[str, object]:
    """The cloud snapshots; none for a coordinator without them yet."""
    data = getattr(coordinator, "data", None)
    return data if isinstance(data, Mapping) else {}


def local_only_paths(coordinator: BoschBuderusDataUpdateCoordinator) -> frozenset[str]:
    """The paths the local access currently reads beyond the cloud's."""
    if (plant := _plant_view(coordinator)) is not None:
        return frozenset(plant.local_plant_sensors())
    selection = getattr(coordinator, "local_selection", None)
    if selection is None:
        return frozenset()
    return frozenset(selection.local_only_observations())


def known_paths(
    coordinator: BoschBuderusDataUpdateCoordinator, pattern: re.Pattern[str]
) -> tuple[str, ...]:
    """Every known path of a pattern, cloud and local-only, sorted."""
    paths = {*_data(coordinator), *local_only_paths(coordinator)}
    return tuple(sorted(path for path in paths if pattern.fullmatch(path)))


def read(coordinator: BoschBuderusDataUpdateCoordinator, path: str) -> Input | None:
    """The current reading of a path, or ``None``."""
    if path in _data(coordinator):
        snapshot = coordinator.display_snapshot(path)
        if (
            snapshot is None
            or not snapshot.available
            or snapshot.freshness is not Freshness.FRESH
        ):
            return None
        resource = snapshot.resource
        origin = LOCAL if snapshot.source is SnapshotSource.LOCAL else CLOUD
        return Input(
            path,
            resource.value,
            origin,
            snapshot.last_success,
            resource,
            resource.metadata.unit,
        )
    observation = _local_only_observation(coordinator, path)
    definition = definition_for(path)
    if observation is None or definition is None:
        return None
    value = native_value(definition, observation)
    if value is None:
        return None
    return Input(
        path, value, LOCAL, observation.timing.received_at, unit=observation.unit
    )


def state(reading: Input | None) -> str | None:
    """A state as an option key (``DEFROST`` -> ``defrost``)."""
    return state_key(reading.value) if reading is not None else None


def flag(reading: Input | None) -> bool | None:
    """On/off of a flag reading; ``None`` for anything else."""
    if reading is None:
        return None
    result = flag_state(reading.value)
    return None if result is None else result == "on"


def number(reading: Input | None) -> float | None:
    """A finite number that is no marker or declared error value."""
    if reading is None:
        return None
    value = reading.value
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    if not isfinite(result) or result in _SENTINELS:
        return None
    resource = reading.resource
    if resource is not None and value in resource.metadata.numeric_error_values:
        return None
    return result


def energy_part(reading: Input | None, part: str) -> float | None:
    """One part of an energy collection in kWh, if valid."""
    resource = reading.resource if reading is not None else None
    return resource_energy_part(resource, part) if resource is not None else None


def resource_energy_part(resource: Resource, part: str) -> float | None:
    if not is_energy_collection(resource):
        return None
    for key, item in assess_energy(resource).readings:
        if key == part:
            return item.value
    return None


def runtime_hours(reading: Input | None, part: str) -> float | None:
    """One part of a working-time counter in hours, if valid."""
    resource = reading.resource if reading is not None else None
    if resource is None or not is_runtime_path(resource.path):
        return None
    return assess_runtime(resource).reading(part).value


def starts(reading: Input | None, part: str) -> float | None:
    """One part of a start counter, if valid."""
    resource = reading.resource if reading is not None else None
    if resource is None or not is_start_counter_path(resource.path):
        return None
    value = assess_start_counter(resource).reading(part).value
    return None if value is None else float(value)


def first[T](
    coordinator: BoschBuderusDataUpdateCoordinator,
    paths: Iterable[str],
    interpret: Callable[[Input | None], T | None],
    trace: Trace | None = None,
) -> tuple[Input, T] | None:
    """The first path in order whose current reading means something."""
    for path in paths:
        reading = read(coordinator, path)
        result = interpret(reading)
        if reading is not None and result is not None:
            if trace is not None:
                trace.note(reading)
            return reading, result
    return None
