"""P16: confirmed cloud writes and local readings of the same circuit.

The tests run real ``async_write_control`` calls and real local observations;
only the PointT write service and the local source driver are replaced.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.coordinator import SnapshotSource
from custom_components.bosch_buderus_heating.local_selection import (
    MAX_WRITE_SCOPES,
    WRITE_CHECK_SECONDS,
    LocalSelection,
    write_scope,
)
from custom_components.bosch_buderus_heating.pointt import (
    PointTError,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.writes import (
    WriteRequest,
    WriteResult,
    number_policy_for_resource,
)
from tests.test_local_api import read, scalar
from tests.test_local_selection import LOCAL_BINDING, TARGET, FakeRuntime
from tests.test_validity import _coordinator

SETPOINT = "/heatingCircuits/hc1/manualRoomSetpoint"
CURRENT = "/heatingCircuits/hc1/currentRoomSetpoint"
OTHER = "/heatingCircuits/hc2/currentRoomSetpoint"
MAX_FLOW = "/heatingCircuits/hc1/maxFlowTemp"
WRITABLE = ResourceMetadata(
    resource_type="floatValue", unit="C", minimum=5, maximum=30, writable=True
)
READ_ONLY = ResourceMetadata(resource_type="floatValue", unit="C")


class Driver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def refresh(self, paths: tuple[str, ...]) -> int:
        self.calls.append(paths)
        return len(paths)


class Runtime(FakeRuntime):
    def __init__(self, observations=(), **kwargs) -> None:
        super().__init__(observations, **kwargs)
        self.driver = Driver()

    def source_driver(self, binding_id: str):
        return self.driver if binding_id == LOCAL_BINDING else None


def local(path: str, value: float, at: datetime, monotonic: float):
    return read(scalar(path, value=value), observed_at=at, observed_monotonic=monotonic)


def setup(hass: HomeAssistant, *, cloud_received: datetime | None = None):
    before = datetime.now(UTC) - timedelta(seconds=30)
    coordinator = _coordinator(
        hass,
        Resource(SETPOINT, value=20.0, has_value=True, metadata=WRITABLE),
        Resource(CURRENT, value=20.0, has_value=True, metadata=READ_ONLY),
        Resource(OTHER, value=19.0, has_value=True, metadata=READ_ONLY),
        Resource(MAX_FLOW, value=55.0, has_value=True, metadata=WRITABLE),
        received=cloud_received or before - timedelta(seconds=30),
    )
    runtime = Runtime(
        [local(CURRENT, 20.0, before, 1.0), local(OTHER, 19.5, before, 1.0)]
    )
    coordinator.local_selection = LocalSelection(runtime, TARGET)
    return coordinator, runtime


async def write(coordinator, path: str, value: float) -> Resource:
    current = coordinator.data[path].resource
    confirmed = replace(current, value=value)
    coordinator._write_service = AsyncMock()
    coordinator._write_service.async_write_number.return_value = WriteResult(
        WriteRequest("gateway-one", path, value), confirmed
    )
    policy = number_policy_for_resource(current)
    assert policy is not None
    return await coordinator.async_write_control(path, value, policy)


def cloud_reads_again(coordinator, path: str, value: float) -> None:
    """A regular cloud cycle after the write delivers the dependent value."""
    snapshot = coordinator.data[path]
    coordinator.data[path] = replace(
        snapshot,
        resource=replace(snapshot.resource, value=value),
        last_success=datetime.now(UTC),
    )


def shown(coordinator, path: str):
    snapshot = coordinator.display_snapshot(path)
    return snapshot.source, snapshot.resource.value


def test_write_scope_is_the_circuit() -> None:
    assert write_scope(SETPOINT) == "/heatingCircuits/hc1"
    assert write_scope("/dhwCircuits/dhw1/operationMode") == "/dhwCircuits/dhw1"
    assert write_scope("/system/x") == "/system/x"


async def test_old_local_reading_loses_against_a_newer_cloud_reading(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = setup(hass)
    assert shown(coordinator, CURRENT) == (SnapshotSource.LOCAL, 20.0)

    await write(coordinator, SETPOINT, 21.5)
    # The cloud has not read the dependent value again: the local one is the
    # newer of two readings from before the write and stays.
    assert shown(coordinator, CURRENT)[0] is SnapshotSource.LOCAL
    cloud_reads_again(coordinator, CURRENT, 21.5)
    source, value = shown(coordinator, CURRENT)
    assert source is not SnapshotSource.LOCAL and value == 21.5
    # Another circuit is not affected.
    assert shown(coordinator, OTHER) == (SnapshotSource.LOCAL, 19.5)
    summary = coordinator.local_selection.summary(coordinator.data)
    assert summary["cloud_reasons"] == {"older_than_write": 1}
    assert summary["cloud_reason_measurements"] == {
        "older_than_write": ["room_setpoint_current"]
    }

    # A newer local reading takes over again.
    runtime.observations[0] = local(CURRENT, 21.5, datetime.now(UTC), 2.0)
    assert shown(coordinator, CURRENT) == (SnapshotSource.LOCAL, 21.5)
    assert coordinator.local_selection.summary(coordinator.data)["cloud_reasons"] == {}


async def test_a_confirmed_write_asks_the_local_source_to_read_the_circuit(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = setup(hass)
    runtime.observations.append(local(MAX_FLOW, 55.0, datetime.now(UTC), 1.0))
    await write(coordinator, SETPOINT, 21.5)
    # Readings of hc1 only; the written setting itself has no catalog pair.
    assert runtime.driver.calls == [(CURRENT, MAX_FLOW)]


async def test_no_local_request_while_local_values_are_off(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = setup(hass)
    runtime.snapshot.enabled = False
    await write(coordinator, SETPOINT, 21.5)
    assert runtime.driver.calls == []


async def test_failed_write_changes_nothing_for_local_readings(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = setup(hass)
    cloud_reads_again(coordinator, CURRENT, 20.0)
    coordinator._write_service = AsyncMock()
    coordinator._write_service.async_write_number.side_effect = PointTError("x")
    policy = number_policy_for_resource(coordinator.data[SETPOINT].resource)
    with pytest.raises(PointTError):
        await coordinator.async_write_control(SETPOINT, 21.5, policy)
    assert runtime.driver.calls == []
    assert shown(coordinator, CURRENT)[0] is SnapshotSource.LOCAL


async def test_write_of_a_paired_setting_is_checked_against_local_readback(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = setup(hass)
    runtime.observations.append(local(MAX_FLOW, 55.0, datetime.now(UTC), 1.0))
    selection = coordinator.local_selection
    await write(coordinator, MAX_FLOW, 50.0)
    # The reading from before the write does not count.
    selection.check_writes(datetime.now(UTC))
    assert selection.write_check_summary() == {}
    runtime.observations[-1] = local(
        MAX_FLOW, 55.0, datetime.now(UTC) + timedelta(seconds=1), 3.0
    )
    coordinator.async_update_listeners()  # a new local reading arrives
    assert selection.write_check_summary() == {"max_flow_temperature": {"differ": 1}}
    # Counted once, never repeated or written again.
    selection.check_writes(datetime.now(UTC))
    assert selection.write_check_summary() == {"max_flow_temperature": {"differ": 1}}
    coordinator._write_service.async_write_number.assert_awaited_once()


async def test_agreeing_and_missing_readbacks_are_counted(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = setup(hass)
    runtime.observations.append(local(MAX_FLOW, 55.0, datetime.now(UTC), 1.0))
    selection = coordinator.local_selection
    await write(coordinator, MAX_FLOW, 50.0)
    runtime.observations[-1] = local(
        MAX_FLOW, 50.0, datetime.now(UTC) + timedelta(seconds=1), 3.0
    )
    selection.check_writes(datetime.now(UTC))
    # The next write sees only the reading from before it.
    runtime.observations[-1] = local(
        MAX_FLOW, 50.0, datetime.now(UTC) - timedelta(seconds=5), 4.0
    )
    await write(coordinator, MAX_FLOW, 45.0)
    selection.check_writes(
        datetime.now(UTC) + timedelta(seconds=WRITE_CHECK_SECONDS + 1)
    )
    assert selection.write_check_summary() == {
        "max_flow_temperature": {"agree": 1, "unchecked": 1}
    }
    assert selection.summary(coordinator.data)["write_checks"] == {
        "max_flow_temperature": {"agree": 1, "unchecked": 1}
    }


def test_write_bookkeeping_stays_bounded() -> None:
    selection = LocalSelection(Runtime(), TARGET)
    base = datetime.now(UTC)
    for index in range(MAX_WRITE_SCOPES + 5):
        snapshot = type("S", (), {"last_success": base + timedelta(seconds=index)})()
        selection.note_confirmed_write(f"/zones/z{index}/setpoint", snapshot)
    assert len(selection._confirmed_writes) == MAX_WRITE_SCOPES
    assert "/zones/z0" not in selection._confirmed_writes


async def test_a_failing_local_refresh_never_fails_a_confirmed_write(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = setup(hass)

    def broken(paths):
        raise RuntimeError("local source broke")

    runtime.driver.refresh = broken
    confirmed = await write(coordinator, SETPOINT, 21.5)
    assert confirmed.value == 21.5
    assert coordinator.data[SETPOINT].resource.value == 21.5
    assert coordinator.data[SETPOINT].source is SnapshotSource.WRITE


async def test_no_write_check_without_a_local_reading_or_with_the_option_off(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = setup(hass)
    selection = coordinator.local_selection
    await write(coordinator, MAX_FLOW, 50.0)  # no local reading of it
    runtime.observations.append(local(MAX_FLOW, 50.0, datetime.now(UTC), 1.0))
    runtime.snapshot.enabled = False
    await write(coordinator, MAX_FLOW, 45.0)  # option off
    selection.check_writes(
        datetime.now(UTC) + timedelta(seconds=WRITE_CHECK_SECONDS + 1)
    )
    assert selection.write_check_summary() == {}
    # The written setting is only read again when the source already reads it.
    runtime.snapshot.enabled = True
    runtime.observations.pop()
    runtime.driver.calls.clear()
    await write(coordinator, MAX_FLOW, 40.0)
    assert runtime.driver.calls == [(CURRENT,)]


async def test_replaced_and_late_read_backs_count_as_unchecked(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = setup(hass)
    runtime.observations.append(local(MAX_FLOW, 55.0, datetime.now(UTC), 1.0))
    selection = coordinator.local_selection
    await write(coordinator, MAX_FLOW, 50.0)
    await write(coordinator, MAX_FLOW, 45.0)  # before the first read-back
    assert selection.write_check_summary() == {"max_flow_temperature": {"unchecked": 1}}
    late = datetime.now(UTC) + timedelta(seconds=WRITE_CHECK_SECONDS + 5)
    runtime.observations[-1] = local(MAX_FLOW, 45.0, late, 9.0)
    selection.check_writes(late)
    assert selection.write_check_summary() == {"max_flow_temperature": {"unchecked": 2}}
