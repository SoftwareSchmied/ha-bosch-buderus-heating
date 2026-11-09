"""How the reading rates of 0.9.0-alpha.3 work together.

Controls whose entities are all disabled (O51), cloud reads spread over the
minutes (O41), slower cloud copies of locally shown readings (O45), the
incremental local publishing (O13), kept local connections (G4) and the
outage log (#60) each have their own tests; these check them in combination.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from itertools import pairwise
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating import local_read, refresh_outage
from custom_components.bosch_buderus_heating.const import PollingProfile
from custom_components.bosch_buderus_heating.diagnostics import _gateway_diagnostics
from custom_components.bosch_buderus_heating.local_polling import service as module
from custom_components.bosch_buderus_heating.local_polling.service import (
    LocalPollingService,
)
from custom_components.bosch_buderus_heating.local_polling.storage import (
    LocalScheduleStore,
)
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    poll_group,
)
from tests.test_cloud_polling_plan import (
    CYCLE,
    SUPPLY,
    TICK,
    Clock,
    _capture,
    _cycle,
    _patched,
    _plant,
    _run,
    _with_local,
)
from tests.test_coordinator import _coordinator as cloud_coordinator
from tests.test_coordinator import _resource, _snapshot
from tests.test_local_keepalive import answer, reuse_client
from tests.test_local_keepalive import keepalive_gateway as keepalive_gateway
from tests.test_local_polling import no_discovery
from tests.test_local_probe import CONTEXT
from tests.test_local_read import REQUEST
from tests.test_local_read import clock as clock
from tests.test_local_transport import certificates as certificates

GATEWAY = "gateway-1"
# A control the climate, hot water and holiday entities do not read (O51).
CONTROL = "/heatingCircuits/hc1/maxFlowTemp"
MINUTE = TICK + CYCLE


def _disable(coordinator, *paths: str) -> None:
    """As after the registry reported that only disabled entities show them."""
    coordinator.entity_use._disabled = {GATEWAY: frozenset(paths)} if paths else {}
    coordinator._entity_use_changed()


def _gaps(times: list[float]) -> list[float]:
    return [round((b - a) / MINUTE) for a, b in pairwise(times)]


# ------------------------------------------------------ O51 with O41 / O45


async def test_a_disabled_control_keeps_the_slow_rate_with_spread_reads(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    switched: list[bool] = []
    with (
        _patched(clock)[0],
        _patched(clock)[1],
        _patched(clock)[2],
        patch.object(
            coordinator, "_request_switch_read", lambda: switched.append(True)
        ),
    ):
        assert coordinator._slot_plan()[CONTROL][0] is PollGroup.CONTROL
        _disable(coordinator, CONTROL)
        # Slower: nothing to read at once.
        assert not switched and not coordinator._urgent_reads
        await _run(coordinator, clock, 120)
        # The slot planner follows the effective group, never the base one.
        assert coordinator._slot_plan()[CONTROL][0] is PollGroup.SLOW
        times = cloud.reads(clock.now - 60 * MINUTE + 1, clock.now + 1)[CONTROL]
        assert len(times) == 4
        assert set(_gaps(cloud.reads(0, clock.now + 1)[CONTROL][-5:])) == {15}
        summary = coordinator.cloud_rate_summary()
        assert summary["controls_slowed_unused"] == 1
        assert coordinator.polling_group(coordinator.resources[CONTROL]) is (
            PollGroup.SLOW
        )
        report = _gateway_diagnostics(1, coordinator)
        base = sum(
            poll_group(item) is PollGroup.SLOW
            for item in coordinator.resources.values()
        )
        assert report["inventory"]["polling_groups"]["slow"] == base + 1
        assert report["runtime"]["controls_slowed_unused"] == 1
        assert {
            "early_read_paths",
            "local_served_paths",
            "local_served_interval_seconds",
            "switch_reads_pending",
        } <= set(report["runtime"])
        (capability,) = (
            item
            for item in report["capabilities"]
            if item["path"] == CONTROL and item["poll_group"] == "slow"
        )
        assert capability["writable"]
        # Its copy is older than the control interval when it is enabled
        # again: read at once, then every five minutes.
        while clock.utc() - coordinator.data[CONTROL].last_success < timedelta(
            minutes=6
        ):
            await _run(coordinator, clock, 1)
        _disable(coordinator)
        assert switched == [True]
        assert coordinator._urgent_reads == {CONTROL}
        assert coordinator.cloud_rate_summary()["controls_slowed_unused"] == 0
        before = len(cloud.batches)
        clock.now += 2  # the immediate cycle, between two wake-ups
        await _cycle(coordinator)
        assert CONTROL in cloud.batches[before][1]
        await _run(coordinator, clock, 15)
        times = cloud.reads(clock.now - 15 * MINUTE + 1, clock.now + 1)[CONTROL]
        assert len(times) == 3
        assert set(_gaps(times)) == {5}


async def test_a_control_enabled_again_with_a_recent_copy_waits_for_its_group(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    coordinator, _cloud = _plant(hass, clock)
    switched: list[bool] = []
    with (
        _patched(clock)[0],
        _patched(clock)[1],
        _patched(clock)[2],
        patch.object(
            coordinator, "_request_switch_read", lambda: switched.append(True)
        ),
    ):
        _disable(coordinator, CONTROL)
        await _run(coordinator, clock, 2)
        _disable(coordinator)
    assert not switched and not coordinator._urgent_reads


@pytest.mark.parametrize(
    ("profile", "before", "after"),
    [(PollingProfile.STANDARD, 15, 15), (PollingProfile.CLOUD_FRIENDLY, 10, 30)],
)
async def test_a_locally_shown_control_with_disabled_entities_takes_the_slower_rate(
    hass: HomeAssistant, profile: PollingProfile, before: int, after: int
) -> None:
    """A paired reading the catalog would poll with the controls: shown from
    the local access (15 minutes in the standard profile) and then without
    an enabled entity (slow values) it uses the slower of both rates, with
    no extra read at the change."""
    original = coordinator_module.poll_group

    def grouped(resource):
        return PollGroup.CONTROL if resource.path == SUPPLY else original(resource)

    clock = Clock()
    switched: list[bool] = []
    with (
        patch.object(coordinator_module, "poll_group", grouped),
        _patched(clock)[0],
        _patched(clock)[1],
        _patched(clock)[2],
    ):
        coordinator, cloud = _plant(hass, clock, profile=profile)
        with patch.object(
            coordinator, "_request_switch_read", lambda: switched.append(True)
        ):
            _with_local(coordinator, _capture(), clock)
            await _run(coordinator, clock, 70)
            assert (SUPPLY in coordinator._local_served) is (
                profile is PollingProfile.STANDARD
            )
            gaps = _gaps(cloud.reads(0, clock.now + 1)[SUPPLY][-3:])
            assert set(gaps) == {before}
            _disable(coordinator, SUPPLY)
            await _run(coordinator, clock, 1)
            assert SUPPLY not in coordinator._local_served
            # No switch to the cloud value: the local value is still shown.
            assert SUPPLY not in coordinator._served_left
            assert not coordinator._urgent_reads and not switched
            mark = clock.now
            await _run(coordinator, clock, 2 * after + 1)
            times = cloud.reads(mark, clock.now + 1)[SUPPLY]
            assert set(_gaps(times)) == {after}
            assert len(times) in (2, 3)
            assert coordinator.display_snapshot(SUPPLY).available
            assert coordinator.diagnostics_summary()["resources_expired"] == 0
            # The local polling cap follows the effective group.
            assert coordinator.scheduled_interval(SUPPLY) == (
                True,
                coordinator._poll_intervals[PollGroup.SLOW],
            )


# ---------------------------------------------------------- O13 with O45


async def test_the_local_cap_ignores_the_slower_cloud_rate(
    hass: HomeAssistant,
) -> None:
    """``scheduled_interval`` does not depend on the shown source (O45), so
    the O13 token needs no part of it; an O51 change replaces the token."""
    clock = Clock()
    coordinator, _cloud = _plant(hass, clock)
    with _patched(clock)[0], _patched(clock)[1], _patched(clock)[2]:
        normal = coordinator.scheduled_interval(SUPPLY)
        assert normal == (True, timedelta(seconds=60))
        runtime = _with_local(coordinator, _capture(), clock)
        await _run(coordinator, clock, 3)
        assert SUPPLY in coordinator._local_served
        assert coordinator.scheduled_interval(SUPPLY) == normal
        token = coordinator.schedule_token()
        # Shown from the cloud again: same answer.
        runtime.observations = []
        with patch.object(coordinator, "_request_switch_read"):
            coordinator.async_update_listeners()
        assert SUPPLY not in coordinator._local_served
        assert coordinator.scheduled_interval(SUPPLY) == normal
        assert all(
            a is b for a, b in zip(token, coordinator.schedule_token(), strict=True)
        )
        # O51 moves a path: the token is replaced.
        _disable(coordinator, CONTROL)
        changed = coordinator.schedule_token()
        assert len(changed) != len(token) or any(
            a is not b for a, b in zip(token, changed, strict=True)
        )


# ------------------------------------------------ G4 with the local planner


@pytest.mark.usefixtures("socket_enabled")
async def test_a_kept_connection_closed_before_an_answer_is_read_again_at_once(
    hass, keepalive_gateway, clock, tmp_path, monkeypatch
) -> None:
    """The kept-connection deferral of the read client, handled by the local
    polling service: no pause, no new certificate check, read again after
    0.25 seconds on a new connection, and nothing republished meanwhile."""

    async def close_second(raw, writer, number):
        if number == 2:
            return False
        answer(writer)

    monkeypatch.setattr(module, "start_offset", lambda scope: 0)
    # Observations carry the time of the service's clock.
    monkeypatch.setattr(local_read, "monotonic", clock)
    _, state, binding = await keepalive_gateway(close_second)
    transport_client, budget = reuse_client(binding, clock)
    store = LocalScheduleStore(hass, "synthetic-scope")
    store._path = tmp_path / "schedule.json"
    published = []
    service = LocalPollingService(
        CONTEXT,
        transport_client,
        budget,
        store,
        is_current=lambda: True,
        publish=published.append,
        clock=clock,
    )
    no_discovery(service.planner)
    service.planner.hints((REQUEST,), clock())

    async def requests(count: int) -> None:
        for _ in range(1000):
            if len(state.requests) >= count and not service._workers:
                break
            await asyncio.sleep(0.01)
        async with asyncio.timeout(5), service.store._lock:
            pass

    try:
        await service.start()
        service.confirm_identity(CONTEXT.identity)
        await requests(1)
        assert service.snapshot().resources[0].available
        record = service.inventory.records[REQUEST.target]
        first = record.observation
        # The certificate inspection and the kept read connection.
        opened = state.connections
        period = service.inventory.period(REQUEST)
        clock.advance(period)
        service.wake()
        await requests(2)
        # Deferred: the record keeps its due time and nothing is published.
        assert state.connections == opened
        assert transport_client.health()["deferred_reasons"] == {
            "reused_connection_closed": 1
        }
        assert record.observation is first
        assert record.due <= clock()
        shown = len(published)
        assert budget.pause_seconds == 0
        assert service.condition is LocalCondition.READY
        assert service._failures == 0
        # A quarter of a second later: read again on a new connection.
        clock.advance(0.25)
        service.wake()
        await requests(3)
        assert state.connections == opened + 1
        assert record.observation is not first
        assert record.due > clock()
        assert len(published) > shown
        assert transport_client.metrics.tls_inspections == 1
        assert transport_client.connection_reuse()["reuse_failures"] == {"closed": 1}
        assert service.condition is LocalCondition.READY
    finally:
        await service.close()


# ---------------------------------------------------- #60 with O41 / O45


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_a_cycle_with_nothing_due_does_not_end_an_outage(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    path = "/heatSources/actualModulation"
    client = AsyncMock()
    coordinator = cloud_coordinator(hass, client)
    coordinator.resources = {path: _resource(path, 1.0)}
    coordinator.data = {path: _snapshot(coordinator.resources[path])}
    coordinator._paths_by_group = {PollGroup.FAST: (path,)}
    client.get_resources_bulk.side_effect = ServiceUnavailable(503)
    client.get_resource.side_effect = ServiceUnavailable(503)
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    clock = _Clock()

    async def refresh(at: float, *, due: bool = True) -> None:
        clock.now = 1000.0 + at
        coordinator._next_update = {PollGroup.FAST: 0.0 if due else 1e9}
        await coordinator.async_refresh()

    def errors() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.ERROR
        ]

    with (
        patch.object(coordinator_module, "monotonic", clock),
        patch.object(refresh_outage, "monotonic", clock),
    ):
        await refresh(0.0)
        await refresh(60.0)
        assert coordinator._refresh_outage.failures == 2
        # A minute without anything due (for example only locally shown
        # readings): no request, so neither a failure nor the end.
        await refresh(120.0, due=False)
        assert coordinator._refresh_outage.failures == 2
        assert coordinator._refresh_outage.since == 1000.0
        await refresh(180.0)
        await refresh(240.0)
        assert errors() == []
        await refresh(300.0)
        (error,) = errors()
        assert "for 5 minutes (5 failed refreshes in a row)" in error
        # An answered request ends it.
        coordinator._circuit_open_until = 0.0
        client.get_resources_bulk.side_effect = None
        client.get_resources_bulk.return_value = ()
        await refresh(360.0)
        assert coordinator._refresh_outage.failures == 0


async def test_an_outage_with_spread_and_locally_shown_reads_is_one_error(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Every failed cycle counts once, early reads included; no switch read
    starts while the cloud fails, and only the answer ends the outage."""
    clock = Clock()
    coordinator, cloud = _plant(hass, clock)
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    with (
        _patched(clock)[0],
        _patched(clock)[1],
        _patched(clock)[2],
        patch.object(refresh_outage, "monotonic", clock),
    ):
        runtime = _with_local(coordinator, _capture(), clock)
        await _run(coordinator, clock, 20)
        assert coordinator.cloud_rate_summary()["early_read_paths"] > 0
        assert coordinator._local_served
        coordinator.client.get_resources_bulk.side_effect = ServiceUnavailable(503)
        coordinator.client.get_resource.side_effect = ServiceUnavailable(503)
        failures = []
        tasks = []
        with patch.object(
            coordinator.config_entry,
            "async_create_background_task",
            side_effect=lambda *args: tasks.append(args),
        ):
            for minute in range(10):
                clock.now += MINUTE
                await coordinator.async_refresh()
                failures.append(coordinator._refresh_outage.failures)
                if minute == 1:
                    # The local access stops meanwhile: the switch read
                    # waits for a working cloud.
                    runtime.observations = []
                    coordinator.async_update_listeners()
        assert not tasks
        assert failures == list(range(1, 11))
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "failed refreshes in a row" in errors[0].getMessage()
        assert not any("data recovered" in r.getMessage() for r in caplog.records)
        coordinator.client.get_resources_bulk.side_effect = cloud.bulk
        coordinator._circuit_open_until = 0.0
        clock.now += MINUTE
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert coordinator._refresh_outage.failures == 0
        assert any("data recovered" in r.getMessage() for r in caplog.records)
