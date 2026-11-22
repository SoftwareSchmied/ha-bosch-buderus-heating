"""The plant kind: decision table, gates, version rule, names never decide."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.bosch_buderus_heating import system_components
from custom_components.bosch_buderus_heating.component_rows import (
    ComponentRow,
    parse_cloud_rows,
    parse_local_rows,
)
from custom_components.bosch_buderus_heating.local_only import (
    hybrid_evidence,
    hybrid_path_counts,
)
from custom_components.bosch_buderus_heating.plant_detection import (
    CONFLICT_CODES,
    EVIDENCE_CODES,
    GATE_NAME,
    PLANT_KINDS,
    PlantInputs,
    resolve,
    version_at_least,
    version_parts,
)
from custom_components.bosch_buderus_heating.plant_inputs import (
    CENTRAL,
    gateway_class,
)
from custom_components.bosch_buderus_heating.system_components import (
    BEHAVIOUR_BASES,
    ComponentTable,
    ComponentType,
    load_table,
)

CASES = Path(__file__).parent / "fixtures" / "plant_cases"
_HEAT_SOURCE_TYPE = re.compile(r"/heatSources/(hs[0-9]+)/type")
_HEAT_PUMP_TYPE = re.compile(r"/heatSources(?:/(hs[0-9]+))?/heatPumpType")


def load_case(name: str) -> dict[str, Any]:
    return json.loads((CASES / f"{name}.json").read_text(encoding="utf-8"))


def case_names() -> list[str]:
    return sorted(path.stem for path in CASES.glob("*.json"))


def inputs_of(case: dict[str, Any]) -> PlantInputs:
    """The inputs a gateway with this plant reports (see plant_inputs)."""
    values: dict[str, str] = case["values"]
    gateway = case["gateway"] or {}
    if case["cloud"] is not None:
        rows = parse_cloud_rows(case["cloud"])
    else:
        rows = parse_local_rows(case["local"] or [])
    readings = [(path, value) for path, value in values.items()]
    readings += [(path, None) for path in case.get("named_paths", ())]
    klass = gateway_class(
        SimpleNamespace(device_type=gateway.get("device_type"), model=None)
    )
    device_type = str(gateway.get("device_type") or "").casefold()
    platform = case.get("platform") or ("icom" if device_type == "icom" else None)
    return PlantInputs(
        gateway_class=klass,
        gateway_firmware=gateway.get("firmware"),
        system_type=values.get("/system/type"),
        heat_source_types=tuple(
            sorted(
                (match.group(1), value)
                for path, value in values.items()
                if (match := _HEAT_SOURCE_TYPE.fullmatch(path))
            )
        ),
        heat_sources_type=values.get("/heatSources/type"),
        heat_pump_types=tuple(
            sorted(
                (match.group(1) or CENTRAL, value)
                for path, value in values.items()
                if (match := _HEAT_PUMP_TYPE.fullmatch(path))
            )
        ),
        bus=values.get("/system/bus"),
        outdoor_status=values.get("/heatSources/hybrid/outdoorStatus"),
        components=rows,
        hybrid_evidence=hybrid_evidence(
            item for item in readings if hybrid_path_counts(item[0])
        ),
        platform=platform,
    )


def test_every_case_is_constructed_and_described() -> None:
    names = case_names()
    assert len(names) >= 30
    for name in names:
        case = load_case(name)
        header = case["header"]
        assert (
            header["method"] == "constructed from the structure of a diagnostics file"
        )
        assert {"device_class", "firmware_family", "captured", "case"} <= set(header)
        rendered = json.dumps(case)
        # Identifiers in the cases are markers, never real values.
        for match in re.findall(r"[0-9A-Fa-f]{16,}", rendered):
            pytest.fail(f"{name}: long hexadecimal text {match[:4]}...")


@pytest.mark.parametrize("name", case_names())
def test_decision_table(name: str) -> None:
    case = load_case(name)
    if case.get("no_block"):
        pytest.skip("no plant block for this kind of device")
    expected = case["expected"]
    result = resolve(inputs_of(case), load_table())
    assert result.plant_kind == expected["plant_kind"]
    assert result.fuel == expected["fuel"]
    assert result.cascade == expected["cascade"]
    assert list(result.evidence) == expected["evidence"]
    assert list(result.conflicts) == expected["conflicts"]
    assert result.gates[GATE_NAME].result == expected["gate"]
    assert result.transient is expected.get("transient", False)
    assert set(result.evidence) <= set(EVIDENCE_CODES)
    assert set(result.conflicts) <= set(CONFLICT_CODES)
    assert result.plant_kind is None or result.plant_kind in PLANT_KINDS


@pytest.mark.parametrize(
    ("version", "passes"),
    [
        ("06.01", True),
        ("6.1", True),
        ("06.01-x", True),
        ("06.01.00-12-ab", True),
        ("06.02", True),
        ("7", True),
        ("06.00.99", False),
        ("06", False),
        ("05.99", False),
        ("", False),
        (None, False),
        ("ab.cd", False),
        ("06.0a", False),
        ("x6.01", False),
        ("-06.01", False),
    ],
)
def test_version_rule(version: str | None, passes: bool) -> None:
    assert version_at_least(version, "06.01") is passes


def test_version_parts() -> None:
    assert version_parts("06.01-5.33") == (6, 1)
    assert version_parts("1.b.3") == (1, 0, 3)
    assert version_parts("") == (0,)
    assert version_parts("١٢.1") == (0, 1)
    assert version_at_least("11.00.00", "11.00.00")
    assert not version_at_least("10.99.99", "11.00.00")
    assert version_at_least("08.02.02", "08.02.02")
    assert not version_at_least("8.2.1", "08.02.02")


def _hybrid_place(
    *rows: ComponentRow, klass: str = "k40", firmware: str = "12.0"
) -> PlantInputs:
    return PlantInputs(
        gateway_class=klass,
        gateway_firmware=firmware,
        heat_source_types=(("hs1", "GasBoiler"),),
        components=rows,
    )


def test_only_the_first_row_at_the_hybrid_place_counts() -> None:
    table = load_table()
    old = ComponentRow(248, 83, "06.00")
    new = ComponentRow(248, 83, "06.02")
    xcu = ComponentRow(16, 83)
    first_old = resolve(_hybrid_place(old, xcu), table)
    assert first_old.plant_kind == "boiler"
    gate = first_old.gates[GATE_NAME]
    assert (gate.result, gate.reason, gate.hybrid_place_rows) == (
        "failed",
        "version",
        2,
    )
    assert (gate.first_type, gate.first_version) == (248, "06.00")
    assert resolve(_hybrid_place(xcu, old), table).plant_kind == "hybrid"
    assert resolve(_hybrid_place(new, old), table).plant_kind == "hybrid"


def test_the_hybrid_manager_counts_only_at_the_hybrid_place() -> None:
    table = load_table()
    for row in (ComponentRow(248, 8, "06.02"), ComponentRow(248, None, "06.02")):
        result = resolve(_hybrid_place(row), table)
        assert result.plant_kind == "boiler"
        assert result.gates[GATE_NAME].result == "not_applicable"
    other = resolve(_hybrid_place(ComponentRow(8, 83, "9.0")), table)
    assert other.plant_kind == "boiler"
    assert other.gates[GATE_NAME].reason == "not_a_hybrid_manager"


@pytest.mark.parametrize(
    ("klass", "firmware", "result", "reason"),
    [
        ("k40", "11.00.00", "passed", None),
        ("k40rf", "10.99.99", "failed", "firmware"),
        ("mx400", None, "failed", "firmware"),
        ("mx300", "08.02.02", "passed", None),
        ("k30", "08.02.01", "failed", "firmware"),
        ("heating_gateway", "99.0", "failed", "platform"),
        ("heat_pump_gateway", "99.0", "failed", "platform"),
    ],
)
def test_firmware_limits(
    klass: str, firmware: str | None, result: str, reason: str | None
) -> None:
    outcome = resolve(
        _hybrid_place(ComponentRow(16, 83), klass=klass, firmware=firmware or ""),
        load_table(),
    )
    gate = outcome.gates[GATE_NAME]
    assert (gate.result, gate.reason) == (result, reason)
    assert outcome.plant_kind == ("hybrid" if result == "passed" else "boiler")


@pytest.mark.parametrize("platform", ["icom", "rrc"])
def test_other_gateways_never_use_the_rule(platform: str) -> None:
    inputs = replace(_hybrid_place(ComponentRow(248, 83, "06.02")), platform=platform)
    result = resolve(inputs, load_table())
    assert result.plant_kind == "boiler"
    assert result.gates[GATE_NAME].reason == "platform"
    assert result.conflicts == ("component_rule_skipped",)


def test_without_a_table_the_rule_is_skipped() -> None:
    result = resolve(_hybrid_place(ComponentRow(16, 83)), None)
    assert result.plant_kind == "boiler"
    assert result.gates[GATE_NAME].reason == "table_unavailable"


def test_the_component_type_172_never_decides() -> None:
    """W1: the controller of an iCom heat pump is a name only."""
    table = load_table()
    assert table is not None
    rows = (ComponentRow(172, 8), ComponentRow(158, 16), ComponentRow(165, 56))
    assert resolve(PlantInputs(components=rows, bus="EMS2_0"), table).plant_kind is None
    boiler = resolve(
        PlantInputs(components=rows, heat_source_types=(("hs1", "OilBoiler"),)), table
    )
    assert boiler.plant_kind == "boiler" and boiler.conflicts == ()


def _without(table: ComponentTable, type_id: int) -> ComponentTable:
    components = dict(table.components)
    components.pop(type_id)
    return replace(table, components=components)


def _renamed(table: ComponentTable, type_id: int) -> ComponentTable:
    components = dict(table.components)
    entry = components[type_id]
    components[type_id] = ComponentType(
        entry.id, "BURNER_HYBRID_BOILER", (), entry.basis
    )
    return replace(table, components=components)


@pytest.mark.parametrize("name", case_names())
def test_name_entries_never_change_the_result(name: str) -> None:
    """I5: removing, renaming or adding a name entry changes no result."""
    case = load_case(name)
    if case.get("no_block"):
        pytest.skip("no plant block for this kind of device")
    table = load_table()
    assert table is not None
    inputs = inputs_of(case)
    expected = resolve(inputs, table)
    for type_id in list(table.components):
        for changed in (_without(table, type_id), _renamed(table, type_id)):
            result = resolve(inputs, changed)
            assert (result.plant_kind, result.evidence, result.conflicts) == (
                expected.plant_kind,
                expected.evidence,
                expected.conflicts,
            )
    extra = dict(table.components)
    for row in inputs.components:
        if row.type_id is not None and row.type_id not in extra:
            extra[row.type_id] = ComponentType(row.type_id, "HYBRID", (), "name")
    result = resolve(inputs, replace(table, components=extra))
    assert result.plant_kind == expected.plant_kind


def test_a_role_with_the_basis_name_never_decides() -> None:
    table = load_table()
    assert table is not None
    roles = tuple(
        replace(role, basis="name") if role.role == "hybrid_manager" else role
        for role in table.roles
    )
    # The stub table cannot hold such a role; the rule must ignore it anyway.
    changed = replace(table, roles=roles)
    result = resolve(_hybrid_place(ComponentRow(16, 83)), changed)
    assert result.plant_kind == "boiler"
    assert result.gates[GATE_NAME].reason == "not_a_hybrid_manager"
    assert "name" not in BEHAVIOUR_BASES


def test_the_component_table_interface() -> None:
    table = system_components.load_table()
    assert table is not None
    assert system_components.load_table() is table
    assert table.role(248, 83) is not None
    assert table.role(248, 8) is None
    assert table.has_flag(240, "hmi800_hp_light")
    assert not table.has_flag(248, "hmi800_hp_light")
    place = table.place(83)
    assert place is not None and place.key == "hybrid_manager"
    assert system_components.is_gateway_row(17, 72)
    assert system_components.is_gateway_row(None, 75)
    assert system_components.is_gateway_row(17, None)
    assert not system_components.is_gateway_row(8, 8)
    assert system_components.parse_table({"schema": 99}) is None
    assert system_components.parse_table("x") is None


def test_the_order_of_rows_never_changes_the_kind_except_at_the_hybrid_place() -> None:
    case = load_case("k40_own")
    inputs = inputs_of(case)
    table = load_table()
    expected = resolve(inputs, table)
    reordered = replace(inputs, components=tuple(reversed(inputs.components)))
    assert resolve(reordered, table) == expected
