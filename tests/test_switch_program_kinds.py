"""The further program kinds (cooling, zones, ventilation, WDDW2, absolute).

Block G: a program is read whenever its path is reachable; it is changed
only when the reported metadata give limits, setpoints and form. Every write
test counts the PUT requests and checks the body shape.
"""

from __future__ import annotations

import copy
from time import monotonic
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating.diagnostics import (
    switch_program_write_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    UnexpectedHttpStatus,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.resource_catalog import (
    CapabilityMaturity,
    capability_maturity,
    resource_name,
    supports_entity,
)
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from custom_components.bosch_buderus_heating.switch_program_edit import (
    edit_issue,
    editable_program,
    encode_body,
    replace_day,
    setpoint_text,
    value_issue,
    week_issue,
)
from custom_components.bosch_buderus_heating.switch_program_sensor import (
    attributes,
    state,
)
from custom_components.bosch_buderus_heating.switch_program_service import (
    SwitchProgramService,
)
from custom_components.bosch_buderus_heating.switch_programs import (
    RANGE_FIELDS,
    SwitchPoint,
    assess_switch_program,
    is_switch_program_path,
    program_kind,
)
from custom_components.bosch_buderus_heating.write_policies import ContractRejected
from custom_components.bosch_buderus_heating.writes import ControlUnused
from tests import test_dynamic_controls
from tests.program_kind_helpers import CountingGateway, payloads, resource

COOLING = "/heatingCircuits/hc1/cooling/switchPrograms/A"
COOLING_ON = "/heatingCircuits/hc1/cooling/temperatureLevels/on"
ZONE_HEAT = "/zones/zone1/switchPrograms/heating"
ZONE_COOL = "/zones/zone1/switchPrograms/cooling"
ZONE2 = "/zones/zone2/switchPrograms/heating"
VENT = "/ventilation/zone1/switchPrograms/cp"
VENT_LEVELS = "/ventilation/zone1/ventilationLevels"
WDDW2 = "/dhwCircuits/dhw1/switchPrograms/program1"
WDDW2_RANGE = "/dhwCircuits/dhw1/manualSetpoint"
ABS_A = "/heatingCircuits/hc1/switchPrograms/A"
ABS_B = "/heatingCircuits/hc1/switchPrograms/B"
MODE = "/heatingCircuits/hc1/switchProgramMode"

FILES = {
    "cooling": "connectkey/cooling-switch-programs.json",
    "zone": "connectkey/zone-switch-programs.json",
    "ventilation": "connectkey/ventilation-switch-program.json",
    "wddw2": "wddw2/switch-program.json",
    "absolute": "connectkey/switch-programs-absolute.json",
}


def items(name: str) -> dict:
    return payloads(FILES[name])


def readings(name: str, **changes) -> dict:
    data = items(name)
    for path, item in changes.items():
        if item is None:
            data.pop(path, None)
        else:
            data[path] = item
    return {path: resource(item) for path, item in data.items()}


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


def gateway_for(environment, name: str, **changes) -> CountingGateway:
    coordinator, client, _ = environment
    coordinator._switch_program_service = SwitchProgramService(
        client, sleep=AsyncMock()
    )
    data = items(name)
    for path, item in changes.items():
        if item is None:
            data.pop(path, None)
        else:
            data[path] = item
    return CountingGateway(coordinator, client, data)


def shifted(points, day=0, old=None, new=None, level=None):
    """The week with one point of a day moved and/or given another level."""
    result = []
    for point in points:
        if point.day == day and point.minute == old:
            result.append(
                SwitchPoint(
                    day, point.minute if new is None else new, level or point.level
                )
            )
        else:
            result.append(point)
    return tuple(result)


# Recognition (TPG-T06) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        (COOLING, "cooling"),
        (ZONE_HEAT, "zone"),
        (ZONE_COOL, "zone"),
        (VENT, "ventilation"),
        (WDDW2, "wddw2"),
        (ABS_A, "heating"),
        ("/heatingCircuits/hc1/cooling/switchPrograms/B", None),
        ("/heatingCircuits/hc1/switchPrograms/xyz", None),
        ("/dhwCircuits/dhw1/switchPrograms/program2", None),
        ("/dhwCircuits/dhw2/switchPrograms/program1", None),
        ("/ventilation/zone2/switchPrograms/cp", None),
        ("/zones/zone1/switchPrograms/A", None),
        ("/zones/zone0/switchPrograms/heating", None),
    ],
)
def test_program_paths_are_exact_and_never_guessed(path, kind):
    found = program_kind(path)
    assert (found.key if found else None) == kind
    assert is_switch_program_path(path) is (kind is not None)


def test_new_kinds_are_understood_named_and_polled():
    for path in (COOLING, ZONE_HEAT, VENT, WDDW2):
        assert capability_maturity(path) is CapabilityMaturity.UNDERSTOOD
    assert resource_name(COOLING) == "Zeitprogramm Kühlung"
    assert resource_name(ZONE_HEAT, language="en") == "Heating time program"
    assert resource_name(ZONE_COOL, language="en") == "Cooling time program"
    assert resource_name(VENT, language="en") == "Ventilation time program"
    assert resource_name(WDDW2) == "Zeitprogramm Warmwasser"


# Block G -----------------------------------------------------------------------


@pytest.mark.parametrize("path", [COOLING, VENT])
def test_extra_point_field_is_shown_but_not_changed(path):
    name = "cooling" if path == COOLING else "ventilation"
    item = copy.deepcopy(items(name)[path])
    item["switchPoints"][0]["extra"] = 1
    assert assess_switch_program(resource(item)).read_only == "point_extra_keys"
    assert edit_issue(resource(item), readings(name)) == "point_extra_keys"


def test_extra_program_field_is_shown_but_not_changed():
    item = copy.deepcopy(items("cooling")[COOLING])
    item["somethingNew"] = 1
    assessment = assess_switch_program(resource(item))
    assert assessment.supported and assessment.read_only == "unknown_fields"
    assert edit_issue(resource(item), readings("cooling")) == "unknown_fields"


def test_a_point_without_its_fields_still_hides_the_program():
    item = copy.deepcopy(items("cooling")[COOLING])
    del item["switchPoints"][0]["time"]
    assert assess_switch_program(resource(item)).reason == "invalid_switch_point"


def test_missing_limits_keep_the_program_readable_but_not_editable():
    item = copy.deepcopy(items("cooling")[COOLING])
    del item["maxNbOfSwitchPoints"]
    reading = resource(item)
    assessment = assess_switch_program(reading)
    assert assessment.supported
    assert "program_limits_missing" in assessment.notes
    assert supports_entity(reading)
    assert edit_issue(reading, readings("cooling")) == "program_limits_missing"


async def test_missing_limits_send_nothing(environment):
    item = copy.deepcopy(items("cooling")[COOLING])
    del item["switchPointTimeRaster"]
    gateway = gateway_for(environment, "cooling", **{COOLING: item})
    shown = gateway.read(COOLING)
    with pytest.raises(ContractRejected, match="program_not_editable"):
        await gateway.coordinator.async_write_switch_program(
            shown, assess_switch_program(shown).points
        )
    assert gateway.puts == 0


def test_flags_lock_the_program():
    item = copy.deepcopy(items("cooling")[COOLING])
    item["writeable"] = 0
    assert edit_issue(resource(item), readings("cooling")) == "not_writable"
    item = copy.deepcopy(items("cooling")[COOLING])
    item["used"] = "false"
    assert edit_issue(resource(item), readings("cooling")) == "not_used"
    item["used"] = False
    assert edit_issue(resource(item), readings("cooling")) == "not_used"


async def test_unused_program_sends_nothing(environment):
    gateway = gateway_for(environment, "cooling")
    shown = gateway.read(COOLING)
    week = shifted(assess_switch_program(shown).points, old=600, new=615)
    gateway.change(COOLING, used=False)
    with pytest.raises((ControlUnused, WriteValidationError)):
        await gateway.coordinator.async_write_switch_program(shown, week)
    assert gateway.puts == 0


def test_diagnostics_learn_the_form_without_values(environment):
    gateway = gateway_for(environment, "zone")
    result = switch_program_write_diagnostics(gateway.coordinator)
    program = result["programs"][ZONE2]
    assert program["kind"] == "zone"
    assert program["edit_issue"] == "program_step_missing"
    assert "program_step_missing" in program["notes"]
    assert program["point_shapes"] == ["reviewed"]
    text = repr(result)
    for value in ("20.0", "16.0", "420", "1320"):
        assert value not in text
    assert "zone" in result and "cooling" in result


# TP-09 cooling ---------------------------------------------------------------


def test_cooling_program_reads_on_and_off():
    reading = readings("cooling")[COOLING]
    assessment = assess_switch_program(reading)
    assert assessment.value_type == "level"
    assert state(assessment, False, 0, 700) == "on"
    assert state(assessment, False, 0, 1200) == "off"
    (description,) = [
        item
        for item in build_sensor_descriptions(readings("cooling"))
        if item.resource_path == COOLING
    ]
    assert description.translation_key == "cooling_switch_program"
    assert description.options[:2] == ["on", "off"]
    assert description.entity_registry_enabled_default is True
    shown = editable_program(reading, readings("cooling"))
    assert shown is not None and shown.levels == ("on", "off")


async def test_cooling_program_is_written_once_as_a_raw_array(environment):
    gateway = gateway_for(environment, "cooling")
    shown = gateway.read(COOLING)
    week = shifted(assess_switch_program(shown).points, old=600, new=615)
    confirmed = await gateway.coordinator.async_write_switch_program(shown, week)
    assert len(gateway.raw_puts) == 1 and gateway.puts == 1
    path, body = gateway.raw_puts[0]
    assert path == COOLING
    assert isinstance(body, list)
    assert body[0] == {"dayOfWeek": "Mo", "setpoint": "on", "time": 615}
    assert assess_switch_program(confirmed).points == week


def test_cooling_level_outside_on_off_is_refused():
    shown = editable_program(readings("cooling")[COOLING], readings("cooling"))
    assert shown is not None
    week = shifted(shown.points, old=600, level="eco")
    assert week_issue(week, shown) == "level_not_allowed"


def test_cooling_without_its_on_level_is_read_only():
    data = readings("cooling", **{COOLING_ON: None})
    assert edit_issue(data[COOLING], data) == "levels_unknown"


def test_cooling_program_with_another_level_is_read_only():
    item = copy.deepcopy(items("cooling")[COOLING])
    item["switchPoints"][0]["setpoint"] = "boost"
    data = readings("cooling", **{COOLING: item})
    assert assess_switch_program(data[COOLING]).supported
    assert edit_issue(data[COOLING], data) == "level_not_listed"


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (UnexpectedHttpStatus(400), UnexpectedHttpStatus),
        # A server error after sending: only reading tells; never sent again.
        (UnexpectedHttpStatus(500), WriteNotConfirmed),
        (RateLimited(60), RateLimited),
    ],
)
async def test_cooling_put_failure_is_never_repeated(environment, error, raised):
    gateway = gateway_for(environment, "cooling")
    shown = gateway.read(COOLING)
    week = shifted(assess_switch_program(shown).points, old=600, new=615)
    gateway.client.put_resource_raw_json.side_effect = error
    with pytest.raises(raised):
        await gateway.coordinator.async_write_switch_program(shown, week)
    assert gateway.client.put_resource_raw_json.await_count == 1


async def test_cooling_read_back_mismatch_is_not_confirmed(environment):
    gateway = gateway_for(environment, "cooling")
    gateway.ignore_writes = True
    shown = gateway.read(COOLING)
    week = shifted(assess_switch_program(shown).points, old=600, new=615)
    with pytest.raises(WriteNotConfirmed):
        await gateway.coordinator.async_write_switch_program(shown, week)
    assert len(gateway.raw_puts) == 1


async def test_cloud_pause_sends_no_program(environment):
    gateway = gateway_for(environment, "cooling")
    gateway.coordinator._cloud_backoff_until = monotonic() + 600
    shown = gateway.read(COOLING)
    week = shifted(assess_switch_program(shown).points, old=600, new=615)
    with pytest.raises(RateLimited):
        await gateway.coordinator.async_write_switch_program(shown, week)
    assert gateway.puts == 0


# TP-10 zones -------------------------------------------------------------------


def test_zone_program_shows_the_scheduled_temperature():
    data = readings("zone")
    assessment = assess_switch_program(data[ZONE_HEAT])
    assert assessment.value_type == "absolute"
    assert assessment.numbers_as_text is True
    assert state(assessment, True, 0, 400) == 21.0
    assert state(assessment, True, 0, 600) == 17.5
    (description,) = [
        item
        for item in build_sensor_descriptions(data)
        if item.resource_path == ZONE_HEAT
    ]
    assert description.native_unit_of_measurement == "°C"
    assert description.options is None
    attrs = attributes(data[ZONE_HEAT], data, True, 0, 400)
    assert attrs["min_setpoint"] == 5.0 and attrs["setpoint_step"] == 0.5


async def test_zone_program_is_written_with_text_numbers(environment):
    gateway = gateway_for(environment, "zone")
    shown = gateway.read(ZONE_HEAT)
    week = shifted(assess_switch_program(shown).points, old=360, level="21.5")
    await gateway.coordinator.async_write_switch_program(shown, week)
    ((path, body),) = gateway.raw_puts
    assert path == ZONE_HEAT
    assert body[0] == {"dayOfWeek": "Mo", "setpoint": "21.5", "time": 360}
    assert all(isinstance(item["setpoint"], str) for item in body)


async def test_zone_heating_and_cooling_are_separate_writes(environment):
    gateway = gateway_for(environment, "zone")
    heating = gateway.read(ZONE_HEAT)
    await gateway.coordinator.async_write_switch_program(
        heating, shifted(assess_switch_program(heating).points, old=360, level="21.5")
    )
    gateway.refresh()
    cooling = gateway.read(ZONE_COOL)
    await gateway.coordinator.async_write_switch_program(
        cooling, shifted(assess_switch_program(cooling).points, old=600, level="24.5")
    )
    assert [path for path, _body in gateway.raw_puts] == [ZONE_HEAT, ZONE_COOL]


@pytest.mark.parametrize(
    ("level", "issue"),
    [
        ("30.5", "value_out_of_range"),
        ("4.5", "value_out_of_range"),
        ("21.2", "value_off_step"),
        ("0.0", "off_not_offered"),
        ("eco", "value_not_allowed"),
        ("21.50", "value_not_allowed"),
    ],
)
def test_zone_values_are_refused_not_rounded(level, issue):
    data = readings("zone")
    shown = editable_program(data[ZONE_HEAT], data)
    assert shown is not None
    assert value_issue(level, shown) == issue
    week = shifted(shown.points, old=360, level=level)
    assert week_issue(week, shown) == issue


def test_zone_without_step_is_read_only():
    data = readings("zone")
    assert assess_switch_program(data[ZONE2]).supported
    assert edit_issue(data[ZONE2], data) == "program_step_missing"


def test_zone_with_mixed_setpoints_is_not_read_or_not_changed():
    item = copy.deepcopy(items("zone")[ZONE_HEAT])
    item["switchPoints"][0]["setpoint"] = "comfort"
    assert assess_switch_program(resource(item)).reason == "mixed_setpoints"
    # Numbers as text and as JSON numbers: shown, never changed.
    item = copy.deepcopy(items("zone")[ZONE_HEAT])
    item["switchPoints"][0]["setpoint"] = 21.0
    assessment = assess_switch_program(resource(item))
    assert assessment.supported and assessment.read_only == "mixed_setpoints"


async def test_zone_numbers_keep_the_json_number_form(environment):
    item = copy.deepcopy(items("zone")[ZONE_HEAT])
    for point in item["switchPoints"]:
        point["setpoint"] = float(point["setpoint"])
    gateway = gateway_for(environment, "zone", **{ZONE_HEAT: item})
    shown = gateway.read(ZONE_HEAT)
    assert assess_switch_program(shown).numbers_as_text is False
    week = shifted(assess_switch_program(shown).points, old=360, level="21.5")
    await gateway.coordinator.async_write_switch_program(shown, week)
    ((_path, body),) = gateway.raw_puts
    assert body[0]["setpoint"] == 21.5


def test_zone_off_only_where_the_reading_has_it():
    item = copy.deepcopy(items("zone")[ZONE_HEAT])
    item["switchPoints"][1]["setpoint"] = "0.0"
    data = readings("zone", **{ZONE_HEAT: item})
    shown = editable_program(data[ZONE_HEAT], data)
    assert shown is not None and shown.off_value == "0.0"
    assert value_issue("0.0", shown) is None


# TP-11 ventilation -------------------------------------------------------------


async def test_ventilation_program_uses_the_listed_levels(environment):
    data = readings("ventilation")
    shown = editable_program(data[VENT], data)
    assert shown is not None
    assert set(shown.levels) == {"min", "red", "nom", "max"}
    gateway = gateway_for(environment, "ventilation")
    reading = gateway.read(VENT)
    week = shifted(assess_switch_program(reading).points, old=420, level="max")
    await gateway.coordinator.async_write_switch_program(reading, week)
    ((path, body),) = gateway.raw_puts
    assert path == VENT and body[0]["setpoint"] == "max"


def test_ventilation_without_listed_levels_is_read_only():
    item = copy.deepcopy(items("ventilation")[VENT_LEVELS])
    item["references"] = []
    data = readings("ventilation", **{VENT_LEVELS: item})
    assert edit_issue(data[VENT], data) == "levels_unknown"


def test_ventilation_level_not_listed():
    item = copy.deepcopy(items("ventilation")[VENT])
    item["switchPoints"][0]["setpoint"] = "dem"
    data = readings("ventilation", **{VENT: item})
    assert edit_issue(data[VENT], data) == "level_not_listed"
    shown = editable_program(readings("ventilation")[VENT], readings("ventilation"))
    assert shown is not None
    assert week_issue(shifted(shown.points, old=420, level="dem"), shown) == (
        "level_not_allowed"
    )


def test_ventilation_unknown_reference_ending_is_never_offered():
    item = copy.deepcopy(items("ventilation")[VENT_LEVELS])
    item["references"].append({"id": "/ventilation/zone1/ventilationLevels/turbo"})
    data = readings("ventilation", **{VENT_LEVELS: item})
    shown = editable_program(data[VENT], data)
    assert shown is not None and "turbo" not in shown.levels


# TP-12 WDDW2 -------------------------------------------------------------------


async def test_wddw2_program_is_written_as_an_object(environment):
    gateway = gateway_for(environment, "wddw2")
    shown = gateway.read(WDDW2)
    week = shifted(assess_switch_program(shown).points, old=360, level="50")
    await gateway.coordinator.async_write_switch_program(shown, week)
    assert gateway.raw_puts == [] and len(gateway.object_puts) == 1
    path, body = gateway.object_puts[0]
    assert path == WDDW2
    assert set(body) == {"switchPoints"}
    assert body["switchPoints"][0] == {"dayOfWeek": "Mo", "setpoint": "50", "time": 360}


def test_heating_program_body_stays_a_raw_array():
    data = readings("absolute")
    shown = editable_program(data[ABS_A], data, program_mode=data[MODE])
    assert shown is not None
    body = encode_body(shown.points, shown)
    assert isinstance(body, list)


@pytest.mark.parametrize(
    ("value", "issue"),
    [
        ("21.5", "value_not_allowed"),
        ("65", "value_out_of_range"),
        ("25", "value_out_of_range"),
    ],
)
def test_wddw2_values_are_whole_degrees_in_range(value, issue):
    data = readings("wddw2")
    shown = editable_program(data[WDDW2], data)
    assert shown is not None
    text = setpoint_text(shown, value)
    assert value_issue(text, shown) == issue


def test_wddw2_off_is_always_offered():
    data = readings("wddw2")
    shown = editable_program(data[WDDW2], data)
    assert shown is not None and value_issue("off", shown) is None


def test_wddw2_without_range_is_read_only():
    data = readings("wddw2", **{WDDW2_RANGE: None})
    assert edit_issue(data[WDDW2], data) == "program_range_missing"


def test_wddw2_without_limits_does_not_use_the_app_fallback():
    item = copy.deepcopy(items("wddw2")[WDDW2])
    for key in (
        "maxNbOfSwitchPoints",
        "maxNbOfSwitchPointsPerDay",
        "switchPointTimeRaster",
    ):
        del item[key]
    data = readings("wddw2", **{WDDW2: item})
    assert edit_issue(data[WDDW2], data) == "program_limits_missing"


def test_wddw2_day_without_points_has_no_carry_over():
    data = readings("wddw2")
    assessment = assess_switch_program(data[WDDW2])
    assert assessment.carry_over is False
    # Sunday has no points; Monday before 06:00 has no setpoint either.
    assert assessment.active_level(6, 600) is None
    assert assessment.active_level(0, 100) is None
    assert assessment.active_level(0, 400) == "55"
    assert state(assessment, True, 0, 400) == 55.0
    assert state(assessment, True, 0, 1330) is None
    attrs = attributes(data[WDDW2], data, True, 0, 1330)
    assert attrs["scheduled"] == "off"
    assert (attrs["min_setpoint"], attrs["max_setpoint"]) == (30.0, 60.0)


def test_wddw2_empty_week_is_never_sent():
    data = readings("wddw2")
    shown = editable_program(data[WDDW2], data)
    assert shown is not None
    assert week_issue((), shown) == "empty_week"


# TP-13 absolute heating programs -----------------------------------------------


def test_absolute_program_is_read_as_temperatures():
    data = readings("absolute")
    assessment = assess_switch_program(data[ABS_A])
    assert assessment.value_type == "absolute"
    assert "setpoint_property_levels" in assessment.notes
    assert "setpoint_property_missing" in assess_switch_program(data[ABS_B]).notes
    (description,) = [
        item for item in build_sensor_descriptions(data) if item.resource_path == ABS_B
    ]
    assert description.translation_key == "absolute_switch_program"
    assert attributes(data[ABS_A], data, True, 0, 400)["program_mode"] == "absolute"


async def test_absolute_point_is_written_as_text(environment):
    gateway = gateway_for(environment, "absolute")
    shown = gateway.read(ABS_B)
    week = shifted(assess_switch_program(shown).points, old=420, level="21.5")
    await gateway.coordinator.async_write_switch_program(shown, week)
    ((path, body),) = gateway.raw_puts
    assert path == ABS_B and body[0]["setpoint"] == "21.5"
    # The program type is read right before the PUT and never written.
    assert MODE in gateway.gets
    assert all(path != MODE for path, _value in gateway.value_puts)


def test_absolute_program_refuses_level_names_and_level_programs_refuse_numbers():
    data = readings("absolute")
    shown = editable_program(data[ABS_A], data, program_mode=data[MODE])
    assert shown is not None
    assert value_issue("comfort2", shown) == "value_not_allowed"
    from tests.switch_program_helpers import PAYLOADS as LEVEL_PAYLOADS
    from tests.switch_program_helpers import PROGRAM_A

    levels = {path: resource(item) for path, item in LEVEL_PAYLOADS.items()}
    level_program = editable_program(
        levels[PROGRAM_A],
        levels,
        program_mode=levels["/heatingCircuits/hc1/switchProgramMode"],
    )
    assert level_program is not None
    assert value_issue("21.0", level_program) == "level_not_allowed"


async def test_absolute_program_type_change_sends_nothing(environment):
    gateway = gateway_for(environment, "absolute")
    shown = gateway.read(ABS_B)
    week = shifted(assess_switch_program(shown).points, old=420, level="21.5")
    gateway.change(MODE, value="levels")
    with pytest.raises(ContractRejected, match="program_mode_not_absolute"):
        await gateway.coordinator.async_write_switch_program(shown, week)
    assert gateway.puts == 0


def test_absolute_program_needs_its_program_type():
    data = readings("absolute")
    assert edit_issue(data[ABS_A], data, program_mode=None) == "program_mode_unknown"
    levels = resource({**items("absolute")[MODE], "value": "levels"})
    assert edit_issue(data[ABS_A], data, program_mode=levels) == (
        "program_mode_not_absolute"
    )


def test_absolute_off_only_where_the_program_has_it():
    data = readings("absolute")
    with_off = editable_program(data[ABS_A], data, program_mode=data[MODE])
    without = editable_program(data[ABS_B], data, program_mode=data[MODE])
    assert with_off is not None and without is not None
    assert value_issue("0.0", with_off) is None
    assert value_issue("0.0", without) == "off_not_offered"


def _without_range(*keys):
    item = copy.deepcopy(items("absolute")[ABS_B])
    for key in keys:
        item.pop(key, None)
    return item


def test_absolute_program_without_minimum_takes_5():
    data = readings("absolute", **{ABS_B: _without_range("switchPointMin")})
    assessment = assess_switch_program(data[ABS_B])
    reported = items("absolute")[ABS_B]
    assert assessment.minimum == 5.0
    assert assessment.maximum == reported["switchPointMax"]
    assert "program_range_default" in assessment.notes
    assert edit_issue(data[ABS_B], data, program_mode=data[MODE]) is None


def test_absolute_heating_program_without_range_takes_5_to_30_in_half_steps():
    item = _without_range(*RANGE_FIELDS)
    data = readings("absolute", **{ABS_B: item})
    assessment = assess_switch_program(data[ABS_B])
    assert (assessment.minimum, assessment.maximum, assessment.step) == (
        5.0,
        30.0,
        0.5,
    )
    assert {"program_range_default", "program_step_default"} <= set(assessment.notes)
    shown = editable_program(data[ABS_B], data, program_mode=data[MODE])
    assert shown is not None
    assert value_issue("5.0", shown) is None
    assert value_issue("30.0", shown) is None
    assert value_issue("30.5", shown) == "value_out_of_range"
    assert value_issue("4.5", shown) == "value_out_of_range"
    assert value_issue("21.25", shown) == "value_off_step"


@pytest.mark.parametrize("level", ["30.5", "4.5", "21.25"])
async def test_absolute_program_without_range_never_sends_outside_it(
    environment, level
):
    gateway = gateway_for(
        environment, "absolute", **{ABS_B: _without_range(*RANGE_FIELDS)}
    )
    shown = gateway.read(ABS_B)
    week = shifted(assess_switch_program(shown).points, old=420, level=level)
    with pytest.raises((ContractRejected, WriteValidationError)):
        await gateway.coordinator.async_write_switch_program(shown, week)
    assert gateway.puts == 0


async def test_absolute_program_without_range_sends_a_value_within_it(environment):
    gateway = gateway_for(
        environment, "absolute", **{ABS_B: _without_range(*RANGE_FIELDS)}
    )
    shown = gateway.read(ABS_B)
    week = shifted(assess_switch_program(shown).points, old=420, level="29.5")
    await gateway.coordinator.async_write_switch_program(shown, week)
    ((path, body),) = gateway.raw_puts
    assert path == ABS_B and body[0]["setpoint"] == "29.5"


def test_a_zone_program_without_step_takes_no_default():
    data = readings("zone")
    assessment = assess_switch_program(data[ZONE2])
    assert "program_step_default" not in assessment.notes
    assert edit_issue(data[ZONE2], data) == "program_step_missing"


def test_absolute_heating_program_with_json_numbers_is_shown_but_not_changed():
    item = copy.deepcopy(items("absolute")[ABS_B])
    for point in item["switchPoints"]:
        point["setpoint"] = float(point["setpoint"])
    data = readings("absolute", **{ABS_B: item})
    assessment = assess_switch_program(data[ABS_B])
    assert assessment.supported and assessment.read_only == "setpoint_form_other"
    assert edit_issue(data[ABS_B], data, program_mode=data[MODE]) == (
        "setpoint_form_other"
    )


def test_day_replacement_keeps_other_days():
    data = readings("absolute")
    shown = editable_program(data[ABS_B], data, program_mode=data[MODE])
    assert shown is not None
    week = replace_day(shown.points, 2, [SwitchPoint(2, 480, "19.5")])
    assert week_issue(week, shown) is None
    assert [point for point in week if point.day != 2] == [
        point for point in shown.points if point.day != 2
    ]
