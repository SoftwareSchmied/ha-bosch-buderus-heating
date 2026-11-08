"""Bounded manual-setpoint recovery, regular polling and entity continuity."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from custom_components.bosch_buderus_heating import async_unload_entry
from custom_components.bosch_buderus_heating.const import DOMAIN, PollingProfile
from custom_components.bosch_buderus_heating.coordinator import (
    MANUAL_SETPOINT_RETRY_DELAYS,
    POLL_INTERVALS_CLOUD_FRIENDLY,
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.number import (
    async_setup_entry as setup_numbers,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    BatchItemResult,
    Gateway,
    RateLimited,
    RequestMetrics,
    Resource,
    ResourceMetadata,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    RequestTimeout,
    TransportError,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
)
from custom_components.bosch_buderus_heating.sensor import (
    async_setup_entry as setup_sensors,
)
from tests.ha_test_helpers import MockConfigEntry

MODE = "/heatingCircuits/hc2/operationMode"
TARGET = "/heatingCircuits/hc2/manualRoomSetpoint"
SLEEP = "custom_components.bosch_buderus_heating.coordinator.asyncio.sleep"


def mode(value="manual", path=MODE):
    return Resource(path, value=value, has_value=True)


def setpoint(path=TARGET, *, writable=True):
    return Resource(
        path,
        value=21.0,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue",
            unit="C",
            writable=writable,
            minimum=5.0,
            maximum=30.0,
        ),
    )


def success(resource, gateway="gateway-one"):
    return BatchItemResult(gateway, resource.path, 200, resource=resource)


def apply(coordinator, *results):
    snapshots = dict(coordinator.data)
    coordinator._apply_results(
        snapshots,
        results,
        attempted_at=datetime.now(UTC),
        now_monotonic=monotonic(),
        source=SnapshotSource.BATCH,
    )
    coordinator.data = snapshots


def add_known_target(coordinator, *, available=False):
    resource = setpoint()
    coordinator.resources[TARGET] = resource
    coordinator.data[TARGET] = ResourceSnapshot(
        resource,
        available,
        datetime.now(UTC),
        freshness=Freshness.FRESH if available else Freshness.STALE,
    )
    coordinator._paths_by_group[PollGroup.CONTROL] += (TARGET,)


async def run_series(coordinator, mode_path=MODE):
    coordinator._queue_manual_setpoint_recovery(mode_path, coordinator.data)
    path = mode_path.removesuffix("/operationMode") + "/manualRoomSetpoint"
    await coordinator._manual_setpoint_tasks[path]


@pytest.fixture
async def recovery(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.metrics = RequestMetrics()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway-one"), entry
    )
    resource = mode()
    coordinator.resources = {MODE: resource}
    coordinator.data = {MODE: ResourceSnapshot(resource, True, datetime.now(UTC))}
    coordinator.last_update_success = True
    coordinator._paths_by_group = {PollGroup.CONTROL: (MODE,)}
    coordinator._next_update = {
        group: monotonic() + 3600 for group in coordinator._poll_intervals
    }
    client.get_resources_bulk.return_value = (
        BatchItemResult("gateway-one", TARGET, 404),
    )
    callbacks = []
    platform_entry = SimpleNamespace(
        entry_id=entry.entry_id,
        runtime_data=SimpleNamespace(
            bacon=None, coordinators=(coordinator,), client=client
        ),
        async_on_unload=callbacks.append,
    )
    yield coordinator, client, entry, platform_entry
    tasks = list(coordinator._manual_setpoint_tasks.values())
    coordinator.cancel_manual_setpoint_recovery()
    await asyncio.gather(*tasks, return_exceptions=True)
    for callback in callbacks:
        callback()


@pytest.mark.parametrize("known", [False, True])
async def test_one_series_per_outage_despite_repeated_failure_polls(recovery, known):
    coordinator, client, _, _ = recovery
    if known:
        add_known_target(coordinator)
    with patch(SLEEP, new_callable=AsyncMock) as sleep:
        await run_series(coordinator)
        assert sleep.await_args_list == [call(3.0), call(10.0), call(20.0)]
        assert MANUAL_SETPOINT_RETRY_DELAYS == (3.0, 10.0, 20.0)
        for _ in range(4):
            apply(
                coordinator,
                success(mode()),
                BatchItemResult("gateway-one", TARGET, 404),
            )
            assert not coordinator._manual_setpoint_tasks
    assert client.get_resources_bulk.await_count == 3
    assert TARGET in coordinator._manual_setpoint_attempted
    assert all(
        request == call("gateway-one", (TARGET,), retryable=False)
        for request in client.get_resources_bulk.await_args_list
    )


@pytest.mark.parametrize("cloud_friendly", [False, True])
async def test_missing_startup_target_recovers_at_normal_control_cadence(
    recovery, cloud_friendly
):
    coordinator, client, _, _ = recovery
    if cloud_friendly:
        coordinator.polling_profile = PollingProfile.CLOUD_FRIENDLY
        coordinator._poll_intervals = POLL_INTERVALS_CLOUD_FRIENDLY
    with patch(SLEEP, new_callable=AsyncMock):
        await run_series(coordinator)
    interval = coordinator._poll_intervals[PollGroup.CONTROL].total_seconds()
    deadline = monotonic() + interval
    coordinator._next_update[PollGroup.CONTROL] = deadline

    async def read(gateway, paths):
        return tuple(success(mode() if path == MODE else setpoint()) for path in paths)

    client.get_resources_bulk.side_effect = read
    await coordinator._async_update_data_locked()
    assert client.get_resources_bulk.await_count == 3
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.monotonic",
        return_value=deadline,
    ):
        coordinator.data = await coordinator._async_update_data_locked()
    assert client.get_resources_bulk.await_count == 4
    assert client.get_resources_bulk.await_args == call("gateway-one", (MODE, TARGET))
    assert coordinator._next_update[PollGroup.CONTROL] == deadline + interval
    assert coordinator.data[TARGET].available
    assert TARGET in coordinator._paths_by_group[PollGroup.CONTROL]
    assert TARGET not in coordinator._manual_setpoint_candidates
    assert TARGET not in coordinator._manual_setpoint_attempted

    coordinator._next_update[PollGroup.CONTROL] = 0
    coordinator.data = await coordinator._async_update_data_locked()
    assert client.get_resources_bulk.await_args == call("gateway-one", (MODE, TARGET))
    assert coordinator._paths_by_group[PollGroup.CONTROL].count(TARGET) == 1


async def test_regular_failure_reads_do_not_restart_exhausted_series(recovery):
    coordinator, client, _, _ = recovery
    with patch(SLEEP, new_callable=AsyncMock):
        await run_series(coordinator)
        for _ in range(3):
            coordinator._next_update[PollGroup.CONTROL] = 0
            coordinator.data = await coordinator._async_update_data_locked()
            assert not coordinator._manual_setpoint_tasks
    assert client.get_resources_bulk.await_count == 6
    assert (
        client.get_resources_bulk.await_args_list[3:]
        == [
            call("gateway-one", (MODE, TARGET)),
        ]
        * 3
    )


async def test_success_ends_episode_and_later_outage_gets_one_new_series(recovery):
    coordinator, client, _, _ = recovery
    with patch(SLEEP, new_callable=AsyncMock):
        await run_series(coordinator)
        apply(coordinator, success(setpoint()))
        assert TARGET not in coordinator._manual_setpoint_attempted
        apply(coordinator, BatchItemResult("gateway-one", TARGET, 404))
        await coordinator._manual_setpoint_tasks[TARGET]
        apply(coordinator, BatchItemResult("gateway-one", TARGET, 404))
    assert client.get_resources_bulk.await_count == 6
    assert not coordinator._manual_setpoint_tasks


@pytest.mark.parametrize("known,expected_reads", [(False, 1), (True, 2)])
async def test_forbidden_pause_is_retained(recovery, known, expected_reads):
    coordinator, client, _, _ = recovery
    if known:
        add_known_target(coordinator)
    client.get_resources_bulk.return_value = (
        BatchItemResult("gateway-one", TARGET, 403),
    )
    with patch(SLEEP, new_callable=AsyncMock):
        await run_series(coordinator)
    assert client.get_resources_bulk.await_count == expected_reads
    assert coordinator._negative_until[TARGET] > monotonic()
    coordinator._next_update[PollGroup.CONTROL] = 0
    await coordinator._async_update_data_locked()
    assert TARGET not in client.get_resources_bulk.await_args.args[1]


@pytest.mark.parametrize("pause", ["cloud", "circuit", "resource"])
async def test_existing_pauses_prevent_targeted_reads(recovery, pause):
    coordinator, client, _, _ = recovery
    until = monotonic() + 600
    if pause == "cloud":
        coordinator._cloud_backoff_until = until
    elif pause == "circuit":
        coordinator._circuit_open_until = until
    else:
        coordinator._negative_until[TARGET] = until
    with patch(SLEEP, new_callable=AsyncMock):
        await run_series(coordinator)
    client.get_resources_bulk.assert_not_awaited()


@pytest.mark.parametrize("status", [401, 429, 500, 503, 504, None])
async def test_batch_item_errors_end_the_series(recovery, status):
    coordinator, client, entry, _ = recovery
    client.get_resources_bulk.return_value = (
        BatchItemResult("gateway-one", TARGET, status),
    )
    with patch.object(entry, "async_start_reauth") as reauth:
        with patch(SLEEP, new_callable=AsyncMock):
            await run_series(coordinator)
        assert reauth.call_count == (1 if status == 401 else 0)
    assert client.get_resources_bulk.await_count == 1
    assert bool(coordinator._cloud_backoff_until) == (status == 429)


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError(),
        RateLimited(retry_after=600),
        ServiceUnavailable(),
        RequestTimeout(),
        TransportError(),
    ],
)
async def test_transport_errors_end_the_series_without_immediate_retries(
    recovery, error
):
    coordinator, client, entry, _ = recovery
    client.get_resources_bulk.side_effect = error
    with patch.object(entry, "async_start_reauth") as reauth:
        with patch(SLEEP, new_callable=AsyncMock):
            await run_series(coordinator)
        assert reauth.call_count == (1 if isinstance(error, AuthenticationError) else 0)
    client.get_resources_bulk.assert_awaited_once_with(
        "gateway-one", (TARGET,), retryable=False
    )
    assert bool(coordinator._cloud_backoff_until) == isinstance(error, RateLimited)


async def test_rate_limit_in_batch_preserves_retry_after(recovery):
    coordinator, client, _, _ = recovery
    client.get_resources_bulk.return_value = (
        BatchItemResult("gateway-one", TARGET, 429, error=RateLimited(retry_after=600)),
    )
    with patch(SLEEP, new_callable=AsyncMock):
        await run_series(coordinator)
    assert coordinator._cloud_backoff_until - monotonic() > 590
    assert client.get_resources_bulk.await_count == 1


@pytest.mark.parametrize(
    "value,available,fresh",
    [
        ("auto", True, True),
        ("manual", False, True),
        ("manual", True, False),
    ],
)
async def test_unconfirmed_or_non_manual_modes_do_not_start(
    recovery, value, available, fresh
):
    coordinator, client, _, _ = recovery
    coordinator.data[MODE] = ResourceSnapshot(
        mode(value),
        available,
        datetime.now(UTC),
        freshness=Freshness.FRESH if fresh else Freshness.STALE,
    )
    coordinator._queue_manual_setpoint_recovery(MODE, coordinator.data)
    assert not coordinator._manual_setpoint_tasks
    client.get_resources_bulk.assert_not_awaited()


@pytest.mark.parametrize(
    "path",
    [
        "/heatingCircuits/hc2/cooling/operationMode",
        "/dhwCircuits/dhw1/operationMode",
        "/heatingCircuits/hc3/operationMode",
    ],
)
async def test_only_reported_direct_heating_circuit_modes_are_eligible(recovery, path):
    coordinator, client, _, _ = recovery
    if "hc3" not in path:
        coordinator.data[path] = ResourceSnapshot(
            mode(path=path), True, datetime.now(UTC)
        )
    coordinator._queue_manual_setpoint_recovery(path, coordinator.data)
    assert not coordinator._manual_setpoint_tasks
    client.get_resources_bulk.assert_not_awaited()


async def test_distinct_path_budget_also_bounds_missing_target_recovery(recovery):
    coordinator, client, _, _ = recovery
    coordinator.startup_data.begin_discovery(maximum_resources=1)
    coordinator.startup_data.reserve_discovery((MODE,))
    coordinator._queue_manual_setpoint_recovery(MODE, coordinator.data)
    assert not coordinator._manual_setpoint_candidates
    assert not coordinator._manual_setpoint_tasks
    client.get_resources_bulk.assert_not_awaited()


async def test_mode_change_cancels_old_task_without_removing_its_successor(recovery):
    coordinator, client, _, _ = recovery
    entered = asyncio.Event()
    release = asyncio.Event()

    async def sleep(_delay):
        entered.set()
        await release.wait()

    with patch(SLEEP, side_effect=sleep):
        coordinator._queue_manual_setpoint_recovery(MODE, coordinator.data)
        old = coordinator._manual_setpoint_tasks[TARGET]
        await entered.wait()
        apply(coordinator, success(mode("auto")))
        assert TARGET not in coordinator._manual_setpoint_candidates
        apply(coordinator, success(mode()))
        new = coordinator._manual_setpoint_tasks[TARGET]
        await asyncio.gather(old, return_exceptions=True)
        assert old.cancelled()
        assert coordinator._manual_setpoint_tasks[TARGET] is new
        release.set()
        await new
    assert client.get_resources_bulk.await_count == 3


@pytest.mark.parametrize("unloaded", [True, False])
async def test_integration_unload_cancels_pending_recovery_only_on_success(
    recovery, hass, unloaded
):
    coordinator, client, entry, _ = recovery
    entered = asyncio.Event()
    release = asyncio.Event()

    async def sleep(_delay):
        entered.set()
        await release.wait()

    entry.runtime_data = SimpleNamespace(
        startup=None, coordinators=(coordinator,), notifications=(), bacon=None
    )
    with patch(SLEEP, side_effect=sleep):
        coordinator._queue_manual_setpoint_recovery(MODE, coordinator.data)
        task = coordinator._manual_setpoint_tasks[TARGET]
        await entered.wait()
        with patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new=AsyncMock(return_value=unloaded),
        ):
            assert await async_unload_entry(hass, entry) is unloaded
        if unloaded:
            await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled()
            coordinator._queue_manual_setpoint_recovery(MODE, coordinator.data)
            assert not coordinator._manual_setpoint_tasks
            client.get_resources_bulk.assert_not_awaited()
        else:
            release.set()
            await task
            assert client.get_resources_bulk.await_count == 3


async def test_two_circuits_recover_independently(recovery):
    coordinator, client, _, _ = recovery
    mode1 = "/heatingCircuits/hc1/operationMode"
    target1 = "/heatingCircuits/hc1/manualRoomSetpoint"
    coordinator.data[mode1] = ResourceSnapshot(
        mode(path=mode1), True, datetime.now(UTC)
    )

    async def read(gateway, paths, **kwargs):
        path = paths[0]
        return (
            success(setpoint(path))
            if path == target1
            else BatchItemResult(gateway, path, 404),
        )

    client.get_resources_bulk.side_effect = read
    with patch(SLEEP, new_callable=AsyncMock):
        coordinator._queue_manual_setpoint_recovery(mode1, coordinator.data)
        coordinator._queue_manual_setpoint_recovery(MODE, coordinator.data)
        await asyncio.gather(*coordinator._manual_setpoint_tasks.values())
    requested = [args.args[1][0] for args in client.get_resources_bulk.await_args_list]
    assert requested.count(target1) == 1
    assert requested.count(TARGET) == 3
    assert coordinator.data[target1].available
    assert TARGET not in coordinator.data


@pytest.mark.parametrize("writable", [False, True])
async def test_late_discovery_adds_entities_once_and_keeps_write_metadata(
    recovery, hass, writable
):
    coordinator, client, _, entry = recovery
    numbers = []
    sensors = []
    await setup_numbers(hass, entry, numbers.extend)
    await setup_sensors(hass, entry, sensors.extend)
    assert not numbers
    assert not coordinator.startup_data.tracks(TARGET)
    client.get_resources_bulk.return_value = (success(setpoint(writable=writable)),)
    with patch(SLEEP, new_callable=AsyncMock):
        await run_series(coordinator)
    assert len(numbers) == int(writable)
    target_sensors = [
        entity
        for entity in sensors
        if isinstance(entity, BoschBuderusSensor)
        and entity.entity_description.resource_path == TARGET
    ]
    assert len(target_sensors) == 1
    original_sensor = target_sensors[0]
    original_number = numbers[0] if writable else None
    if writable:
        assert original_number.available
        assert original_number.native_min_value == 5.0
        assert original_number.native_max_value == 30.0
    with patch(SLEEP, new_callable=AsyncMock):
        apply(coordinator, BatchItemResult("gateway-one", TARGET, 404))
        task = coordinator._manual_setpoint_tasks[TARGET]
        coordinator.async_update_listeners()
        assert not original_sensor.available
        await task
    assert len(numbers) == int(writable)
    assert original_sensor.available
    if writable:
        assert numbers[0] is original_number
        assert original_number.available
    assert (
        sum(
            isinstance(entity, BoschBuderusSensor)
            and entity.entity_description.resource_path == TARGET
            for entity in sensors
        )
        == 1
    )


async def test_supplementary_success_does_not_clear_failed_main_cycle(recovery):
    coordinator, client, _, _ = recovery
    coordinator.last_update_success = False
    coordinator._main_cycle_failed = True
    before = dict(coordinator._next_update)
    client.get_resources_bulk.return_value = (success(setpoint()),)
    with patch(SLEEP, new_callable=AsyncMock):
        await run_series(coordinator)
    assert coordinator.data[TARGET].available
    assert not coordinator.last_update_success
    assert coordinator._main_cycle_failed
    assert coordinator._next_update == before


async def test_missing_candidate_404_during_auto_does_not_block_next_manual_episode(
    recovery,
):
    coordinator, client, _, _ = recovery
    with patch(SLEEP, new_callable=AsyncMock):
        await run_series(coordinator)
        apply(
            coordinator,
            success(mode("auto")),
            BatchItemResult("gateway-one", TARGET, 404),
        )
        assert TARGET not in coordinator._manual_setpoint_candidates
        coordinator._next_update[PollGroup.CONTROL] = 0
        client.get_resources_bulk.return_value = (success(mode("auto")),)
        coordinator.data = await coordinator._async_update_data_locked()
        assert client.get_resources_bulk.await_args == call("gateway-one", (MODE,))
        client.get_resources_bulk.return_value = (success(setpoint()),)
        apply(coordinator, success(mode()))
        await coordinator._manual_setpoint_tasks[TARGET]
    assert coordinator.data[TARGET].available
    assert client.get_resources_bulk.await_count == 5


async def test_startup_in_manual_queues_the_missing_setpoint(recovery):
    coordinator, client, _, _ = recovery
    coordinator.resources = {}
    coordinator.data = {}
    client.get_resources_bulk.return_value = (success(setpoint()),)
    with (
        patch(
            "custom_components.bosch_buderus_heating.coordinator.async_discover_resources",
            new=AsyncMock(return_value={MODE: mode()}),
        ),
        patch(
            "custom_components.bosch_buderus_heating.coordinator.fault_resource_candidates",
            return_value=(),
        ),
        patch(SLEEP, new_callable=AsyncMock),
    ):
        coordinator.data = await coordinator._async_discover_data()
        await coordinator._manual_setpoint_tasks[TARGET]
    assert coordinator.data[TARGET].available
    client.get_resources_bulk.assert_awaited_once_with(
        "gateway-one", (TARGET,), retryable=False
    )


async def test_same_circuit_on_two_gateways_has_independent_attempt_budgets(
    recovery, hass
):
    first, first_client, entry, _ = recovery
    second_client = AsyncMock()
    second_client.get_resources_bulk.return_value = (
        success(setpoint(), gateway="gateway-two"),
    )
    second = BoschBuderusDataUpdateCoordinator(
        hass, second_client, Gateway("gateway-two"), entry
    )
    second.data = dict(first.data)
    second.resources = dict(first.resources)
    try:
        with patch(SLEEP, new_callable=AsyncMock):
            await run_series(first)
            await run_series(second)
        assert first_client.get_resources_bulk.await_count == 3
        second_client.get_resources_bulk.assert_awaited_once_with(
            "gateway-two", (TARGET,), retryable=False
        )
        assert TARGET in first._manual_setpoint_attempted
        assert TARGET not in second._manual_setpoint_attempted
        assert TARGET not in first.data
        assert second.data[TARGET].available
    finally:
        second.cancel_manual_setpoint_recovery()
