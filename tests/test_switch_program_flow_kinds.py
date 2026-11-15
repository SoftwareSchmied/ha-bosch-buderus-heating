"""The program dialog for the further kinds: number field and weekly view."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating.config_flow import BoschBuderusOptionsFlow
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from custom_components.bosch_buderus_heating.switch_program_service import (
    SwitchProgramService,
)
from tests import test_dynamic_controls
from tests.helpers.options_menu import outcome
from tests.program_kind_helpers import CountingGateway, payloads


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


def make(hass, environment, name):
    coordinator, client, _ = environment
    coordinator._switch_program_service = SwitchProgramService(
        client, sleep=AsyncMock()
    )
    gateway = CountingGateway(coordinator, client, payloads(name))
    entry = coordinator.config_entry
    entry.runtime_data = BoschBuderusRuntimeData(
        client=client,
        token_manager=AsyncMock(),
        gateways=(coordinator.gateway,),
        coordinators=(coordinator,),
    )
    flow = BoschBuderusOptionsFlow(entry)
    flow.hass = hass
    return flow, gateway


async def choose(flow, path):
    first = await flow.async_step_switch_programs()
    options = first["data_schema"].schema["program"].config["options"]
    key = next(item["value"] for item in options if item["value"].endswith(path))
    return await flow.async_step_switch_programs({"program": key})


async def test_zone_program_point_uses_a_number_field(hass, environment):
    flow, gateway = make(hass, environment, "connectkey/zone-switch-programs.json")
    week = await choose(flow, "/zones/zone1/switchPrograms/heating")
    assert week["step_id"] == "switch_program_week"
    view = week["data_schema"].schema
    assert "week_view" in {str(key) for key in view}
    selector = next(value for key, value in view.items() if str(key) == "week_view")
    config = selector.serialize()["selector"]["bosch-buderus-schedule-week"]
    assert len(config["days"]) == 7
    assert "21 °C" in week["description_placeholders"]["week"]
    await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    point = await flow.async_step_switch_program_day({"point": "0"})
    assert point["step_id"] == "switch_program_point_value"
    fields = {str(key): value for key, value in point["data_schema"].schema.items()}
    assert set(fields) == {"time", "value", "delete"}
    assert fields["value"].config["min"] == 5.0
    assert fields["value"].config["step"] == 0.5
    assert point["description_placeholders"]["minimum"] == "5"
    refused = await flow.async_step_switch_program_point_value(
        {"time": "06:00", "value": 21.2}
    )
    assert refused["errors"] == {"value": "switch_program_value"}
    await flow.async_step_switch_program_point_value({"time": "06:00", "value": 21.5})
    await flow.async_step_switch_program_day({"point": "done"})
    confirm = await flow.async_step_switch_program_week({"action": "review"})
    assert confirm["step_id"] == "switch_program_confirm"
    assert gateway.puts == 0
    result = await flow.async_step_switch_program_confirm({})
    assert await outcome(hass, result) == "switch_program_saved"
    ((_path, body),) = gateway.raw_puts
    assert body[0] == {"dayOfWeek": "Mo", "setpoint": "21.5", "time": 360}


async def test_zone_program_without_step_is_not_offered(hass, environment):
    flow, _gateway = make(hass, environment, "connectkey/zone-switch-programs.json")
    first = await flow.async_step_switch_programs()
    values = [item["value"] for item in first["data_schema"].schema["program"].config["options"]]
    assert not any(value.endswith("/zones/zone2/switchPrograms/heating") for value in values)


async def test_absolute_program_offers_off_only_where_read(hass, environment):
    flow, _gateway = make(hass, environment, "connectkey/switch-programs-absolute.json")
    await choose(flow, "/heatingCircuits/hc1/switchPrograms/A")
    await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    point = await flow.async_step_switch_program_day({"point": "1"})
    fields = {str(key): key for key in point["data_schema"].schema}
    assert "off" in fields and fields["off"].default() is True
    flow, _gateway = make(hass, environment, "connectkey/switch-programs-absolute.json")
    await choose(flow, "/heatingCircuits/hc1/switchPrograms/B")
    await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    point = await flow.async_step_switch_program_day({"point": "0"})
    assert "off" not in {str(key) for key in point["data_schema"].schema}


async def test_cooling_program_offers_on_and_off(hass, environment):
    flow, _gateway = make(hass, environment, "connectkey/cooling-switch-programs.json")
    week = await choose(flow, "/heatingCircuits/hc1/cooling/switchPrograms/A")
    assert week["step_id"] == "switch_program_week"
    await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    point = await flow.async_step_switch_program_day({"point": "0"})
    options = [item["value"] for item in point["data_schema"].schema["level"].config["options"]]
    assert options == ["on", "off"]


async def test_wddw2_program_uses_whole_degrees_and_off(hass, environment):
    flow, gateway = make(hass, environment, "wddw2/switch-program.json")
    await choose(flow, "/dhwCircuits/dhw1/switchPrograms/program1")
    await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    point = await flow.async_step_switch_program_day({"point": "0"})
    fields = {str(key): value for key, value in point["data_schema"].schema.items()}
    assert fields["value"].config["step"] == 1.0 and "off" in fields
    refused = await flow.async_step_switch_program_point_value(
        {"time": "06:00", "value": 50.5}
    )
    assert refused["errors"] == {"value": "switch_program_value"}
    await flow.async_step_switch_program_point_value({"time": "06:00", "value": 50})
    await flow.async_step_switch_program_day({"point": "done"})
    await flow.async_step_switch_program_week({"action": "review"})
    await flow.async_step_switch_program_confirm({})
    ((_path, body),) = gateway.object_puts
    assert body["switchPoints"][0] == {"dayOfWeek": "Mo", "setpoint": "50", "time": 360}
