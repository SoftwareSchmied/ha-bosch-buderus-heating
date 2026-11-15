"""TP-07: the data of the read-only weekly view and its browser module."""

from __future__ import annotations

import json
import re
from itertools import pairwise
from pathlib import Path

import pytest

from custom_components.bosch_buderus_heating import holiday_ui
from custom_components.bosch_buderus_heating.schedule_week import (
    ScheduleWeekSelector,
    week_days,
)
from custom_components.bosch_buderus_heating.switch_programs import SwitchPoint

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components/bosch_buderus_heating"
MODULE = COMPONENT / "www/schedule-week.js"
WEEKS = json.loads(
    (ROOT / "tests/fixtures/frontend/schedule-week.json").read_text(encoding="utf-8")
)["weeks"]
DAY_NAMES = {key: key.upper() for key in ("mo", "tu", "we", "th", "fr", "sa", "su")}


def days_of(name: str, **extra):
    week = WEEKS[name]
    return week_days(
        [SwitchPoint(*point) for point in week["points"]],
        labels=week["labels"],
        day_names=DAY_NAMES,
        carry_over=week["carry_over"],
        mark_empty_days=week["mark_empty_days"],
        **extra,
    )


def gapless(day) -> bool:
    segments = day["segments"]
    return (
        segments[0]["start"] == 0
        and segments[-1]["end"] == 1440
        and all(first["end"] == second["start"] for first, second in pairwise(segments))
    )


def test_heating_week_starts_each_day_with_the_previous_level():
    days = days_of("heating_two_points")
    assert len(days) == 7
    for day in days:
        assert gapless(day)
        first = day["segments"][0]
        assert (first["start"], first["end"], first["level"]) == (0, 390, "eco")
        assert first["carried"] is True and first["point"] is False
        assert [segment["level"] for segment in day["segments"]] == [
            "eco",
            "comfort2",
            "eco",
        ]
        assert day["segments"][1]["label"] == "Heizen"


def test_six_points_keep_every_narrow_segment():
    monday = days_of("dhw_six_points")[0]
    assert gapless(monday)
    assert sum(segment["point"] for segment in monday["segments"]) == 6
    assert (
        min(segment["end"] - segment["start"] for segment in monday["segments"]) == 15
    )


def test_rrc_day_without_points_is_marked_not_off():
    days = days_of("rrc_empty_day")
    tuesday = days[1]
    assert tuesday["empty"] is True
    assert tuesday["segments"] == [
        {
            "start": 0,
            "end": 1440,
            "level": None,
            "label": None,
            "point": False,
            "carried": False,
            "known": True,
        }
    ]


def test_unknown_level_is_flagged_and_keeps_its_text():
    (segment, *_rest) = [
        item for item in days_of("unknown_level")[0]["segments"] if item["point"]
    ]
    assert segment["known"] is False
    assert segment["label"] == "turbo"


def test_kind_without_carry_over_has_no_level_before_the_first_point():
    days = week_days(
        [SwitchPoint(0, 360, "55")],
        labels={},
        day_names=DAY_NAMES,
        carry_over=False,
    )
    assert days[0]["segments"][0]["level"] is None
    assert days[1]["empty"] is True and days[1]["segments"][0]["level"] is None


def test_changed_days_are_marked():
    days = days_of("heating_two_points", changed=frozenset({2}))
    assert [day["changed"] for day in days] == [
        False,
        False,
        True,
        False,
        False,
        False,
        False,
    ]


def test_view_does_not_depend_on_the_time_zone(monkeypatch):
    before = days_of("heating_two_points")
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    assert days_of("heating_two_points") == before


def test_selector_sends_only_view_data():
    days = days_of("script_name")
    serialized = ScheduleWeekSelector(days).serialize()
    config = serialized["selector"]["bosch-buderus-schedule-week"]
    assert set(config) == {"days", "title", "texts", "aliases"}
    text = json.dumps(config)
    assert "/heatingCircuits" not in text and "gateway" not in text
    assert set(config["texts"]) >= {"en", "de"}


def test_every_language_has_the_same_view_texts():
    files = sorted((COMPONENT / "i18n").glob("*.json"))
    assert len(files) == 19
    keys = None
    for path in files:
        texts = json.loads(path.read_text(encoding="utf-8"))["dialogs"]["schedule"]
        placeholders = re.findall(r"\{(\w+)\}", texts["segment"])
        assert sorted(placeholders) == ["day", "end", "level", "start"], path
        if keys is None:
            keys = set(texts)
        assert set(texts) == keys, path


def test_module_writes_device_texts_only_as_text():
    source = MODULE.read_text(encoding="utf-8")
    assert len(source.encode()) < 15_000
    assert "fetch(" not in source and "XMLHttpRequest" not in source
    assert "innerHTML" not in source and "insertAdjacentHTML" not in source
    assert "textContent" in source and "aria-label" in source
    assert 'scope = "row"' in source and 'scope = "col"' in source
    assert "prefers-reduced-motion" in source
    assert "ha-selector-bosch-buderus-schedule-week" in source


async def test_module_is_registered_like_the_other_dialog_modules(hass):
    hass.config.components.add("frontend")
    calls = []

    async def register(_hass, key, name):
        calls.append((key, name))

    original = holiday_ui._async_register_module
    holiday_ui._async_register_module = register
    try:
        await holiday_ui.async_setup_schedule_ui(hass)
    finally:
        holiday_ui._async_register_module = original
    assert calls == [("bosch_buderus_heating.schedule_ui", "schedule-week")]


@pytest.mark.parametrize("step", ["switch_program_week", "rrc_schedule"])
def test_week_steps_label_the_view(step):
    for path in [
        COMPONENT / "strings.json",
        *(COMPONENT / "translations").glob("*.json"),
    ]:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["options"]["step"][step]["data"]["week_view"], path
