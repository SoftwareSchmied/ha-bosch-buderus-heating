"""P22: a firmware change while running checks the capabilities again."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.discovery_inventory import (
    RECOVERY_SCHEDULE,
)
from custom_components.bosch_buderus_heating.pointt import (
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.resource_catalog import (
    FIRMWARE_PATHS,
    PollGroup,
    poll_group,
)
from tests.test_discovery_recovery import (
    COORDINATOR,
    Clock,
    Cloud,
    _coordinator,
    _inventory,
    container,
    leaf,
)

FIRMWARE = "/gateway/versionFirmware"
MODULATION = "/heatSources/actualModulation"
NEW = "/heatSources/compressorStatus"


def version(value: str) -> Resource:
    return Resource(
        FIRMWARE,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue"),
    )


def plant(firmware: str = "5.27") -> dict[str, Resource]:
    return {
        "/gateway": container("/gateway", FIRMWARE),
        FIRMWARE: version(firmware),
        "/heatSources": container("/heatSources", MODULATION),
        MODULATION: leaf(MODULATION, 40.0, "%"),
    }


@pytest.fixture
def clock():
    clock = Clock()
    with (
        patch(f"{COORDINATOR}.monotonic", clock),
        patch(f"{COORDINATOR}.fault_resource_candidates", return_value=()),
    ):
        yield clock


def test_firmware_versions_are_read_with_the_slow_group() -> None:
    for path in FIRMWARE_PATHS:
        resource = Resource(
            path, value="1", has_value=True, metadata=ResourceMetadata("stringValue")
        )
        assert poll_group(resource) is PollGroup.SLOW


async def test_firmware_change_while_running_discovers_once(
    hass: HomeAssistant, clock: Clock, caplog
) -> None:
    cloud = Cloud(plant())
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    assert coordinator.firmware.seen == {FIRMWARE: "5.27"}

    # Same version on the next slow poll: nothing happens.
    clock.now += 15 * 60
    await coordinator.async_refresh()
    assert coordinator.firmware.due is None

    # The gateway updated itself and offers a new resource.
    cloud.tree = plant("5.31")
    cloud.tree["/heatSources"] = container("/heatSources", MODULATION, NEW)
    cloud.tree[NEW] = Resource(
        NEW,
        value="heating",
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue"),
    )
    clock.now += 15 * 60
    with caplog.at_level(logging.INFO):
        await coordinator.async_refresh()
    assert "firmware changed while running" in caplog.text
    assert "5.31" not in caplog.text and "5.27" not in caplog.text
    assert NEW not in coordinator.resources  # discovered in the next cycle
    clock.now += 60
    with caplog.at_level(logging.INFO):
        await coordinator.async_refresh()
    assert NEW in coordinator.resources
    report = coordinator.firmware.diagnostics(clock.now)
    assert report["changes_while_running"] == 1
    assert report["discovery_result"] == "success"
    assert report["next_discovery_in_seconds"] is None
    assert "capabilities checked again after a firmware change" in caplog.text

    # No further discovery for the same version.
    sent = len(cloud.requests)
    clock.now += 15 * 60
    await coordinator.async_refresh()
    assert coordinator.firmware.due is None
    assert all(
        path in coordinator.resources
        for paths in cloud.requests[sent:]
        for path in paths
    )


async def test_firmware_discovery_waits_for_the_brake_and_retries(
    hass: HomeAssistant, clock: Clock
) -> None:
    from custom_components.bosch_buderus_heating.pointt import RequestTimeout

    cloud = Cloud(plant())
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    cloud.tree[FIRMWARE] = version("5.31")
    clock.now += 15 * 60
    await coordinator.async_refresh()
    watch = coordinator.firmware
    assert watch.due is not None

    # The discovery meets a rate limit: postponed, no attempt used.
    cloud.down["/gateway"] = 429
    clock.now += 60
    await coordinator.async_refresh()
    assert watch.result == "postponed" and watch.attempts == 0
    assert watch.due == coordinator._cloud_backoff_until
    sent = len(cloud.requests)
    clock.now += 60  # still within the brake: nothing is sent
    await coordinator.async_refresh()
    assert len(cloud.requests) == sent

    # After the brake it fails once more (timeout) and is retried later.
    del cloud.down["/gateway"]
    cloud.request_errors["/gateway"] = RequestTimeout()  # the whole request fails
    clock.now = coordinator._cloud_backoff_until + 1
    await coordinator.async_refresh()  # a working poll first
    clock.now += 60
    await coordinator.async_refresh()
    assert watch.result == "failed" and watch.attempts == 1
    assert (
        watch.diagnostics(clock.now)["next_discovery_in_seconds"]
        == (RECOVERY_SCHEDULE[0])
    )
    assert coordinator.last_update_success  # the values stay


async def test_change_seen_together_with_a_rate_limit_waits_for_the_brake(
    hass: HomeAssistant, clock: Clock
) -> None:
    cloud = Cloud(plant())
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    cloud.tree[FIRMWARE] = version("5.31")
    cloud.down[MODULATION] = 429  # same cycle: one item is rate limited
    clock.now += 15 * 60
    await coordinator.async_refresh()
    assert coordinator.firmware.due is not None
    assert coordinator._cloud_backoff_until > clock.now
    del cloud.down[MODULATION]
    sent = len(cloud.requests)
    clock.now += 60
    await coordinator.async_refresh()
    assert len(cloud.requests) == sent  # no discovery during the brake


async def test_firmware_change_since_the_last_start_is_reported(
    hass: HomeAssistant, clock: Clock, caplog
) -> None:
    inventory = _inventory(hass)
    first = _coordinator(hass, Cloud(plant("5.27")), inventory)
    await first.async_refresh()
    with caplog.at_level(logging.INFO):
        restarted = _coordinator(hass, Cloud(plant("5.31")), inventory)
        await restarted.async_refresh()
    report = restarted.firmware.diagnostics(clock.now)
    assert report["changed_since_last_start"] is True
    assert report["changes_while_running"] == 0
    assert restarted.firmware.due is None  # start-up discovery already checked
    assert "firmware changed since the last start" in caplog.text


def test_only_well_formed_versions_are_compared() -> None:
    from custom_components.bosch_buderus_heating.firmware_watch import (
        firmware_values,
    )

    resources = {
        FIRMWARE: version("x" * 65),
        "/system/appliance/versionFirmware": Resource(
            "/system/appliance/versionFirmware",
            value=5,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue"),
        ),
    }
    assert firmware_values(resources) == {}
    assert firmware_values({FIRMWARE: version("05.31.00")}) == {FIRMWARE: "05.31.00"}


async def test_change_since_the_last_start_is_reported_after_an_inventory_start(
    hass: HomeAssistant, clock: Clock
) -> None:
    from datetime import UTC, datetime

    inventory = _inventory(hass)
    first = _coordinator(hass, Cloud(plant("5.27")), inventory)
    await first.async_refresh()
    stored = inventory.resources(first.gateway.gateway_id)
    restarted = _coordinator(hass, Cloud(plant("5.31")), inventory)
    restarted.start_from_inventory(
        stored,
        reason="gateway_list_timeout",
        age_seconds=600,
        seen=dict.fromkeys(stored, datetime(2026, 11, 5, tzinfo=UTC)),
    )
    for _ in range(3):  # the first poll, then the discovery
        clock.now += 60
        await restarted.async_refresh()
    assert restarted.startup_inventory_diagnostics(clock.now)["discovery_result"] == (
        "success"
    )
    report = restarted.firmware.diagnostics(clock.now)
    assert report["changed_since_last_start"] is True
    assert report["changes_while_running"] == 0
    assert restarted.firmware.due is None


async def test_failing_firmware_discovery_follows_the_schedule_and_gives_up(
    hass: HomeAssistant, clock: Clock
) -> None:
    from custom_components.bosch_buderus_heating.pointt import RequestTimeout

    cloud = Cloud(plant())
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    cloud.tree[FIRMWARE] = version("5.31")
    clock.now += 15 * 60
    await coordinator.async_refresh()
    cloud.request_errors["/gateway"] = RequestTimeout()
    watch = coordinator.firmware
    clock.now += 60
    await coordinator.async_refresh()
    first_failure = clock.now
    for attempt, offset in enumerate(RECOVERY_SCHEDULE, start=1):
        assert watch.attempts == attempt
        if attempt == len(RECOVERY_SCHEDULE):
            break
        assert watch.result == "failed"
        assert watch.due == first_failure + offset  # counted from the first failure
        clock.now = watch.due
        await coordinator.async_refresh()
    assert watch.result == "given_up" and watch.due is None
    assert clock.now - first_failure == RECOVERY_SCHEDULE[-2]
    assert coordinator.last_update_success


async def test_failed_firmware_discovery_adds_no_other_requests(
    hass: HomeAssistant, clock: Clock
) -> None:
    """The cycle of a failed discovery neither searches nor confirms (P46/P47)."""
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.bosch_buderus_heating.pointt import RequestTimeout

    cloud = Cloud(plant())
    coordinator = _coordinator(hass, cloud, _inventory(hass))
    await coordinator.async_refresh()
    cloud.tree[FIRMWARE] = version("5.31")
    clock.now += 15 * 60
    await coordinator.async_refresh()
    cloud.request_errors["/gateway"] = RequestTimeout()
    recover = AsyncMock()
    confirm = MagicMock()
    clock.now += 60
    with (
        patch.object(coordinator.discovery_recovery, "due", return_value=True),
        patch.object(coordinator, "_async_recover_missing", recover),
        patch.object(coordinator, "_confirm_inventory", confirm),
    ):
        await coordinator.async_refresh()
        assert coordinator.firmware.result == "failed"
        recover.assert_not_called()
        confirm.assert_not_called()
        assert not coordinator.startup_data.healthy
        # The next, working poll continues as usual.
        clock.now += 60
        await coordinator.async_refresh()
        recover.assert_called_once()
        confirm.assert_called_once()


async def test_firmware_discovery_waits_for_the_circuit_breaker(
    hass: HomeAssistant, clock: Clock
) -> None:
    cloud = Cloud(plant())
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    cloud.tree[FIRMWARE] = version("5.31")
    clock.now += 15 * 60
    await coordinator.async_refresh()
    coordinator._circuit_open_until = clock.now + 600
    clock.now += 60
    await coordinator.async_refresh()
    assert coordinator.firmware.attempts == 0
    assert cloud.requested("/gateway") == 1  # only the start-up discovery


# --------------------------------------------------------------------- local


@pytest.mark.parametrize(
    ("new", "blocked"),
    [("14.09.00", True), ("15.01.00", False), ("16.0.0-beta", False)],
)
async def test_local_firmware_change_below_the_minimum_pauses_access(
    hass, tmp_path, monkeypatch, new, blocked
) -> None:
    from types import MappingProxyType

    from custom_components.bosch_buderus_heating.local_api.catalog import (
        request_for_path,
    )
    from custom_components.bosch_buderus_heating.local_budget import LocalReadBudget
    from custom_components.bosch_buderus_heating.local_polling import service as module
    from custom_components.bosch_buderus_heating.local_polling.service import (
        LocalPollingService,
    )
    from custom_components.bosch_buderus_heating.local_polling.storage import (
        LocalScheduleStore,
    )
    from custom_components.bosch_buderus_heating.local_state import LocalCondition
    from tests.test_local_budget import Clock as LocalClock
    from tests.test_local_polling import no_discovery, success
    from tests.test_local_polling_service import Client, advance, settle
    from tests.test_local_probe import CONTEXT

    monkeypatch.setattr(module, "start_offset", lambda scope: 0)
    clock = LocalClock()
    client = Client(clock)
    request = request_for_path(FIRMWARE)
    versions = iter(["15.00.01", new])

    def read(req):
        value = next(versions, new)
        return success(
            req,
            clock(),
            value=value,
            document=MappingProxyType({"id": FIRMWARE, "value": value}),
        )

    client.read = lambda req: _respond(client, read(req))
    store = LocalScheduleStore(hass, "synthetic-scope")
    store._path = tmp_path / "schedule.json"
    service = LocalPollingService(
        CONTEXT,
        client,
        LocalReadBudget(CONTEXT, clock=clock),
        store,
        is_current=lambda: True,
        publish=lambda snapshot: None,
        clock=clock,
    )
    no_discovery(service.planner)
    service.planner.hints((request,), clock())
    try:
        service.confirm_identity(CONTEXT.identity)
        await service.start()
        await settle()
        assert len(client.calls) == 1

        async def within(condition, seconds=5.0):
            # Real time, not only loop turns: the service may wait for its
            # store in an executor thread, which takes longer under load.
            for _ in range(int(seconds / 0.01)):
                if condition():
                    return True
                await asyncio.sleep(0.01)
            return condition()

        for _ in range(10):  # until the firmware is read again
            await advance(service, clock, 3600)
            if await within(lambda: len(client.calls) > 1, 1.0):
                break
        assert len(client.calls) >= 2
        # The service takes the new version and decides on the pause in the
        # same step; waiting for the version avoids waiting out a pause that
        # must not come.
        assert await within(lambda: service.planner.firmware == new, 2.0)
        assert (service.condition is LocalCondition.UNAVAILABLE) is blocked
        assert (service.snapshot().problem == "firmware_update_required") is blocked
    finally:
        await service.close()


async def _respond(client, result):
    client.calls.append(result.request)
    return result
