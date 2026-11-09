"""P46: parts missing at start are found again without a restart (#104).

The tests run real coordinator cycles (discovery, normal polls and the search)
against a small synthetic cloud whose single items can fail. Time is moved with
a fake monotonic clock; final states are never set by hand.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.discovery_inventory import (
    MAX_AGE,
    RECOVERY_SCHEDULE,
    DiscoveryInventory,
    DiscoveryRecovery,
    async_remove_discovery_inventory,
    discovery_inventory,
    is_transient_result,
    recovery_roots,
    resource_from_data,
    resource_to_data,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    BatchItemResult,
    Gateway,
    InvalidBatchEnvelope,
    InvalidPayload,
    PointTError,
    RateLimited,
    RequestTimeout,
    Resource,
    ResourceError,
    ResourceMetadata,
    ResourceReference,
    ServiceUnavailable,
)
from tests.ha_test_helpers import MockConfigEntry

GATEWAY = "synthetic-gateway-4711"
OUTDOOR = "/system/sensors/temperatures/outdoor_t1"
MODULATION = "/heatSources/actualModulation"
COORDINATOR = "custom_components.bosch_buderus_heating.coordinator"


def container(path: str, *children: str) -> Resource:
    return Resource(
        path,
        metadata=ResourceMetadata(resource_type="refEnum"),
        references=tuple(ResourceReference(child) for child in children),
    )


def leaf(path: str, value: float, unit: str = "C") -> Resource:
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit=unit),
    )


def tree() -> dict[str, Resource]:
    return {
        "/system": container("/system", "/system/sensors"),
        "/system/sensors": container("/system/sensors", "/system/sensors/temperatures"),
        "/system/sensors/temperatures": container(
            "/system/sensors/temperatures", OUTDOOR
        ),
        OUTDOOR: leaf(OUTDOOR, 7.5),
        "/heatSources": container("/heatSources", MODULATION),
        MODULATION: leaf(MODULATION, 40.0, "%"),
    }


class Cloud:
    """Answers bulk reads from a tree; single items can fail on purpose."""

    def __init__(self, resources: dict[str, Resource]) -> None:
        self.tree = dict(resources)
        self.down: dict[str, int | PointTError] = {}
        self.bulk_error: PointTError | None = None
        # Requests containing one of these paths fail with this error.
        self.request_errors: dict[str, PointTError] = {}
        # Every request for one of these gateways fails with this error.
        self.gateway_errors: dict[str, PointTError] = {}
        # A single read of one of these paths fails with this error, such as
        # ServiceUnavailable(504) as the transport raises it for HTTP 504.
        self.get_errors: dict[str, PointTError] = {}
        self.requests: list[tuple[str, ...]] = []

    def requested(self, path: str) -> int:
        return sum(path in paths for paths in self.requests)

    async def get_resources_bulk(self, gateway_id, paths, **_kwargs):
        self.requests.append(tuple(paths))
        if self.bulk_error is not None:
            raise self.bulk_error
        if gateway_id in self.gateway_errors:
            raise self.gateway_errors[gateway_id]
        for path in paths:
            if path in self.request_errors:
                raise self.request_errors[path]
        results = []
        for path in paths:
            failure = self.down.get(path)
            if isinstance(failure, int):
                error = (
                    RateLimited(retry_after=None)
                    if failure == 429
                    else ResourceError(path, failure)
                )
                results.append(BatchItemResult(gateway_id, path, failure, error=error))
            elif failure is not None:
                results.append(BatchItemResult(gateway_id, path, None, error=failure))
            elif path in self.tree:
                results.append(
                    BatchItemResult(gateway_id, path, 200, resource=self.tree[path])
                )
            else:
                results.append(
                    BatchItemResult(
                        gateway_id, path, 404, error=ResourceError(path, 404)
                    )
                )
        return tuple(results)

    async def get_resource(self, gateway_id, path, **_kwargs):
        if self.bulk_error is not None:
            raise self.bulk_error
        if gateway_id in self.gateway_errors:
            raise self.gateway_errors[gateway_id]
        if path in self.get_errors:
            raise self.get_errors[path]
        failure = self.down.get(path)
        if isinstance(failure, int):
            raise ResourceError(path, failure)
        if failure is not None:
            raise failure
        if path in self.tree:
            return self.tree[path]
        raise ResourceError(path, 404)


class Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    clock = Clock()
    with (
        patch(f"{COORDINATOR}.monotonic", clock),
        patch(f"{COORDINATOR}.fault_resource_candidates", return_value=()),
    ):
        yield clock


def _inventory(hass: HomeAssistant, saves: list | None = None) -> DiscoveryInventory:
    inventory = DiscoveryInventory(hass, "entry")
    inventory._loaded = True
    inventory.store.async_delay_save = lambda data, delay: (
        saves.append(data()) if saves is not None else None
    )
    return inventory


def _coordinator(hass: HomeAssistant, cloud: Cloud, inventory=None, entry=None):
    if entry is None:
        entry = MockConfigEntry(domain=DOMAIN, version=3)
        entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, cloud, Gateway(GATEWAY, model="MX400"), entry
    )
    coordinator.inventory = inventory
    return coordinator


async def _known_before(hass, inventory, entry=None) -> None:
    """A previous start discovered the full tree."""
    first = _coordinator(hass, Cloud(tree()), inventory, entry)
    await first.async_refresh()
    assert first.last_update_success
    assert OUTDOOR in first.resources


# ------------------------------------------------------------------- search


async def test_missing_subtree_is_found_again_without_restart(
    hass: HomeAssistant, clock: Clock, caplog
) -> None:
    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.down["/system"] = 404  # the start-up answer lacks the whole subtree
    coordinator = _coordinator(hass, cloud, inventory)

    with caplog.at_level(logging.INFO):
        await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert OUTDOOR not in coordinator.resources
    report = coordinator.discovery_recovery.diagnostics(clock.now)
    assert report["state"] == "pending"
    assert report["missing"] == 4  # /system and the three parts below it
    assert report["missing_templates"][0] == "/system"
    assert report["missing_reasons"] == {"http_404": 1, "not_referenced": 3}
    assert report["next_attempt_in_seconds"] == RECOVERY_SCHEDULE[0]
    assert "4 previously known PointT resources are missing" in caplog.text
    assert GATEWAY not in caplog.text

    # Not yet due: an ordinary cycle does not search.
    del cloud.down["/system"]
    before = cloud.requested("/system")
    clock.now += RECOVERY_SCHEDULE[0] - 1
    await coordinator.async_refresh()
    assert cloud.requested("/system") == before

    clock.now += 1
    with caplog.at_level(logging.INFO):
        await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert OUTDOOR in coordinator.resources
    snapshot = coordinator.data[OUTDOOR]
    assert snapshot.available and snapshot.resource.value == 7.5
    report = coordinator.discovery_recovery.diagnostics(clock.now)
    assert report["state"] == "recovered"
    assert report["recovered"] == 4 and report["attempts"] == 1
    assert report["next_attempt_in_seconds"] is None
    assert "All 4 missing PointT resources were found again" in caplog.text
    # The recovered resource is polled normally from now on.
    assert any(OUTDOOR in group for group in coordinator._paths_by_group.values())
    # And no further search runs.
    searched = cloud.requested("/system")
    clock.now += 2 * 86400
    await coordinator.async_refresh()
    assert cloud.requested("/system") == searched


async def test_found_start_up_values_keep_their_once_only_reading(
    hass: HomeAssistant, clock: Clock
) -> None:
    """A start-up value below a recovered subtree is read by the start-up
    reader, not polled as an ordinary resource."""
    date_time = "/system/dateTime"
    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.tree["/system"] = container("/system", "/system/sensors", date_time)
    cloud.tree[date_time] = Resource(
        date_time,
        value="2026-11-05T10:00:00",
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue"),
    )
    cloud.down["/system"] = 404
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()
    assert not coordinator.startup_data.tracks(date_time)

    del cloud.down["/system"]
    clock.now += RECOVERY_SCHEDULE[0]
    await coordinator.async_refresh()
    assert coordinator.discovery_recovery.state == "recovered"
    assert date_time not in coordinator.resources
    assert coordinator.startup_data.tracks(date_time)
    assert date_time not in inventory.resources(GATEWAY)
    # Queued for the start-up reader within the shared budget, not read here.
    assert cloud.requested(date_time) == 0
    assert date_time not in coordinator.startup_data.requested


async def test_container_without_its_references_is_searched_below_it(
    hass: HomeAssistant, clock: Clock
) -> None:
    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.tree["/system/sensors/temperatures"] = container(
        "/system/sensors/temperatures"  # answers, but without the sensor
    )
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()
    assert coordinator.discovery_recovery.missing == {OUTDOOR: "not_referenced"}
    assert coordinator.discovery_recovery.roots() == (OUTDOOR,)

    clock.now += RECOVERY_SCHEDULE[0]
    await coordinator.async_refresh()
    assert coordinator.discovery_recovery.state == "recovered"
    assert coordinator.data[OUTDOOR].available


async def test_permanently_missing_part_gives_up_after_the_schedule(
    hass: HomeAssistant, clock: Clock, caplog
) -> None:
    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.down[OUTDOOR] = 404
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()
    start = clock.now
    attempts = []
    for delay in RECOVERY_SCHEDULE:
        clock.now = start + delay
        before = cloud.requested(OUTDOOR)
        await coordinator.async_refresh()
        attempts.append(cloud.requested(OUTDOOR) - before)
        assert coordinator.last_update_success  # the normal poll goes on
    assert attempts == [1] * len(RECOVERY_SCHEDULE)
    report = coordinator.discovery_recovery.diagnostics(clock.now)
    assert report["state"] == "given_up"
    assert report["attempts"] == len(RECOVERY_SCHEDULE)
    assert report["missing_reasons"] == {"http_404": 1}
    assert "still missing after 7 searches" in caplog.text
    # No further requests for it, and the next start no longer expects it.
    clock.now += 3 * 86400
    before = cloud.requested(OUTDOOR)
    await coordinator.async_refresh()
    assert cloud.requested(OUTDOOR) == before
    assert OUTDOOR not in inventory.resources(GATEWAY)
    assert MODULATION in inventory.resources(GATEWAY)
    caplog.clear()
    restarted = _coordinator(hass, cloud, inventory)
    await restarted.async_refresh()
    assert restarted.discovery_recovery.state == "complete"
    assert "missing" not in caplog.text


async def test_rate_limit_postpones_the_search_without_using_an_attempt(
    hass: HomeAssistant, clock: Clock
) -> None:
    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.down["/system"] = 404
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()

    cloud.down["/system"] = 429
    clock.now += RECOVERY_SCHEDULE[0]
    await coordinator.async_refresh()
    # The poll of this cycle succeeded; the brake starts with the next one.
    assert coordinator.last_update_success
    assert coordinator._cloud_backoff_until > clock.now
    recovery = coordinator.discovery_recovery
    assert recovery.attempts == 0 and recovery.postponed == 1
    assert recovery.state == "pending"

    # During the backoff nothing is sent at all.
    del cloud.down["/system"]
    sent = len(cloud.requests)
    clock.now += 10
    await coordinator.async_refresh()
    assert len(cloud.requests) == sent

    assert not coordinator.last_update_success

    clock.now = coordinator._cloud_backoff_until + 1
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert recovery.state == "recovered" and recovery.attempts == 1


async def test_outage_does_not_use_up_attempts(
    hass: HomeAssistant, clock: Clock
) -> None:
    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.down["/system"] = 404
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()
    del cloud.down["/system"]
    cloud.bulk_error = RequestTimeout()  # the cloud is down when the slot comes
    clock.now += RECOVERY_SCHEDULE[0]
    sent = len(cloud.requests)
    await coordinator.async_refresh()
    clock.now += 60
    await coordinator.async_refresh()
    recovery = coordinator.discovery_recovery
    assert not coordinator.last_update_success
    assert recovery.attempts == 0 and recovery.state == "pending"
    assert not any("/system" in paths for paths in cloud.requests[sent:])
    cloud.bulk_error = None
    clock.now += 60
    await coordinator.async_refresh()  # working again: poll, then search
    assert coordinator.last_update_success
    assert recovery.state == "recovered" and recovery.attempts == 1


async def test_one_path_answering_5xx_does_not_stop_the_discovery(
    hass: HomeAssistant, clock: Clock
) -> None:
    """B07: HTTP 504 in the bulk item and in its single read stays local."""
    cloud = Cloud(tree())
    cloud.down[OUTDOOR] = 504
    cloud.get_errors[OUTDOOR] = ServiceUnavailable(504)
    coordinator = _coordinator(hass, cloud)

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert MODULATION in coordinator.resources
    assert coordinator.data[MODULATION].available
    assert OUTDOOR not in coordinator.resources
    report = coordinator.discovery_diagnostics
    assert report.completed and report.stop_reason == "complete"
    assert report.paths[OUTDOOR].fallback_result == "http_504"


async def test_search_meeting_a_path_5xx_uses_its_attempts(
    hass: HomeAssistant, clock: Clock, caplog
) -> None:
    """A path answering 5xx is an answer: the search keeps to its schedule."""
    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.down[OUTDOOR] = 504
    cloud.get_errors[OUTDOOR] = ServiceUnavailable(504)
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    recovery = coordinator.discovery_recovery
    assert recovery.missing == {OUTDOOR: "http_504"}

    start = clock.now
    searches = []
    for delay in (*RECOVERY_SCHEDULE, RECOVERY_SCHEDULE[-1] + 3600):
        clock.now = start + delay
        before = cloud.requested(OUTDOOR)
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        searches.append(cloud.requested(OUTDOOR) - before)

    assert searches == [1] * len(RECOVERY_SCHEDULE) + [0]
    assert recovery.state == "given_up"
    assert recovery.attempts == len(RECOVERY_SCHEDULE)
    assert recovery.postponed == 0
    assert "still missing after 7 searches" in caplog.text


async def test_full_resource_budget_ends_the_search_at_once(
    hass: HomeAssistant, clock: Clock, caplog
) -> None:
    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.down["/system"] = 404
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()
    clock.now += RECOVERY_SCHEDULE[0]
    sent = cloud.requested("/system")
    with patch(f"{COORDINATOR}.MAX_DISCOVERY_RESOURCES", len(coordinator.resources)):
        await coordinator.async_refresh()
    recovery = coordinator.discovery_recovery
    assert recovery.state == "given_up" and recovery.last_result == "resource_limit"
    assert cloud.requested("/system") == sent
    assert "cannot be searched (resource_limit)" in caplog.text
    assert "/system" not in inventory.resources(GATEWAY)


async def test_failing_search_is_counted_and_never_blocks_the_poll(
    hass: HomeAssistant, clock: Clock
) -> None:
    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.down["/system"] = RequestTimeout()
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()
    assert coordinator.discovery_recovery.missing["/system"] == "timeout"

    clock.now += RECOVERY_SCHEDULE[0]
    cloud.down["/system"] = InvalidPayload()
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data[MODULATION].available
    recovery = coordinator.discovery_recovery
    assert recovery.attempts == 1 and recovery.state == "pending"
    assert recovery.diagnostics(clock.now)["next_attempt_in_seconds"] == (
        RECOVERY_SCHEDULE[1] - RECOVERY_SCHEDULE[0]
    )
    # A late attempt (here hours after its slot) keeps a minimum gap to the
    # next one instead of searching again in the very next cycle.
    clock.now += RECOVERY_SCHEDULE[3]
    await coordinator.async_refresh()
    assert recovery.attempts == 2
    assert recovery.diagnostics(clock.now)["next_attempt_in_seconds"] == 300


async def test_authentication_failure_during_the_search_asks_for_sign_in(
    hass: HomeAssistant, clock: Clock
) -> None:
    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.down["/system"] = 404
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()
    clock.now += RECOVERY_SCHEDULE[0]
    # Only the search meets the rejected sign-in; the normal poll would not.
    cloud.request_errors["/system"] = AuthenticationError()
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    assert coordinator.discovery_recovery.attempts == 0


def _individual_reads(cloud: Cloud) -> AsyncMock:
    """Count the individual GETs of the search without changing their answers."""
    reads = AsyncMock(side_effect=cloud.get_resource)
    cloud.get_resource = reads
    return reads


@pytest.mark.parametrize(
    ("answer", "individual_reads"),
    [
        # A definite answer: never read individually (ADR 0003).
        (406, 0),
        # An unreadable item: one individual read, which is unreadable too.
        (InvalidPayload("unreadable"), 1),
    ],
    ids=["http_406", "malformed_item"],
)
async def test_unusable_answer_during_the_search_uses_an_attempt(
    hass: HomeAssistant, clock: Clock, caplog, answer, individual_reads
) -> None:
    """406 and an unreadable item are answers, not an outage.

    Each slot of the schedule uses one attempt with exactly one batch for the
    missing subtree; nothing is postponed or braked, the series ends after the
    last slot and the registry entry of the missing part stays.
    """
    from homeassistant.helpers import entity_registry as er

    inventory = _inventory(hass)
    entry = MockConfigEntry(domain=DOMAIN, version=3)
    entry.add_to_hass(hass)
    await _known_before(hass, inventory, entry)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{GATEWAY}:system:sensors:temperatures:outdoor_t1",
        config_entry=entry,
    )
    registered = {
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    cloud = Cloud(tree())
    cloud.down["/system"] = 404
    coordinator = _coordinator(hass, cloud, inventory, entry)
    await coordinator.async_refresh()
    recovery = coordinator.discovery_recovery
    assert recovery.state == "pending" and recovery.roots() == ("/system",)
    start = clock.now

    cloud.down["/system"] = answer
    reads = _individual_reads(cloud)
    for attempt, delay in enumerate(RECOVERY_SCHEDULE, start=1):
        searched = cloud.requested("/system")
        read = reads.await_count
        clock.now = start + delay
        await coordinator.async_refresh()
        assert coordinator.last_update_success  # the poll goes on
        assert coordinator.data[MODULATION].available
        assert coordinator._cloud_backoff_until <= clock.now  # no brake
        assert cloud.requested("/system") - searched == 1
        assert reads.await_count - read == individual_reads
        assert recovery.attempts == attempt and recovery.postponed == 0
        assert recovery.last_result == "nothing_found"
        if attempt < len(RECOVERY_SCHEDULE):
            assert recovery.state == "pending"
            assert recovery.diagnostics(clock.now)["next_attempt_in_seconds"] == (
                RECOVERY_SCHEDULE[attempt] - delay
            )
    if individual_reads:
        assert {call.args[1] for call in reads.await_args_list} == {"/system"}
        assert {call.kwargs["fallback_reason"] for call in reads.await_args_list} == {
            "malformed"
        }
    assert recovery.state == "given_up"
    assert f"still missing after {len(RECOVERY_SCHEDULE)} searches" in caplog.text
    # No longer expected at the next start, but the registry keeps it.
    assert OUTDOOR not in inventory.resources(GATEWAY)
    assert MODULATION in inventory.resources(GATEWAY)
    assert {
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    } == registered
    # And the series sends nothing more.
    sent = cloud.requested("/system")
    clock.now += 2 * 86400
    await coordinator.async_refresh()
    assert cloud.requested("/system") == sent


async def test_unreadable_search_envelope_is_read_individually_once(
    hass: HomeAssistant, clock: Clock
) -> None:
    """An unreadable batch body: one individual read per path of that batch."""
    from collections import Counter

    from custom_components.bosch_buderus_heating.discovery import (
        MAX_DISCOVERY_RESOURCES,
    )

    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.down["/system"] = 404
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()
    polled = set(coordinator.resources)
    budget = MAX_DISCOVERY_RESOURCES - len(polled)

    del cloud.down["/system"]
    cloud.request_errors["/system"] = InvalidBatchEnvelope("unreadable")
    reads = _individual_reads(cloud)
    sent = len(cloud.requests)
    clock.now += RECOVERY_SCHEDULE[0]
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert cloud.requested("/system") == 2  # the earlier start, then this batch
    assert [call.args[1] for call in reads.await_args_list] == ["/system"]
    assert reads.await_args.kwargs["fallback_reason"] == "malformed"
    recovery = coordinator.discovery_recovery
    assert recovery.state == "recovered" and recovery.attempts == 1
    assert coordinator.data[OUTDOOR].available
    # Every path is requested once, and the search keeps to its bound.
    paths = Counter(path for batch in cloud.requests[sent:] for path in batch)
    assert max(paths.values()) == 1
    assert "/system" in paths and OUTDOOR in paths
    assert 0 < len(set(paths) - polled) <= budget


async def test_start_without_gaps_sends_nothing_extra(
    hass: HomeAssistant, clock: Clock, caplog
) -> None:
    saves: list = []
    inventory = _inventory(hass, saves)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()
    report = coordinator.discovery_recovery.diagnostics(clock.now)
    assert report["state"] == "complete" and report["missing"] == 0
    assert report["reference_resources"] == len(tree())
    sent = len(cloud.requests)
    for _ in range(3):
        clock.now += RECOVERY_SCHEDULE[-1]
        await coordinator.async_refresh()
    # Only normal polls: every request is for a known resource.
    assert all(
        path in coordinator.resources
        for paths in cloud.requests[sent:]
        for path in paths
    )
    assert "missing" not in caplog.text
    assert saves and set(saves[-1]["gateways"][GATEWAY]["resources"]) == set(tree())


async def test_temporary_root_failure_is_searched_without_an_inventory(
    hass: HomeAssistant, clock: Clock
) -> None:
    cloud = Cloud(tree())
    cloud.down["/heatSources"] = RequestTimeout()
    coordinator = _coordinator(hass, cloud, None)  # e.g. first start after upgrade
    await coordinator.async_refresh()
    assert coordinator.discovery_recovery.missing == {"/heatSources": "timeout"}
    del cloud.down["/heatSources"]
    clock.now += RECOVERY_SCHEDULE[0]
    await coordinator.async_refresh()
    assert coordinator.data[MODULATION].available
    # A definite 404 of a root without inventory is not searched.
    other = Cloud(tree())
    del other.tree["/heatSources"]
    fresh = _coordinator(hass, other, None)
    await fresh.async_refresh()
    assert fresh.discovery_recovery.state == "none"


async def test_temporary_root_failure_with_an_inventory_is_not_expected(
    hass: HomeAssistant, clock: Clock, caplog
) -> None:
    """A root that always fails and was never discovered is no gap."""
    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.down["/pv"] = RequestTimeout()
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()
    assert coordinator.discovery_recovery.state == "complete"
    assert "missing" not in caplog.text


async def test_search_on_a_water_softener_keeps_to_the_missing_subtree(
    hass: HomeAssistant,
) -> None:
    from custom_components.bosch_buderus_heating.discovery import (
        async_discover_resources,
    )
    from custom_components.bosch_buderus_heating.startup_data import (
        StartupGatewayData,
    )

    missing = "/swCircuits/sw1/x"
    cloud = Cloud({missing: leaf(missing, 1.0, "%")})
    startup = StartupGatewayData()
    startup.record_main_cycle(successful=True)
    found = await async_discover_resources(
        cloud,
        GATEWAY,
        roots=(missing,),
        startup_data=startup,
        gateway=Gateway(GATEWAY, model="watersoftener"),
        rediscovery=True,
    )
    assert found == {missing: leaf(missing, 1.0, "%")}
    assert cloud.requests == [(missing,)]
    assert startup.healthy  # the start-up state is not reset by a search


async def test_holiday_calendar_appears_when_its_resources_are_found_later(
    hass: HomeAssistant,
) -> None:
    from types import SimpleNamespace

    from custom_components.bosch_buderus_heating.calendar import (
        BoschBuderusHolidayCalendar,
        _track_holiday_calendar,
    )
    from custom_components.bosch_buderus_heating.holidays import HOLIDAY_LIST_PATH

    coordinator = _coordinator(hass, Cloud({}))
    unloads: list = []
    entry = SimpleNamespace(async_on_unload=unloads.append)
    added: list = []
    _track_holiday_calendar(entry, coordinator, added.extend)
    assert added == []
    coordinator.resources[HOLIDAY_LIST_PATH] = Resource(HOLIDAY_LIST_PATH)
    coordinator.async_update_listeners()
    coordinator.async_update_listeners()
    assert len(added) == 1 and isinstance(added[0], BoschBuderusHolidayCalendar)
    # A gateway that has them from the start gets it at once, without listener.
    other = _coordinator(hass, Cloud({}))
    other.resources[HOLIDAY_LIST_PATH] = Resource(HOLIDAY_LIST_PATH)
    _track_holiday_calendar(entry, other, added.extend)
    assert len(added) == 2 and not other._listeners
    assert len(unloads) == 1
    for remove in unloads:
        remove()


async def test_setup_wires_the_inventory_and_removal_deletes_it(
    hass: HomeAssistant, hass_storage
) -> None:
    from unittest.mock import AsyncMock

    from custom_components.bosch_buderus_heating import (
        async_remove_entry,
        async_setup_entry,
    )
    from tests.test_entry import _entry

    entry = _entry(hass)
    inventory = discovery_inventory(hass, entry.entry_id)
    await inventory.async_load()
    inventory.store.async_delay_save = lambda data, delay: None
    inventory.record(Gateway("deselected"), tree())
    with (
        patch(
            "custom_components.bosch_buderus_heating.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("gateway-one"),)),
        ),
        patch(
            f"{COORDINATOR}.BoschBuderusDataUpdateCoordinator."
            "async_config_entry_first_refresh",
            AsyncMock(),
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch(
            "custom_components.bosch_buderus_heating.async_setup_notifications",
            AsyncMock(return_value=()),
        ),
    ):
        assert await async_setup_entry(hass, entry)
    (coordinator,) = entry.runtime_data.coordinators
    assert coordinator.inventory is inventory
    assert inventory.gateway("deselected") is None
    key = f"bosch_buderus_heating.discovery_inventory.{entry.entry_id}"
    await inventory.store.async_save({"gateways": {}})
    assert key in hass_storage
    await async_remove_entry(hass, entry)
    assert key not in hass_storage


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_recovered_entities_keep_their_registered_identity(
    hass: HomeAssistant, clock: Clock
) -> None:
    """End to end: an old registry entry gets its state back, exactly once."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.bosch_buderus_heating.const import PLATFORMS
    from custom_components.bosch_buderus_heating.pointt import PointTClient
    from custom_components.bosch_buderus_heating.runtime import (
        BoschBuderusRuntimeData,
    )
    from custom_components.bosch_buderus_heating.sensor import (
        BoschBuderusSensor,
        build_sensor_descriptions,
    )

    inventory = _inventory(hass)
    entry = MockConfigEntry(domain=DOMAIN, version=3)
    entry.add_to_hass(hass)
    await _known_before(hass, inventory, entry)
    cloud = Cloud(tree())
    cloud.down["/system"] = 404
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, cloud, Gateway(GATEWAY, model="MX400"), entry
    )
    coordinator.inventory = inventory
    (description,) = build_sensor_descriptions({OUTDOOR: tree()[OUTDOOR]})
    unique_id = BoschBuderusSensor(coordinator, description).unique_id
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id,
        suggested_object_id="my_outdoor_temperature",
        config_entry=entry,
    )
    await coordinator.async_refresh()

    async def start(hass, config):
        config.runtime_data = BoschBuderusRuntimeData(
            PointTClient(None, "synthetic"),
            None,
            (coordinator.gateway,),
            (coordinator,),
        )
        await hass.config_entries.async_forward_entry_setups(config, PLATFORMS)
        return True

    async def stop(hass, config):
        return await hass.config_entries.async_unload_platforms(config, PLATFORMS)

    with (
        patch(
            "custom_components.bosch_buderus_heating.async_setup_entry",
            side_effect=start,
        ),
        patch(
            "custom_components.bosch_buderus_heating.async_unload_entry",
            side_effect=stop,
        ),
        # Feature branch: keep the legacy runtime of this platform-level test.
        patch(
            "custom_components.bosch_buderus_heating.async_migrate_entry",
            AsyncMock(return_value=True),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        state = hass.states.get(old.entity_id)
        assert state is None or state.state == "unavailable"

        del cloud.down["/system"]
        clock.now += RECOVERY_SCHEDULE[0]
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        state = hass.states.get(old.entity_id)
        assert state is not None and float(state.state) == 7.5
        assert old.entity_id == "sensor.my_outdoor_temperature"
        same = [
            item
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
            if item.unique_id == old.unique_id
        ]
        assert len(same) == 1
        # Another cycle does not add it twice.
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert len(hass.states.async_entity_ids("sensor")) == len(
            set(hass.states.async_entity_ids("sensor"))
        )
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize(
    "failure", [404, RequestTimeout()], ids=["http_404", "timeout"]
)
async def test_heating_circuit_controls_appear_when_found_again(
    hass: HomeAssistant, failure
) -> None:
    """Real setup: a circuit root missing at start brings its controls back
    with the search, without a reload and with the identities from before."""
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import entity_registry as er

    from tests.test_entry import _entry
    from tests.test_startup_inventory import (
        CIRCUIT,
        Setup,
        _gateway_entities,
        _normal_start,
        _run,
    )

    entry = _entry(hass)
    setup = Setup(hass)
    gateway_id = "gateway-one"
    circuit = f"{gateway_id}:{CIRCUIT.strip('/').replace('/', ':')}:"

    async def body() -> None:
        live = await _normal_start(hass, setup, entry)
        before = _gateway_entities(hass, entry)
        controls = {
            unique_id: item
            for unique_id, item in before.items()
            if unique_id.startswith(circuit)
            and item.domain in {"climate", "number", "select", "switch"}
        }
        assert {"climate", "number", "select"} <= {
            item.domain for item in controls.values()
        }

        setup.cloud.down["/heatingCircuits"] = failure
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        coordinator = entry.runtime_data.coordinators[0]
        assert coordinator.startup_mode == "live"
        recovery = coordinator.discovery_recovery
        assert recovery.state == "pending"
        assert recovery.roots() == ("/heatingCircuits",)
        components = hass.data["entity_components"]
        for item in controls.values():
            assert components[item.domain].get_entity(item.entity_id) is None
            state = hass.states.get(item.entity_id)
            assert state is None or state.state == "unavailable"
        assert set(_gateway_entities(hass, entry)) == live  # nothing removed

        del setup.cloud.down["/heatingCircuits"]
        setup.clock.now += RECOVERY_SCHEDULE[0]
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert recovery.state == "recovered" and recovery.attempts == 1
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.coordinators[0] is coordinator  # no reload
        after = _gateway_entities(hass, entry)
        assert set(after) == live
        for unique_id, item in controls.items():
            assert after[unique_id].entity_id == item.entity_id
            assert components[item.domain].get_entity(item.entity_id) is not None
            state = hass.states.get(item.entity_id)
            assert state is not None and state.state != "unavailable"
        by_domain = {item.domain: item.entity_id for item in controls.values()}
        assert hass.states.get(by_domain["select"]).state == "manual"
        assert float(hass.states.get(by_domain["number"]).state) == 19
        same = [
            item.unique_id
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        ]
        assert len(same) == len(set(same))  # each identity exactly once

    with patch(
        "custom_components.bosch_buderus_heating.async_migrate_entry",
        AsyncMock(return_value=True),
    ):
        await _run(setup, entry, body)


async def test_diagnostics_show_the_search_without_identifiers(
    hass: HomeAssistant, clock: Clock
) -> None:
    inventory = _inventory(hass)
    await _known_before(hass, inventory)
    cloud = Cloud(tree())
    cloud.down["/system"] = 404
    coordinator = _coordinator(hass, cloud, inventory)
    await coordinator.async_refresh()
    report = coordinator.discovery_recovery.diagnostics(clock.now)
    text = json.dumps(report)
    assert GATEWAY not in text and "7.5" not in text
    assert set(report) == {
        "state",
        "reference_resources",
        "missing_at_start",
        "missing",
        "missing_templates",
        "missing_reasons",
        "recovered",
        "attempts",
        "attempts_planned",
        "postponed",
        "last_result",
        "next_attempt_in_seconds",
    }


# ---------------------------------------------------------------- inventory


def test_resources_round_trip_losslessly() -> None:
    resource = Resource(
        "/heatSources/emon/totalConsumption",
        value="text",
        has_value=True,
        values=({"compressor": 10.5, "eheater": 2}, [1, None, True]),
        has_values=True,
        metadata=ResourceMetadata(
            resource_type="emonValue",
            unit="kWh",
            allowed_values=("on", "off", 3),
            minimum=-5,
            maximum=55.5,
            writable=True,
            numeric_error_values=(-3276.8,),
            numeric_error_states_valid=False,
            step_size=0.5,
            step_size_valid=True,
            used=True,
            available=False,
            availability_flags_valid=True,
        ),
        references=(ResourceReference("/a", "A"), ResourceReference("/b")),
        unknown_fields=frozenset({"vendor", "extra"}),
        program_fields={"name": "P1", "days": [1, 2]},
    )
    data = json.loads(json.dumps(resource_to_data(resource)))
    assert resource_from_data(data) == resource


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.update(path="relative"),
        lambda data: data.update(has_value="yes"),
        lambda data: data["metadata"].update(minimum=True),
        lambda data: data["metadata"].update(minimum=float("nan")),
        lambda data: data["metadata"].update(allowed_values=[[1]]),
        lambda data: data["metadata"].update(numeric_error_values=["x"]),
        lambda data: data.update(references=[{"path": 1}]),
        lambda data: data.update(unknown_fields=[1]),
        lambda data: data.update(program_fields=[]),
        lambda data: data.update(value={1: 2}),
        lambda data: data.pop("metadata"),
    ],
)
def test_damaged_resources_are_rejected(change) -> None:
    data = resource_to_data(leaf(OUTDOOR, 7.5))
    change(data)
    with pytest.raises(ValueError):
        resource_from_data(data)


async def test_inventory_survives_a_restart_and_drops_old_or_damaged_rows(
    hass: HomeAssistant, hass_storage
) -> None:
    inventory = DiscoveryInventory(hass, "entry")
    await inventory.async_load()
    inventory.store.async_delay_save = lambda data, delay: None
    inventory.record(Gateway(GATEWAY, model="MX400", firmware_version="1.2"), tree())
    data = inventory._data()
    rows = data["gateways"][GATEWAY]["resources"]
    old = (datetime.now(UTC) - MAX_AGE - timedelta(days=1)).isoformat()
    rows["/system"]["seen"] = old
    rows["/heatSources"]["resource"]["metadata"] = "damaged"
    data["gateways"]["broken"] = {"gateway": None}
    hass_storage["bosch_buderus_heating.discovery_inventory.entry"] = {
        "version": 1,
        "minor_version": 1,
        "key": "bosch_buderus_heating.discovery_inventory.entry",
        "data": data,
    }
    restarted = DiscoveryInventory(hass, "entry")
    await restarted.async_load()
    stored = restarted.gateway(GATEWAY)
    assert stored is not None
    assert stored.gateway == Gateway(GATEWAY, model="MX400", firmware_version="1.2")
    assert set(restarted.resources(GATEWAY)) == set(tree()) - {
        "/system",
        "/heatSources",
    }
    assert restarted.resources(GATEWAY)[OUTDOOR] == tree()[OUTDOOR]
    assert restarted.gateway("broken") is None

    damaged = DiscoveryInventory(hass, "other")

    async def broken():
        raise ValueError("damaged")

    damaged.store.async_load = broken
    await damaged.async_load()
    assert damaged.resources(GATEWAY) == {}


async def test_inventory_keeps_parts_missing_now_and_forgets_deselected(
    hass: HomeAssistant,
) -> None:
    saves: list = []
    inventory = _inventory(hass, saves)
    gateway = Gateway(GATEWAY)
    inventory.record(gateway, tree())
    partial = {MODULATION: leaf(MODULATION, 41.0, "%")}
    inventory.record(gateway, partial)
    assert set(inventory.resources(GATEWAY)) == set(tree())
    assert inventory.resources(GATEWAY)[MODULATION].value == 41.0
    inventory.record(gateway, {})  # nothing discovered: nothing changes
    assert len(saves) == 2
    inventory.retain(["another"])
    assert inventory.resources(GATEWAY) == {}
    assert saves[-1] == {"gateways": {}}


async def test_inventory_is_shared_per_entry_and_removed_with_it(
    hass: HomeAssistant, hass_storage
) -> None:
    first = discovery_inventory(hass, "entry")
    assert discovery_inventory(hass, "entry") is first
    assert discovery_inventory(hass, "other") is not first
    await first.async_load()
    first.record(Gateway(GATEWAY), tree())
    await first.store.async_save(first._data())
    assert "bosch_buderus_heating.discovery_inventory.entry" in hass_storage
    await async_remove_discovery_inventory(hass, "entry")
    assert "bosch_buderus_heating.discovery_inventory.entry" not in hass_storage
    assert discovery_inventory(hass, "entry") is not first
    await async_remove_discovery_inventory(hass, "never-loaded")


# ----------------------------------------------------------------- helpers


def test_roots_cover_each_missing_subtree_once() -> None:
    assert recovery_roots(
        ["/system/a/b", "/system", "/system/a", "/heatSources/x", "/heatSources/xy"]
    ) == ("/system", "/heatSources/x", "/heatSources/xy")


@pytest.mark.parametrize(
    ("category", "transient"),
    [
        ("timeout", True),
        ("http_503", True),
        ("http_500", True),
        ("malformed", True),
        ("http_404", False),
        ("http_5", False),
        ("not_attempted", False),
        (None, False),
    ],
)
def test_transient_results(category, transient) -> None:
    assert is_transient_result(category) is transient


def test_many_missing_templates_are_shortened() -> None:
    recovery = DiscoveryRecovery()
    recovery.plan({f"/root{index}": "http_404" for index in range(30)}, 30, 0.0)
    templates = recovery.templates()
    assert len(templates) == 21 and templates[-1] == "…"
    recovery.plan({}, 0, 0.0)
    assert recovery.state == "none"
