"""The send window and a restart on the newer program paths (synthetic).

The names of the weekly programs, a day of a room controller's program and
its hot water week: a write whose time is too short sends nothing and keeps
the reading; a restart between the request and its read-back leaves the
reading not current until read again, and nothing is sent again. A name the
gateway does not use is not written, and an existing program sensor that
was disabled stays disabled. Every value is constructed.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import write_window
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.rrc_programs import (
    RrcProgramService,
    day_change,
)
from custom_components.bosch_buderus_heating.switch_program_service import (
    SwitchProgramService,
)
from custom_components.bosch_buderus_heating.switch_programs import SwitchPoint
from custom_components.bosch_buderus_heating.write_policies import ContractRejected
from custom_components.bosch_buderus_heating.writes import WriteBusy
from tests import test_dynamic_controls
from tests.program_kind_helpers import CountingGateway, load, payloads
from tests.test_rrc_dhw_program import ITEMS as DHW_ITEMS
from tests.test_rrc_dhw_program import PATH as DHW_WEEK
from tests.test_rrc_dhw_program import program as dhw_program
from tests.test_rrc_point_add_delete import ITEMS as DAY_ITEMS
from tests.test_rrc_point_add_delete import WEEK as DAY_WEEK
from tests.test_rrc_point_add_delete import RrcGateway, schedule
from tests.test_switch_programs import HEATING, _program_sensor, _resource

NAME = "/heatingCircuits/hc1/switchPrograms/nameA"


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


def _name(environment):
    coordinator, client, _ = environment
    coordinator._switch_program_service = SwitchProgramService(
        client, sleep=AsyncMock()
    )
    case = copy.deepcopy(load("connectkey/switch-program-names.json")["cases"])
    gateway = CountingGateway(coordinator, client, {NAME: case["ascii_plain"]})

    async def write():
        await coordinator.async_write_program_name(gateway.read(NAME), "Home")

    return gateway, NAME, write, coordinator._switch_program_service


def _day(environment):
    coordinator, client, _ = environment
    coordinator._rrc_program_service = RrcProgramService(client, sleep=AsyncMock())
    gateway = RrcGateway(coordinator, client, copy.deepcopy(DAY_ITEMS))
    points = day_change(schedule(), "Mo", add=(720, 15.0))

    async def write():
        await coordinator.async_write_rrc_day(gateway.read(DAY_WEEK), "Mo", points)

    return gateway, DAY_WEEK, write, coordinator._rrc_program_service


def _dhw(environment):
    coordinator, client, _ = environment
    coordinator._rrc_program_service = RrcProgramService(client, sleep=AsyncMock())
    gateway = CountingGateway(coordinator, client, copy.deepcopy(DHW_ITEMS))
    week = tuple(
        SwitchPoint(point.day, point.minute, "off")
        if (point.day, point.minute) == (0, 330)
        else point
        for point in dhw_program().points
    )

    async def write():
        await coordinator.async_write_rrc_dhw_program(gateway.read(DHW_WEEK), week)

    return gateway, DHW_WEEK, write, coordinator._rrc_program_service


WRITERS = {"program_name": _name, "rrc_day": _day, "rrc_dhw_week": _dhw}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize("writer", WRITERS)
async def test_too_short_a_window_sends_nothing_and_keeps_the_reading(
    environment, writer
):
    gateway, path, write, _service = WRITERS[writer](environment)
    clock = _Clock()
    original = gateway.client.get_resource.side_effect

    async def slow(gateway_id, read_path, **kwargs):
        # The check before the request takes almost all of the time.
        clock.now += 10_000.0
        return await original(gateway_id, read_path, **kwargs)

    gateway.client.get_resource.side_effect = slow
    with (
        patch.object(write_window, "_now", clock),
        pytest.raises(WriteBusy),
    ):
        await write()
    assert gateway.puts == 0
    snapshot = gateway.coordinator.data[path]
    assert snapshot.available and snapshot.freshness is Freshness.FRESH
    assert not gateway.coordinator._update_lock.locked()


@pytest.mark.parametrize("writer", WRITERS)
async def test_a_restart_before_the_read_back_ends_the_reading_once(
    environment, writer
):
    """Home Assistant stops while the change waits for its read-back: the
    request went out once, the reading is no longer current, it is read
    with the next cycle, and nothing is sent again."""
    gateway, path, write, service = WRITERS[writer](environment)
    service.sleep = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await write()
    assert gateway.puts == 1
    snapshot = gateway.coordinator.data[path]
    assert not snapshot.available
    assert snapshot.freshness is Freshness.STALE and snapshot.ended
    assert path in gateway.coordinator._urgent_reads
    assert not gateway.coordinator._update_lock.locked()


@pytest.mark.parametrize("flag", ["used", "available"])
@pytest.mark.parametrize("on_gateway", [False, True])
async def test_a_name_the_gateway_does_not_use_is_not_written(
    environment, flag, on_gateway
):
    gateway, _path, _write, _service = _name(environment)
    if on_gateway:
        shown = gateway.read(NAME)
        gateway.change(NAME, **{flag: False})
    else:
        gateway.change(NAME, **{flag: False})
        shown = gateway.read(NAME)
    with pytest.raises(ContractRejected):
        await gateway.coordinator.async_write_program_name(shown, "Home")
    assert gateway.puts == 0


@pytest.mark.parametrize(
    "disabled_by",
    [er.RegistryEntryDisabler.INTEGRATION, er.RegistryEntryDisabler.USER],
)
async def test_an_existing_disabled_program_sensor_is_not_enabled(hass, disabled_by):
    """A writable program's sensor is enabled when first created; one that
    exists already (for example disabled by default before) keeps its
    setting."""
    sensor = _program_sensor(hass, _resource(HEATING))
    assert sensor.entity_registry_enabled_default
    registry = er.async_get(hass)
    entry = sensor.coordinator.config_entry
    registered = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        sensor.unique_id,
        config_entry=entry,
        disabled_by=disabled_by,
    )
    from homeassistant.helpers.entity_platform import EntityPlatform

    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="sensor",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    platform.async_prepare()
    sensor.entity_id = None
    await platform.async_add_entities([sensor])
    await hass.async_block_till_done()
    assert registry.async_get(registered.entity_id).disabled_by is disabled_by
    await platform.async_reset()


def test_the_day_fixture_is_the_one_of_the_day_tests():
    assert payloads("rrc/day-programs.json").keys() == DAY_ITEMS.keys()
