"""Gateway outages in real polling cycles: pauses, idle cycles, recovery."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating import refresh_outage
from custom_components.bosch_buderus_heating.coordinator import (
    GATEWAY_TIMEOUT_PAUSE,
    IdleAfterFailure,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from tests.test_coordinator import _coordinator, _resource, _snapshot, _success

# A core path: the per-item fallback would read it individually.
CORE = "/heatSources/actualModulation"
OTHER = "/heatSources/noncritical"
THIRD = "/system/sensors/temperatures/outdoor_t1"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@contextmanager
def clock():
    current = Clock()
    with (
        patch.object(coordinator_module, "monotonic", current),
        patch.object(refresh_outage, "monotonic", current),
    ):
        yield current


def _timeout(path: str) -> BatchItemResult:
    """The cloud answered, but the gateway did not answer for this item."""
    return BatchItemResult("gateway-one", path, 504, server_status=504)


def _polling(hass: HomeAssistant, client: AsyncMock, *paths: str):
    coordinator = _coordinator(hass, client)
    coordinator.resources = {path: _resource(path, 1.0) for path in paths}
    coordinator.data = {
        path: _snapshot(resource) for path, resource in coordinator.resources.items()
    }
    coordinator._paths_by_group = {PollGroup.FAST: paths}
    return coordinator


async def test_gateway_wide_server_errors_pause_no_reading(
    hass: HomeAssistant,
) -> None:
    """B02: every item 5xx is the gateway's outage, not each reading's."""
    client = AsyncMock()
    paths = (CORE, OTHER, THIRD)
    coordinator = _polling(hass, client, *paths)
    client.get_resources_bulk.return_value = tuple(_timeout(path) for path in paths)
    client.get_resource.side_effect = ServiceUnavailable(504)
    with clock() as now:
        await coordinator.async_refresh()

        assert not coordinator.last_update_success
        # No reading is paused, and no single read goes to the silent gateway.
        assert all(coordinator.resource_pause_remaining_seconds(p) == 0 for p in paths)
        client.get_resource.assert_not_awaited()
        assert coordinator._gateway_failure_count == 1
        assert all(not coordinator.data[path].available for path in paths)
        assert all(coordinator.data[path].resource.value == 1.0 for path in paths)

        # The gateway answers a minute later: every reading is read at once.
        client.get_resources_bulk.return_value = tuple(
            _success(path, 2.0) for path in paths
        )
        now.now += 60
        await coordinator.async_refresh()

    assert client.get_resources_bulk.await_count == 2
    assert coordinator.last_update_success
    assert all(coordinator.data[path].available for path in paths)
    assert all(coordinator.data[path].resource.value == 2.0 for path in paths)


async def test_repeated_gateway_wide_errors_open_only_the_circuit_breaker(
    hass: HomeAssistant,
) -> None:
    """B02: the breaker handles the outage; afterwards nothing stays paused."""
    client = AsyncMock()
    paths = (CORE, OTHER)
    coordinator = _polling(hass, client, *paths)
    client.get_resources_bulk.return_value = tuple(_timeout(path) for path in paths)
    with clock() as now:
        for _ in range(coordinator_module.CIRCUIT_FAILURE_THRESHOLD):
            await coordinator.async_refresh()
            now.now += 60
        assert coordinator._circuit_open_until > now.now
        assert not coordinator._negative_until
        client.get_resource.assert_not_awaited()

        client.get_resources_bulk.return_value = tuple(
            _success(path, 3.0) for path in paths
        )
        now.now = coordinator._circuit_open_until + 1
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert all(coordinator.data[path].resource.value == 3.0 for path in paths)


async def test_single_slow_reading_keeps_its_gateway_timeout_pause(
    hass: HomeAssistant,
) -> None:
    """B02: a 504 next to answered items still pauses that one reading."""
    client = AsyncMock()
    coordinator = _polling(hass, client, CORE, OTHER)
    client.get_resources_bulk.return_value = (_success(OTHER, 2.0), _timeout(CORE))
    client.get_resource.side_effect = ServiceUnavailable(504)
    with clock():
        await coordinator.async_refresh()
        remaining = coordinator.resource_pause_remaining_seconds(CORE)

    assert coordinator.last_update_success
    assert remaining == GATEWAY_TIMEOUT_PAUSE.total_seconds()
    assert coordinator.resource_pause_remaining_seconds(OTHER) == 0
    # The bounded per-item fallback still tries the slow reading once.
    client.get_resource.assert_awaited_once()


async def test_idle_cycle_after_failure_stays_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """B03: Leerlauf nach Fehlschlag bleibt Fehlschlag.

    A single item with 504 cannot tell a slow reading from a silent gateway:
    the reading is paused and the cycle fails. The following cycles have
    nothing to request; they must not report the cloud recovered.
    """
    client = AsyncMock()
    coordinator = _polling(hass, client, CORE)
    client.get_resources_bulk.return_value = (_timeout(CORE),)
    client.get_resource.side_effect = ServiceUnavailable(504)
    caplog.set_level(logging.DEBUG)
    with clock() as now:
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        assert coordinator.cloud_unavailable()
        outage_failures = coordinator._refresh_outage.failures
        poll_failures = coordinator._poll_failures
        since = coordinator._unreachable_since
        caplog.clear()

        for _ in range(3):
            now.now += 60
            await coordinator.async_refresh()
            assert not coordinator.last_update_success
            assert coordinator.cloud_unavailable()
            assert coordinator.control_status()[0] == "cloud_unreachable"
            assert isinstance(coordinator.last_exception, IdleAfterFailure)

        # Nothing was requested and nothing counted as another failure.
        assert client.get_resources_bulk.await_count == 1
        assert coordinator._refresh_outage.failures == outage_failures
        assert coordinator._poll_failures == poll_failures
        # The time without the cloud keeps running (paired counters).
        assert coordinator._unreachable_since == since
        assert "recovered" not in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

        # Only an answered request ends the failure.
        client.get_resources_bulk.return_value = (_success(CORE, 2.0),)
        now.now = 1000.0 + GATEWAY_TIMEOUT_PAUSE.total_seconds() + 60
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert not coordinator.cloud_unavailable()
    assert coordinator.data[CORE].resource.value == 2.0


async def test_idle_cycle_while_successful_stays_successful(
    hass: HomeAssistant,
) -> None:
    """B03: without a failure before it, an idle cycle is a normal cycle."""
    client = AsyncMock()
    coordinator = _polling(hass, client, CORE)
    client.get_resources_bulk.return_value = (_success(CORE, 2.0),)
    with clock() as now:
        await coordinator.async_refresh()
        now.now += 10  # FAST is not due again yet
        await coordinator.async_refresh()

    assert client.get_resources_bulk.await_count == 1
    assert coordinator.last_update_success
    assert coordinator.last_exception is None
