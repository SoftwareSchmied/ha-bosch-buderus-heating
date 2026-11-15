"""Add, delete and guard branches of the room controller program dialog."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    RateLimited,
    RequestTimeout,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.rrc_programs import parse_schedule
from custom_components.bosch_buderus_heating.write_policies import ContractRejected
from custom_components.bosch_buderus_heating.writes import WriteBusy
from tests.helpers.options_menu import outcome
from tests.test_rrc import WEEK
from tests.test_rrc_flow import environment as environment
from tests.test_rrc_flow import make_flow, open_point

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def _open_day(flow, day="mo"):
    first = await flow.async_step_rrc_programs()
    options = next(iter(first["data_schema"].schema.values())).config["options"]
    await flow.async_step_rrc_programs({"program": options[1]["value"]})
    return await flow.async_step_rrc_schedule({"day": day})


def _is_form(shown, step):
    assert shown["type"] is FlowResultType.FORM, shown
    assert shown["step_id"] == step, shown


@pytest.mark.parametrize(
    "step",
    ["rrc_schedule", "rrc_day", "rrc_add", "rrc_point", "rrc_confirm"],
)
async def test_a_page_without_a_chosen_program_returns_to_the_program_list(
    hass, environment, step
) -> None:
    flow, coordinator = make_flow(hass, environment)
    coordinator.async_write_rrc_day = AsyncMock()
    coordinator.async_edit_rrc_program = AsyncMock()
    shown = await getattr(flow, f"async_step_{step}")()
    _is_form(shown, "rrc_programs")
    coordinator.async_write_rrc_day.assert_not_awaited()
    coordinator.async_edit_rrc_program.assert_not_awaited()


async def test_an_unknown_weekday_or_point_ends_the_dialog_without_a_write(
    hass, environment
) -> None:
    flow, coordinator = make_flow(hass, environment)
    coordinator.async_write_rrc_day = AsyncMock()
    await _open_day(flow)
    shown = await flow.async_step_rrc_schedule({"day": "xx"})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "no_rrc_schedules"
    await flow.async_step_rrc_schedule({"day": "mo"})
    shown = await flow.async_step_rrc_day({"point": "99"})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "no_rrc_schedules"
    coordinator.async_write_rrc_day.assert_not_awaited()


@pytest.mark.parametrize("step", ["rrc_point", "rrc_confirm"])
async def test_a_program_whose_controller_is_gone_cannot_be_changed(
    hass, environment, step
) -> None:
    flow, coordinator = make_flow(hass, environment)
    coordinator.async_edit_rrc_program = AsyncMock()
    await open_point(flow)
    await flow.async_step_rrc_point({"time": "07:10", "temperature": 22})
    flow._entry.runtime_data.coordinators = ()
    shown = await getattr(flow, f"async_step_{step}")({})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "no_rrc_schedules"
    coordinator.async_edit_rrc_program.assert_not_awaited()


async def test_a_confirmation_without_a_pending_change_sends_nothing(
    hass, environment
) -> None:
    flow, coordinator = make_flow(hass, environment)
    coordinator.async_edit_rrc_program = AsyncMock()
    await open_point(flow)
    # The point page was shown, but nothing was entered yet.
    shown = await flow.async_step_rrc_confirm({})
    _is_form(shown, "rrc_programs")
    coordinator.async_edit_rrc_program.assert_not_awaited()


async def test_the_confirmation_page_is_shown_again_without_an_answer(
    hass, environment
) -> None:
    flow, coordinator = make_flow(hass, environment)
    coordinator.async_edit_rrc_program = AsyncMock()
    await open_point(flow)
    await flow.async_step_rrc_point({"time": "07:10", "temperature": 22})
    shown = await flow.async_step_rrc_confirm()
    _is_form(shown, "rrc_confirm")
    assert shown["description_placeholders"]["after"] == "07:10 (22 °C)"
    coordinator.async_edit_rrc_program.assert_not_awaited()


async def test_a_day_with_a_free_slot_offers_the_next_free_time(
    hass, environment
) -> None:
    flow, _coordinator = make_flow(hass, environment)
    await _open_day(flow)
    await flow.async_step_rrc_schedule({"day": "mo"})
    shown = await flow.async_step_rrc_day({"point": "add"})
    _is_form(shown, "rrc_add")
    suggested = next(iter(shown["data_schema"].schema)).default()
    schedule = flow._rrc_choice.schedule
    taken = {point.minute for point in schedule.day_points("Mo")}
    hours, minutes = map(int, suggested.split(":"))
    assert hours * 60 + minutes not in taken


@pytest.mark.parametrize(
    "values,key",
    [
        ({"time": "12:00", "temperature": 99}, "rrc_point_invalid"),
        ({"time": "07:05", "temperature": 20}, "rrc_point_duplicate"),
    ],
)
async def test_a_point_that_the_controller_would_refuse_is_not_offered_for_sending(
    hass, environment, values, key
) -> None:
    flow, coordinator = make_flow(hass, environment)
    coordinator.async_write_rrc_day = AsyncMock()
    await _open_day(flow)
    await flow.async_step_rrc_schedule({"day": "mo"})
    await flow.async_step_rrc_day({"point": "add"})
    shown = await flow.async_step_rrc_add(values)
    _is_form(shown, "rrc_add")
    assert shown["errors"] == {"base": key}
    coordinator.async_write_rrc_day.assert_not_awaited()


def _single_point_day(schedule):
    for day in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"):
        if len(schedule.day_points(day)) == 1:
            return day
    return None


async def test_the_last_point_of_a_day_cannot_be_deleted(hass, environment) -> None:
    flow, coordinator = make_flow(hass, environment)
    coordinator.async_write_rrc_day = AsyncMock()
    await _open_day(flow)
    schedule = flow._rrc_choice.schedule
    day = _single_point_day(schedule)
    if day is None:
        pytest.skip("the synthetic week has no day with a single point")
    await flow.async_step_rrc_schedule({"day": day.lower()})
    index = next(i for i, p in enumerate(schedule.points) if p.day == day)
    await flow.async_step_rrc_day({"point": str(index)})
    shown = await flow.async_step_rrc_point({"delete": True})
    _is_form(shown, "rrc_point")
    assert shown["errors"] == {"base": "rrc_point_last"}
    coordinator.async_write_rrc_day.assert_not_awaited()


async def _confirm_added_point(flow):
    await _open_day(flow)
    await flow.async_step_rrc_schedule({"day": "mo"})
    await flow.async_step_rrc_day({"point": "add"})
    shown = await flow.async_step_rrc_add({"time": "12:00", "temperature": 20})
    _is_form(shown, "rrc_confirm")
    return shown


async def test_an_added_point_is_sent_once_after_the_confirmation(
    hass, environment
) -> None:
    flow, coordinator = make_flow(hass, environment)
    coordinator.async_write_rrc_day = AsyncMock()
    shown = await _confirm_added_point(flow)
    assert "12:00 (20 °C)" in shown["description_placeholders"]["after"]
    # Shown again without an answer: nothing is sent.
    again = await flow.async_step_rrc_confirm()
    _is_form(again, "rrc_confirm")
    coordinator.async_write_rrc_day.assert_not_awaited()
    result = await flow.async_step_rrc_confirm({})
    assert await outcome(hass, result) == "rrc_program_saved"
    coordinator.async_write_rrc_day.assert_awaited_once()


@pytest.mark.parametrize(
    "error,key",
    [
        (RateLimited(60), "write_rate_limited"),
        (AuthenticationError("expired"), "write_authentication_failed"),
        (ContractRejected("rrc_point_limit"), "rrc_point_limit"),
        (ContractRejected("other_reason"), "rrc_changed"),
        (WriteValidationError("changed"), "rrc_changed"),
        (WriteNotConfirmed("lost"), "rrc_not_confirmed"),
        (WriteBusy("busy"), "write_busy"),
        (RequestTimeout("lost"), "rrc_failed"),
    ],
)
async def test_a_failed_day_write_is_explained_and_can_be_confirmed_again(
    hass, environment, error, key
) -> None:
    flow, coordinator = make_flow(hass, environment)
    coordinator.async_write_rrc_day = AsyncMock(side_effect=error)
    await _confirm_added_point(flow)
    shown = await flow.async_step_rrc_confirm({})
    _is_form(shown, "rrc_confirm")
    assert shown["errors"] == {"base": key}
    coordinator.async_write_rrc_day.assert_awaited_once()
    # The same change can be confirmed again; it is sent as a new request.
    coordinator.async_write_rrc_day.side_effect = None
    result = await flow.async_step_rrc_confirm({})
    assert await outcome(hass, result) == "rrc_program_saved"
    assert coordinator.async_write_rrc_day.await_count == 2


async def test_a_day_confirmation_without_its_day_returns_to_the_programs(
    hass, environment
) -> None:
    flow, coordinator = make_flow(hass, environment)
    coordinator.async_write_rrc_day = AsyncMock()
    await _confirm_added_point(flow)
    flow._rrc_pending = None
    shown = await flow.async_step_rrc_confirm({})
    _is_form(shown, "rrc_programs")
    coordinator.async_write_rrc_day.assert_not_awaited()


async def test_the_schedule_of_a_read_only_week_is_not_listed_for_editing(
    hass, environment
) -> None:
    flow, coordinator = make_flow(hass, environment)
    snapshot = coordinator.data[WEEK]
    assert parse_schedule(snapshot.resource) is not None
    # A week the integration cannot read is left out of the program list.
    coordinator.data[WEEK] = replace(
        snapshot, resource=replace(snapshot.resource, value="unreadable")
    )
    assert flow._rrc_choices() == {}
