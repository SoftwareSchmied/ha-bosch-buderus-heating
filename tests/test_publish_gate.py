"""X16: unchanged views are not worked out again, changes still arrive at once."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_budget import LocalReadBudget
from custom_components.bosch_buderus_heating.local_polling import service as module
from custom_components.bosch_buderus_heating.local_polling.inventory import (
    Freshness,
    ResourceInventory,
)
from custom_components.bosch_buderus_heating.local_polling.service import (
    LocalPollingService,
)
from custom_components.bosch_buderus_heating.local_polling.storage import (
    LocalScheduleStore,
)
from custom_components.bosch_buderus_heating.local_read import ReadResult, ReadState
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.test_local_budget import Clock
from tests.test_local_polling import REQUEST, no_discovery, signals, success
from tests.test_local_polling_service import Client, advance, settle
from tests.test_local_probe import CONTEXT
from tests.test_local_selection import (
    NOW,
    RETURN,
    TARGET,
    FakeRuntime,
    FakeSnapshot,
    cloud,
    local,
)
from tests.test_validity import _coordinator, _sensor_for


@pytest.fixture
async def running(hass, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "start_offset", lambda scope: 0)
    clock = Clock()
    client = Client(clock)
    store = LocalScheduleStore(hass, "synthetic-scope")
    store._path = tmp_path / "schedule.json"
    published = []
    service = LocalPollingService(
        CONTEXT,
        client,
        LocalReadBudget(CONTEXT, clock=clock),
        store,
        is_current=lambda: True,
        publish=published.append,
        clock=clock,
        period_token=lambda: (),
    )
    no_discovery(service.planner)
    service.planner.hints((REQUEST,), clock())
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    assert client.calls
    yield service, clock, client, published
    await service.close()


def counted(service):
    """The number of views worked out again so far."""
    calls = []
    original = service.inventory.timed_view

    def timed_view(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    service.inventory.timed_view = timed_view
    return calls


async def test_unchanged_views_are_not_worked_out_again(running):
    service, _, _, published = running
    worked = counted(service)
    sent = len(published)
    for _ in range(50):
        service._publish()
    assert not worked and len(published) == sent


async def test_an_ageing_reading_is_published_when_its_freshness_changes(running):
    service, clock, _, published = running
    assert service.snapshot().resources[0].freshness is Freshness.FRESH
    await service.budget.pause(7200)  # no new read; only the age changes
    await settle()
    sent = len(published)
    worked = counted(service)
    await advance(service, clock, 70)
    assert worked and len(published) > sent
    assert published[-1].resources[0].freshness is Freshness.LATE
    await advance(service, clock, 110)
    assert published[-1].resources[0].freshness is Freshness.EXPIRED
    assert not published[-1].resources[0].available


async def test_a_new_reading_is_published_at_once(running):
    service, clock, client, published = running
    sent = len(published)
    before = service.snapshot().resources[0].observation
    clock.advance(1)
    client.result = success(REQUEST, clock(), value=22)
    service.inventory.accept(client.result, clock())
    worked = counted(service)
    service._publish()
    assert worked and len(published) == sent + 1
    shown = published[-1].resources[0].observation
    assert shown is not before and shown.data.value == 22


async def test_a_failed_read_is_published_at_once(running):
    service, clock, _, published = running
    sent = len(published)
    clock.advance(1)
    failed = ReadResult(REQUEST, ReadState.PATH_ERROR, 404, "not_found")
    service.inventory.accept(failed, clock())
    service._publish()
    assert len(published) == sent + 1
    assert published[-1].resources[0].problem == "not_found"
    assert not published[-1].resources[0].available


async def test_a_new_resource_is_published_at_once(running):
    service, clock, _, published = running
    sent = len(published)
    service.inventory.add(request_for_path("/gateway/brand"), clock())
    service._publish()
    assert len(published) == sent + 1
    assert len(published[-1].resources) == 2


async def test_a_pause_of_the_source_is_published_at_once(running):
    service, _, _, published = running
    sent = len(published)
    await service.budget.pause(60)
    service.wake()
    await settle()
    assert len(published) > sent
    assert published[-1].resources[0].available  # still within its age


async def test_a_changed_period_is_worked_out_again(running):
    service, *_ = running
    token = [()]
    service._period_token = lambda: token[0]
    service._publish()  # the token is taken over
    asked = []
    original = service.inventory.freshness_period
    service.inventory.freshness_period = lambda record: (
        asked.append(record) or original(record)
    )
    service._publish()
    assert not asked  # the same token: no period is asked again
    token[0] = ("other",)
    service._publish()
    assert asked  # a new token: every period is asked again


def test_every_change_of_the_records_counts_as_a_revision():
    inventory = ResourceInventory(CONTEXT.identity, 60)
    seen = [inventory.revision]

    def moved() -> bool:
        before = seen[-1]
        seen.append(inventory.revision)
        return seen[-1] > before

    inventory.add(REQUEST, 0)
    assert moved()
    inventory.add(REQUEST, 1)  # the same request again: nothing changes
    assert not moved()
    inventory.accept(success(REQUEST, 1), 1)
    assert moved()
    inventory.accept(ReadResult(REQUEST, ReadState.PATH_ERROR, 404, "not_found"), 2)
    assert moved()
    signals(inventory, ["a", "b"], 3)
    assert moved()
    inventory._drop_observation(inventory.records[REQUEST.target])
    assert moved()
    signals(inventory, ["a"], 4)
    signals(inventory, ["a"], 5)
    moved()
    inventory.prune(10**7)
    assert moved()


# ------------------------------------------------------ changed local paths


FLOW = "/heatingCircuits/hc1/maxFlowTemp"


def test_changed_paths_name_only_the_paths_that_differ():
    runtime = FakeRuntime(
        [local(RETURN, 31.5, monotonic=10.0), local(FLOW, 55.0, monotonic=10.0)]
    )
    selection = LocalSelection(runtime, TARGET)
    assert selection.changed() is True
    assert selection.changed_paths is None  # the first pass: everything
    assert selection.changed() is False
    assert selection.changed_paths == frozenset()
    # The same answers read again: no path.
    runtime.observations = [
        local(RETURN, 31.5, monotonic=70.0),
        local(FLOW, 55.0, monotonic=70.0),
    ]
    assert selection.changed() is False
    assert selection.changed_paths == frozenset()
    # One value changes: only its path.
    runtime.observations = [
        local(RETURN, 31.6, monotonic=130.0),
        local(FLOW, 55.0, monotonic=130.0),
    ]
    assert selection.changed() is True
    assert selection.changed_paths == {RETURN}
    # One reading is gone (expired, paused), one is new.
    runtime.observations = [local(FLOW, 55.0, monotonic=190.0)]
    assert selection.changed() is True
    assert selection.changed_paths == {RETURN}
    runtime.observations = [
        local(FLOW, 55.0, monotonic=250.0),
        local(RETURN, 31.6, monotonic=250.0),
    ]
    assert selection.changed() is True
    assert selection.changed_paths == {RETURN}
    # Another unit is a change of that path.
    runtime.observations = [
        local(FLOW, 55.0, monotonic=310.0, unitOfMeasure="K"),
        local(RETURN, 31.6, monotonic=310.0),
    ]
    assert selection.changed() is True
    assert selection.changed_paths == {FLOW}


def test_everything_is_looked_at_again_when_more_than_readings_changed():
    runtime = FakeRuntime([local(RETURN, 31.5, monotonic=10.0)])
    selection = LocalSelection(runtime, TARGET)
    selection.changed()
    runtime.snapshot.enabled = False  # the option itself
    assert selection.changed() is True and selection.changed_paths is None
    runtime.observations = [local(RETURN, 31.6, monotonic=70.0)]
    assert selection.changed() is True
    assert selection.changed_paths == {RETURN}


def test_everything_is_looked_at_again_when_the_local_access_changes():
    runtime = FakeRuntime(
        [local(RETURN, 31.5, monotonic=10.0)], enabled=False, binding_enabled=True
    )
    selection = LocalSelection(runtime, TARGET)
    selection.changed()
    runtime.snapshot = FakeSnapshot(enabled=False, binding_enabled=True)
    runtime.observations = [local(RETURN, 31.6, monotonic=70.0)]
    assert selection.changed() is True and selection.changed_paths == {RETURN}
    # The option stays off; the local access goes away and comes back.
    runtime.snapshot = FakeSnapshot(enabled=False, binding_enabled=False)
    assert selection.changed() is True and selection.changed_paths is None
    runtime.snapshot = FakeSnapshot(enabled=False, binding_enabled=True)
    assert selection.changed() is True and selection.changed_paths is None


def test_everything_is_looked_at_again_after_a_confirmed_write():
    runtime = FakeRuntime(
        [local(RETURN, 31.5, monotonic=10.0), local(FLOW, 55.0, monotonic=10.0)]
    )
    selection = LocalSelection(runtime, TARGET)
    selection.changed()
    written = NOW + timedelta(seconds=30)
    confirmed = replace(cloud(FLOW, 50.0, writable=True), last_success=written)
    selection.note_confirmed_write(FLOW, confirmed)
    assert selection.changed() is True
    assert selection.changed_paths is None


# ------------------------------------------------------------ entity gating


def _resource(path, value=21.0):
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )


def _written(sensor):
    sensor.async_write_ha_state = MagicMock()
    return sensor.async_write_ha_state


async def test_a_sensor_only_looks_again_when_its_path_changed(hass):
    coordinator = _coordinator(hass, _resource(RETURN), _resource(FLOW))
    first, second = _sensor_for(coordinator, RETURN), _sensor_for(coordinator, FLOW)
    first_write, second_write = _written(first), _written(second)
    coordinator.local_changed_paths = frozenset({RETURN})
    first._handle_coordinator_update()
    second._handle_coordinator_update()
    assert first_write.call_count == 1 and second_write.call_count == 0
    # Every other pass: both look again.
    coordinator.local_changed_paths = None
    first._handle_coordinator_update()
    second._handle_coordinator_update()
    assert first_write.call_count == 2 and second_write.call_count == 1
    coordinator.local_changed_paths = frozenset()
    second._handle_coordinator_update()
    assert second_write.call_count == 1


async def test_calculated_and_guarded_sensors_always_look_again(hass):
    coordinator = _coordinator(hass, _resource(RETURN), _resource(FLOW))
    sensor = _sensor_for(coordinator, FLOW)
    write = _written(sensor)
    coordinator.local_changed_paths = frozenset({RETURN})
    sensor._handle_coordinator_update()
    assert write.call_count == 0
    description = sensor.entity_description
    for changes in (
        {"value_kind": "dew_point"},
        {"value_kind": "total_electricity"},
        {"value_kind": "switch_program"},
        {"value_kind": "pressure_status"},
        {"secondary_resource_path": RETURN},
        {"resource_path": "/heatSources/systemPressure"},
    ):
        sensor.entity_description = replace(description, **changes)
        sensor._handle_coordinator_update()
    assert write.call_count == 6
    sensor.entity_description = description
    sensor._exchange_guard = object()
    sensor._handle_coordinator_update()
    assert write.call_count == 7


async def test_a_local_only_sensor_only_looks_again_when_its_path_changed(hass):
    from custom_components.bosch_buderus_heating.local_only import definition_for
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        BoschBuderusLocalOnlySensor,
    )

    path = "/heatSources/currentSupplySetpoint"
    other = "/heatSources/returnTemperature"
    coordinator = _coordinator(hass, _resource(RETURN))
    sensor = BoschBuderusLocalOnlySensor(coordinator, path, definition_for(path))
    write = _written(sensor)
    remembered = MagicMock()
    sensor._remember_unit = remembered
    coordinator.local_changed_paths = frozenset({other})
    sensor._handle_coordinator_update()
    assert write.call_count == 0 and remembered.call_count == 0
    coordinator.local_changed_paths = frozenset({path})
    sensor._handle_coordinator_update()
    assert write.call_count == 1 and remembered.call_count == 1
    coordinator.local_changed_paths = None
    sensor._handle_coordinator_update()
    assert write.call_count == 2
    # The local plant view has its own readings: always.
    coordinator.read_only_view = True
    coordinator.local_changed_paths = frozenset({other})
    sensor._handle_coordinator_update()
    assert write.call_count == 3


def test_a_mock_coordinator_never_narrows_a_pass():
    from custom_components.bosch_buderus_heating.update_gate import untouched

    assert not untouched(MagicMock(), ("a",))
    assert not untouched(object(), ("a",))


def _fake_coordinator(selection, faults_changed=False, down=False):
    seen = []
    fake = SimpleNamespace(
        source=SimpleNamespace(current=lambda: True),
        local_retained=False,
        local_selection=selection,
        _local_faults_updated=lambda _selection: faults_changed,
        _local_fault_failure=MagicMock(),
        cloud_unavailable=lambda: down,
        counter_cloud_unavailable=lambda: False,
        _local_pass_context=None,
        local_changed_paths=None,
    )
    fake.async_update_listeners = MagicMock(
        side_effect=lambda: seen.append(fake.local_changed_paths)
    )
    return fake, seen


def test_a_pass_names_the_changed_paths_only_while_it_runs():
    from custom_components.bosch_buderus_heating.managed_pointt import (
        ManagedPointTCoordinator,
    )

    runtime = FakeRuntime([local(RETURN, 31.5, monotonic=10.0)])
    fake, seen = _fake_coordinator(LocalSelection(runtime, TARGET))
    update = ManagedPointTCoordinator._local_updated
    update(fake)  # the first pass: everything
    runtime.observations = [local(RETURN, 31.6, monotonic=70.0)]
    update(fake)
    assert seen == [None, frozenset({RETURN})]
    assert fake.local_changed_paths is None
    # The same answer again is no pass at all.
    runtime.observations = [local(RETURN, 31.6, monotonic=130.0)]
    update(fake)
    assert len(seen) == 2


def test_a_pass_with_a_fault_change_or_a_new_cloud_state_is_not_narrowed():
    from custom_components.bosch_buderus_heating.managed_pointt import (
        ManagedPointTCoordinator,
    )

    runtime = FakeRuntime([local(RETURN, 31.5, monotonic=10.0)])
    fake, seen = _fake_coordinator(LocalSelection(runtime, TARGET))
    update = ManagedPointTCoordinator._local_updated
    update(fake)
    runtime.observations = [local(RETURN, 31.6, monotonic=70.0)]
    fake._local_faults_updated = lambda _selection: True
    update(fake)
    assert seen[-1] is None  # the fault entities look again with everything
    fake._local_faults_updated = lambda _selection: False
    runtime.observations = [local(RETURN, 31.7, monotonic=130.0)]
    fake.cloud_unavailable = lambda: True  # the cloud became unavailable
    update(fake)
    assert seen[-1] is None
    runtime.observations = [local(RETURN, 31.8, monotonic=190.0)]
    update(fake)  # nothing else changed any more
    assert seen[-1] == frozenset({RETURN})
    fake.counter_cloud_unavailable = lambda: True  # a durable outage began
    runtime.observations = [local(RETURN, 31.9, monotonic=250.0)]
    update(fake)
    assert seen[-1] is None


async def test_only_the_views_of_changed_records_are_worked_out_again(running):
    service, clock, _, published = running
    inventory = service.inventory
    signals(inventory, [f"s{n}" for n in range(30)], clock())
    for record in tuple(inventory.records.values()):
        if record.observation is None:
            inventory.accept(success(record.request, clock()), clock())
    service._publish()
    before = published[-1].resources
    sent = len(published)
    worked = counted(service)
    target = list(inventory.records)[7]
    clock.advance(1)
    inventory.accept(
        success(inventory.records[target].request, clock(), value=9), clock()
    )
    service._publish()
    assert len(worked) == 1 and len(published) == sent + 1
    after = published[-1].resources
    assert len(after) == len(before)
    # Only that view is new, at the same place.
    assert [
        i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a is not b
    ] == [7]
    assert after[7].observation.data.value == 9
    # Nothing more to work out afterwards.
    service._publish()
    assert len(worked) == 1
    # Two changes before one publication: exactly these two.
    for number in (3, 11):
        clock.advance(1)
        request = list(inventory.records.values())[number].request
        inventory.accept(success(request, clock(), value=number), clock())
    service._publish()
    assert len(worked) == 3
    assert [
        v.observation.data.value
        for v in published[-1].resources
        if hasattr(v.observation.data, "value") and v.observation.data.value in (3, 11)
    ] == [3, 11]


async def test_a_new_signal_list_works_out_every_view_again(running):
    service, clock, _, published = running
    inventory = service.inventory
    names = [f"s{n}" for n in range(10)]
    signals(inventory, names, clock())
    for record in tuple(inventory.records.values()):
        if record.observation is None:
            inventory.accept(success(record.request, clock()), clock())
    service._publish()
    worked = counted(service)
    sent = len(published)
    for _ in range(2):  # a signal the gateway no longer lists is dropped
        signals(inventory, names[1:], clock())
    service._publish()
    assert worked  # the dropped signal; the others stay as published
    assert not inventory.touched_all and not inventory.touched
    assert len(published) == sent + 1
    gone = [v for v in published[-1].resources if v.request.path == "/signals/s0"]
    assert gone and not gone[0].available


async def test_a_swapped_record_with_the_same_count_is_found_by_the_full_check(
    running,
):
    service, clock, _, published = running
    inventory = service.inventory
    signals(inventory, ["a", "b"], clock())
    service._publish()
    key = next(
        k for k, r in inventory.records.items() if r.request.path == "/signals/b"
    )
    inventory._drop_observation(inventory.records[key])  # as ``prune`` does
    del inventory.records[key]
    new = inventory.add(request_for_path("/gateway/brand"), clock())
    assert len(inventory.records) == len(service._shown)
    sent = len(published)
    service._publish()
    shown = {v.request.path for v in published[-1].resources}
    assert "/gateway/brand" in shown and "/signals/b" not in shown
    assert len(published) == sent + 1 and new.request.path in shown
    assert not inventory.touched


async def test_a_new_reading_after_an_expired_one_ages_again(running):
    service, clock, _, published = running
    await service.budget.pause(7200)  # no read starts; the age alone moves
    await advance(service, clock, 200)
    assert published[-1].resources[0].freshness is Freshness.EXPIRED
    assert service._next_change == float("inf")
    clock.advance(1)
    service.inventory.accept(success(REQUEST, clock(), value=30), clock())
    service._publish()
    assert published[-1].resources[0].freshness is Freshness.FRESH
    assert service._next_change < float("inf")
    await advance(service, clock, 70)
    assert published[-1].resources[0].freshness is Freshness.LATE
