"""RRC programs: refused changes that must not cost a request."""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_buderus_heating.pointt import WriteValidationError
from custom_components.bosch_buderus_heating.rrc_programs import (
    RrcProgramService,
    day_change,
    day_shows,
    edit_shows,
    parse_schedule,
)
from tests.program_kind_helpers import payloads, resource

WEEK = "/programs/pg3/week"
ITEMS = payloads("rrc/day-programs.json")


def week_resource(**changes):
    item = copy.deepcopy(ITEMS[WEEK])
    item.update(changes)
    return resource(item)


def service() -> tuple[RrcProgramService, MagicMock]:
    client = MagicMock()
    client.get_resource = AsyncMock()
    client.put_resource_value = AsyncMock()
    return RrcProgramService(client, sleep=AsyncMock(), delays=(0.0,)), client


def schedule():
    parsed = parse_schedule(week_resource())
    assert parsed is not None and parsed.editable
    return parsed


@pytest.mark.parametrize("index", [-1, 99, 4])
def test_deleting_a_point_that_is_not_on_the_day_is_refused(index):
    # Index 4 belongs to Tuesday, not Monday.
    with pytest.raises(WriteValidationError):
        day_change(schedule(), "Mo", delete=index)


def test_a_reading_of_another_shape_never_shows_a_day():
    shows = day_shows(WEEK, "Mo", ())
    assert shows(week_resource(value="garbage")) is False
    assert (
        shows(resource({**copy.deepcopy(ITEMS[WEEK]), "id": "/programs/pg3/week2"}))
        is False
    )


def test_a_change_to_an_unreadable_week_has_nothing_to_confirm():
    assert edit_shows(week_resource(value="garbage"), 0, 365, 21.0) is None
    # An index that does not exist is refused the same way.
    assert edit_shows(week_resource(), 999, 365, 21.0) is None


@pytest.mark.parametrize(
    ("path", "program"),
    [
        ("/programs/pg3/week", 3),  # not the selection
        ("/zones/zn1/clockProgram", True),  # a flag is no program
        ("/zones/zn1/clockProgram", "3"),
    ],
)
async def test_selecting_with_an_invalid_target_sends_and_reads_nothing(path, program):
    rrc, client = service()
    with pytest.raises(WriteValidationError):
        await rrc.select("gw", path, program)
    client.get_resource.assert_not_awaited()
    client.put_resource_value.assert_not_awaited()


async def test_changing_an_unreadable_week_sends_and_reads_nothing():
    rrc, client = service()
    broken = week_resource(value="garbage")
    with pytest.raises(WriteValidationError):
        await rrc.edit("gw", broken, 0, 365, 21.0)
    with pytest.raises(WriteValidationError):
        await rrc.write_day("gw", broken, "Mo", [(360, 21.0)])
    client.get_resource.assert_not_awaited()
    client.put_resource_value.assert_not_awaited()


async def test_changing_a_resource_that_is_no_week_sends_nothing():
    rrc, client = service()
    other = resource({**copy.deepcopy(ITEMS[WEEK]), "id": "/programs/pg3/monday"})
    with pytest.raises(WriteValidationError):
        await rrc.edit("gw", other, 0, 365, 21.0)
    with pytest.raises(WriteValidationError):
        await rrc.write_day("gw", other, "Mo", [(360, 21.0)])
    client.put_resource_value.assert_not_awaited()
