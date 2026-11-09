"""The latency brake follows the gateway, not Home Assistant (T6, T7, T8)."""

from __future__ import annotations

import asyncio
import logging
import random
import statistics
import time
from types import SimpleNamespace

import pytest

from custom_components.bosch_buderus_heating import local_read as reader
from custom_components.bosch_buderus_heating import local_transport as transport
from custom_components.bosch_buderus_heating.local_budget import (
    LocalBudgetError,
    LocalBudgetManager,
    LocalReadBudget,
)
from custom_components.bosch_buderus_heating.local_keepalive import ConnectionKind
from custom_components.bosch_buderus_heating.local_polling import service as module
from custom_components.bosch_buderus_heating.local_polling.inventory import (
    ResourceInventory,
)
from custom_components.bosch_buderus_heating.local_polling.planner import (
    Lane,
    PollPlanner,
    Work,
)
from custom_components.bosch_buderus_heating.local_polling.policy import (
    PollClass,
    candidates,
    category,
)
from custom_components.bosch_buderus_heating.local_probe import TransportFailure
from custom_components.bosch_buderus_heating.local_read import (
    BUDGET_CLOSED,
    LocalReadClient,
    LocalReadLimits,
    ReadResult,
    ReadState,
)
from custom_components.bosch_buderus_heating.local_transport import (
    CertificateBinding,
    PortRole,
)
from tests.test_local_budget import Clock
from tests.test_local_keepalive import answer, keepalive_gateway, reuse_client
from tests.test_local_keepalive import read_ok as keep_read_ok
from tests.test_local_polling import REQUEST, no_discovery, success
from tests.test_local_probe import CONTEXT
from tests.test_local_read import client, payload
from tests.test_local_read import clock as clock
from tests.test_local_throughput import service as service
from tests.test_local_transport import TOKEN, wire_reply
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import gateways as gateways

__all__ = ["keepalive_gateway"]


async def instant(raw, writer):
    writer.write(wire_reply(payload()))
    await writer.drain()


def phase(transport_client, name):
    return transport_client.health()["response_phases"][name]


# T6: what the brake measures.


@pytest.mark.usefixtures("socket_enabled")
async def test_busy_home_assistant_before_the_request_does_not_brake(gateways, clock):
    """Regression (deep review B1): the loop was busy for 300 ms during a
    read; that is no gateway time."""
    _, _, _, binding = await gateways(instant)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        budget._state.root().responses.clear()
        clock.advance(1)

        def block():
            time.sleep(0.3)  # other work of Home Assistant on the loop

        async def hog():
            await asyncio.sleep(0)
            block()

        busy = asyncio.create_task(hog())
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        await busy
    spacing = budget.start_spacing()
    assert spacing["median_response_ms"] < 100
    assert spacing["current_ms"] == 200
    # The time is still visible: it falls into a phase of its own.
    times = transport_client.health()["response_times"]
    assert times["maximum_ms"] >= 300


@pytest.mark.usefixtures("socket_enabled")
async def test_connection_setup_is_no_gateway_time(gateways, clock, monkeypatch):
    """Regression (deep review B2): a slow address check before the request."""
    _, _, _, binding = await gateways(instant)
    transport_client, budget = client(binding, clock)
    original = transport._resolve

    async def slow_resolve(endpoint):
        await asyncio.sleep(0.25)
        return await original(endpoint)

    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        budget._state.root().responses.clear()
        clock.advance(1)
        transport_client._resolved = None
        monkeypatch.setattr(transport, "_resolve", slow_resolve)
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
    assert budget.start_spacing()["median_response_ms"] < 100
    assert phase(transport_client, "connection_setup")["maximum_ms"] >= 250
    assert phase(transport_client, "gateway_wait")["maximum_ms"] < 100


@pytest.mark.usefixtures("socket_enabled")
async def test_reading_and_decoding_the_body_is_no_gateway_time(
    gateways, clock, monkeypatch
):
    _, _, _, binding = await gateways(instant)
    transport_client, budget = client(binding, clock)
    decode = reader.decode_resource

    def slow_decode(*args, **kwargs):
        time.sleep(0.25)
        return decode(*args, **kwargs)

    monkeypatch.setattr(reader, "decode_resource", slow_decode)
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
    assert budget.start_spacing()["median_response_ms"] < 100
    assert budget.start_spacing()["current_ms"] == 200
    assert phase(transport_client, "body_and_decode")["median_ms"] >= 250


@pytest.mark.usefixtures("socket_enabled")
async def test_a_slow_gateway_is_still_braked(gateways, clock):
    async def slow(raw, writer):
        await asyncio.sleep(0.35)
        writer.write(wire_reply(payload()))
        await writer.drain()

    _, _, _, binding = await gateways(slow)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
    spacing = budget.start_spacing()
    assert spacing["median_response_ms"] >= 350
    assert spacing["current_ms"] >= 525  # 1.5 times the gateway time
    assert phase(transport_client, "gateway_wait")["median_ms"] >= 350


@pytest.mark.usefixtures("socket_enabled")
async def test_phases_add_up_to_the_response_time(gateways, clock):
    _, _, _, binding = await gateways(instant)
    transport_client, _ = client(binding, clock)
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
    health = transport_client.health()
    parts = sum(
        health["response_phases"][name]["median_ms"]
        for name in ("connection_setup", "gateway_wait", "body_and_decode")
    )
    assert health["response_times"]["samples"] == 1
    assert parts == pytest.approx(health["response_times"]["average_ms"], abs=0.5)


@pytest.mark.usefixtures("socket_enabled")
async def test_rate_limit_sample_excludes_saving_the_pause(
    hass, tmp_path, gateways, clock, monkeypatch
):
    """T8 (N2): saving the received pause is no gateway or response time."""

    async def limited(raw, writer):
        writer.write(wire_reply(b"{}", status=429, headers={"Retry-After": "30"}))
        await writer.drain()

    _, _, _, binding = await gateways(limited)
    manager = LocalBudgetManager(hass)
    manager._path = tmp_path / "local-budget.json"
    handle = await manager.budget(binding.context)
    persist = manager.persist

    async def slow_persist():
        await asyncio.sleep(0.3)
        await persist()

    monkeypatch.setattr(manager, "persist", slow_persist)
    transport_client, _ = client(binding, clock, budget=handle)
    async with transport_client:
        result = await transport_client.read(REQUEST)
    assert result.status == 429
    assert handle.start_spacing()["median_response_ms"] < 150
    assert transport_client.health()["response_times"]["maximum_ms"] < 250
    await manager.close()


# T7: timeouts.


@pytest.mark.usefixtures("socket_enabled")
async def test_timeout_on_a_kept_connection_does_not_brake(keepalive_gateway, clock):
    stall = asyncio.Event()

    async def stall_second(raw, writer, number):
        if number == 2:
            await stall.wait()
        answer(writer)

    _, _, binding = await keepalive_gateway(stall_second)
    transport_client, budget = reuse_client(
        binding, clock, limits=LocalReadLimits(timeout=0.2)
    )
    async with transport_client:
        await keep_read_ok(transport_client, clock)
        clock.advance()
        result = await transport_client.read(REQUEST)
        stall.set()
    assert result.problem == TransportFailure.TIMEOUT
    # Only the keep-alive fallback counts it.
    assert transport_client.connection_reuse()["reuse_failures"] == {"timeout": 1}
    assert budget.start_spacing()["brake_active"] is False


@pytest.mark.usefixtures("socket_enabled")
async def test_timeout_on_a_new_connection_brakes(gateways, clock):
    async def never(raw, writer):
        await asyncio.sleep(1)

    _, _, _, binding = await gateways(never)
    limits = LocalReadLimits(timeout=0.3)
    transport_client, budget = client(binding, clock, limits=limits)
    async with transport_client:
        result = await transport_client.read(REQUEST)
    assert result.problem == TransportFailure.TIMEOUT
    assert budget.start_spacing()["brake_reason"] == "timeout"


def test_doubled_spacing_stays_at_most_one_second():
    clock = Clock()
    budget = LocalReadBudget(CONTEXT, clock=clock)
    for _ in range(32):
        budget.observe(0.6)  # 1.5 times: 0.9 s
    assert budget.start_gap() == pytest.approx(0.9)
    budget.brake("server_error")
    assert budget.start_gap() == 1.0


@pytest.mark.parametrize("state", [ReadState.PATH_ERROR, ReadState.ACCESS_ERROR])
def test_timed_out_read_is_repeated_once_early(state):
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    no_discovery(planner)
    planner.inventory.add(REQUEST, 0)
    planner.inventory.accept(success(REQUEST, 0), 0)
    planner.inventory.records[REQUEST.target].due = 0
    timeout = ReadResult(REQUEST, state, problem=TransportFailure.TIMEOUT)
    record = planner.inventory.records[REQUEST.target]
    work = planner.pick(100)
    planner.finish(work, timeout, 105)
    assert record.due == 105  # at once, after the start spacing
    again = planner.pick(105.2)
    assert again is not None
    planner.finish(again, timeout, 110)
    assert record.due == pytest.approx(165.2)  # a second timeout: its period
    third = planner.pick(165.2)
    planner.finish(third, success(REQUEST, 166), 166)
    assert record.due == pytest.approx(225.2) and not record.timeouts
    # After a timeout and a successful early retry, the next timeout is
    # repeated early again.
    fourth = planner.pick(225.2)
    planner.finish(fourth, timeout, 230)
    assert record.due == 230
    fifth = planner.pick(230.2)
    planner.finish(fifth, success(REQUEST, 231), 231)
    sixth = planner.pick(290.2)
    planner.finish(sixth, timeout, 295)
    assert record.due == 295


# T8 (N3): Home Assistant stops.


@pytest.mark.usefixtures("socket_enabled")
async def test_reads_after_the_stop_report_a_closed_budget(
    hass, tmp_path, gateways, clock
):
    _, _, _, binding = await gateways(instant)
    manager = LocalBudgetManager(hass)
    manager._path = tmp_path / "local-budget.json"
    handle = await manager.budget(binding.context)
    transport_client, _ = client(binding, clock, budget=handle)
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        await manager.close()  # what happens on EVENT_HOMEASSISTANT_STOP
        result = await transport_client.read(REQUEST)
    assert result.state is ReadState.ACCESS_ERROR
    assert result.problem == BUDGET_CLOSED
    with pytest.raises(LocalBudgetError, match="local_budget_closed"):
        manager.check_ready()


async def test_service_stops_quietly_when_the_budget_closed(service, caplog):
    clock = Clock()
    clock.now = 1000.0
    polling, _ = service(clock)

    class Closed:
        calls = 0

        async def read(self, request):
            Closed.calls += 1
            return ReadResult(request, ReadState.ACCESS_ERROR, problem=BUDGET_CLOSED)

        async def close(self):
            return None

    polling.client = Closed()
    polling.inventory.add(REQUEST, clock())
    work = Work(REQUEST, Lane.REGULAR, 1, clock())
    polling.planner.inflight[REQUEST.target] = work
    with caplog.at_level(logging.DEBUG):
        await polling._read(work)
        async with asyncio.timeout(5):
            await polling._run()  # ends at once, no further read
    assert Closed.calls == 1
    assert not [r for r in caplog.records if r.levelno > logging.DEBUG]
    assert polling._fault is None and polling._blocked is None


async def test_service_stops_quietly_when_the_budget_raises_closed(service, caplog):
    clock = Clock()
    clock.now = 1000.0
    polling, _ = service(clock)

    class Raising:
        async def read(self, request):
            raise LocalBudgetError(BUDGET_CLOSED)

        async def close(self):
            return None

    polling.client = Raising()
    polling.inventory.add(REQUEST, clock())
    work = Work(REQUEST, Lane.REGULAR, 1, clock())
    polling.planner.inflight[REQUEST.target] = work
    with caplog.at_level(logging.DEBUG):
        await polling._read(work)
    assert polling._stopping and polling._fault is None
    assert not [r for r in caplog.records if r.levelno > logging.DEBUG]


# T6/T7: throughput with the real read client measurement (virtual time).


class VirtualLoop(asyncio.SelectorEventLoop):
    """Time advances only while the loop would wait."""

    def __init__(self):
        super().__init__()
        self._now = 1000.0
        self._selector.select = self._select

    def time(self):
        return self._now

    def _select(self, timeout=None):
        if timeout:
            self._now += timeout
        return []

    def burn(self, seconds):
        """Work on the loop that blocks every other callback meanwhile."""
        self._now += seconds


class Profile(SimpleNamespace):
    setup: float = 0.0  # before the request headers (waiting, not CPU)
    gateway: float = 0.03
    after: float = 0.0  # body transfer and waiting for the loop afterwards
    cpu: float = 0.002  # decoding on the loop, blocking everything else
    timeout_every: int = 0
    kind: ConnectionKind = ConnectionKind.REUSED


class SimulatedClient(LocalReadClient):
    """The real read client, only its network part is simulated."""

    def __init__(self, loop, profile, budget, rng):
        binding = CertificateBinding(CONTEXT, PortRole.RESOURCES, b"\0" * 32)
        super().__init__(
            CONTEXT, TOKEN, binding, budget=budget, is_current=lambda: True
        )
        self.loop, self.profile, self.rng = loop, profile, rng
        self.reads = 0

    async def _execute_counted(self, request, attempt):
        profile, rng, loop = self.profile, self.rng, self.loop
        self.reads += 1
        attempt.connection = profile.kind
        await asyncio.sleep(profile.setup * rng.uniform(0.7, 1.3))
        trace = SimpleNamespace(trace_request_ctx=attempt)
        await self._headers_sent(None, trace, None)
        if profile.timeout_every and rng.random() < 1 / profile.timeout_every:
            await asyncio.sleep(5.0)
            return ReadResult(
                request,
                ReadState.PATH_ERROR,
                problem=TransportFailure.TIMEOUT,
                http_requests=1,
            )
        wait = profile.gateway * rng.lognormvariate(0, 0.3)
        if rng.random() < 0.05:
            wait += rng.uniform(0.2, 0.5)
        await asyncio.sleep(wait)
        attempt.answered = loop.time()
        attempt.status = 200
        await asyncio.sleep(profile.after * rng.uniform(0.7, 1.3))
        loop.burn(profile.cpu)
        attempt.done = loop.time()
        await asyncio.sleep(0)
        return success(request, loop.time())


class Store:
    async def load(self):
        return None

    async def save(self, value):
        return None

    async def set_aside(self):
        return None


def run_plant(monkeypatch, profile, *, warmup=240.0, measure=900.0, seed=3):
    loop = VirtualLoop()
    monkeypatch.setattr(reader, "monotonic", loop.time)
    monkeypatch.setattr(module, "start_offset", lambda scope: 0)
    rng = random.Random(seed)

    async def main():
        budget = LocalReadBudget(CONTEXT, clock=loop.time)
        read_client = SimulatedClient(loop, profile, budget, rng)
        polling = module.LocalPollingService(
            CONTEXT,
            read_client,
            budget,
            Store(),
            is_current=lambda: True,
            publish=lambda snapshot: None,
            clock=loop.time,
        )
        no_discovery(polling.planner)
        inventory = polling.inventory
        groups = {c: [] for c in PollClass}
        for request in candidates():
            groups[category(request)].append(request)
        now = loop.time()
        chosen = (
            [(r, 60) for r in groups[PollClass.LIVE][:153]]
            + [(r, 300) for r in groups[PollClass.SETTINGS][:5]]
            + [(r, 900) for r in groups[PollClass.METADATA][:10]]
            + [(r, 3600) for r in groups[PollClass.SYSTEM_METADATA][:6]]
        )
        live = set()
        for request, period in chosen:
            inventory.add(request, now - 4000)
            observed = now - rng.uniform(0, period)
            inventory.accept(success(request, observed), observed)
            if period == 60:
                live.add(request.target)
        intervals, measuring = [], [False]
        accept = inventory.accept

        def spy(result, at, **kwargs):
            value = accept(result, at, **kwargs)
            record = inventory.records[result.request.target]
            if (
                measuring[0]
                and result.request.target in live
                and result.state is ReadState.SUCCESS
                and record.actual_interval is not None
            ):
                intervals.append(record.actual_interval)
            return value

        inventory.accept = spy
        polling.confirm_identity(CONTEXT.identity)
        await polling.start()
        await asyncio.sleep(warmup)
        measuring[0] = True
        await asyncio.sleep(measure)
        spacing = budget.start_spacing()
        await polling.close()
        return intervals, spacing

    try:
        return loop.run_until_complete(main())
    finally:
        loop.close()


def summary(intervals):
    return (
        statistics.median(intervals),
        statistics.quantiles(intervals, n=20)[-1],
        max(intervals),
    )


@pytest.mark.parametrize(
    ("setup", "after"),
    [(0.06, 0.17), (0.08, 0.2), (0.1, 0.25)],
)
def test_live_values_hold_60_s_with_slow_home_assistant(monkeypatch, setup, after):
    """The observed plant: 250-350 ms end to end, ~30 ms of it the gateway."""
    intervals, spacing = run_plant(
        monkeypatch, Profile(setup=setup, gateway=0.03, after=after)
    )
    median, p95, maximum = summary(intervals)
    assert median <= 60.5 and p95 <= 61.5 and maximum <= 66
    assert spacing["current_ms"] == 200


def test_live_values_hold_60_s_in_the_keep_alive_fallback(monkeypatch):
    """A new connection per read: the handshake is no gateway time."""
    intervals, spacing = run_plant(
        monkeypatch,
        Profile(setup=0.15, gateway=0.03, after=0.17, kind=ConnectionKind.NEW),
    )
    median, p95, maximum = summary(intervals)
    assert median <= 60.5 and p95 <= 61.5 and maximum <= 66
    assert spacing["current_ms"] == 200


@pytest.mark.parametrize("kind", [ConnectionKind.REUSED, ConnectionKind.NEW])
def test_occasional_timeouts_never_let_a_live_value_expire(monkeypatch, kind):
    intervals, _ = run_plant(
        monkeypatch,
        Profile(setup=0.06, gateway=0.03, after=0.17, timeout_every=300, kind=kind),
    )
    median, _, maximum = summary(intervals)
    assert maximum < 180  # never expired (3 periods)
    if kind is ConnectionKind.REUSED:
        # Only the keep-alive fallback counts these: no brake at all.
        assert median <= 60.5


def test_a_genuinely_slow_gateway_is_still_braked(monkeypatch):
    intervals, spacing = run_plant(
        monkeypatch, Profile(setup=0.02, gateway=0.33, after=0.02)
    )
    assert spacing["current_ms"] >= 450
    assert statistics.median(intervals) > 60


def test_setup_shows_a_closed_budget_as_a_storage_problem():
    from custom_components.bosch_buderus_heating.local_flow import local_error

    assert local_error(LocalBudgetError(BUDGET_CLOSED)) == "local_storage_failed"
