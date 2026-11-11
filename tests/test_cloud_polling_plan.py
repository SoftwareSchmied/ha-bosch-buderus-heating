"""O41 and O45: cloud reads packed into the regular batches, and the slower
cloud rate of readings shown from the local access.

The simulations use the anonymized cloud inventory of my own heat pump
(K40RF gateway, MX400 controller) with a simulated clock: one wake-up per
minute, each cycle half a second long.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating.const import DOMAIN, PollingProfile
from custom_components.bosch_buderus_heating.coordinator import (
    LOCAL_SERVED_INTERVAL,
    POLL_INTERVALS,
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.discovery_inventory import (
    resource_from_data,
)
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    RequestMetrics,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import RequestTimeout
from custom_components.bosch_buderus_heating.poll_slots import (
    batches_per_period,
    plan_poll_slots,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from tests.ha_test_helpers import MockConfigEntry
from tests.test_local_api import read
from tests.test_local_selection import TARGET, FakeRuntime

COORDINATOR = "custom_components.bosch_buderus_heating.coordinator"
CAPTURE = (
    Path(__file__).parent
    / "fixtures"
    / "contracts"
    / "captures"
    / "k40-cloud-2026-11-05.json"
)
START = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)
TICK = 60.0
CYCLE = 0.5
SUPPLY = "/heatSources/actualSupplyTemperature"
DHW_TEMP = "/dhwCircuits/dhw1/actualTemp"
ROOM_SETPOINT = "/heatingCircuits/hc1/currentRoomSetpoint"
# Read by a control or a calculated entity: always at the normal rate.
KEPT = (
    "/dhwCircuits/dhw1/currentSetpoint",
    "/heatingCircuits/hc1/currentSuWiMode",
    "/heatSources/systemPressure",
)


# ------------------------------------------------------------------ helpers


class Clock:
    """Monotonic and calendar time that move together."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def utc(self) -> datetime:
        return START + timedelta(seconds=self.now - 1000.0)


def _patched(clock: Clock):
    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.utc()

    return (
        patch(f"{COORDINATOR}.monotonic", clock),
        patch(f"{COORDINATOR}.datetime", FakeDatetime),
        patch(
            f"{COORDINATOR}.dt_util",
            SimpleNamespace(
                utcnow=clock.utc, get_default_time_zone=dt_util.get_default_time_zone
            ),
        ),
    )


def _capture() -> dict[str, Resource]:
    capture = json.loads(CAPTURE.read_text(encoding="utf-8"))
    return {
        path: resource_from_data(value) for path, value in capture["resources"].items()
    }


class Cloud:
    """Answers every bulk read with the stored resource; records the reads."""

    def __init__(self, resources: dict[str, Resource], clock: Clock) -> None:
        self.resources = resources
        self.clock = clock
        self.batches: list[tuple[float, tuple[str, ...]]] = []

    async def bulk(self, gateway: str, paths) -> tuple[BatchItemResult, ...]:
        self.batches.append((self.clock.now, tuple(paths)))
        return tuple(
            BatchItemResult(gateway, path, 200, resource=self.resources[path])
            if path in self.resources
            else BatchItemResult(gateway, path, 404)
            for path in paths
        )

    def reads(self, start: float, end: float) -> dict[str, list[float]]:
        found: dict[str, list[float]] = defaultdict(list)
        for at, paths in self.batches:
            if start <= at < end:
                for path in paths:
                    found[path].append(at)
        return found

    def batch_count(self, start: float, end: float) -> int:
        return sum(start <= at < end for at, _ in self.batches)


def _plant(
    hass: HomeAssistant,
    clock: Clock,
    *,
    profile: PollingProfile = PollingProfile.STANDARD,
) -> tuple[BoschBuderusDataUpdateCoordinator, Cloud]:
    resources = _capture()
    cloud = Cloud(resources, clock)
    client = AsyncMock()
    client.metrics = RequestMetrics()
    client.get_resources_bulk.side_effect = cloud.bulk
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway-1", device_type="k40"), entry, profile
    )
    coordinator.resources = dict(resources)
    coordinator.data = {
        path: ResourceSnapshot(resource, True, clock.utc())
        for path, resource in resources.items()
    }
    coordinator._rebuild_poll_groups()
    # As after the discovery: everything was just read.
    coordinator._next_update = {
        group: clock.now + interval.total_seconds()
        for group, interval in coordinator._poll_intervals.items()
    }
    return coordinator, cloud


async def _cycle(coordinator: BoschBuderusDataUpdateCoordinator) -> None:
    coordinator.data = await coordinator._async_update_data_locked()
    coordinator._last_poll_duration_ms = CYCLE * 1000
    coordinator.async_update_listeners()


async def _run(coordinator, clock: Clock, minutes: int) -> None:
    for _ in range(minutes):
        clock.now += TICK + CYCLE
        await _cycle(coordinator)


def _local_runtime(resources: dict[str, Resource], clock: Clock) -> FakeRuntime:
    """A ready local access with a current reading for each paired path."""
    observations = []
    for index, (path, resource) in enumerate(resources.items()):
        metadata = resource.metadata
        if metadata.resource_type not in {"floatValue", "stringValue"}:
            continue
        raw = {
            "id": path,
            "type": metadata.resource_type,
            "value": resource.value,
            "writeable": 0,
        }
        if metadata.unit:
            raw["unitOfMeasure"] = metadata.unit
        if metadata.allowed_values:
            raw["allowedValues"] = list(metadata.allowed_values)
        try:
            observations.append(
                read(raw, observed_at=clock.utc(), observed_monotonic=float(index))
            )
        except Exception:
            continue
    return FakeRuntime(observations)


def _with_local(coordinator, resources, clock, *, enabled=True) -> FakeRuntime:
    runtime = _local_runtime(resources, clock)
    runtime.snapshot.enabled = enabled
    coordinator.local_selection = LocalSelection(runtime, TARGET)
    return runtime


def _per_hour(reads: dict[str, list[float]]) -> int:
    return sum(len(item) for item in reads.values())


# ------------------------------------------------------------ slot planner


def test_a_small_plant_keeps_every_read_at_its_deadline() -> None:
    paths = {
        PollGroup.FAST: tuple(f"/f{index}" for index in range(10)),
        PollGroup.CONTROL: tuple(f"/c{index}" for index in range(10)),
        PollGroup.SLOW: tuple(f"/s{index}" for index in range(5)),
    }
    slots = plan_poll_slots(
        paths,
        {PollGroup.FAST: 1, PollGroup.CONTROL: 5, PollGroup.SLOW: 15},
        capacity=30,
    )
    assert {slot for _, slot in slots.values()} == {0}
    assert set(slots) == {path for group in paths.values() for path in group}


def test_the_reference_plant_needs_one_batch_per_minute() -> None:
    """20 live values, 32 every five minutes, 16 slow and 9 hourly paths."""
    paths = {
        PollGroup.FAST: tuple(f"/f{index}" for index in range(20)),
        PollGroup.CONTROL: tuple(f"/c{index}" for index in range(27)),
        PollGroup.ENERGY: tuple(f"/e{index}" for index in range(4)),
        PollGroup.NOTIFICATIONS: ("/notifications",),
        PollGroup.SLOW: tuple(f"/s{index}" for index in range(16)),
        PollGroup.HOURLY: tuple(f"/h{index}" for index in range(9)),
    }
    periods = {
        PollGroup.FAST: 1,
        PollGroup.CONTROL: 5,
        PollGroup.ENERGY: 5,
        PollGroup.NOTIFICATIONS: 5,
        PollGroup.SLOW: 15,
        PollGroup.HOURLY: 60,
    }
    slots = plan_poll_slots(paths, periods, capacity=30)
    unspread = {path: (group, 0) for group, items in paths.items() for path in items}
    assert batches_per_period(paths, periods, unspread, capacity=30) == 76
    assert batches_per_period(paths, periods, slots, capacity=30) == 60
    # Each path keeps its own group; only the phase moved.
    assert all(
        slots[path][0] is group for group, items in paths.items() for path in items
    )


def test_slots_stay_while_the_group_stays() -> None:
    paths = {
        PollGroup.FAST: tuple(f"/f{index}" for index in range(28)),
        PollGroup.CONTROL: tuple(f"/c{index}" for index in range(12)),
    }
    periods = {PollGroup.FAST: 1, PollGroup.CONTROL: 5}
    first = plan_poll_slots(paths, periods, capacity=30)
    assert {slot for _, slot in first.values()} == {0, 1, 2, 3, 4}
    # A new live value takes room: the planned paths keep their slots.
    grown = {**paths, PollGroup.FAST: (*paths[PollGroup.FAST], "/f28")}
    second = plan_poll_slots(grown, periods, capacity=30, previous=first)
    assert all(second[path] == first[path] for path in first)
    # A path that changed its group is planned again.
    moved = {PollGroup.FAST: paths[PollGroup.FAST], PollGroup.SLOW: ("/c0",)}
    third = plan_poll_slots(
        moved, {**periods, PollGroup.SLOW: 15}, capacity=30, previous=first
    )
    assert third["/c0"][0] is PollGroup.SLOW


def test_empty_wake_ups_stay_empty_and_full_ones_add_least() -> None:
    # Cloud-friendly: live values every second wake-up; the others never
    # start a batch on a wake-up without one.
    paths = {
        PollGroup.FAST: tuple(f"/f{index}" for index in range(10)),
        PollGroup.CONTROL: tuple(f"/c{index}" for index in range(30)),
    }
    slots = plan_poll_slots(
        paths, {PollGroup.FAST: 2, PollGroup.CONTROL: 10}, capacity=30
    )
    assert all(slot % 2 == 0 for _, slot in slots.values())
    # Without room anywhere, the deadline itself is used, as before.
    full = {PollGroup.FAST: tuple(f"/f{i}" for i in range(30)), PollGroup.SLOW: ("/s",)}
    planned = plan_poll_slots(
        full, {PollGroup.FAST: 1, PollGroup.SLOW: 15}, capacity=30
    )
    assert planned["/s"] == (PollGroup.SLOW, 0)
    with pytest.raises(ValueError, match="capacity"):
        plan_poll_slots(full, {PollGroup.FAST: 1}, capacity=0)


# ------------------------------------------------ O41 on the reference plant


async def test_reference_plant_uses_one_batch_per_minute(hass: HomeAssistant) -> None:
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        await _run(coordinator, clock, 120)
    start, end = clock.now - 60 * (TICK + CYCLE) + 1, clock.now + 1
    reads = cloud.reads(start, end)
    # One batch per minute, and the fault sources in one of their own every
    # five minutes, so that no failing reading takes them along.
    assert cloud.batch_count(start, end) == 60 + 12
    # Exactly as many path reads as the groups promise: 20 every minute,
    # 32 every five, 16 every fifteen minutes and 9 every hour.
    assert _per_hour(reads) == 60 * 20 + 12 * 32 + 4 * 16 + 9
    all_reads = cloud.reads(0, end)
    for path, times in all_reads.items():
        interval = coordinator._poll_intervals[
            coordinator._validity_group(coordinator.resources[path])
        ]
        gaps = [later - earlier for earlier, later in pairwise(times)]
        # Never less often than the group allows by more than one wake-up,
        # and never more often after the first period.
        assert max(gaps, default=0) <= interval.total_seconds() + TICK + CYCLE * 2
        assert min(gaps[1:], default=interval.total_seconds()) >= (
            interval.total_seconds() - TICK
        )


async def test_without_slots_the_same_plant_needs_16_more_batches(
    hass: HomeAssistant,
) -> None:
    """The behaviour before O41, for comparison."""
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    with (
        patch.object(coordinator, "_slot_plan", dict),
        _patched(clock)[0],
        _patched(clock)[1],
        _patched(clock)[2],
    ):
        await _run(coordinator, clock, 120)
    start, end = clock.now - 60 * (TICK + CYCLE) + 1, clock.now + 1
    assert cloud.batch_count(start, end) == 76 + 12
    assert _per_hour(cloud.reads(start, end)) == 60 * 20 + 12 * 32 + 4 * 16 + 9


async def test_a_failed_early_read_comes_again_and_a_refresh_reads_all(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    slots = coordinator._slot_plan()
    early = next(path for path, (_, slot) in slots.items() if slot >= 2)
    group = slots[early][0]
    failing = {"active": True}
    original = cloud.bulk

    async def bulk(gateway, paths):
        if failing["active"] and early in paths:
            failing["active"] = False
            raise RequestTimeout
        return await original(gateway, paths)

    coordinator.client.get_resources_bulk.side_effect = bulk
    coordinator.client.get_resource.side_effect = RequestTimeout()
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        while failing["active"]:
            clock.now += TICK + CYCLE
            try:
                await _cycle(coordinator)
            except UpdateFailed:
                assert not failing["active"]
        assert early not in coordinator._spread_progress.get(group, set())
        failed_at = clock.now
        clock.now += TICK + CYCLE
        await _cycle(coordinator)
        assert early in cloud.reads(failed_at + 1, clock.now + 1)
        assert early in coordinator._spread_progress[group]
        # Not read again before its group's deadline.
        clock.now += TICK + CYCLE
        await _cycle(coordinator)
        assert early not in cloud.reads(clock.now - 1, clock.now + 1)
        # An explicit refresh reads everything again, also early paths.
        with patch(
            "homeassistant.helpers.update_coordinator.DataUpdateCoordinator."
            "async_request_refresh",
            AsyncMock(),
        ):
            await coordinator.async_request_refresh()
        assert not coordinator._spread_progress
        clock.now += 5
        await _cycle(coordinator)
    polled = {
        path
        for group_paths in coordinator._paths_by_group.values()
        for path in group_paths
    }
    assert set(cloud.reads(clock.now - 1, clock.now + 1)) == polled


async def test_an_early_read_comes_again_while_its_group_stays_incomplete(
    hass: HomeAssistant,
) -> None:
    """A path of the same group keeps failing with its whole batch while the
    cycles go on: a path read in its slot is read again within one period
    of its group (and two wake-ups), and its value never expires."""
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    slots = coordinator._slot_plan()
    group = PollGroup.CONTROL
    poison = next(p for p, (g, s) in slots.items() if g is group and s == 0)
    early = next(p for p, (g, s) in slots.items() if g is group and s >= 1)
    original = cloud.bulk

    async def bulk(gateway, paths):
        if poison in paths:
            raise RequestTimeout
        return await original(gateway, paths)

    async def single(gateway, path, **kwargs):
        return cloud.resources[path]

    coordinator.client.get_resources_bulk.side_effect = bulk
    coordinator.client.get_resource.side_effect = single
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        for _ in range(40):
            clock.now += TICK + CYCLE
            await _cycle(coordinator)  # the cycles themselves never fail
            shown = coordinator.displayed_cloud_snapshot(early)
            assert shown is not None and shown.available, clock.now
    reads = cloud.reads(0, clock.now + 1)[early]
    assert reads, "never read"
    # Its period, the wake-up of slack and the wake-up it then falls on.
    limit = coordinator._poll_intervals[group].total_seconds() + 2 * (TICK + CYCLE)
    assert all(later - first <= limit for first, later in pairwise(reads))
    assert clock.now - reads[-1] <= limit
    # The group itself is still waiting for the failing path.
    assert coordinator._next_update[group] < clock.now


async def test_budget_progress_counts_for_one_period_only(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    interval = coordinator._poll_intervals[PollGroup.FAST].total_seconds()
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        # Read in an earlier cycle of this period: not read again yet.
        coordinator._budget_progress = {PollGroup.FAST: {SUPPLY: clock.now}}
        clock.now += TICK + CYCLE
        await _cycle(coordinator)
        assert SUPPLY not in cloud.reads(clock.now - 1, clock.now + 1)
        # Read longer ago than one period and one wake-up: read again.
        coordinator._budget_progress = {
            PollGroup.FAST: {SUPPLY: clock.now - interval - TICK}
        }
        clock.now += TICK + CYCLE
        await _cycle(coordinator)
        assert SUPPLY in cloud.reads(clock.now - 1, clock.now + 1)


async def test_an_active_notification_is_read_every_minute(hass) -> None:
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    coordinator._poll_slots = {"/notifications": (PollGroup.NOTIFICATIONS, 3)}
    coordinator._poll_slots_key = tuple(coordinator._paths_by_group.items())
    with (
        patch.object(
            type(coordinator.faults),
            "active",
            new_callable=PropertyMock,
            return_value=(object(),),
        ),
        _patched(clock)[0],
        _patched(clock)[1],
        _patched(clock)[2],
    ):
        coordinator._advance_groups((PollGroup.NOTIFICATIONS,), clock.now)
        await _run(coordinator, clock, 5)
    times = cloud.reads(0, clock.now + 1)["/notifications"]
    assert len(times) == 5  # every wake-up, never as an early read


# -------------------------------------------- O45 locally shown readings


async def test_locally_shown_readings_are_read_every_fifteen_minutes(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    resources = _capture()
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        _with_local(coordinator, resources, clock)
        await _run(coordinator, clock, 120)
        served = set(coordinator._local_served)
        assert {SUPPLY, DHW_TEMP, ROOM_SETPOINT} <= served
        # Controls, counters, calculated inputs and slow values keep their rate.
        assert not served & set(KEPT)
        assert all(
            not coordinator.data[path].resource.metadata.writable for path in served
        )
        assert "/heatSources/emon/totalConsumption" not in served
        assert "/system/sensors/temperatures/outdoor_t1" not in served
        start, end = clock.now - 60 * (TICK + CYCLE) + 1, clock.now + 1
        reads = cloud.reads(start, end)
        for path in served:
            assert len(reads[path]) == 4, path
            times = cloud.reads(0, end)[path]
            gaps = [b - a for a, b in pairwise(times[-4:])]
            assert all(abs(gap - 15 * (TICK + CYCLE)) <= TICK for gap in gaps)
        # Fewer path reads, still one batch per minute and the fault sources.
        full = 60 * 20 + 12 * 32 + 4 * 16 + 9
        assert _per_hour(reads) == full - len(served) * (60 - 4)
        assert cloud.batch_count(start, end) == 60 + 12
        # The cloud copy, up to 15 minutes old, never expires for that.
        for path in served:
            snapshot = coordinator.data[path]
            assert clock.utc() - snapshot.last_success < timedelta(minutes=16)
            assert coordinator.displayed_cloud_snapshot(path).available
            assert coordinator.source_snapshot(path, "cloud").available
            assert coordinator.display_snapshot(path).source is SnapshotSource.LOCAL
        # The local polling cap keeps the normal cloud interval.
        assert coordinator.scheduled_interval(SUPPLY) == (True, timedelta(seconds=60))
        assert coordinator.diagnostics_summary()["resources_expired"] == 0
        summary = coordinator.cloud_rate_summary()
        assert summary["local_served_paths"] == len(served)
        assert summary["local_served_interval_seconds"] == 900
        assert summary["early_read_paths"] > 0
        assert summary["switch_reads_pending"] == 0


async def test_a_switch_to_the_cloud_reads_at_once_then_every_minute(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    resources = _capture()
    switched: list[bool] = []
    with (
        _patched(clock)[0],
        _patched(clock)[1],
        _patched(clock)[2],
        patch.object(
            coordinator, "_request_switch_read", lambda: switched.append(True)
        ),
    ):
        runtime = _with_local(coordinator, resources, clock)
        await _run(coordinator, clock, 25)
        served = set(coordinator._local_served)
        assert SUPPLY in served
        age = clock.utc() - coordinator.data[SUPPLY].last_success
        assert age > timedelta(minutes=2)  # older than the normal limit
        # The local access stops (not ready, paused or failed): the entities
        # show the cloud copy at once, still valid, and it is read now.
        runtime.observations = []
        clock.now += 5
        coordinator.async_update_listeners()
        assert switched == [True]
        assert not coordinator._local_served
        assert coordinator._urgent_reads == served
        shown = coordinator.display_snapshot(SUPPLY)
        assert shown.source is not SnapshotSource.LOCAL and shown.available
        before = len(cloud.batches)
        clock.now += 2  # the immediate cycle, between two wake-ups
        await _cycle(coordinator)
        assert set(cloud.batches[before][1]) == served
        assert not coordinator._urgent_reads
        assert coordinator.data[SUPPLY].last_success == clock.utc()
        # From now on at the normal rate again.
        await _run(coordinator, clock, 5)
        assert len(cloud.reads(clock.now - 4.9 * TICK, clock.now + 1)[SUPPLY]) == 5


async def test_a_switch_without_a_cloud_shows_the_copy_only_briefly(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    coordinator, _cloud = _plant(hass, clock)
    resources = _capture()
    with (
        _patched(clock)[0],
        _patched(clock)[1],
        _patched(clock)[2],
        patch.object(coordinator, "_request_switch_read", lambda: None),
    ):
        runtime = _with_local(coordinator, resources, clock)
        await _run(coordinator, clock, 25)
        read_at = coordinator.data[SUPPLY].last_success
        limit = coordinator.validity_limit(coordinator.data[SUPPLY])
        # While shown locally: the slower rate plus the normal limit.
        normal = (timedelta(seconds=60) + timedelta(seconds=CYCLE)) * 3
        assert limit == LOCAL_SERVED_INTERVAL + normal
        runtime.observations = []
        coordinator.async_update_listeners()
        left = clock.utc()
        # The normal limit counts from the switch, never beyond the slower one.
        assert coordinator.validity_limit(coordinator.data[SUPPLY]) == min(
            LOCAL_SERVED_INTERVAL + normal, (left - read_at) + normal
        )
        clock.now += normal.total_seconds() - 1
        assert coordinator.displayed_cloud_snapshot(SUPPLY).available
        clock.now += 2
        shown = coordinator.displayed_cloud_snapshot(SUPPLY)
        assert not shown.available and shown.last_error_category == "expired"
        # A copy older than the slower limit is not brought back by a switch.
        old = replace(
            coordinator.data[SUPPLY],
            last_success=clock.utc() - LOCAL_SERVED_INTERVAL - 2 * normal,
        )
        coordinator._served_left[SUPPLY] = clock.utc()
        assert coordinator.validity_limit(old) == LOCAL_SERVED_INTERVAL + normal


async def test_switching_the_option_off_reads_the_cloud_at_its_normal_rate(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    resources = _capture()
    with (
        _patched(clock)[0],
        _patched(clock)[1],
        _patched(clock)[2],
        patch.object(coordinator, "_request_switch_read", lambda: None),
    ):
        runtime = _with_local(coordinator, resources, clock)
        await _run(coordinator, clock, 10)
        assert SUPPLY in coordinator._local_served
        runtime.snapshot.enabled = False
        await _run(coordinator, clock, 3)
        assert not coordinator._local_served
        assert len(cloud.reads(clock.now - 2.9 * TICK, clock.now + 1)[SUPPLY]) == 3


async def test_with_the_option_off_nothing_is_read_less_often(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        _with_local(coordinator, _capture(), clock, enabled=False)
        await _run(coordinator, clock, 70)
    assert not coordinator._local_served
    start, end = clock.now - 60 * (TICK + CYCLE) + 1, clock.now + 1
    assert _per_hour(cloud.reads(start, end)) == 60 * 20 + 12 * 32 + 4 * 16 + 9


async def test_a_confirmed_write_reads_its_circuit_with_the_next_cycle(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        _with_local(coordinator, _capture(), clock)
        await _run(coordinator, clock, 3)
        assert ROOM_SETPOINT in coordinator._local_served
        assert SUPPLY in coordinator._local_served
        coordinator._read_circuit_soon("/heatingCircuits/hc1/manualRoomSetpoint")
        assert coordinator._local_served[ROOM_SETPOINT] == 0.0
        assert coordinator._local_served[SUPPLY] > clock.now
        await _run(coordinator, clock, 1)
        reads = cloud.reads(clock.now - 1, clock.now + 1)
        assert ROOM_SETPOINT in reads and SUPPLY not in reads
        assert coordinator._local_served[ROOM_SETPOINT] > clock.now + 800


async def test_write_control_marks_the_circuit(hass: HomeAssistant) -> None:
    from custom_components.bosch_buderus_heating.writes import NumberWritePolicy

    clock = Clock()
    coordinator, _cloud = _plant(hass, clock)
    path = "/heatingCircuits/hc1/maxFlowTemp"
    snapshot = coordinator.data[path]
    coordinator._local_served[ROOM_SETPOINT] = clock.now + 600
    coordinator._write_service = SimpleNamespace(
        async_write_number=AsyncMock(
            return_value=SimpleNamespace(resource=snapshot.resource)
        ),
        # D25: the deadline reads the read-back schedule.
        read_back_delays=lambda _context: (),
    )
    with patch.object(coordinator, "async_set_updated_data"):
        await coordinator.async_write_control(
            path, 50.0, NumberWritePolicy(path_pattern=".*", unit="C", step=1.0)
        )
    assert coordinator._local_served[ROOM_SETPOINT] == 0.0


async def test_cloud_friendly_profile_uses_whole_intervals(hass) -> None:
    clock = Clock()
    coordinator, _cloud = _plant(hass, clock, profile=PollingProfile.CLOUD_FRIENDLY)
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        _with_local(coordinator, _capture(), clock)
        await _run(coordinator, clock, 4)
    assert SUPPLY in coordinator._local_served
    assert coordinator._served_period(SUPPLY) == timedelta(minutes=14)
    # A ten-minute group would gain nothing within 15 minutes.
    control = Resource(
        "/heatSources/x",
        value=1.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )
    coordinator.data["/heatSources/x"] = ResourceSnapshot(control, True, clock.utc())
    coordinator._validity_index = {"/heatSources/x": PollGroup.CONTROL}
    coordinator._validity_index_key = tuple(coordinator._paths_by_group.items())
    with patch(f"{COORDINATOR}._CLOUD_COPY_IN_USE") as kept:
        kept.match.return_value = None
        with patch(
            "custom_components.bosch_buderus_heating.local_selection.eligible",
            return_value=True,
        ):
            assert coordinator._served_period("/heatSources/x") is None


async def test_the_switch_read_starts_a_cycle_without_forcing_groups(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    coordinator, _cloud = _plant(hass, clock)
    deadlines = dict(coordinator._next_update)
    with patch.object(
        coordinator, "_async_update_data", AsyncMock(return_value=coordinator.data)
    ) as update:
        coordinator._request_switch_read()
        await hass.async_block_till_done()
        assert update.await_count == 1
        assert coordinator._next_update == deadlines
    await coordinator.async_shutdown()
    # Without a working cloud the next regular cycle reads them first.
    coordinator.last_update_success = False
    with patch.object(coordinator.config_entry, "async_create_background_task") as task:
        coordinator._request_switch_read()
    task.assert_not_called()


async def test_views_without_polling_never_use_the_slower_rate(hass) -> None:
    clock = Clock()
    coordinator, _cloud = _plant(hass, clock)
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        _with_local(coordinator, _capture(), clock)
        coordinator.update_interval = None
        coordinator.async_update_listeners()
    assert not coordinator._local_served


def test_intervals_of_the_standard_profile() -> None:
    # The slower rate is a whole number of the live interval.
    assert LOCAL_SERVED_INTERVAL % POLL_INTERVALS[PollGroup.FAST] == timedelta(0)
    assert LOCAL_SERVED_INTERVAL % POLL_INTERVALS[PollGroup.CONTROL] == timedelta(0)


async def test_without_a_cloud_the_option_off_keeps_the_normal_rate(hass) -> None:
    """Shown locally only because the cloud is down: nothing to slow down."""
    clock = Clock()
    coordinator, _cloud = _plant(hass, clock)
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        _with_local(coordinator, _capture(), clock, enabled=False)
        coordinator.last_update_success = False
        assert coordinator.display_snapshot(SUPPLY).source is SnapshotSource.LOCAL
        coordinator.async_update_listeners()
    assert not coordinator._local_served


async def test_a_refresh_reads_locally_shown_readings_too(hass) -> None:
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        _with_local(coordinator, _capture(), clock)
        await _run(coordinator, clock, 3)
        assert SUPPLY in coordinator._local_served
        with patch(
            "homeassistant.helpers.update_coordinator.DataUpdateCoordinator."
            "async_request_refresh",
            AsyncMock(),
        ):
            await coordinator.async_request_refresh()
        assert set(coordinator._local_served.values()) == {0.0}
        clock.now += 5
        await _cycle(coordinator)
    assert SUPPLY in cloud.reads(clock.now - 1, clock.now + 1)


async def test_a_new_discovery_starts_new_periods(hass) -> None:
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    coordinator._spread_progress = {PollGroup.CONTROL: {"/heatSources/x": 0.0}}
    coordinator._urgent_reads = {SUPPLY}
    coordinator._local_served = {SUPPLY: 1e12}
    with (
        _patched(clock)[0],
        _patched(clock)[1],
        _patched(clock)[2],
        patch(
            f"{COORDINATOR}.async_discover_resources",
            AsyncMock(return_value=dict(cloud.resources)),
        ),
    ):
        await coordinator._async_discover_data()
    assert not coordinator._spread_progress
    assert not coordinator._urgent_reads
    assert coordinator._local_served[SUPPLY] == clock.now + 900
    coordinator._spread_progress = {PollGroup.CONTROL: {"/heatSources/x": 0.0}}
    coordinator.start_from_inventory(
        dict(cloud.resources), reason="test", age_seconds=0, seen={}
    )
    assert not coordinator._spread_progress


# ------------------------------------------- with the managed sources


@pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
async def test_managed_switch_reads_the_cloud_value_at_once(hass) -> None:
    from tests.test_local_backed import (
        CURRENT,
        GATEWAY,
        ReadingBackend,
        local_values,
        started,
        state,
    )
    from tests.test_managed_pointt import eventually, running, source_for

    hass.config.components.add(DOMAIN)
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, _, owner, _):
        await started(hass, owner)
        coordinator = source_for(owner, GATEWAY)
        await eventually(lambda: CURRENT in coordinator._local_served)
        # The local value is shown; its cloud copy is ten minutes old.
        coordinator.data[CURRENT] = replace(
            coordinator.data[CURRENT],
            last_success=dt_util.utcnow() - timedelta(minutes=10),
        )
        before = len(backend.calls)
        runtime = owner.connections
        local_state = runtime._local_states["local"]
        runtime._local_states["local"] = SimpleNamespace(
            context=local_state.context, current_observations=()
        )
        runtime._notify()  # the local access stopped delivering
        await eventually(
            lambda: any(
                call[0] == "bulk" and CURRENT in call[2]
                for call in backend.calls[before:]
            )
        )
        await eventually(lambda: state(hass, owner) == "20.0")
        assert CURRENT not in coordinator._local_served
        # The request is recorded before its answer is applied: wait for it.
        await eventually(
            lambda: (
                coordinator.data[CURRENT].last_success
                > dt_util.utcnow() - timedelta(minutes=1)
            )
        )
