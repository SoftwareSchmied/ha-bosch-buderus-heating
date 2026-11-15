"""Icon translations (``icons.json``) per translation key and state.

An entity that sets no icon of its own takes the icon of its translation key,
and per state where one is listed. These checks keep ``icons.json`` in line
with ``strings.json`` and with the entities that left their code icon out.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from custom_components.bosch_buderus_heating import (
    binary_sensor,
    calendar,
    derived_defrost,
    event,
    flow_range,
    heat_generator_sensor,
    heating_climate,
    pointt_water_heater,
    rrc_event_calendar,
    rrc_zone_entities,
    sensor,
)
from custom_components.bosch_buderus_heating.icon_translations import (
    TRANSLATED_SELECT_ICONS,
)
from custom_components.bosch_buderus_heating.select import build_select_descriptions
from tests.test_heating_controls import MODE, scalar

PACKAGE = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
ICONS = json.loads((PACKAGE / "icons.json").read_text("utf-8"))
STRINGS = json.loads((PACKAGE / "strings.json").read_text("utf-8"))
ICON = re.compile(r"^mdi:[a-z0-9]+(?:-[a-z0-9]+)*$")


def test_icons_follow_the_home_assistant_shape():
    assert set(ICONS) == {"entity", "services"}
    for name, entry in ICONS["services"].items():
        assert set(entry) == {"service"}, name
        assert ICON.fullmatch(entry["service"]), name
    for platform, keys in ICONS["entity"].items():
        for key, entry in keys.items():
            assert set(entry) <= {"default", "state"}, (platform, key)
            assert ICON.fullmatch(entry["default"]), (platform, key)
            for state, icon in entry.get("state", {}).items():
                assert ICON.fullmatch(icon), (platform, key, state)


def test_every_icon_belongs_to_a_translated_entity_and_state():
    entities = STRINGS["entity"]
    for platform, keys in ICONS["entity"].items():
        for key, entry in keys.items():
            assert key in entities[platform], (platform, key)
            states = entry.get("state", {})
            if platform == "binary_sensor":
                assert set(states) <= {"on", "off"}, key
            elif states:
                assert set(states) <= set(entities[platform][key]["state"]), key


@pytest.mark.parametrize(
    ("entity_class", "platform", "key"),
    [
        (
            heating_climate.HeatingCircuitTemporaryClearButton,
            "button",
            "heating_temporary_setpoint_clear",
        ),
        (
            binary_sensor.BoschBuderusHolidayActiveBinarySensor,
            "binary_sensor",
            "holiday_active",
        ),
        (
            binary_sensor.BoschBuderusSystemFaultBinarySensor,
            "binary_sensor",
            "system_fault",
        ),
        (calendar.BoschBuderusHolidayCalendar, "calendar", "holiday_periods"),
        (rrc_event_calendar.RrcEventCalendar, "calendar", "rrc_events"),
        (event.BoschBuderusNotificationEvent, "event", "system_notifications"),
        (
            heat_generator_sensor.BoschBuderusHeatGeneratorSensor,
            "sensor",
            "heat_generator",
        ),
        (pointt_water_heater.PointTWaterHeater, "water_heater", "pointt_dhw"),
        (sensor.BoschBuderusNextHolidaySensor, "sensor", "next_holiday"),
        (sensor.BoschBuderusActiveFaultsSensor, "sensor", "active_faults"),
        (
            sensor.BoschBuderusActiveNotificationsSensor,
            "sensor",
            "active_notifications",
        ),
    ],
)
def test_entities_without_a_code_icon_have_a_translated_one(
    entity_class, platform, key
):
    assert not _sets_code_icon(entity_class)
    assert key in ICONS["entity"][platform]


@pytest.mark.parametrize(
    ("entity_class", "platform", "keys"),
    [
        (
            derived_defrost.DefrostActiveBinarySensor,
            "binary_sensor",
            ("calculated_defrost_active",),
        ),
        (derived_defrost.DefrostEvent, "event", ("calculated_defrost",)),
        (derived_defrost.LastDefrostSensor, "sensor", ("calculated_last_defrost",)),
        (
            flow_range.BoschBuderusFlowRangeBinarySensor,
            "binary_sensor",
            tuple(flow.key for flow in flow_range.FLOW_RANGES),
        ),
    ],
)
def test_the_defrost_and_flow_entities_have_translated_icons(
    entity_class, platform, keys
):
    assert not _sets_code_icon(entity_class)
    for key in keys:
        assert key in ICONS["entity"][platform], key


def _sets_code_icon(entity_class: type) -> bool:
    return any(
        "_attr_icon" in vars(item)
        for item in entity_class.__mro__
        if item.__module__.startswith("custom_components.")
    )


def test_the_code_icon_check_sees_a_code_icon():
    from custom_components.bosch_buderus_heating.rrc_device_entities import (
        RadioSignalSensor,
    )

    assert _sets_code_icon(RadioSignalSensor)


@pytest.mark.parametrize(
    ("entity_class", "key"),
    [
        (rrc_zone_entities.RrcOverrideClearButton, "rrc_override_clear"),
        (rrc_zone_entities.RrcAdvanceSwitchPointButton, "rrc_override_advance"),
    ],
)
def test_the_zone_buttons_take_their_icons_from_icons_json(entity_class, key):
    assert not _sets_code_icon(entity_class)
    assert key in ICONS["entity"]["button"]


def test_generic_selects_leave_their_icon_to_icons_json():
    assert set(ICONS["entity"]["select"]) >= TRANSLATED_SELECT_ICONS
    mode = scalar(MODE, "auto", options=("off", "manual", "auto"))
    (description,) = build_select_descriptions({MODE: mode})
    assert description.translation_key == "heating_circuit_operation_mode"
    assert description.icon is None


@pytest.mark.parametrize(
    "module",
    [
        "derived_activity.py",
        "derived_defrost.py",
        "derived_sensors.py",
        "flow_range.py",
        "outdoor_unit_software.py",
        "recordings/sensor.py",
    ],
)
def test_newer_entities_set_no_code_icon(module):
    """Entities with a translation key take a fixed icon from icons.json."""
    assert "_attr_icon" not in (PACKAGE / module).read_text("utf-8")


def test_calculated_activity_and_defrost_count_icons():
    sensors = ICONS["entity"]["sensor"]
    assert set(sensors["calculated_activity"]["state"]) == set(
        STRINGS["entity"]["sensor"]["calculated_activity"]["state"]
    )
    assert sensors["calculated_defrost_count"]["default"] == "mdi:snowflake-melt"
