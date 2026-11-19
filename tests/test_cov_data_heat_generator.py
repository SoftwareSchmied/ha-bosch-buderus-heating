"""Heat generator table and module lists: limits of long or damaged lists,
faulty table entries, kept data of a restart and the choice between two
sources that disagree."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.bosch_buderus_heating import heat_generator
from custom_components.bosch_buderus_heating.heat_generator import (
    MAX_ROWS,
    ModuleRow,
    choose,
    clean_text,
    from_data,
    interpret,
    interpret_row,
    manufacturer_source,
    parse_table,
    rows_from_cloud,
    rows_from_local,
    unnamed_hardware_cloud,
    unnamed_hardware_local,
)

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
OBSERVED = {"kind": "observed", "note": "seen in a module list"}


def _table(*entries: dict[str, Any], **top: Any) -> Any:
    return parse_table(
        {
            "schema": 1,
            "table_version": 3,
            "constructions": ["monobloc"],
            "brand_families": ["bosch_group"],
            "documented_sources": ["bosch-homecomfort.com"],
            "entries": list(entries),
            **top,
        }
    )


def _entry(**change: Any) -> dict[str, Any]:
    return {
        "match": {"product_name": "Example"},
        "role": "heat_source",
        "examples": [{"product_name": "Example", "hardware_type": "HW"}],
        "evidence": OBSERVED,
        **change,
    }


def _modules(count: int) -> list[dict[str, str]]:
    return [{"ProductName": f"Module {index}"} for index in range(count)]


# ------------------------------------------------------------- module lists
def test_a_long_cloud_list_is_cut_at_the_limit() -> None:
    assert len(rows_from_cloud(_modules(MAX_ROWS + 20))) == MAX_ROWS
    unnamed = [{"ModuleHwIdentStr": f"HW{index}"} for index in range(MAX_ROWS + 20)]
    assert sum(unnamed_hardware_cloud(unnamed).values()) == MAX_ROWS


def test_a_long_local_list_is_cut_at_the_limit() -> None:
    named = [SimpleNamespace(product_name=f"Module {i}") for i in range(MAX_ROWS + 20)]
    assert len(rows_from_local(named)) == MAX_ROWS
    unnamed = [SimpleNamespace(hardware=f"HW{i}") for i in range(MAX_ROWS + 20)]
    assert sum(unnamed_hardware_local(unnamed).values()) == MAX_ROWS


def test_entries_of_a_cloud_list_that_are_no_modules_are_skipped() -> None:
    values: list[object] = [None, "text", 5, ["x"], {"ProductName": "Real"}]
    assert [row.product_name for row in rows_from_cloud(values)] == ["Real"]
    assert unnamed_hardware_cloud([None, 3, {"ModuleHwIdentStr": "XCU"}]) == {"XCU": 1}


def test_a_name_equal_to_a_serial_or_a_long_number_is_no_name() -> None:
    values = [
        {"ProductName": "SERIAL-1", "ModuleSerialNumber": "serial-1"},
        {"ProductName": "12345678", "ModuleHwIdentStr": "XCU"},
        {"ProductName": "Fine", "ModuleHwIdentStr": "NO-1", "No": "no-1"},
        {"ProductName": 42, "ProductUuid": 42},
        {"ProductName": True},
        {"ProductName": "   "},
        {"ProductName": "\xff\xff"},
    ]
    assert rows_from_cloud(values) == (ModuleRow("Fine", None),)
    assert unnamed_hardware_cloud(values) == {"XCU": 1, "unknown": 5}


def test_a_local_module_that_reports_no_serial_number_is_counted_by_hardware() -> None:
    """#139: missing descriptive fields are None, never a placeholder."""
    module = SimpleNamespace(
        product_name=None,
        serial_number=None,
        product_serial=None,
        hardware="Burner module 239",
        version=None,
    )
    bare = SimpleNamespace(
        product_name=None, serial_number=None, hardware=None, version=None
    )
    assert rows_from_local([module, bare]) == ()
    assert unnamed_hardware_local([module, bare]) == {
        "Burner module 239": 1,
        "unknown": 1,
    }


def test_local_modules_hide_serial_numbers_in_name_fields() -> None:
    module = SimpleNamespace(
        product_name="SN-9", serial_number="sn-9", product_serial=None, hardware="HW"
    )
    named = SimpleNamespace(
        product_name="Boiler", hardware="SN-9", serial_number="SN-9"
    )
    assert rows_from_local([module, named]) == (ModuleRow("Boiler", None),)
    assert unnamed_hardware_local([module, named]) == {"HW": 1}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (5, None),
        ("", None),
        ("  \t ", None),
        ("\xff\xff", None),
        ("a\x00b\tc  d", "a b c d"),
        ("name�x", "namex"),
        ("x" * 400, "x" * heat_generator.MAX_NAME_LENGTH),
    ],
)
def test_a_shown_text_is_cleaned_or_dropped(
    value: object, expected: str | None
) -> None:
    assert clean_text(value) == expected


# -------------------------------------------------------------- table faults
@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"family": 5}, "invalid_meaning"),
        ({"family": ""}, "invalid_meaning"),
        ({"family": "x" * 100}, "invalid_meaning"),
        ({"power_kw": 0}, "invalid_meaning"),
        ({"power_kw": 99999}, "invalid_meaning"),
        ({"match": {"product_name": ""}}, "invalid_match"),
        ({"match": {"product_name": 5}}, "invalid_match"),
        ({"match": {"hardware_type": 5}}, "invalid_match"),
        ({"match": {"hardware_type": " padded "}}, "invalid_match"),
        ({"match": "Example"}, "invalid_match"),
        ({"examples": "Example"}, "entry_without_examples"),
        ({"examples": ["Example"]}, "invalid_example"),
        (
            {"examples": [{"product_name": "Example", "serial": "x"}]},
            "invalid_example",
        ),
        ({"examples": [{"hardware_type": "HW"}]}, "invalid_example"),
        (
            {"examples": [{"product_name": " Example "}]},
            "invalid_example",
        ),
        (
            {"examples": [{"product_name": "Example", "hardware_type": " HW "}]},
            "invalid_example",
        ),
        (
            {"evidence": {"kind": "observed", "note": "x" * 600}},
            "entry_without_evidence",
        ),
        (
            {"evidence": {"kind": "observed", "note": "x", "extra": 1}},
            "invalid_evidence",
        ),
        ("not an entry", "invalid_entry"),
    ],
)
def test_a_faulty_table_entry_rejects_the_whole_table(
    change: Any, problem: str
) -> None:
    entry = change if isinstance(change, str) else _entry(**change)
    with pytest.raises(ValueError, match=problem):
        _table(_entry(), entry)


@pytest.mark.parametrize(
    "top",
    [
        {"table_version": True},
        {"table_version": 0},
        {"table_version": "1"},
        {"constructions": "monobloc"},
        {"constructions": [""]},
        {"brand_families": [5]},
        {"documented_sources": ["https://example.com"]},
        {"documented_sources": ["no-dot"]},
        {"documented_sources": "example.com"},
        {"entries": {}},
    ],
)
def test_a_table_header_that_does_not_fit_the_schema_is_rejected(top: Any) -> None:
    with pytest.raises(ValueError, match="invalid_table"):
        _table(_entry(), **top)


@pytest.mark.parametrize("data", [None, [], "x", {"schema": 2}, {}])
def test_a_table_of_another_shape_is_unsupported(data: object) -> None:
    with pytest.raises(ValueError, match="unsupported_table"):
        parse_table(data)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        5,
        "https://[::1",
        "https:///path",
        "https://user:pw@bosch-homecomfort.com/a",
    ],
)
def test_a_source_address_that_cannot_be_trusted_is_refused(url: object) -> None:
    assert manufacturer_source(url, ["bosch-homecomfort.com"]) is False


def test_a_source_with_a_path_prefix_needs_that_path() -> None:
    allowed = ["github.com/bosch-home-comfort"]
    assert manufacturer_source("https://github.com/bosch-home-comfort/x", allowed)
    assert manufacturer_source("https://github.com/bosch-home-comfort", allowed)
    assert not manufacturer_source(
        "https://github.com/bosch-home-comfort-fake/x", allowed
    )
    assert not manufacturer_source("https://github.com/someone/x", allowed)


def test_a_more_specific_entry_wins_per_field_and_unknown_modules_stay_other() -> None:
    table = _table(
        _entry(match={"hardware_type": "HW"}, role="outdoor_unit", power_kw=5),
        _entry(
            match={"product_name": "Example"},
            role=None,
            power_kw=9,
            family="Example family",
        ),
    )
    known = interpret_row(ModuleRow("Example", "HW"), table)
    assert (known.role, known.power_kw, known.family, known.known) == (
        "outdoor_unit",
        9,
        "Example family",
        True,
    )
    unknown = interpret_row(ModuleRow("Something else", "XX"), table)
    assert unknown.role == "other" and unknown.power_kw is None
    assert unknown.known is False
    assert interpret_row(ModuleRow("Another", "HW"), table).known is False


# ------------------------------------------------------- answers and restarts
def test_an_unknown_source_or_no_module_gives_nothing_or_an_error() -> None:
    with pytest.raises(ValueError, match="unknown_source"):
        interpret([ModuleRow("A", None)], "elsewhere")
    assert interpret([], "cloud") is None


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "x",
        {"source": "cloud"},
        {"rows": []},
        {"source": "elsewhere", "rows": []},
        {"source": "cloud", "rows": "x"},
        {"source": "cloud", "rows": [["A"]]},
        {"source": "cloud", "rows": [["A", None, 3]]},
        {"source": "cloud", "rows": [[None, None]]},
        {"source": "cloud", "rows": [["A", None], ["12345678", None]]},
        {"source": "cloud", "rows": ["A"]},
    ],
)
def test_kept_data_that_is_damaged_is_not_used(data: object) -> None:
    assert from_data(data) is None


def test_kept_data_is_worked_out_again_and_cut_at_the_limit() -> None:
    rows = [[f"Module {index}", None] for index in range(MAX_ROWS + 5)]
    kept = from_data({"source": "local", "rows": rows})
    assert kept is not None and len(kept.rows) == MAX_ROWS and kept.source == "local"
    assert kept.to_data()["source"] == "local"


def _answer(source: str, *names: str, at: datetime | None = NOW) -> Any:
    return interpret([ModuleRow(name, None) for name in names], source, observed_at=at)


def test_without_a_time_the_cloud_answer_stands_in_for_the_newer_one() -> None:
    cloud = _answer("cloud", "Boiler", at=None)
    local = _answer("local", "Other", at=None)
    assert choose(cloud, local, None) == (cloud, True)
    timed_local = _answer("local", "Other", at=NOW)
    assert choose(cloud, timed_local, None) == (timed_local, True)
    timed_cloud = _answer("cloud", "Boiler", at=NOW)
    untimed_local = _answer("local", "Other", at=None)
    assert choose(timed_cloud, untimed_local, None) == (timed_cloud, True)
    later = _answer("local", "Other", at=NOW + timedelta(minutes=1))
    assert choose(timed_cloud, later, None) == (later, True)


def test_disagreeing_sources_keep_the_answer_that_matches_the_last_shown() -> None:
    cloud = _answer("cloud", "Boiler", at=NOW)
    local = _answer("local", "Other", at=NOW + timedelta(minutes=1))
    assert choose(cloud, local, cloud) == (cloud, True)
    assert choose(cloud, local, local) == (local, True)
    neither = _answer("cloud", "Third")
    assert choose(cloud, local, neither) == (local, True)
    assert choose(None, None, neither) == (neither, False)
    assert choose(cloud, None, neither) == (cloud, False)
    assert choose(None, None, None) == (None, False)


def test_the_diagnostics_of_conflicting_sources_name_both_without_serials() -> None:
    cloud = _answer("cloud", "Boiler")
    local = _answer("local", "Other", at=NOW + timedelta(minutes=1))
    report = heat_generator.diagnostics(cloud, local, None)
    assert report["cloud_modules"] == 1 and report["local_modules"] == 1
    assert "Boiler" in str(report) and "SERIAL" not in str(report)
