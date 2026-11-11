"""D25: a write transaction has an overall deadline once it holds the lock.

A slow cloud with request retries no longer holds polling and the caller for
minutes. At the deadline the transaction stops like a cancelled write: the
change counts as not confirmed, its reading is no longer current, and nothing
is ever sent a second time.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating import write_window
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.holidays import icom_write_values
from custom_components.bosch_buderus_heating.pointt import (
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.writes import (
    WriteBusy,
    WriteDeadlineExceeded,
)
from tests import test_dynamic_controls
from tests.test_heating_controls import TARGET, setup
from tests.test_legacy_holidays import FREE
from tests.test_legacy_holidays import wire as wire_holidays
from tests.test_rrc import SELECTION
from tests.test_rrc import wire as wire_rrc
from tests.test_write_guards import Cloud

SHORT = coordinator_module.timedelta(seconds=0.05)
# The real send budget, before a fixture replaces it.
REAL_BUDGET = write_window.send_budget


async def _hang(*_args, **_kwargs):
    await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def no_floor():
    """The tests shorten the deadlines; no read-back floor may lengthen them,
    and no send budget refuses the request before it is sent."""
    with (
        patch.object(write_window, "send_budget", return_value=0.0),
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


# M1/M2 (review of alpha.6): the time left is checked before the request.


class _Clock:
    """The loop time the send window sees; a slow read moves it on."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def window_clock():
    clock = _Clock()
    with (
        patch.object(write_window, "_now", clock),
        patch.object(write_window, "send_budget", REAL_BUDGET),
    ):
        yield clock


def test_the_send_window_refuses_before_and_stops_after_a_request(window_clock):
    with write_window.send_window(60.0) as window:
        write_window.ensure_send_window(3.5)
        assert window.sent
        window_clock.now += 27.0
        # 33 s left, 30 s request and 3.5 s read-back needed: nothing more.
        with pytest.raises(WriteDeadlineExceeded):
            write_window.ensure_send_window(3.5)
    with write_window.send_window(60.0) as window:
        window_clock.now += 26.6
        with pytest.raises(WriteBusy):
            write_window.ensure_send_window(3.5)
        assert not window.sent
    # Outside a write transaction nothing is checked.
    write_window.ensure_send_window(1000.0)


@pytest.mark.parametrize(("slow", "sent"), [(45.0, False), (20.0, True)])
async def test_a_slow_check_before_a_write_sends_nothing(
    hass, window_clock, slow, sent
):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    original = cloud.bulk

    async def bulk(gateway, paths, **kwargs):
        if not cloud.puts:
            window_clock.now += slow
        return await original(gateway, paths, **kwargs)

    coordinator.client.get_resources_bulk.side_effect = bulk
    if sent:
        await entity.async_set_temperature(temperature=20.5)
        assert cloud.puts == [(TARGET, 20.5)]
        return
    with pytest.raises(HomeAssistantError) as error:
        await entity.async_set_temperature(temperature=20.5)
    assert error.value.translation_key == "write_busy"
    assert isinstance(error.value.__cause__, WriteBusy)
    assert cloud.puts == []
    # Nothing was sent: the reading stays current.
    assert coordinator.data[TARGET].freshness is Freshness.FRESH
    assert coordinator.data[TARGET].available
    assert not coordinator._update_lock.locked()


async def test_a_write_stopped_before_its_request_keeps_the_reading(hass):
    """M2: the deadline ends the transaction before the PUT went out."""
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    service = coordinator._write_service
    original = service.async_write_number

    async def slow(*args, **kwargs):
        await _hang()  # a slow check inside the service, before its PUT
        return await original(*args, **kwargs)

    with (
        patch.object(service, "async_write_number", side_effect=slow),
        patch.object(coordinator_module, "WRITE_TRANSACTION_DEADLINE", SHORT),
        pytest.raises(HomeAssistantError) as error,
    ):
        await entity.async_set_temperature(temperature=20.5)
    assert error.value.translation_key == "write_busy"
    assert cloud.puts == []
    assert coordinator.data[TARGET].freshness is Freshness.FRESH
    assert coordinator.data[TARGET].available
    assert TARGET not in coordinator._urgent_reads
    assert not coordinator._update_lock.locked()


async def test_a_slow_rrc_preflight_sends_nothing(environment, window_clock):
    coordinator, client, _ = environment
    wire_rrc(environment)
    original = client.get_resource.side_effect

    async def get(gateway, path):
        if not client.put_resource_value.await_count:
            window_clock.now += 45.0
        return await original(gateway, path)

    client.get_resource.side_effect = get
    with pytest.raises(WriteBusy):
        await coordinator.async_select_rrc_program(SELECTION, 7)
    client.put_resource_value.assert_not_awaited()
    assert coordinator.data[SELECTION].freshness is Freshness.FRESH
    stats = coordinator.rrc_write_stats.snapshot("core")
    assert stats["writes_sent"] == 0 and stats["writes_unconfirmed"] == 0
    # No guard is left pending: the next write starts normally.
    client.get_resource.side_effect = original
    await coordinator.async_select_rrc_program(SELECTION, 7)
    assert client.put_resource_value.await_count == 1


async def test_an_rrc_operation_stopped_before_its_request_counts_nothing(
    environment,
):
    """M2: no stale reading and no unconfirmed write without a send."""
    coordinator, client, _ = environment
    wire_rrc(environment)
    original = client.get_resource.side_effect

    async def get(gateway, path):
        if not client.put_resource_value.await_count:
            await _hang()
        return await original(gateway, path)

    client.get_resource.side_effect = get
    with (
        patch.object(coordinator_module, "WRITE_TRANSACTION_DEADLINE", SHORT),
        pytest.raises(WriteBusy),
    ):
        await coordinator.async_select_rrc_program(SELECTION, 7)
    client.put_resource_value.assert_not_awaited()
    assert coordinator.data[SELECTION].freshness is Freshness.FRESH
    stats = coordinator.rrc_write_stats.snapshot("core")
    assert stats["writes_sent"] == 0 and stats["writes_unconfirmed"] == 0
    assert not coordinator._update_lock.locked()


async def test_a_slow_legacy_holiday_check_sends_nothing(environment, window_clock):
    coordinator, client, _ = environment
    state = wire_holidays(environment)
    original = client.get_resource.side_effect

    async def get(gateway, path):
        if not client.put_resource_value.await_count:
            window_clock.now += 160.0
        return await original(gateway, path)

    client.get_resource.side_effect = get
    values = replace(
        icom_write_values(state, FREE),
        start_date="2030-11-01",
        end_date="2030-11-03",
        assigned_to=("hc2",),
    )
    with pytest.raises(WriteBusy):
        await coordinator.async_create_holiday(values)
    client.put_resource_value.assert_not_awaited()
    client.put_icom_holiday_assignments.assert_not_awaited()
    assert not coordinator._update_lock.locked()


@pytest.mark.parametrize("operation", ["create", "delete", "update"])
async def test_a_slow_holiday_check_sends_nothing(hass, window_clock, operation):
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
    listed = coordinator.data[HOLIDAY_LIST_PATH].resource

    async def slow(*_args, **_kwargs):
        window_clock.now += 160.0
        return listed

    client.get_resource.side_effect = slow
    entity = BoschBuderusHolidayCalendar(coordinator)
    with pytest.raises(HomeAssistantError):
        if operation == "create":
            await entity.async_create_event(
                dtstart=datetime(2030, 9, 1).date(),
                dtend=datetime(2030, 9, 8).date(),
                summary="New trip",
            )
        elif operation == "delete":
            await entity.async_delete_event(_uid(entity))
        else:
            event = (await entity.async_get_events(hass, *_span()))[0]
            await entity.async_update_event(
                _uid(entity),
                {"summary": "Away", "dtstart": event.start, "dtend": event.end},
            )
    client.create_holiday_period.assert_not_awaited()
    client.delete_holiday_period.assert_not_awaited()
    client.update_holiday_period.assert_not_awaited()
    assert HOLIDAY_LIST_PATH not in coordinator._urgent_reads
    assert not coordinator._update_lock.locked()


async def test_a_holiday_stopped_before_its_request_is_not_read_again(hass):
    """M2: the deadline ends the holiday change before its request."""
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
    client.get_resource.side_effect = _hang
    entity = BoschBuderusHolidayCalendar(coordinator)
    with (
        patch.object(coordinator_module, "HOLIDAY_WRITE_DEADLINE", SHORT),
        pytest.raises(HomeAssistantError),
    ):
        await entity.async_delete_event(_uid(entity))
    client.delete_holiday_period.assert_not_awaited()
    assert HOLIDAY_LIST_PATH not in coordinator._urgent_reads
    assert not coordinator._update_lock.locked()


def _span():
    from datetime import UTC

    return datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
