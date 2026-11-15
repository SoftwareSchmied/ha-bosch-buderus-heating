"""Reading a program strictly: what is shown, what is only noted and what is
refused (limits, references, number forms, aircon shapes)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.switch_programs import (
    assess_switch_program,
)

HEATING = "/heatingCircuits/hc1/switchPrograms/A"
COOLING = "/heatingCircuits/hc1/cooling/switchPrograms/A"
ZONE = "/zones/zone1/switchPrograms/heating"
WDDW2 = "/dhwCircuits/dhw1/switchPrograms/program1"
AIRCON = "/airConditioning/switchPrograms/program1"
AIRCON_FIXTURE = (
    Path(__file__).parent / "fixtures/contracts/pointt-rac/switch-programs.json"
)


def point(day: str, minute: int, setpoint: Any) -> dict[str, Any]:
    return {"dayOfWeek": day, "time": minute, "setpoint": setpoint}


def program(path: str, points: list[Any] | None, **fields: Any) -> Any:
    item: dict[str, Any] = {
        "id": path,
        "type": "switchProgram",
        "writeable": 1,
        "maxNbOfSwitchPoints": 42,
        "maxNbOfSwitchPointsPerDay": 6,
        "switchPointTimeRaster": 15,
    }
    if points is not None:
        item["switchPoints"] = points
    item.update(fields)
    return parse_resource(item, path=path)


def levels_reference(circuit: str = "/heatingCircuits/hc1") -> dict[str, str]:
    return {"id": f"{circuit}/temperatureLevels"}


def assess(path: str, points, **fields):
    return assess_switch_program(program(path, points, **fields))


# ------------------------------------------------------------ state shape
def test_structured_view_lists_sorted_points_per_lower_case_day():
    result = assess(
        HEATING,
        [
            point("Tu", 60, "eco"),
            point("Mo", 1320, "eco"),
            point("Mo", 390, "comfort2"),
        ],
        setpointProperty=levels_reference(),
    )
    assert result.supported
    assert result.structured() == {
        "mo": [
            {"time": "06:30", "level": "comfort2"},
            {"time": "22:00", "level": "eco"},
        ],
        "tu": [{"time": "01:00", "level": "eco"}],
    }


# ----------------------------------------------- heating (legacy) programs
@pytest.mark.parametrize(
    ("reference", "note"),
    [
        (levels_reference(), "setpoint_property_levels"),
        ({"id": "/heatingCircuits/hc1/other"}, "setpoint_property_other"),
        (None, "setpoint_property_missing"),
    ],
)
def test_an_absolute_heating_program_reports_how_its_level_reference_reads(
    reference, note
):
    fields = {} if reference is None else {"setpointProperty": reference}
    result = assess(
        HEATING, [point("Mo", 360, "21.0"), point("Mo", 1320, "17.5")], **fields
    )
    assert result.supported
    assert result.value_type == "absolute"
    assert note in result.notes


def test_a_level_program_with_a_range_is_an_unknown_form():
    result = assess(
        HEATING,
        [point("Mo", 390, "comfort2")],
        setpointProperty=levels_reference(),
        switchPointMin=10.0,
    )
    assert not result.supported
    assert result.reason == "unsupported_schema"


def test_a_level_program_needs_its_level_reference():
    result = assess(HEATING, [point("Mo", 390, "comfort2")])
    assert not result.supported
    assert result.reason == "unsupported_setpoint_property"


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"switchPointTimeRaster": 7}, "invalid_limits"),
        ({"maxNbOfSwitchPoints": 0}, "invalid_limits"),
        ({"maxNbOfSwitchPoints": 1}, "too_many_switch_points"),
        ({"maxNbOfSwitchPointsPerDay": 1}, "too_many_switch_points"),
    ],
)
def test_limits_of_a_heating_program_are_enforced_on_reading(fields, reason):
    result = assess(
        HEATING,
        [point("Mo", 390, "comfort2"), point("Mo", 1320, "eco")],
        setpointProperty=levels_reference(),
        **fields,
    )
    assert not result.supported
    assert result.reason == reason


def test_a_heating_program_without_points_is_refused():
    reference = {"setpointProperty": levels_reference()}
    assert assess(HEATING, None, **reference).reason == "missing_switch_points"
    assert assess(HEATING, [], **reference).reason == "no_switch_points"


# ------------------------------------------------ cooling, zones, WDDW2
def test_a_cooling_program_without_points_is_refused():
    result = assess(COOLING, None)
    assert not result.supported
    assert result.reason == "missing_switch_points"


def test_an_implausibly_long_cooling_program_is_refused():
    points = [point("Mo", 0, "on")] * (168 * 3 + 1)
    result = assess(COOLING, points)
    assert not result.supported
    assert result.reason == "too_many_switch_points"


def test_a_raster_that_does_not_divide_the_day_counts_as_unreported():
    result = assess(COOLING, [point("Mo", 600, "on")], switchPointTimeRaster=7)
    assert result.supported
    assert result.time_raster is None
    assert "program_limits_missing" in result.notes


def test_a_level_reference_on_a_cooling_program_is_only_reported():
    result = assess(
        COOLING,
        [point("Mo", 600, "on")],
        setpointProperty=levels_reference("/heatingCircuits/hc1/cooling"),
    )
    assert result.supported
    assert "setpoint_property_reported" in result.notes


def test_an_empty_program_and_one_over_its_limits_are_shown_with_notes():
    empty = assess(COOLING, [])
    assert empty.supported and "program_empty" in empty.notes
    over_all = assess(
        COOLING,
        [point("Mo", 0, "on"), point("Mo", 60, "off")],
        maxNbOfSwitchPoints=1,
    )
    assert over_all.supported and "program_over_limit" in over_all.notes
    over_day = assess(
        COOLING,
        [point("Mo", 0, "on"), point("Mo", 60, "off")],
        maxNbOfSwitchPointsPerDay=1,
    )
    assert over_day.supported and "program_over_limit" in over_day.notes


@pytest.mark.parametrize(
    ("setpoint", "supported"),
    [
        ("21.0", True),
        (21, True),
        (21.5, True),
        ("21.50", False),
        ("abc", False),
        (True, False),
        (None, False),
    ],
)
def test_zone_numbers_are_read_only_in_the_form_the_app_writes(setpoint, supported):
    result = assess(ZONE, [point("Mo", 360, setpoint)])
    assert result.supported is supported
    if not supported:
        # Text that is no number is taken for a level of another program kind.
        assert result.reason in {"invalid_switch_point", "mixed_setpoints"}


def test_zone_numbers_as_text_and_as_numbers_in_one_program_are_refused():
    result = assess(ZONE, [point("Mo", 360, "21.0"), point("Mo", 540, 17.5)])
    assert not result.supported
    assert result.reason == "mixed_setpoints"


@pytest.mark.parametrize(
    ("setpoint", "supported"),
    [("off", True), ("45", True), (45, True), ("045", False), (45.5, False)],
)
def test_wddw2_degrees_are_whole_numbers_or_off(setpoint, supported):
    result = assess(WDDW2, [point("Mo", 360, setpoint)])
    assert result.supported is supported
    assert result.value_type == "degrees" if supported else not result.supported


def test_a_heating_program_in_absolute_form_never_takes_json_numbers():
    result = assess(
        HEATING,
        [point("Mo", 360, 21.0)],
        setpointProperty=levels_reference(),
    )
    assert not result.supported


# ------------------------------------------------------------ air conditioner
def aircon(points=None, **changes):
    data = json.loads(AIRCON_FIXTURE.read_text(encoding="utf-8"))["resources"]
    item = copy.deepcopy(data[AIRCON])
    if points is not None:
        item["switchPoints"] = points
    item.update(changes)
    return parse_resource(item, path=AIRCON)


def test_the_aircon_fixture_reads():
    result = assess_switch_program(aircon())
    assert result.supported and result.kind == "aircon"


@pytest.mark.parametrize(
    "changes",
    [
        {"value": 1, "type": "stringValue"},
        {"unexpectedField": 1},
    ],
)
def test_an_aircon_program_of_another_shape_is_refused(changes):
    result = assess_switch_program(aircon(**changes))
    assert not result.supported
    assert result.reason == "unsupported_schema"


def test_an_aircon_program_without_a_point_list_is_refused():
    item = json.loads(AIRCON_FIXTURE.read_text(encoding="utf-8"))["resources"][AIRCON]
    item = copy.deepcopy(item)
    item.pop("switchPoints")
    result = assess_switch_program(parse_resource(item, path=AIRCON))
    assert result.reason == "missing_switch_points"


def test_an_implausibly_long_aircon_program_is_refused():
    base = aircon().program_fields["switchPoints"][0]
    result = assess_switch_program(aircon([base] * (168 * 3 + 1)))
    assert result.reason == "too_many_switch_points"


def test_a_repeated_aircon_time_is_refused_and_an_empty_one_is_noted():
    base = aircon().program_fields["switchPoints"][0]
    result = assess_switch_program(aircon([base, base]))
    assert not result.supported and result.reason == "duplicate_switch_point"
    empty = assess_switch_program(aircon([]))
    assert empty.supported and "program_empty" in empty.notes


def test_an_aircon_raster_that_does_not_divide_the_day_counts_as_unreported():
    result = assess_switch_program(aircon(switchPointTimeRaster=7))
    assert result.supported
    assert result.time_raster is None
    assert "program_limits_missing" in result.notes


@pytest.mark.parametrize(
    "change",
    [
        {"dayOfWeek": "Xx"},
        {"time": 1440},
        {"time": -5},
        {"time": 7},  # not on the 15-minute raster
        {"unexpected": 1},
    ],
)
def test_an_aircon_point_that_does_not_fit_refuses_the_whole_program(change):
    base = copy.deepcopy(aircon().program_fields["switchPoints"][0])
    base.update(change)
    result = assess_switch_program(aircon([base], switchPointTimeRaster=15))
    assert not result.supported
    assert result.reason == "invalid_switch_point"


def test_an_aircon_program_of_another_resource_type_is_shown_with_a_note():
    result = assess_switch_program(aircon(type="stringValue"))
    assert result.supported
    assert "program_type_other" in result.notes
