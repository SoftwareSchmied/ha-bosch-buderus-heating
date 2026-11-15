"""TP-19: changing the hot water week program of an RRC room controller."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from time import monotonic
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating.config_flow import BoschBuderusOptionsFlow
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    UnexpectedHttpStatus,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.rrc_dhw_program import (
    edit_issue,
    editable,
    encode_week,
)
from custom_components.bosch_buderus_heating.rrc_programs import RrcProgramService
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from custom_components.bosch_buderus_heating.switch_program_edit import (
    replace_day,
    week_issue,
)
from custom_components.bosch_buderus_heating.switch_programs import SwitchPoint
from custom_components.bosch_buderus_heating.write_policies import ContractRejected
from tests import test_dynamic_controls
from tests.helpers.options_menu import outcome
from tests.program_kind_helpers import CountingGateway, payloads, resource

PATH = "/dhwCircuits/dhw1/programs/pg1/week"
ITEMS = payloads("rrc/dhw-week-program.json")
COMPONENT = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


@pytest.fixture
def gateway(environment):
    coordinator, client, _ = environment
    coordinator._rrc_program_service = RrcProgramService(client, sleep=AsyncMock())
    return CountingGateway(coordinator, client, ITEMS)


def program(item=None):
    shown = editable(resource(item or ITEMS[PATH]))
    assert shown is not None
    return shown


def with_point(week, day, minute, level):
    return replace_day(
        week,
        day,
        [*(point for point in week if point.day == day), SwitchPoint(day, minute, level)],
    )


def test_program_is_offered_with_program_levels():
    shown = program()
    assert shown.levels == ("high", "off", "low")
    assert shown.max_switch_points_per_day == 6 and shown.time_raster == 5


async def test_changed_point_writes_the_whole_week_once(gateway):
    shown = program()
    week = tuple(
        SwitchPoint(point.day, point.minute, "off")
        if (point.day, point.minute) == (0, 330)
        else point
        for point in shown.points
    )
    await gateway.coordinator.async_write_rrc_dhw_program(gateway.read(PATH), week)
    ((path, body),) = gateway.value_puts
    assert path == PATH
    assert len(body) == 28
    assert body[0] == {"d": "Mo", "t": 330, "dhw": "off"}
    days = [item["d"] for item in body]
    assert days == sorted(days, key=["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"].index)


async def test_add_and_delete_within_six_points(gateway):
    shown = program()
    week = with_point(shown.points, 1, 720, "off")
    week = tuple(point for point in week if (point.day, point.minute) != (2, 1020))
    assert week_issue(week, shown) is None
    await gateway.coordinator.async_write_rrc_dhw_program(gateway.read(PATH), week)
    assert gateway.puts == 1


def test_seventh_point_is_refused():
    shown = program()
    week = shown.points
    for minute in (600, 660, 700):
        week = with_point(week, 0, minute, "off")
    assert week_issue(week, shown) == "too_many_per_day"


def test_eco_is_not_offered_when_the_program_does_not_use_it():
    shown = program()
    week = with_point(shown.points, 0, 600, "eco")
    assert week_issue(week, shown) == "level_not_allowed"


async def test_low_and_off_spellings_stay_as_read(gateway):
    shown = program()
    week = with_point(shown.points, 0, 600, "high")
    body = encode_week(gateway.read(PATH), week)
    assert {"d": "Sa", "t": 480, "dhw": "low"} in body
    assert {"d": "Su", "t": 1320, "dhw": "Off"} in body


def test_unknown_level_or_extra_field_is_read_only():
    item = copy.deepcopy(ITEMS[PATH])
    item["value"][0]["dhw"] = "boost"
    assert edit_issue(resource(item)) == "unknown_levels"
    item = copy.deepcopy(ITEMS[PATH])
    item["value"][0]["x"] = 1
    assert edit_issue(resource(item)) == "unknown_fields"


async def test_week_changed_in_the_app_sends_nothing(gateway):
    shown = gateway.read(PATH)
    gateway.change(PATH, value=[{"d": "Mo", "t": 0, "dhw": "high"}])
    with pytest.raises(ContractRejected, match="program_changed"):
        await gateway.coordinator.async_write_rrc_dhw_program(
            shown, with_point(program().points, 0, 600, "off")
        )
    assert gateway.puts == 0


@pytest.mark.parametrize(
    ("change", "issue"),
    [
        ({"writeable": False}, "not_writable"),
        ({"used": False}, "program_unreadable"),
        ({"available": "false"}, "program_unreadable"),
    ],
)
def test_flags_lock_the_program(change, issue):
    item = {**copy.deepcopy(ITEMS[PATH]), **change}
    assert edit_issue(resource(item)) == issue


def test_other_program_ids_are_not_changed():
    item = {**copy.deepcopy(ITEMS[PATH]), "id": "/dhwCircuits/dhw1/programs/pg2/week"}
    assert edit_issue(resource(item)) == "program_id_unexpected"


def test_last_point_of_a_day_stays():
    shown = program()
    week = tuple(point for point in shown.points if point.day != 3)
    assert week_issue(week, shown) == "day_emptied"


@pytest.mark.parametrize("status", [400, 403, 404, 500])
async def test_failed_put_is_not_repeated(gateway, status):
    gateway.client.put_resource_value.side_effect = UnexpectedHttpStatus(status)
    with pytest.raises((UnexpectedHttpStatus, WriteNotConfirmed)):
        await gateway.coordinator.async_write_rrc_dhw_program(
            gateway.read(PATH), with_point(program().points, 0, 600, "off")
        )
    assert gateway.client.put_resource_value.await_count == 1


async def test_read_back_mismatch_is_not_confirmed(gateway):
    gateway.ignore_writes = True
    with pytest.raises(WriteNotConfirmed):
        await gateway.coordinator.async_write_rrc_dhw_program(
            gateway.read(PATH), with_point(program().points, 0, 600, "off")
        )
    assert len(gateway.value_puts) == 1


async def test_cloud_pause_sends_nothing(gateway):
    gateway.coordinator._cloud_backoff_until = monotonic() + 600
    with pytest.raises((RateLimited, WriteValidationError)):
        await gateway.coordinator.async_write_rrc_dhw_program(
            gateway.read(PATH), with_point(program().points, 0, 600, "off")
        )
    assert gateway.puts == 0


def test_level_names_are_program_levels_never_temperatures():
    for path in [COMPONENT / "strings.json", *(COMPONENT / "translations").glob("*.json")]:
        options = json.loads(path.read_text(encoding="utf-8"))["selector"][
            "rrc_dhw_level"
        ]["options"]
        assert set(options) == {"high", "off", "low", "eco"}, path
        assert not any("°" in text for text in options.values()), path


async def test_dialog_edits_the_week_and_sends_once(hass, gateway):
    coordinator = gateway.coordinator
    entry = coordinator.config_entry
    entry.runtime_data = BoschBuderusRuntimeData(
        client=gateway.client,
        token_manager=AsyncMock(),
        gateways=(coordinator.gateway,),
        coordinators=(coordinator,),
    )
    flow = BoschBuderusOptionsFlow(entry)
    flow.hass = hass
    menu = await flow.async_step_init()
    assert "rrc_programs" in menu["menu_options"]
    first = await flow.async_step_rrc_programs()
    options = next(iter(first["data_schema"].schema.values())).config["options"]
    dhw = next(item["value"] for item in options if item["value"].startswith("dhw:"))
    week = await flow.async_step_rrc_programs({"program": dhw})
    assert week["step_id"] == "switch_program_week_rrc_dhw"
    day = await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    point = await flow.async_step_switch_program_day({"point": "0"})
    levels = [
        item["value"]
        for item in point["data_schema"].schema["level"].config["options"]
    ]
    assert levels == ["high", "off", "low"]
    await flow.async_step_switch_program_point({"time": "05:30", "level": "off"})
    await flow.async_step_switch_program_day({"point": "done"})
    confirm = await flow.async_step_switch_program_week({"action": "review"})
    assert confirm["step_id"] == "switch_program_confirm"
    assert gateway.puts == 0
    result = await flow.async_step_switch_program_confirm({})
    assert await outcome(hass, result) == "switch_program_saved"
    ((_path, body),) = gateway.value_puts
    assert body[0] == {"d": "Mo", "t": 330, "dhw": "off"}
    del day
