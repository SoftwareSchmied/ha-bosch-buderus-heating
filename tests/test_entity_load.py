"""0.9.0-alpha.2 optimizations of entities, coordinator and log messages.

Each test pins the behaviour the decision keeps or changes: fewer state
writes, caches with the same answers, held capabilities, and log levels.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate.const import HVACMode
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.bosch_buderus_heating import (
    binary_sensor,
    control,
    holidays,
    legacy_holidays,
    local_only,
    local_plant_view,
    resource_catalog,
    sensor,
    writes,
    zone_controls,
)
from custom_components.bosch_buderus_heating import (
    coordinator as coordinator_module,
)
from custom_components.bosch_buderus_heating.coordinator import (
    CapabilityMetrics,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.exchange_counters import (
    ExchangeCounterGuard,
)
from custom_components.bosch_buderus_heating.heating_climate import (
    HeatingCircuitClimate,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Resource,
    ResourceMetadata,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.metrics import RequestMetrics
from custom_components.bosch_buderus_heating.pointt.models import (
    MAX_SHARED_METADATA,
    shared_metadata,
)
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    poll_group,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusRequestMetricSensor,
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from tests.ha_test_helpers import async_fire_time_changed
from tests.test_coordinator import _coordinator as cloud_coordinator
from tests.test_coordinator import _resource, _snapshot, _success
from tests.test_heating_controls import MODE, ROOT, TARGET
from tests.test_heating_controls import setup as climate_setup
from tests.test_validity import RETURN, _temperature
from tests.test_validity import _coordinator as valid_coordinator

# ------------------------------------------------- G2 request metric sensors


def _metric(key: str, metrics: RequestMetrics) -> BoschBuderusRequestMetricSensor:
    description = next(
        item for item in sensor._REQUEST_METRIC_DESCRIPTIONS if item.key == key
    )
    return BoschBuderusRequestMetricSensor("entry", metrics, description)


def _record(metrics: RequestMetrics) -> None:
    metrics.record_request(
        category="bulk", method="POST", status=200, outcome="success", duration_ms=80
    )


def test_entity_summary_is_the_snapshot_without_recent_requests() -> None:
    metrics = RequestMetrics()
    _record(metrics)
    summary, snapshot = metrics.entity_summary(), metrics.snapshot()
    assert "recent_requests" not in summary and snapshot["recent_requests"]
    for key, value in summary.items():
        assert snapshot[key] == value, key


def test_request_counter_is_a_total_since_the_start() -> None:
    metrics = RequestMetrics()
    total = _metric("pointt_api_requests_total", metrics)
    assert total.state_class is SensorStateClass.TOTAL
    assert total.last_reset == metrics.started_utc
    assert _metric("pointt_api_requests_last_hour", metrics).last_reset is None


def test_changing_and_structured_metric_attributes_are_not_recorded() -> None:
    unrecorded = BoschBuderusRequestMetricSensor._unrecorded_attributes
    metrics = RequestMetrics()
    for description in sensor._REQUEST_METRIC_DESCRIPTIONS:
        attributes = _metric(description.key, metrics).extra_state_attributes
        expected = {
            key
            for key, value in attributes.items()
            if isinstance(value, dict | list) or key == "observation_seconds"
        }
        assert expected <= unrecorded


async def test_metric_sensor_writes_only_changes_once_a_minute(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    metrics = RequestMetrics()
    entity = _metric("pointt_api_requests_total", metrics)
    entity.hass = hass
    entity.entity_id = "sensor.requests"
    with patch.object(entity, "async_write_ha_state") as write:
        await entity.async_added_to_hass()
        # Time alone (the observation seconds) is no change.
        freezer.tick(timedelta(seconds=61))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        write.assert_not_called()
        # Several requests within the minute: one write at the next tick.
        _record(metrics)
        _record(metrics)
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        write.assert_not_called()
        freezer.tick(timedelta(seconds=31))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert write.call_count == 1
        assert entity.native_value == 2
        # The last state is written when Home Assistant stops.
        _record(metrics)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert write.call_count == 2
        assert entity.native_value == 3
        # Removal writes nothing: the counts start anew with the next start.
        _record(metrics)
        await entity.async_will_remove_from_hass()
        assert write.call_count == 2
    for remove in entity._on_remove or ():
        remove()


async def test_metric_sensor_has_no_coordinator_or_runtime_listener(
    hass: HomeAssistant,
) -> None:
    coordinator = Mock()
    metrics = RequestMetrics()
    entity = _metric("pointt_api_requests_last_hour", metrics)
    entity.hass = hass
    entity.entity_id = "sensor.last_hour"
    await entity.async_added_to_hass()
    coordinator.async_add_listener.assert_not_called()
    for remove in entity._on_remove or ():
        remove()


# ------------------------------------------------ O1 discovery input guard


def test_discovery_skips_the_rebuild_for_the_same_input() -> None:
    resources = {RETURN: _temperature(RETURN)}
    checked: list[object] = []
    assert not control.unchanged_input(checked, resources)
    assert control.unchanged_input(checked, dict(resources))
    # An equal, newly parsed resource is the same input.
    assert control.unchanged_input(checked, {RETURN: _temperature(RETURN)})
    assert not control.unchanged_input(checked, {RETURN: _temperature(RETURN, 31.0)})
    assert not control.unchanged_input(checked, {})


async def test_binary_and_control_discovery_build_once_per_input(
    hass: HomeAssistant,
) -> None:
    coordinator = valid_coordinator(hass, _temperature(RETURN))
    coordinator.stored_resources = dict
    added: list[object] = []
    scope = SimpleNamespace(async_on_unload=lambda _listener: None)
    build = Mock(return_value=())
    with patch.object(binary_sensor, "build_binary_sensor_descriptions", build):
        binary_sensor._track_binary_sensors(scope, coordinator, added.extend)
        coordinator.async_update_listeners()
        coordinator.async_update_listeners()
        assert build.call_count == 1
        coordinator.data = {
            RETURN: ResourceSnapshot(
                _temperature(RETURN, 35.0), True, datetime.now(UTC)
            )
        }
        coordinator.async_update_listeners()
        assert build.call_count == 2
    controls = Mock(return_value=())
    control.track_control_entities(scope, coordinator, added.extend, controls, Mock())
    coordinator.async_update_listeners()
    coordinator.async_update_listeners()
    assert controls.call_count == 1
    await coordinator.async_shutdown()


# ------------------------------------------------------ O2 display snapshot


def test_display_snapshot_is_worked_out_once_per_pass(hass: HomeAssistant) -> None:
    coordinator = valid_coordinator(hass, _temperature(RETURN))
    calls = Mock(wraps=coordinator._display_snapshot)
    selection = SimpleNamespace(
        _pass=None, select=lambda _path, snapshot, **_kwargs: snapshot
    )

    def cached(key, compute):
        if selection._pass is None:
            return compute()
        if key not in selection._pass:
            selection._pass[key] = compute()
        return selection._pass[key]

    selection._cached = cached
    coordinator.local_selection = selection
    with patch.object(coordinator, "_display_snapshot", calls):
        selection._pass = {}
        first = coordinator.display_snapshot(RETURN)
        assert coordinator.display_snapshot(RETURN) is first
        assert calls.call_count == 1
        selection._pass = None
        coordinator.display_snapshot(RETURN)
        coordinator.display_snapshot(RETURN)
        assert calls.call_count == 3


# ----------------------------------------------------------- caches (O4-O9)


PATHS = (
    "/heatingCircuits/hc1/operationMode",
    "/heatingCircuits/hc2/manualRoomSetpoint",
    "/dhwCircuits/dhw1/operationMode",
    "/dhwCircuits/dhw1/charge",
    "/zones/zone1/heating/operationMode",
    "/zones/zone1/heating/manualRoomSetpoint",
    "/zones/zn1/userMode",
    "/system/seasonOptimizer/mode",
    "/system/awayMode/enabled",
    "/pool/setpointTemp",
    "/swCircuits/sw1/sensor/saltLevelAlarm",
    "/heatSources/hs1/actualPower",
    "/ventilation/zone1/exhaustFanLevel",
    "/solarCircuits/sc1/collectorTemperature",
    "/devices/device1/battery",
    "/system/holidayModes/hm1/startStop",
    "/system/holidayMode",
    "/holidayMode/list",
    "/gateway/versionFirmware",
)


def test_control_policy_cache_keeps_the_first_match_in_order() -> None:
    for path in PATHS:
        expected = next(
            (
                policy
                for policy in (*writes._ENUM_POLICIES, *writes.NUMBER_WRITE_POLICIES)
                if writes.re.fullmatch(policy.path_pattern, path)
            ),
            None,
        )
        assert writes._policy_for_path(path) is expected


@pytest.mark.parametrize(
    "cached",
    [
        resource_catalog.function_group_for_path,
        resource_catalog.capability_maturity,
        resource_catalog.resource_name,
        zone_controls.is_zone_control,
        holidays.is_holiday_resource_path,
        holidays.is_holiday_period_path,
        legacy_holidays.is_legacy_holiday_path,
        local_plant_view.diagnostic_suffix,
    ],
)
def test_cached_answers_equal_the_computed_ones(cached) -> None:
    for path in PATHS:
        argument = (
            "local_only:" + path.strip("/").replace("/", ":")
            if cached is local_plant_view.diagnostic_suffix
            else path
        )
        assert cached(argument) == cached.__wrapped__(argument)
        assert cached(argument) == cached(argument)


def test_resource_names_keep_language_and_subkey_apart() -> None:
    path = "/heatSources/emon/totalConsumption"
    assert resource_catalog.resource_name(path, "outputProduced") != (
        resource_catalog.resource_name(path, "outputProduced", language="en")
    )
    assert resource_catalog.resource_name(path) != resource_catalog.resource_name(
        path, "outputProduced"
    )


def test_state_key_cache_accepts_any_value() -> None:
    assert local_only.state_key("Normal Operation") == "normal_operation"
    assert local_only.state_key(["not", "hashable"]) is None
    assert local_only.state_key("\x00") is None
    assert local_only.state_key(1) is None


# --------------------------------------------- O10 plant view builders once


def test_cloud_entity_question_builds_each_description_once() -> None:
    resource = _temperature(RETURN)
    with (
        patch.object(
            sensor,
            "build_sensor_descriptions",
            wraps=sensor.build_sensor_descriptions,
        ) as sensors,
        patch.object(
            binary_sensor,
            "build_binary_sensor_descriptions",
            wraps=binary_sensor.build_binary_sensor_descriptions,
        ) as binaries,
    ):
        assert local_plant_view._cloud_entity(resource, offered=True)
        unknown = Resource("/system/unknownValue", value=3, has_value=True)
        local_plant_view._cloud_entity(unknown, offered=True)
    assert sensors.call_count == 2 and binaries.call_count == 1


@pytest.mark.parametrize("offered", [False, True])
def test_cloud_entity_matches_the_earlier_two_questions(offered: bool) -> None:
    for resource in (
        _temperature(RETURN),
        Resource("/system/unknownValue", value=3, has_value=True),
        Resource(
            "/heatSources/hs1/defrostActive",
            value=True,
            has_value=True,
            metadata=ResourceMetadata(resource_type="booleanValue"),
        ),
    ):
        resources = {resource.path: resource}
        known = any(
            not item.value_kind.startswith("unknown_")
            for item in sensor.build_sensor_descriptions(resources)
        ) or bool(binary_sensor.build_binary_sensor_descriptions(resources))
        earlier = known or (
            offered and bool(local_plant_view.description_shapes(resources))
        )
        assert local_plant_view._cloud_entity(resource, offered=offered) is earlier


# --------------------------------------------------- O14 timers re-armed less


async def test_validity_timer_is_armed_again_only_for_a_new_due_time(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = valid_coordinator(hass, _temperature(RETURN))
    listener = Mock()
    remove = coordinator.async_add_listener(listener)
    with patch.object(
        coordinator_module,
        "async_track_point_in_utc_time",
        wraps=coordinator_module.async_track_point_in_utc_time,
    ) as track:
        coordinator.async_update_listeners()
        coordinator.async_update_listeners()
        coordinator.async_update_listeners()
        assert track.call_count == 1
        # A new reading moves the due time: armed again.
        freezer.tick(timedelta(seconds=30))
        coordinator.data = {
            RETURN: ResourceSnapshot(_temperature(RETURN), True, datetime.now(UTC))
        }
        coordinator.async_update_listeners()
        assert track.call_count == 2
    listener.reset_mock()
    # Still on time: three minutes after the last reading, plus the margin.
    freezer.tick(timedelta(minutes=2, seconds=59))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    listener.assert_not_called()
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    listener.assert_called_once()
    assert not coordinator.display_snapshot(RETURN).available
    remove()
    await coordinator.async_shutdown()


async def test_pause_timer_is_armed_again_only_for_a_new_end(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = valid_coordinator(hass, _temperature(RETURN))
    until = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
    status = Mock(return_value=("protection_pause", until))
    coordinator.control_status = status
    entity = binary_sensor.BoschBuderusCloudControlBinarySensor(coordinator)
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    with patch.object(
        binary_sensor, "async_call_later", wraps=binary_sensor.async_call_later
    ) as later:
        entity._refresh_status()
        entity._refresh_status()
        assert later.call_count == 1
        status.return_value = ("protection_pause", until + timedelta(minutes=1))
        entity._refresh_status()
        assert later.call_count == 2
    status.return_value = (None, None)
    freezer.tick(timedelta(minutes=6, seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    entity.async_write_ha_state.assert_called_once()
    assert entity.is_on
    entity._cancel_pause_end()
    await coordinator.async_shutdown()


# ------------------------------------------------- O22, O24, O25 (memory)


def test_sensor_helpers_are_class_defaults_until_used(hass: HomeAssistant) -> None:
    coordinator = valid_coordinator(hass, _temperature(RETURN))
    (description,) = build_sensor_descriptions(coordinator.resources)
    entity = BoschBuderusSensor(coordinator, description)
    for name in (
        "_unknown_enum_values",
        "_contract_changes",
        "_unknown_enum_warning_logged",
        "_switch_timer",
        "_last_switch_level",
        "_exchange_guard",
    ):
        assert name not in vars(entity), name
    entity._contract_changed("value_type")
    entity._contract_changed("value_type")
    assert entity._contract_changes == {"value_type"}
    assert coordinator.diagnostics_summary()["changed_contracts_detected"] == 1
    assert BoschBuderusSensor._contract_changes is None


def test_capability_counters_start_with_the_first_attempt() -> None:
    empty = CapabilityMetrics()
    assert empty.results is None and empty.sources is None
    assert empty.snapshot() == {
        "attempts_total": 0,
        "successful": 0,
        "failed": 0,
        "success_rate_percent": None,
        "results": {},
        "attempts_by_source": {},
        "last_result": None,
    }
    empty.record(result="success", source=SnapshotSource.BATCH)
    assert empty.snapshot()["results"] == {"success": 1}


def test_counter_guard_has_no_instance_dictionary() -> None:
    guard = ExchangeCounterGuard(Mock(), "target", "uid", SensorStateClass.TOTAL)
    assert not hasattr(guard, "__dict__")


def test_equal_metadata_is_shared_and_the_table_is_bounded() -> None:
    first = Resource("/a", metadata=ResourceMetadata(resource_type="floatValue"))
    second = Resource("/b", metadata=ResourceMetadata(resource_type="floatValue"))
    assert first.metadata is second.metadata
    assert replace(first, value=1).metadata is first.metadata
    table = "custom_components.bosch_buderus_heating.pointt.models._SHARED_METADATA"
    with patch.dict(table, {}, clear=True):
        for index in range(MAX_SHARED_METADATA):
            shared_metadata(ResourceMetadata(unit=str(index)))
        new = ResourceMetadata(unit="beyond")
        assert shared_metadata(new) is new
        assert shared_metadata(ResourceMetadata(unit="beyond")) is not new


def test_shared_metadata_keeps_the_exact_types() -> None:
    table = "custom_components.bosch_buderus_heating.pointt.models._SHARED_METADATA"
    with patch.dict(table, {}, clear=True):
        for values in (
            {"minimum": 0},
            {"minimum": 0.0},
            {"allowed_values": (0, 1)},
            {"allowed_values": (False, True)},
            {"allowed_values": (0.0, 1.0)},
            {"numeric_error_values": (1,)},
            {"numeric_error_values": (1.0,)},
        ):
            stored = shared_metadata(ResourceMetadata(**values))
            for name, value in values.items():
                # repr tells 0, 0.0 and False apart, also inside tuples.
                assert repr(getattr(stored, name)) == repr(value)


# --------------------------------------------- O30 capabilities held


async def test_controls_keep_capabilities_while_unavailable(
    hass: HomeAssistant,
) -> None:
    _, coordinator, entity = climate_setup(hass)
    current = dict(entity.capability_attributes)
    assert current["hvac_modes"]
    coordinator.data[MODE] = replace(coordinator.data[MODE], available=False)
    assert not entity.available
    assert entity.capability_attributes == current
    # A new entity (after a reload) starts from its registry entry.
    later = HeatingCircuitClimate(coordinator, ROOT)
    later.hass = hass
    later.registry_entry = SimpleNamespace(capabilities=current)
    assert later.capability_attributes == current
    without = HeatingCircuitClimate(coordinator, ROOT)
    without.hass = hass
    assert without.capability_attributes != current


# --------------------------------------------- O32 dew point attributes


def test_dew_point_inputs_are_not_recorded() -> None:
    assert {
        "source_room_temperature_c",
        "source_relative_humidity_percent",
        "calculation_method",
        "magnus_a",
        "magnus_b_c",
    } <= BoschBuderusSensor._unrecorded_attributes


# --------------------------------------------- O40, O50 cloud cadences


@pytest.mark.parametrize(
    ("path", "group"),
    [
        ("/system/iSRC/installationStatus", PollGroup.STATIC),
        ("/system/variableTariff/supportStatus", PollGroup.STATIC),
        ("/system/sensors/temperatures/outdoorTemperatureSource", PollGroup.SLOW),
        ("/gateway/update/status", PollGroup.SLOW),
        ("/system/globalSeasonOptimizer/currentMode", PollGroup.FAST),
        ("/devices/list", PollGroup.HOURLY),
        ("/zones/list", PollGroup.HOURLY),
        ("/zones/configuration", PollGroup.HOURLY),
        ("/heatSources/info", PollGroup.HOURLY),
        ("/solarCircuits/sc1", PollGroup.HOURLY),
        ("/devices/inclusionWhitelist", PollGroup.HOURLY),
    ],
)
def test_settled_and_container_paths_are_read_less_often(
    path: str, group: PollGroup
) -> None:
    resource = Resource(
        path,
        value="x",
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue"),
    )
    assert poll_group(resource) is group


def test_a_container_with_references_stays_static() -> None:
    from custom_components.bosch_buderus_heating.pointt import ResourceReference

    resource = Resource("/devices", references=(ResourceReference("/devices/list"),))
    assert poll_group(resource) is PollGroup.STATIC
    assert coordinator_module.POLL_INTERVALS[PollGroup.HOURLY] == timedelta(hours=1)


# ----------------------------------------------- O80-O82 log messages


async def test_breaker_warns_once_and_reports_the_recovery(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    client = AsyncMock()
    coordinator = cloud_coordinator(hass, client)
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    for _ in range(3):
        coordinator._record_gateway_failure(100.0)
    coordinator._record_gateway_failure(500.0)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "after 3 consecutive failures" in warnings[0].getMessage()
    assert "next attempt follows in 300 seconds" in warnings[0].getMessage()
    assert any("opened again after 4" in r.getMessage() for r in caplog.records)
    coordinator._record_gateway_recovery(1000.0)
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert ["closed after 900 seconds" in r.getMessage() for r in infos] == [True]
    coordinator._record_gateway_recovery(1100.0)
    assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1
    assert coordinator._gateway_failure_count == 0
    assert coordinator._circuit_open_until == 0.0


async def test_batch_failure_names_its_category_and_warns_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    client = AsyncMock()
    coordinator = cloud_coordinator(hass, client)
    path = "/heatSources/actualModulation"
    coordinator.resources = {path: _resource(path, 1.0)}
    coordinator.data = {path: _snapshot(coordinator.resources[path])}
    coordinator._paths_by_group = {PollGroup.FAST: (path,)}
    client.get_resources_bulk.side_effect = ServiceUnavailable(503)
    client.get_resource.side_effect = ServiceUnavailable(503)
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    for _ in range(2):
        coordinator._next_update = {PollGroup.FAST: 0.0}
        with pytest.raises(UpdateFailed, match=r"no usable resources \(service_"):
            await coordinator._async_update_data()
    batch = [r for r in caplog.records if "batch refresh failed" in r.getMessage()]
    assert [r.levelno for r in batch] == [logging.WARNING, logging.DEBUG]
    assert "(service_unavailable)" in batch[0].getMessage()
    # A fallback that answers is only a debug note, also in a new outage.
    caplog.clear()
    client.get_resources_bulk.side_effect = None
    client.get_resources_bulk.return_value = (_success(path, 2.0),)
    coordinator._next_update = {PollGroup.FAST: 0.0}
    await coordinator._async_update_data()
    assert not coordinator._batch_failure_logged
    client.get_resources_bulk.side_effect = ServiceUnavailable(503)
    client.get_resource.side_effect = None
    client.get_resource.return_value = _resource(path, 3.0)
    coordinator._next_update = {PollGroup.FAST: 0.0}
    await coordinator._async_update_data()
    batch = [r for r in caplog.records if "batch refresh failed" in r.getMessage()]
    assert [r.levelno for r in batch] == [logging.DEBUG]


async def test_one_outage_warns_once_while_the_fallback_answers_between(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """COORD-3: the batch endpoint fails throughout, so it stays one outage
    although the fallback answers in between; only a cycle in which the batch
    answers again ends it."""
    client = AsyncMock()
    coordinator = cloud_coordinator(hass, client)
    path = "/heatSources/actualModulation"
    coordinator.resources = {path: _resource(path, 1.0)}
    coordinator.data = {path: _snapshot(coordinator.resources[path])}
    coordinator._paths_by_group = {PollGroup.FAST: (path,)}
    # The batch endpoint fails throughout.
    client.get_resources_bulk.side_effect = ServiceUnavailable(503)
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    for fallback in (False, True, False):
        client.get_resource.side_effect = None if fallback else ServiceUnavailable(503)
        client.get_resource.return_value = _resource(path, 2.0)
        coordinator._next_update = {PollGroup.FAST: 0.0}
        if fallback:
            await coordinator._async_update_data()
        else:
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
    batch = [r for r in caplog.records if "batch refresh failed" in r.getMessage()]
    assert [r.levelno for r in batch] == [
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
    ]
    client.get_resources_bulk.side_effect = None
    client.get_resources_bulk.return_value = (_success(path, 4.0),)
    coordinator._next_update = {PollGroup.FAST: 0.0}
    await coordinator._async_update_data()
    client.get_resources_bulk.side_effect = ServiceUnavailable(503)
    client.get_resource.side_effect = ServiceUnavailable(503)
    coordinator._next_update = {PollGroup.FAST: 0.0}
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    batch = [r for r in caplog.records if "batch refresh failed" in r.getMessage()]
    assert batch[-1].levelno == logging.WARNING


def test_failed_cycle_category_is_the_most_frequent_error() -> None:
    results = [
        BatchItemResult("gw", "/a", 503),
        BatchItemResult("gw", "/b", 503),
        BatchItemResult("gw", "/c", 404),
    ]
    assert coordinator_module._cycle_failure_category(results) == (
        "service_unavailable"
    )
    assert coordinator_module._cycle_failure_category([]) == "unknown"


# ------------------------------------------------- O94 diagnostics details


def test_poll_duration_percentile_and_failing_paths(hass: HomeAssistant) -> None:
    coordinator = valid_coordinator(hass, _temperature(RETURN))
    coordinator._recent_poll_durations_ms.extend(
        float(value) for value in range(1, 101)
    )
    coordinator._record_capability("/a", "not_found", SnapshotSource.BATCH)
    coordinator._record_capability("/a", "not_found", SnapshotSource.BATCH)
    coordinator._record_capability("/b", "timeout", SnapshotSource.BATCH)
    coordinator._record_capability("/b", "success", SnapshotSource.BATCH)
    summary = coordinator.diagnostics_summary()
    assert summary["p95_poll_duration_ms"] == 95.0
    assert summary["poll_duration_samples"] == 100
    assert summary["capability_attempts"]["failed"] == 3
    assert summary["capability_attempts"]["paths_failing"] == 1
    assert coordinator_module._percentile([], 0.95) is None


# ------------------------------------------ O100, O101, O104, O107, #80


def test_compressor_power_signal_is_named_as_heat_output() -> None:
    definition = local_only.definition_for("/signals/SRC.CUHP.HP1.CompressorPower")
    assert definition is not None
    assert definition.names == (
        "Wärmeleistung (Verdichter)",
        "Heat output (compressor)",
    )
    assert definition.key == "compressor_output"


async def test_climate_stays_available_with_valid_readings(
    hass: HomeAssistant,
) -> None:
    _, coordinator, entity = climate_setup(hass)
    # A failed cloud cycle no longer hides readings within their validity.
    coordinator.last_update_success = False
    assert entity.available
    assert entity.hvac_mode is HVACMode.HEAT
    assert entity.native_target_temperature == 19
    # Changes still need a ready cloud, and the message says so.
    with pytest.raises(HomeAssistantError) as refused:
        await entity.async_set_hvac_mode(HVACMode.OFF)
    assert refused.value.translation_key == "command_cloud_unreachable"
    with pytest.raises(HomeAssistantError) as refused:
        await entity.async_set_temperature(temperature=20)
    assert refused.value.translation_key == "command_cloud_unreachable"
    coordinator._write_service.async_write_enum.assert_not_awaited()
    # An expired reading makes it unavailable, like a sensor.
    old = datetime.now(UTC) - timedelta(days=1)
    coordinator.data[TARGET] = replace(coordinator.data[TARGET], last_success=old)
    coordinator.data[MODE] = replace(coordinator.data[MODE], last_success=old)
    with patch.object(
        coordinator, "validity_limit", return_value=timedelta(minutes=15)
    ):
        assert not entity.available


def _enum_sensor(hass: HomeAssistant, path: str, value: str, allowed=()):
    resource = Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue", allowed_values=allowed),
    )
    coordinator = valid_coordinator(hass, resource)
    (description,) = build_sensor_descriptions(coordinator.resources)
    return BoschBuderusSensor(coordinator, description)


@pytest.mark.parametrize(
    ("path", "raw", "shown", "key"),
    [
        (
            "/system/iSRC/installationStatus",
            "TIMEOUT",
            "timeout",
            "isrc_installation_status",
        ),
        ("/system/iSRC/supportStatus", "SUPPORTED", "supported", "isrc_support_status"),
        (
            "/system/seasonOptimizer/mode",
            "forcedHeat",
            "forced_heat",
            "season_optimizer_mode",
        ),
    ],
)
def test_text_values_become_translated_states(
    hass: HomeAssistant, path: str, raw: str, shown: str, key: str
) -> None:
    entity = _enum_sensor(hass, path, raw)
    description = entity.entity_description
    assert description.translation_key == key
    assert description.device_class is SensorDeviceClass.ENUM
    assert description.state_class is None
    assert raw not in description.options and shown in description.options
    assert entity.native_value == shown


# Odd spellings of other enums, as alpha.1 showed them (``enum_value_to_ha``
# alone): unchanged by O107.
@pytest.mark.parametrize(
    ("key", "raw"),
    [
        (key, raw)
        for key in sorted(set(sensor._ENUM_OPTIONS) - sensor._NORMALIZED_ENUM_KEYS)
        for raw in ("OFF", "On", "HEATING", "forcedHeat", "notConnected", "AUTO_X")
    ],
)
def test_other_enums_keep_their_alpha_1_states(key: str, raw: str) -> None:
    from custom_components.bosch_buderus_heating.enum_translation import (
        enum_value_to_ha,
    )

    assert sensor._enum_state(key, raw) == enum_value_to_ha(key, raw)


def test_other_enum_entities_show_the_alpha_1_value(hass: HomeAssistant) -> None:
    entity = _enum_sensor(hass, "/heatSources/compressor/status", "HEATING")
    assert entity.entity_description.translation_key == "compressor_status"
    assert "HEATING" in entity.entity_description.options
    assert entity.native_value == "HEATING"


def test_unknown_spellings_still_extend_the_options(hass: HomeAssistant) -> None:
    entity = _enum_sensor(hass, "/system/iSRC/installationStatus", "pairingDone")
    assert "pairing_done" not in sensor._ENUM_OPTIONS["isrc_installation_status"]
    assert entity.native_value == "pairingDone"


def test_request_metric_description_is_unchanged_for_other_keys() -> None:
    by_key = {item.key: item for item in sensor._REQUEST_METRIC_DESCRIPTIONS}
    assert by_key["pointt_api_requests_last_hour"].state_class is (
        SensorStateClass.MEASUREMENT
    )
    assert by_key["pointt_api_requests_total"].native_unit_of_measurement == (
        "requests"
    )


async def test_number_and_select_keep_capabilities_and_unit(
    hass: HomeAssistant,
) -> None:
    from tests.test_number import _number
    from tests.test_select import _select

    number, select = _number(hass), _select(hass)
    held = {entity: dict(entity.capability_attributes) for entity in (number, select)}
    unit = number.native_unit_of_measurement
    assert held[select]["options"] and held[number]["max"] == 30.0
    for entity in (number, select):
        # Before the first reading after a reload, or during a source switch.
        entity.coordinator.data = {}
        assert not entity.available
        assert entity.capability_attributes == held[entity]
    assert number.native_unit_of_measurement == unit


async def test_local_only_sensor_keeps_its_unit_and_statistics(
    hass: HomeAssistant,
) -> None:
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        BoschBuderusLocalOnlySensor,
    )

    path = "/signals/SRC.Some.Temperature"
    definition = local_only.definition_for(path)
    assert definition is not None and definition.kind == local_only.GENERIC
    coordinator = valid_coordinator(hass, _temperature(RETURN))
    entity = BoschBuderusLocalOnlySensor(coordinator, path, definition)
    with patch.object(entity, "_shape", return_value=(21.5, "°C", None)):
        assert entity.native_unit_of_measurement == "°C"
        assert entity.state_class is SensorStateClass.MEASUREMENT
        current = dict(entity.capability_attributes or {})
    with patch.object(entity, "_shape", return_value=None):
        assert not entity.available
        assert entity.native_unit_of_measurement == "°C"
        assert entity.state_class is SensorStateClass.MEASUREMENT
        assert dict(entity.capability_attributes or {}) == current
    # A run time or a number without a unit never gets statistics.
    with patch.object(entity, "_shape", return_value=(5, "s", None)):
        assert entity.state_class is None
    fresh = BoschBuderusLocalOnlySensor(coordinator, path, definition)
    fresh.registry_entry = SimpleNamespace(
        options={"bosch_buderus_heating": {"native_unit": "°C"}},
        capabilities={"state_class": "measurement"},
    )
    with patch.object(fresh, "_shape", return_value=None):
        assert fresh.native_unit_of_measurement == "°C"
        assert fresh.capability_attributes == {"state_class": "measurement"}


async def test_removal_neither_writes_nor_changes_the_platform_state(
    hass: HomeAssistant,
) -> None:
    metrics = RequestMetrics()
    entity = _metric("pointt_api_requests_total", metrics)
    entity.hass = hass
    _record(metrics)
    platform_states = []

    def write() -> None:
        platform_states.append(getattr(entity, "_platform_state", None))

    with patch.object(entity, "async_write_ha_state", side_effect=write):
        before = getattr(entity, "_platform_state", None)
        await entity.async_will_remove_from_hass()
        assert getattr(entity, "_platform_state", None) is before
    # The base class removal only; no state write that needs a private
    # platform state of Home Assistant.
    assert platform_states == []
