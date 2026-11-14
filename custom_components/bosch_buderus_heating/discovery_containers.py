"""Direct reads below circuit containers that cannot be listed.

Some controllers refuse the directory of a circuit (HTTP 403 or 406) while
its values stay readable one by one, or answer with an empty or shorter list
than a sibling circuit of the same kind. The references of that directory
are then missing, and with them the values the integration only learns from
the references (the name of the circuit, its current setpoint, its status,
its program). An empty directory without such a sibling is a valid answer
and leads to no direct reads.

For every circuit container of a known kind (heating circuits, hot-water
circuits, heat sources, solar circuits, ventilation zones and zones), this
module plans direct reads of the known children instead: the children a
readable sibling container lists, and the children named in the catalogs of
the integration. Nothing is planned for a container the controller does not
have (HTTP 404) or for a circuit it does not advertise. The planned paths are
ordinary discovery reads within its request budget; a 403 or 404 answer to
one of them is kept like any other.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import NamedTuple

from .local_api.catalog import CATALOG
from .pointt import Resource

# At most this many direct reads per discovery, apart from its overall budget.
MAX_DIRECT_CONTAINER_PROBES = 256

REFUSED_STATUSES = frozenset({403, 406})

_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("heatingCircuits", re.compile(r"hc\d+", re.IGNORECASE)),
    ("dhwCircuits", re.compile(r"dhw\d+", re.IGNORECASE)),
    ("heatSources", re.compile(r"hs\d+", re.IGNORECASE)),
    ("solarCircuits", re.compile(r"sc\d+", re.IGNORECASE)),
    ("ventilation", re.compile(r"zone\d+", re.IGNORECASE)),
    ("zones", re.compile(r"zone\d+", re.IGNORECASE)),
)

# Children seen on real controllers of each kind below the circuit, in
# addition to the catalog and optional children discovery always requests.
_KNOWN_CHILDREN: dict[str, tuple[str, ...]] = {
    "heatingCircuits": (
        "/name",
        "/currentRoomSetpoint",
        "/overallStatus",
        "/heatingType",
        "/currentSuWiMode",
        "/heatCoolMode",
        "/maxFlowTemp",
        "/activeSwitchProgram",
        "/switchProgramMode",
        "/suWiSwitchMode",
        "/switchPrograms",
        "/switchPrograms/A",
        "/switchPrograms/B",
        "/switchPrograms/nameA",
        "/switchPrograms/nameB",
        "/supplyTemperatureSetpoint",
        "/heatCurve",
        "/heatCurveMax",
        "/roomInfluence",
        "/roomTempOffset",
        "/pumpPressureSetpoint",
        "/boostShortcut",
        "/cooling",
        "/openWindowDetection",
    ),
    "dhwCircuits": (
        "/name",
        "/operationMode",
        "/actualTemp",
        "/currentSetpoint",
        "/currentTemperatureLevel",
        "/overallStatus",
        "/charge",
        "/chargeDuration",
        "/chargeRemainingTime",
        "/singleChargeSetpoint",
        "/reduceTempOnAlarm",
        "/circulationPump",
        "/tdMode",
        "/tdmaxDuration",
        "/tdrunningStatus",
        "/tdsetPoint",
        "/tdwarmKeepingTime",
        "/temperatureLevels",
        "/temperatureLevels/high",
        "/temperatureLevels/low",
        "/temperatureLevels/eco",
        "/temperatureLevels/off",
        "/ecoChargingDelta",
        "/ecoStop",
        "/highChargingDelta",
        "/highStop",
        "/lowChargingDelta",
        "/lowStop",
        "/switchPrograms",
        "/switchPrograms/B",
        "/sensor",
        "/recirculation",
    ),
    "heatSources": (
        "/type",
        "/heatPumpType",
        "/numberOfStarts",
        "/pumpVolumeFlow",
        "/supplyFlowCondenserTemp",
        "/workingTime",
        "/refrigerant",
        "/brineCircuit",
        "/emon",
        "/activefailure",
        "/failurelist",
        "/sensor/electricity",
        "/sensor/gas",
    ),
    "solarCircuits": (),
    "ventilation": (),
    "zones": (),
}


class ContainerProbe(NamedTuple):
    """One planned direct read."""

    path: str
    depth: int
    # A refused or empty container: read like a catalog path. A child only a
    # sibling lists: read like an optional probe.
    core: bool


@dataclass(slots=True)
class DirectProbeDiagnostic:
    """Why the children of one container were requested directly."""

    reason: str
    references: set[str] = field(default_factory=set)
    paths: list[str] = field(default_factory=list)


def family_of(path: str) -> tuple[str, str, str] | None:
    """Return root, circuit ID and the rest of a path below a known circuit.

    The rest is empty for the circuit itself and starts with ``/`` below it.
    """
    parts = path.split("/")
    if len(parts) < 3 or parts[0]:
        return None
    for root, identifier in _FAMILIES:
        if parts[1] == root and identifier.fullmatch(parts[2]):
            rest = "/".join(parts[3:])
            return root, parts[2], f"/{rest}" if rest else ""
    return None


def known_child_suffixes(
    extra: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    """All known children per kind: curated, catalog and the local API spec.

    ``extra`` adds the catalog and optional children discovery uses already.
    """
    table: dict[str, dict[str, None]] = {
        root: dict.fromkeys(children) for root, children in _KNOWN_CHILDREN.items()
    }
    for root, children in (extra or {}).items():
        table.setdefault(root, {}).update(dict.fromkeys(children))
    for definition in CATALOG:
        match = re.fullmatch(
            r"/([^/{}]+)/\{[^/{}]+\}((?:/[^/{}]+)+)", definition.template
        )
        if match is not None and match.group(1) in table:
            table[match.group(1)][match.group(2)] = None
    return {root: tuple(children) for root, children in table.items()}


def _shorter_reason(children: frozenset[str]) -> str:
    """Why a listed container is read directly: empty or shorter list."""
    return "missing_children" if children else "empty_listing"


class ContainerProbes:
    """Plan direct reads below circuit containers during one discovery."""

    def __init__(
        self,
        suffixes: Mapping[str, tuple[str, ...]],
        *,
        known: Callable[[str], bool],
        records: dict[str, DirectProbeDiagnostic],
        limit: int = MAX_DIRECT_CONTAINER_PROBES,
    ) -> None:
        self._suffixes = suffixes
        self._known = known
        self._records = records
        self._limit = limit
        self._planned = 0
        self.over_limit: set[str] = set()
        self._listings: dict[str, frozenset[str]] = {}
        self._incomplete: dict[str, str] = {}
        self._depths: dict[str, int] = {}
        self._kinds: dict[tuple[str, str], dict[str, None]] = {}

    def refused(
        self, container: str, status: int | None, depth: int
    ) -> list[ContainerProbe]:
        """A container answered HTTP 403 or 406: read its known children."""
        if status not in REFUSED_STATUSES or family_of(container) is None:
            return []
        if container in self._listings or container in self._incomplete:
            return []
        reason = f"http_{status}"
        self._register(container, depth)
        self._incomplete[container] = reason
        return self._fill(container, reason)

    def listed(
        self, container: str, resource: Resource, depth: int
    ) -> list[ContainerProbe]:
        """A container answered: compare its list with its siblings."""
        if family_of(container) is None or container in self._listings:
            return []
        prefix = f"{container}/"
        children = frozenset(
            reference.path[len(container) :]
            for reference in resource.references
            if reference.path.startswith(prefix)
        )
        if not children and resource.metadata.resource_type != "refEnum":
            return []  # a value, not a directory
        self._register(container, depth)
        self._listings[container] = children
        probes: list[ContainerProbe] = []
        # An empty directory is a valid answer (a ventilation zone of a heat
        # pump without ventilation); only a sibling of the same kind that
        # lists children makes it, like any shorter list, incomplete.
        siblings = self._listed_siblings(container)
        missing = sorted(
            {child for sibling in siblings for child in self._listings[sibling]}
            - children
        )
        if missing:
            probes += self._plan(
                container,
                _shorter_reason(children),
                (f"{container}{child}" for child in missing),
                references=siblings,
                core=False,
            )
        for sibling in self._siblings(container):
            if sibling in self._incomplete:
                probes += self._plan(
                    sibling,
                    self._incomplete[sibling],
                    (f"{sibling}{child}" for child in sorted(children)),
                    references=(container,),
                    core=True,
                )
            elif children and (
                missing_there := sorted(children - self._listings[sibling])
            ):
                probes += self._plan(
                    sibling,
                    _shorter_reason(self._listings[sibling]),
                    (f"{sibling}{child}" for child in missing_there),
                    references=(container,),
                    core=False,
                )
        return probes

    def _fill(self, container: str, reason: str) -> list[ContainerProbe]:
        """Known children of the kind, then those its siblings list."""
        family = family_of(container)
        assert family is not None
        root, identifier, rest = family
        circuit = f"/{root}/{identifier}"
        known = tuple(
            f"{circuit}{suffix}"
            for suffix in self._suffixes.get(root, ())
            if suffix.startswith(f"{rest}/")
        )
        siblings = self._listed_siblings(container)
        listed = sorted(
            {child for sibling in siblings for child in self._listings[sibling]}
        )
        return self._plan(
            container,
            reason,
            (*known, *(f"{container}{child}" for child in listed)),
            references=siblings,
            core=True,
        )

    def _plan(
        self,
        container: str,
        reason: str,
        paths: Iterable[str],
        *,
        references: Iterable[str],
        core: bool,
    ) -> list[ContainerProbe]:
        depth = self._depths[container]
        probes: list[ContainerProbe] = []
        seen: set[str] = set()
        for path in paths:
            if path in seen or self._known(path) or path in self.over_limit:
                continue
            seen.add(path)
            if self._planned >= self._limit:
                self.over_limit.add(path)
                continue
            self._planned += 1
            levels = path[len(container) :].count("/")
            probes.append(ContainerProbe(path, depth + levels, core))
        if probes:
            record = self._records.setdefault(
                container, DirectProbeDiagnostic(reason=reason)
            )
            record.references.update(references)
            record.paths.extend(probe.path for probe in probes)
        return probes

    def _register(self, container: str, depth: int) -> None:
        family = family_of(container)
        assert family is not None
        self._depths[container] = depth
        self._kinds.setdefault((family[0], family[2]), {})[container] = None

    def _siblings(self, container: str) -> tuple[str, ...]:
        family = family_of(container)
        assert family is not None
        return tuple(
            sibling
            for sibling in self._kinds.get((family[0], family[2]), {})
            if sibling != container
        )

    def _listed_siblings(self, container: str) -> tuple[str, ...]:
        return tuple(
            sibling
            for sibling in self._siblings(container)
            if self._listings.get(sibling)
        )
