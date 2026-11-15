"""TP-17: add and delete switch points of an RRC heating program.

One day is written at a time ({d, t, Th} for every point of the day), at
most six points per day (the app's limit), the last point of a day stays,
read-back of the week. Every test counts the PUT requests.
"""

from __future__ import annotations

import copy
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
from custom_components.bosch_buderus_heating.rrc_programs import (
    APP_MAX_POINTS_PER_DAY,
    RrcProgramService,
    day_body,
    day_change,
    parse_schedule,
)
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from custom_components.bosch_buderus_heating.write_policies import ContractRejected
from tests import test_dynamic_controls
from tests.helpers.options_menu import outcome
from tests.program_kind_helpers import CountingGateway, payloads, resource

WEEK = "/programs/pg3/week"
ITEMS = payloads("rrc/day-programs.json")


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


class RrcGateway(CountingGateway):
    """A day write also changes the week, as on the controller."""

    async def put_value(self, gateway, path, value):
        await super().put_value(gateway, path, value)
        if (
            path.startswith("/programs/pg3/")
            and path != WEEK
            and not self.ignore_writes
        ):
            day = value[0]["d"] if value else None
            others = [item for item in self.payloads[WEEK]["value"] if item["d"] != day]
            self.payloads[WEEK]["value"] = others + copy.deepcopy(value)


@pytest.fixture
def gateway(environment):
    coordinator, client, _ = environment
    coordinator._rrc_program_service = RrcProgramService(client, sleep=AsyncMock())
    return RrcGateway(coordinator, client, ITEMS)


def schedule():
    parsed = parse_schedule(resource(ITEMS[WEEK]))
    assert parsed is not None and parsed.editable
    return parsed


def test_fixture_is_synthetic():
    from tests.program_kind_helpers import load

    assert load("rrc/day-programs.json")["provenance"]["kind"] == "synthetic"


async def test_adding_a_point_writes_the_whole_day_once(gateway):
    points = day_change(schedule(), "Mo", add=(720, 15.0))
    await gateway.coordinator.async_write_rrc_day(gateway.read(WEEK), "Mo", points)
    ((path, body),) = gateway.value_puts
    assert path == "/programs/pg3/monday"
    assert body == [
        {"d": "Mo", "t": 360, "Th": 21.0},
        {"d": "Mo", "t": 480, "Th": 17.0},
        {"d": "Mo", "t": 720, "Th": 15.0},
        {"d": "Mo", "t": 1320, "Th": 16.0},
    ]
    week = parse_schedule(gateway.coordinator.data[WEEK].resource)
    assert [point.minute for point in week.day_points("Mo")] == [360, 480, 720, 1320]


def test_seventh_point_is_refused_before_anything_is_read():
    with pytest.raises(ContractRejected, match="rrc_point_limit"):
        day_change(schedule(), "Tu", add=(900, 20.0))
    assert APP_MAX_POINTS_PER_DAY == 6
    too_many = [(minute, 20.0) for minute in range(0, 35, 5)]
    with pytest.raises(ContractRejected, match="rrc_point_limit"):
        day_body(schedule(), "Th", too_many)


@pytest.mark.parametrize(
    ("point", "error"),
    [
        ((722, 15.0), WriteValidationError),
        ((480, 15.0), ContractRejected),
        ((720, 4.5), WriteValidationError),
        ((720, 30.5), WriteValidationError),
        ((720, 21.2), WriteValidationError),
        ((720, float("nan")), WriteValidationError),
    ],
)
async def test_invalid_new_points_send_nothing(gateway, point, error):
    with pytest.raises(error):
        await gateway.coordinator.async_write_rrc_day(
            gateway.read(WEEK), "Mo", day_change(schedule(), "Mo", add=point)
        )
    assert gateway.puts == 0


async def test_deleting_a_point_writes_the_rest_of_the_day(gateway):
    index = next(
        position
        for position, point in enumerate(schedule().points)
        if point.day == "Mo" and point.minute == 480
    )
    points = day_change(schedule(), "Mo", delete=index)
    await gateway.coordinator.async_write_rrc_day(gateway.read(WEEK), "Mo", points)
    ((_path, body),) = gateway.value_puts
    assert body == [
        {"d": "Mo", "t": 360, "Th": 21.0},
        {"d": "Mo", "t": 1320, "Th": 16.0},
    ]


async def test_last_point_of_a_day_stays(gateway):
    index = next(
        position
        for position, point in enumerate(schedule().points)
        if point.day == "We"
    )
    with pytest.raises(ContractRejected, match="rrc_point_last"):
        await gateway.coordinator.async_write_rrc_day(
            gateway.read(WEEK), "We", day_change(schedule(), "We", delete=index)
        )
    assert gateway.puts == 0


async def test_a_day_without_points_gets_its_first_point(gateway):
    await gateway.coordinator.async_write_rrc_day(
        gateway.read(WEEK), "Th", day_change(schedule(), "Th", add=(720, 15.0))
    )
    assert gateway.value_puts == [
        ("/programs/pg3/thursday", [{"d": "Th", "t": 720, "Th": 15.0}])
    ]


async def test_names_are_left_out_and_ignored(gateway):
    points = day_change(schedule(), "Fr", add=(720, 15.0))
    await gateway.coordinator.async_write_rrc_day(gateway.read(WEEK), "Fr", points)
    ((_path, body),) = gateway.value_puts
    assert all(set(item) == {"d", "t", "Th"} for item in body)


async def test_day_changed_in_the_app_sends_nothing(gateway):
    shown = gateway.read(WEEK)
    gateway.change(
        "/programs/pg3/monday",
        value=[{"d": "Mo", "t": 365, "Th": 21.0}],
    )
    with pytest.raises(WriteValidationError):
        await gateway.coordinator.async_write_rrc_day(
            shown, "Mo", day_change(schedule(), "Mo", add=(720, 15.0))
        )
    assert gateway.puts == 0


def test_not_editable_program_offers_no_change():
    item = copy.deepcopy(ITEMS[WEEK])
    item["value"][0]["t"] = 362
    parsed = parse_schedule(resource(item))
    assert parsed is not None and not parsed.editable
    with pytest.raises(WriteValidationError):
        day_body(parsed, "Mo", [(720, 15.0)])


@pytest.mark.parametrize("status", [400, 403, 404])
async def test_refused_day_is_not_sent_again(gateway, status):
    gateway.client.put_resource_value.side_effect = UnexpectedHttpStatus(status)
    with pytest.raises(UnexpectedHttpStatus):
        await gateway.coordinator.async_write_rrc_day(
            gateway.read(WEEK), "Mo", day_change(schedule(), "Mo", add=(720, 15.0))
        )
    assert gateway.client.put_resource_value.await_count == 1


async def test_server_error_and_mismatch_are_not_confirmed(gateway):
    gateway.ignore_writes = True
    with pytest.raises(WriteNotConfirmed):
        await gateway.coordinator.async_write_rrc_day(
            gateway.read(WEEK), "Mo", day_change(schedule(), "Mo", add=(720, 15.0))
        )
    assert len(gateway.value_puts) == 1
    # Not current until the next reading; a later reading may still confirm.
    assert gateway.coordinator.data[WEEK].available is False


async def test_cloud_pause_sends_no_day(gateway):
    gateway.coordinator._cloud_backoff_until = monotonic() + 600
    with pytest.raises(RateLimited):
        await gateway.coordinator.async_write_rrc_day(
            gateway.read(WEEK), "Mo", day_change(schedule(), "Mo", add=(720, 15.0))
        )
    assert gateway.puts == 0


# The dialog --------------------------------------------------------------------


def make_flow(hass, gateway):
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
    return flow


async def open_day(flow, day="mo"):
    first = await flow.async_step_rrc_programs()
    options = next(iter(first["data_schema"].schema.values())).config["options"]
    week = await flow.async_step_rrc_programs({"program": options[1]["value"]})
    assert week["step_id"] == "rrc_schedule"
    assert "week_view" in {str(key) for key in week["data_schema"].schema}
    return await flow.async_step_rrc_schedule({"day": day})


async def test_dialog_adds_a_point_after_before_and_after(hass, gateway):
    flow = make_flow(hass, gateway)
    day = await open_day(flow)
    options = [
        item["value"] for item in day["data_schema"].schema["point"].config["options"]
    ]
    assert options[-1] == "add"
    add = await flow.async_step_rrc_day({"point": "add"})
    assert add["step_id"] == "rrc_add"
    defaults = {str(key): key.default() for key in add["data_schema"].schema}
    assert defaults == {"time": "12:00", "temperature": 15.0}
    confirm = await flow.async_step_rrc_add({"time": "12:00", "temperature": 15.0})
    assert confirm["step_id"] == "rrc_confirm"
    assert "12:00" in confirm["description_placeholders"]["after"]
    assert "12:00" not in confirm["description_placeholders"]["before"]
    assert gateway.puts == 0
    result = await flow.async_step_rrc_confirm({})
    assert await outcome(hass, result) == "rrc_program_saved"
    assert len(gateway.value_puts) == 1


async def test_dialog_offers_no_add_on_a_full_day(hass, gateway):
    flow = make_flow(hass, gateway)
    day = await open_day(flow, "tu")
    options = [
        item["value"] for item in day["data_schema"].schema["point"].config["options"]
    ]
    assert "add" not in options
    refused = await flow.async_step_rrc_day({"point": "add"})
    assert refused["errors"] == {"base": "rrc_point_limit"}
    assert gateway.puts == 0


async def test_dialog_deletes_a_point_and_keeps_the_last(hass, gateway):
    flow = make_flow(hass, gateway)
    await open_day(flow)
    point = await flow.async_step_rrc_day({"point": "1"})
    assert "delete" in {str(key) for key in point["data_schema"].schema}
    confirm = await flow.async_step_rrc_point(
        {"time": "08:00", "temperature": 17, "delete": True}
    )
    assert confirm["step_id"] == "rrc_confirm"
    result = await flow.async_step_rrc_confirm({})
    assert await outcome(hass, result) == "rrc_program_saved"
    ((_path, body),) = gateway.value_puts
    assert [item["t"] for item in body] == [360, 1320]
    # A single point offers no delete field.
    flow = make_flow(hass, gateway)
    await open_day(flow, "we")
    index = next(
        str(position)
        for position, point in enumerate(
            parse_schedule(gateway.coordinator.data[WEEK].resource).points
        )
        if point.day == "We"
    )
    point = await flow.async_step_rrc_day({"point": index})
    assert "delete" not in {str(key) for key in point["data_schema"].schema}
