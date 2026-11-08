"""Exercise asynchronous scheduling, independent expiry and actual private files."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating.local_budget import LocalReadBudget
from custom_components.bosch_buderus_heating.local_polling import service as module
from custom_components.bosch_buderus_heating.local_polling.inventory import Freshness
from custom_components.bosch_buderus_heating.local_polling.service import (
    LocalPollingService,
)
from custom_components.bosch_buderus_heating.local_polling.storage import (
    LocalScheduleStore,
    ScheduleStorageError,
    checkpoint,
)
from custom_components.bosch_buderus_heating.local_read import (
    LocalReadClient,
    ReadProblem,
    ReadResult,
    ReadState,
)
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.local_transport import StaleLocalRequest
from tests.test_local_budget import Clock
from tests.test_local_polling import REQUEST, no_discovery, success
from tests.test_local_probe import CONTEXT
from tests.test_local_read import payload
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import gateways as gateways
from tests.test_local_transport import wire_reply


class Client:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.closed = False
        self.result = None
        self.hold = None

    async def read(self, request):
        self.calls.append(request)
        if self.hold is not None:
            await self.hold.wait()
        return self.result or success(request, self.clock())

    async def close(self):
        self.closed = True


@pytest.fixture
async def setup(hass, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "start_offset", lambda scope: 0)
    clock = Clock()
    client = Client(clock)
    budget = LocalReadBudget(CONTEXT, clock=clock)
    store = LocalScheduleStore(hass, "synthetic-scope")
    store._path = tmp_path / "schedule.json"
    published = []
    service = LocalPollingService(
        CONTEXT,
        client,
        budget,
        store,
        is_current=lambda: True,
        publish=published.append,
        clock=clock,
    )
    no_discovery(service.planner)
    service.planner.hints((REQUEST,), clock())
    yield service, clock, client, published
    await service.close()


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def advance(service, clock, seconds):
    clock.advance(seconds)
    service.wake()
    await settle()


async def test_identity_gate_then_actual_age_changes_without_refreshing_observation(
    setup,
):
    service, clock, client, published = setup
    await service.start()
    await settle()
    assert not client.calls
    assert service.condition is LocalCondition.INITIALIZING
    service.confirm_identity(CONTEXT.identity)
    await settle()
    assert len(client.calls) == 1
    observation = service.snapshot().resources[0].observation
    assert service.snapshot().resources[0].available
    await service.budget.pause(7200)
    await advance(service, clock, 60)
    assert service.snapshot().resources[0].freshness is Freshness.LATE
    await advance(service, clock, 120)
    assert service.snapshot().resources[0].freshness is Freshness.EXPIRED
    assert service.snapshot().resources[0].observation is observation
    assert len(client.calls) == 1 and published[-1].condition is LocalCondition.PAUSED


async def test_expiry_timer_runs_while_main_loop_is_waiting_for_checkpoint(
    setup, monkeypatch
):
    service, clock, client, published = setup
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.store.save

    async def blocked(value):
        entered.set()
        await release.wait()
        await original(value)

    monkeypatch.setattr(service.store, "save", blocked)
    await advance(service, clock, 60)
    await entered.wait()
    await advance(service, clock, 120)
    assert any(s.resources[0].freshness is Freshness.EXPIRED for s in published)
    assert len(client.calls) == 1
    release.set()
    await service.close()


@pytest.mark.parametrize(
    "problem,condition",
    [
        (ReadProblem.AUTH_REQUIRED, LocalCondition.AUTH_REQUIRED),
        ("certificate_changed", LocalCondition.CERTIFICATE_CHANGED),
        ("local_budget_storage_failed", LocalCondition.UNAVAILABLE),
    ],
)
async def test_auth_certificate_and_storage_errors_stop_automatic_work(
    setup, problem, condition
):
    service, clock, client, _ = setup
    client.result = ReadResult(REQUEST, ReadState.ACCESS_ERROR, problem=problem)
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    assert service.condition is condition and len(client.calls) == 1
    await advance(service, clock, 86400)
    assert len(client.calls) == 1
    with pytest.raises(StaleLocalRequest):
        service.confirm_identity(CONTEXT.identity)


async def test_transient_failure_backoff_does_not_shorten_gateway_pause(setup):
    service, clock, _, _ = setup
    result = ReadResult(
        REQUEST, ReadState.ACCESS_ERROR, problem=ReadProblem.SERVICE_UNAVAILABLE
    )
    for delay in (60, 120, 300, 600, 600):
        await service._access_error(result)
        assert service.budget.pause_seconds == delay
        clock.advance(delay)
    await service.budget.pause(7200)
    await service._access_error(result)
    assert service.budget.pause_seconds == 7200
    service.confirm_identity(CONTEXT.identity)
    assert service._failures == 0 and service.budget.pause_seconds == 7200


async def test_explicit_deadline_expires_during_long_pause_without_any_request(setup):
    service, clock, client, _ = setup
    service.confirm_identity(CONTEXT.identity)
    await service.budget.pause(7200)
    await service.start()
    operation = service.request((REQUEST,))
    await advance(service, clock, 120)
    assert operation.done and operation.problem == "deadline"
    assert not client.calls and not service.planner.operations


async def test_stale_generation_and_close_cannot_publish_late_success(setup):
    service, _, client, _ = setup
    active = True
    service._current = lambda: active
    client.hold = asyncio.Event()
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    assert len(client.calls) == 1
    active = False
    client.hold.set()
    service.wake()
    await settle()
    assert service.inventory.records[REQUEST.target].observation is None
    assert service.condition is LocalCondition.UNAVAILABLE
    await service.close()
    assert client.closed and not service._workers and not service.planner.inflight
    assert service._main.done() and service._age.done()


async def test_corrupt_checkpoint_is_preserved_and_prevents_network(setup):
    service, _, client, _ = setup
    service.store._path.write_text('{"version":999}')
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    assert service.condition is LocalCondition.UNAVAILABLE and not client.calls
    await service.close()
    assert service.store._path.read_text() == '{"version":999}'


async def test_checkpoint_failure_stops_reads_and_keeps_existing_values_historical(
    setup, monkeypatch
):
    service, clock, client, _ = setup
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    monkeypatch.setattr(
        service.store, "save", AsyncMock(side_effect=ScheduleStorageError())
    )
    await advance(service, clock, 60)
    assert service.condition is LocalCondition.UNAVAILABLE
    assert service.snapshot().resources[0].observation is not None
    assert not service.snapshot().resources[0].available and len(client.calls) == 1
    await advance(service, clock, 60)
    assert len(client.calls) == 1


async def test_checkpoint_load_failure_and_size_limit_are_fixed_errors(setup):
    service, _, _, _ = setup
    path = service.store._path
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ScheduleStorageError):
        await service.store.load()
    with pytest.raises(ScheduleStorageError):
        await service.store.save({"large": "x" * (1024 * 1024)})
    assert path.stat().st_size == 1024 * 1024 + 1


async def test_cancelled_disk_write_finishes_before_lock_is_released(
    setup, monkeypatch
):
    from custom_components.bosch_buderus_heating.local_polling import storage

    service, clock, _, _ = setup
    begun, release = threading.Event(), threading.Event()
    original = storage.save_json

    def blocked(*args, **kwargs):
        begun.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "save_json", blocked)
    data = checkpoint(service.planner, clock())
    task = asyncio.create_task(service.store.save(data))
    await asyncio.to_thread(begun.wait, 2)
    assert begun.is_set()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert service.store._lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await service.store.load() == data


async def test_restart_retains_paths_but_requires_fresh_identity_and_values(setup):
    service, clock, _, _ = setup
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    await service.close()
    saved = json.loads(service.store._path.read_text())
    assert saved["records"][0]["confirmed"] and "value" not in saved["records"][0]
    restarted = LocalPollingService(
        CONTEXT,
        Client(clock),
        service.budget,
        service.store,
        is_current=lambda: True,
        publish=lambda result: None,
        clock=clock,
    )
    try:
        await restarted.start()
        await settle()
        assert restarted.inventory.records[REQUEST.target].observation is None
        assert not restarted.client.calls
        restarted.confirm_identity(CONTEXT.identity)
        await settle()
        assert restarted.snapshot().resources[0].available
        assert len(restarted.client.calls) == 1
    finally:
        await restarted.close()


@pytest.mark.usefixtures("socket_enabled")
async def test_scheduler_uses_existing_client_and_gateway_budget_over_actual_tls(
    setup, gateways
):
    service, _, _, _ = setup

    async def reply(raw, writer):
        writer.write(wire_reply(payload()))
        await writer.drain()

    _, seen, _, binding = await gateways(reply)
    # Actual transport observation times use the process monotonic clock.
    from time import monotonic

    service._clock = monotonic
    service.planner.epoch = monotonic()
    service.planner.next_start = service.planner.epoch
    service.inventory.records[REQUEST.target].due = service.planner.epoch
    service.budget = LocalReadBudget(CONTEXT)
    service.client = LocalReadClient(
        CONTEXT,
        "synthetic-token",
        binding,
        budget=service.budget,
        is_current=lambda: True,
    )
    service.confirm_identity(CONTEXT.identity)
    available = asyncio.Event()
    service._publish_callback = lambda snapshot: (
        available.set() if snapshot.resources[0].available else None
    )
    await service.start()
    async with asyncio.timeout(3):
        await available.wait()
    assert len(seen) == 1 and seen[0].startswith(
        b"GET /heatingCircuits/hc2/currentRoomSetpoint "
    )
    assert service.client.metrics.http_requests == 1
    await service.close()


async def test_close_waiter_cancellation_does_not_cancel_owned_cleanup(
    setup, monkeypatch
):
    service, _, client, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()
        client.closed = True

    monkeypatch.setattr(client, "close", close)
    waiting = asyncio.create_task(service.close())
    await entered.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not service._close_task.done()
    release.set()
    await service.close()
    assert client.closed


async def test_cancelled_start_keeps_checkpoint_that_was_not_loaded(setup, monkeypatch):
    service, clock, client, _ = setup
    data = checkpoint(service.planner, clock())
    await service.store.save(data)
    original = service.store._path.read_bytes()
    entered, release = asyncio.Event(), asyncio.Event()

    async def loading():
        entered.set()
        await release.wait()
        return data

    monkeypatch.setattr(service.store, "load", loading)
    task = asyncio.create_task(service.start())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await service.close()
    assert service.store._path.read_bytes() == original
    assert client.closed and not client.calls


async def test_unexpected_planner_failure_stops_own_work(setup, monkeypatch):
    service, _, client, _ = setup

    def broken(now):
        raise RuntimeError("synthetic private detail")

    monkeypatch.setattr(service.planner, "pick", broken)
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    assert service.condition is LocalCondition.UNAVAILABLE
    assert service.snapshot().problem == "local_poll_failed"
    assert not client.calls


async def test_unexpected_client_and_listener_errors_are_fixed_codes(
    setup, monkeypatch
):
    service, _, client, _ = setup
    monkeypatch.setattr(
        client, "read", AsyncMock(side_effect=RuntimeError("private synthetic detail"))
    )
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    assert service.snapshot().problem == "local_poll_failed"

    def listener(snapshot):
        raise RuntimeError("private synthetic detail")

    service._publish_callback = listener
    service._signature = None
    service._publish()
    assert service.snapshot().problem == "local_listener_failed"
