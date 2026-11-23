"""The kind of a plant, worked out from what the plant reports.

``resolve`` combines the reported type values (``/system/type``, the heat
source types, the heat pump types, ``/system/bus``, the outdoor status of a
hybrid control), the component list and the hybrid evidence the
integration already uses into one result: the plant kind (``heat_pump``,
``hybrid``, ``boiler``, ``ventilation``, ``none``, or ``None`` when it is
not known), the fuel of a boiler, whether the heat pumps form a cascade, the
evidence and the conflicts as fixed codes, and the inputs of the rule for
a hybrid manager at the hybrid place.

Rules (in this order):

1. A bus that is still detecting gives no result.
2. The plant is a hybrid system when any evidence says so: a heat source
   type of a hybrid system (``Hybrid``, ``Hybrid_Gas``, ``Hybrid_Oil``), a
   heat pump type and a boiler type in the same plant, the outdoor status
   ``CONNECTED``, a hybrid manager at the hybrid place (see below), or the
   hybrid evidence the integration used before (signals of a hybrid
   manager, resources of the hybrid control, a type that names a hybrid
   system). Evidence of the last kind alone is reported as the conflict
   ``hybrid_name_rule_only``.
3. Otherwise the heat source types decide (heat pump or boiler), then the
   system type; if they contradict each other the kind stays unknown.
4. Heat sources that report no appliance give ``ventilation`` with a
   Modbus bus or a component at the ventilation place, else ``none``.

The system type ``eHybrid`` is a heat pump with an additional heater, not
a hybrid system: it is no hybrid evidence, and the heat source types decide.

A hybrid manager at the hybrid place counts only for the first row at
place 83, with the type 248 from version 06.01 on or with the type 16, and
only on K30 and K40 gateways from firmware 08.02.02 (K30) or 11.00.00 (K40)
on. A version is compared by the part before its first ``-``, split at
dots; a part that is not a number counts as 0; a missing version never
passes. Other gateways never use the rule. A boiler type with the outdoor
status ``NOT_CONNECTED`` overrules it.

Component names never decide anything: only roles with a basis of
``rule``, ``observed`` or ``documented`` count.

Pure logic without Home Assistant imports.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from .component_rows import ComponentRow
from .system_components import BEHAVIOUR_BASES, ComponentTable

# The version of these rules; a stored result of another version is worked
# out again from its stored inputs.
MODEL_VERSION: Final = 1

PLANT_KINDS: Final = ("heat_pump", "hybrid", "boiler", "ventilation", "none")
FUELS: Final = ("gas", "oil", "unknown")
EVIDENCE_CODES: Final = (
    "reported_system_type",
    "reported_heat_source_type",
    "reported_heat_pump_type",
    "reported_outdoor_status",
    "hybrid_manager_component",
    "heat_source_types_combined",
    "hybrid_resources",
    "hybrid_manager_signals",
    "stored",
)
CONFLICT_CODES: Final = (
    "system_type_vs_heat_source_type",
    "components_vs_outdoor_status",
    "heat_pump_component_on_boiler_plant",
    "hybrid_name_rule_only",
    "cloud_local_components_differ",
    "unknown_type_value",
    "component_rule_skipped",
)

# The known type values, compared by their letters and digits without case.
SYSTEM_TYPES: Final = frozenset(
    {
        "heatpumpsingle",
        "heatpumpcascade",
        "ehybrid",
        "hybman",
        "hybridboiler",
        "boiler",
        "unknown",
    }
)
HEAT_SOURCE_TYPES: Final = frozenset(
    {
        "heatpump",
        "gasboiler",
        "oilboiler",
        "unknownboiler",
        "noappliance",
        # The heat source types of a room controller (``/heatSources/type``):
        # Boiler, Hybrid, Hybrid_Gas, Hybrid_Oil and No_Applicance (the
        # devices write it so).
        "boiler",
        "hybrid",
        "hybridgas",
        "hybridoil",
        "noapplicance",
    }
)
HEAT_PUMP_TYPES: Final = frozenset({"airwater", "liquidwater", "exhaustairwater"})
BUSES: Final = frozenset({"ems10", "ems20", "can", "modbus", "nobus", "detecting"})
OUTDOOR_STATUSES: Final = frozenset({"connected", "notconnected"})
# The boiler types with their fuel (None: the type states none).
_BOILERS: Final[Mapping[str, str | None]] = {
    "gasboiler": "gas",
    "oilboiler": "oil",
    "unknownboiler": "unknown",
    "boiler": None,
}
BOILER_TYPES: Final = frozenset(_BOILERS)
# The types of a hybrid system with their fuel (None: the type states none).
_HYBRIDS: Final[Mapping[str, str | None]] = {
    "hybrid": None,
    "hybridgas": "gas",
    "hybridoil": "oil",
}
_NO_APPLIANCE: Final = frozenset({"noappliance", "noapplicance"})

# The hybrid manager rule.
HYBRID_PLACE: Final = 83
VENTILATION_PLACE: Final = 81
HEAT_GENERATOR_PLACE: Final = 8
# Hybrid manager types and the version each needs (None: any version).
HYBRID_MANAGER_TYPES: Final[Mapping[int, str | None]] = {248: "06.01", 16: None}
GATEWAY_FAMILIES: Final[Mapping[str, str]] = {
    "k30": "k30",
    "k30rf": "k30",
    "mx300": "k30",
    "k40": "k40",
    "k40rf": "k40",
    "mx400": "k40",
}
FIRMWARE_MINIMUM: Final[Mapping[str, str]] = {"k30": "08.02.02", "k40": "11.00.00"}
GATE_NAME: Final = "hybrid_manager_place"

# The hybrid evidence the integration used before, under the codes of the
# model: a type that names a hybrid system counts as reported.
_PREVIOUS_CODES: Final[Mapping[str, str]] = {
    "system_type": "reported_system_type",
    "heat_source_type": "reported_heat_source_type",
    "hybrid_resources": "hybrid_resources",
    "hybrid_manager_signals": "hybrid_manager_signals",
}
_NOT_ALNUM: Final = re.compile(r"[^a-z0-9]")


def normalize(value: object) -> str | None:
    """Letters and digits of a type value without case; ``None`` for no text."""
    if not isinstance(value, str):
        return None
    text = _NOT_ALNUM.sub("", value.casefold())
    return text or None


def version_parts(text: str) -> tuple[int, ...]:
    """The numbers of a version: the part before the first ``-``, split at
    dots; a part that is not a number counts as 0."""
    head = text.split("-", 1)[0]
    return tuple(
        int(part) if part.isascii() and part.isdigit() else 0
        for part in head.split(".")
    )


def version_at_least(version: str | None, minimum: str) -> bool:
    """Whether a version reaches a minimum; a missing version never does."""
    if not version:
        return False
    have, need = version_parts(version), version_parts(minimum)
    width = max(len(have), len(need))
    return have + (0,) * (width - len(have)) >= need + (0,) * (width - len(need))


@dataclass(frozen=True, slots=True)
class PlantInputs:
    """What the plant reports, as safe texts (see ``plant_inputs``)."""

    gateway_class: str | None = None
    gateway_firmware: str | None = None
    system_type: str | None = None
    # Per heat source (``hs1``, ...) its type; ``/heatSources/type`` apart.
    heat_source_types: tuple[tuple[str, str], ...] = ()
    heat_sources_type: str | None = None
    # Per heat source its heat pump type (```` for ``/heatSources``).
    heat_pump_types: tuple[tuple[str, str], ...] = ()
    bus: str | None = None
    outdoor_status: str | None = None
    components: tuple[ComponentRow, ...] = ()
    # The hybrid evidence codes the integration used before.
    hybrid_evidence: tuple[str, ...] = ()
    platform: str | None = None


@dataclass(frozen=True, slots=True)
class Gate:
    """The rule for a hybrid manager at the hybrid place, with its inputs."""

    result: str  # passed, failed or not_applicable
    reason: str | None
    gateway_class: str | None
    platform: str | None
    gateway_firmware: str | None
    firmware_minimum: str | None
    firmware_passed: bool | None
    hybrid_place_rows: int
    first_type: int | None
    first_version: str | None
    version_minimum: str | None
    version_passed: bool | None

    def as_dict(self) -> dict[str, object]:
        return {
            "result": self.result,
            "reason": self.reason,
            "gateway_class": self.gateway_class,
            "platform": self.platform,
            "gateway_firmware": self.gateway_firmware,
            "firmware_minimum": self.firmware_minimum,
            "firmware_passed": self.firmware_passed,
            "hybrid_place_rows": self.hybrid_place_rows,
            "first_type": self.first_type,
            "first_version": self.first_version,
            "version_minimum": self.version_minimum,
            "version_passed": self.version_passed,
        }


@dataclass(frozen=True, slots=True)
class PlantResult:
    """The plant kind with its evidence, conflicts and gate inputs."""

    plant_kind: str | None
    fuel: str | None = None
    cascade: bool | None = None
    evidence: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    gates: Mapping[str, Gate] = field(default_factory=dict)
    model_version: int = MODEL_VERSION
    # The bus is still detecting: keep what was known before.
    transient: bool = False

    @property
    def hybrid(self) -> bool:
        return self.plant_kind == "hybrid"


def platform_of(gateway_class: str | None, platform: str | None) -> str | None:
    """The gateway family of the hybrid manager rule: k30, k40 or other."""
    if platform in ("icom", "rrc"):
        return platform
    if gateway_class is None:
        return None
    return GATEWAY_FAMILIES.get(gateway_class.casefold(), "other")


def _role(table: ComponentTable | None, row: ComponentRow) -> str | None:
    """The role of a row, only from entries whose basis is not a name."""
    if table is None or row.type_id is None:
        return None
    role = table.role(row.type_id, row.tok)
    if role is None or role.basis not in BEHAVIOUR_BASES:
        return None
    return role.role


def hybrid_manager_gate(inputs: PlantInputs, table: ComponentTable | None) -> Gate:
    """The rule for a hybrid manager at the hybrid place (first row only)."""
    family = platform_of(inputs.gateway_class, inputs.platform)
    minimum = FIRMWARE_MINIMUM.get(family or "")
    rows = [row for row in inputs.components if row.tok == HYBRID_PLACE]
    first = rows[0] if rows else None
    firmware_passed = (
        version_at_least(inputs.gateway_firmware, minimum)
        if minimum is not None
        else None
    )
    version_minimum = (
        HYBRID_MANAGER_TYPES.get(first.type_id)
        if first is not None and first.type_id is not None
        else None
    )
    version_passed = (
        version_at_least(first.version, version_minimum)
        if first is not None and version_minimum is not None
        else None
    )

    def gate(result: str, reason: str | None) -> Gate:
        return Gate(
            result,
            reason,
            inputs.gateway_class,
            family,
            inputs.gateway_firmware,
            minimum,
            firmware_passed,
            len(rows),
            first.type_id if first is not None else None,
            first.version if first is not None else None,
            version_minimum,
            version_passed,
        )

    if first is None:
        return gate("not_applicable", "no_hybrid_place")
    if table is None:
        return gate("failed", "table_unavailable")
    if (
        first.type_id not in HYBRID_MANAGER_TYPES
        or _role(table, first) != "hybrid_manager"
    ):
        return gate("failed", "not_a_hybrid_manager")
    if minimum is None:
        return gate("failed", "platform")
    if not firmware_passed:
        return gate("failed", "firmware")
    if version_minimum is not None and not version_passed:
        return gate("failed", "version")
    return gate("passed", None)


def _known(value: str | None, vocabulary: frozenset[str]) -> tuple[str | None, bool]:
    """The normalized value if known, and whether an unknown value was seen."""
    normal = normalize(value)
    if normal is None:
        return None, False
    return (normal, False) if normal in vocabulary else (None, True)


def resolve(inputs: PlantInputs, table: ComponentTable | None) -> PlantResult:
    """The plant kind of one gateway (see the module description)."""
    unknown = False
    system_type, flag = _known(inputs.system_type, SYSTEM_TYPES)
    unknown |= flag
    heat_sources: list[str] = []
    for value in (
        *(value for _name, value in inputs.heat_source_types),
        inputs.heat_sources_type,
    ):
        known, flag = _known(value, HEAT_SOURCE_TYPES)
        unknown |= flag
        if known is not None:
            heat_sources.append(known)
    heat_pump_types: list[str] = []
    for _name, value in inputs.heat_pump_types:
        known, flag = _known(value, HEAT_PUMP_TYPES)
        unknown |= flag
        if known is not None:
            heat_pump_types.append(known)
    bus, flag = _known(inputs.bus, BUSES)
    unknown |= flag
    outdoor, flag = _known(inputs.outdoor_status, OUTDOOR_STATUSES)
    unknown |= flag

    conflicts: set[str] = {"unknown_type_value"} if unknown else set()
    gate = hybrid_manager_gate(inputs, table)
    gates = {GATE_NAME: gate}
    if bus == "detecting":
        return PlantResult(
            None, conflicts=tuple(sorted(conflicts)), gates=gates, transient=True
        )

    boilers = [item for item in heat_sources if item in _BOILERS]
    hybrids = [item for item in heat_sources if item in _HYBRIDS]
    fuels = [
        fuel
        for item in heat_sources
        if (fuel := _BOILERS.get(item) or _HYBRIDS.get(item)) is not None
    ]
    names_heat_pump = "heatpump" in heat_sources or bool(heat_pump_types)
    cascade = (
        None if system_type in (None, "unknown") else system_type == "heatpumpcascade"
    )
    fuel = (fuels[0] if len(set(fuels)) == 1 else "unknown") if fuels else None

    # 2. Hybrid evidence.
    rule_evidence: set[str] = set()
    if hybrids:
        rule_evidence.add("reported_heat_source_type")
    if names_heat_pump and boilers:
        rule_evidence.add("heat_source_types_combined")
    if outdoor == "connected":
        rule_evidence.add("reported_outdoor_status")
    if gate.result == "passed":
        if boilers and outdoor == "notconnected":
            conflicts.add("components_vs_outdoor_status")
        else:
            rule_evidence.add("hybrid_manager_component")
    elif gate.result == "failed":
        conflicts.add("component_rule_skipped")
    previous = {
        _PREVIOUS_CODES[code]
        for code in inputs.hybrid_evidence
        if code in _PREVIOUS_CODES
    }
    if rule_evidence or previous:
        if not rule_evidence:
            conflicts.add("hybrid_name_rule_only")
        return PlantResult(
            "hybrid",
            fuel or "unknown",
            cascade,
            tuple(sorted(rule_evidence | previous)),
            tuple(sorted(conflicts)),
            gates,
        )

    # 3. Heat source types, then the system type.
    by_system = (
        "heat_pump"
        if system_type in ("heatpumpsingle", "heatpumpcascade")
        else "boiler"
        if system_type == "boiler"
        else None
    )
    by_heat_source: str | None = None
    if names_heat_pump:
        by_heat_source = "heat_pump"
    elif boilers:
        by_heat_source = "boiler"
    elif heat_sources and all(item in _NO_APPLIANCE for item in heat_sources):
        by_heat_source = "no_appliance"
    evidence: set[str] = set()
    kind: str | None
    if (
        by_heat_source is not None
        and by_system is not None
        and (by_heat_source != by_system)
    ):
        conflicts.add("system_type_vs_heat_source_type")
        kind = None
    elif by_heat_source == "no_appliance":
        ventilation = bus == "modbus" or any(
            row.tok == VENTILATION_PLACE for row in inputs.components
        )
        kind = "ventilation" if ventilation else "none"
        evidence.add("reported_heat_source_type")
    else:
        kind = by_heat_source or by_system
        if kind == "heat_pump" and "heatpump" in heat_sources:
            evidence.add("reported_heat_source_type")
        if kind == "heat_pump" and heat_pump_types:
            evidence.add("reported_heat_pump_type")
        if kind == "boiler" and boilers:
            evidence.add("reported_heat_source_type")
        if kind is not None and by_system == kind:
            evidence.add("reported_system_type")
    if kind == "boiler" and any(
        row.tok == HEAT_GENERATOR_PLACE
        and _role(table, row) in ("heat_source", "outdoor_unit")
        for row in inputs.components
    ):
        conflicts.add("heat_pump_component_on_boiler_plant")
    return PlantResult(
        kind,
        (fuel or "unknown") if kind == "boiler" else None,
        cascade if kind is not None else None,
        tuple(sorted(evidence)),
        tuple(sorted(conflicts)),
        gates,
    )
