"""Regression tests for Home Assistant timer rounding and polling deadlines."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import DOMAIN, PollingProfile
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    RequestMetrics,
    Resource,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup


def _ready_coordinator(hass, profile=PollingProfile.STANDARD, group=PollGroup.FAST):
    client = AsyncMock()
    client.metrics = RequestMetrics()
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway-one"), entry, profile
    )
    resource = (
        Resource(
            "/notifications", values=({"ccd": "6249", "fc": "12"},), has_values=True
        )
        if group is PollGroup.NOTIFICATIONS
        else Resource(
            "/heatingCircuits/hc1/currentRoomSetpoint", value=20, has_value=True
        )
    )
    coordinator.resources = {resource.path: resource}
    coordinator._paths_by_group = {group: (resource.path,)}
    coordinator.data = {
        resource.path: ResourceSnapshot(
            resource=resource,
            available=True,
            last_success=datetime(2026, 9, 10, tzinfo=UTC),
            last_attempt=datetime(2026, 9, 10, tzinfo=UTC),
            source=SnapshotSource.DISCOVERY,
        )
    }
    client.get_resources_bulk.return_value = (
        BatchItemResult("gateway-one", resource.path, 200, resource),
    )
    return coordinator, client, resource


@pytest.mark.parametrize(
    ("profile", "group", "active_fault", "interval"),
    [
        (PollingProfile.STANDARD, PollGroup.FAST, False, 60),
        (PollingProfile.STANDARD, PollGroup.CONTROL, False, 300),
        (PollingProfile.STANDARD, PollGroup.ENERGY, False, 300),
        (PollingProfile.STANDARD, PollGroup.SLOW, False, 900),
        (PollingProfile.STANDARD, PollGroup.NOTIFICATIONS, False, 300),
        (PollingProfile.CLOUD_FRIENDLY, PollGroup.FAST, False, 120),
        (PollingProfile.CLOUD_FRIENDLY, PollGroup.CONTROL, False, 600),
        (PollingProfile.CLOUD_FRIENDLY, PollGroup.NOTIFICATIONS, True, 60),
    ],
)
async def test_timer_rounding_does_not_skip_due_poll(
    hass: HomeAssistant, profile, group, active_fault, interval
) -> None:
    coordinator, client, resource = _ready_coordinator(hass, profile, group)
    if group is PollGroup.NOTIFICATIONS and not active_fault:
        resource = Resource("/notifications", has_values=True)
        client.get_resources_bulk.return_value = (
            BatchItemResult("gateway-one", resource.path, 200, resource),
        )
    if active_fault:
        coordinator.faults.process_resources({resource.path: resource})
    start = 1000.202
    coordinator._advance_groups(tuple(coordinator._poll_intervals), start)
    coordinator._microsecond = 0.2
    finished = start + 0.35
    ticks_per_read = interval // 60

    for tick in range(1, ticks_per_read * 3 + 1):
        # Exercise HA's actual scheduler, including its whole-second rounding.
        with (
            patch.object(hass.loop, "time", return_value=finished),
            patch.object(hass.loop, "call_at") as schedule,
        ):
            coordinator._schedule_refresh()
        scheduled = schedule.call_args.args[0]
        # The first callback is 1 ms early relative to our exact deadline.
        now = scheduled + (0.001, 0.004, 0.002, 0.005)[(tick - 1) % 4]
        with patch(
            "custom_components.bosch_buderus_heating.coordinator.monotonic",
            return_value=now,
        ):
            await coordinator._async_update_data()
        assert client.get_resources_bulk.await_count == tick // ticks_per_read
        finished = now + 0.35
    coordinator._async_unsub_refresh()


@pytest.mark.parametrize(
    ("elapsed", "expected_reads"), [(58.9, 0), (59.0, 1), (59.999, 1), (60.0, 1)]
)
async def test_group_due_tolerance_is_bounded(hass, elapsed, expected_reads):
    coordinator, client, _ = _ready_coordinator(hass)
    coordinator._advance_groups(tuple(coordinator._poll_intervals), 1000.0)
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.monotonic",
        return_value=1000.0 + elapsed,
    ):
        await coordinator._async_update_data()
    assert client.get_resources_bulk.await_count == expected_reads


@pytest.mark.parametrize("barrier", ["rate_limit", "circuit_breaker", "resource"])
async def test_due_tolerance_never_shortens_backoff(hass, barrier):
    coordinator, client, resource = _ready_coordinator(hass)
    coordinator._advance_groups(tuple(coordinator._poll_intervals), 1000.0)
    if barrier == "rate_limit":
        coordinator._cloud_backoff_until = 1060.0
    elif barrier == "circuit_breaker":
        coordinator._circuit_open_until = 1060.0
    else:
        coordinator._negative_until[resource.path] = 1060.0
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.monotonic",
        return_value=1059.5,
    ):
        if barrier == "resource":
            await coordinator._async_update_data()
        else:
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
    client.get_resources_bulk.assert_not_awaited()


async def test_overdue_group_does_not_generate_catch_up_reads(hass):
    coordinator, client, _ = _ready_coordinator(hass)
    coordinator._advance_groups(tuple(coordinator._poll_intervals), 1000.0)
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.monotonic",
        return_value=1800.0,
    ):
        await coordinator._async_update_data()
        await coordinator._async_update_data()
    client.get_resources_bulk.assert_awaited_once()
