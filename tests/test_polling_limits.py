"""P41: partial answers, error classes and polling budgets."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.coordinator import CYCLE_BUDGET_SECONDS
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    RequestTimeout,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from tests.test_coordinator import _coordinator, _resource, _snapshot, _success
from tests.test_local_polling_service import setup  # noqa: F401 (fixture)

COORDINATOR = "custom_components.bosch_buderus_heating.coordinator"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _plant(hass, count):
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    paths = tuple(f"/heatSources/value{index}" for index in range(count))
    coordinator.resources = {path: _resource(path, 1.0) for path in paths}
    coordinator.data = {
        path: _snapshot(resource) for path, resource in coordinator.resources.items()
    }
    coordinator._paths_by_group = {PollGroup.FAST: paths}
    coordinator._next_update = {PollGroup.FAST: 0.0}
    return client, coordinator, paths


# ------------------------------------------------------------- cycle budget


async def test_a_slow_cycle_stops_new_requests_and_keeps_the_values(
    hass: HomeAssistant,
) -> None:
    client, coordinator, paths = _plant(hass, 61)
    clock = Clock()

    async def slow(gateway, requested):
        clock.now += CYCLE_BUDGET_SECONDS  # the first chunk used the budget
        return tuple(_success(path, 2.0) for path in requested)

    client.get_resources_bulk.side_effect = slow
    with patch(f"{COORDINATOR}.monotonic", clock):
        result = await coordinator._async_update_data()
    assert client.get_resources_bulk.await_count == 1
    assert result[paths[0]].resource.value == 2.0
    # Not requested, but not hidden either: the value keeps its validity.
    skipped = result[paths[-1]]
    assert skipped.available and skipped.resource.value == 1.0
    assert skipped.last_error_category != "poll_deferred"
    assert coordinator.diagnostics_summary()["cycles_over_budget"] == 1
    # The next cycle starts with what was skipped.
    assert coordinator._deferred_poll_paths[0] == paths[30]


async def test_one_chunk_is_always_read_even_after_a_slow_start(
    hass: HomeAssistant,
) -> None:
    client, coordinator, paths = _plant(hass, 5)
    client.get_resources_bulk.return_value = tuple(_success(p, 3.0) for p in paths)
    clock = Clock()
    with patch(f"{COORDINATOR}.monotonic", clock):
        clock.now += 10 * CYCLE_BUDGET_SECONDS
        result = await coordinator._async_update_data()
    assert result[paths[0]].resource.value == 3.0


# ----------------------------------------------------------------- fallback


async def test_single_reads_stop_at_a_clear_connection_problem(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    paths = (
        "/heatSources/actualSupplyTemperature",
        "/heatSources/returnTemperature",
        "/heatSources/actualModulation",
    )
    client.get_resource.side_effect = (RequestTimeout(), _resource(paths[1], 1.0))
    await coordinator._async_single_get_fallback(paths)
    assert client.get_resource.await_count == 1


async def test_single_reads_continue_after_a_server_error(hass: HomeAssistant) -> None:
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    paths = ("/heatSources/actualSupplyTemperature", "/heatSources/returnTemperature")
    client.get_resource.side_effect = (
        ServiceUnavailable(503),
        _resource(paths[1], 1.0),
    )
    results = await coordinator._async_single_get_fallback(paths)
    assert client.get_resource.await_count == 2
    assert [item.path for item in results] == [paths[1]]


async def test_single_reads_stop_at_a_network_failure(hass: HomeAssistant) -> None:
    """The transport maps connection errors to ServiceUnavailable without status."""
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    paths = ("/heatSources/actualSupplyTemperature", "/heatSources/returnTemperature")
    client.get_resource.side_effect = (ServiceUnavailable(), _resource(paths[1], 1.0))
    await coordinator._async_single_get_fallback(paths)
    assert client.get_resource.await_count == 1


async def test_a_group_split_by_the_budget_completes_over_cycles(
    hass: HomeAssistant,
) -> None:
    client, coordinator, paths = _plant(hass, 75)  # three chunks
    clock = Clock()
    requested: list[tuple[str, ...]] = []

    async def slow(gateway, chunk):
        requested.append(tuple(chunk))
        clock.now += CYCLE_BUDGET_SECONDS
        return tuple(_success(path, 2.0) for path in chunk)

    client.get_resources_bulk.side_effect = slow
    with patch(f"{COORDINATOR}.monotonic", clock):
        for _ in range(3):
            await coordinator._async_update_data()
            clock.now += 60
    # Every path read exactly once, then the group waits its interval.
    read = [path for chunk in requested for path in chunk]
    assert sorted(read) == sorted(paths)
    assert coordinator._next_update[PollGroup.FAST] > clock.now - 60
    assert not coordinator._budget_progress


# ---------------------------------------------------- firmware rediscovery


async def test_a_firmware_rediscovery_keeps_parts_and_clears_pauses(
    hass: HomeAssistant,
) -> None:
    from tests.test_discovery_recovery import Cloud, _inventory
    from tests.test_discovery_recovery import _coordinator as cloud_coordinator
    from tests.test_firmware_watch import FIRMWARE, MODULATION, plant, version

    clock = Clock()
    with (
        patch(f"{COORDINATOR}.monotonic", clock),
        patch(f"{COORDINATOR}.fault_resource_candidates", return_value=()),
    ):
        cloud = Cloud(plant())
        coordinator = cloud_coordinator(hass, cloud, _inventory(hass))
        await coordinator.async_refresh()
        assert MODULATION in coordinator.resources
        coordinator._negative_until["/heatSources/notThereBefore"] = clock.now + 9e4
        # After the update the gateway briefly does not report a part.
        cloud.tree[FIRMWARE] = version("5.31")
        clock.now += 15 * 60
        await coordinator.async_refresh()
        del cloud.tree["/heatSources"]
        clock.now += 60
        await coordinator.async_refresh()  # the discovery after the change
        assert coordinator.firmware.result == "success"
        assert MODULATION in coordinator.resources
        assert MODULATION in coordinator.data
        assert not coordinator._negative_until
        assert MODULATION not in coordinator.discovery_recovery.missing
        # Really gone: its own 404 ends the kept reading.
        cloud.tree["/heatSources"] = plant()["/heatSources"]
        cloud.down[MODULATION] = 404
        clock.now += 60
        coordinator._next_update = dict.fromkeys(coordinator._poll_intervals, 0)
        await coordinator.async_refresh()
        assert MODULATION not in coordinator.resources


# ------------------------------------------------------- P46 search errors


async def test_a_search_without_an_answer_uses_no_attempt(hass: HomeAssistant) -> None:
    from tests.test_discovery_recovery import (
        GATEWAY,
        Cloud,
        _inventory,
        _known_before,
        tree,
    )
    from tests.test_discovery_recovery import Clock as RecoveryClock
    from tests.test_discovery_recovery import (
        _coordinator as cloud_coordinator,
    )

    clock = RecoveryClock()
    with (
        patch(f"{COORDINATOR}.monotonic", clock),
        patch(f"{COORDINATOR}.fault_resource_candidates", return_value=()),
    ):
        inventory = _inventory(hass)
        await _known_before(hass, inventory)
        cloud = Cloud(tree())
        cloud.down["/system"] = 503  # missing after this start
        coordinator = cloud_coordinator(hass, cloud, inventory)
        await coordinator.async_refresh()
        recovery = coordinator.discovery_recovery
        assert recovery.missing
        del cloud.down["/system"]
        cloud.request_errors["/system"] = RequestTimeout()
        clock.now += 5 * 60
        await coordinator.async_refresh()
        assert recovery.attempts == 0 and recovery.postponed == 1
        assert not recovery.due(clock.now + 299)  # waits the minimum gap
        del cloud.request_errors["/system"]
        clock.now += 300
        await coordinator.async_refresh()
        assert not recovery.missing
        assert GATEWAY


# --------------------------------------------------------------- local side


def test_the_record_limit_ends_the_search_until_the_next_rescan(monkeypatch):
    from custom_components.bosch_buderus_heating.local_polling import (
        planner as planner_module,
    )
    from custom_components.bosch_buderus_heating.local_polling.inventory import (
        ResourceInventory,
    )
    from custom_components.bosch_buderus_heating.local_polling.planner import (
        PollPlanner,
    )
    from tests.test_local_probe import CONTEXT

    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    monkeypatch.setattr(planner_module, "MAX_RECORDS", 0)
    assert planner._discovery(10.0) is None
    assert planner.exhausted
    assert planner.discovery_problem == "local_resource_limit"
    assert planner.next_wake(10.0) > 10.25  # no busy loop at the limit
    # The state survives a restart (it used to break the stored schedule).
    from custom_components.bosch_buderus_heating.local_polling import (
        storage as storage_module,
    )
    from custom_components.bosch_buderus_heating.local_polling.storage import (
        checkpoint,
        restore,
    )

    monkeypatch.setattr(storage_module, "MAX_RECORDS", 0)
    saved = checkpoint(planner, 10.0)
    fresh = PollPlanner(ResourceInventory(CONTEXT.identity), 20.0)
    restore(fresh, saved, 20.0)
    assert fresh.exhausted


def test_confirmed_signals_no_longer_listed_are_pruned():
    from custom_components.bosch_buderus_heating.local_api.catalog import (
        request_for_path,
    )
    from custom_components.bosch_buderus_heating.local_polling.inventory import (
        ResourceInventory,
    )
    from tests.test_local_polling import CONTEXT, signals, success

    inventory = ResourceInventory(CONTEXT.identity)
    signals(inventory, ["old"], 0)
    old = request_for_path("/signals/old", signal_ids=("old",))
    assert inventory.accept(success(old, 1), 1)
    assert inventory.records[old.target].confirmed
    signals(inventory, ["new"], 100000)
    signals(inventory, ["new"], 100001)
    assert not inventory.records[old.target].active
    inventory.prune(100002)  # a day after creation, but just inactive
    assert old.target in inventory.records
    inventory.prune(100001 + 86400)
    assert old.target not in inventory.records


def test_an_empty_signal_list_after_a_restart_is_no_removal():
    from custom_components.bosch_buderus_heating.local_api.catalog import (
        request_for_path,
    )
    from custom_components.bosch_buderus_heating.local_polling.inventory import (
        ResourceInventory,
        ResourceRecord,
    )
    from tests.test_local_polling import CONTEXT, signals

    inventory = ResourceInventory(CONTEXT.identity)
    # As restored: signal records exist, but no directory was read yet.
    kept = request_for_path("/signals/kept", signal_ids=("kept",))
    inventory.records[kept.target] = ResourceRecord(kept, 0, 0, confirmed=True)
    assert not inventory.signal_ids
    signals(inventory, [], 1)
    assert inventory.signal_problem == "empty_local_signal_list"
    record = inventory.records[
        request_for_path("/signals/kept", signal_ids=("kept",)).target
    ]
    assert record.signal_misses == 0


async def test_a_working_read_resets_the_service_backoff(setup) -> None:  # noqa: F811
    from custom_components.bosch_buderus_heating.local_read import (
        ReadProblem,
        ReadResult,
        ReadState,
    )
    from tests.test_local_polling_service import REQUEST, settle
    from tests.test_local_probe import CONTEXT

    service, clock, client, _ = setup
    failure = ReadResult(
        REQUEST, ReadState.ACCESS_ERROR, problem=ReadProblem.SERVICE_UNAVAILABLE
    )
    for _ in range(3):
        await service._access_error(failure)
        clock.advance(600)
    service.confirm_identity(CONTEXT.identity)
    service._failures = 3  # as after three errors in a running service
    await service.start()
    await settle()
    assert client.calls and service._failures == 0


# ----------------------------------------------------------- recorder load


@pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
async def test_an_unchanged_answer_writes_no_new_state(hass) -> None:
    from custom_components.bosch_buderus_heating.const import DOMAIN
    from tests.test_local_backed import SENSOR, ReadingBackend, native
    from tests.test_managed_pointt import eventually, running, source_for
    from tests.test_source_isolation import paired_target

    hass.config.components.add(DOMAIN)
    backend = ReadingBackend()
    async with running(hass, backend, configure=paired_target) as (_, _, owner, _):
        await owner.start_platforms()
        await eventually(lambda: native(owner, "sensor", SENSOR))
        entity_id = native(owner, "sensor", SENSOR).entity_id
        coordinator = source_for(owner, "000123")
        events = []
        remove = hass.bus.async_listen(
            "state_changed",
            lambda event: (
                events.append(event) if event.data["entity_id"] == entity_id else None
            ),
        )
        try:
            for _ in range(2):
                coordinator._next_update = dict.fromkeys(coordinator._poll_intervals, 0)
                await coordinator.async_refresh()
                await hass.async_block_till_done()
            assert (
                coordinator.data[
                    "/heatingCircuits/hc2/currentRoomSetpoint"
                ].last_success
                is not None
            )
            assert events == []  # same value, same attributes: no recorder write
            backend.values["000123"] = 21.5
            coordinator._next_update = dict.fromkeys(coordinator._poll_intervals, 0)
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            assert len(events) == 1
        finally:
            remove()


def test_new_access_settings_forget_learned_pauses() -> None:
    from custom_components.bosch_buderus_heating.local_polling.inventory import (
        ResourceInventory,
    )
    from custom_components.bosch_buderus_heating.local_polling.planner import (
        PollPlanner,
    )
    from custom_components.bosch_buderus_heating.local_polling.storage import (
        checkpoint,
        restore,
    )
    from tests.test_local_polling import REQUEST
    from tests.test_local_probe import CONTEXT

    def saved_with(key):
        planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
        planner.access_key = key
        record = planner.inventory.add(REQUEST, 0)
        record.failures, record.due = 3, 86000
        return checkpoint(planner, 0)

    for stored, current, reset in (
        ("1:4", "1:4", False),
        ("1:4", "2:4", True),  # new sign-in
        ("1:4", "1:5", True),  # changed settings
        (None, "1:5", False),  # saved before P41: nothing to compare
    ):
        planner = PollPlanner(ResourceInventory(CONTEXT.identity), 100)
        planner.access_key = current
        restore(planner, saved_with(stored), 100)
        record = planner.inventory.records[REQUEST.target]
        assert (record.failures == 0) is reset, (stored, current)


async def test_a_firmware_change_while_stopped_is_noticed(
    setup,  # noqa: F811
) -> None:
    from types import MappingProxyType

    from custom_components.bosch_buderus_heating.local_api.catalog import (
        request_for_path,
    )
    from tests.test_local_polling import success
    from tests.test_local_polling_service import settle
    from tests.test_local_probe import CONTEXT

    service, clock, client, _ = setup
    request = request_for_path("/gateway/versionFirmware")

    async def read(req):
        client.calls.append(req)
        return success(
            req,
            clock(),
            value="15.01.00",
            document=MappingProxyType({"id": req.path, "value": "15.01.00"}),
        )

    client.read = read
    service.planner.restored_firmware = "15.00.01"  # seen before the restart
    reconsidered = []
    original = service.planner.reconsider
    service.planner.reconsider = lambda now: (reconsidered.append(now), original(now))
    from tests.test_local_polling import no_discovery

    no_discovery(service.planner)
    service.planner.hints((request,), clock())
    service.confirm_identity(CONTEXT.identity)
    await service.start()
    await settle()
    assert client.calls and reconsidered
    assert service.planner.firmware == "15.01.00"
    assert service.planner.restored_firmware is None
