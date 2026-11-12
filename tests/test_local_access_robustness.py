"""Robustness of the local access: pauses, retries, freshness and decoding.

Real scheduling cycles of the polling service, the planner and the read
client against loopback gateways; synthetic values only.
"""

from __future__ import annotations

import asyncio
import logging
import math

import pytest

from custom_components.bosch_buderus_heating import local_read as reader
from custom_components.bosch_buderus_heating.local_api.catalog import (
    make_request,
    request_for_path,
)
from custom_components.bosch_buderus_heating.local_budget import LocalReadBudget
from custom_components.bosch_buderus_heating.local_polling import service as module
from custom_components.bosch_buderus_heating.local_polling.inventory import (
    QUICK_RETRY,
    Freshness,
    ResourceInventory,
    late_after,
)
from custom_components.bosch_buderus_heating.local_polling.planner import PollPlanner
from custom_components.bosch_buderus_heating.local_polling.service import (
    LocalPollingService,
)
from custom_components.bosch_buderus_heating.local_polling.storage import (
    LocalScheduleStore,
    checkpoint,
    restore,
)
from custom_components.bosch_buderus_heating.local_probe import TransportFailure
from custom_components.bosch_buderus_heating.local_read import (
    DECODE_FAILED,
    LocalReadLimits,
    ReadProblem,
    ReadResult,
    ReadState,
)
from custom_components.bosch_buderus_heating.local_state import (
    LocalCondition,
    LocalSourceState,
)
from tests.test_local_budget import Clock
from tests.test_local_keepalive import KEEP, answer, keepalive_gateway, reuse_client
from tests.test_local_keepalive import read_ok as keep_read_ok
from tests.test_local_polling import (
    REQUEST,
    missing,
    no_discovery,
    signals,
    success,
)
from tests.test_local_probe import CONTEXT
from tests.test_local_read import client, payload
from tests.test_local_read import clock as clock
from tests.test_local_read import success as instant
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import gateways as gateways
from tests.test_local_transport import wire_reply

__all__ = ["keepalive_gateway"]
NAMES = ("HC1.SupplyTemp", "DHW.Temp")
STATIC = request_for_path("/gateway/versionHardware")


def planner_at(now):
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), now)
    no_discovery(planner)
    return planner.inventory, planner


def restarted(planner, now):
    """The same schedule after a restart of Home Assistant."""
    saved = checkpoint(planner, now - 1)
    inventory, again = planner_at(now)
    restore(again, saved, now)
    return inventory, again


def failure(request, state=ReadState.ACCESS_ERROR, problem="connection"):
    return ReadResult(request, state, None, problem)


# ------------------------------------------- PLAN-1: first read after a restart


def test_a_failed_first_signal_list_after_a_restart_is_asked_again_soon():
    inventory, planner = planner_at(0)
    signals(inventory, NAMES, 0)
    for name in NAMES:
        request = make_request(
            "/signals/{signalId}", parameters={"signalId": name}, signal_ids=NAMES
        )
        inventory.accept(success(request, 1), 1)
    inventory, planner = restarted(planner, 1000)
    listing = inventory.records["/signals"]
    work = planner.pick(1000)
    assert work.request.path == "/signals"
    planner.finish(work, failure(work.request), 1000.5)
    # Asked again after 30 s, not after its 15 minutes.
    assert listing.due == pytest.approx(1030.5)
    # The next answer is an empty list (no answer after known signals): soon
    # again, a little later.
    work = planner.pick(1030.5)
    empty = success(work.request, 1031, data=signals_data(()))
    planner.finish(work, empty, 1031)
    assert listing.invalid and listing.due == pytest.approx(1091)
    # A valid list makes every signal current again and ends the retries.
    work = planner.pick(1091)
    planner.finish(work, success(work.request, 1092, data=signals_data(NAMES)), 1092)
    assert listing.retries == 0 and not listing.invalid
    assert all(
        record.active
        for target, record in inventory.records.items()
        if target.startswith("/signals/")
    )
    assert listing.due == pytest.approx(1091 + 900)


def signals_data(names):
    from custom_components.bosch_buderus_heating.local_api.models import (
        ReferenceList,
        ResourceReference,
    )

    return ReferenceList(
        tuple(ResourceReference("/signals/" + name, "unused-uri") for name in names)
    )


def test_quick_retries_grow_and_never_exceed_the_period():
    inventory, planner = planner_at(0)
    inventory.add(STATIC, 0)
    inventory.accept(success(STATIC, 1), 1)
    inventory, planner = restarted(planner, 1000)
    record = inventory.records[STATIC.target]
    now, waits = 1000.0, []
    for _ in range(len(QUICK_RETRY) + 2):
        inventory.accept(failure(STATIC, problem="service_unavailable"), now)
        waits.append(record.due - now)
        now = record.due
    # 30, 60, 120, 300 s, then its period of a day.
    assert waits == [*QUICK_RETRY, 86400.0, 86400.0]
    inventory.accept(success(STATIC, now), now)
    assert record.retries == 0
    # A path error of a value read in this process withdraws it: also soon.
    inventory.accept(missing(STATIC), now + 5)
    assert record.invalid and record.due == now + 5 + QUICK_RETRY[0]
    # Never later than a short period.
    live_inventory, _ = planner_at(0)
    live_inventory.add(REQUEST, 0)
    live = live_inventory.records[REQUEST.target]
    live.confirmed = True
    for _ in range(3):
        live_inventory.accept(failure(REQUEST), 10)
    assert live.due == 10 + 60


def test_a_value_still_usable_keeps_its_due_time():
    inventory, _ = planner_at(0)
    inventory.add(REQUEST, 0)
    inventory.accept(success(REQUEST, 1), 1)
    record = inventory.records[REQUEST.target]
    inventory.accept(failure(REQUEST), 61, started=61)
    assert record.due == 121 and record.retries == 0


# --------------------------------------------- PLAN-5: timeouts in a row


def test_only_the_first_of_several_timeouts_in_a_row_is_repeated_at_once():
    inventory, _ = planner_at(0)
    inventory.add(REQUEST, 0)
    inventory.accept(success(REQUEST, 1), 1)
    record = inventory.records[REQUEST.target]
    timeout = failure(REQUEST, ReadState.PATH_ERROR, TransportFailure.TIMEOUT)
    waits, now = [], 100.0
    for _ in range(4):
        inventory.accept(timeout, now + 5, started=now)
        waits.append(record.due - (now + 5))
        now = record.due
    assert waits == [0.0, 55.0, 55.0, 55.0]
    inventory.accept(success(REQUEST, now + 1), now + 1, started=now)
    inventory.accept(timeout, now + 70, started=now + 65)
    assert record.due == now + 70  # a new series starts at once again


# -------------------------------------------------- PLAN-3: late, with grace


@pytest.mark.parametrize(
    ("interval", "fresh", "late"),
    [(60, 69.9, 70), (300, 329.9, 330), (900, 989.9, 990)],
)
def test_a_reading_is_late_only_after_its_grace(interval, fresh, late):
    assert late_after(interval) == late
    inventory, _ = planner_at(0)
    inventory.interval = interval
    inventory.add(REQUEST, 0)
    inventory.accept(success(REQUEST, 100), 100)
    record = inventory.records[REQUEST.target]
    for age, freshness in (
        (fresh, Freshness.FRESH),
        (late, Freshness.LATE),
        (3 * interval, Freshness.EXPIRED),
    ):
        view = inventory.view(record, 100 + age, source_ready=True)
        assert view.freshness is freshness
    assert inventory.next_expiry(100) == pytest.approx(100 + late)
    assert inventory.next_expiry(100 + late) == 100 + 3 * interval


# ------------------------------------------------------- polling service


class Gateway:
    """A read client answering from ``answer(request)``; ``hold`` stalls."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.hold: asyncio.Event | None = None
        self.answer = None

    async def read(self, request):
        self.calls.append(request)
        if self.hold is not None:
            await self.hold.wait()
        if self.answer is not None:
            return self.answer(request)
        return success(request, self.clock())

    async def close(self):
        pass


@pytest.fixture
async def polling(hass, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "start_offset", lambda scope: 0)
    clock = Clock()
    gateway = Gateway(clock)
    budget = LocalReadBudget(CONTEXT, clock=clock)
    store = LocalScheduleStore(hass, "synthetic-scope")
    store._path = tmp_path / "schedule.json"
    published = []
    service = LocalPollingService(
        CONTEXT,
        gateway,
        budget,
        store,
        is_current=lambda: True,
        publish=published.append,
        clock=clock,
    )
    no_discovery(service.planner)
    yield service, clock, gateway, published
    await service.close()


async def settle():
    for _ in range(30):
        await asyncio.sleep(0)


async def advance(service, clock, seconds):
    clock.advance(seconds)
    service.wake()
    await settle()
    async with asyncio.timeout(5), service.store._lock:
        pass
    await settle()


async def running(service, clock, *requests):
    service.planner.hints(requests, clock())
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    for _ in requests[1:]:
        await advance(service, clock, 0.2)
    assert all(
        service.inventory.records[r.target].observation is not None for r in requests
    )


async def test_two_reads_failing_together_count_as_one_failure(polling):
    """PLAN-2: an unreachable gateway lets both running reads time out."""
    service, clock, gateway, _ = polling
    second = request_for_path("/heatingCircuits/hc1/currentRoomSetpoint")
    await running(service, clock, REQUEST, second)
    gateway.hold = asyncio.Event()
    gateway.answer = lambda request: failure(request, problem=TransportFailure.CONNECT)
    await advance(service, clock, 60)
    await advance(service, clock, 0.2)
    assert len(gateway.calls) == 4  # both reads are running
    clock.advance(5)  # both time out
    gateway.hold.set()
    await settle()
    assert service._failures == 1
    assert service.budget.pause_seconds == pytest.approx(60, abs=0.5)
    # The next failure after the pause grows it once.
    gateway.hold = None
    await advance(service, clock, 61)
    assert service._failures == 2
    assert service.budget.pause_seconds == pytest.approx(120, abs=0.5)


async def test_a_short_retry_after_does_not_stop_the_growing_pause(polling):
    """TLS-2: 429 with Retry-After 0 grows like every other failed access;
    a longer Retry-After is kept in full."""
    service, clock, gateway, _ = polling
    await running(service, clock, REQUEST)
    retry_after = [0.0]
    gateway.answer = lambda request: ReadResult(
        request,
        ReadState.ACCESS_ERROR,
        429,
        ReadProblem.RATE_LIMITED,
        retry_after_seconds=retry_after[0],
    )
    pauses = []
    await advance(service, clock, 60)
    for _ in range(3):
        pauses.append(round(service.budget.pause_seconds))
        if len(pauses) == 3:
            retry_after[0] = 5000.0
        await advance(service, clock, service.budget.pause_seconds + 0.5)
    pauses.append(round(service.budget.pause_seconds))
    assert pauses == [60, 120, 300, 5000]
    assert len(gateway.calls) == 5


async def test_a_pause_keeps_the_values_already_read(polling):
    """SEL-1: a pause only slows the requests; the values stay current until
    their own expiry, a stop of the access withdraws them at once."""
    service, clock, _, published = polling
    await running(service, clock, REQUEST)
    await service.budget.pause(600)
    await advance(service, clock, 1)
    state = published[-1]
    assert state.condition is LocalCondition.PAUSED
    assert state.resources[0].available
    local = LocalSourceState(
        CONTEXT.identity,
        state.condition,
        state.identity_confirmed,
        tuple(v.observation for v in state.resources if v.observation),
        resources=state.resources,
    )
    assert len(local.current_observations) == 1
    # Late is still current, expired is not.
    await advance(service, clock, 69)
    assert published[-1].resources[0].freshness is Freshness.LATE
    assert published[-1].resources[0].available
    await advance(service, clock, 120)
    assert not published[-1].resources[0].available
    # Other conditions withdraw the values at once.
    for condition in (
        LocalCondition.AUTH_REQUIRED,
        LocalCondition.CERTIFICATE_CHANGED,
        LocalCondition.UNAVAILABLE,
        LocalCondition.INITIALIZING,
    ):
        assert not LocalSourceState(
            CONTEXT.identity,
            condition,
            condition is not LocalCondition.INITIALIZING,
            resources=state.resources,
        ).current_observations
    # Without a confirmed identity a pause supplies nothing either.
    assert not LocalSourceState(
        CONTEXT.identity, LocalCondition.PAUSED, False, resources=state.resources
    ).current_observations


async def test_the_expiry_timer_wakes_only_what_it_must(polling, monkeypatch):
    """PLAN-4/PERF-1: ageing never wakes the scheduler; a new reading that
    ages earlier than the timer looks next wakes the timer."""
    service, clock, gateway, published = polling
    parked = asyncio.Event()

    async def wait_for_event(event, delay):
        # Time passes only with the test's clock: both loops run when their
        # event is set, never because a real timeout ran out meanwhile. A
        # real one (the scheduler waits 0.2 s for the start gap) could
        # otherwise run the scheduler on a slow machine after the clock
        # moved past the next due read.
        if event is not service._wake:
            await event.wait()
            return
        parked.set()
        try:
            await event.wait()
        finally:
            parked.clear()

    async def scheduler_parked():
        # A pass of the scheduler may save the schedule, which takes real
        # time in the executor: wait until it waits for its event again.
        await settle()
        async with asyncio.timeout(5):
            await parked.wait()
        await settle()

    monkeypatch.setattr(service, "_wait", wait_for_event)
    await running(service, clock, STATIC)
    service._age_wake.set()
    await settle()
    # Only the slow value: the timer looks next when it becomes late.
    observed = service.inventory.records[STATIC.target].observation.observed_monotonic
    assert service._expiry_at == pytest.approx(observed + late_after(86400))
    # A live value read by the scheduler alone wakes the timer earlier.
    service.planner.hints((REQUEST,), clock())
    clock.advance(0.2)
    service._wake.set()
    await settle()
    observed = service.inventory.records[REQUEST.target].observation.observed_monotonic
    assert service._expiry_at == pytest.approx(observed + late_after(60), abs=0.01)
    # The timer's own pass does not wake the scheduler.
    steps = []
    original = service._step

    async def counted():
        steps.append(clock())
        return await original()

    service._step = counted
    service._wake.clear()
    clock.advance(late_after(60) + 0.01)
    service._age_wake.set()
    await scheduler_parked()
    assert published[-1].resources[-1].freshness is Freshness.LATE
    assert not steps and not service._wake.is_set()
    # An expired explicit operation does.
    gateway.hold = asyncio.Event()
    operation = service.request((REQUEST,))
    await scheduler_parked()
    assert not operation.done
    service._wake.clear()
    steps.clear()
    clock.advance(121)
    service._age_wake.set()
    await settle()
    gateway.hold.set()
    assert operation.done and operation.problem == "deadline" and steps


async def test_search_records_are_pruned_at_most_once_a_minute(polling, monkeypatch):
    service, clock, _, _ = polling
    await running(service, clock, REQUEST)
    calls = []
    monkeypatch.setattr(service.inventory, "prune", calls.append)
    for _ in range(5):
        await advance(service, clock, 5)
    assert len(calls) <= 1
    await advance(service, clock, 60)
    assert 1 <= len(calls) <= 2


async def test_the_same_unexpected_problem_warns_at_most_once_an_hour(polling, caplog):
    """ERR-5: a problem that comes back after every working read is logged
    as a warning once an hour, otherwise at debug level."""
    service, clock, _, _ = polling
    caplog.set_level(logging.DEBUG, module.__name__)

    def warnings():
        return [r for r in caplog.records if r.levelno == logging.WARNING]

    service._recover("local_poll_failed")
    assert len(warnings()) == 1
    # A working read ends the pause and starts the backoff again.
    service._fault, service._faults = None, 0
    service._recover("local_poll_failed")
    assert len(warnings()) == 1
    assert "paused again" in caplog.text
    service._fault, service._faults = None, 0
    service._recover("local_listener_failed")
    assert len(warnings()) == 2
    service._fault, service._faults = None, 0
    clock.advance(3600)
    service._recover("local_poll_failed")
    assert len(warnings()) == 3


# --------------------------------------------------------- read client


@pytest.mark.usefixtures("socket_enabled")
async def test_an_undecodable_answer_is_a_path_error_of_that_path(
    gateways, clock, monkeypatch, caplog
):
    """ERR-5: no exception escapes to pause every local read."""
    _, _, _, binding = await gateways(instant)
    transport_client, budget = client(binding, clock)

    def broken(*args, **kwargs):
        raise KeyError("private value 42")

    monkeypatch.setattr(reader, "decode_resource", broken)
    caplog.set_level(logging.DEBUG, reader.__name__)
    async with transport_client:
        for _ in range(2):
            clock.advance()
            result = await transport_client.read(REQUEST)
            assert result.state is ReadState.PATH_ERROR
            assert result.problem == DECODE_FAILED and result.status == 200
    assert budget.pause_seconds == 0
    # Only the reader's own warnings: on a loaded machine asyncio may add a
    # slow-callback warning of its own.
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == reader.__name__
    ]
    assert len(warnings) == 1
    assert REQUEST.definition.template in warnings[0].getMessage()
    assert "KeyError" in warnings[0].getMessage()
    assert "private value" not in warnings[0].getMessage()
    assert transport_client.health()["failures_by_class"] == {"other": 2}


@pytest.mark.usefixtures("socket_enabled")
async def test_a_slow_body_on_a_kept_connection_is_no_reuse_failure(
    keepalive_gateway, clock
):
    """TLS-3: the gateway answered; only the body came too late."""
    stall = asyncio.Event()

    async def slow_body(raw, writer, number):
        if number == 2:
            reply = wire_reply(payload(), headers=KEEP)
            writer.write(reply[:-5])
            await writer.drain()
            await stall.wait()
        answer(writer)

    _, _, binding = await keepalive_gateway(slow_body)
    transport_client, budget = reuse_client(
        binding, clock, limits=LocalReadLimits(timeout=1.0)
    )
    async with transport_client:
        await keep_read_ok(transport_client, clock)
        clock.advance()
        result = await transport_client.read(REQUEST)
        stall.set()
        assert result.state is ReadState.PATH_ERROR
        assert result.problem == TransportFailure.TIMEOUT and result.status == 200
        assert budget.pause_seconds == 0
        reuse = transport_client.connection_reuse()
        assert reuse["reuse_failures"] == {}
        assert not reuse["fallback_active"]


def test_a_request_works_out_its_target_once():
    """PERF-1: the target is kept, equality and hashing are unchanged."""
    request = request_for_path("/heatingCircuits/hc2/currentRoomSetpoint")
    assert request.target is request.target
    assert request == REQUEST and hash(request) == hash(REQUEST)
    assert request.target == "/heatingCircuits/hc2/currentRoomSetpoint"
    assert math.isfinite(late_after(60))
