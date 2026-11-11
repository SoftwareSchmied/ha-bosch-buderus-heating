"""I1: the load profile of iCom gateways in real polling cycles.

Every rule applies only to a gateway that reports the device type ``icom``;
the regression cases show that every other gateway keeps its behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.icom_profile import (
    ICOM_READ_BACK_DELAYS,
    AdaptiveBulkSize,
    GrowingCircuitPause,
    any_icom_gateway,
    is_icom_device_type,
)
from custom_components.bosch_buderus_heating.legacy_holiday_writes import (
    IcomHolidayWriteService,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    RequestTimeout,
    WriteNotConfirmed,
)
from custom_components.bosch_buderus_heating.pointt.metrics import RequestMetrics
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from custom_components.bosch_buderus_heating.writes import WriteService
from tests.ha_test_helpers import MockConfigEntry
from tests.test_cloud_setup import backend as backend
from tests.test_cloud_setup import new_setup
from tests.test_coordinator import _resource, _snapshot, _success
from tests.test_coordinator_outages import clock

# Readings without a core fallback: a failed batch ends the cycle.
PATHS = tuple(f"/system/sensors/temperatures/synthetic{n:02}" for n in range(40))


def _polling(
    hass: HomeAssistant, client: AsyncMock, device_type: str | None
) -> BoschBuderusDataUpdateCoordinator:
    client.metrics = RequestMetrics()
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway-one", device_type=device_type), entry
    )
    coordinator.resources = {path: _resource(path, 1.0) for path in PATHS}
    coordinator.data = {
        path: _snapshot(resource) for path, resource in coordinator.resources.items()
    }
    coordinator._paths_by_group = {PollGroup.FAST: PATHS}
    return coordinator


def _answer(_gateway: str, paths: tuple[str, ...]):
    return tuple(_success(path, 2.0) for path in paths)


def _sizes(client: AsyncMock, start: int = 0) -> list[int]:
    return [len(call.args[1]) for call in client.get_resources_bulk.await_args_list][
        start:
    ]


def test_bulk_size_halves_once_per_timeout_and_recovers_step_by_step() -> None:
    bulk = AdaptiveBulkSize()
    sizes = []
    for _ in range(3):
        bulk.record_timeout()
        sizes.append(bulk.size)
    assert sizes == [15, 10, 10]
    for _ in range(2):
        bulk.record_good_cycle()
    assert bulk.size == 10
    # A timeout starts the count of good cycles again.
    bulk.record_timeout()
    for _ in range(2):
        bulk.record_good_cycle()
    assert bulk.size == 10
    bulk.record_good_cycle()
    assert bulk.size == 20
    for _ in range(3):
        bulk.record_good_cycle()
    assert bulk.size == 30
    for _ in range(5):
        bulk.record_good_cycle()
    assert bulk.size == 30
    assert bulk.diagnostics()["bulk_size_reductions"] == 2


def test_breaker_pause_grows_to_thirty_minutes_and_resets() -> None:
    pause = GrowingCircuitPause()
    assert [pause.next_pause_seconds() / 60 for _ in range(6)] == [
        5,
        10,
        20,
        30,
        30,
        30,
    ]
    pause.reset()
    assert pause.next_pause_seconds() == 300


@pytest.mark.parametrize(
    ("device_type", "expected"),
    [
        ("icom", True),
        (" ICOM ", True),
        ("rrc2", False),
        ("MX300", False),
        (None, False),
    ],
)
def test_only_the_reported_device_type_counts(device_type, expected) -> None:
    assert is_icom_device_type(device_type) is expected
    gateways = (Gateway("a", device_type=device_type), Gateway("b", device_type="k40"))
    assert any_icom_gateway(gateways, {"a"}) is expected
    assert any_icom_gateway(gateways, {"b"}) is False


@pytest.mark.parametrize(
    ("device_type", "after_timeout", "recovered"),
    [
        ("icom", [15, 15, 10], [30, 10]),
        # Regression: every other gateway keeps the batches of 30.
        ("MX300", [30, 10], [30, 10]),
        (None, [30, 10], [30, 10]),
    ],
)
async def test_timeout_halves_the_next_cycle_only_on_icom(
    hass: HomeAssistant, device_type, after_timeout, recovered
) -> None:
    client = AsyncMock()
    coordinator = _polling(hass, client, device_type)
    client.get_resources_bulk.side_effect = RequestTimeout("timeout")
    with clock() as now:
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        assert _sizes(client) == [30]

        client.get_resources_bulk.side_effect = _answer
        now.now += 60
        seen = client.get_resources_bulk.await_count
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert _sizes(client, seen) == after_timeout

        # Three good cycles later the size has doubled back (15 -> 30).
        for _ in range(3):
            now.now += 60
            await coordinator.async_refresh()
        now.now += 60
        seen = client.get_resources_bulk.await_count
        await coordinator.async_refresh()
    assert _sizes(client, seen) == recovered


async def test_fault_sources_stay_one_batch_with_a_smaller_size(
    hass: HomeAssistant,
) -> None:
    """FAULT-1: the halved size never splits the fault sources."""
    client = AsyncMock()
    coordinator = _polling(hass, client, "icom")
    faults = tuple(f"/heatSources/hs{n}/activefailure" for n in range(1, 13))
    for path in faults:
        coordinator.resources[path] = _resource(path, 1.0)
        coordinator.data[path] = _snapshot(coordinator.resources[path])
    coordinator._paths_by_group = {
        PollGroup.NOTIFICATIONS: faults,
        PollGroup.FAST: PATHS,
    }
    for _ in range(2):
        coordinator._icom_bulk.record_timeout()
    client.get_resources_bulk.side_effect = _answer
    with clock():
        await coordinator.async_refresh()
    calls = [call.args[1] for call in client.get_resources_bulk.await_args_list]
    assert tuple(calls[0]) == faults
    assert [len(paths) for paths in calls[1:]] == [10, 10, 10, 10]


@pytest.mark.parametrize(
    ("device_type", "pauses"),
    [
        ("icom", [5, 10, 20, 30, 30]),
        # Regression: the breaker of every other gateway stays at 5 minutes.
        ("MX300", [5, 5, 5, 5, 5]),
    ],
)
async def test_breaker_pause_grows_during_an_outage_only_on_icom(
    hass: HomeAssistant, device_type, pauses
) -> None:
    client = AsyncMock()
    coordinator = _polling(hass, client, device_type)
    client.get_resources_bulk.side_effect = RequestTimeout("timeout")
    seen = []
    with clock() as now:
        for _ in range(coordinator_module.CIRCUIT_FAILURE_THRESHOLD - 1):
            await coordinator.async_refresh()
            now.now += 60
        for _ in pauses:
            await coordinator.async_refresh()
            seen.append(round((coordinator._circuit_open_until - now.now) / 60))
            now.now = coordinator._circuit_open_until + 1
        # The first usable cycle closes the breaker; the next outage starts
        # again at 5 minutes.
        client.get_resources_bulk.side_effect = _answer
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        client.get_resources_bulk.side_effect = RequestTimeout("timeout")
        for _ in range(coordinator_module.CIRCUIT_FAILURE_THRESHOLD):
            now.now += 60
            await coordinator.async_refresh()
        again = round((coordinator._circuit_open_until - now.now) / 60)
    assert seen == pauses
    assert again == 5


@pytest.mark.parametrize(
    ("device_type", "delays"),
    [
        ("icom", ICOM_READ_BACK_DELAYS),
        ("MX300", (0.5, 1.0, 2.0)),
        (None, (0.5, 1.0, 2.0)),
    ],
)
async def test_read_back_schedule_follows_the_device_type(
    hass: HomeAssistant, device_type, delays
) -> None:
    coordinator = _polling(hass, AsyncMock(), device_type)
    assert coordinator.write_read_back_delays() == delays
    assert coordinator._icom_holiday_write_service._read_back_delays == delays
    diagnostics = coordinator.icom_profile_diagnostics()
    assert (diagnostics is not None) is (device_type == "icom")


async def test_icom_write_is_read_back_after_one_to_four_seconds() -> None:
    sleep = AsyncMock()
    service = WriteService(
        AsyncMock(), sleep=sleep, read_back_schedule=ICOM_READ_BACK_DELAYS
    )
    assert service.read_back_delays(None) == (1.0, 2.0, 3.0, 4.0)
    assert sum(service.read_back_delays(None)) == 10
    with pytest.raises(ValueError):
        WriteService(AsyncMock(), read_back_schedule=())
    with pytest.raises(ValueError):
        WriteService(AsyncMock(), read_back_schedule=(1.0, -1.0))


async def test_icom_holiday_read_back_waits_one_to_four_seconds() -> None:
    from dataclasses import replace

    from custom_components.bosch_buderus_heating.holidays import icom_write_values
    from tests.test_legacy_holidays import RESOURCES, SLOT

    sleep = AsyncMock()
    client = AsyncMock()

    async def get(_gateway, path):
        return RESOURCES[path]

    client.get_resource.side_effect = get
    service = IcomHolidayWriteService(
        client, sleep=sleep, read_back_delays=ICOM_READ_BACK_DELAYS
    )
    baseline = icom_write_values(RESOURCES, SLOT)
    with pytest.raises(WriteNotConfirmed):
        await service.async_write(
            "gateway-one",
            RESOURCES,
            "update",
            SLOT,
            replace(baseline, dhw_mode="LOW"),
            baseline,
        )
    assert [call.args[0] for call in sleep.await_args_list] == [1.0, 2.0, 3.0, 4.0]
    with pytest.raises(ValueError):
        IcomHolidayWriteService(client, read_back_delays=())


@pytest.mark.parametrize(
    ("device_type", "profile"),
    [("icom", "cloud_friendly"), ("rrc2", "standard"), (None, "standard")],
)
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_new_connection_with_icom_starts_cloud_friendly(
    hass: HomeAssistant, backend, device_type, profile
) -> None:
    backend.gateways.return_value = (
        Gateway("000123", device_type=device_type),
        Gateway("gateway-b", device_type="rrc2"),
    )
    result = await new_setup(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_ids": ["000123"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    (connection,) = entry.options["connections"].values()
    assert connection["polling_profile"] == profile
