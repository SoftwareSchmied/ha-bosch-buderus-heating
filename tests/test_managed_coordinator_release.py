"""B04: discarded managed coordinators are released, not kept by the entry."""

import asyncio
import gc
import json
import weakref
from unittest.mock import patch

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.bosch_buderus_heating import managed_pointt
from custom_components.bosch_buderus_heating.connection_runtime import (
    RETRY_MAX_SECONDS,
)
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.managed_pointt import (
    ManagedPointTCoordinator,
)
from custom_components.bosch_buderus_heating.managed_wallbox import (
    ManagedWallboxCoordinator,
)
from tests.test_managed_pointt import Backend, eventually, running, source_for

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
ATTEMPTS = 8


def pinned(entry):
    """Coordinator shutdowns the entry keeps until it unloads."""
    return [
        job
        for job in entry._on_unload or []
        if isinstance(getattr(job, "__self__", None), DataUpdateCoordinator)
    ]


async def test_failed_source_attempts_leave_no_coordinators_alive(hass):
    backend = Backend()
    backend.status["000123"] = 500  # the discovery of one gateway keeps failing
    created = []
    original = ManagedPointTCoordinator.__init__
    retry_pauses: list[float] = []

    async def no_retry_pause(delay):
        # The HTTP retry pause of each failing read is recorded, not waited.
        retry_pauses.append(delay)
        await asyncio.sleep(0)

    def tracked(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(weakref.ref(self))
        self.client._transport._sleep = no_retry_pause

    with patch.object(ManagedPointTCoordinator, "__init__", tracked):
        async with running(hass, backend) as (entry, _, owner, harness):
            await eventually(lambda: source_for(owner, "gateway-b"))
            await eventually(lambda: harness.sleeps.qsize() >= 1)
            before = len(entry._on_unload or [])
            harness.retry.set()  # every retry pause ends at once
            await eventually(lambda: harness.sleeps.qsize() >= ATTEMPTS, timeout=60)
            harness.retry.clear()
            await asyncio.sleep(0.2)
            delays = [harness.sleeps.get_nowait() for _ in range(ATTEMPTS)]

            # B04 option 3: the retries of the same source wait longer.
            assert delays[:6] == [60, 120, 240, 480, 960, RETRY_MAX_SECONDS]
            # Each failed attempt still retried its read once over HTTP.
            assert len(retry_pauses) >= ATTEMPTS
            # B04 option 1: no coordinator shutdown is kept on the entry.
            assert not pinned(entry)
            assert len(entry._on_unload or []) <= before
            working = source_for(owner, "gateway-b")
            assert working.config_entry is entry
            # The test's client patch records every call, with the source.
            managed_pointt.PointTClient.reset_mock()
            gc.collect()
            alive = [
                coordinator
                for ref in created
                if (coordinator := ref()) is not None
                and coordinator.gateway.gateway_id == "000123"
            ]
            attempts = sum(
                ref() is None or ref().gateway.gateway_id == "000123" for ref in created
            )
            assert attempts >= ATTEMPTS
            # At most the last failed attempt (its loop waits for the retry)
            # and the one that may still run; never one per attempt.
            assert len(alive) <= 2
            del alive

            # B08 option 2: the failures are counted per source.
            result = await async_get_config_entry_diagnostics(hass, entry)
            failing = [
                source
                for source in result["sources"]
                if source["failures"]["failures"] >= ATTEMPTS
            ]
            assert len(failing) == 1
            assert failing[0]["failures"]["last_failure"] == "SourceFailure"
            assert failing[0]["failures"]["last_failure_stage"] == "starting"
            assert failing[0]["failures"]["last_failure_phase"] == "unavailable"
            assert "000123" not in json.dumps(result)


async def test_managed_wallbox_is_released_with_its_source(hass):
    backend = Backend()
    backend.wallbox = "gateway-b"
    async with running(hass, backend) as (entry, _, owner, _):
        await eventually(lambda: source_for(owner, "gateway-b"))
        wallbox = source_for(owner, "gateway-b")
        assert isinstance(wallbox, ManagedWallboxCoordinator)
        assert wallbox.config_entry is entry
        assert not pinned(entry)
        assert not wallbox._shutdown_requested
    # Its source, not the entry, shut it down when the runtime closed.
    assert wallbox._shutdown_requested
    assert not wallbox.last_update_success
