"""Gateway-wide bounds, durable pauses and storage failure isolation."""

import asyncio
import json
import threading
from dataclasses import replace
from unittest.mock import patch

import pytest

from custom_components.bosch_buderus_heating import local_budget as module
from custom_components.bosch_buderus_heating.local_budget import (
    MINIMUM_START_GAP,
    BudgetDeferred,
    LocalBudgetError,
    LocalBudgetManager,
    LocalReadBudget,
    local_budget_manager,
)
from custom_components.bosch_buderus_heating.local_probe import LocalEndpoint
from custom_components.bosch_buderus_heating.local_transport import StaleLocalRequest
from tests.test_local_probe import CONTEXT


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, value):
        self.now += value


@pytest.fixture
async def manager(hass, tmp_path):
    result = LocalBudgetManager(hass, clock=Clock())
    result._path = tmp_path / "local-budget.json"
    await result.initialize()
    yield result
    await result.close()


def context(*, connection="other", host="192.0.2.42"):
    return replace(
        CONTEXT,
        identity=replace(CONTEXT.identity, connection_id=connection),
        endpoint=LocalEndpoint(host),
    )


async def test_two_admitted_reads_and_quarter_second_spacing(manager):
    budget = await manager.budget(CONTEXT)
    first = budget.claim()
    assert first is not None and budget.claim() is None
    manager._clock.advance(0.25)
    second = budget.claim()
    assert second is not None and budget.busy
    manager._clock.advance(10)
    assert budget.claim() is None
    budget.release(first)
    budget.release(first)
    third = budget.claim()
    assert third is not None
    budget.release(second)
    budget.release(third)
    assert not budget.busy
    with pytest.raises(BudgetDeferred):
        budget.check_lease(first)


async def test_handles_share_target_endpoint_peer_and_verified_gateway(manager):
    first = await manager.budget(CONTEXT, target_id="plant")
    replacement = await manager.budget(context(host="192.0.2.42"), target_id="plant")
    await first.bind_peers(("192.0.2.43",))
    alias = await manager.budget(context(connection="alias", host="192.0.2.43"))
    await replacement.bind_gateway("gateway-one")
    recreated = await manager.budget(
        context(connection="fresh", host="192.0.2.44"), gateway_id="gateway-one"
    )
    await first.pause(7200, "rate_limited")
    for budget in (first, replacement, alias, recreated):
        assert budget.pause_seconds == 7200 and budget.claim() is None
    independent = await manager.budget(
        context(connection="independent", host="192.0.2.45")
    )
    lease = independent.claim()
    assert lease is not None
    independent.release(lease)
    raw = manager._path.read_text()
    assert "gateway-one" not in raw and CONTEXT.endpoint.host not in raw
    assert "7200" in raw
    with pytest.raises(ValueError, match="scope_mismatch"):
        first.check_scope(context())


async def test_dns_alias_merge_rechecks_admission_before_tls(manager):
    a = await manager.budget(CONTEXT)
    b = await manager.budget(context())
    first = a.claim()
    manager._clock.advance(0.25)
    second = a.claim()
    third = b.claim()
    assert first and second and third
    await a.bind_peers(("192.0.2.47",))
    await b.bind_peers(("192.0.2.47",))
    a.check_lease(first)
    a.check_lease(second)
    with pytest.raises(BudgetDeferred):
        b.check_lease(third)
    for budget, lease in ((a, first), (a, second), (b, third)):
        budget.release(lease)
    assert not a.busy and not b.busy


async def test_http_pacing_and_exclusive_setup_use_same_gate():
    budget = LocalReadBudget(CONTEXT)
    first = budget.claim()
    await budget.start_http(first, lambda: True)
    times = [asyncio.get_running_loop().time()]
    await asyncio.sleep(0.26)
    second = budget.claim()
    assert second is not None
    await budget.start_http(second, lambda: True)
    times.append(asyncio.get_running_loop().time())
    entered = asyncio.Event()

    async def setup():
        async with budget.slot(is_current=lambda: True) as lease:
            entered.set()
            await budget.start_http(lease, lambda: True)
            assert budget.claim() is None

    task = asyncio.create_task(setup())
    await asyncio.sleep(0)
    assert not entered.is_set()
    budget.release(first)
    budget.release(second)
    await task
    assert times[1] - times[0] >= 0.25
    assert not budget.busy


async def test_actual_headers_are_spaced_even_when_admission_was_long_ago():
    budget = LocalReadBudget(CONTEXT)
    first = budget.claim()
    await asyncio.sleep(0.26)
    second = budget.claim()
    await budget.start_http(first, lambda: True)
    began = asyncio.get_running_loop().time()
    await budget.start_http(second, lambda: True)
    assert asyncio.get_running_loop().time() - began >= MINIMUM_START_GAP - 0.01
    budget.release(first)
    budget.release(second)


async def test_setup_cancellation_and_invalid_generation_release_waiter(manager):
    budget = await manager.budget(CONTEXT)
    held = budget.claim()

    async def setup():
        async with budget.slot(is_current=lambda: True):
            pytest.fail("A setup step overtook a running read")

    task = asyncio.create_task(setup())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget._state.root().exclusive_waiters == 0
    budget.release(held)
    with pytest.raises(StaleLocalRequest):
        async with budget.slot(is_current=lambda: False):
            pass
    with pytest.raises(StaleLocalRequest):
        await budget.start_http(held, lambda: False)


async def test_pause_survives_restart_reconfiguration_and_wall_clock_jumps(
    hass, manager
):
    budget = await manager.budget(CONTEXT, target_id="plant")
    await budget.pause(7200, "rate_limited")
    manager._clock.advance(90)
    with patch("time.time", return_value=10**12):
        assert budget.pause_seconds == 7110
        await manager.persist()
    fresh = LocalBudgetManager(hass, clock=Clock())
    fresh._path = manager._path
    try:
        restored = await fresh.budget(context(), target_id="plant")
        assert restored.pause_seconds == 7110
        with patch("time.time", return_value=0):
            assert restored.claim() is None
        fresh._clock.advance(7110)
        lease = restored.claim()
        assert lease is not None
        restored.release(lease)
        await fresh.persist()
        assert module._load(fresh._path) == []
        assert fresh._checkpoint is None
    finally:
        await fresh.close()


async def test_cancelled_pause_save_finishes_before_admission_reopens(
    manager, monkeypatch
):
    budget = await manager.budget(CONTEXT)
    started, finish = threading.Event(), threading.Event()
    original = module._save

    def blocked(*args):
        started.set()
        assert finish.wait(5)
        original(*args)

    monkeypatch.setattr(module, "_save", blocked)
    task = asyncio.create_task(budget.pause(7200, "rate_limited"))
    await asyncio.to_thread(started.wait, 2)
    assert started.is_set()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(BudgetDeferred):
        budget.claim()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert module._load(manager._path)[0]["remaining"] == 7200
    assert budget.claim() is None


async def test_cancelled_save_waiting_behind_another_write_still_commits(
    manager, monkeypatch
):
    first = await manager.budget(CONTEXT)
    second = await manager.budget(context())
    started, finish = threading.Event(), threading.Event()
    original = module._save

    def blocked(*args):
        started.set()
        assert finish.wait(5)
        original(*args)

    monkeypatch.setattr(module, "_save", blocked)
    task1 = asyncio.create_task(first.pause(7200))
    await asyncio.to_thread(started.wait, 2)
    assert started.is_set()
    task2 = asyncio.create_task(second.pause(9000))
    await asyncio.sleep(0)
    task2.cancel()
    await asyncio.sleep(0)
    task2.cancel()
    with pytest.raises(BudgetDeferred):
        second.claim()
    finish.set()
    await task1
    with pytest.raises(asyncio.CancelledError):
        await task2
    assert sorted(row["remaining"] for row in module._load(manager._path)) == [
        7200,
        9000,
    ]
    assert first.pause_seconds == 7200 and second.pause_seconds == 9000


async def test_checkpoint_and_clean_stop_save_remaining_monotonic_duration(manager):
    budget = await manager.budget(CONTEXT)
    await budget.pause(7200)
    manager._clock.advance(60)
    manager._checkpoint._run()
    await manager._hass.async_block_till_done()
    assert module._load(manager._path)[0]["remaining"] == 7140
    manager._clock.advance(10)
    manager._hass.bus.async_fire(module.EVENT_HOMEASSISTANT_STOP)
    await manager._hass.async_block_till_done()
    assert module._load(manager._path)[0]["remaining"] == 7130
    assert manager._closed and manager._checkpoint is None
    with pytest.raises(LocalBudgetError):
        budget.claim()


async def test_storage_failure_keeps_pauses_in_memory_and_saves_later(
    manager, monkeypatch, caplog
):
    first = await manager.budget(CONTEXT)
    second = await manager.budget(context())
    original = module._save

    def fail(*args):
        raise OSError("synthetic private storage path")

    monkeypatch.setattr(module, "_save", fail)
    await first.pause(7200)
    await first.pause(7300)
    # The pause holds; other gateways are admitted as before.
    assert first.pause_seconds == 7300 and first.claim() is None
    lease = second.claim()
    assert lease is not None
    second.release(lease)
    warnings = [r for r in caplog.records if "could not be saved" in r.getMessage()]
    assert len(warnings) == 1 and "private" not in caplog.text
    assert manager._unsaved and manager._checkpoint is not None
    monkeypatch.setattr(module, "_save", original)
    manager._checkpoint._run()
    await manager._hass.async_block_till_done()
    assert module._load(manager._path)[0]["remaining"] == 7300
    assert not manager._unsaved


async def test_pauses_never_exceed_one_day(manager):
    budget = await manager.budget(CONTEXT)
    await budget.pause(10**9, "rate_limited")
    assert budget.pause_seconds == module.MAX_LOCAL_PAUSE_SECONDS == 86400
    standalone = LocalReadBudget(context(connection="standalone"), clock=Clock())
    standalone.defer(10**9)
    assert standalone.pause_seconds == 86400


async def test_stored_pause_longer_than_one_day_is_shortened(hass, tmp_path):
    path = tmp_path / "long-budget.json"
    alias = "a" * 64
    row = {"aliases": [alias], "remaining": 1e9, "reason": "rate_limited"}
    path.write_text(json.dumps({"version": 1, "pauses": [row]}))
    loaded = LocalBudgetManager(hass, clock=Clock())
    loaded._path = path
    try:
        await loaded.initialize()
        assert loaded._aliases[alias].pause_until - loaded._clock() == 86400
    finally:
        await loaded.close()


async def test_unreadable_store_is_read_again_on_the_next_start(hass, tmp_path):
    """A file that cannot be read at all (here a directory in its place) is
    a storage failure: tried again, never replaced."""
    path = tmp_path / "budget.json"
    path.mkdir()
    loaded = LocalBudgetManager(hass, clock=Clock())
    loaded._path = path
    try:
        with pytest.raises(LocalBudgetError):
            await loaded.budget(CONTEXT)
        assert path.is_dir() and not path.with_name("budget.json.corrupt").exists()
        path.rmdir()
        budget = await loaded.budget(CONTEXT)
        assert budget.claim() is not None
    finally:
        await loaded.close()


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan"), "1", None])
async def test_invalid_pause_is_rejected_without_mutation(manager, value):
    budget = await manager.budget(CONTEXT)
    with pytest.raises(LocalBudgetError):
        await budget.pause(value)
    assert budget.pause_seconds == 0


async def test_pause_extension_cannot_shorten_existing_wait(manager):
    budget = await manager.budget(CONTEXT)
    await budget.pause(7200)
    await budget.pause(60)
    await budget.pause(0)
    assert budget.pause_seconds == 7200
    with pytest.raises(LocalBudgetError):
        budget.defer(1)
    with pytest.raises(LocalBudgetError):
        await budget.pause(1, "private token")
    with pytest.raises(LocalBudgetError):
        await budget.bind_gateway("")
    with pytest.raises(BudgetDeferred):
        async with budget.slot(is_current=lambda: True):
            pass


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{",
        b"\xff\xfe",
        b"[]",
        b"{}",
        b'{"version":2,"pauses":[]}',
        b'{"version":1,"version":1,"pauses":[]}',
        b'{"version":true,"pauses":[]}',
        b'{"version":1,"pauses":{}}',
        b"[" * 100000 + b"]" * 100000,
    ],
)
async def test_unreadable_storage_is_set_aside_and_the_access_starts(
    hass, tmp_path, body, caplog
):
    """STORE-2: an unreadable file never keeps the local access closed: it
    is kept as the only backup and the access starts without saved pauses."""
    manager = LocalBudgetManager(hass, clock=Clock())
    manager._path = tmp_path / "invalid-budget.json"
    manager._path.write_bytes(body)
    try:
        budget = await manager.budget(CONTEXT)
        assert budget.claim() is not None and budget.pause_seconds == 0
        backup = tmp_path / "invalid-budget.json.corrupt"
        assert backup.read_bytes() == body and not manager._path.exists()
        assert "kept as a backup" in caplog.text
        # The next pause writes a new, readable file.
        await budget.pause(60)
        assert json.loads(manager._path.read_bytes())["pauses"][0]["remaining"] == 60
    finally:
        await manager.close()


async def test_a_later_format_of_the_same_version_keeps_its_usable_pauses(
    hass, tmp_path
):
    """STORE-2: a downgrade after a new reason or field, or a damaged row,
    keeps every pause that can still be used."""
    good, other, damaged = "a" * 64, "b" * 64, "c" * 64
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "pauses": [
                    # A reason and a field of a later release.
                    {
                        "aliases": [good],
                        "remaining": 600,
                        "reason": "brake",
                        "since": 1,
                    },
                    # A bad alias and a duplicate are left out, the rest kept.
                    {
                        "aliases": ["bad", good, other],
                        "remaining": 300.5,
                        "reason": "rate_limited",
                    },
                    {"aliases": [damaged], "remaining": -1, "reason": "failure"},
                    {"aliases": [damaged], "remaining": "1", "reason": "failure"},
                    {"aliases": "x", "remaining": 1, "reason": "failure"},
                    {"aliases": ["bad"], "remaining": 1, "reason": "failure"},
                    {"aliases": [damaged], "remaining": 9, "reason": ["x"]},
                    [],
                ],
                "written_by": "a later release",
            }
        )
    )
    loaded = LocalBudgetManager(hass, clock=Clock())
    loaded._path = path
    try:
        await loaded.initialize()
        now = loaded._clock()
        assert loaded._aliases[good].pause_until - now == 600
        assert loaded._aliases[good].reason == "failure"
        assert loaded._aliases[other].pause_until - now == 300.5
        assert loaded._aliases[other].reason == "rate_limited"
        assert loaded._aliases[damaged].pause_until - now == 9
        assert loaded._aliases[damaged].reason == "failure"
        assert not path.with_name("budget.json.corrupt").exists()
    finally:
        await loaded.close()


async def test_tolerant_reading_keeps_the_limits(monkeypatch):
    monkeypatch.setattr(module, "MAX_GROUPS", 2)
    monkeypatch.setattr(module, "MAX_ALIASES", 3)
    rows = [
        {"aliases": [f"{index:064x}", f"{index + 100:064x}"], "remaining": 1}
        for index in range(4)
    ]
    parsed = module._parse(json.dumps({"version": 1, "pauses": rows}).encode())
    assert [row["aliases"] for row in parsed] == [
        [f"{0:064x}", f"{100:064x}"],
        [f"{1:064x}"],
    ]
    assert all(row["reason"] == "failure" for row in parsed)
    with pytest.raises(LocalBudgetError):
        module._parse(b" " * (module.MAX_STORE_BYTES + 1))


async def test_a_pause_the_running_one_already_holds_writes_nothing(manager):
    """TLS-2: the same failure noted by the read and by the polling, or a
    zero Retry-After, does not write the file again."""
    budget = await manager.budget(CONTEXT)
    with patch.object(manager, "persist", wraps=manager.persist) as persist:
        await budget.pause(0)
        assert persist.call_count == 0 and budget.pause_seconds == 0
        await budget.pause(60)
        assert persist.call_count == 1
        manager._clock.advance(0.2)
        await budget.pause(60, "connection")  # 0.2 s longer
        await budget.pause(30)
        assert persist.call_count == 1
        assert budget.pause_seconds == pytest.approx(60)
        manager._clock.advance(10)
        await budget.pause(120)
        assert persist.call_count == 2


async def test_group_limit_rejects_new_scope_without_resetting_existing_pause(
    manager, monkeypatch
):
    first = await manager.budget(CONTEXT)
    await first.pause(60)
    monkeypatch.setattr(module, "MAX_GROUPS", 1)
    with pytest.raises(LocalBudgetError, match="capacity"):
        await manager.budget(context())
    assert first.pause_seconds == 60


async def test_manager_is_singleton_and_close_prevents_new_work(hass, tmp_path):
    manager = local_budget_manager(hass)
    manager._path = tmp_path / "singleton-budget.json"
    assert local_budget_manager(hass) is manager
    budget = await manager.budget(CONTEXT)
    await manager.close()
    with pytest.raises(LocalBudgetError):
        budget.claim()


async def test_setup_wait_reserves_time_for_active_network_work(manager, monkeypatch):
    budget = await manager.budget(CONTEXT)
    held = budget.claim()
    monkeypatch.setattr(module, "SETUP_WAIT_LIMIT", 0.03)
    with pytest.raises(TimeoutError):
        async with budget.slot(is_current=lambda: True, active_reserve=0.02):
            pytest.fail("Admission overtook a held read")
    assert budget._state.root().exclusive_waiters == 0
    budget.release(held)


async def test_waiter_and_alias_limits_preserve_existing_gateway(manager, monkeypatch):
    budget = await manager.budget(CONTEXT)
    budget._state.root().exclusive_waiters = 8
    with pytest.raises(BudgetDeferred):
        async with budget.slot(is_current=lambda: True):
            pass
    assert budget._state.root().exclusive_waiters == 8
    budget._state.root().exclusive_waiters = 0
    monkeypatch.setattr(module, "MAX_ALIASES", len(manager._aliases))
    with pytest.raises(LocalBudgetError, match="capacity"):
        await budget.bind_peers(("192.0.2.199",))
    lease = budget.claim()
    assert lease is not None
    budget.release(lease)


async def test_pauses_are_written_compact_and_pretty_ones_still_load(manager):
    budget = await manager.budget(CONTEXT)
    await budget.pause(7200, "rate_limited")
    body = manager._path.read_bytes()
    assert b"\n" not in body and body.startswith(b'{"version":1,"pauses":[')
    rows = module._load(manager._path)
    assert rows[0]["remaining"] == 7200
    manager._path.write_text(json.dumps({"version": 1, "pauses": rows}, indent=2))
    assert module._load(manager._path) == rows
    with pytest.raises(LocalBudgetError, match="invalid_local_read_pause"):
        module._save(manager._path, [{**rows[0], "remaining": float("inf")}])
    assert module._load(manager._path) == rows
