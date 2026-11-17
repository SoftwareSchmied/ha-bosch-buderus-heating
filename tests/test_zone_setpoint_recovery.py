"""A zone's setpoint follows its mode: reading it again after a mode change.

Synthetic readings in the shape of the zone controller of the issue: of the
two setpoints a zone has only the one of its current mode is answered. After
a change of the mode the other one must be found again, within a budget.
Nothing here confirms behaviour on a real device.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    RequestMetrics,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from custom_components.bosch_buderus_heating.zone_setpoint_recovery import (
    EPISODES_PER_TARGET,
    EPISODES_TOTAL,
    MIN_GAP_SECONDS,
    WINDOW_SECONDS,
    RecoveryBudget,
    is_zone_target,
    required_mode,
    targets_for_mode,
    zone_mode_path,
)
from tests.ha_test_helpers import MockConfigEntry

ROOT = "/zones/zone5/heating"
MODE = f"{ROOT}/operationMode"
MANUAL = f"{ROOT}/manualRoomSetpoint"
TEMPORARY = f"{ROOT}/temporaryRoomSetpoint"
SLEEP = "custom_components.bosch_buderus_heating.coordinator.asyncio.sleep"
CLOCK = "custom_components.bosch_buderus_heating.coordinator.monotonic"


def mode(value="auto", path=MODE):
    return Resource(path, value=value, has_value=True)


def setpoint(path):
    return Resource(
        path,
        value=21.0,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue",
            unit="C",
            writable=True,
            minimum=5.0,
            maximum=30.0,
        ),
    )


def success(resource):
    return BatchItemResult("gateway-one", resource.path, 200, resource=resource)


def missing(path):
    return BatchItemResult("gateway-one", path, 404)


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


def fresh(resource):
    return ResourceSnapshot(resource, True, datetime.now(UTC))


async def settle(coordinator, path):
    task = coordinator._manual_setpoint_tasks.get(path)
    if task is not None:
        await task


@pytest.fixture
async def zone(hass):
    """A zone that was discovered in manual: only its manual setpoint exists."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.metrics = RequestMetrics()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway-one"), entry
    )
    resources = {MODE: mode("manual"), MANUAL: setpoint(MANUAL)}
    coordinator.resources = dict(resources)
    coordinator.data = {path: fresh(item) for path, item in resources.items()}
    coordinator.last_update_success = True
    coordinator._paths_by_group = {PollGroup.CONTROL: (MODE, MANUAL)}
    coordinator._next_update = {
        group: monotonic() + 3600 for group in coordinator._poll_intervals
    }
    client.get_resources_bulk.return_value = (missing(TEMPORARY),)
    yield coordinator, client
    tasks = list(coordinator._manual_setpoint_tasks.values())
    coordinator.cancel_manual_setpoint_recovery()
    await asyncio.gather(*tasks, return_exceptions=True)


# The pure rules ---------------------------------------------------------


def test_paths_and_modes_belong_together():
    assert zone_mode_path(MODE) == MODE
    assert zone_mode_path(MANUAL) == MODE
    assert zone_mode_path(TEMPORARY) == MODE
    assert zone_mode_path("/zones/zone5/cooling/manualRoomSetpoint") == (
        "/zones/zone5/cooling/operationMode"
    )
    for other in (
        "/zones/zone5/heating/currentRoomSetpoint",
        "/zones/zone5/name",
        "/heatingCircuits/hc1/operationMode",
        "/zones/zone5/heat/operationMode",
    ):
        assert zone_mode_path(other) is None
    assert is_zone_target(TEMPORARY) and not is_zone_target(MODE)
    assert required_mode(TEMPORARY) == "auto"
    assert required_mode(MANUAL) == "manual"
    assert required_mode("/heatingCircuits/hc1/manualRoomSetpoint") == "manual"
    assert targets_for_mode(MODE, "auto") == (TEMPORARY, MANUAL)
    assert targets_for_mode(MODE, "manual") == (MANUAL, TEMPORARY)
    for value in ("off", "", None, 3, ["auto"]):
        assert targets_for_mode(MODE, value) == (None, None)
    assert targets_for_mode(MANUAL, "auto") == (None, None)


def test_budget_bounds_series_per_target_and_in_all():
    budget = RecoveryBudget()
    now = 1000.0
    assert budget.allow(TEMPORARY, now)
    assert not budget.allow(TEMPORARY, now + MIN_GAP_SECONDS - 1)  # too soon
    assert budget.allow(TEMPORARY, now + MIN_GAP_SECONDS)
    assert budget.allow(TEMPORARY, now + 2 * MIN_GAP_SECONDS)
    assert EPISODES_PER_TARGET == 3
    assert not budget.allow(TEMPORARY, now + 3 * MIN_GAP_SECONDS)
    assert budget.limited == 1
    # The hour passes: the first series left the window.
    assert budget.allow(TEMPORARY, now + WINDOW_SECONDS)
    assert budget.diagnostics() == {
        "series_started": 4,
        "setpoints_found": 0,
        "series_refused_by_budget": 1,
    }
    many = RecoveryBudget()
    paths = [f"/zones/zone{n}/heating/temporaryRoomSetpoint" for n in range(40)]
    granted = sum(many.allow(path, 5.0) for path in paths)
    assert granted == EPISODES_TOTAL
    assert many.limited == len(paths) - EPISODES_TOTAL


# The data flow ----------------------------------------------------------


async def test_mode_change_finds_the_setpoint_of_the_new_mode_once(zone):
    coordinator, client = zone
    # The 404 of discovery had paused the path for a day.
    coordinator._negative_until[TEMPORARY] = monotonic() + 86400
    client.get_resources_bulk.return_value = (success(setpoint(TEMPORARY)),)
    with patch(SLEEP, new_callable=AsyncMock):
        apply(coordinator, success(mode("auto")))
        assert TEMPORARY in coordinator._manual_setpoint_candidates
        await settle(coordinator, TEMPORARY)
    client.get_resources_bulk.assert_awaited_once_with(
        "gateway-one", (TEMPORARY,), retryable=False
    )
    assert coordinator.data[TEMPORARY].available
    assert TEMPORARY in coordinator.resources
    assert TEMPORARY not in coordinator._negative_until
    assert coordinator._paths_by_group[PollGroup.CONTROL].count(TEMPORARY) == 1
    assert coordinator.zone_recovery.diagnostics()["setpoints_found"] == 1
    # The next poll with the target fresh ends the episode; no further read.
    apply(coordinator, success(mode("auto")), success(setpoint(TEMPORARY)))
    assert TEMPORARY not in coordinator._manual_setpoint_attempted
    assert client.get_resources_bulk.await_count == 1


async def test_a_change_made_by_this_integration_does_the_same(zone):
    coordinator, client = zone
    client.get_resources_bulk.return_value = (success(setpoint(TEMPORARY)),)
    snapshots = dict(coordinator.data)
    snapshots[MODE] = fresh(mode("auto"))
    with patch(SLEEP, new_callable=AsyncMock):
        coordinator._queue_manual_setpoint_recovery(MODE, snapshots)
        coordinator.data = snapshots
        await settle(coordinator, TEMPORARY)
    assert coordinator.data[TEMPORARY].available


async def test_the_cooling_side_works_alike(zone):
    coordinator, client = zone
    cool_mode = "/zones/zone5/cooling/operationMode"
    cool_target = "/zones/zone5/cooling/temporaryRoomSetpoint"
    client.get_resources_bulk.return_value = (success(setpoint(cool_target)),)
    with patch(SLEEP, new_callable=AsyncMock):
        apply(coordinator, success(mode("auto", cool_mode)))
        await settle(coordinator, cool_target)
    assert coordinator.data[cool_target].available


async def test_no_setpoint_is_looked_for_in_off_or_without_a_fresh_mode(zone):
    coordinator, client = zone
    with patch(SLEEP, new_callable=AsyncMock):
        apply(coordinator, success(mode("off")))
        assert not coordinator._manual_setpoint_tasks
        stale = coordinator.data[MODE]
        coordinator.data[MODE] = ResourceSnapshot(
            mode("auto"), False, stale.last_success, freshness=Freshness.STALE
        )
        coordinator._queue_manual_setpoint_recovery(MODE, coordinator.data)
        assert not coordinator._manual_setpoint_tasks
    client.get_resources_bulk.assert_not_awaited()
    assert coordinator.zone_recovery.episodes == 0


async def test_a_refusing_controller_is_asked_in_one_bounded_series(zone):
    coordinator, client = zone
    with patch(SLEEP, new_callable=AsyncMock):
        apply(coordinator, success(mode("auto")))
        await settle(coordinator, TEMPORARY)
        assert client.get_resources_bulk.await_count == 3
        # More polls in the same minutes start nothing new (minimum gap).
        for _ in range(5):
            apply(coordinator, success(mode("auto")))
            assert not coordinator._manual_setpoint_tasks
    assert client.get_resources_bulk.await_count == 3
    # Not found: it is not polled and not a candidate until a later series.
    assert TEMPORARY not in coordinator._manual_setpoint_candidates
    assert TEMPORARY not in coordinator._paths_by_group[PollGroup.CONTROL]


async def test_probes_per_hour_stay_within_the_budget(zone):
    coordinator, client = zone
    now = 10_000.0
    with patch(SLEEP, new_callable=AsyncMock), patch(CLOCK) as clock:
        # One poll every five minutes for two hours.
        for step in range(24):
            clock.return_value = now + step * 300.0
            apply(coordinator, success(mode("auto")))
            await settle(coordinator, TEMPORARY)
    series = client.get_resources_bulk.await_count / 3
    assert series == coordinator.zone_recovery.episodes
    # At most three series per hour for the one target, three reads each.
    assert series <= 2 * EPISODES_PER_TARGET + 1
    assert coordinator.zone_recovery.limited >= 0


async def test_the_other_modes_search_ends_with_a_mode_change(zone):
    coordinator, _client = zone
    entered = asyncio.Event()
    release = asyncio.Event()

    async def sleep(_delay):
        entered.set()
        await release.wait()

    with patch(SLEEP, side_effect=sleep):
        apply(coordinator, success(mode("auto")))
        old = coordinator._manual_setpoint_tasks[TEMPORARY]
        await entered.wait()
        # Back to manual: the temporary setpoint is no longer wanted.
        apply(coordinator, success(mode("manual")))
        await asyncio.gather(old, return_exceptions=True)
        assert old.cancelled()
        assert TEMPORARY not in coordinator._manual_setpoint_candidates
        assert TEMPORARY not in coordinator._manual_setpoint_attempted
        release.set()


async def test_a_discovered_setpoint_that_disappears_is_not_searched_twice(zone):
    coordinator, client = zone
    # In manual with the manual setpoint known: a poll sees it fresh, nothing
    # is read for it.
    with patch(SLEEP, new_callable=AsyncMock):
        apply(coordinator, success(mode("manual")), success(setpoint(MANUAL)))
    assert not coordinator._manual_setpoint_tasks
    client.get_resources_bulk.assert_not_awaited()


async def test_a_spent_discovery_budget_starts_no_series(zone):
    coordinator, client = zone
    coordinator.startup_data.begin_discovery(maximum_resources=1)
    coordinator.startup_data.reserve_discovery((MODE,))
    with patch(SLEEP, new_callable=AsyncMock):
        apply(coordinator, success(mode("auto")))
    assert not coordinator._manual_setpoint_tasks
    assert not coordinator._manual_setpoint_candidates
    client.get_resources_bulk.assert_not_awaited()


async def test_heating_circuits_keep_their_own_rules(zone):
    coordinator, client = zone
    circuit_mode = "/heatingCircuits/hc2/operationMode"
    circuit_target = "/heatingCircuits/hc2/manualRoomSetpoint"
    coordinator.resources[circuit_mode] = mode("manual", circuit_mode)
    coordinator.data[circuit_mode] = fresh(mode("manual", circuit_mode))
    client.get_resources_bulk.return_value = (success(setpoint(circuit_target)),)
    with patch(SLEEP, new_callable=AsyncMock):
        coordinator._queue_manual_setpoint_recovery(circuit_mode, coordinator.data)
        await settle(coordinator, circuit_target)
    assert coordinator.data[circuit_target].available
    assert coordinator.zone_recovery.episodes == 0  # not a zone series


async def test_a_zone_target_is_not_polled_before_its_series_found_it(zone):
    coordinator, client = zone
    pending = SimpleNamespace(path=TEMPORARY)
    coordinator._manual_setpoint_candidates[pending.path] = MODE
    # Auto mode, the candidate is not a resource yet: the regular cycle must
    # not add it to its reads (the series reads it, within its budget).
    coordinator.data[MODE] = fresh(mode("auto"))
    client.get_resources_bulk.side_effect = None
    client.get_resources_bulk.return_value = (success(mode("auto")),)
    coordinator._next_update[PollGroup.CONTROL] = 0
    coordinator.data = await coordinator._async_update_data_locked()
    for call_args in client.get_resources_bulk.await_args_list:
        assert TEMPORARY not in call_args.args[1]
