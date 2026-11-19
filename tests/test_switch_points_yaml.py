"""The action ``set_switch_points`` takes what YAML makes of unquoted values.

Home Assistant reads scripts and automations with YAML 1.1: an unquoted
``off``/``on`` becomes a boolean and an unquoted ``12:00`` the number 720
(hours * 60 + minutes; a time with a leading zero such as ``06:30`` stays
text). The schema maps them back to the level and to the
minute of the day.
"""

from __future__ import annotations

import pytest
from homeassistant.helpers import config_validation as cv
from homeassistant.util.yaml import parse_yaml

from custom_components.bosch_buderus_heating.ha_schema import validation as vol
from custom_components.bosch_buderus_heating.switch_point_action import (
    POINT_SCHEMA,
    SERVICE_SCHEMA,
)

SCHEMA = cv.make_entity_service_schema(SERVICE_SCHEMA)


def test_unquoted_yaml_values_are_the_level_and_the_minute() -> None:
    data = parse_yaml(
        """
entity_id: sensor.hot_water_program
switch_points:
  - {day: mo, time: 06:30, level: on}
  - {day: mo, time: 22:00, level: off}
  - {day: tu, time: "07:00", level: "off"}
"""
    )
    # What YAML hands over, before the schema: a time with a leading zero
    # stays text, 22:00 becomes 1320.
    assert data["switch_points"][0] == {"day": "mo", "time": "06:30", "level": True}
    assert data["switch_points"][1] == {"day": "mo", "time": 1320, "level": False}
    points = SCHEMA(data)["switch_points"]
    assert points == [
        {"day": "mo", "time": 390, "level": "on"},
        {"day": "mo", "time": 1320, "level": "off"},
        {"day": "tu", "time": 420, "level": "off"},
    ]


@pytest.mark.parametrize("time", [0, 1439])
def test_whole_minutes_of_the_day_are_accepted(time) -> None:
    assert POINT_SCHEMA({"day": "we", "time": time, "level": "eco"})["time"] == time


@pytest.mark.parametrize("time", [-1, 1440, 45000, True, 6.5])
def test_other_numbers_are_refused(time) -> None:
    with pytest.raises(vol.Invalid):
        POINT_SCHEMA({"day": "we", "time": time, "level": "eco"})
