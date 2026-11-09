"""Poll cycle limits: a failing batch, rate-limit events, explicit refreshes
and energy counter decreases (0.9 review: COORD-3, COORD-5, CHAOS-5, SEC-2,
NRG-3).

The cycles run through the real coordinator with a synthetic cloud client and
a controlled monotonic clock.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating import refresh_outage
from custom_components.bosch_buderus_heating.const import (
    DOMAIN,
    RATE_LIMIT_ISSUE_PREFIX,
    PollingProfile,
)
from custom_components.bosch_buderus_heating.coordinator import (
    CIRCUIT_FAILURE_THRESHOLD,
    RATE_LIMIT_REPAIR_THRESHOLD,
    RATE_LIMIT_REPAIR_WINDOW,
    _energy_decreases,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    RateLimited,
    Resource,
    ResourceMetadata,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from tests.test_coordinator import _coordinator as cloud_coordinator
from tests.test_coordinator import _resource, _snapshot, _success

FAST = "/heatSources/actualModulation"
SLOW = tuple(f"/heatSources/hs1/slow{index}" for index in range(40))
BAD = "/heatSources/hs1/slowBad"
CONTROL = "/heatingCircuits/hc1/operationMode"
ENERGY = "/heatSources/emon/totalConsumption"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    clock = Clock()
    with (
        patch.object(coordinator_module, "monotonic", clock),
        patch.object(refresh_outage, "monotonic", clock),
    ):
        yield clock


def _plant(hass: HomeAssistant, client: AsyncMock, groups: dict[PollGroup, tuple]):
    coordinator = cloud_coordinator(hass, client)
    paths = [path for group in groups.values() for path in group]
    coordinator.resources = {path: _resource(path, 1.0) for path in paths}
    coordinator.data = {path: _snapshot(coordinator.resources[path]) for path in paths}
    coordinator._paths_by_group = dict(groups)
    coordinator.last_update_success = True
    return coordinator


class FailingBatch:
    """Bulk reads answer, except a request with BAD fails as a whole (500)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, ...]] = []
        self.down = False

    async def read(self, _gateway, chunk, **_kwargs):
        self.requests.append(tuple(chunk))
        if self.down or BAD in chunk:
            raise ServiceUnavailable(500)
        return tuple(_success(path, 2.0) for path in chunk)


# ------------------------------------------------------------------ COORD-3


async def test_a_failing_batch_does_not_read_the_answered_paths_each_minute(
    hass: HomeAssistant, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    client = AsyncMock()
    cloud = FailingBatch()
    client.get_resources_bulk.side_effect = cloud.read
    client.get_resource.side_effect = ServiceUnavailable(500)
    coordinator = _plant(
        hass, client, {PollGroup.FAST: (FAST,), PollGroup.SLOW: (*SLOW, BAD)}
    )
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    slow_reads: list[int] = []
    for minute in range(17):
        clock.now = 1000.0 + minute * 60.0
        before = len(cloud.requests)
        await coordinator.async_refresh()
        # The failing batch alone is no failure of the gateway.
        assert coordinator.last_update_success, minute
        sent = cloud.requests[before:]
        slow_reads.append(sum(path in SLOW for chunk in sent for path in chunk))
        # The live value keeps its minute and never shares the failing batch.
        assert any(FAST in chunk for chunk in sent), minute
        assert not any(FAST in chunk and BAD in chunk for chunk in sent)
    # The answered slow paths are read once per 15 minutes; only the failing
    # batch (BAD and the paths it shared a request with) is tried each minute.
    shared = len(SLOW) - 29  # the first batch holds FAST and 29 slow paths
    assert slow_reads[0] == len(SLOW)
    assert slow_reads[1:15] == [shared] * 14
    assert slow_reads[15] == len(SLOW)
    assert coordinator._gateway_failure_count == 0
    assert coordinator.data[SLOW[0]].available
    batch = [
        record
        for record in caplog.records
        if "batch refresh failed" in record.getMessage()
    ]
    # O81: one warning for the whole outage of that batch.
    assert [record.levelno for record in batch].count(logging.WARNING) == 1

    # The cloud goes down completely: the live value fails as well, so the
    # cycle fails as before and the breaker counts it.
    cloud.down = True
    clock.now += 60.0
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator._gateway_failure_count == 1

    # A cycle in which every batch answers ends the outage: the next failure
    # warns again.
    cloud.down = False
    del coordinator.resources[BAD]
    coordinator._paths_by_group[PollGroup.SLOW] = SLOW
    clock.now += 60.0
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert not coordinator._batch_failure_logged


async def test_a_recovered_batch_completes_its_group(
    hass: HomeAssistant, clock: Clock
) -> None:
    client = AsyncMock()
    cloud = FailingBatch()
    client.get_resources_bulk.side_effect = cloud.read
    client.get_resource.side_effect = ServiceUnavailable(500)
    coordinator = _plant(hass, client, {PollGroup.SLOW: (*SLOW, BAD)})
    await coordinator.async_refresh()
    assert coordinator._next_update.get(PollGroup.SLOW, 0.0) <= clock.now
    # Cycles that read only the failing batch: no failure of the gateway,
    # as when it failed beside the answered ones.
    for _ in range(CIRCUIT_FAILURE_THRESHOLD + 1):
        clock.now += 60.0
        before = len(cloud.requests)
        await coordinator.async_refresh()
        assert all(BAD in chunk for chunk in cloud.requests[before:])
        assert coordinator.last_update_success
    assert coordinator._gateway_failure_count == 0
    assert coordinator._circuit_open_until == 0.0
    # The failing path answers again: the group completes and waits its
    # interval; nothing is read before it.
    client.get_resources_bulk.side_effect = lambda _g, chunk, **_k: tuple(
        _success(path, 3.0) for path in chunk
    )
    clock.now += 60.0
    await coordinator.async_refresh()
    assert coordinator._next_update[PollGroup.SLOW] == clock.now + 900
    assert not coordinator._interrupted_progress
    assert not coordinator._failing_batch_paths
    assert coordinator.data[BAD].available


# ------------------------------------------------------- COORD-5 / CHAOS-5


async def test_the_pause_starts_when_the_cloud_answered_429(
    hass: HomeAssistant, clock: Clock
) -> None:
    client = AsyncMock()
    paths = tuple(f"/heatSources/value{index}" for index in range(45))
    coordinator = _plant(hass, client, {PollGroup.FAST: paths})

    async def bulk(_gateway, chunk, **_kwargs):
        if paths[0] in chunk:
            clock.now += 20.0  # the first batch takes a while
            return tuple(_success(path, 2.0) for path in chunk)
        raise RateLimited(retry_after=300)

    client.get_resources_bulk.side_effect = bulk
    started = clock.now
    await coordinator.async_refresh()
    assert coordinator._cloud_backoff_until == started + 20.0 + 300.0


async def test_a_request_held_back_by_the_shared_pause_is_no_new_event(
    hass: HomeAssistant, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    client = AsyncMock()
    shared = SimpleNamespace(remaining=0.0)
    client.backoff = shared
    coordinator = _plant(hass, client, {PollGroup.FAST: (FAST,)})
    # Another gateway of the cloud access met the limit.
    shared.remaining = 400.0
    client.get_resources_bulk.side_effect = RateLimited(400.0)
    caplog.set_level(logging.WARNING, logger=coordinator_module.__name__)
    with pytest.raises(UpdateFailed, match="backoff"):
        await coordinator._async_update_data()
    client.get_resources_bulk.assert_not_awaited()
    assert coordinator._cloud_backoff_until == clock.now + 400.0
    assert coordinator.diagnostics_summary()["rate_limit_events"] == 0
    assert "rate limit reached" not in caplog.text

    # Activated by another gateway while this request waited for its turn.
    shared.remaining = 0.0
    coordinator._cloud_backoff_until = 0.0
    coordinator._next_update = {PollGroup.FAST: 0.0}

    async def held(_gateway, _chunk, **_kwargs):
        raise RateLimited(300.0)

    client.get_resources_bulk.side_effect = held
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    # Sampled before the request: this one was sent and counts.
    assert coordinator.diagnostics_summary()["rate_limit_events"] == 1

    # The pause starts during this cycle (another gateway met the limit):
    # the next batch is held back before it is sent and is no new event.
    coordinator._cloud_backoff_until = 0.0
    coordinator.resources = {path: _resource(path, 1.0) for path in (FAST, *SLOW)}
    coordinator._paths_by_group = {PollGroup.FAST: (FAST, *SLOW)}
    coordinator._next_update = {PollGroup.FAST: 0.0}

    async def sibling(_gateway, chunk, **_kwargs):
        if shared.remaining:
            raise RateLimited(shared.remaining)
        shared.remaining = 400.0
        return tuple(_success(path, 2.0) for path in chunk)

    client.get_resources_bulk.side_effect = sibling
    await coordinator._async_update_data()
    assert client.get_resources_bulk.await_count == 3
    assert coordinator.diagnostics_summary()["rate_limit_events"] == 1
    assert coordinator._cloud_backoff_until >= clock.now + 400.0

    # Discovery and the single reads use the same rule.
    shared.remaining = 400.0
    coordinator._activate_rate_limit_backoff(
        RateLimited(400.0), clock.now, counted=False
    )
    assert coordinator.diagnostics_summary()["rate_limit_events"] == 1
    coordinator.resources = {}
    with (
        pytest.raises(UpdateFailed, match="rate limit"),
        patch.object(
            coordinator_module,
            "async_discover_resources",
            AsyncMock(side_effect=RateLimited(400.0)),
        ),
    ):
        await coordinator._async_update_data()
    assert coordinator.diagnostics_summary()["rate_limit_events"] == 1


def _issue(hass: HomeAssistant, coordinator) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{RATE_LIMIT_ISSUE_PREFIX}{coordinator._config_entry.entry_id}"
    )


async def test_the_repair_counts_rate_limits_within_a_day_only(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = cloud_coordinator(hass, AsyncMock())
    window = RATE_LIMIT_REPAIR_WINDOW.total_seconds()
    for _ in range(RATE_LIMIT_REPAIR_THRESHOLD - 1):
        coordinator._activate_rate_limit_backoff(RateLimited(60), clock.now)
        clock.now += window / 2
    # The first one is older than a day now: two in the window, no repair.
    coordinator._activate_rate_limit_backoff(RateLimited(60), clock.now)
    assert _issue(hass, coordinator) is None
    summary = coordinator.diagnostics_summary()
    assert summary["rate_limit_events"] == RATE_LIMIT_REPAIR_THRESHOLD
    assert summary["rate_limit_events_last_24h"] == RATE_LIMIT_REPAIR_THRESHOLD - 1
    clock.now += 60
    coordinator._activate_rate_limit_backoff(RateLimited(60), clock.now)
    assert _issue(hass, coordinator) is not None


async def test_no_repair_while_the_cloud_friendly_profile_is_in_use(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = cloud_coordinator(hass, AsyncMock())
    coordinator.polling_profile = PollingProfile.CLOUD_FRIENDLY
    for _ in range(RATE_LIMIT_REPAIR_THRESHOLD + 1):
        coordinator._activate_rate_limit_backoff(RateLimited(60), clock.now)
    assert _issue(hass, coordinator) is None
    # The pause itself applies as before.
    assert coordinator._cloud_backoff_until > clock.now


# ------------------------------------------------------------------- SEC-2


async def test_explicit_refreshes_read_everything_once_per_minute(
    hass: HomeAssistant, clock: Clock
) -> None:
    client = AsyncMock()
    client.get_resources_bulk.side_effect = lambda _g, chunk, **_k: tuple(
        _success(path, 2.0) for path in chunk
    )
    coordinator = _plant(
        hass,
        client,
        {
            PollGroup.FAST: (FAST,),
            PollGroup.CONTROL: (CONTROL,),
            PollGroup.SLOW: SLOW[:3],
        },
    )
    await coordinator.async_refresh()  # every group read and advanced

    def requested() -> set[str]:
        return {
            path
            for call in client.get_resources_bulk.await_args_list
            for path in call.args[1]
        }

    async def explicit() -> set[str]:
        client.get_resources_bulk.reset_mock()
        with patch.object(
            coordinator._debounced_refresh,
            "async_call",
            AsyncMock(side_effect=coordinator.async_refresh),
        ):
            await coordinator.async_request_refresh()
        return requested()

    clock.now += 5
    assert await explicit() == {FAST, CONTROL, *SLOW[:3]}
    # An automation asking every ten seconds: only the controls.
    for _ in range(5):
        clock.now += 10
        assert await explicit() == {CONTROL}
    clock.now += 10  # 60 seconds after the full refresh
    assert await explicit() == {FAST, CONTROL, *SLOW[:3]}
    summary = coordinator.cloud_rate_summary()
    assert summary["explicit_refreshes_full"] == 2
    assert summary["explicit_refreshes_control_only"] == 5
    assert summary["explicit_refresh_gap_seconds"] == 60.0


def test_the_cloud_friendly_profile_keeps_two_minutes_between_full_refreshes(
    hass: HomeAssistant,
) -> None:
    coordinator = cloud_coordinator(hass, AsyncMock())
    coordinator._poll_intervals = coordinator_module.POLL_INTERVALS_CLOUD_FRIENDLY
    assert coordinator._explicit_refresh_gap() == 120.0


# ------------------------------------------------------------------- NRG-3


def _energy(parts: dict[str, float], unit: str | None = "kWh") -> Resource:
    return Resource(
        ENERGY,
        values=(parts,),
        metadata=ResourceMetadata(resource_type="emonValue", unit=unit),
    )


def test_a_defrost_is_no_counter_reset() -> None:
    before = _energy({"compressor": 195.06, "eheater": 0, "outputProduced": 788.51})
    defrost = _energy({"compressor": 195.06, "eheater": 0, "outputProduced": 787.47})
    assert _energy_decreases(before, defrost) == (0, 1)
    reset = _energy({"compressor": 2.0, "eheater": 0, "outputProduced": 3.0})
    assert _energy_decreases(before, reset) == (1, 1)
    assert _energy_decreases(reset, reset) == (0, 0)


def test_a_unit_change_is_compared_in_kwh() -> None:
    wh = _energy({"compressor": 195060.0}, "Wh")
    kwh = _energy({"compressor": 195.07})
    assert _energy_decreases(wh, kwh) == (0, 0)
    assert _energy_decreases(kwh, _energy({"compressor": 195000.0}, "Wh")) == (1, 0)


def test_only_valid_energy_collection_parts_count() -> None:
    before = _energy({"compressor": 120.0})
    # Not an energy collection, another path, or an invalid part: skipped.
    other = Resource(ENERGY, values=({"compressor": 2.0},))
    assert _energy_decreases(before, other) == (0, 0)
    moved = Resource(
        "/heatSources/emon/chConsumption",
        values=({"compressor": 2.0},),
        metadata=ResourceMetadata(resource_type="emonValue", unit="kWh"),
    )
    assert _energy_decreases(before, moved) == (0, 0)
    assert _energy_decreases(before, _energy({"compressor": -1.0})) == (0, 0)
    assert _energy_decreases(before, _energy({"compressor": 2.0}, "MJ?")) == (0, 0)


async def test_the_poll_counts_resets_and_defrosts_apart(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    coordinator = cloud_coordinator(hass, client)
    previous = _energy({"compressor": 120.0, "outputProduced": 350.0})
    current = _energy({"compressor": 120.5, "outputProduced": 349.0})
    coordinator.resources = {ENERGY: previous}
    coordinator.data = {ENERGY: _snapshot(previous)}
    coordinator._paths_by_group = {PollGroup.ENERGY: (ENERGY,)}
    coordinator._next_update = {PollGroup.ENERGY: 0.0}
    client.get_resources_bulk.return_value = (
        BatchItemResult("gateway-one", ENERGY, 200, resource=current),
    )
    result = await coordinator._async_update_data()
    assert result[ENERGY].resource is current
    summary = coordinator.diagnostics_summary()
    assert summary["energy_counter_resets_detected"] == 0
    assert summary["produced_heat_decreases_detected"] == 1
