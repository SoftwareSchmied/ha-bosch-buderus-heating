"""The component table: integrity, version, schema, fail-safe and lookups."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.bosch_buderus_heating import system_components
from custom_components.bosch_buderus_heating.system_components import (
    BASIS_VALUES,
    BEHAVIOUR_BASES,
    GATEWAY_PLACES,
    GATEWAY_TYPE_IDS,
    PLACE_KEYS,
    ROLE_VALUES,
    TABLE_PATH,
    ComponentTable,
    ComponentType,
    Place,
    Role,
    is_gateway_row,
    load_table,
    parse_table,
)
from tests.system_components_variants import (
    each_name_entry_alone,
    each_name_entry_removed,
    name_entry_types,
    raw_table,
    with_only_name_entry,
    without_name_entry,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "schemas" / "system-components.v1.json"
DOC_PATH = ROOT / "docs" / "system-components.md"
CAPTURES = ROOT / "tests" / "fixtures" / "contracts" / "captures"
# Component lists of real systems kept on their own (type and place only).
COMPONENT_LISTS = ROOT / "tests" / "fixtures" / "contracts" / "component_lists"

# Each table version with the hash of its content (everything except
# table_version and updated). A change of the content adds a new version
# with its hash here; an existing line is never edited.
TABLE_CONTENT_SHA256 = {
    1: "c2eaa6f1d225c215db852506c41616efe3a3232c438427db65ddebb9bcaa0040",
    2: "c70fdc33929ab1f1536c1044897b8d875c1954d513b4abf436cf21f0551e426f",
}

EXPECTED_PLACES = {
    8: ("heat_generator_control", "APPLIANCE_CU", "rule"),
    9: ("basic_controller", "", "rule"),
    16: ("system_controller", "SYSTEM_CONTROLLER", "rule"),
    17: ("pool", "POOL", "rule"),
    24: ("remote_hc1_legacy", "", "rule"),
    25: ("remote_hc2_legacy", "", "rule"),
    26: ("remote_hc3_legacy", "", "rule"),
    27: ("remote_hc4_legacy", "", "rule"),
    42: ("fresh_water", "", "rule"),
    48: ("solar", "SOLAR", "rule"),
    56: ("remote_hc1", "HC1_REMOTE", "name"),
    57: ("remote_hc2", "HC2_REMOTE", "name"),
    58: ("remote_hc3", "HC3_REMOTE", "name"),
    59: ("remote_hc4", "HC4_REMOTE", "name"),
    72: ("gateway", "INET", "observed"),
    75: ("gateway_service", "", "observed"),
    80: ("radio", "RF", "name"),
    81: ("ventilation", "HRV", "rule"),
    83: ("hybrid_manager", "HYBRID", "rule"),
}
# Every component that is not known by its name only. Raising an entry
# needs its criterion (rule, observation with a capture, document) and a
# change here.
EXPECTED_NOT_NAME_ONLY = {
    8: "observed",
    16: "rule",
    17: "observed",
    158: "observed",
    172: "observed",
    240: "rule",
    248: "rule",
    253: "observed",
    65281: "observed",
}
EXPECTED_ROLES = {
    (8, 8): ("heat_source", "observed"),
    (16, 83): ("hybrid_manager", "rule"),
    (17, 72): ("gateway", "observed"),
    (17, 75): ("gateway", "observed"),
    (158, 16): ("system_controller", "observed"),
    (172, 8): ("heat_source", "observed"),
    (248, 83): ("hybrid_manager", "rule"),
    (253, 16): ("system_controller", "observed"),
    (65281, 8): ("outdoor_unit", "observed"),
}


def content_hash(raw: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in raw.items()
        if key not in ("table_version", "updated")
    }
    text = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


@pytest.fixture
def table() -> ComponentTable:
    loaded = load_table()
    assert loaded is not None
    return loaded


@pytest.fixture
def fresh_cache() -> Iterator[None]:
    load_table.cache_clear()
    yield
    # The patched path is restored after this fixture; the next call reads
    # the shipped table again.
    load_table.cache_clear()


# ------------------------------------------------------------------ integrity


def test_the_shipped_table_loads_without_a_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert system_components._read(TABLE_PATH) == load_table()
    assert caplog.records == []


def test_the_file_is_ascii_with_line_feeds() -> None:
    data = TABLE_PATH.read_bytes()
    data.decode("ascii")
    assert b"\r" not in data
    assert data.endswith(b"}\n")


def test_the_table_has_178_component_rows(table: ComponentTable) -> None:
    raw = raw_table()
    types = [item["type"] for item in raw["components"]]
    assert len(types) == len(set(types)) == len(table.components) == 178
    assert types == sorted(types)
    # 177 type numbers of the table and the gateway row 17.
    assert len([t for t in types if t not in GATEWAY_TYPE_IDS]) == 177
    assert table.component(17) == ComponentType(17, "", (), "observed")


def test_names_are_kept_exactly(table: ComponentTable) -> None:
    def name(type_id: int) -> str:
        component = table.component(type_id)
        assert component is not None
        return component.name

    assert name(248) == "HYBRID_MOD_HYBRID_MOD_LIN"
    assert name(8) == "xCU_THH"
    assert name(65281) == "XCU_SRH"
    assert name(172) == "CUHP"
    assert name(253) == "HMI800_2_HP"
    assert name(0) == "INVALID"
    assert table.component(1) is None
    # No row for the place-only entry of the hybrid manager.
    assert "HYBRID" not in {c.name for c in table.components.values()}


def test_only_type_103_has_an_alias(table: ComponentTable) -> None:
    with_aliases = {
        c.id: (c.name, c.aliases) for c in table.components.values() if c.aliases
    }
    assert with_aliases == {103: ("IPM2", ("ISM2",))}
    names = [
        name
        for component in table.components.values()
        for name in (component.name, *component.aliases)
        if name
    ]
    assert len(names) == len(set(names))


def test_the_19_places_and_their_keys(table: ComponentTable) -> None:
    assert len(table.places) == 19
    assert {
        tok: (place.key, place.name, place.basis) for tok, place in table.places.items()
    } == EXPECTED_PLACES
    assert sorted(place.key for place in table.places.values()) == sorted(PLACE_KEYS)
    assert table.place(83) == Place(83, "hybrid_manager", "HYBRID", "rule")
    assert table.place(1) is None


def test_basis_values(table: ComponentTable) -> None:
    assert BASIS_VALUES == ("name", "rule", "observed", "documented")
    assert frozenset({"rule", "observed", "documented"}) == BEHAVIOUR_BASES
    assert all(c.basis in BASIS_VALUES for c in table.components.values())
    assert all(p.basis in BASIS_VALUES for p in table.places.values())
    assert all(r.basis in BEHAVIOUR_BASES for r in table.roles)
    not_name_only = {
        c.id: c.basis for c in table.components.values() if c.basis != "name"
    }
    assert not_name_only == EXPECTED_NOT_NAME_ONLY


def test_roles_and_flags_at_start(table: ComponentTable) -> None:
    assert {
        (role.type_id, role.tok): (role.role, role.basis) for role in table.roles
    } == EXPECTED_ROLES
    assert all(role.role in ROLE_VALUES for role in table.roles)
    assert table.flags == {240: frozenset({"hmi800_hp_light"})}
    raw = raw_table()
    assert raw["flags"] == [{"type": 240, "flag": "hmi800_hp_light", "basis": "rule"}]


def _capture_rows() -> set[tuple[str, str]]:
    """(Id, Tok) of every component row in the observed captures."""
    rows: set[tuple[str, str]] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("Id"), str) and isinstance(value.get("Tok"), str):
                rows.add((value["Id"], value["Tok"]))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for path in sorted([*CAPTURES.glob("*.json"), *COMPONENT_LISTS.glob("*.json")]):
        data = json.loads(path.read_text(encoding="utf-8"))
        provenance = data.get("provenance", {})
        if provenance.get("basis") == "observed_structure" and provenance.get(
            "physical_device_verified"
        ):
            visit(data.get("resources", {}))
    return rows


def test_observed_entries_have_a_note_and_a_capture() -> None:
    raw = raw_table()
    rows = _capture_rows()
    assert rows, "no observed capture with a component list"
    types = {type_id for type_id, _tok in rows}
    places = {tok for _type_id, tok in rows}
    checked = 0
    for kind in ("components", "places", "roles", "flags"):
        for item in raw[kind]:
            if item["basis"] != "observed":
                continue
            checked += 1
            assert item.get("note"), item
            if kind == "roles":
                assert (str(item["type"]), str(item["place"])) in rows, item
            elif kind == "places":
                assert str(item["place"]) in places, item
            else:
                assert str(item["type"]) in types, item
    assert checked == 15


def test_documented_entries_need_a_manufacturer_address() -> None:
    raw = raw_table()
    assert not any(
        item.get("source")
        for kind in ("components", "places", "roles", "flags")
        for item in raw[kind]
    )
    documented = copy.deepcopy(raw)
    documented["roles"][0].update(
        basis="documented",
        note="stated in the installation manual",
        source="https://www.buderus.de/de/produkte/manual",
    )
    assert parse_table(documented) is not None
    documented["roles"][0]["source"] = "https://example.com/manual"
    assert parse_table(documented) is None
    documented["roles"][0]["source"] = "http://www.buderus.de/manual"
    assert parse_table(documented) is None
    del documented["roles"][0]["source"]
    assert parse_table(documented) is None


# -------------------------------------------------------------- table version


def test_every_content_change_raises_the_table_version() -> None:
    raw = raw_table()
    versions = sorted(TABLE_CONTENT_SHA256)
    assert versions == list(range(1, len(versions) + 1))
    assert len(set(TABLE_CONTENT_SHA256.values())) == len(versions)
    assert raw["table_version"] == versions[-1]
    assert content_hash(raw) == TABLE_CONTENT_SHA256[raw["table_version"]]


def test_the_hash_sees_any_change_of_the_content() -> None:
    raw = raw_table()
    changed = copy.deepcopy(raw)
    changed["components"][1]["name"] = "RT801"
    assert content_hash(changed) != content_hash(raw)
    renumbered = copy.deepcopy(raw)
    renumbered["table_version"] = 2
    renumbered["updated"] = "2027-01-01"
    assert content_hash(renumbered) == content_hash(raw)


# --------------------------------------------------------------------- schema


_SUPPORTED_KEYWORDS = {
    "$schema",
    "title",
    "description",
    "type",
    "additionalProperties",
    "required",
    "properties",
    "const",
    "enum",
    "minimum",
    "maximum",
    "pattern",
    "maxLength",
    "minItems",
    "uniqueItems",
    "items",
    "anyOf",
    "allOf",
    "if",
    "then",
    "not",
}
_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def schema_errors(value: object, schema: Mapping[str, Any], at: str = "$") -> list[str]:
    """A small validator for the keywords the table schema uses."""
    unknown = set(schema) - _SUPPORTED_KEYWORDS
    assert not unknown, f"unsupported schema keywords {unknown} at {at}"
    errors: list[str] = []
    if "type" in schema and not _TYPES[schema["type"]](value):
        return [f"{at}: not {schema['type']}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{at}: not {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{at}: {value!r} not allowed")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{at}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{at}: above maximum")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{at}: pattern")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{at}: too long")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{at}: too few items")
        if schema.get("uniqueItems") and len(
            {json.dumps(item, sort_keys=True) for item in value}
        ) != len(value):
            errors.append(f"{at}: duplicate items")
        if "items" in schema:
            for index, item in enumerate(value):
                errors += schema_errors(item, schema["items"], f"{at}[{index}]")
    if isinstance(value, dict):
        errors += [
            f"{at}: missing {key}"
            for key in schema.get("required", ())
            if key not in value
        ]
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                errors += schema_errors(item, properties[key], f"{at}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{at}: unexpected {key}")
    if "anyOf" in schema and all(
        schema_errors(value, option, at) for option in schema["anyOf"]
    ):
        errors.append(f"{at}: no option matches")
    for option in schema.get("allOf", ()):
        errors += schema_errors(value, option, at)
    if "if" in schema and not schema_errors(value, schema["if"], at):
        errors += schema_errors(value, schema.get("then", {}), at)
    if "not" in schema and not schema_errors(value, schema["not"], at):
        errors.append(f"{at}: matches a forbidden form")
    return errors


def _schema() -> dict[str, Any]:
    value = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_the_shipped_table_meets_its_schema() -> None:
    assert schema_errors(raw_table(), _schema()) == []


def _broken_tables() -> Iterator[tuple[str, dict[str, Any]]]:
    raw = raw_table()

    def changed(label: str, change: Any) -> tuple[str, dict[str, Any]]:
        copied = copy.deepcopy(raw)
        change(copied)
        return label, copied

    yield changed("unknown field", lambda t: t.update(extra=1))
    yield changed("other schema", lambda t: t.update(schema=2))
    yield changed("version zero", lambda t: t.update(table_version=0))
    yield changed("version text", lambda t: t.update(table_version="1"))
    yield changed("bad date", lambda t: t.update(updated="22.11.2026"))
    yield changed("role with name basis", lambda t: t["roles"][0].update(basis="name"))
    yield changed("flag with name basis", lambda t: t["flags"][0].update(basis="name"))
    yield changed("unknown basis", lambda t: t["components"][1].update(basis="guess"))
    yield changed(
        "observed without note",
        lambda t: t["components"][6].pop("note"),
    )
    yield changed(
        "name basis without name",
        lambda t: t["components"][1].update(name=None),
    )
    yield changed("unknown place key", lambda t: t["places"][0].update(key="boiler"))
    yield changed("unknown role", lambda t: t["roles"][0].update(role="boiler"))
    yield changed("category field", lambda t: t["components"][1].update(category="x"))
    yield changed("type as text", lambda t: t["components"][1].update(type="3"))
    yield changed("type too large", lambda t: t["components"][1].update(type=10**6))
    yield changed("negative place", lambda t: t["places"][0].update(place=-1))
    yield changed("control character", lambda t: t["components"][1].update(name="A\tB"))
    yield changed("empty aliases", lambda t: t["components"][1].update(aliases=[]))
    yield changed(
        "source without documented",
        lambda t: t["roles"][0].update(source="https://www.buderus.de/x"),
    )


@pytest.mark.parametrize(
    ("label", "broken"), list(_broken_tables()), ids=lambda v: str(v)[:30]
)
def test_schema_and_parser_reject_the_same_faults(
    label: str, broken: dict[str, Any]
) -> None:
    assert schema_errors(broken, _schema()), label
    assert parse_table(broken) is None, label


@pytest.mark.parametrize(
    "change",
    [
        lambda t: t["components"].append(copy.deepcopy(t["components"][1])),
        lambda t: t["places"].append(copy.deepcopy(t["places"][0])),
        lambda t: t["roles"].append(copy.deepcopy(t["roles"][0])),
        lambda t: t["flags"].append(copy.deepcopy(t["flags"][0])),
        lambda t: t["places"][1].update(key="heat_generator_control"),
        lambda t: t["components"][1].update(aliases=["RT800"]),
        lambda t: t["roles"][0].update(type=1),
        lambda t: t["roles"][0].update(place=1),
        lambda t: t["flags"][0].update(type=1),
        lambda t: t["flags"][0].update(flag="other"),
        lambda t: t.update(components=[]),
        lambda t: t.update(places={}),
        lambda t: t.update(roles={}),
        lambda t: t.update(flags=None),
        lambda t: t.update(updated="2026-02-30"),
        lambda t: t.update(documented_sources=["Buderus"]),
    ],
)
def test_the_parser_rejects_duplicates_and_dangling_entries(change: Any) -> None:
    broken = raw_table()
    change(broken)
    assert parse_table(broken) is None


# ------------------------------------------------------------------ fail-safe


@pytest.mark.parametrize(
    "content",
    [
        None,
        b"",
        b"{",
        b"[]",
        b'{"schema": 2}',
        b"\xff\xfe",
        "partly broken",
    ],
)
def test_a_missing_or_faulty_table_gives_one_warning_and_no_table(
    tmp_path, monkeypatch, caplog, fresh_cache, content
) -> None:
    path = tmp_path / "system_components.json"
    if content == "partly broken":
        raw = raw_table()
        raw["components"][100]["basis"] = "guess"
        path.write_text(json.dumps(raw), encoding="utf-8")
    elif content is not None:
        path.write_bytes(content)
    monkeypatch.setattr(system_components, "TABLE_PATH", path)
    with caplog.at_level(logging.WARNING):
        assert load_table() is None
        assert load_table() is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "system component table could not be loaded" in warnings[0].getMessage()


def test_any_error_while_reading_gives_the_warning(
    monkeypatch, caplog, fresh_cache
) -> None:
    def broken(_raw: object) -> ComponentTable:
        raise RecursionError

    monkeypatch.setattr(system_components, "_parse", broken)
    with caplog.at_level(logging.WARNING):
        assert load_table() is None
    assert len(caplog.records) == 1


def test_the_shipped_table_is_read_once(monkeypatch, fresh_cache) -> None:
    reads: list[Path] = []
    original = system_components._read

    def counting(path: Path) -> ComponentTable | None:
        reads.append(path)
        return original(path)

    monkeypatch.setattr(system_components, "_read", counting)
    first = load_table()
    assert first is not None
    assert load_table() is first
    assert reads == [TABLE_PATH]


@pytest.mark.parametrize(
    "raw",
    [None, [], "table", 1, {"schema": 1}, {"schema": True}, {1: 2}, {"a": [1, {}]}],
)
def test_the_parser_never_raises(raw: object) -> None:
    assert parse_table(raw) is None


_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda inner: (
        st.lists(inner, max_size=4)
        | st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=12,
)


@settings(max_examples=150, deadline=None)
@given(
    kind=st.sampled_from(("components", "places", "roles", "flags")),
    index=st.integers(min_value=0, max_value=200),
    key=st.sampled_from(("type", "place", "name", "basis", "note", "key", "role")),
    value=_JSON,
)
def test_a_damaged_entry_never_raises(
    kind: str, index: int, key: str, value: object
) -> None:
    raw = raw_table()
    entries = raw[kind]
    entries[index % len(entries)][key] = value
    table = parse_table(raw)
    assert table is None or isinstance(table, ComponentTable)


# -------------------------------------------------------------------- lookups


def _table(
    *roles: Role, flags: Mapping[int, frozenset[str]] | None = None
) -> ComponentTable:
    return ComponentTable(
        schema=1,
        table_version=1,
        updated="2026-11-22",
        components=MappingProxyType({}),
        places=MappingProxyType({}),
        roles=roles,
        flags=MappingProxyType(dict(flags or {})),
    )


def test_a_role_at_the_exact_place_comes_before_a_role_at_any_place() -> None:
    anywhere = Role(8, None, "heat_source", "rule")
    exact = Role(8, 8, "outdoor_unit", "observed")
    for table in (_table(anywhere, exact), _table(exact, anywhere)):
        assert table.role(8, 8) == exact
        assert table.role(8, 16) == anywhere
        assert table.role(8, None) == anywhere
        assert table.role(9, 8) is None
    assert _table(exact).role(8, 16) is None
    assert _table(exact).role(8, None) is None


def test_a_role_without_a_behaviour_basis_never_applies() -> None:
    table = _table(
        Role(172, 8, "heat_source", "name"), Role(172, None, "heat_source", "name")
    )
    assert table.role(172, 8) is None
    assert table.role(172, None) is None
    fallback = _table(
        Role(8, 8, "heat_source", "name"), Role(8, None, "outdoor_unit", "rule")
    )
    assert fallback.role(8, 8) == Role(8, None, "outdoor_unit", "rule")


def test_shipped_roles(table: ComponentTable) -> None:
    def role(type_id: int, tok: int | None) -> str | None:
        found = table.role(type_id, tok)
        return None if found is None else found.role

    assert role(248, 83) == "hybrid_manager"
    assert role(16, 83) == "hybrid_manager"
    assert role(248, 8) is None
    assert role(248, None) is None
    assert role(16, 16) is None
    assert role(8, 8) == "heat_source"
    assert role(8, 16) is None
    assert role(65281, 8) == "outdoor_unit"
    assert role(253, 16) == "system_controller"
    assert role(17, 72) == "gateway"
    assert role(17, 75) == "gateway"
    # Seen on an iCom gateway: the controller of a heat pump and the system
    # controller, each only at its place.
    assert role(172, 8) == "heat_source"
    assert role(172, 16) is None
    assert role(172, None) is None
    assert role(158, 16) == "system_controller"
    assert role(158, 8) is None


def test_flags(table: ComponentTable) -> None:
    assert table.has_flag(240, "hmi800_hp_light")
    assert not table.has_flag(240, "other")
    assert not table.has_flag(241, "hmi800_hp_light")
    assert not _table().has_flag(240, "hmi800_hp_light")


@pytest.mark.parametrize(
    ("type_id", "tok", "gateway"),
    [
        (17, 72, True),
        (17, 75, True),
        (17, 8, True),
        (17, None, True),
        (8, 72, True),
        (None, 75, True),
        (189, 72, True),
        (8, 8, False),
        (253, 16, False),
        (72, 17, False),
        (75, 8, False),
        (None, None, False),
        (0, 0, False),
    ],
)
def test_gateway_rows(type_id: int | None, tok: int | None, gateway: bool) -> None:
    assert frozenset({72, 75}) == GATEWAY_PLACES
    assert frozenset({17}) == GATEWAY_TYPE_IDS
    assert is_gateway_row(type_id, tok) is gateway


# ------------------------------------------------------- name entries (I5)


def _behaviour(table: ComponentTable) -> tuple[object, ...]:
    types = (0, 3, 8, 16, 17, 103, 172, 240, 248, 253, 65281)
    pairs = [(t, tok) for t in types for tok in (None, 8, 16, 56, 72, 75, 83)]
    return (
        table.roles,
        tuple(table.role(type_id, tok) for type_id, tok in pairs),
        dict(table.flags),
        dict(table.places),
    )


def test_no_name_entry_changes_roles_flags_or_places(table: ComponentTable) -> None:
    expected = _behaviour(table)
    types = name_entry_types()
    assert len(types) == 169
    removed = dict(each_name_entry_removed())
    alone = dict(each_name_entry_alone())
    assert sorted(removed) == sorted(alone) == sorted(types)
    for type_id in types:
        assert _behaviour(removed[type_id]) == expected, type_id
        assert _behaviour(alone[type_id]) == expected, type_id
        assert removed[type_id].component(type_id) is None
        assert alone[type_id].component(type_id) == table.component(type_id)


def test_the_variant_helpers_keep_the_other_entries() -> None:
    raw = raw_table()
    without = without_name_entry(raw, 165)
    assert len(without["components"]) == 177
    only = with_only_name_entry(raw, 165)
    assert [c["type"] for c in only["components"]] == sorted(
        [*EXPECTED_NOT_NAME_ONLY, 165]
    )
    with pytest.raises(AssertionError):
        without_name_entry(raw, 8)


# ---------------------------------------------------------------------- docs


def _doc_section(heading: str) -> str:
    text = DOC_PATH.read_text(encoding="utf-8")
    start = text.index(heading + "\n")
    rest = text[start + len(heading) + 1 :]
    following = re.search(r"^#{1,3} ", rest, re.MULTILINE)
    return rest if following is None else rest[: following.start()]


def _doc_rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        if line.startswith("| ") and not line.startswith("|---"):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows[1:]


_NO_NAME = "\u2013"


def _shown(name: str) -> str:
    return f"`{name}`" if name else _NO_NAME


def test_the_documented_component_names_match_the_table(table: ComponentTable) -> None:
    expected = [
        [
            str(c.id),
            ", ".join(_shown(n) for n in (c.name, *c.aliases) if n) or _NO_NAME,
            f"`{c.basis}`",
        ]
        for c in sorted(table.components.values(), key=lambda c: c.id)
    ]
    assert _doc_rows(_doc_section("### Component names")) == expected


@pytest.mark.parametrize("heading", ["### Places", "### Plätze"])
def test_the_documented_places_match_the_table(
    table: ComponentTable, heading: str
) -> None:
    expected = [
        [str(p.tok), f"`{p.key}`", _shown(p.name), f"`{p.basis}`"]
        for p in sorted(table.places.values(), key=lambda p: p.tok)
    ]
    assert _doc_rows(_doc_section(heading)) == expected


@pytest.mark.parametrize(
    ("heading", "any_place"),
    [("### Basis of an entry", "any"), ("### Grundlage eines Eintrags", "jeder")],
)
def test_the_documented_roles_match_the_table(
    table: ComponentTable, heading: str, any_place: str
) -> None:
    rows = _doc_rows(_doc_section(heading))
    roles = rows[len(BASIS_VALUES) + 1 :]
    assert [row[0].strip("`") for row in rows[: len(BASIS_VALUES)]] == list(
        BASIS_VALUES
    )
    assert roles == [
        [
            str(r.type_id),
            any_place if r.tok is None else str(r.tok),
            f"`{r.role}`",
            f"`{r.basis}`",
        ]
        for r in sorted(table.roles, key=lambda r: (r.type_id, r.tok or 0))
    ]
