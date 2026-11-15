"""The program dialogs: steps that must end or stay on their form when the
state is gone or the entry is wrong, and the pure helpers behind the forms."""

# Fixtures are imported from the module that defines them.
# ruff: noqa: F811

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.bosch_buderus_heating.aircon_program_rules import (
    parse_setting,
)
from custom_components.bosch_buderus_heating.ha_schema import validation as vol
from custom_components.bosch_buderus_heating.switch_program_edit import (
    editable_program,
)
from custom_components.bosch_buderus_heating.switch_program_flow import (
    SwitchProgramOptionsFlow,
    _number_label,
    aircons_of,
)
from custom_components.bosch_buderus_heating.switch_programs import SwitchPoint
from tests.test_aircon_programs import P1 as AIRCON_PROGRAM
from tests.test_aircon_programs import readings as aircon_readings
from tests.test_rrc_dhw_program import gateway as dhw_gateway  # noqa: F401
from tests.test_rrc_point_add_delete import make_flow as make_rrc_flow
from tests.test_switch_program_flow import (  # noqa: F401 - fixtures and helpers
    _to_confirm,
    environment,
    make_flow,
    open_program,
)
from tests.test_switch_program_kinds import ABS_A, ZONE_HEAT, readings


# ------------------------------------------------------------ pure helpers
def test_a_level_that_is_no_number_is_labelled_as_it_is():
    assert _number_label("comfort2", ",") == "comfort2"
    assert _number_label("21.5", ",") == "21,5 °C"
    assert _number_label("21", "") == "21 °C"


def test_a_setting_that_cannot_be_parsed_keeps_its_text():
    assert SwitchProgramOptionsFlow._aircon_label("not a setting at all", {}, ",") == (
        "not a setting at all"
    )


def aircon_program():
    data = aircon_readings()
    program = editable_program(data[AIRCON_PROGRAM], data)
    assert program is not None and program.aircon is not None
    return program


def zone_program():
    data = readings("zone")
    program = editable_program(data[ZONE_HEAT], data)
    assert program is not None and program.numeric
    return program


def test_the_aircon_entry_is_built_from_the_mode_and_what_the_mode_has():
    program = aircon_program()
    entered = SwitchProgramOptionsFlow._sp_entered_level
    modes = program.aircon.modes
    assert entered(program, {"mode": "off"}) == "off"
    assert entered(program, {"mode": "unknown"}) is None
    assert entered(program, {"mode": None}) is None
    ranged = next(rule for rule in modes.values() if rule.minimum is not None)
    # A flag or text is no temperature.
    assert entered(program, {"mode": ranged.mode, "value": True}) is None
    assert entered(program, {"mode": ranged.mode, "value": "22"}) is None
    with_value = entered(program, {"mode": ranged.mode, "value": 22.0})
    assert with_value is not None and with_value.startswith(ranged.mode)
    assert "22" in with_value
    # A mode without a range ignores a left-over temperature.
    plain = next((rule for rule in modes.values() if rule.minimum is None), None)
    if plain is not None:
        assert "22" not in (entered(program, {"mode": plain.mode, "value": 22.0}) or "")


def test_a_number_entry_can_switch_the_point_off_where_that_is_offered():
    program = zone_program()
    entered = SwitchProgramOptionsFlow._sp_entered_level
    assert entered(program, {"value": 21.5}) == "21.5"
    assert entered(program, {"value": None}) is None
    if program.off_value is not None:
        assert entered(program, {"off": True}) == program.off_value


def test_the_number_form_defaults_only_to_a_readable_original():
    program = zone_program()
    number = SwitchProgramOptionsFlow._sp_number_fields

    def defaults(original):
        return vol.Schema(number(program, original))({})

    assert defaults(SwitchPoint(0, 360, "21.5"))["value"] == 21.5
    # A level that is no number, or no original at all, offers no default.
    assert "value" not in defaults(SwitchPoint(0, 360, "not a number"))
    assert "value" not in defaults(None)


def test_the_aircon_form_defaults_to_the_mode_of_the_original_only():
    program = aircon_program()
    fields = SwitchProgramOptionsFlow._sp_aircon_fields
    with pytest.raises(vol.Invalid):
        vol.Schema(fields(program.aircon, None, {}))({})
    original = program.points[0]
    shown = vol.Schema(fields(program.aircon, original, {}))({})
    assert shown["mode"] == parse_setting(original.level).mode


async def test_the_labels_of_a_dialog_without_a_program_are_the_days_only(
    hass, environment
):
    flow, _ = make_flow(hass, environment)
    flow._sp_choice = None
    days, levels = await flow._sp_labels()
    assert set(days) == {"mo", "tu", "we", "th", "fr", "sa", "su"}
    assert levels == {}


# ------------------------------------------------------------------- steps
async def test_an_unknown_action_asks_again(hass, environment):
    flow, gateway = make_flow(hass, environment)
    await open_program(flow)
    result = await flow.async_step_switch_program_week({"action": "bogus"})
    assert result["errors"] == {"action": "switch_program_day_required"}
    assert gateway.raw_puts == []


async def test_a_program_that_is_not_offered_ends_the_dialog(hass, environment):
    flow, _ = make_flow(hass, environment)
    await flow.async_step_switch_programs()
    result = await flow.async_step_switch_programs(
        {"program": "1:/heatingCircuits/hc1/switchPrograms/Z"}
    )
    assert result["reason"] == "no_switch_programs"


async def test_the_week_variants_show_the_same_week(hass, environment):
    flow, _ = make_flow(hass, environment)
    await open_program(flow)
    plain = await flow.async_step_switch_program_week()
    for step in (
        flow.async_step_switch_program_week_inactive,
        flow.async_step_switch_program_week_dhw,
        flow.async_step_switch_program_week_rrc_dhw,
        flow.async_step_switch_program_week_aircon,
        flow.async_step_switch_program_week_aircon_inactive,
    ):
        shown = await step()
        assert shown["step_id"] == plain["step_id"]
        assert shown["description_placeholders"] == plain["description_placeholders"]


async def test_the_day_step_ends_without_a_day_or_a_program(hass, environment):
    flow, gateway = make_flow(hass, environment)
    await open_program(flow)
    flow._sp_day = None
    assert (await flow.async_step_switch_program_day())["reason"] == (
        "switch_program_changed"
    )
    assert (await flow.async_step_switch_program_point())["reason"] == (
        "switch_program_changed"
    )
    assert (await flow.async_step_switch_program_copy())["reason"] == (
        "switch_program_changed"
    )
    flow._sp_choice = None
    assert (await flow.async_step_switch_program_confirm())["reason"] == (
        "switch_program_changed"
    )
    assert gateway.raw_puts == []


@pytest.mark.parametrize("selected", ["9", "x1", "", None])
async def test_a_point_that_does_not_exist_keeps_the_day_form(
    hass, environment, selected
):
    flow, gateway = make_flow(hass, environment)
    await open_program(flow)
    await flow.async_step_switch_program_week({"action": "edit", "day": "sa"})
    result = await flow.async_step_switch_program_day({"point": selected})
    assert result["step_id"] == "switch_program_day"
    assert result["errors"] == {"base": "switch_program_changed"}
    assert gateway.raw_puts == []


async def test_a_point_that_vanished_ends_the_point_step(hass, environment):
    flow, gateway = make_flow(hass, environment)
    await open_program(flow)
    await flow.async_step_switch_program_week({"action": "edit", "day": "sa"})
    flow._sp_point = 99
    result = await flow.async_step_switch_program_point(
        {"time": "08:00", "level": "eco"}
    )
    assert result["reason"] == "switch_program_changed"
    assert flow._sp_draft == flow._sp_choice.program.points
    assert gateway.raw_puts == []


async def test_copying_a_day_beyond_the_total_limit_is_refused(hass, environment):
    from dataclasses import replace

    flow, gateway = make_flow(hass, environment)
    snapshot = gateway.coordinator.data["/heatingCircuits/hc1/switchPrograms/A"]
    gateway.coordinator.data["/heatingCircuits/hc1/switchPrograms/A"] = replace(
        snapshot,
        resource=replace(
            snapshot.resource,
            program_fields={
                **snapshot.resource.program_fields,
                "maxNbOfSwitchPoints": 16,
            },
        ),
    )
    await open_program(flow)
    await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    for minute in ("09:00", "10:00"):
        await flow.async_step_switch_program_day({"point": "add"})
        await flow.async_step_switch_program_point({"time": minute, "level": "eco"})
    await flow.async_step_switch_program_day({"point": "done"})
    before = flow._sp_draft
    await flow.async_step_switch_program_week({"action": "copy", "day": "mo"})
    result = await flow.async_step_switch_program_copy({"days": ["tu"]})
    assert result["step_id"] == "switch_program_copy"
    assert result["errors"] == {"base": "switch_program_too_many_in_all"}
    assert flow._sp_draft == before
    assert gateway.raw_puts == []


async def test_a_confirmed_dialog_needs_a_changed_program(hass, environment):
    flow, gateway = make_flow(hass, environment)
    await _to_confirm(flow)
    assert gateway.raw_puts == []


# ------------------------------------------------------- hot water (RRC)
async def test_the_last_point_of_a_hot_water_day_cannot_be_deleted(hass, dhw_gateway):
    flow = make_rrc_flow(hass, dhw_gateway)
    first = await flow.async_step_rrc_programs()
    options = next(iter(first["data_schema"].schema.values())).config["options"]
    dhw = next(item["value"] for item in options if item["value"].startswith("dhw:"))
    await flow.async_step_rrc_programs({"program": dhw})
    await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    count = len([point for point in flow._sp_draft if point.day == 0])
    assert count >= 2
    for _ in range(count - 1):
        await flow.async_step_switch_program_day({"point": "0"})
        await flow.async_step_switch_program_point(
            {"time": "00:00", "level": "off", "delete": True}
        )
    await flow.async_step_switch_program_day({"point": "0"})
    result = await flow.async_step_switch_program_point(
        {"time": "00:00", "level": "off", "delete": True}
    )
    assert result["errors"] == {"base": "switch_program_day_emptied"}
    assert len([point for point in flow._sp_draft if point.day == 0]) == 1
    assert dhw_gateway.puts == 0


def test_a_number_entry_switches_a_point_off_where_the_program_has_an_off_value():
    data = readings("absolute")
    program = editable_program(
        data[ABS_A], data, program_mode=data["/heatingCircuits/hc1/switchProgramMode"]
    )
    assert program is not None and program.off_value is not None
    entered = SwitchProgramOptionsFlow._sp_entered_level
    assert entered(program, {"off": True, "value": 21.0}) == program.off_value
    assert entered(program, {"off": False, "value": 21.0}) == "21.0"


class _Entry:
    def __init__(self, runtime):
        self.runtime_data = runtime


def test_the_air_conditioners_of_an_entry_come_from_its_connections():
    from unittest.mock import MagicMock

    from custom_components.bosch_buderus_heating.aircon import AirconCoordinator

    unit = MagicMock(spec=AirconCoordinator)
    other = object()
    values = {"unit": unit, "other": other}
    connections = SimpleNamespace(
        snapshot=SimpleNamespace(bindings={"unit": 1, "other": 2}),
        display_data=values.__getitem__,
    )
    assert aircons_of(_Entry(SimpleNamespace(connections=connections))) == (unit,)  # type: ignore[arg-type]
    # Without connections, or without a snapshot, there is no unit.
    assert aircons_of(_Entry(SimpleNamespace())) == ()  # type: ignore[arg-type]
    assert aircons_of(_Entry(None)) == ()  # type: ignore[arg-type]
    assert (
        aircons_of(
            _Entry(SimpleNamespace(connections=SimpleNamespace(snapshot=None)))  # type: ignore[arg-type]
        )
        == ()
    )
    # A tuple the entry carries itself is used as it is.
    assert aircons_of(_Entry(SimpleNamespace(aircons=(unit,)))) == (unit,)  # type: ignore[arg-type]
