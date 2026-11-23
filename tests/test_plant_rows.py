"""Component rows: every field checked, serial numbers never kept."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.bosch_buderus_heating import legacy_holidays
from custom_components.bosch_buderus_heating.component_rows import (
    IDENTIFIER_FIELDS,
    MAX_ROWS,
    ComponentRow,
    clean_version,
    component_number,
    identifiers,
    parse_cloud_rows,
    parse_local_rows,
    row_from_list,
    rows_from_lists,
)
from custom_components.bosch_buderus_heating.outdoor_unit_software import (
    clean_version as outdoor_clean_version,
)
from custom_components.bosch_buderus_heating.pointt import Resource

SERIAL = "SERIAL-MARKER-77"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("8", 8),
        (" 83 ", 83),
        (65281, 65281),
        ("999999", 999999),
        (0, 0),
        ("1234567", None),
        (1_000_000, None),
        (True, None),
        (False, None),
        (8.0, None),
        ("8.0", None),
        ("", None),
        ("  ", None),
        (-1, None),
        ("-1", None),
        ("0x53", None),
        (None, None),
        ("\N{FULLWIDTH DIGIT EIGHT}", None),
    ],
)
def test_component_numbers(value: object, expected: int | None) -> None:
    assert component_number(value) == expected


def test_a_full_cloud_row_keeps_checked_fields_only() -> None:
    rows = parse_cloud_rows(
        [
            {
                "Id": "248",
                "Tok": "83",
                "Ver": "06.02.00-abc",
                "ProductName": "Example hybrid manager",
                "ModuleHwIdentStr": "HM200",
                "SwUpdateType": "can_uds_example",
                "SwIdenStr": "example.sw:hyb.1",
                "ProductTtn": "1000000009",
                "ModuleTtn": "2000000009",
                "No": SERIAL,
                "ModuleSerialNumber": SERIAL + "1",
                "ProductSerialNumber": SERIAL + "2",
                "ProductUuid": "UUID-MARKER-1",
            }
        ]
    )
    assert rows == (
        ComponentRow(
            248,
            83,
            "06.02.00-abc",
            "Example hybrid manager",
            "HM200",
            "can_uds_example",
            "example.sw:hyb.1",
            "1000000009",
        ),
    )
    assert SERIAL not in repr(rows) and "UUID-MARKER" not in repr(rows)


def test_type_numbers_and_places_are_not_mixed_up() -> None:
    rows = parse_cloud_rows(
        [{"Id": "16", "Tok": "17"}, {"Id": "17", "Tok": "16"}, {"Id": 103, "Tok": 8}]
    )
    assert [(row.type_id, row.tok) for row in rows] == [(16, 17), (17, 16), (103, 8)]
    assert [row.key for row in rows] == [(17, 16), (16, 17), (8, 103)]


def test_a_row_without_type_and_place_is_dropped() -> None:
    rows = parse_cloud_rows(
        [
            {"ProductName": "Example name", "Ver": "1.0"},
            {"Id": "", "Tok": "", "Ver": "", "ProductName": ""},
            {"Id": True, "Tok": 8.5},
            {"Id": "8"},
            {"Tok": "72"},
            "not a row",
            None,
        ]
    )
    assert [(row.type_id, row.tok) for row in rows] == [(8, None), (None, 72)]


def test_serial_numbers_in_other_fields_are_left_out() -> None:
    (row,) = parse_cloud_rows(
        [
            {
                "Id": "8",
                "Tok": "8",
                "Ver": SERIAL,
                "ProductName": SERIAL,
                "ModuleHwIdentStr": SERIAL,
                "SwUpdateType": SERIAL,
                "SwIdenStr": SERIAL,
                "ProductTtn": SERIAL,
                "No": SERIAL,
            }
        ]
    )
    assert row == ComponentRow(8, 8)
    (row,) = parse_cloud_rows(
        [{"Id": "8", "Tok": "8", "ProductName": "12345678901", "Ver": "1.0"}]
    )
    assert row.product_name is None and row.version == "1.0"


def test_a_version_equal_to_an_identifier_is_left_out() -> None:
    (row,) = parse_cloud_rows(
        [{"Id": "8", "Tok": "8", "Ver": "1.2.3", "ModuleSerialNumber": " 1.2.3 "}]
    )
    assert row.version is None
    (row,) = parse_cloud_rows(
        [{"Id": "8", "Tok": "8", "Ver": "1.2.3", "ProductUuid": "1.2.3"}]
    )
    assert row.version is None


@pytest.mark.parametrize(
    "value",
    [
        "\xff\xff\xff",
        "1.0\x00\x01x",
        # A control character drops the whole text (as for technical texts),
        # also where the rest would pass.
        "1.0\x00",
        "\x1b12.11.1",
        "1" + ".2" * 32,  # 65 characters: dropped, never cut
        "abc",
        "1.0 beta",
        "-1.0",
        "1;0",
        12.5,
        None,
        "",
    ],
)
def test_unsafe_versions(value: object) -> None:
    assert clean_version(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "06.01",
        "12.11.1-2fc97779",
        "47.12-FIELD",
        "V01.012.05",
        "1+build",
        "\xff\xff1.0",
        "abc",
        "",
        "x" * 70,
        SERIAL,
    ],
)
def test_versions_follow_the_outdoor_unit_rule(value: str) -> None:
    known = frozenset({SERIAL.casefold()})
    assert clean_version(value, known) == outdoor_clean_version(value, known)


def test_control_characters_and_padding_are_never_kept() -> None:
    (row,) = parse_cloud_rows(
        [
            {
                "Id": "8",
                "Tok": "8",
                "ProductName": "\xff\xff",
                "ModuleHwIdentStr": "XCU\x00\x01",
                "SwIdenStr": "a b  c\nd",
                "SwUpdateType": "can uds;drop",
                "ProductTtn": "12 34",
                "ModuleTtn": "87;65",
            }
        ]
    )
    assert row.product_name is None
    assert row.hardware_type is None
    assert row.sw_ident == "a b c d"
    assert row.sw_update_type is None
    assert row.article_number is None


def test_the_article_number_prefers_the_product() -> None:
    (row,) = parse_cloud_rows(
        [{"Id": "253", "Tok": "16", "ProductTtn": "x y", "ModuleTtn": "2000000002"}]
    )
    assert row.article_number == "2000000002"


def test_at_most_64_rows_are_read() -> None:
    values = [{"Id": str(number), "Tok": "8"} for number in range(100)]
    rows = parse_cloud_rows(values)
    assert len(rows) == MAX_ROWS
    assert rows[-1].type_id == MAX_ROWS - 1
    assert len(parse_local_rows(values)) == MAX_ROWS


def test_local_rows_from_the_answer_and_from_decoded_modules() -> None:
    answer = [
        {
            "Tok": "8",
            "Id": "8",
            "Ver": "23.06.02-5.33-d510f849",
            "ProductName": "Example indoor unit",
            "ModuleSerialNumber": 5.703731019068751e21,
            "ProductSerialNumber": SERIAL,
            "ModuleHwIdentStr": "XCU_THH",
        }
    ]
    (row,) = parse_local_rows(answer)
    assert row == ComponentRow(
        8, 8, "23.06.02-5.33-d510f849", "Example indoor unit", "XCU_THH"
    )
    module = SimpleNamespace(
        token="72",
        id="17",
        version=SERIAL,
        serial_number=SERIAL,
        hardware="K40RF",
        product_serial=None,
        product_name=None,
    )
    assert parse_local_rows([module]) == (ComponentRow(17, 72, None, None, "K40RF"),)


def test_stored_rows_are_checked_again() -> None:
    row = ComponentRow(248, 83, "06.02", "Example", "HM200", "u", "s", "1")
    assert row_from_list(row.to_list()) == row
    assert rows_from_lists([row.to_list()]) == (row,)
    assert row_from_list([83, 248]) is None
    assert row_from_list(["x", 248, None, None, None, None, None, None]) is None
    assert row_from_list([None, None, None, None, None, None, None, None]) is None
    assert row_from_list([83, 248, "bad version", None, None, None, None, None]) == (
        ComponentRow(248, 83)
    )
    assert rows_from_lists([row.to_list(), "x"]) is None
    assert rows_from_lists("x") is None
    assert rows_from_lists([row.to_list()] * (MAX_ROWS + 1)) is None


def test_the_holiday_rule_keeps_its_own_parser() -> None:
    """I8: the holiday rule of iCom gateways reads its numbers unchanged."""
    resource = Resource(
        path=legacy_holidays.SYSTEM_INFO_PATH,
        values=(
            {"Id": "240", "Tok": "9"},
            {"Id": " 16 ", "Tok": "16"},
            {"Id": "158", "Tok": "24"},
            {"Id": 1_000_000, "Tok": "25"},
            {"Id": "x", "Tok": "26"},
        ),
    )
    components = legacy_holidays.icom_components(resource)
    assert components.items == (("240", "9"), ("16", "16"), ("158", "24"))
    assert components.hmi800_hp_light is True
    assert components.hidden_circuits == frozenset({"hc1"})
    # Its numbers stay texts as reported: a leading zero is kept.
    padded = Resource(
        path=legacy_holidays.SYSTEM_INFO_PATH,
        values=({"Id": "0240", "Tok": "016"}, {"Id": "1", "Tok": "024"}),
    )
    components = legacy_holidays.icom_components(padded)
    assert components.items == (("0240", "016"), ("1", "024"))
    assert components.hmi800_hp_light is False
    assert components.hidden_circuits == frozenset()


_TEXT = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=80),
    st.text(alphabet="0123456789.-_ \xff\x00", max_size=12),
)
_ROW = st.dictionaries(
    st.sampled_from(
        [
            "Id",
            "Tok",
            "Ver",
            "ProductName",
            "ModuleHwIdentStr",
            "SwUpdateType",
            "SwIdenStr",
            "ProductTtn",
            "ModuleTtn",
            "No",
            "ModuleSerialNumber",
            "ProductSerialNumber",
            "ProductUuid",
        ]
    ),
    _TEXT,
    max_size=13,
)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.one_of(_ROW, _TEXT), max_size=80))
def test_parsing_never_fails_and_never_keeps_identifiers(values: list[object]) -> None:
    rows = parse_cloud_rows(values)
    assert len(rows) <= MAX_ROWS
    for row in rows:
        assert row.type_id is not None or row.tok is not None
        assert row_from_list(row.to_list()) == row
        for text in (
            row.version,
            row.product_name,
            row.hardware_type,
            row.sw_update_type,
            row.sw_ident,
            row.article_number,
        ):
            assert text is None or (len(text) <= 64 and text.isprintable())
    for item in values[:MAX_ROWS]:
        if not isinstance(item, dict):
            continue
        known = identifiers(item.get(key) for key in IDENTIFIER_FIELDS)
        for row in parse_cloud_rows([item]):
            for text in (
                row.version,
                row.product_name,
                row.hardware_type,
                row.sw_update_type,
                row.sw_ident,
                row.article_number,
            ):
                # A kept text is never an identifier of its own row.
                assert text is None or text.casefold() not in known


@settings(max_examples=200, deadline=None)
@given(st.lists(_ROW, max_size=20))
def test_rows_do_not_depend_on_their_order(values: list[dict[str, object]]) -> None:
    shuffled = list(values)
    random.Random(7).shuffle(shuffled)
    assert sorted(parse_cloud_rows(values), key=repr) == sorted(
        parse_cloud_rows(shuffled), key=repr
    )


@pytest.mark.parametrize(
    "field",
    ["SwIdenStr", "ModuleHwIdentStr", "SwUpdateType", "ProductTtn"],
)
@pytest.mark.parametrize("bad", ["\x00", "\x07", "\N{ZERO WIDTH SPACE}"])
def test_a_technical_text_with_an_unprintable_character_is_dropped_whole(
    field: str, bad: str
) -> None:
    """Nothing of such a text is kept, not even its printable part."""
    value = "TOKEN_A"
    row = parse_cloud_rows([{"Id": "253", "Tok": "16", field: f"{value}{bad}"}])[0]
    kept = (
        row.version,
        row.hardware_type,
        row.sw_update_type,
        row.sw_ident,
        row.article_number,
    )
    assert all(part is None for part in kept)
