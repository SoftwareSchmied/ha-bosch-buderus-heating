"""Exercise asynchronous scheduling, independent expiry and actual private files."""

import asyncio
import json
import logging
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating import local_budget
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_budget import (
    LocalBudgetError,
    LocalBudgetManager,
    LocalReadBudget,
)
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
    # A schedule save writes in the executor: let it finish first.
    async with asyncio.timeout(5), service.store._lock:
        pass
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
        ("local_identity_conflict", LocalCondition.UNAVAILABLE),
    ],
)
async def test_auth_certificate_and_identity_errors_stop_automatic_work(
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


@pytest.mark.parametrize("content", ['{"version":999}', "{not json", "x" * 2**20 + "x"])
async def test_corrupt_checkpoint_is_kept_as_backup_and_reads_start_fresh(
    setup, caplog, content
):
    service, clock, client, _ = setup
    path = service.store._path
    backup = path.with_name(path.name + ".corrupt")
    backup.write_text("an older backup")
    path.write_text(content)
    service.confirm_identity(CONTEXT.identity)
    with caplog.at_level(logging.WARNING):
        await service.start()
        await settle()
    assert service.condition is LocalCondition.READY and len(client.calls) == 1
    assert backup.read_text() == content  # the one backup, replacing the older
    assert not path.exists()
    assert caplog.text.count("could not be used") == 1
    assert service.snapshot().resources[0].available
    await advance(service, clock, 60)
    assert len(client.calls) == 2
    await service.close()
    assert json.loads(path.read_text())["records"][0]["confirmed"]
    assert backup.read_text() == content
    assert list(path.parent.glob("*.corrupt")) == [backup]


async def test_checkpoint_failure_keeps_reading_and_saves_again_later(
    setup, monkeypatch, caplog
):
    service, clock, client, _ = setup
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    original = service.store.save
    failing = AsyncMock(side_effect=ScheduleStorageError())
    monkeypatch.setattr(service.store, "save", failing)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            await advance(service, clock, 60)
    # The reads go on with the schedule in memory.
    assert service.condition is LocalCondition.READY
    assert service.snapshot().problem is None
    assert service.snapshot().resources[0].available and len(client.calls) == 4
    assert failing.await_count == 2  # tried again after 60 s, then after 120 s
    warnings = [r for r in caplog.records if "could not be saved" in r.getMessage()]
    assert len(warnings) == 1  # logged once
    monkeypatch.setattr(service.store, "save", original)
    await advance(service, clock, 60)
    assert json.loads(service.store._path.read_text())["records"][0]["confirmed"]


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
    original = storage.write_utf8_file_atomic

    def blocked(*args, **kwargs):
        begun.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "write_utf8_file_atomic", blocked)
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


async def test_unexpected_planner_failure_pauses_own_work(setup, monkeypatch):
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
    assert "private" not in repr(service.snapshot())


# --------------------------------------------- recovery after a problem (A2)


def failing_once(client, error):
    """The next read raises ``error``; later reads answer normally."""
    original, pending = client.read, [error]

    async def read(request):
        if pending:
            client.calls.append(request)
            raise pending.pop()
        return await original(request)

    client.read = read


async def test_transient_read_error_pauses_reads_then_they_resume(setup, caplog):
    service, clock, client, _ = setup
    failing_once(client, RuntimeError("synthetic private detail"))
    service.confirm_identity(CONTEXT.identity)
    with caplog.at_level(logging.WARNING):
        await service.start()
        await settle()
    assert service.condition is LocalCondition.UNAVAILABLE
    assert service.snapshot().problem == "local_poll_failed"
    assert "private" not in caplog.text
    await advance(service, clock, 59)
    assert len(client.calls) == 1  # no read during the pause
    await advance(service, clock, 1)
    assert len(client.calls) == 2
    assert service.condition is LocalCondition.READY
    assert service.snapshot().problem is None
    assert service.snapshot().resources[0].available
    assert service._faults == 0  # a working read restarts the backoff


async def test_repeated_errors_back_off_longer_and_stay_bounded(setup):
    service, clock, client, _ = setup
    client.read = AsyncMock(side_effect=RuntimeError("synthetic"))
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    reads = [client.read.await_count]
    for _ in range(40):  # 40 minutes
        await advance(service, clock, 60)
        reads.append(client.read.await_count)
    # Read again after 1, 2, 4, 8 and then every 15 minutes, never stopped.
    starts = [minute for minute in range(1, 41) if reads[minute] > reads[minute - 1]]
    assert starts == [1, 3, 7, 15, 30]
    assert service.condition is LocalCondition.UNAVAILABLE


async def test_read_budget_error_pauses_reads_then_they_resume(setup):
    service, clock, client, _ = setup
    failing_once(client, LocalBudgetError("local_budget_storage_failed"))
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    assert service.snapshot().problem == "local_budget_storage_failed"
    client.result = ReadResult(
        REQUEST, ReadState.ACCESS_ERROR, problem="local_budget_storage_failed"
    )
    await advance(service, clock, 60)
    assert len(client.calls) == 2 and service._faults == 2
    client.result = None
    await advance(service, clock, 120)
    assert len(client.calls) == 3
    assert service.condition is LocalCondition.READY


async def test_failed_budget_store_write_keeps_the_pause_and_the_reads(
    hass, tmp_path, monkeypatch, caplog
):
    clock = Clock()
    manager = LocalBudgetManager(hass, clock=clock)
    manager._path = tmp_path / "budget.json"
    await manager.initialize()
    budget = await manager.budget(CONTEXT)
    original = local_budget._save

    def fail(*args):
        raise OSError("synthetic private storage path")

    monkeypatch.setattr(local_budget, "_save", fail)
    monkeypatch.setattr(module, "start_offset", lambda scope: 0)
    client = Client(clock)
    client.result = ReadResult(
        REQUEST, ReadState.ACCESS_ERROR, problem=ReadProblem.SERVICE_UNAVAILABLE
    )
    store = LocalScheduleStore(hass, "synthetic-scope")
    store._path = tmp_path / "schedule.json"
    service = LocalPollingService(
        CONTEXT,
        client,
        budget,
        store,
        is_current=lambda: True,
        publish=lambda snapshot: None,
        clock=clock,
    )
    no_discovery(service.planner)
    service.planner.hints((REQUEST,), clock())
    try:
        service.confirm_identity(CONTEXT.identity)
        with caplog.at_level(logging.WARNING):
            await service.start()
            await settle()
        # The pause holds in memory although it could not be saved.
        assert service.condition is LocalCondition.PAUSED
        assert budget.pause_seconds == 60 and len(client.calls) == 1
        assert "could not be saved" in caplog.text and "private" not in caplog.text
        assert manager._unsaved and manager._checkpoint is not None
        monkeypatch.setattr(local_budget, "_save", original)
        manager._checkpoint._run()
        await hass.async_block_till_done()
        assert local_budget._load(manager._path)[0]["remaining"] == 60
        assert not manager._unsaved
        client.result = None
        await advance(service, clock, 60)
        assert len(client.calls) == 2
        assert service.condition is LocalCondition.READY
        assert service.snapshot().resources[0].available
    finally:
        await service.close()
        await manager.close()


async def test_listener_error_pauses_reads_then_they_resume(setup):
    service, clock, client, published = setup
    failed = []

    def listener(snapshot):
        if not failed:
            failed.append(snapshot)
            raise RuntimeError("synthetic private detail")
        published.append(snapshot)

    service._publish_callback = listener
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    assert service.snapshot().problem == "local_listener_failed"
    assert service.condition is LocalCondition.UNAVAILABLE and not client.calls
    await advance(service, clock, 60)
    assert len(client.calls) == 1
    assert published[-1].condition is LocalCondition.READY
    assert published[-1].resources[0].available


async def test_unreadable_schedule_is_loaded_again_later(setup, monkeypatch):
    service, clock, client, _ = setup
    path = service.store._path
    saved = checkpoint(service.planner, clock())
    await service.store.save(saved)
    original = path.read_bytes()
    read = service.store._load

    def unreadable():
        raise OSError("synthetic private read error")

    monkeypatch.setattr(service.store, "_load", unreadable)
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    assert service.snapshot().problem == "local_schedule_storage_failed"
    await advance(service, clock, 60)
    # Passing read errors are retried; the file is neither moved nor rewritten.
    assert not client.calls and path.read_bytes() == original
    assert not path.with_name(path.name + ".corrupt").exists()
    monkeypatch.setattr(service.store, "_load", read)
    await advance(service, clock, 120)
    assert len(client.calls) == 1
    assert service.condition is LocalCondition.READY


# --------------------------------------------- schedule file writes (A5)


def counted_saves(service, monkeypatch):
    original, saves = service.store.save, []

    async def save(value):
        saves.append(value)
        await original(value)

    monkeypatch.setattr(service.store, "save", save)
    return saves


async def test_unchanged_schedule_is_not_written_again(setup, monkeypatch):
    service, clock, client, _ = setup
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    saves = counted_saves(service, monkeypatch)
    await advance(service, clock, 60)
    assert len(saves) == 1  # nothing was saved before
    before = service.store._path.stat().st_mtime_ns
    for _ in range(25):
        await advance(service, clock, 60)
    assert len(saves) == 1 and len(client.calls) == 27
    assert service.store._path.stat().st_mtime_ns == before
    # Only the timing changed (lane rotation): saved again after 30 minutes.
    for _ in range(5):
        await advance(service, clock, 60)
    assert len(saves) == 2


async def test_a_real_change_is_written_soon(setup, monkeypatch):
    service, clock, _, _ = setup
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    await advance(service, clock, 60)
    saves = counted_saves(service, monkeypatch)
    await advance(service, clock, 60)
    assert not saves
    other = request_for_path("/heatingCircuits/hc1/currentRoomSetpoint")
    service.planner.hints((other,), clock())
    await advance(service, clock, 60)
    assert len(saves) == 1
    assert {r["path"] for r in saves[0]["records"]} == {REQUEST.path, other.path}


async def test_stop_writes_the_schedule(setup, monkeypatch):
    service, clock, _, _ = setup
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    await advance(service, clock, 60)
    saves = counted_saves(service, monkeypatch)
    await advance(service, clock, 60)
    assert not saves
    await service.close()
    assert len(saves) == 1


# --------------------------------------------------- compact stores (O63)


async def test_schedules_are_written_compact_and_pretty_ones_still_load(
    setup, monkeypatch
):
    from custom_components.bosch_buderus_heating.local_polling import storage

    service, clock, _, _ = setup
    data = checkpoint(service.planner, clock())
    dumps, calls = storage.json.dumps, []

    def counted(*args, **kwargs):
        calls.append(1)
        return dumps(*args, **kwargs)

    monkeypatch.setattr(storage.json, "dumps", counted)
    await service.store.save(data)
    assert len(calls) == 1  # serialized once
    body = service.store._path.read_bytes()
    assert body == json.dumps(data, separators=(",", ":")).encode()
    assert b"\n" not in body and b", " not in body
    assert await service.store.load() == data
    # Written before with indentation: loads the same way.
    service.store._path.write_text(json.dumps(data, indent=2))
    assert await service.store.load() == data
    # Non-finite numbers are never written.
    with pytest.raises(ValueError, match="Out of range float"):
        await service.store.save({**data, "rescan_remaining": float("nan")})
    assert await service.store.load() == data


async def test_a_schedule_write_is_checked_on_its_bytes(setup, monkeypatch):
    from custom_components.bosch_buderus_heating.local_polling import storage

    service, clock, _, _ = setup

    def truncated(path, body, private, mode):
        Path(path).write_bytes(body[:-1])

    monkeypatch.setattr(storage, "write_utf8_file_atomic", truncated)
    with pytest.raises(ScheduleStorageError):
        await service.store.save(checkpoint(service.planner, clock()))
