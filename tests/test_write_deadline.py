"""D25: a write transaction has an overall deadline once it holds the lock.

A slow cloud with request retries no longer holds polling and the caller for
minutes. At the deadline the transaction stops like a cancelled write: the
change counts as not confirmed, its reading is no longer current, and nothing
is ever sent a second time.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.holidays import icom_write_values
from custom_components.bosch_buderus_heating.pointt import (
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.writes import WriteDeadlineExceeded
from tests import test_dynamic_controls
from tests.test_heating_controls import TARGET, setup
from tests.test_legacy_holidays import FREE
from tests.test_legacy_holidays import wire as wire_holidays
from tests.test_rrc import SELECTION
from tests.test_rrc import wire as wire_rrc
from tests.test_write_guards import Cloud

SHORT = coordinator_module.timedelta(seconds=0.05)


async def _hang(*_args, **_kwargs):
    await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def no_floor():
    """The tests shorten the deadlines; no read-back floor may lengthen them."""
    with (
        patch.object(coordinator_module, "WRITE_STEP_MARGIN_SECONDS", 0.0),
        patch.object(
            coordinator_module.BoschBuderusDataUpdateCoordinator,
            "write_read_back_delays",
            return_value=(0.0,),
        ),
    ):
        yield


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


def test_the_deadlines_leave_room_for_a_healthy_write() -> None:
    # RRC read-back waits 15 s in all; a legacy holiday sends up to five steps.
    assert coordinator_module.WRITE_TRANSACTION_DEADLINE.total_seconds() == 60
    assert coordinator_module.HOLIDAY_WRITE_DEADLINE.total_seconds() == 180
    assert coordinator_module.HOLIDAY_WRITE_STEPS == 5
    assert issubclass(WriteDeadlineExceeded, WriteNotConfirmed)


@pytest.mark.parametrize(
    ("delays", "steps", "seconds"),
    [
        # The schedule of most gateways: the constants stand.
        ((0.5, 1.0, 2.0), 1, 60),
        ((0.5, 1.0, 2.0), 5, 180),
        # An iCom gateway reads back after 1, 2, 3 and 4 s (10 s per step).
        ((1.0, 2.0, 3.0, 4.0), 1, 60),
        ((1.0, 2.0, 3.0, 4.0), 5, 200),
        # A slower schedule lengthens the deadline instead of cutting it.
        ((10.0, 20.0, 40.0), 1, 100),
    ],
)
def test_the_deadline_follows_the_read_back_schedule(
    environment, delays, steps, seconds
):
    coordinator, _client, _ = environment
    with (
        patch.object(coordinator_module, "WRITE_STEP_MARGIN_SECONDS", 30.0),
        patch.object(coordinator, "write_read_back_delays", return_value=delays),
    ):
        deadline = coordinator_module.HOLIDAY_WRITE_DEADLINE if steps > 1 else None
        assert coordinator._write_deadline(deadline, steps).total_seconds() == (seconds)


async def test_hanging_read_back_ends_unconfirmed_after_one_put(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    original = cloud.get

    async def get(gateway, path, **kwargs):
        if cloud.puts:
            await _hang()
        return await original(gateway, path, **kwargs)

    coordinator.client.get_resource.side_effect = get
    with (
        patch.object(coordinator_module, "WRITE_TRANSACTION_DEADLINE", SHORT),
        pytest.raises(HomeAssistantError) as error,
    ):
        await entity.async_set_temperature(temperature=20.5)
    assert error.value.translation_key == "write_not_confirmed"
    assert isinstance(error.value.__cause__, WriteDeadlineExceeded)
    assert cloud.puts == [(TARGET, 20.5)]
    # The change may have been applied: the old reading is not current.
    assert coordinator.data[TARGET].freshness is Freshness.STALE
    assert not coordinator._update_lock.locked()


async def test_hanging_put_is_never_sent_again(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    original = cloud.put

    async def put(gateway, path, value):
        await original(gateway, path, value)
        await _hang()

    coordinator.client.put_resource_value.side_effect = put
    with (
        patch.object(coordinator_module, "WRITE_TRANSACTION_DEADLINE", SHORT),
        pytest.raises(HomeAssistantError),
    ):
        await entity.async_set_temperature(temperature=20.5)
    assert cloud.puts == [(TARGET, 20.5)]
    assert not coordinator._update_lock.locked()
    # The next command starts normally and is not refused as pending.
    coordinator.client.put_resource_value.side_effect = original
    coordinator.data[TARGET] = replace(
        coordinator.data[TARGET],
        resource=cloud.state[TARGET],
        available=True,
        freshness=Freshness.FRESH,
    )
    await entity.async_set_temperature(temperature=21.0)
    assert cloud.puts[-1] == (TARGET, 21.0)


async def test_a_quick_write_is_not_affected(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    with patch.object(
        coordinator_module,
        "WRITE_TRANSACTION_DEADLINE",
        coordinator_module.timedelta(seconds=5),
    ):
        await entity.async_set_temperature(temperature=20.5)
    assert cloud.puts == [(TARGET, 20.5)]
    assert coordinator.data[TARGET].freshness is Freshness.FRESH


async def test_rrc_operation_stops_and_counts_the_send_unconfirmed(environment):
    coordinator, client, _ = environment
    wire_rrc(environment)
    original = client.get_resource.side_effect

    async def get(gateway, path):
        if client.put_resource_value.await_count:
            await _hang()
        return await original(gateway, path)

    client.get_resource.side_effect = get
    with (
        patch.object(coordinator_module, "WRITE_TRANSACTION_DEADLINE", SHORT),
        pytest.raises(WriteDeadlineExceeded),
    ):
        await coordinator.async_select_rrc_program(SELECTION, 7)
    assert client.put_resource_value.await_count == 1
    assert coordinator.data[SELECTION].freshness is Freshness.STALE
    stats = coordinator.rrc_write_stats.snapshot("core")
    assert stats["writes_sent"] == 1 and stats["writes_unconfirmed"] == 1
    assert not coordinator._update_lock.locked()


async def test_legacy_holiday_has_the_longer_deadline(environment):
    coordinator, client, _ = environment
    state = wire_holidays(environment)
    original = client.get_resource.side_effect

    async def slow(gateway, path):
        await asyncio.sleep(0.002)
        return await original(gateway, path)

    client.get_resource.side_effect = slow
    values = replace(
        icom_write_values(state, FREE),
        start_date="2030-11-01",
        end_date="2030-11-03",
        assigned_to=("hc2",),
    )
    with patch.object(
        coordinator_module,
        "WRITE_TRANSACTION_DEADLINE",
        coordinator_module.timedelta(seconds=0.001),
    ):
        await coordinator.async_create_holiday(values)
    assert icom_write_values(coordinator.resources, FREE) == values


async def test_legacy_holiday_at_its_deadline_is_not_current(environment):
    coordinator, client, _ = environment
    state = wire_holidays(environment)
    original = client.get_resource.side_effect

    async def get(gateway, path):
        if client.put_resource_value.await_count:
            await _hang()
        return await original(gateway, path)

    client.get_resource.side_effect = get
    values = replace(
        icom_write_values(state, FREE),
        start_date="2030-11-01",
        end_date="2030-11-03",
        dhw_mode="LOW",
        assigned_to=("hc2",),
    )
    with (
        patch.object(coordinator_module, "HOLIDAY_WRITE_DEADLINE", SHORT),
        pytest.raises(WriteDeadlineExceeded),
    ):
        await coordinator.async_create_holiday(values)
    assert client.put_resource_value.await_count == 1
    assert coordinator.data[f"{FREE}/startStop"].freshness is Freshness.STALE
    assert not coordinator._update_lock.locked()


async def test_other_errors_and_outside_cancellation_pass_unchanged(environment):
    coordinator, _client, _ = environment
    with pytest.raises(WriteValidationError):
        async with coordinator._write_transaction():
            raise WriteValidationError("refused")
    with pytest.raises(TimeoutError):
        async with coordinator._write_transaction():
            raise TimeoutError
    started = asyncio.Event()

    async def hold() -> None:
        async with coordinator._write_transaction():
            started.set()
            await _hang()

    task = asyncio.create_task(hold())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not coordinator._update_lock.locked()


async def test_holiday_list_at_its_deadline_is_read_with_the_next_cycle(hass):
    from custom_components.bosch_buderus_heating.calendar import (
        BoschBuderusHolidayCalendar,
    )
    from custom_components.bosch_buderus_heating.holiday_writes import (
        HolidayWriteService,
    )
    from custom_components.bosch_buderus_heating.holidays import HOLIDAY_LIST_PATH
    from tests.test_calendar import _coordinator, _uid, _writable_holiday_resources

    coordinator = _coordinator(hass, _writable_holiday_resources())
    client = coordinator.client
    coordinator._holiday_write_service = HolidayWriteService(client, sleep=AsyncMock())
    client.get_resource.return_value = coordinator.data[HOLIDAY_LIST_PATH].resource
    client.delete_holiday_period.side_effect = _hang
    entity = BoschBuderusHolidayCalendar(coordinator)
    with (
        patch.object(coordinator_module, "HOLIDAY_WRITE_DEADLINE", SHORT),
        pytest.raises(HomeAssistantError),
    ):
        await entity.async_delete_event(_uid(entity))
    client.delete_holiday_period.assert_awaited_once()
    assert HOLIDAY_LIST_PATH in coordinator._urgent_reads
    assert not coordinator._update_lock.locked()
