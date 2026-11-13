"""R4 (T4): why an RRC program is not read or not editable, in diagnostics.

The reasons are stable tokens. The forms below are synthetic. A day
program without ``d`` and a point with an integer ``name`` are the forms a
TC100.2 reported (as reasons and in a diagnostics download, never as values);
they are accepted. Any other extra key, or a ``name`` of another type, keeps
the program read-only.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
from custom_components.bosch_buderus_heating.diagnostics import (
    _capability_diagnostics,
    _rrc_schedule_summary,
)
from custom_components.bosch_buderus_heating.pointt import Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_programs import (
    EDIT_ISSUES,
    SHAPE_ISSUES,
    parse_schedule,
    schedule_issues,
)

WEEK = "/programs/pg3/week"
DAY = "/programs/pg3/monday"
POINTS = [
    {"d": "Mo", "t": 0, "Th": 16},
    {"d": "Mo", "t": 425, "Th": 21.5},
    {"d": "Tu", "t": 0, "Th": 17},
]


def program(path: str = WEEK, value: Any = None, **fields: Any) -> Resource:
    return parse_resource(
        {
            "id": path,
            "type": "dayProgram" if path == DAY else "weekProgram",
            "writeable": True,
            "value": POINTS if value is None else value,
            **fields,
        }
    )


def with_point(index: int, **changes: Any) -> list[dict[str, Any]]:
    points = [dict(item) for item in POINTS]
    points[index].update(changes)
    return [
        {key: value for key, value in item.items() if value is not ...}
        for item in points
    ]


def test_an_editable_program_has_no_reason() -> None:
    assert schedule_issues(program()) == ()
    assert schedule_issues(program(DAY, POINTS[:2])) == ()
    # The reported forms are editable.
    assert schedule_issues(program(value=with_point(1, name=0))) == ()
    assert schedule_issues(program(DAY, [{"t": 0, "Th": 16, "name": 0}])) == ()


@pytest.mark.parametrize(
    ("resource", "reasons"),
    [
        # A week point needs ``d``; a day point may leave it out.
        (
            program(value=with_point(1, d=...)),
            ("day_missing_d",),
        ),
        (program(DAY, [{"t": 0, "Th": 16}, {"t": 425, "Th": 21.5}]), ()),
        # Only an integer ``name`` is allowed besides ``d``, ``t`` and ``Th``.
        (program(value=with_point(1, name=0)), ()),
        (program(value=with_point(1, name="0")), ("optional_key_type",)),
        (program(value=with_point(1, name=1.0)), ("optional_key_type",)),
        (program(value=with_point(1, name=True)), ("optional_key_type",)),
        (program(value=with_point(1, name=None)), ("optional_key_type",)),
        (program(value=with_point(1, note=0)), ("extra_key",)),
        (program(value=with_point(1, name="x", note=0)), ("extra_key",)),
        (program(DAY, [{"t": 0, "Th": 16, "level": 1}]), ("extra_key",)),
        (program(value=with_point(1, t=427)), ("off_grid_minute",)),
        (program(value=with_point(1, Th=4.5)), ("temperature_range",)),
        (program(value=with_point(1, Th=21.3)), ("temperature_grid",)),
        (program(value=with_point(1, t=0)), ("unsorted",)),
        (program(writeable=False), ("not_writable",)),
        (
            replace(program(), unknown_fields=frozenset({"private-note"})),
            ("unknown_fields",),
        ),
        (
            program(value=with_point(1, t=427, Th=21.3, note=0), writeable=False),
            ("not_writable", "extra_key", "off_grid_minute", "temperature_grid"),
        ),
        (
            program(value=with_point(1, t=427, name="0")),
            ("optional_key_type", "off_grid_minute"),
        ),
        (program(value=with_point(1, d="Xx")), ("day_invalid",)),
        (program(DAY, [{"d": "Tu", "t": 0, "Th": 16}]), ("day_mismatch",)),
        (program(value=with_point(1, t="425")), ("minute_invalid",)),
        (program(value=with_point(1, t=1440)), ("minute_invalid",)),
        (program(value=with_point(1, t=...)), ("minute_invalid",)),
        (program(value=with_point(1, Th=31)), ("temperature_invalid",)),
        (program(value=with_point(1, Th=True)), ("temperature_invalid",)),
        (program(value=[*POINTS, "Mo 06:00"]), ("point_not_object",)),
        (
            program(value=[*with_point(0, note=0), "Mo 06:00"]),
            ("extra_key", "point_not_object"),
        ),
        (program(value=POINTS * 200), ("too_many_points",)),
        (program(value={"d": "Mo"}), ("not_a_list",)),
        (program(type="dayProgram"), ("type_unexpected",)),
        (program(used="false"), ("not_available",)),
        (parse_resource({"id": WEEK, "type": "weekProgram"}), ("no_value",)),
        (program("/zones/zn1/clockProgram"), ("not_a_program",)),
    ],
)
def test_each_reason_names_why(resource: Resource, reasons: tuple[str, ...]) -> None:
    assert schedule_issues(resource) == reasons


def _variants() -> list[Resource]:
    """Every single change of a point and their pairs, on a week and a day."""
    changes: list[dict[str, Any]] = [
        {"name": 0},
        {"name": "0"},
        {"note": 0},
        {"t": 427},
        {"t": 0},
        {"t": 1440},
        {"t": "425"},
        {"t": ...},
        {"d": ...},
        {"d": "Xx"},
        {"d": "Tu"},
        {"Th": 4.5},
        {"Th": 21.3},
        {"Th": 31},
        {"Th": "21"},
        {"Th": None},
    ]
    variants = []
    for first, second in itertools.combinations([{}, *changes], 2):
        value = with_point(1, **{**first, **second})
        variants.append(program(value=value))
        variants.append(program(DAY, value[:2]))
        variants.append(program(value=value, writeable=False))
    return variants


@pytest.mark.parametrize("resource", _variants())
def test_the_reasons_agree_with_the_parser(resource: Resource) -> None:
    """R4 guard: a reason exactly when the parser rejects or locks the
    program, and a shape reason exactly when it rejects it."""
    schedule = parse_schedule(resource)
    reasons = schedule_issues(resource)
    assert set(reasons) <= set(SHAPE_ISSUES) | set(EDIT_ISSUES)
    assert (reasons == ()) is (schedule is not None and schedule.editable)
    assert (schedule is None) is any(item in SHAPE_ISSUES for item in reasons)


def test_capability_diagnostics_carry_the_reasons_per_program() -> None:
    day = program(DAY, [{"t": 0, "Th": 16, "name": "private-label"}])
    contract = _capability_diagnostics(day, None, {}, 0)["rrc_contract"]
    assert contract["schedule_shape_supported"] is True
    assert contract["existing_points_editable"] is False
    assert contract["schedule_issues"] == ("optional_key_type",)
    # Only tokens: no key names or values of the points.
    text = json.dumps(contract)
    assert "Th" not in text and "name" not in text and "private" not in text
    week = _capability_diagnostics(program(), None, {}, 0)["rrc_contract"]
    assert week["existing_points_editable"] is True
    assert week["schedule_issues"] == ()


def test_the_summary_counts_programs_and_reasons_per_kind() -> None:
    from datetime import UTC, datetime

    resources = {
        WEEK: program(),
        "/programs/pg4/week": program("/programs/pg4/week", with_point(1, note=0)),
        DAY: program(DAY, [{"t": 0, "Th": 16}]),
        "/programs/pg4/monday": program(
            "/programs/pg4/monday", [{"d": "Mo", "t": 0, "Th": 16}, "Mo 06:00"]
        ),
        "/zones/zn1/clockProgram": program("/zones/zn1/clockProgram"),
    }
    # A current reading takes the place of the stored one.
    current = program(DAY, POINTS[:2])
    coordinator = SimpleNamespace(
        resources=resources,
        data={DAY: ResourceSnapshot(current, True, datetime.now(UTC))},
    )
    summary = _rrc_schedule_summary(coordinator)  # type: ignore[arg-type]
    assert summary == {
        "week": {
            "count": 2,
            "readable": 2,
            "editable": 1,
            "issues": {"extra_key": 1},
            "point_shapes": {
                "dict{Th:float,d:str,note:int,t:int}": 1,
                "dict{Th:float,d:str,t:int}": 1,
                "dict{Th:int,d:str,t:int}": 2,
            },
        },
        "day": {
            "count": 2,
            "readable": 1,
            "editable": 1,
            "issues": {"point_not_object": 1},
            "point_shapes": {
                "dict{Th:float,d:str,t:int}": 1,
                "dict{Th:int,d:str,t:int}": 2,
                "str": 1,
            },
        },
    }
