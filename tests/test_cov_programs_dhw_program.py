"""Hot water week program: what is refused, and writes that must not happen."""

# Fixtures are imported from the module that defines them.
# ruff: noqa: F811

from __future__ import annotations

import copy

import pytest

from custom_components.bosch_buderus_heating.pointt import (
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.rrc_dhw_program import (
    edit_issue,
    editable,
    encode_week,
    points_of,
)
from custom_components.bosch_buderus_heating.switch_programs import SwitchPoint
from custom_components.bosch_buderus_heating.write_policies import ContractRejected
from tests.program_kind_helpers import resource
from tests.test_rrc_dhw_program import (  # noqa: F401 - fixtures
    ITEMS,
    PATH,
    environment,
    gateway,
    program,
    with_point,
)


def item(**changes):
    data = copy.deepcopy(ITEMS[PATH])
    data.update(changes)
    return data


def mondays(times):
    """The fixture week with Monday replaced by points at ``times``."""
    value = [point for point in ITEMS[PATH]["value"] if point["d"] != "Mo"]
    first = [{"d": "Mo", "t": minute, "dhw": "high"} for minute in times]
    return [*first, *value]


def test_no_resource_and_other_paths_are_not_programs():
    assert edit_issue(None) == "not_a_program"
    other = item(id="/dhwCircuits/dhw1/operationMode")
    assert edit_issue(resource(other)) == "not_a_program"


def test_unsorted_week_is_read_only():
    assert edit_issue(resource(item(value=mondays([480, 330])))) == "unsorted"


def test_minute_off_the_five_minute_grid_is_read_only():
    assert edit_issue(resource(item(value=mondays([331, 480])))) == "off_grid_minute"


def test_more_than_six_points_a_day_is_read_only():
    times = [330, 400, 480, 600, 660, 720, 780]
    assert edit_issue(resource(item(value=mondays(times)))) == "too_many_per_day"
    assert editable(resource(item(value=mondays(times)))) is None


def test_unreadable_week_has_no_points_and_keeps_levels_when_encoded():
    broken = resource(item(value="garbage"))
    assert edit_issue(broken) == "program_unreadable"
    assert points_of(broken) == ()
    body = encode_week(
        broken, [SwitchPoint(0, 600, "off"), SwitchPoint(0, 300, "high")]
    )
    assert body == [
        {"d": "Mo", "t": 300, "dhw": "high"},
        {"d": "Mo", "t": 600, "dhw": "off"},
    ]


async def test_unwritable_week_is_refused_before_any_request(gateway):
    gateway.change(PATH, writeable=False)
    shown = gateway.read(PATH)
    with pytest.raises(WriteValidationError, match="cannot be changed"):
        await gateway.coordinator.async_write_rrc_dhw_program(
            shown, with_point(program().points, 0, 600, "off")
        )
    assert gateway.puts == 0
    assert gateway.gets == []


async def test_invalid_week_is_refused_before_any_request(gateway):
    shown = program()
    emptied = tuple(point for point in shown.points if point.day != 3)
    with pytest.raises(ContractRejected, match="program_day_emptied"):
        await gateway.coordinator.async_write_rrc_dhw_program(
            gateway.read(PATH), emptied
        )
    assert gateway.puts == 0
    assert gateway.gets == []


async def test_unchanged_week_sends_nothing(gateway):
    shown = program()
    await gateway.coordinator.async_write_rrc_dhw_program(
        gateway.read(PATH), shown.points
    )
    assert gateway.puts == 0
    assert gateway.gets == [PATH]


async def test_week_that_becomes_unusable_meanwhile_sends_nothing(gateway):
    shown = gateway.read(PATH)
    gateway.change(PATH, used=False)
    with pytest.raises(ContractRejected, match="program_changed"):
        await gateway.coordinator.async_write_rrc_dhw_program(
            shown, with_point(program().points, 0, 600, "off")
        )
    assert gateway.puts == 0


async def test_unreadable_read_back_is_not_taken_for_a_confirmation(gateway):
    async def garble(_gateway, path, value):
        gateway.value_puts.append((path, copy.deepcopy(value)))
        gateway.payloads[path]["value"] = "garbage"

    gateway.client.put_resource_value.side_effect = garble
    with pytest.raises(WriteNotConfirmed):
        await gateway.coordinator.async_write_rrc_dhw_program(
            gateway.read(PATH), with_point(program().points, 0, 600, "off")
        )
    assert len(gateway.value_puts) == 1


async def test_accepted_put_with_unreadable_answer_is_decided_by_reading(gateway):
    from custom_components.bosch_buderus_heating.pointt import ProtocolError

    async def accepted(_gateway, path, value):
        gateway.value_puts.append((path, copy.deepcopy(value)))
        gateway.payloads[path]["value"] = copy.deepcopy(value)
        raise ProtocolError("answer is not JSON")

    gateway.client.put_resource_value.side_effect = accepted
    await gateway.coordinator.async_write_rrc_dhw_program(
        gateway.read(PATH), with_point(program().points, 0, 600, "off")
    )
    assert len(gateway.value_puts) == 1
    assert {"d": "Mo", "t": 600, "dhw": "off"} in gateway.value_puts[0][1]
