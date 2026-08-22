"""Tests for dynamic gateway discovery and polling."""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import (
    DOMAIN,
    RATE_LIMIT_ISSUE_PREFIX,
    PollingProfile,
)
from custom_components.bosch_buderus_heating.coordinator import (
    ACTIVE_NOTIFICATION_INTERVAL,
    CIRCUIT_FAILURE_THRESHOLD,
    FORBIDDEN_PAUSE_THRESHOLD,
    POLL_INTERVALS,
    POLL_INTERVALS_CLOUD_FRIENDLY,
    RATE_LIMIT_REPAIR_THRESHOLD,
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
    SnapshotSource,
    _energy_reset_count,
    _error_category,
)
from custom_components.bosch_buderus_heating.holidays import (
    HOLIDAY_CONFIGURATION_PATH,
    HOLIDAY_LIST_PATH,
    HolidayWriteValues,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    BatchItemResult,
    Gateway,
    RateLimited,
    RequestMetrics,
    Resource,
    ResourceError,
    ResourceForbidden,
    ResourceMetadata,
    ResourceNotFound,
    ResourceReference,
    ServiceUnavailable,
    WriteRequest,
    WriteResult,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from custom_components.bosch_buderus_heating.writes import (
    HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    number_policy_for_resource,
)


def _coordinator(
    hass: HomeAssistant, client: AsyncMock
) -> BoschBuderusDataUpdateCoordinator:
    client.metrics = RequestMetrics()
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    return BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway-one", device_type="MX300"), entry
    )


def _resource(path: str, value: float, *, writable: bool = False) -> Resource:
    return Resource(
        path=path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(writable=writable),
    )


def _success(path: str, value: float) -> BatchItemResult:
    return BatchItemResult(
        gateway_id="gateway-one",
        path=path,
        status=200,
        resource=_resource(path, value),
    )


async def test_first_refresh_discovers_and_classifies_resources(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    fast = _resource("/heatSources/actualModulation", 10.0)
    slow = Resource(
        path="/heatSources/hs1/workingTime",
        values=({"total": 3600},),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    static = _resource("/gateway/versionFirmware", 1.0)
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.async_discover_resources",
        AsyncMock(return_value={item.path: item for item in (fast, slow, static)}),
    ):
        result = await coordinator._async_update_data()

    assert set(result) == {fast.path, slow.path, static.path}
    assert coordinator._paths_by_group[PollGroup.FAST] == (fast.path,)
    assert coordinator._paths_by_group[PollGroup.SLOW] == (slow.path,)
    assert coordinator.resources[static.path] is static
    assert coordinator.capability_metrics(fast.path)["attempts_by_source"] == {
        "discovery": 1
    }


async def test_discovery_adds_fault_sources_and_adapts_notification_polling(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    notifications = Resource(
        path="/notifications",
        values=({"ccd": 6249, "fc": "12"},),
        metadata=ResourceMetadata(resource_type="errorList"),
    )
    heat_sources = Resource(
        path="/heatSources",
        references=(ResourceReference("/heatSources/hs1"),),
    )
    heat_source = Resource(path="/heatSources/hs1")
    active_failure = "/heatSources/hs1/activefailure"
    failure_list = "/heatSources/hs1/failurelist"
    client.get_resources_bulk.return_value = (
        BatchItemResult(
            "gateway-one",
            active_failure,
            404,
            error=ResourceNotFound(active_failure, 404),
        ),
        BatchItemResult(
            "gateway-one",
            failure_list,
            404,
            error=ResourceNotFound(failure_list, 404),
        ),
    )
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.async_discover_resources",
        AsyncMock(
            return_value={
                item.path: item for item in (notifications, heat_sources, heat_source)
            }
        ),
    ):
        result = await coordinator._async_update_data()

    assert set(result) == {"/notifications", "/heatSources", "/heatSources/hs1"}
    assert active_failure in coordinator.resources
    assert failure_list in coordinator.resources
    assert coordinator._paths_by_group[PollGroup.NOTIFICATIONS] == (
        "/notifications",
        active_failure,
    )
    assert len(coordinator.faults.active_faults) == 1
    assert coordinator.diagnostics_summary()["notification_poll_interval_seconds"] == 60
    assert coordinator._negative_until[active_failure] > 0


async def test_optional_fault_probe_failure_does_not_block_main_discovery(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    notifications = Resource(path="/notifications", values=())
    heat_source = Resource(path="/heatSources/hs1")
    client.get_resources_bulk.side_effect = ServiceUnavailable()
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.async_discover_resources",
        AsyncMock(
            return_value={item.path: item for item in (notifications, heat_source)}
        ),
    ):
        result = await coordinator._async_update_data()

    assert set(result) == {"/notifications", "/heatSources/hs1"}
    assert coordinator.faults.has_supported_source
    assert (
        coordinator.diagnostics_summary()["notification_poll_interval_seconds"] == 300
    )


async def test_polling_preserves_last_good_values(hass: HomeAssistant) -> None:
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    fast_path = "/heatSources/actualModulation"
    slow_path = "/heatSources/hs1/workingTime"
    first = _resource(fast_path, 21.5)
    second = _resource(slow_path, 31.0)
    coordinator.resources = {fast_path: first, slow_path: second}
    coordinator._paths_by_group = {
        PollGroup.FAST: (fast_path,),
        PollGroup.SLOW: (slow_path,),
    }
    coordinator._next_update = {group: 0.0 for group in POLL_INTERVALS}
    coordinator.data = {
        fast_path: _snapshot(first),
        slow_path: _snapshot(second),
    }
    client.get_resources_bulk.return_value = (
        _success(fast_path, 22.0),
        BatchItemResult(
            gateway_id="gateway-one",
            path=slow_path,
            status=404,
            error=ResourceNotFound(slow_path, 404),
        ),
    )

    result = await coordinator._async_update_data()

    assert client.get_resources_bulk.await_args.args[1] == (fast_path, slow_path)
    assert result[fast_path].resource.value == 22.0
    assert result[slow_path].resource.value == 31.0
    assert not result[slow_path].available
    assert result[slow_path].freshness is Freshness.STALE
    assert result[slow_path].last_error_category == "http_404"
    assert result[slow_path].consecutive_failures == 1
    assert result[slow_path].last_attempt is not None
    assert coordinator.capability_metrics(fast_path) == {
        "attempts_total": 1,
        "successful": 1,
        "failed": 0,
        "success_rate_percent": 100.0,
        "results": {"success": 1},
        "attempts_by_source": {"batch": 1},
        "last_result": "success",
    }
    assert coordinator.capability_metrics(slow_path)["results"] == {"not_found": 1}
    assert coordinator.diagnostics_summary()["capability_attempts"] == {
        "total": 2,
        "successful": 1,
        "failed": 1,
        "success_rate_percent": 50.0,
    }


def _snapshot(resource: Resource) -> ResourceSnapshot:
    return ResourceSnapshot(resource, True, datetime.now(UTC))


def test_energy_reset_detection_uses_only_valid_counter_components() -> None:
    path = "/heatSources/emon/totalConsumption"
    previous = Resource(
        path=path,
        values=({"compressor": 120.0, "eheater": 4.0, "outputProduced": 350.0},),
    )
    reset = Resource(
        path=path,
        values=({"compressor": 2.0, "eheater": 4.5, "outputProduced": 3.0},),
    )

    assert _energy_reset_count(previous, reset) == 2
    assert _energy_reset_count(reset, reset) == 0
    assert (
        _energy_reset_count(
            previous,
            Resource(path="/other", values=reset.values),
        )
        == 0
    )


async def test_poll_records_energy_resets_without_changing_values(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    path = "/heatSources/emon/totalConsumption"
    metadata = ResourceMetadata(resource_type="emonValue", unit="kWh")
    previous = Resource(
        path=path,
        values=({"compressor": 120.0, "outputProduced": 350.0},),
        metadata=metadata,
    )
    current = Resource(
        path=path,
        values=({"compressor": 2.0, "outputProduced": 3.0},),
        metadata=metadata,
    )
    coordinator.resources = {path: previous}
    coordinator.data = {path: _snapshot(previous)}
    coordinator._paths_by_group = {PollGroup.ENERGY: (path,)}
    coordinator._next_update = {PollGroup.ENERGY: 0.0}
    client.get_resources_bulk.return_value = (
        BatchItemResult("gateway-one", path, 200, resource=current),
    )

    result = await coordinator._async_update_data()

    assert result[path].resource is current
    assert coordinator.diagnostics_summary()["energy_counter_resets_detected"] == 2


async def test_confirmed_write_updates_coordinator_without_batch_refresh(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    path = "/heatingCircuits/hc1/operationMode"
    current = Resource(
        path=path,
        value="manual",
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue",
            allowed_values=("off", "manual", "auto"),
            writable=True,
        ),
    )
    confirmed = Resource(
        path=path,
        value="auto",
        has_value=True,
        metadata=current.metadata,
    )
    coordinator.resources = {path: current}
    coordinator.data = {path: _snapshot(current)}
    coordinator.last_update_success = True
    coordinator._write_service = AsyncMock()
    coordinator._write_service.async_write_enum.return_value = WriteResult(
        WriteRequest("gateway-one", path, "auto"), confirmed
    )

    result = await coordinator.async_write_control(
        path, "auto", HEATING_CIRCUIT_OPERATION_MODE_POLICY
    )

    assert result is confirmed
    assert coordinator.resources[path] is confirmed
    assert coordinator.data[path].resource is confirmed
    assert coordinator.data[path].source is SnapshotSource.WRITE
    assert coordinator.capability_metrics(path)["attempts_by_source"] == {"write": 1}


async def test_confirmed_number_write_updates_coordinator(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    path = "/heatingCircuits/hc1/manualRoomSetpoint"
    metadata = ResourceMetadata(
        resource_type="floatValue",
        unit="C",
        minimum=5,
        maximum=30,
        writable=True,
    )
    current = Resource(path=path, value=20.0, has_value=True, metadata=metadata)
    confirmed = Resource(path=path, value=20.5, has_value=True, metadata=metadata)
    policy = number_policy_for_resource(current)
    assert policy is not None
    coordinator.resources = {path: current}
    coordinator.data = {path: _snapshot(current)}
    coordinator.last_update_success = True
    coordinator._write_service = AsyncMock()
    coordinator._write_service.async_write_number.return_value = WriteResult(
        WriteRequest("gateway-one", path, 20.5), confirmed
    )

    result = await coordinator.async_write_control(path, 20.5, policy)

    assert result is confirmed
    coordinator._write_service.async_write_number.assert_awaited_once_with(
        "gateway-one", current, 20.5, policy
    )


def _holiday_resources() -> tuple[Resource, Resource]:
    periods = Resource(
        path=HOLIDAY_LIST_PATH,
        value=[],
        has_value=True,
        metadata=ResourceMetadata(writable=True),
    )
    configuration = Resource(
        path=HOLIDAY_CONFIGURATION_PATH,
        value={
            "values": {
                "date": {"allowedValues": ["dateTime"]},
                "heatingMode": {"allowedValues": ["FIX_TEMPERATURE", "OFF"]},
                "dhwMode": {"allowedValues": ["OFF"]},
                "assignedTo": {"allowedValues": ["hc1", "dhw1"]},
            }
        },
        has_value=True,
    )
    return periods, configuration


async def test_holiday_writes_publish_confirmed_list_snapshot(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    periods, configuration = _holiday_resources()
    coordinator.resources = {
        periods.path: periods,
        configuration.path: configuration,
    }
    coordinator.data = {
        periods.path: _snapshot(periods),
        configuration.path: _snapshot(configuration),
    }
    coordinator.last_update_success = True
    coordinator._holiday_write_service = AsyncMock()
    confirmed = Resource(
        path=HOLIDAY_LIST_PATH,
        value=[{"id": 7}],
        has_value=True,
        metadata=periods.metadata,
    )
    coordinator._holiday_write_service.async_create.return_value = confirmed
    coordinator._holiday_write_service.async_update.return_value = confirmed
    coordinator._holiday_write_service.async_delete.return_value = confirmed
    values = HolidayWriteValues(
        start_date="2030-08-01T08:00:00",
        end_date="2030-08-08T18:00:00",
        heating_mode="FIX_TEMPERATURE",
        dhw_mode="OFF",
        ventilation_mode=None,
        assigned_to=("hc1", "dhw1"),
        name=None,
        thermal_disinfection=None,
        fix_temperature=17.0,
    )

    assert await coordinator.async_create_holiday(values) is confirmed
    assert await coordinator.async_update_holiday(7, values) is confirmed
    assert await coordinator.async_delete_holiday(7) is confirmed

    coordinator._holiday_write_service.async_create.assert_awaited_once()
    coordinator._holiday_write_service.async_update.assert_awaited_once()
    coordinator._holiday_write_service.async_delete.assert_awaited_once()
    assert coordinator.resources[HOLIDAY_LIST_PATH] is confirmed
    assert coordinator.data[HOLIDAY_LIST_PATH].source is SnapshotSource.WRITE
    assert coordinator.capability_metrics(HOLIDAY_LIST_PATH)["attempts_by_source"] == {
        "write": 3
    }


async def test_holiday_write_rejects_stale_capabilities(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    periods, configuration = _holiday_resources()
    coordinator.data = {
        periods.path: ResourceSnapshot(
            periods,
            True,
            datetime.now(UTC),
            freshness=Freshness.STALE,
        ),
        configuration.path: _snapshot(configuration),
    }
    coordinator._holiday_write_service = AsyncMock()

    with pytest.raises(WriteValidationError, match="not current"):
        await coordinator.async_delete_holiday(7)

    coordinator._holiday_write_service.async_delete.assert_not_awaited()


async def test_number_policy_rejects_non_numeric_value(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    path = "/heatingCircuits/hc1/manualRoomSetpoint"
    resource = Resource(
        path=path,
        value=20.0,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue",
            unit="C",
            minimum=5,
            maximum=30,
            writable=True,
        ),
    )
    policy = number_policy_for_resource(resource)
    assert policy is not None
    coordinator.data = {path: _snapshot(resource)}
    coordinator._write_service = AsyncMock()

    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(path, True, policy)  # type: ignore[arg-type]
    coordinator._write_service.async_write_number.assert_not_awaited()


async def test_write_rejects_stale_resource_before_cloud_call(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    path = "/heatingCircuits/hc1/operationMode"
    resource = Resource(path=path, value="manual", has_value=True)
    coordinator.resources = {path: resource}
    coordinator.data = {
        path: ResourceSnapshot(
            resource,
            True,
            datetime.now(UTC),
            freshness=Freshness.STALE,
        )
    }
    coordinator._write_service = AsyncMock()

    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(
            path, "auto", HEATING_CIRCUIT_OPERATION_MODE_POLICY
        )
    coordinator._write_service.async_write_enum.assert_not_awaited()


async def test_write_rate_limit_activates_global_backoff(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    path = "/heatingCircuits/hc1/operationMode"
    resource = Resource(path=path, value="manual", has_value=True)
    coordinator.resources = {path: resource}
    coordinator.data = {path: _snapshot(resource)}
    coordinator._write_service = AsyncMock()
    coordinator._write_service.async_write_enum.side_effect = RateLimited(30)

    with pytest.raises(RateLimited):
        await coordinator.async_write_control(
            path, "auto", HEATING_CIRCUIT_OPERATION_MODE_POLICY
        )

    assert coordinator._cloud_backoff_until > 0
    assert coordinator.capability_metrics(path)["results"] == {"rate_limited": 1}


async def test_write_respects_existing_backoff_without_cloud_call(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    coordinator._cloud_backoff_until = 10**20
    coordinator._write_service = AsyncMock()

    with pytest.raises(RateLimited):
        await coordinator.async_write_control(
            "/heatingCircuits/hc1/operationMode",
            "auto",
            HEATING_CIRCUIT_OPERATION_MODE_POLICY,
        )
    coordinator._write_service.async_write_enum.assert_not_awaited()

    coordinator._cloud_backoff_until = 0
    coordinator._circuit_open_until = 10**20
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(
            "/heatingCircuits/hc1/operationMode",
            "auto",
            HEATING_CIRCUIT_OPERATION_MODE_POLICY,
        )


async def test_write_auth_failure_starts_reauthentication(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    path = "/heatingCircuits/hc1/operationMode"
    resource = Resource(path=path, value="manual", has_value=True)
    coordinator.resources = {path: resource}
    coordinator.data = {path: _snapshot(resource)}
    coordinator._write_service = AsyncMock()
    coordinator._write_service.async_write_enum.side_effect = AuthenticationError()
    coordinator._config_entry.async_start_reauth = Mock()

    with pytest.raises(AuthenticationError):
        await coordinator.async_write_control(
            path, "auto", HEATING_CIRCUIT_OPERATION_MODE_POLICY
        )

    coordinator._config_entry.async_start_reauth.assert_called_once_with(hass)


def test_snapshot_compatibility_and_sanitized_error_categories() -> None:
    snapshot = _snapshot(_resource("/path", 1.0))

    assert snapshot.updated_at == snapshot.last_success
    assert _error_category(ServiceUnavailable(), None) == "serviceunavailable"


def test_cloud_friendly_profile_uses_longer_intervals(hass: HomeAssistant) -> None:
    client = AsyncMock()
    client.metrics = RequestMetrics()
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass,
        client,
        Gateway("gateway-one"),
        entry,
        PollingProfile.CLOUD_FRIENDLY,
    )

    assert coordinator._poll_intervals == POLL_INTERVALS_CLOUD_FRIENDLY
    assert coordinator.update_interval == ACTIVE_NOTIFICATION_INTERVAL
    coordinator._advance_groups((PollGroup.CONTROL,), 100.0)
    assert coordinator._next_update[PollGroup.CONTROL] == 700.0
    assert coordinator.diagnostics_summary()["polling_profile"] == "cloud_friendly"


async def test_manual_refresh_forces_dynamic_groups_due(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    future = monotonic() + 3_600
    coordinator._next_update = {group: future for group in coordinator._poll_intervals}
    refresh = AsyncMock()

    with patch.object(coordinator._debounced_refresh, "async_call", refresh):
        before = monotonic()
        await coordinator.async_request_refresh()

    refresh.assert_awaited_once_with()
    assert all(
        before <= coordinator._next_update[group] <= monotonic()
        for group in coordinator._poll_intervals
    )


def test_repeated_rate_limits_create_repair_issue(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    for _ in range(RATE_LIMIT_REPAIR_THRESHOLD):
        coordinator._activate_rate_limit_backoff(RateLimited(retry_after=10), 100.0)

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN,
        f"{RATE_LIMIT_ISSUE_PREFIX}{coordinator._config_entry.entry_id}",
    )
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_key == "repeated_rate_limit"
    assert coordinator.diagnostics_summary()["rate_limit_events"] == 3
    assert _error_category(None, None) == "unknown"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthenticationError("expired"), ConfigEntryAuthFailed),
        (ServiceUnavailable(), UpdateFailed),
    ],
)
async def test_coordinator_maps_discovery_failures(
    hass: HomeAssistant,
    error: Exception,
    expected: type[Exception],
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    with (
        patch(
            "custom_components.bosch_buderus_heating.coordinator."
            "async_discover_resources",
            AsyncMock(side_effect=error),
        ),
        pytest.raises(expected),
    ):
        await coordinator._async_update_data()


async def test_coordinator_rejects_empty_discovery(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    with (
        patch(
            "custom_components.bosch_buderus_heating.coordinator."
            "async_discover_resources",
            AsyncMock(return_value={}),
        ),
        pytest.raises(UpdateFailed, match="no resources"),
    ):
        await coordinator._async_update_data()


async def test_discovery_rate_limit_activates_backoff(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    with (
        patch(
            "custom_components.bosch_buderus_heating.coordinator."
            "async_discover_resources",
            AsyncMock(side_effect=RateLimited(None)),
        ),
        pytest.raises(UpdateFailed, match="rate limit"),
    ):
        await coordinator._async_update_data()

    assert coordinator._cloud_backoff_until > 0


async def test_coordinator_keeps_resource_scoped_failure_local(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    path = "/heatSources/actualModulation"
    coordinator = _coordinator(hass, client)
    coordinator.resources = {path: _resource(path, 1.0)}
    coordinator.data = {path: _snapshot(coordinator.resources[path])}
    coordinator._paths_by_group = {PollGroup.FAST: (path,)}
    coordinator._next_update = {PollGroup.FAST: 0.0}
    client.get_resources_bulk.return_value = (
        BatchItemResult(
            gateway_id="gateway-one",
            path=path,
            status=404,
            error=ResourceNotFound(path, 404),
        ),
    )

    result = await coordinator._async_update_data()

    assert not result[path].available
    assert result[path].last_error_category == "http_404"


async def test_polling_chunks_large_cycles_and_preserves_partial_success(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    paths = tuple(f"/heatSources/value{index}" for index in range(31))
    coordinator = _coordinator(hass, client)
    coordinator.resources = {path: _resource(path, 1.0) for path in paths}
    coordinator.data = {
        path: _snapshot(resource) for path, resource in coordinator.resources.items()
    }
    coordinator._paths_by_group = {PollGroup.FAST: paths}
    coordinator._next_update = {PollGroup.FAST: 0.0}

    first_chunk = tuple(_success(path, 2.0) for path in paths[:30])
    client.get_resources_bulk.side_effect = (first_chunk, ServiceUnavailable())

    result = await coordinator._async_update_data()

    assert client.get_resources_bulk.await_count == 2
    assert result[paths[0]].resource.value == 2.0
    assert result[paths[-1]].resource.value == 1.0


async def test_rate_limit_starts_bounded_no_request_backoff(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    path = "/heatSources/actualModulation"
    coordinator = _coordinator(hass, client)
    coordinator.resources = {path: _resource(path, 1.0)}
    coordinator.data = {path: _snapshot(coordinator.resources[path])}
    coordinator._paths_by_group = {PollGroup.FAST: (path,)}
    coordinator._next_update = {PollGroup.FAST: 0.0}
    client.get_resources_bulk.side_effect = RateLimited(120)

    with pytest.raises(UpdateFailed, match="no usable resources"):
        await coordinator._async_update_data()
    with pytest.raises(UpdateFailed, match="backoff"):
        await coordinator._async_update_data()

    assert client.get_resources_bulk.await_count == 1


async def test_bulk_item_rate_limit_stops_chunks_and_starts_backoff(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    paths = tuple(f"/heatSources/value{index}" for index in range(31))
    coordinator.resources = {path: Resource(path=path) for path in paths}
    coordinator.data = {path: _snapshot(Resource(path=path)) for path in paths}
    coordinator._paths_by_group = {PollGroup.FAST: paths}
    coordinator._next_update = {PollGroup.FAST: 0.0}
    client.get_resources_bulk.return_value = (
        BatchItemResult("gateway-one", paths[0], 429),
    )

    with pytest.raises(UpdateFailed, match="no usable resources"):
        await coordinator._async_update_data()

    assert client.get_resources_bulk.await_count == 1
    assert coordinator._cloud_backoff_until > monotonic()
    assert coordinator.diagnostics_summary()["rate_limit_events"] == 1


async def test_bulk_item_server_failure_marks_the_poll_failed(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    path = "/notifications"
    coordinator.resources = {path: Resource(path=path)}
    coordinator.data = {path: _snapshot(Resource(path=path))}
    coordinator._paths_by_group = {PollGroup.NOTIFICATIONS: (path,)}
    coordinator._next_update = {PollGroup.NOTIFICATIONS: 0.0}
    client.get_resources_bulk.return_value = (
        BatchItemResult("gateway-one", path, 503),
    )

    with pytest.raises(UpdateFailed, match="no usable resources"):
        await coordinator._async_update_data()

    assert coordinator._gateway_failure_count == 1
    assert coordinator.faults.diagnostics()["resource_results"] == {path: "503"}


async def test_not_found_resource_is_paused_without_blocking_other_paths(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    missing = "/heatSources/missing"
    healthy = "/heatSources/actualModulation"
    coordinator = _coordinator(hass, client)
    coordinator.resources = {
        missing: _resource(missing, 1.0),
        healthy: _resource(healthy, 2.0),
    }
    coordinator.data = {
        path: _snapshot(resource) for path, resource in coordinator.resources.items()
    }
    coordinator._paths_by_group = {PollGroup.FAST: (missing, healthy)}
    coordinator._next_update = {PollGroup.FAST: 0.0}
    client.get_resources_bulk.side_effect = (
        (
            BatchItemResult(
                "gateway-one",
                missing,
                404,
                error=ResourceNotFound(missing, 404),
            ),
            _success(healthy, 3.0),
        ),
        (_success(healthy, 4.0),),
    )

    await coordinator._async_update_data()
    coordinator._next_update[PollGroup.FAST] = 0.0
    result = await coordinator._async_update_data()

    assert client.get_resources_bulk.await_args.args[1] == (healthy,)
    assert result[missing].resource.value == 1.0
    assert result[healthy].resource.value == 4.0


async def test_repeated_forbidden_and_gateway_timeout_pause_resources(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    forbidden = "/heatSources/forbidden"
    timeout = "/heatSources/timeout"
    coordinator = _coordinator(hass, client)
    coordinator.resources = {
        forbidden: _resource(forbidden, 1.0),
        timeout: _resource(timeout, 2.0),
    }
    coordinator.data = {
        path: _snapshot(resource) for path, resource in coordinator.resources.items()
    }
    coordinator._paths_by_group = {PollGroup.SLOW: (forbidden, timeout)}
    coordinator._next_update = {PollGroup.SLOW: 0.0}
    client.get_resources_bulk.return_value = (
        BatchItemResult(
            "gateway-one",
            forbidden,
            403,
            error=ResourceForbidden(forbidden, 403),
        ),
        BatchItemResult(
            "gateway-one",
            timeout,
            504,
            error=ResourceError(timeout, 504),
        ),
    )

    for _ in range(FORBIDDEN_PAUSE_THRESHOLD):
        coordinator._next_update[PollGroup.SLOW] = 0.0
        coordinator.data = await coordinator._async_update_data()
        coordinator._negative_until.pop(timeout, None)

    assert forbidden in coordinator._negative_until
    assert timeout not in coordinator._negative_until


async def test_batch_failure_uses_bounded_core_fallback(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    path = "/heatSources/actualSupplyTemperature"
    coordinator = _coordinator(hass, client)
    coordinator.resources = {path: _resource(path, 20.0)}
    coordinator.data = {path: _snapshot(coordinator.resources[path])}
    coordinator._paths_by_group = {PollGroup.FAST: (path,)}
    coordinator._next_update = {PollGroup.FAST: 0.0}
    client.get_resources_bulk.side_effect = ServiceUnavailable()
    client.get_resource.return_value = _resource(path, 21.5)

    result = await coordinator._async_update_data()

    assert client.get_resource.await_count == 1
    assert result[path].resource.value == 21.5
    assert result[path].source is SnapshotSource.FALLBACK
    assert result[path].freshness is Freshness.FRESH
    assert coordinator.capability_metrics(path) == {
        "attempts_total": 2,
        "successful": 1,
        "failed": 1,
        "success_rate_percent": 50.0,
        "results": {"service_unavailable": 1, "success": 1},
        "attempts_by_source": {"batch": 1, "fallback": 1},
        "last_result": "success",
    }


async def test_repeated_gateway_failure_opens_circuit_breaker(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    path = "/heatSources/noncritical"
    coordinator = _coordinator(hass, client)
    coordinator.resources = {path: _resource(path, 1.0)}
    coordinator.data = {path: _snapshot(coordinator.resources[path])}
    coordinator._paths_by_group = {PollGroup.SLOW: (path,)}
    coordinator._next_update = {PollGroup.SLOW: 0.0}
    client.get_resources_bulk.side_effect = ServiceUnavailable()

    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        with pytest.raises(UpdateFailed, match="no usable resources"):
            await coordinator._async_update_data()

    with pytest.raises(UpdateFailed, match="circuit breaker"):
        await coordinator._async_update_data()

    assert client.get_resources_bulk.await_count == CIRCUIT_FAILURE_THRESHOLD


async def test_poll_auth_failure_and_empty_due_cycle(hass: HomeAssistant) -> None:
    client = AsyncMock()
    path = "/heatSources/actualModulation"
    coordinator = _coordinator(hass, client)
    coordinator.resources = {path: _resource(path, 1.0)}
    coordinator.data = {path: _snapshot(coordinator.resources[path])}
    coordinator._paths_by_group = {PollGroup.FAST: (path,)}
    coordinator._next_update = {PollGroup.FAST: 0.0}
    client.get_resources_bulk.side_effect = AuthenticationError("expired")

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    coordinator._paths_by_group = {}
    coordinator._next_update = {PollGroup.FAST: 0.0}
    assert await coordinator._async_update_data() == coordinator.data


async def test_fallback_maps_scoped_errors_and_stops_on_rate_limit(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    paths = (
        "/heatSources/actualSupplyTemperature",
        "/heatSources/returnTemperature",
        "/heatSources/actualModulation",
    )
    client.get_resource.side_effect = (
        ResourceNotFound(paths[0], 404),
        ServiceUnavailable(),
        RateLimited(90),
    )

    results = await coordinator._async_core_fallback(paths)

    assert len(results) == 1
    assert results[0].status == 404
    assert coordinator._cloud_backoff_until > 0


async def test_fallback_auth_failure_requests_reauthentication(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    client.get_resource.side_effect = AuthenticationError("expired")

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_core_fallback(
            ("/heatSources/actualSupplyTemperature",)
        )


def test_expired_negative_pause_and_unknown_result_are_safe(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    coordinator._negative_until["/expired"] = 1.0

    assert not coordinator._resource_is_paused("/expired", 2.0)
    assert "/expired" not in coordinator._negative_until
    assert (
        coordinator._apply_results(
            {},
            (BatchItemResult("gateway-one", "/unknown", None),),
            attempted_at=datetime.now(UTC),
            now_monotonic=2.0,
            source=SnapshotSource.BATCH,
        )
        == 0
    )
