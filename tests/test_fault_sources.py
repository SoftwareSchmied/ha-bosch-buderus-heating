"""Fault sources: read together (FAULT-1), retired only when gone (FAULT-2),
and the event entity without the cloud (FAULT-4)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    CYCLE_BUDGET_SECONDS,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.discovery import (
    DiscoveryDiagnostics,
    DiscoveryPathDiagnostic,
    DiscoveryPathSource,
)
from custom_components.bosch_buderus_heating.event import (
    BoschBuderusNotificationEvent,
)
from custom_components.bosch_buderus_heating.faults import (
    FAULT_SOURCE_ISSUE_PREFIX,
    FaultEventType,
    FaultTracker,
    discovery_fully_answered,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Resource,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    RequestTimeout,
    ResourceError,
)
from custom_components.bosch_buderus_heating.poll_slots import plan_poll_slots
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    poll_group,
)
from tests.test_cloud_polling_plan import Clock, Cloud, _patched, _plant, _run
from tests.test_event import _coordinator

NOW = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)
NOTIFICATIONS = "/notifications"
DEVICE = "/devices/device3/errors"
HEAT_SOURCE = "/heatSources/hs1/activefailure"
FAULT = {"ccd": "6249", "fc": "12"}


def _notifications(*values: object) -> Resource:
    return Resource(path=NOTIFICATIONS, values=values, has_values=True)  # type: ignore[arg-type]


def _device(path: str = DEVICE) -> Resource:
    return Resource(path=path, values=(), has_values=True)


def _issues(hass: HomeAssistant) -> list[ir.IssueEntry]:
    return [
        issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue_id.startswith(FAULT_SOURCE_ISSUE_PREFIX)
    ]


def _tracker_with_device_fault(hass: HomeAssistant) -> FaultTracker:
    """A fault on the system list, and a radio device's error list as source."""
    tracker = FaultTracker(hass, "entry", "gateway")
    active = _notifications(FAULT)
    tracker.process_resources({active.path: active, DEVICE: _device()}, observed_at=NOW)
    assert len(tracker.active_faults) == 1
    return tracker


def _empty_reads(tracker: FaultTracker, at: datetime, count: int = 2) -> list:
    empty = _notifications()
    events = []
    for index in range(count):
        events.extend(
            tracker.process_resources(
                {empty.path: empty}, observed_at=at + timedelta(minutes=index)
            )
        )
    return events


# ---------------------------------------------- FAULT-1: read together


@pytest.mark.parametrize("fast", range(61))
def test_the_planner_never_spreads_the_fault_sources(fast: int) -> None:
    """The case the review found: 28, 29, 58 and 59 live values split them."""
    notifications = (NOTIFICATIONS, HEAT_SOURCE, "/devices/device1/errors")
    paths = {
        PollGroup.FAST: tuple(f"/f{index}" for index in range(fast)),
        PollGroup.NOTIFICATIONS: notifications,
        PollGroup.CONTROL: tuple(f"/c{index}" for index in range(27)),
        PollGroup.ENERGY: tuple(f"/e{index}" for index in range(4)),
        PollGroup.SLOW: tuple(f"/s{index}" for index in range(16)),
        PollGroup.HOURLY: tuple(f"/h{index}" for index in range(9)),
    }
    periods = {
        PollGroup.FAST: 1,
        PollGroup.NOTIFICATIONS: 5,
        PollGroup.CONTROL: 5,
        PollGroup.ENERGY: 5,
        PollGroup.SLOW: 15,
        PollGroup.HOURLY: 60,
    }
    previous = {path: (PollGroup.NOTIFICATIONS, 2) for path in notifications}
    slots = plan_poll_slots(
        paths,
        periods,
        capacity=30,
        previous=previous,
        atomic=(PollGroup.NOTIFICATIONS,),
    )
    assert {slots[path] for path in notifications} == {(PollGroup.NOTIFICATIONS, 0)}
    # Without it, the same plan does spread them for this plant.
    if fast in (28, 29, 58, 59):
        spread = plan_poll_slots(paths, periods, capacity=30)
        assert len({spread[path][1] for path in notifications}) > 1


def _plant_with_fault_sources(hass: HomeAssistant, clock: Clock, fast_count: int):
    """The reference plant with two more fault sources and more live values."""
    coordinator, cloud = _plant(hass, clock)
    template = coordinator.resources[NOTIFICATIONS]
    for path in (HEAT_SOURCE, "/devices/device1/errors"):
        resource = replace(template, path=path)
        coordinator.resources[path] = cloud.resources[path] = resource
        coordinator.data[path] = ResourceSnapshot(resource, True, clock.utc())
    coordinator._rebuild_poll_groups()
    fast = list(coordinator._paths_by_group[PollGroup.FAST])
    missing = fast_count - len(fast)
    assert missing >= 0
    copy = 0
    while missing:
        copy += 1
        for source in fast:
            candidate = replace(coordinator.resources[source], path=f"{source}x{copy}")
            if poll_group(candidate) is PollGroup.FAST:
                coordinator.resources[candidate.path] = candidate
                cloud.resources[candidate.path] = candidate
                coordinator.data[candidate.path] = ResourceSnapshot(
                    candidate, True, clock.utc()
                )
                missing -= 1
                break
    coordinator._rebuild_poll_groups()
    sources = coordinator._paths_by_group[PollGroup.NOTIFICATIONS]
    assert set(sources) == {NOTIFICATIONS, HEAT_SOURCE, "/devices/device1/errors"}
    # The first complete read, as after the discovery.
    coordinator.faults.process_resources(
        {path: coordinator.resources[path] for path in sources}
    )
    assert coordinator.faults.has_known_state
    return coordinator, cloud, sources


@pytest.mark.parametrize("fast_count", [28, 29, 58])
async def test_a_healthy_plant_keeps_a_known_fault_state(
    hass: HomeAssistant, fast_count: int
) -> None:
    clock = Clock()
    coordinator, cloud, sources = _plant_with_fault_sources(hass, clock, fast_count)
    known = []
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        for _ in range(30):
            await _run(coordinator, clock, 1)
            known.append(coordinator.faults.has_known_state)
    assert all(known)
    batches = [set(paths) & set(sources) for _, paths in cloud.batches]
    # Every read of the group contains all of its sources.
    assert [found for found in batches if found] == [set(sources)] * 6


async def test_the_cycle_budget_never_splits_the_fault_sources(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    coordinator, cloud, sources = _plant_with_fault_sources(hass, clock, 58)

    async def slow(gateway: str, paths) -> tuple[BatchItemResult, ...]:
        result = await Cloud.bulk(cloud, gateway, paths)
        clock.now += CYCLE_BUDGET_SECONDS  # every batch uses the whole budget
        return result

    coordinator.client.get_resources_bulk.side_effect = slow
    # Work left over from an interrupted cycle comes first otherwise.
    fast = coordinator._paths_by_group[PollGroup.FAST]
    assert len(fast) == 58
    coordinator._deferred_poll_paths = fast
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        for group in (PollGroup.FAST, PollGroup.NOTIFICATIONS):
            coordinator._next_update[group] = clock.now
        clock.now += 1
        await coordinator._async_update_data_locked()
    assert set(sources) <= set(cloud.batches[0][1])
    assert len(cloud.batches) == 1  # the budget stopped the cycle
    assert coordinator.faults.has_known_state


# --------------------------------------- FAULT-2: a source that is gone


async def test_a_source_answering_404_for_a_day_is_retired(
    hass: HomeAssistant,
) -> None:
    tracker = _tracker_with_device_fault(hass)
    gone = BatchItemResult("gateway", DEVICE, 404)
    tracker.record_results((gone,), observed_at=NOW + timedelta(minutes=5))
    # The device fault's source is missing: nothing ends, for a whole day.
    assert _empty_reads(tracker, NOW + timedelta(minutes=6), 3) == []
    tracker.record_results((gone,), observed_at=NOW + timedelta(hours=24))
    assert _empty_reads(tracker, NOW + timedelta(hours=24, minutes=1), 3) == []
    assert len(tracker.active_faults) == 1
    assert not _issues(hass)

    tracker.record_results((gone,), observed_at=NOW + timedelta(hours=24, minutes=5))
    events = _empty_reads(tracker, NOW + timedelta(hours=24, minutes=6))
    assert [event.event_type for event in events] == [FaultEventType.RESOLVED]
    assert not tracker.active_faults
    assert tracker.has_known_state
    (issue,) = _issues(hass)
    assert issue.translation_key == "fault_source_removed"
    assert issue.translation_placeholders == {"count": "1"}
    assert not issue.is_fixable and not issue.is_persistent
    assert tracker.diagnostics()["fault_sources"] == {
        "required": 1,
        "not_found": 0,
        "removed": 1,
        "unverified_faults": 0,
    }
    # It answers again: required again, without a new notice.
    tracker.process_resources(
        {NOTIFICATIONS: _notifications(), DEVICE: _device()},
        observed_at=NOW + timedelta(hours=25),
    )
    assert tracker.diagnostics()["fault_sources"]["required"] == 2


@pytest.mark.parametrize(
    "other",
    [
        BatchItemResult("gateway", DEVICE, 503),
        BatchItemResult("gateway", DEVICE, None, error=RequestTimeout()),
        BatchItemResult("gateway", DEVICE, 403),
        BatchItemResult("gateway", DEVICE, 200, resource=_device()),
    ],
    ids=["5xx", "timeout", "403", "answer"],
)
async def test_any_other_answer_ends_the_run_of_404_answers(
    hass: HomeAssistant, other: BatchItemResult
) -> None:
    tracker = _tracker_with_device_fault(hass)
    gone = BatchItemResult("gateway", DEVICE, 404)
    tracker.record_results((gone,), observed_at=NOW)
    tracker.record_results((other,), observed_at=NOW + timedelta(hours=12))
    tracker.record_results((gone,), observed_at=NOW + timedelta(hours=24))
    tracker.record_results((gone,), observed_at=NOW + timedelta(hours=30))
    _empty_reads(tracker, NOW + timedelta(hours=30, minutes=1), 3)
    assert len(tracker.active_faults) == 1
    assert not _issues(hass)


@pytest.mark.parametrize(
    "failure",
    [
        BatchItemResult("gateway", DEVICE, 503),
        BatchItemResult("gateway", DEVICE, None, error=RequestTimeout()),
        BatchItemResult("gateway", DEVICE, 403),
    ],
    ids=["5xx", "timeout", "403"],
)
async def test_a_source_that_keeps_failing_is_never_retired(
    hass: HomeAssistant, failure: BatchItemResult
) -> None:
    tracker = _tracker_with_device_fault(hass)
    for hour in range(0, 97, 6):
        tracker.record_results((failure,), observed_at=NOW + timedelta(hours=hour))
        _empty_reads(tracker, NOW + timedelta(hours=hour, minutes=1), 2)
    assert len(tracker.active_faults) == 1
    assert tracker.diagnostics()["fault_sources"]["required"] == 2
    assert not _issues(hass)


async def test_a_run_of_404_answers_survives_a_restart(hass: HomeAssistant) -> None:
    tracker = _tracker_with_device_fault(hass)
    gone = BatchItemResult("gateway", DEVICE, 404)
    tracker.record_results((gone,), observed_at=NOW)
    stored = json.loads(json.dumps(tracker._serialize()))
    assert "device3" not in json.dumps(stored)
    restarted = FaultTracker(hass, "entry", "gateway")
    restarted._store.async_load = AsyncMock(return_value=stored)
    await restarted.async_load()
    restarted.record_results((gone,), observed_at=NOW + timedelta(hours=24))
    events = _empty_reads(restarted, NOW + timedelta(hours=24, minutes=1))
    assert [event.event_type for event in events] == [FaultEventType.RESOLVED]
    assert len(_issues(hass)) == 1


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"unknown-key": ["2026-11-09T06:00:00+00:00", 1]},
        {"KEY": ["not a time", 1]},
        {"KEY": ["2026-11-09T06:00:00", 1]},
        {"KEY": ["2026-11-09T06:00:00+00:00", 0]},
        {"KEY": ["2026-11-09T06:00:00+00:00", True]},
        {"KEY": ["2026-11-09T06:00:00+00:00"]},
        {"KEY": "2026-11-09T06:00:00+00:00"},
    ],
)
async def test_an_invalid_stored_run_starts_again(hass: HomeAssistant, value) -> None:
    tracker = _tracker_with_device_fault(hass)
    stored = tracker._serialize()
    key = next(
        key for key in stored["required_source_keys"] if key != _key(NOTIFICATIONS)
    )
    if isinstance(value, dict) and "KEY" in value:
        value = {key: value["KEY"]}
    stored["sources_not_found"] = value
    restarted = FaultTracker(hass, "entry", "gateway")
    restarted._store.async_load = AsyncMock(return_value=stored)
    await restarted.async_load()
    gone = BatchItemResult("gateway", DEVICE, 404)
    restarted.record_results((gone,), observed_at=NOW + timedelta(hours=24))
    _empty_reads(restarted, NOW + timedelta(hours=24, minutes=1), 3)
    assert len(restarted.active_faults) == 1  # a new run has just started


def _key(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


async def test_a_clock_that_went_back_starts_the_run_again(
    hass: HomeAssistant,
) -> None:
    """A run begun at a wrong time a year ahead must not hold for a year."""
    tracker = _tracker_with_device_fault(hass)
    gone = BatchItemResult("gateway", DEVICE, 404)
    tracker.record_results((gone,), observed_at=NOW + timedelta(days=365))
    tracker.record_results((gone,), observed_at=NOW)  # the clock is corrected
    tracker.record_results((gone,), observed_at=NOW + timedelta(hours=23))
    _empty_reads(tracker, NOW + timedelta(hours=23, minutes=1), 3)
    assert len(tracker.active_faults) == 1  # the new run is not a day old yet
    tracker.record_results((gone,), observed_at=NOW + timedelta(hours=24))
    events = _empty_reads(tracker, NOW + timedelta(hours=24, minutes=1))
    assert [event.event_type for event in events] == [FaultEventType.RESOLVED]


async def test_a_fully_answered_discovery_retires_a_missing_source(
    hass: HomeAssistant,
) -> None:
    tracker = _tracker_with_device_fault(hass)
    root = Resource(path="/devices", has_values=True)
    tracker.reconcile_discovery({root.path: root, "/devices/device3": root})
    assert not _issues(hass)  # still there: a candidate of the tree
    tracker.reconcile_discovery({root.path: root})
    events = _empty_reads(tracker, NOW + timedelta(minutes=5))
    assert [event.event_type for event in events] == [FaultEventType.RESOLVED]
    (issue,) = _issues(hass)
    assert issue.translation_placeholders == {"count": "1"}


def _diagnostics(*results: tuple[str, str, bool], completed: bool = True):
    diagnostics = DiscoveryDiagnostics()
    diagnostics.completed = completed
    for path, result, discovered in results:
        item = DiscoveryPathDiagnostic(source=DiscoveryPathSource.ROOT)
        item.bulk_result = result
        item.discovered = discovered
        diagnostics.paths[path] = item
    return diagnostics


def test_only_final_answers_make_a_discovery_fully_answered() -> None:
    ok = ("/gateway", "success", True)
    assert discovery_fully_answered(_diagnostics(ok, ("/pv", "http_404", False)))
    assert discovery_fully_answered(_diagnostics(ok, ("/pv", "http_403", False)))
    assert not discovery_fully_answered(_diagnostics(ok, completed=False))
    for result in (
        "http_500",
        "http_503",
        "timeout",
        "malformed",
        "pending",
        "not_attempted",
        "http_401",
        "http_408",
        "http_429",
        "service_unavailable",
    ):
        assert not discovery_fully_answered(
            _diagnostics(ok, ("/devices", result, False))
        ), result
    # Recovered by its individual read.
    recovered = _diagnostics(ok, ("/devices", "http_503", True))
    assert discovery_fully_answered(recovered)
    failed = _diagnostics(ok, ("/devices", "http_503", False))
    failed.paths["/devices"].fallback_result = "http_404"
    assert discovery_fully_answered(failed)
    failed.paths["/devices"].fallback_result = "timeout"
    assert not discovery_fully_answered(failed)
    # Start-up leaves are read elsewhere.
    leaf = _diagnostics(ok, ("/devices/device1/sgtin", "not_attempted", False))
    leaf.paths["/devices/device1/sgtin"].read_policy = "startup_once"
    assert discovery_fully_answered(leaf)


class DiscoveryCloud:
    """The reference plant's tree; one path can fail."""

    def __init__(self, resources: dict[str, Resource]) -> None:
        self.resources = resources
        self.failing: dict[str, BatchItemResult] = {}

    async def bulk(self, gateway: str, paths) -> tuple[BatchItemResult, ...]:
        return tuple(
            self.failing[path]
            if path in self.failing
            else BatchItemResult(gateway, path, 200, resource=self.resources[path])
            if path in self.resources
            else BatchItemResult(gateway, path, 404)
            for path in paths
        )

    async def single(self, gateway: str, path: str, **_: object) -> Resource:
        raise ResourceError(path, 503)


@pytest.mark.parametrize(
    ("failing", "retired"),
    [
        (None, True),
        (BatchItemResult("gateway-1", "/devices/list", 503), False),
        (
            BatchItemResult("gateway-1", "/devices", None, error=RequestTimeout()),
            False,
        ),
    ],
    ids=["complete", "5xx", "timeout"],
)
async def test_a_start_retires_a_device_source_only_after_a_complete_discovery(
    hass: HomeAssistant, failing: BatchItemResult | None, retired: bool
) -> None:
    clock = Clock()
    coordinator, plant = _plant(hass, clock)
    cloud = DiscoveryCloud(dict(plant.resources))
    if failing is not None:
        cloud.failing[failing.path] = failing
    coordinator.client.get_resources_bulk.side_effect = cloud.bulk
    coordinator.client.get_resource.side_effect = cloud.single
    # The stored baseline: a fault, and the removed device's error list.
    active = _notifications(FAULT)
    coordinator.faults.process_resources(
        {NOTIFICATIONS: active, DEVICE: _device()}, observed_at=NOW
    )
    coordinator.resources = {}
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        await coordinator._async_discover_data()
    sources = coordinator.faults.diagnostics()["fault_sources"]
    assert sources["removed"] == (1 if retired else 0)
    assert bool(_issues(hass)) is retired
    # The remaining sources were read completely: the state is confirmed.
    assert coordinator.faults._has_valid_state is retired


# ------------------------------------- FAULT-2: old baselines without evidence


async def test_an_unseen_legacy_fault_ends_after_a_day_of_complete_reads(
    hass: HomeAssistant,
) -> None:
    source = FaultTracker(hass, "source", "gateway")
    active = _notifications(FAULT)
    source.process_resources({active.path: active})
    legacy = {"active": source._serialize()["active"]}  # no source evidence
    tracker = FaultTracker(hass, "entry", "gateway")
    tracker._store.async_load = AsyncMock(return_value=legacy)
    await tracker.async_load()
    empty = _notifications()
    start = NOW
    events = []
    for minute in range(0, 24 * 60, 60):  # hourly complete reads for a day
        events += tracker.process_resources(
            {empty.path: empty}, observed_at=start + timedelta(minutes=minute)
        )
    assert events == []
    assert len(tracker.active_faults) == 1
    # A restart keeps the run.
    stored = json.loads(json.dumps(tracker._serialize()))
    tracker = FaultTracker(hass, "entry", "gateway")
    tracker._store.async_load = AsyncMock(return_value=stored)
    await tracker.async_load()
    events = _empty_reads(tracker, start + timedelta(hours=24), 3)
    assert [event.event_type for event in events] == [FaultEventType.RESOLVED]


async def test_a_legacy_fault_needs_enough_reads_and_a_whole_day(
    hass: HomeAssistant,
) -> None:
    source = FaultTracker(hass, "source", "gateway")
    active = _notifications(FAULT)
    source.process_resources({active.path: active})
    legacy = {"active": source._serialize()["active"]}
    tracker = FaultTracker(hass, "entry", "gateway")
    tracker._store.async_load = AsyncMock(return_value=legacy)
    await tracker.async_load()
    empty = _notifications()
    # Many reads within less than a day.
    for minute in range(0, 23 * 60, 5):
        tracker.process_resources(
            {empty.path: empty}, observed_at=NOW + timedelta(minutes=minute)
        )
    assert len(tracker.active_faults) == 1
    # A day, but too few reads.
    other = FaultTracker(hass, "other", "gateway")
    other._store.async_load = AsyncMock(return_value=legacy)
    await other.async_load()
    for hour in (0, 30, 60):
        _empty_reads(other, NOW + timedelta(hours=hour), 1)
    assert len(other.active_faults) == 1
    # Incomplete reads never count.
    third = FaultTracker(hass, "third", "gateway")
    third._store.async_load = AsyncMock(return_value=legacy)
    await third.async_load()
    third.process_resources({empty.path: empty, DEVICE: _device()}, observed_at=NOW)
    for hour in range(1, 49):
        third.process_resources(
            {empty.path: empty}, observed_at=NOW + timedelta(hours=hour)
        )
    assert len(third.active_faults) == 1


async def test_a_legacy_fault_that_is_reported_again_stays(
    hass: HomeAssistant,
) -> None:
    source = FaultTracker(hass, "source", "gateway")
    active = _notifications(FAULT)
    source.process_resources({active.path: active})
    legacy = {"active": source._serialize()["active"]}
    tracker = FaultTracker(hass, "entry", "gateway")
    tracker._store.async_load = AsyncMock(return_value=legacy)
    await tracker.async_load()
    for hour in range(48):
        assert (
            tracker.process_resources(
                {active.path: active}, observed_at=NOW + timedelta(hours=hour)
            )
            == ()
        )
    assert len(tracker.active_faults) == 1
    assert tracker.current_state_confirmed


# ------------------------------------------- FAULT-4: the event entity


def test_the_event_entity_reports_local_events_without_the_cloud(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    coordinator.last_update_success = False  # no cloud at all
    coordinator.faults.local_authority = True
    entity = BoschBuderusNotificationEvent(coordinator)
    entity._trigger_event = Mock()  # type: ignore[method-assign]
    entity.async_write_ha_state = Mock()  # type: ignore[method-assign]
    remove = coordinator.faults.async_add_listener(entity._handle_fault_event)
    coordinator.faults.process_local(_notifications(), reading=1.0, ready=True)
    coordinator.faults.process_local(_notifications(FAULT), reading=2.0, ready=True)
    assert entity.available
    entity._trigger_event.assert_called_once()
    assert entity._trigger_event.call_args.args[0] == "appeared"
    remove()


def test_the_event_entity_stays_available_during_a_cloud_outage(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    entity = BoschBuderusNotificationEvent(coordinator)
    assert not entity.available  # no fault source yet
    empty = _notifications()
    coordinator.faults.process_resources({empty.path: empty})
    assert entity.available
    coordinator.last_update_success = False
    coordinator.faults.mark_unavailable()
    assert entity.available
