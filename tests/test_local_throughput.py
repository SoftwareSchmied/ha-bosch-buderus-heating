"""Local read throughput: start spacing, latency brake, reserve and due times."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_budget import (
    BRAKE_SECONDS,
    MINIMUM_START_GAP,
    LocalBudgetError,
    LocalBudgetManager,
    LocalReadBudget,
)
from custom_components.bosch_buderus_heating.local_polling import service as module
from custom_components.bosch_buderus_heating.local_polling.inventory import (
    ResourceInventory,
)
from custom_components.bosch_buderus_heating.local_polling.planner import PollPlanner
from custom_components.bosch_buderus_heating.local_polling.policy import (
    SECTION_RESERVE,
)
from custom_components.bosch_buderus_heating.local_polling.service import (
    LocalPollingService,
)
from custom_components.bosch_buderus_heating.local_polling.storage import (
    LocalScheduleStore,
)
from custom_components.bosch_buderus_heating.local_probe import TransportFailure
from custom_components.bosch_buderus_heating.local_read import (
    LocalReadLimits,
    ReadProblem,
    ReadResult,
    ReadState,
)
from tests.test_local_budget import Clock
from tests.test_local_polling import REQUEST, missing, no_discovery, success
from tests.test_local_probe import CONTEXT
from tests.test_local_read import client, payload
from tests.test_local_read import clock as clock
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import gateways as gateways
from tests.test_local_transport import wire_reply

OTHER = request_for_path("/heatSources/actualSupplyTemperature")
THIRD = request_for_path("/heatSources/returnTemperature")


def confirmed(planner, request, at, due=None):
    planner.inventory.add(request, at)
    planner.inventory.accept(success(request, at), at)
    if due is not None:
        planner.inventory.records[request.target].due = due


# T1: the latency brake and the spacing of the shared budget.


def test_base_spacing_is_200_ms_from_the_start_of_a_read():
    clock = Clock()
    budget = LocalReadBudget(CONTEXT, clock=clock)
    assert MINIMUM_START_GAP == 0.2 and budget.start_gap() == 0.2
    lease = budget.claim()
    assert lease is not None and budget.start_wait() == pytest.approx(0.2)
    assert budget.claim() is None
    clock.advance(0.2)
    assert budget.start_wait() == 0 and budget.claim() is not None


async def test_header_spacing_does_not_delay_the_next_start():
    clock = Clock()
    budget = LocalReadBudget(CONTEXT, clock=clock)
    first = budget.claim()
    clock.advance(0.03)  # the connection and handshake before the headers
    await budget.start_http(first, lambda: True)
    clock.advance(0.17)
    # The start gate opened 200 ms after the start; the header gate is
    # still closed for 30 ms but only spaces the next headers.
    assert budget.remaining_seconds == pytest.approx(0.03)
    assert budget.start_wait() == 0
    budget.release(first)
    assert budget.claim() is not None


def test_start_wait_uses_the_given_clock_reading_and_includes_a_pause():
    clock = Clock()
    budget = LocalReadBudget(CONTEXT, clock=clock)
    budget.claim()
    assert budget.start_wait(clock() + 0.1) == pytest.approx(0.1)
    budget.defer(30)
    assert budget.start_wait() == pytest.approx(30)


def test_brake_follows_twice_the_median_and_stays_bounded():
    clock = Clock()
    budget = LocalReadBudget(CONTEXT, clock=clock)
    for _ in range(32):
        budget.observe(0.05)
    assert budget.start_gap() == 0.2  # 100 ms: the base still applies
    for _ in range(17):
        budget.observe(0.15)
    assert budget.start_gap() == pytest.approx(0.3)
    for _ in range(32):
        budget.observe(3.0)
    assert budget.start_gap() == 1.0
    budget.observe(float("nan"))
    budget.observe(-1)
    assert budget.start_spacing()["response_samples"] == 32


@pytest.mark.parametrize("reason", ["timeout", "server_error"])
def test_timeout_or_server_error_doubles_the_spacing_for_five_minutes(reason):
    clock = Clock()
    budget = LocalReadBudget(CONTEXT, clock=clock)
    budget.brake(reason)
    assert budget.start_gap() == pytest.approx(0.4)
    assert budget.start_spacing() == {
        "base_ms": 200,
        "current_ms": 400,
        "median_response_ms": None,
        "response_samples": 0,
        "brake_active": True,
        "brake_reason": reason,
        "brake_remaining_seconds": 300,
    }
    lease = budget.claim()
    assert lease is not None and budget.start_wait() == pytest.approx(0.4)
    for _ in range(32):
        budget.observe(0.9)
    assert budget.start_gap() == 2.0  # twice the 1 s limit at most
    clock.advance(BRAKE_SECONDS)
    assert budget.start_gap() == 1.0
    spacing = budget.start_spacing()
    assert spacing["brake_active"] is False and spacing["brake_reason"] is None
    assert spacing["brake_remaining_seconds"] == 0
    assert spacing["median_response_ms"] == 900.0


def test_brake_reason_is_a_fixed_code():
    budget = LocalReadBudget(CONTEXT, clock=Clock())
    with pytest.raises(LocalBudgetError):
        budget.brake("rate_limited")


async def test_pause_takes_precedence_over_the_brake():
    clock = Clock()
    budget = LocalReadBudget(CONTEXT, clock=clock)
    budget.brake("server_error")
    await budget.pause(120, "service_unavailable")
    assert budget.start_wait() == pytest.approx(120)
    clock.advance(119)
    assert budget.claim() is None
    clock.advance(1)
    assert budget.claim() is not None
    assert budget.start_wait() == pytest.approx(0.4)


async def test_merged_gateway_keeps_the_brake_and_the_response_times(hass, tmp_path):
    clock = Clock()
    manager = LocalBudgetManager(hass, clock=clock)
    manager._path = tmp_path / "local-budget.json"
    await manager.initialize()
    shared = await manager.budget(CONTEXT, gateway_id="gw")
    # A handle that learned about the same gateway on its own joins it later.
    alone = LocalReadBudget(CONTEXT, clock=clock, manager=manager)
    alone.brake("timeout")
    for _ in range(3):
        alone.observe(0.3)
    await alone.bind_gateway("gw")
    spacing = shared.start_spacing()
    assert spacing["brake_reason"] == "timeout"
    assert spacing["response_samples"] == 3
    assert shared.start_gap() == pytest.approx(1.2)
    # Known answer times of the gateway are kept, not replaced.
    later = LocalReadBudget(CONTEXT, clock=clock, manager=manager)
    later.observe(0.01)
    await later.bind_gateway("gw")
    assert shared.start_spacing()["response_samples"] == 3
    await manager.close()


@pytest.mark.usefixtures("socket_enabled")
async def test_reads_feed_the_brake(gateways, clock):
    replies = iter((200, 503, 404))

    async def reply(raw, writer):
        status = next(replies)
        writer.write(wire_reply(payload() if status == 200 else b"{}", status=status))
        await writer.drain()

    _, _, _, binding = await gateways(reply)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        spacing = budget.start_spacing()
        assert spacing["response_samples"] == 1 and not spacing["brake_active"]
        clock.advance(1)
        result = await transport_client.read(REQUEST)
        assert result.problem == ReadProblem.SERVICE_UNAVAILABLE
        spacing = budget.start_spacing()
        # The 503 pauses the access and also starts the brake.
        assert spacing["brake_reason"] == "server_error"
        assert spacing["response_samples"] == 1
        assert budget.pause_seconds > 0
        budget._state.root().pause_until = 0
        clock.advance(1)
        assert (await transport_client.read(REQUEST)).status == 404
        assert budget.start_spacing()["response_samples"] == 2


@pytest.mark.usefixtures("socket_enabled")
async def test_rate_limit_pauses_without_the_brake(gateways, clock):
    async def reply(raw, writer):
        writer.write(wire_reply(b"{}", status=429, headers={"Retry-After": "30"}))
        await writer.drain()

    _, _, _, binding = await gateways(reply)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
    assert result.problem == ReadProblem.RATE_LIMITED
    assert budget.pause_seconds == pytest.approx(30)
    assert budget.start_spacing()["brake_active"] is False


@pytest.mark.usefixtures("socket_enabled")
async def test_timeout_after_the_headers_starts_the_brake(gateways, clock):
    async def reply(raw, writer):
        await asyncio.sleep(1)

    _, _, _, binding = await gateways(reply)
    transport_client, budget = client(
        binding, clock, limits=LocalReadLimits(timeout=0.3)
    )
    async with transport_client:
        result = await transport_client.read(REQUEST)
    assert result.problem == TransportFailure.TIMEOUT
    assert result.state is ReadState.PATH_ERROR
    spacing = budget.start_spacing()
    assert spacing["brake_reason"] == "timeout"
    assert spacing["current_ms"] == 400 and spacing["response_samples"] == 0


@pytest.mark.usefixtures("socket_enabled")
async def test_response_time_excludes_the_header_spacing(gateways, clock):
    async def reply(raw, writer):
        writer.write(wire_reply(payload()))
        await writer.drain()

    _, _, _, binding = await gateways(reply)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        # The previous headers went out just now: this read waits ~0.5 s
        # for the header spacing, which is not the gateway's answer time.
        budget._state.root().http_at = clock() + 0.5

        async def advance():
            await asyncio.sleep(0.5)
            clock.advance(0.5)

        mover = asyncio.create_task(advance())
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        await mover
    assert budget.start_spacing()["median_response_ms"] < 400


# T1: the planner reports due work (regression: it waited for the next
# future due time while due work was waiting).


def test_next_wake_reports_overdue_regular_work_at_once():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    no_discovery(planner)
    confirmed(planner, REQUEST, 0, due=10)
    confirmed(planner, OTHER, 0, due=40)
    assert planner.next_wake(5) == 10
    assert planner.next_wake(12) == 12


def test_next_wake_waits_for_a_finished_read_when_both_are_in_flight():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    no_discovery(planner)
    for request in (REQUEST, OTHER, THIRD):
        confirmed(planner, request, 0, due=10)
    planner.inventory.records[THIRD.target].due = 30
    first, second = planner.pick(11), planner.pick(11.2)
    assert first is not None and second is not None
    planner.inventory.records[THIRD.target].due = 11
    # Two reads run: no start now, only the next due time that is still ahead.
    assert planner.next_wake(12) == planner.rescan_at
    planner.finish(first, success(first.request, 12), 12)
    assert planner.next_wake(12) == 12


def test_next_wake_reports_pending_explicit_work():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    no_discovery(planner)
    planner.explicit((REQUEST,), 1)
    assert planner.next_wake(2) == 2


def test_planner_uses_the_spacing_it_is_given():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    no_discovery(planner)
    for request in (REQUEST, OTHER):
        confirmed(planner, request, 0, due=0)
    planner.start_gap = 0.7
    work = planner.pick(1)
    assert work is not None and planner.next_start == pytest.approx(1.7)
    assert planner.pick(1.69) is None and planner.next_wake(1.5) == pytest.approx(1.7)


# T2: the reserve at the end of each 60-second section.


def test_reserve_is_three_seconds_of_each_section():
    assert SECTION_RESERVE == 3
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    no_discovery(planner)
    for request in (REQUEST, OTHER):
        confirmed(planner, request, 0, due=0)
    assert planner.pick(57.01) is None
    assert planner.next_wake(57.01) == pytest.approx(60)
    work = planner.pick(56.99)
    assert work is not None
    planner.finish(work, success(work.request, 57), 57)
    assert planner.pick(60) is not None


# T3: the next due time counts from the start of a read.


def test_due_time_counts_from_the_read_start():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    no_discovery(planner)
    confirmed(planner, REQUEST, 0, due=0)
    work = planner.pick(100)
    assert work is not None and work.started == 100
    planner.finish(work, success(work.request, 101.5), 101.5)
    record = planner.inventory.records[REQUEST.target]
    assert record.due == 160 and record.last_attempt == 100


@pytest.mark.parametrize(
    "result",
    [
        ReadResult(REQUEST, ReadState.ACCESS_ERROR, problem=TransportFailure.CONNECT),
        ReadResult(REQUEST, ReadState.PATH_ERROR, problem=TransportFailure.TIMEOUT),
        missing(REQUEST),
    ],
)
def test_failed_read_is_not_repeated_at_once(result):
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    no_discovery(planner)
    confirmed(planner, REQUEST, 0, due=0)
    work = planner.pick(100)
    planner.finish(work, result, 105)
    assert planner.inventory.records[REQUEST.target].due == 160
    assert planner.pick(106) is None


def test_deferred_read_stays_due_without_a_second_copy():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    no_discovery(planner)
    confirmed(planner, REQUEST, 0, due=50)
    work = planner.pick(100)
    deferred = ReadResult(
        REQUEST,
        ReadState.DEFERRED,
        problem=ReadProblem.REUSED_CONNECTION_CLOSED,
        retry_after_seconds=0.2,
    )
    planner.finish(work, deferred, 100.01)
    record = planner.inventory.records[REQUEST.target]
    assert record.due == 50 and planner.inflight == {}
    again = planner.pick(100.25)
    assert again is not None and again.request == REQUEST
    assert planner.pick(100.5) is None  # the same path is never twice in flight


def test_due_time_is_never_before_the_end_of_the_read():
    inventory = ResourceInventory(CONTEXT.identity)
    inventory.add(REQUEST, 0)
    inventory.accept(success(REQUEST, 100), 100, started=20)
    assert inventory.records[REQUEST.target].due == 100
    inventory.accept(success(REQUEST, 130), 130, started=140)
    assert inventory.records[REQUEST.target].due == 190
    # A deferred read sent nothing: the resource stays due as it was.
    deferred = ReadResult(REQUEST, ReadState.DEFERRED, problem=ReadProblem.BUSY)
    assert not inventory.accept(deferred, 150, started=149)
    record = inventory.records[REQUEST.target]
    assert record.due == 190 and record.last_attempt == 130


# T1: the service waits exactly for the later of the two gates.


class ClaimingClient:
    """Claims the shared budget like the real read client, without network."""

    def __init__(self, budget, clock):
        self.budget, self.clock = budget, clock
        self.calls = []
        self.hold = asyncio.Event()

    async def read(self, request):
        lease = self.budget.claim()
        if lease is None:
            return ReadResult(request, ReadState.DEFERRED, problem=ReadProblem.BUSY)
        self.calls.append((self.clock(), request))
        try:
            await self.hold.wait()
            return success(request, self.clock())
        finally:
            self.budget.release(lease)

    async def close(self):
        self.hold.set()


class TickingClock(Clock):
    """Every reading is one millisecond later, like a busy event loop."""

    def __call__(self):
        self.now += 0.001
        return self.now


@pytest.fixture
async def service(hass, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "start_offset", lambda scope: 0)
    services = []

    def build(clock):
        budget = LocalReadBudget(CONTEXT, clock=clock)
        reader = ClaimingClient(budget, clock)
        store = LocalScheduleStore(hass, "synthetic-scope")
        store._path = tmp_path / "schedule.json"
        result = LocalPollingService(
            CONTEXT,
            reader,
            budget,
            store,
            is_current=lambda: True,
            publish=lambda snapshot: None,
            clock=clock,
        )
        result._loaded = True
        result._next_save_check = float("inf")
        no_discovery(result.planner)
        result.confirm_identity(CONTEXT.identity)
        services.append(result)
        return result, reader

    yield build
    for result in services:
        await result.close()


def overdue(service, now, *requests):
    for request in requests:
        service.inventory.add(request, now - 120)
        service.inventory.accept(success(request, now - 100), now - 100)
        service.inventory.records[request.target].due = now - 1
    service.inventory.add(THIRD, now - 120)
    service.inventory.accept(success(THIRD, now - 100), now - 100)
    service.inventory.records[THIRD.target].due = now + 30


async def test_service_waits_only_until_the_start_gate_opens(service):
    """Regression: a closed gate made the loop sleep at least 250 ms."""
    clock = Clock()
    clock.now = 1000.0
    polling, reader = service(clock)
    overdue(polling, clock(), REQUEST, OTHER)
    polling.budget._state.root().admission_at = clock() + 0.03
    delay = await polling._step()
    assert not reader.calls
    assert delay == pytest.approx(0.03)


async def test_service_does_not_sleep_past_due_work_when_the_gate_opens(service):
    """Regression: when the gate opened between two clock readings, due work
    waited for the next future due time."""
    clock = TickingClock()
    clock.now = 1000.0
    polling, reader = service(clock)
    overdue(polling, clock(), REQUEST, OTHER)
    polling.budget._state.root().admission_at = clock.now + 0.0025
    delay = await polling._step()
    assert not reader.calls
    assert delay <= 0.05


async def test_service_starts_reads_at_the_budget_spacing(service):
    clock = Clock()
    clock.now = 1000.0
    polling, reader = service(clock)
    overdue(polling, clock(), REQUEST, OTHER)
    delay = await polling._step()
    await asyncio.sleep(0)
    assert len(reader.calls) == 1 and delay == pytest.approx(0.2)
    clock.advance(0.2)
    await polling._step()
    await asyncio.sleep(0)
    assert len(reader.calls) == 2
    assert reader.calls[1][0] - reader.calls[0][0] == pytest.approx(0.2)
    # Two reads run: the budget is busy, a finished read wakes the loop.
    clock.advance(0.2)
    assert await polling._step() == pytest.approx(module.BUSY_WAIT)
    reader.hold.set()


async def test_service_follows_the_brake(service):
    clock = Clock()
    clock.now = 1000.0
    polling, reader = service(clock)
    overdue(polling, clock(), REQUEST, OTHER)
    polling.budget.brake("timeout")
    assert await polling._step() == pytest.approx(0.4)
    await asyncio.sleep(0)
    assert polling.planner.next_start == pytest.approx(clock() + 0.4)
    clock.advance(0.2)
    await polling._step()
    await asyncio.sleep(0)
    assert len(reader.calls) == 1
    reader.hold.set()
