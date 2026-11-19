"""Weekly programs written as the manufacturer's app writes them (synthetic).

The air conditioner's program: the configuration only describes (its own
write flag does not matter), a range without a unit is in °C, without a
reported step the points take whole degrees, and a day that follows a day
ending with the unit on starts with an explicit ``off``. The WDDW2 program:
an ``off`` at 00:00 is not written, and the program is changed only while
the unit is on, runs in schedule mode and has a time zone. Every value is
constructed; nothing here confirms behaviour on a real device.
"""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.bosch_buderus_heating.aircon_program_rules import (
    CONFIGURATION,
    rules,
    setting_issue,
)
from custom_components.bosch_buderus_heating.aircon_program_sensor import (
    AirconProgramSensor,
)
from custom_components.bosch_buderus_heating.switch_point_action import (
    async_set_switch_points,
)
from custom_components.bosch_buderus_heating.switch_program_edit import (
    WDDW2_APPLIANCE,
    WDDW2_OPERATION_MODE,
    WDDW2_TIME_ZONE,
    edit_issue,
    editable_program,
    same_week,
    week_issue,
    wire_week,
)
from custom_components.bosch_buderus_heating.switch_program_service import (
    SwitchProgramService,
)
from custom_components.bosch_buderus_heating.switch_programs import (
    SwitchPoint,
    assess_switch_program,
)
from custom_components.bosch_buderus_heating.write_policies import ContractRejected
from tests.aircon_helpers import aircon_cleanup  # noqa: F401
from tests.program_kind_helpers import CountingGateway
from tests.test_aircon_programs import (
    P1,
    P2,
    ProgramUnit,
    call,
    program_coordinator,
    programs,
    readings,
    sensor_of,
)
from tests.test_switch_program_kinds import WDDW2, environment, items  # noqa: F401
from tests.test_switch_program_kinds import readings as program_readings

OFF = "off"


def week(*items):
    days = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
    return tuple(
        SwitchPoint(days.index(day), minute, level) for day, minute, level in items
    )


# The air conditioner's 00:00 off ------------------------------------------------


def test_a_day_after_a_day_that_ends_on_starts_off():
    written = wire_week(P1, week(("Mo", 420, "cool 24.0 auto")))
    # Tuesday (no points) gets an explicit off; the rest of the week stays off.
    assert written == week(("Mo", 420, "cool 24.0 auto"), ("Tu", 0, OFF))


def test_the_week_is_a_ring_sunday_before_monday():
    written = wire_week(P1, week(("Mo", 420, OFF), ("Su", 1200, "heat 21.0")))
    assert written == week(("Mo", 0, OFF), ("Mo", 420, OFF), ("Su", 1200, "heat 21.0"))


def test_no_off_is_added_after_a_day_that_ends_off_or_at_midnight():
    plain = week(("Mo", 420, "cool 24.0"), ("Mo", 1320, OFF), ("Tu", 0, "cool 24.0"))
    written = wire_week(P1, plain)
    # Tuesday starts with its own point at 00:00; it ends on, so Wednesday
    # gets the off.
    assert written == (*plain, SwitchPoint(2, 0, OFF))


def test_other_programs_are_written_as_they_are():
    plain = week(("Mo", 420, "comfort2"))
    assert wire_week("/heatingCircuits/hc1/switchPrograms/A", plain) == plain


def test_the_added_off_counts_against_the_day_limit():
    data = readings()
    program = editable_program(data[P1], data)
    assert program is not None and program.max_switch_points_per_day == 6
    full = week(*(("Tu", 60 * (hour + 1), "cool 24.0") for hour in range(6)))
    sunday = week(("Mo", 420, "cool 24.0"))
    assert week_issue((*sunday, *full), program) is None
    assert week_issue(wire_week(P1, (*sunday, *full)), program) == "too_many_per_day"


async def test_the_action_refuses_a_week_over_the_limit_once_written(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    entity = sensor_of(hass, coordinator)
    points = [{"day": "mo", "time": 420, "level": "cool 24.0"}] + [
        {"day": "tu", "time": 60 * (hour + 1), "level": "cool 24.0"}
        for hour in range(6)
    ]
    with pytest.raises(ServiceValidationError) as err:
        await async_set_switch_points([entity], call(switch_points=points))
    assert err.value.translation_key == "switch_points_too_many_per_day"
    assert unit.object_puts == []


async def test_a_week_that_differs_only_by_the_added_off_is_unchanged(hass):
    """The unit holds the week as written (with the off): the same week
    without it changes nothing and sends nothing."""
    unit = ProgramUnit()
    unit.resources[P1]["switchPoints"] = [
        {
            "dayOfWeek": "Mo",
            "time": 420,
            "parameters": {"operationMode": "cool", "temperatureSetpoint": 24.0},
        },
        {"dayOfWeek": "Tu", "time": 0, "parameters": {"operationMode": "off"}},
    ]
    coordinator, unit, _ = await program_coordinator(hass, unit)
    entity = sensor_of(hass, coordinator)
    response = await async_set_switch_points(
        [entity], call(switch_points=[{"day": "mo", "time": 420, "level": "cool 24.0"}])
    )
    assert response[entity.entity_id]["result"] == "unchanged"
    assert unit.object_puts == []
    assert same_week(
        P1,
        week(("Mo", 420, "cool 24.0")),
        week(("Mo", 420, "cool 24.0"), ("Tu", 0, OFF)),
    )


# The air conditioner's program rules --------------------------------------------


def test_the_configuration_only_describes_its_write_flag_does_not_matter():
    data = programs()
    data[CONFIGURATION]["writeable"] = 0
    parsed = readings(**data)
    assert not isinstance(rules(parsed[P1], parsed), str)
    assert edit_issue(parsed[P1], parsed) is None


def test_the_program_itself_must_be_writable():
    data = programs()
    data[P1]["writeable"] = 0
    parsed = readings(**data)
    assert edit_issue(parsed[P1], parsed) == "not_writable"


def _configuration(**setpoint):
    data = programs()
    configuration = copy.deepcopy(data[CONFIGURATION])
    for entry in configuration["allowedValues"]:
        if entry["operationMode"]["allowedValues"] == ["cool"]:
            entry["temperatureSetpoint"].update(setpoint)
            for key in [k for k, v in setpoint.items() if v is None]:
                del entry["temperatureSetpoint"][key]
    return readings(**{CONFIGURATION: configuration})


def test_a_range_without_a_unit_is_in_celsius():
    parsed = _configuration(unitOfMeasure=None)
    result = rules(parsed[P1], parsed)
    assert not isinstance(result, str) and "cool" in result.modes


def test_a_range_in_another_unit_is_not_used():
    parsed = _configuration(unitOfMeasure="F")
    result = rules(parsed[P1], parsed)
    assert isinstance(result, str) or "cool" not in result.modes


def test_without_a_reported_step_a_point_takes_whole_degrees():
    """As the app's program editor; the target's own step is not borrowed."""
    parsed = _configuration(stepSize=None)
    result = rules(parsed[P1], parsed)
    assert not isinstance(result, str)
    assert result.modes["cool"].step == 1.0
    assert setting_issue("cool 24.0", result) is None
    assert setting_issue("cool 24.5", result) == "value_off_step"


# The air conditioner's program sensor --------------------------------------------


async def test_a_writable_program_sensor_is_enabled_a_read_only_one_not(hass):
    unit = ProgramUnit()
    unit.resources[P2]["writeable"] = 0
    coordinator, _, _ = await program_coordinator(hass, unit)
    assert AirconProgramSensor(coordinator, P1).entity_registry_enabled_default
    assert not AirconProgramSensor(coordinator, P2).entity_registry_enabled_default


# WDDW2 ---------------------------------------------------------------------------


def test_wddw2_off_at_midnight_is_not_written():
    plain = week(("Mo", 0, OFF), ("Mo", 360, "55"), ("Mo", 1320, OFF))
    assert wire_week(WDDW2, plain) == week(("Mo", 360, "55"), ("Mo", 1320, OFF))
    assert same_week(WDDW2, plain, plain[1:])


async def test_wddw2_week_is_sent_without_the_midnight_off(environment):  # noqa: F811
    coordinator, client, _ = environment
    coordinator._switch_program_service = SwitchProgramService(
        client, sleep=AsyncMock()
    )
    gateway = CountingGateway(coordinator, client, items("wddw2"))
    shown = gateway.read(WDDW2)
    points = assess_switch_program(shown).points
    new = (
        SwitchPoint(0, 0, OFF),
        *(p for p in points if p.day != 0 or p.minute != 360),
    )
    new = (*new, SwitchPoint(0, 420, "50"))
    await coordinator.async_write_switch_program(shown, new)
    ((_, body),) = gateway.object_puts
    assert {"dayOfWeek": "Mo", "setpoint": "off", "time": 0} not in body["switchPoints"]
    assert {"dayOfWeek": "Mo", "setpoint": "50", "time": 420} in body["switchPoints"]


async def test_wddw2_week_differing_only_by_the_midnight_off_sends_nothing(
    environment,  # noqa: F811
):
    coordinator, client, _ = environment
    coordinator._switch_program_service = SwitchProgramService(
        client, sleep=AsyncMock()
    )
    gateway = CountingGateway(coordinator, client, items("wddw2"))
    shown = gateway.read(WDDW2)
    points = assess_switch_program(shown).points
    await coordinator.async_write_switch_program(
        shown, (SwitchPoint(0, 0, OFF), *points)
    )
    assert gateway.object_puts == []


@pytest.mark.parametrize(
    ("path", "value", "token"),
    [
        (WDDW2_APPLIANCE, "off", "unit_switched_off"),
        (WDDW2_APPLIANCE, None, "unit_switched_off"),
        (WDDW2_OPERATION_MODE, "manual", "not_schedule_mode"),
        (WDDW2_OPERATION_MODE, None, "not_schedule_mode"),
        (WDDW2_TIME_ZONE, "", "time_zone_undefined"),
        (WDDW2_TIME_ZONE, None, "time_zone_undefined"),
    ],
)
def test_wddw2_is_changed_only_on_in_schedule_mode_with_a_time_zone(path, value, token):
    """As the manufacturer's app: otherwise the program is shown only; a
    missing reading counts as not given."""
    assert (
        edit_issue(program_readings("wddw2")[WDDW2], program_readings("wddw2")) is None
    )
    item = copy.deepcopy(items("wddw2")[path])
    if value is None:
        data = program_readings("wddw2", **{path: None})
    else:
        item["value"] = value
        data = program_readings("wddw2", **{path: item})
    assert edit_issue(data[WDDW2], data) == token
    assert editable_program(data[WDDW2], data) is None


async def test_wddw2_locked_refuses_the_write_unsent(environment):  # noqa: F811
    coordinator, client, _ = environment
    coordinator._switch_program_service = SwitchProgramService(
        client, sleep=AsyncMock()
    )
    payloads = items("wddw2")
    payloads[WDDW2_APPLIANCE]["value"] = "off"
    gateway = CountingGateway(coordinator, client, payloads)
    shown = gateway.read(WDDW2)
    points = assess_switch_program(shown).points
    with pytest.raises(ContractRejected):
        await coordinator.async_write_switch_program(
            shown, (*points, SwitchPoint(6, 600, "45"))
        )
    assert gateway.object_puts == []
