"""Targeted reads: the areas the reported components and values show.

The discovery reads what the gateway lists and a curated set of optional
paths below the containers it finds. A plant can have an area the gateway
does not list, or whose container it refuses: the heat pump of a hybrid
system, a pool, a ventilation unit, a solar circuit, a hot water circuit or
the heat pumps of a cascade. The component list (``/system/info``) and a
few reported values show such an area. This module decides which paths of
it are read in addition, by fixed rules:

- R1 hybrid: the first heat source reports a boiler type and the outdoor
  status of the hybrid control is ``CONNECTED``, or a component at the
  hybrid place (83) has the role of a hybrid manager, or the system type is
  ``eHybrid``. Not when the outdoor status is ``NOT_CONNECTED``. Reads
  the starts, the defrost state and the energy counters of ``hs2`` and two
  values of the heat sources.
- R2 pool: a component at the pool place (17) while ``/pool`` is not read.
- R3 ventilation: a component at the ventilation place (81) while
  ``/ventilation`` is not read or lists no ``zone1``.
- R4 solar: a component at the solar place (48) while ``/solarCircuits`` is
  not read or lists no ``sc1``.
- R5 hot water: a plant with a heat source or a heating circuit listed whose
  hot water list was asked for but names no circuit (an empty, refused or
  missing list): the operation
  mode of ``dhw1`` and ``dhw2``. A circuit whose operation mode answers
  is then read like a circuit whose directory is refused (its known
  children).
- R6 cascade: the system type ``heatpump_cascade`` with N refrigerant
  circuits (2 to 6): for each heat source ``hs1`` to ``hsN`` the gateway
  does not list, its refrigerant status, compressor speed, starts and
  working time. The number of circuits is read once after the discovery;
  the rule uses the value read last (or the one the inventory keeps), so on
  a new plant it applies from its next discovery on.

A place or a role counts only with a basis other than a name (component
table). The places of remote controls (56 to 59), mixing modules and every
other component lead to no read. A path is read only when the discovery
has neither read nor planned it, at most once per discovery, never one of
the paths the integration never reads, and at most ``MAX_MODULE_PROBES``
paths per discovery. They are read as optional paths after every other
path of the discovery, so they never take the place of another read.
Nothing is read on iCom gateways, room controllers and water softeners,
and nothing in the search for missing parts.

The results are kept for the diagnostics only; a stored result never
decides whether a path is read.

Pure logic without Home Assistant imports.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from .component_rows import ComponentRow, parse_cloud_rows
from .plant_detection import normalize
from .pointt import Resource
from .system_components import BEHAVIOUR_BASES, ComponentTable

MAX_MODULE_PROBES: Final = 48
# The read policy of a planned path beyond the limit (discovery diagnostics).
LIMIT_POLICY: Final = "module_probe_limit"
SYSTEM_INFO_PATH: Final = "/system/info"
SYSTEM_TYPE_PATH: Final = "/system/type"
FIRST_HEAT_SOURCE_TYPE_PATH: Final = "/heatSources/hs1/type"
OUTDOOR_STATUS_PATH: Final = "/heatSources/hybrid/outdoorStatus"
CIRCUITS_PATH: Final = "/heatSources/numberOfRefrigerantCircuitsInstalled"
VALUE_PATHS: Final = frozenset(
    {SYSTEM_TYPE_PATH, FIRST_HEAT_SOURCE_TYPE_PATH, OUTDOOR_STATUS_PATH, CIRCUITS_PATH}
)
# The containers whose list (or refusal) the rules and the diagnostics use.
CONTAINERS: Final = (
    "/heatingCircuits",
    "/dhwCircuits",
    "/heatSources",
    "/solarCircuits",
    "/ventilation",
    "/pool",
)
_CIRCUIT_ID: Final = re.compile(r"[a-z]{1,8}[0-9]{1,3}", re.IGNORECASE)
_BOILERS: Final = frozenset({"gasboiler", "oilboiler", "unknownboiler"})
MAX_CASCADE: Final = 6

HYBRID_PATHS: Final = (
    "/heatSources/hs2/defrostActive",
    "/heatSources/hs2/numberOfStarts",
    "/heatSources/hs2/emon/totalConsumption",
    "/heatSources/systemPressureRange",
    "/heatSources/actualHeatDemand",
)
POOL_PATHS: Final = (
    "/pool/enabled",
    "/pool/currentTemp",
    "/pool/setpointTemp",
    "/pool/additionalHeater/poolMode",
)
VENTILATION_PATHS: Final = tuple(
    f"/ventilation/zone1/{leaf}"
    for leaf in (
        "operationMode",
        "exhaustFanLevel",
        "ventilationLevels",
        "switchPrograms/cp",
        "sensors/supplyTemp",
        "maxRelativeHumidity",
        "maxIndoorAirQuality",
        "filter/remainingTime",
        "filter/maxRunTime",
    )
)
SOLAR_PATHS: Final = tuple(
    f"/solarCircuits/sc1/{leaf}"
    for leaf in (
        "collectorTemperature",
        "pumpModulation",
        "maxCylinderTemperature",
        "dhwTankBottomTemperature",
        "maxTemperatureReached",
    )
)
HOT_WATER_PATHS: Final = (
    "/dhwCircuits/dhw1/operationMode",
    "/dhwCircuits/dhw2/operationMode",
)
CASCADE_LEAVES: Final = (
    "refrigerant/status",
    "refrigerant/compressorActualSpeed",
    "numberOfStarts",
    "workingTime",
)
# The place key of the component table each area rule needs.
_PLACE_RULES: Final = (
    ("R2", "pool", "/pool", POOL_PATHS, None),
    ("R3", "ventilation", "/ventilation", VENTILATION_PATHS, "zone1"),
    ("R4", "solar", "/solarCircuits", SOLAR_PATHS, "sc1"),
)
AREAS: Final[Mapping[str, str]] = {
    "R1": "hybrid",
    "R2": "pool",
    "R3": "ventilation",
    "R4": "solar",
    "R5": "hot_water",
    "R6": "cascade",
}
# The area each place of the area check stands for (diagnostics only).
_AREA_OF_PLACE: Final[Mapping[int, str]] = {
    17: "/pool",
    42: "/dhwCircuits/dhw1",
    48: "/solarCircuits/sc1",
    81: "/ventilation/zone1",
    83: "/heatSources/hs2",
}
_REMOTE_PLACES: Final = frozenset({56, 57, 58, 59})
RESULT_CLASSES: Final = ("ok", "404", "403", "5xx", "429", "skipped", "other")


def result_class(
    bulk_result: str,
    fallback_result: str | None,
    discovered: bool,
    read_policy: str,
) -> str:
    """The class of one planned path's outcome: ``ok``, ``404``, ``403``
    (403 and 406), ``5xx``, ``429``, ``skipped`` (not read) or
    ``other`` (428, a timeout, an unusable answer)."""
    if discovered:
        return "ok"
    if read_policy == "optional_5xx_assumed_missing":
        return "5xx"
    result = fallback_result or bulk_result
    if result in ("not_attempted", "pending") or read_policy in (
        LIMIT_POLICY,
        "never_read_private",
    ):
        return "skipped"
    if result == "http_404":
        return "404"
    if result in ("http_403", "http_406"):
        return "403"
    if result in ("http_429", "rate_limited"):
        return "429"
    if result.startswith("http_5") or result == "service_unavailable":
        return "5xx"
    return "other"


@dataclass(frozen=True, slots=True)
class Planned:
    """One targeted read: its path and the rule it comes from."""

    path: str
    rule: str


def _listed_ids(container: str, resource: Resource) -> frozenset[str]:
    prefix = f"{container}/"
    found: set[str] = set()
    for reference in resource.references:
        rest = (
            reference.path[len(prefix) :] if reference.path.startswith(prefix) else ""
        )
        if rest and "/" not in rest:
            found.add(rest)
    return frozenset(found)


def _count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        # ASCII digits only: a superscript digit passes isdigit() but not int().
        return int(text) if text.isascii() and text.isdecimal() else None
    return None


class ModuleProbes:
    """The targeted reads of one discovery (see the module description)."""

    def __init__(
        self,
        table: ComponentTable | None,
        *,
        never_read: Callable[[str], bool],
        limit: int = MAX_MODULE_PROBES,
    ) -> None:
        self._table = table
        self._never_read = never_read
        self._limit = limit
        self.inactive: str | None = None
        # The rules were looked at (every other path of the discovery read).
        self.evaluated = False
        # Why the targeted reads stopped early (an error of the cloud).
        self.stopped: str | None = None
        self._rows: tuple[ComponentRow, ...] = ()
        self._values: dict[str, object] = {}
        self._listed: dict[str, frozenset[str]] = {}
        self._failed: dict[str, int | None] = {}
        self._planned: dict[str, str] = {}
        self._over_limit: dict[str, str] = {}
        self._never: dict[str, str] = {}
        self._evidence: dict[str, tuple[str, ...]] = {}
        self._conflicts: set[str] = set()

    # ------------------------------------------------------------- inputs
    def deactivate(self, reason: str) -> None:
        """No targeted reads in this discovery (the first reason is kept)."""
        if self.inactive is None:
            self.inactive = reason

    def observe(self, path: str, resource: Resource | None, status: int | None) -> None:
        """One answered or failed path of the discovery."""
        if resource is None:
            if path in CONTAINERS and path not in self._listed:
                self._failed[path] = status
            return
        if path == SYSTEM_INFO_PATH and resource.values:
            self._rows = parse_cloud_rows(resource.values)
        elif path in VALUE_PATHS and resource.has_value:
            self._values[path] = resource.value
        if path in CONTAINERS:
            self._failed.pop(path, None)
            self._listed[path] = _listed_ids(path, resource)

    # -------------------------------------------------------------- rules
    def _places(self, key: str) -> list[ComponentRow]:
        """Rows at the place with this key, when the place's basis is no name."""
        table = self._table
        if table is None:
            return []
        found = []
        for row in self._rows:
            if row.tok is None:
                continue
            place = table.place(row.tok)
            if (
                place is not None
                and place.key == key
                and place.basis in BEHAVIOUR_BASES
            ):
                found.append(row)
        return found

    def _hybrid_managers(self) -> list[ComponentRow]:
        """Rows at the hybrid place whose type has the role of a hybrid
        manager there (roles count only with a basis other than a name)."""
        table = self._table
        if table is None:
            return []
        return [
            row
            for row in self._places("hybrid_manager")
            if row.type_id is not None
            and (role := table.role(row.type_id, row.tok)) is not None
            and role.role == "hybrid_manager"
        ]

    def _readable(self, container: str, child: str | None) -> bool:
        """The container answered (and lists ``child`` when one is given)."""
        listed = self._listed.get(container)
        if listed is None:
            return False
        return child is None or child in listed

    def _rules(self) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
        """The rules that fire now: rule, evidence and paths."""
        fired: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
        outdoor = normalize(self._values.get(OUTDOOR_STATUS_PATH))
        evidence: list[str] = []
        if (
            normalize(self._values.get(FIRST_HEAT_SOURCE_TYPE_PATH)) in _BOILERS
            and outdoor == "connected"
        ):
            evidence.append("outdoor_status_connected")
        evidence.extend(f"tok83_id{row.type_id}" for row in self._hybrid_managers()[:1])
        if normalize(self._values.get(SYSTEM_TYPE_PATH)) == "ehybrid":
            evidence.append("system_type_ehybrid")
        if evidence and outdoor == "notconnected":
            self._conflicts.add("outdoor_status_not_connected")
        elif evidence:
            fired.append(("R1", tuple(evidence), HYBRID_PATHS))
        for rule, key, container, paths, child in _PLACE_RULES:
            rows = self._places(key)
            if rows and not self._readable(container, child):
                place = rows[0].tok
                fired.append((rule, (f"tok{place}",), paths))
        heating = any(
            self._listed.get(container)
            for container in ("/heatSources", "/heatingCircuits")
        )
        # The hot water list was asked for: refused, missing or without a circuit.
        asked = "/dhwCircuits" in self._failed or "/dhwCircuits" in self._listed
        listed = self._listed.get("/dhwCircuits") or frozenset()
        if (
            heating
            and asked
            and not any(item.casefold().startswith("dhw") for item in listed)
        ):
            fired.append(("R5", ("no_hot_water_circuit_listed",), HOT_WATER_PATHS))
        circuits = _count(self._values.get(CIRCUITS_PATH))
        if (
            normalize(self._values.get(SYSTEM_TYPE_PATH)) == "heatpumpcascade"
            and circuits is not None
            and 2 <= circuits <= MAX_CASCADE
        ):
            sources = self._listed.get("/heatSources", frozenset())
            paths_6 = tuple(
                f"/heatSources/hs{index}/{leaf}"
                for index in range(1, circuits + 1)
                if f"hs{index}" not in sources
                for leaf in CASCADE_LEAVES
            )
            fired.append(("R6", (f"system_type_cascade:{circuits}",), paths_6))
        return fired

    def plan(self, known: Callable[[str], bool]) -> list[Planned]:
        """The new targeted reads: each path once, none the discovery knows
        (``known``), none of the paths never read, at most the limit."""
        if self.inactive is not None:
            return []
        self.evaluated = True
        new: list[Planned] = []
        for rule, evidence, paths in self._rules():
            self._evidence[rule] = evidence
            for path in paths:
                if (
                    path in self._planned
                    or path in self._over_limit
                    or path in self._never
                    or known(path)
                ):
                    continue
                if self._never_read(path):
                    self._never[path] = rule
                    continue
                if len(self._planned) >= self._limit:
                    self._over_limit[path] = rule
                    continue
                self._planned[path] = rule
                new.append(Planned(path, rule))
        return new

    def interrupt(self, category: str) -> None:
        """An error ended the targeted reads; what was found stays."""
        self.stopped = category

    @property
    def over_limit(self) -> Mapping[str, str]:
        return dict(self._over_limit)

    def circuit_answered(self, path: str) -> str | None:
        """The hot water circuit whose planned operation mode answered."""
        if self._planned.get(path) != "R5":
            return None
        return path.rsplit("/", 1)[0]

    # -------------------------------------------------------- diagnostics
    def results(
        self, outcome: Callable[[str], str], at: int
    ) -> tuple[dict[str, object], ...]:
        """Per planned path (and each one over the limit): rule, result
        class and time; at most the limit."""
        entries: list[dict[str, object]] = [
            {"path": path, "rule": rule, "result": outcome(path), "at": at}
            for path, rule in self._planned.items()
        ]
        room = max(0, self._limit - len(entries))
        entries.extend(
            {"path": path, "rule": rule, "result": "skipped", "at": at}
            for path, rule in list(self._over_limit.items())[:room]
        )
        return tuple(entries)

    def snapshot(self, outcome: Callable[[str], str]) -> dict[str, object]:
        """What the rules saw and read, without values."""
        rules: dict[str, object] = {}
        for rule in sorted(set(self._evidence) | set(self._planned.values())):
            paths = [path for path, item in self._planned.items() if item == rule]
            counts: dict[str, int] = {}
            for path in paths:
                kind = outcome(path)
                counts[kind] = counts.get(kind, 0) + 1
            rules[rule] = {
                "area": AREAS[rule],
                "evidence": list(self._evidence.get(rule, ())),
                "planned": len(paths),
                "found": counts.get("ok", 0),
                "results": dict(sorted(counts.items())),
                "over_limit": sum(
                    1 for item in self._over_limit.values() if item == rule
                ),
            }
        return {
            "active": self.inactive is None,
            "inactive_reason": self.inactive,
            "limit": self._limit,
            "planned": len(self._planned),
            "over_limit": len(self._over_limit),
            "never_read": len(self._never),
            "rules": rules,
            "conflicts": sorted(self._conflicts),
            "stopped": self.stopped,
            "results": [
                {"path": path, "rule": rule, "result": outcome(path)}
                for path, rule in self._planned.items()
            ],
            "restored": False,
        }

    def listing_refs(self) -> dict[str, object]:
        """Per container the circuits it lists, or how its read failed."""
        report: dict[str, object] = {}
        for container in CONTAINERS:
            name = container.lstrip("/")
            if container in self._listed:
                report[name] = sorted(
                    item
                    for item in self._listed[container]
                    if _CIRCUIT_ID.fullmatch(item)
                )
            elif container in self._failed:
                status = self._failed[container]
                report[name] = f"http_{status}" if status is not None else "failed"
        return report

    def area_check(self, found: Callable[[str], bool]) -> dict[str, object]:
        """Display only: per component at a place that stands for an area,
        whether that area was found; no conclusion is drawn from it."""
        table = self._table
        areas: list[dict[str, object]] = []
        for row in self._rows:
            area = _AREA_OF_PLACE.get(row.tok) if row.tok is not None else None
            if area is None:
                continue
            place = table.place(row.tok) if table is not None and row.tok else None
            areas.append(
                {
                    "token": row.tok,
                    "id": row.type_id,
                    "place": place.key if place is not None else None,
                    "area": area,
                    "found": found(area),
                }
            )
        listed = self._listed.get("/heatingCircuits")
        return {
            "components": len(self._rows),
            "areas": areas,
            "remote_places": sum(1 for row in self._rows if row.tok in _REMOTE_PLACES),
            "heating_circuits_listed": (
                sum(1 for item in listed if _CIRCUIT_ID.fullmatch(item))
                if listed is not None
                else None
            ),
        }


def keep_times(
    previous: Sequence[Mapping[str, object]],
    current: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """The current results, each with the time of an earlier equal result
    (same path, rule and result), so an unchanged run changes nothing."""
    earlier = {
        (item.get("path"), item.get("rule"), item.get("result")): item.get("at")
        for item in previous
    }
    kept: list[dict[str, object]] = []
    for item in current:
        entry = dict(item)
        at = earlier.get((entry.get("path"), entry.get("rule"), entry.get("result")))
        if at is not None:
            entry["at"] = at
        kept.append(entry)
    return kept
