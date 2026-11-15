"""Switch points of an air conditioner's weekly programs (PointT ``rac``).

Synthetic data in the form of the vendor app (``pointt-rac/switch-programs``):
reading, the rules from the program configuration, the single write with
read-back, the action ``set_switch_points``, the program sensor and the
options dialog. Nothing here confirms behaviour on a real unit.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating import aircon
from custom_components.bosch_buderus_heating.aircon_program_rules import (
    CONFIGURATION,
    canonical_setting,
    describe,
    parse_setting,
    rules,
    setting_issue,
)
from custom_components.bosch_buderus_heating.aircon_program_sensor import (
    STATES,
    AirconProgramSensor,
    _programs,
)
from custom_components.bosch_buderus_heating.config_flow import BoschBuderusOptionsFlow
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    RequestTimeout,
    UnexpectedHttpStatus,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from custom_components.bosch_buderus_heating.switch_point_action import (
    async_set_switch_points,
)
from custom_components.bosch_buderus_heating.switch_program_edit import (
    edit_issue,
    editable_program,
    encode_body,
    week_issue,
)
from custom_components.bosch_buderus_heating.switch_program_service import (
    SwitchProgramChanged,
)
from custom_components.bosch_buderus_heating.switch_programs import (
    SwitchPoint,
    assess_switch_program,
    program_kind,
)
from custom_components.bosch_buderus_heating.writes import WriteBusy
from tests.aircon_helpers import (  # noqa: F401
    FakeUnit,
    Sleeps,
    aircon_cleanup,
    make_coordinator,
)
from tests.helpers.options_menu import outcome

FIXTURE = Path(__file__).parent / "fixtures/contracts/pointt-rac/switch-programs.json"
P1 = "/airConditioning/switchPrograms/program1"
P2 = "/airConditioning/switchPrograms/program2"


def programs():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["resources"]


def readings(**overrides):
    data = programs()
    data.update(overrides)
    return {
        path: parse_resource(payload, path=path)
        for path, payload in data.items()
        if payload is not None
    }


class ProgramUnit(FakeUnit):
    """The unit with programs and their configuration; records object PUTs."""

    def __init__(self):
        super().__init__()
        self.resources.update(copy.deepcopy(programs()))
        self.object_puts = []
        self.object_put_error = None

    async def put_resource_object(self, gateway_id, path, body):
        self.object_puts.append((path, copy.deepcopy(body)))
        if self.object_put_error is not None:
            error, applied = self.object_put_error
            if applied:
                self.resources[path]["switchPoints"] = body["switchPoints"]
            raise error
        if self.apply_puts:
            self.resources[path]["switchPoints"] = body["switchPoints"]


async def program_coordinator(hass, unit=None):
    unit = unit or ProgramUnit()
    coordinator, unit, _ = await make_coordinator(hass, unit)
    sleeps = Sleeps()
    coordinator._program_service.sleep = sleeps
    # The second cycle reads the offered programs along.
    await coordinator.async_refresh()
    return coordinator, unit, sleeps


def points(*items):
    days = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
    return tuple(
        SwitchPoint(days.index(day), minute, level) for day, minute, level in items
    )


# Settings as text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cool 24 auto", "cool 24.0 auto"),
        ("cool 24,5 auto", "cool 24.5 auto"),
        ("auto auto", "auto auto"),
        ("fanOnly low", "fanOnly low"),
        ("off", "off"),
        ("heat low 21", "heat 21.0 low"),
        ("cool 24.333 auto", "cool 24.333 auto"),
        ("off 20", None),
        ("cool 24 25", None),
        ("cool auto low", None),
        ("low cool", None),
        ("internalCleaning", None),
        ("cool 24 auto extra", None),
        ("", None),
        (24, None),
    ],
)
def test_setting_text(text, expected):
    assert canonical_setting(text) == expected


def test_parse_setting_keeps_the_parts():
    setting = parse_setting("cool 24.0 auto")
    assert setting is not None
    assert (setting.mode, setting.temperature, setting.fan) == ("cool", 24.0, "auto")


# Reading


def test_program_is_read_with_its_settings():
    assessment = assess_switch_program(readings()[P1])
    assert assessment.supported
    assert assessment.kind == "aircon"
    assert assessment.value_type == "aircon"
    assert not assessment.carry_over
    assert [point.level for point in assessment.points] == [
        "cool 24.0 auto",
        "off",
        "fanOnly low",
    ]
    # The raster the app reads as a float.
    assert assessment.time_raster == 15
    assert program_kind(P1) is not None and program_kind(P1).body == "object"


def test_without_carry_over_a_day_starts_without_a_setting():
    assessment = assess_switch_program(readings()[P1])
    assert assessment.active_level(1, 0) is None
    assert assessment.active_level(0, 600) == "cool 24.0 auto"
    assert assessment.active_level(0, 1330) == "off"


def test_another_type_is_read_with_a_note():
    payload = programs()[P2]
    payload["type"] = "racSwitchProgram"
    assessment = assess_switch_program(parse_resource(payload, path=P2))
    assert assessment.supported
    assert "program_type_other" in assessment.notes
    assert "program_limits_missing" in assessment.notes


@pytest.mark.parametrize(
    "point",
    [
        {
            "dayOfWeek": "Mo",
            "time": 60,
            "parameters": {"operationMode": "internalCleaning"},
        },
        {
            "dayOfWeek": "Mo",
            "time": 60,
            "parameters": {"operationMode": "cool", "temperatureSetpoint": "24"},
        },
        {
            "dayOfWeek": "Mo",
            "time": 60,
            "parameters": {"operationMode": "cool", "fanSpeed": "max"},
        },
        {
            "dayOfWeek": "Mo",
            "time": 60,
            "parameters": {"operationMode": "off", "fanSpeed": "low"},
        },
        {
            "dayOfWeek": "Mo",
            "time": 60,
            "parameters": {"operationMode": "cool", "swing": "on"},
        },
        {"dayOfWeek": "Mo", "time": 60, "setpoint": "cool"},
        {
            "dayOfWeek": "Mo",
            "time": 60,
            "parameters": {"operationMode": "off"},
            "name": "Morning",
        },
        {"dayOfWeek": "Mo", "time": 1440, "parameters": {"operationMode": "off"}},
        {"dayOfWeek": "Mo", "time": 70, "parameters": {"operationMode": "off"}},
        {"dayOfWeek": "Xx", "time": 60, "parameters": {"operationMode": "off"}},
    ],
)
def test_a_malformed_point_hides_the_program(point):
    payload = programs()[P1]
    payload["switchPoints"].append(point)
    assert not assess_switch_program(parse_resource(payload, path=P1)).supported


def test_configuration_with_mode_objects_is_parsed():
    configuration = readings()[CONFIGURATION]
    assert configuration.metadata.writable
    assert configuration.metadata.allowed_values == ()
    assert isinstance(configuration.program_fields["allowedValues"], list)
    assert not configuration.unknown_fields


def test_heating_paths_keep_refusing_object_allowed_values():
    payload = {
        "id": "/heatingCircuits/hc1/operationMode",
        "type": "stringValue",
        "allowedValues": [{"x": 1}],
    }
    with pytest.raises(Exception, match="scalar"):
        parse_resource(payload, path="/heatingCircuits/hc1/operationMode")


# Rules from the configuration


def test_rules_come_from_the_configuration():
    result = rules(readings()[P1], readings())
    assert not isinstance(result, str)
    assert result.offered == ("cool", "heat", "dry", "fanOnly", "off")
    assert result.modes["cool"].fans == ("auto", "low", "mid", "high")
    assert result.modes["fanOnly"].minimum is None
    # Dry reports no step: the target temperature's step, else whole degrees.
    assert result.modes["dry"].step == 1.0
    assert (result.max_switch_points, result.max_switch_points_per_day) == (42, 6)
    assert result.time_raster == 15
    assert "internalCleaning" not in describe(result)


def test_limits_of_the_configuration_count_where_the_program_has_none():
    result = rules(readings()[P2], readings())
    assert not isinstance(result, str)
    assert result.max_switch_points_per_day == 6


@pytest.mark.parametrize(
    ("change", "token"),
    [
        ({CONFIGURATION: None}, "program_configuration_missing"),
        ("not_writable", "configuration_not_writable"),
        ("no_limits", "program_limits_missing"),
        ("only_cleaning", "program_modes_unknown"),
        ("odd_raster", "program_limits_missing"),
    ],
)
def test_without_usable_configuration_there_are_no_rules(change, token):
    data = programs()
    configuration = data[CONFIGURATION]
    if change == "not_writable":
        configuration["writeable"] = 0
    elif change == "no_limits":
        for key in (
            "maxNbOfSwitchPoints",
            "maxNbOfSwitchPointsPerDay",
            "switchPointTimeRaster",
        ):
            configuration.pop(key)
    elif change == "only_cleaning":
        configuration["allowedValues"] = configuration["allowedValues"][-1:]
    elif change == "odd_raster":
        configuration["switchPointTimeRaster"] = 7
    elif isinstance(change, dict):
        data.update(change)
    parsed = {p: parse_resource(v, path=p) for p, v in data.items() if v is not None}
    assert rules(parsed[P2], parsed) == token
    assert edit_issue(parsed[P2], parsed) == token
    assert editable_program(parsed[P2], parsed) is None


def test_a_range_in_fahrenheit_drops_the_mode():
    data = programs()
    data[CONFIGURATION]["allowedValues"][0]["temperatureSetpoint"]["unitOfMeasure"] = (
        "F"
    )
    parsed = {p: parse_resource(v, path=p) for p, v in data.items()}
    result = rules(parsed[P1], parsed)
    assert not isinstance(result, str)
    assert "cool" not in result.modes
    # Program 1 uses cool: shown, not changed.
    assert edit_issue(parsed[P1], parsed) == "point_out_of_range"


def test_an_implausible_range_is_narrowed():
    data = programs()
    data[CONFIGURATION]["allowedValues"][1]["temperatureSetpoint"]["minValue"] = 0.0
    parsed = {p: parse_resource(v, path=p) for p, v in data.items()}
    result = rules(parsed[P1], parsed)
    assert not isinstance(result, str)
    assert result.modes["heat"].minimum == 10.0


@pytest.mark.parametrize(
    ("text", "issue"),
    [
        ("cool 24.0 auto", None),
        ("cool 24.5 high", None),
        ("off", None),
        ("fanOnly low", None),
        ("dry 20.0", None),
        ("cool 24.0", None),
        ("cool 24.2 auto", "value_off_step"),
        ("cool 31.0 auto", "value_out_of_range"),
        ("cool auto", "setting_not_allowed"),
        ("fanOnly 20.0 low", "setting_not_allowed"),
        ("dry 20.0 low", "setting_not_allowed"),
        ("cool 24.0 turbo", "setting_not_allowed"),
        ("auto 24.0", "setting_not_allowed"),
        ("cool 24 auto", "setting_not_allowed"),
    ],
)
def test_setting_rules(text, issue):
    result = rules(readings()[P1], readings())
    assert not isinstance(result, str)
    assert setting_issue(text, result) == issue


def test_week_rules_of_the_editable_program():
    program = editable_program(readings()[P1], readings())
    assert program is not None and program.aircon is not None
    assert not program.numeric
    assert week_issue(points(("Mo", 420, "cool 24.0 auto")), program) is None
    assert week_issue(points(("Mo", 425, "off")), program) == "off_raster"
    assert week_issue(points(("Mo", 420, "cool 24.2 auto")), program) == (
        "level_not_allowed"
    )
    six = points(*(("We", 60 * hour, "off") for hour in range(7)))
    assert week_issue(six, program) == "too_many_per_day"
    assert week_issue((), program) == "empty_week"


def test_point_off_the_configuration_raster_is_shown_only():
    data = programs()
    data[P2]["switchPoints"].append(
        {"dayOfWeek": "Su", "time": 70, "parameters": {"operationMode": "off"}}
    )
    parsed = {p: parse_resource(v, path=p) for p, v in data.items()}
    assert assess_switch_program(parsed[P2]).supported
    assert edit_issue(parsed[P2], parsed) == "point_out_of_range"


def test_not_writable_program_is_shown_only():
    data = programs()
    data[P1]["writeable"] = 0
    parsed = {p: parse_resource(v, path=p) for p, v in data.items()}
    assert edit_issue(parsed[P1], parsed) == "not_writable"


def test_program_over_its_limit_is_shown_only():
    data = programs()
    data[P1]["maxNbOfSwitchPointsPerDay"] = 1
    data[P1]["switchPoints"].append(
        {"dayOfWeek": "Mo", "time": 600, "parameters": {"operationMode": "off"}}
    )
    parsed = {p: parse_resource(v, path=p) for p, v in data.items()}
    assert edit_issue(parsed[P1], parsed) == "program_over_limit"


def test_body_is_an_object_with_parameters():
    program = editable_program(readings()[P1], readings())
    assert program is not None
    body = encode_body(
        points(
            ("Tu", 480, "fanOnly low"),
            ("Mo", 420, "cool 24.5 auto"),
            ("Mo", 1320, "off"),
        ),
        program,
    )
    assert body == {
        "switchPoints": [
            {
                "dayOfWeek": "Mo",
                "time": 420,
                "parameters": {
                    "operationMode": "cool",
                    "temperatureSetpoint": 24.5,
                    "fanSpeed": "auto",
                },
            },
            {"dayOfWeek": "Mo", "time": 1320, "parameters": {"operationMode": "off"}},
            {
                "dayOfWeek": "Tu",
                "time": 480,
                "parameters": {"operationMode": "fanOnly", "fanSpeed": "low"},
            },
        ]
    }


# Polling and the single write


async def test_offered_programs_are_read_in_the_cycle_bulk(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    assert P1 in unit.bulk_calls[-1] and P2 in unit.bulk_calls[-1]
    assert len(unit.bulk_calls) == 3  # detection, first cycle, second cycle
    assert coordinator.program_paths() == (P1, P2)
    assert coordinator.program_active(P1) is True
    assert coordinator.program_active(P2) is False
    report = coordinator.diagnostics()
    assert report["programs_read"] == 2
    assert report["program_edit_issues"] == ["editable"]
    assert "program1" not in json.dumps(report)


async def test_dialog_write_sends_the_week_once_and_reads_it_back(hass):
    coordinator, unit, sleeps = await program_coordinator(hass)
    shown = coordinator.resource(P1)
    week = points(
        ("Mo", 420, "cool 23.5 high"), ("Mo", 1320, "off"), ("Tu", 480, "fanOnly low")
    )
    bulk_before = len(unit.bulk_calls)
    confirmed = await coordinator.async_write_switch_program(shown, week)
    assert [path for path, _ in unit.object_puts] == [P1]
    ((_, body),) = unit.object_puts
    assert body["switchPoints"][0]["parameters"] == {
        "operationMode": "cool",
        "temperatureSetpoint": 23.5,
        "fanSpeed": "high",
    }
    # Fresh program and configuration in one bulk, never retried.
    assert unit.bulk_calls[bulk_before] == (P1, CONFIGURATION)
    assert unit.bulk_kwargs[bulk_before] == {"retryable": False}
    # Read once more right before the PUT, then read back after 1 s.
    assert unit.gets == [P1, P1]
    assert sleeps.delays == [1]
    assert assess_switch_program(confirmed).points == week
    assert unit.puts == []
    assert coordinator.program_writes == {"confirmed": 1, "sent": 1}


async def test_program_changed_since_shown_sends_nothing(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    shown = coordinator.resource(P1)
    unit.resources[P1]["switchPoints"][0]["parameters"]["temperatureSetpoint"] = 25.0
    with pytest.raises(SwitchProgramChanged):
        await coordinator.async_write_switch_program(
            shown, points(("Mo", 420, "cool 23.5 high"))
        )
    assert unit.object_puts == []
    assert coordinator.program_writes == {"refused": 1}


async def test_dialog_older_than_the_last_reading_sends_no_request(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    shown = coordinator.resource(P1)
    unit.resources[P1]["switchPoints"].pop()
    await coordinator.async_refresh()
    requests = (len(unit.bulk_calls), len(unit.gets))
    with pytest.raises(SwitchProgramChanged):
        await coordinator.async_write_switch_program(
            shown, points(("Mo", 420, "cool 23.5 high"))
        )
    assert (len(unit.bulk_calls), len(unit.gets)) == requests
    assert unit.object_puts == []


async def test_unreadable_configuration_sends_nothing(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    shown = coordinator.resource(P1)
    unit.status[CONFIGURATION] = 404
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_switch_program(
            shown, points(("Mo", 420, "cool 23.5 high"))
        )
    assert unit.object_puts == []


async def test_setting_outside_the_configuration_sends_nothing(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    shown = coordinator.resource(P1)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_switch_program(
            shown, points(("Mo", 420, "cool 35.0 high"))
        )
    assert unit.object_puts == []


async def test_refused_put_is_not_repeated(hass):
    coordinator, unit, sleeps = await program_coordinator(hass)
    unit.object_put_error = (UnexpectedHttpStatus(400), False)
    with pytest.raises(UnexpectedHttpStatus):
        await coordinator.async_write_switch_program(
            coordinator.resource(P1), points(("Mo", 420, "cool 23.5 high"))
        )
    assert len(unit.object_puts) == 1
    assert sleeps.delays == []
    assert coordinator.program_writes == {"failed": 1, "sent": 1}


async def test_lost_answer_is_resolved_by_reading(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    unit.object_put_error = (RequestTimeout("lost"), True)
    week = points(("Mo", 420, "cool 23.5 high"))
    confirmed = await coordinator.async_write_switch_program(
        coordinator.resource(P1), week
    )
    assert len(unit.object_puts) == 1
    assert assess_switch_program(confirmed).points == week


async def test_unconfirmed_week_is_not_sent_again(hass):
    coordinator, unit, sleeps = await program_coordinator(hass)
    unit.apply_puts = False
    with pytest.raises(WriteNotConfirmed):
        await coordinator.async_write_switch_program(
            coordinator.resource(P1), points(("Mo", 420, "cool 23.5 high"))
        )
    assert len(unit.object_puts) == 1
    assert sleeps.delays == [1, 2, 4, 8]


async def test_rate_limited_fresh_read_sends_nothing(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    unit.status[P1] = 429
    with pytest.raises(RateLimited):
        await coordinator.async_write_switch_program(
            coordinator.resource(P1), points(("Mo", 420, "cool 23.5 high"))
        )
    assert unit.object_puts == []


async def test_too_short_window_sends_no_program(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    with (
        patch.object(aircon, "PROGRAM_WRITE_DEADLINE_SECONDS", 10.0),
        pytest.raises(WriteBusy),
    ):
        await coordinator.async_write_switch_program(
            coordinator.resource(P1), points(("Mo", 420, "cool 23.5 high"))
        )
    assert unit.object_puts == []


async def test_heating_paths_are_refused_by_the_unit(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    with pytest.raises(WriteValidationError):
        await coordinator.async_plan_switch_program(
            "/heatingCircuits/hc1/switchPrograms/A", lambda *_: ()
        )
    assert unit.object_puts == [] and unit.bulk_calls[-1] != (
        "/heatingCircuits/hc1/switchPrograms/A",
    )


# The action


def sensor_of(hass, coordinator, path=P1):
    entity = AirconProgramSensor(coordinator, path)
    entity.hass = hass
    entity.entity_id = f"sensor.ac_program_{path[-1]}"
    return entity


def call(**data):
    return SimpleNamespace(
        data={"mode": "replace_week", "validate_only": False, **data}
    )


async def test_action_replaces_the_week(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    entity = sensor_of(hass, coordinator)
    response = await async_set_switch_points(
        [entity],
        call(
            switch_points=[
                {"day": "mo", "time": 420, "level": "cool 23 high"},
                {"day": "fr", "time": 1080, "level": "off"},
            ]
        ),
    )
    result = response[entity.entity_id]
    assert result["result"] == "sent" and result["confirmed"] is True
    assert result["switch_points"] == {
        "mo": [{"time": "07:00", "level": "cool 23.0 high"}],
        "fr": [{"time": "18:00", "level": "off"}],
    }
    assert result["previous"]["tu"] == [{"time": "08:00", "level": "fanOnly low"}]
    assert len(unit.object_puts) == 1


async def test_action_replaces_days_only(hass):
    coordinator, _unit, _ = await program_coordinator(hass)
    entity = sensor_of(hass, coordinator)
    response = await async_set_switch_points(
        [entity],
        call(
            mode="replace_days",
            switch_points=[{"day": "tu", "time": 540, "level": "heat 21.5 mid"}],
        ),
    )
    days = response[entity.entity_id]["switch_points"]
    assert days["tu"] == [{"time": "09:00", "level": "heat 21.5 mid"}]
    assert len(days["mo"]) == 2


async def test_action_refuses_a_setting_with_the_allowed_ones(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    entity = sensor_of(hass, coordinator)
    with pytest.raises(ServiceValidationError) as err:
        await async_set_switch_points(
            [entity],
            call(switch_points=[{"day": "mo", "time": 420, "level": "cool 35 high"}]),
        )
    assert err.value.translation_key == "switch_points_aircon_setting_not_allowed"
    assert "cool 16-30/0.5" in err.value.translation_placeholders["settings"]
    with pytest.raises(ServiceValidationError) as err:
        await async_set_switch_points(
            [entity],
            call(switch_points=[{"day": "mo", "time": 420, "level": "warm"}]),
        )
    assert err.value.translation_key == "switch_points_aircon_setting_not_allowed"
    assert unit.object_puts == []


async def test_action_validate_only_and_expected(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    entity = sensor_of(hass, coordinator)
    response = await async_set_switch_points(
        [entity],
        call(
            validate_only=True,
            switch_points=[{"day": "mo", "time": 420, "level": "cool 23 high"}],
        ),
    )
    assert response[entity.entity_id]["result"] == "validated"
    with pytest.raises(ServiceValidationError) as err:
        await async_set_switch_points(
            [entity],
            call(
                expected=[{"day": "mo", "time": 420, "level": "cool 25 auto"}],
                switch_points=[{"day": "mo", "time": 420, "level": "cool 23 high"}],
            ),
        )
    assert err.value.translation_key == "switch_points_program_changed"
    assert unit.object_puts == []


async def test_action_on_a_program_without_configuration(hass):
    unit = ProgramUnit()
    unit.resources[CONFIGURATION]["writeable"] = 0
    coordinator, unit, _ = await program_coordinator(hass, unit)
    entity = sensor_of(hass, coordinator)
    with pytest.raises(ServiceValidationError) as err:
        await async_set_switch_points(
            [entity],
            call(switch_points=[{"day": "mo", "time": 420, "level": "off"}]),
        )
    assert err.value.translation_key == "switch_points_not_editable"
    assert unit.object_puts == []


async def test_action_reports_a_cloud_pause(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    unit.backoff.remaining = 30.0
    entity = sensor_of(hass, coordinator)
    with pytest.raises(HomeAssistantError):
        await async_set_switch_points(
            [entity],
            call(switch_points=[{"day": "mo", "time": 420, "level": "off"}]),
        )
    assert unit.object_puts == [] and unit.bulk_calls[-1] != (P1, CONFIGURATION)


# The program sensor


async def test_program_sensor(hass):
    coordinator, _, _ = await program_coordinator(hass)
    assert [key for key, _ in _programs(coordinator)] == [P1, P2]
    entity = sensor_of(hass, coordinator)
    assert entity.unique_id == "rac-gateway-A:airConditioning:switchPrograms:program1"
    assert entity.translation_key == "aircon_switch_program"
    assert entity.translation_placeholders == {"number": "1"}
    assert not entity.entity_registry_enabled_default
    assert entity.options == list(STATES)
    assert "fan_only" in STATES and "fanOnly" not in STATES
    assert entity.entity_description.value_kind == "switch_program"
    assert entity.available
    attributes = entity.extra_state_attributes
    assert attributes["switch_points"]["Mo"] == ["07:00 cool 24.0 auto", "22:00 off"]
    assert attributes["active"] is True and attributes["editable"] is True
    monday_noon = SimpleNamespace(weekday=lambda: 0, hour=12, minute=0)
    with patch(
        "custom_components.bosch_buderus_heating.aircon_program_sensor.dt_util.now",
        return_value=monday_noon,
    ):
        assert entity.native_value == "cool"
        assert entity.extra_state_attributes["temperature"] == 24.0
    tuesday_early = SimpleNamespace(weekday=lambda: 1, hour=5, minute=0)
    with patch(
        "custom_components.bosch_buderus_heating.aircon_program_sensor.dt_util.now",
        return_value=tuesday_early,
    ):
        # Before the first point of a day the unit is off.
        assert entity.native_value == "off"


async def test_unreadable_program_has_no_sensor(hass):
    unit = ProgramUnit()
    unit.resources[P2]["switchPoints"][0]["parameters"]["operationMode"] = "warm"
    coordinator, _, _ = await program_coordinator(hass, unit)
    assert [key for key, _ in _programs(coordinator)] == [P1]


# The options dialog


def flow_of(hass, coordinator):
    entry = coordinator.config_entry
    entry.runtime_data = BoschBuderusRuntimeData(
        client=coordinator.client,
        token_manager=AsyncMock(),
        gateways=(),
        coordinators=(),
        aircons=(coordinator,),
    )
    flow = BoschBuderusOptionsFlow(entry)
    flow.hass = hass
    return flow


async def test_dialog_changes_a_point_and_sends_once(hass):
    coordinator, unit, _ = await program_coordinator(hass)
    flow = flow_of(hass, coordinator)
    first = await flow.async_step_switch_programs()
    options = first["data_schema"].schema["program"].config["options"]
    keys = [item["value"] for item in options]
    assert any(key.endswith(P1) for key in keys)
    assert any(key.endswith(P2) for key in keys)
    week = await flow.async_step_switch_programs(
        {"program": next(key for key in keys if key.endswith(P1))}
    )
    assert week["step_id"] == "switch_program_week_aircon"
    await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    point = await flow.async_step_switch_program_day({"point": "0"})
    assert point["step_id"] == "switch_program_point_aircon"
    fields = {str(key): key for key in point["data_schema"].schema}
    assert set(fields) == {"time", "mode", "value", "fan", "delete"}
    assert fields["mode"].default() == "cool"
    assert fields["value"].default() == 24.0
    assert "16-30 °C" in point["description_placeholders"]["settings"]
    refused = await flow.async_step_switch_program_point_aircon(
        {"time": "07:00", "mode": "cool", "value": 24.2, "fan": "auto"}
    )
    assert refused["errors"] == {"base": "switch_program_aircon_setting"}
    missing = await flow.async_step_switch_program_point_aircon(
        {"time": "07:00", "mode": "cool", "fan": "auto"}
    )
    assert missing["errors"] == {"base": "switch_program_aircon_setting"}
    # The temperature left in the field does not count for fan only.
    await flow.async_step_switch_program_point_aircon(
        {"time": "07:30", "mode": "fanOnly", "value": 24.0, "fan": "high"}
    )
    await flow.async_step_switch_program_day({"point": "done"})
    confirm = await flow.async_step_switch_program_week({"action": "review"})
    assert confirm["step_id"] == "switch_program_confirm"
    assert unit.object_puts == []
    result = await flow.async_step_switch_program_confirm({})
    assert await outcome(hass, result) == "switch_program_saved"
    ((path, body),) = unit.object_puts
    assert path == P1
    assert body["switchPoints"][0] == {
        "dayOfWeek": "Mo",
        "time": 450,
        "parameters": {"operationMode": "fanOnly", "fanSpeed": "high"},
    }


async def test_dialog_shows_the_inactive_program_with_its_note(hass):
    coordinator, _, _ = await program_coordinator(hass)
    flow = flow_of(hass, coordinator)
    first = await flow.async_step_switch_programs()
    keys = [
        item["value"]
        for item in first["data_schema"].schema["program"].config["options"]
    ]
    week = await flow.async_step_switch_programs(
        {"program": next(key for key in keys if key.endswith(P2))}
    )
    assert week["step_id"] == "switch_program_week_aircon_inactive"


async def test_dialog_offers_no_program_without_configuration(hass):
    unit = ProgramUnit()
    unit.status[CONFIGURATION] = 404
    coordinator, _, _ = await program_coordinator(hass, unit)
    flow = flow_of(hass, coordinator)
    result = await flow.async_step_switch_programs()
    assert result["type"] == "abort" and result["reason"] == "no_switch_programs"
