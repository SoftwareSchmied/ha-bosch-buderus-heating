"""Editing rules of the program kinds: why a program is read only, which
entries are refused, and how numbers go back on the wire."""

from __future__ import annotations

import copy
import math

import pytest

from custom_components.bosch_buderus_heating.switch_program_edit import (
    NumericRules,
    edit_issue,
    editable_program,
    encode_week,
    fingerprint,
    points_per_day,
    program_shapes,
    setpoint_text,
    value_issue,
    wddw2_range,
)
from custom_components.bosch_buderus_heating.switch_programs import SwitchPoint
from tests.program_kind_helpers import resource
from tests.test_switch_program_kinds import (
    WDDW2,
    WDDW2_RANGE,
    ZONE_HEAT,
    items,
    readings,
)


def zone(**changes):
    """The zone fixture with one program changed (readings and program)."""
    item = copy.deepcopy(items("zone")[ZONE_HEAT])
    for key, value in changes.items():
        if value is None:
            item.pop(key, None)
        else:
            item[key] = value
    data = readings("zone", **{ZONE_HEAT: item})
    return data[ZONE_HEAT], data


def with_points(points, **changes):
    return zone(switchPoints=points, **changes)


def numeric_zone():
    shown, data = zone()
    program = editable_program(shown, data)
    assert program is not None
    return program


# --------------------------------------------------------- why read only
def test_no_resource_and_other_paths_are_no_programs():
    assert edit_issue(None, {}) == "not_switch_program"
    other = resource(
        {"id": "/heatingCircuits/hc1/operationMode", "type": "stringValue"}
    )
    assert edit_issue(other, {}) == "not_switch_program"


def test_inconsistent_flags_make_a_program_read_only():
    shown, data = zone(available="maybe")
    assert edit_issue(shown, data) == "invalid_flags"
    assert editable_program(shown, data) is None


def test_a_program_over_its_own_limits_is_read_only():
    points = [
        {"dayOfWeek": "Mo", "time": 360, "setpoint": "21.0"},
        {"dayOfWeek": "Mo", "time": 540, "setpoint": "17.5"},
    ]
    shown, data = with_points(points, maxNbOfSwitchPoints=1)
    assert edit_issue(shown, data) == "program_over_limit"


def test_a_number_outside_the_reported_range_makes_the_program_read_only():
    shown, data = with_points([{"dayOfWeek": "Mo", "time": 360, "setpoint": "35.0"}])
    assert edit_issue(shown, data) == "point_out_of_range"


def test_a_program_that_only_switches_off_has_no_known_number_form():
    # No number read: whether numbers are text or numbers is not known.
    shown, data = with_points([])
    assert edit_issue(shown, data) == "number_format_unknown"
    wddw2 = copy.deepcopy(items("wddw2")[WDDW2])
    wddw2["switchPoints"] = [
        {"dayOfWeek": "Mo", "time": 360, "setpoint": "off"},
        {"dayOfWeek": "Mo", "time": 480, "setpoint": "off"},
    ]
    data = readings("wddw2", **{WDDW2: wddw2})
    assert edit_issue(data[WDDW2], data) == "number_format_unknown"


def test_a_program_that_cannot_be_read_has_no_fingerprint_and_no_counts():
    shown, _data = with_points([{"dayOfWeek": "Mo", "time": 361, "setpoint": "x"}])
    assert fingerprint(shown) is None
    assert points_per_day(shown) is None


def test_the_fingerprint_changes_with_any_point_and_not_with_the_order():
    shown, _data = zone()
    again, _ = zone(switchPoints=list(reversed(shown.program_fields["switchPoints"])))
    assert fingerprint(shown) == fingerprint(again)
    moved = copy.deepcopy(shown.program_fields["switchPoints"])
    moved[0]["time"] += 15
    changed, _ = zone(switchPoints=moved)
    assert fingerprint(shown) != fingerprint(changed)
    assert points_per_day(shown) is not None


# --------------------------------------------------------------- the range
def test_the_range_of_wddw2_skips_unusable_readings():
    broken = resource(
        {
            "id": WDDW2_RANGE,
            "type": "floatValue",
            "value": 50.0,
            "minValue": 60.0,
            "maxValue": 30.0,
        }
    )
    usable = resource(
        {
            "id": "/dhwCircuits/dhw1/manualsetpoint",
            "type": "floatValue",
            "value": 50.0,
            "minValue": 30.0,
            "maxValue": 60.0,
        }
    )
    assert wddw2_range({WDDW2_RANGE: broken}) is None
    assert wddw2_range({WDDW2_RANGE: broken, usable.path: usable}) == (30.0, 60.0)
    assert wddw2_range({}) is None


# ------------------------------------------------------------------ entries
@pytest.mark.parametrize("level", [5, None, 21.5, ["21.0"]])
def test_an_entry_that_is_no_text_is_refused(level):
    assert value_issue(level, numeric_zone()) == "value_not_allowed"
    rules = NumericRules(5.0, 30.0, 0.5, None, True, False)
    assert value_issue(level, rules) == "value_not_allowed"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("off", "off"),
        ("0.0", "0.0"),
        ("abc", None),
        (True, None),
        (None, None),
        (math.inf, None),
        (math.nan, None),
        (21.5, "21.5"),
        ("21.5", "21.5"),
    ],
)
def test_numbers_are_taken_as_entered_or_refused(value, text):
    assert setpoint_text(numeric_zone(), value) == text


def test_an_entry_with_more_decimals_is_kept_so_the_step_check_refuses_it():
    program = numeric_zone()
    text = setpoint_text(program, 21.123)
    assert text == repr(21.123)
    assert value_issue(text, program) == "value_not_allowed"


# ---------------------------------------------------------- the wire form
def test_numbers_read_as_numbers_go_back_as_numbers_and_off_stays_text():
    item = copy.deepcopy(items("wddw2")[WDDW2])
    item["switchPoints"] = [
        {"dayOfWeek": "Mo", "time": 360, "setpoint": 55},
        {"dayOfWeek": "Mo", "time": 1320, "setpoint": "off"},
    ]
    data = readings("wddw2", **{WDDW2: item})
    program = editable_program(data[WDDW2], data)
    assert program is not None and program.numbers_as_text is False
    week = (SwitchPoint(0, 1320, "off"), SwitchPoint(0, 360, "50"))
    assert encode_week(week, program) == [
        {"dayOfWeek": "Mo", "setpoint": 50, "time": 360},
        {"dayOfWeek": "Mo", "setpoint": "off", "time": 1320},
    ]


def test_absolute_numbers_read_as_numbers_go_back_as_floats():
    points = [
        {"dayOfWeek": "Mo", "time": 360, "setpoint": 21.0},
        {"dayOfWeek": "Mo", "time": 540, "setpoint": 17.5},
    ]
    shown, data = with_points(points)
    program = editable_program(shown, data)
    assert program is not None and program.numbers_as_text is False
    assert encode_week((SwitchPoint(0, 360, "20.5"),), program) == [
        {"dayOfWeek": "Mo", "setpoint": 20.5, "time": 360}
    ]


# ---------------------------------------------------------------- diagnostics
def test_diagnostics_name_the_forms_of_points_but_no_values_and_cap_them():
    points = [{key: 1} for key in ("a", "b", "c", "d", "e", "f")]
    shown, _data = with_points(points)
    shapes = program_shapes(shown)["point_shapes"]
    assert len(shapes) == 5 and shapes[-1] == "more"
    assert "1" not in str(shapes)


def test_the_reviewed_point_form_is_named_not_spelled_out():
    shown, _data = zone()
    assert program_shapes(shown)["point_shapes"] == ["reviewed"]
