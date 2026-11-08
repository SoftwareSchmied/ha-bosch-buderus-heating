"""Regression coverage for independent PointT start counters."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.bosch_buderus_heating import (
    _enable_new_default_entities,
    _remove_retired_entities,
    _restore_registered_startup_paths,
)
from custom_components.bosch_buderus_heating.binary_sensor import (
    build_binary_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.const import (
    CONF_BRAND,
    CONF_GATEWAY_IDS,
    DOMAIN,
    PollingProfile,
)
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.diagnostics import _capability_diagnostics
from custom_components.bosch_buderus_heating.discovery import (
    MAX_DISCOVERY_RESOURCES,
    DiscoveryPathDiagnostic,
    DiscoveryPathSource,
)
from custom_components.bosch_buderus_heating.entry_sources import pointt_coordinators
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    AuthTokens,
    BatchItemResult,
    Gateway,
    InvalidPayload,
    RateLimited,
    RequestMetrics,
    RequestTimeout,
    Resource,
    ResourceError,
    ServiceUnavailable,
    TransportError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.pointt.transport import RateLimitBackoff
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    poll_group,
    supports_entity,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    _track_resource_sensors,
    build_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.start_counters import (
    MAX_START_COUNT,
    assess_start_counter,
    registered_start_counter_path,
)
from tests.ha_test_helpers import MockConfigEntry, add_without_device
from tests.helpers.managed_runtime import pointt_client, wait_for_sources

GLOBAL = "/heatSources/numberOfStarts"
SOURCE = "/heatSources/hs1/numberOfStarts"


def global_resource(value: object = 1234, **fields: object) -> Resource:
    return parse_resource(
        {"id": GLOBAL, "type": "floatValue", "value": value, **fields}
    )


def source_resource(**fields: object) -> Resource:
    return parse_resource(
        {"id": SOURCE, "type": "emonValue", "values": [{"total": 42}], **fields}
    )


def coordinator_for(hass: HomeAssistant, *resources: Resource):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one"), entry
    )
    coordinator.update_interval = None
    coordinator.resources = {item.path: item for item in resources}
    coordinator.data = {
        item.path: ResourceSnapshot(item, True, datetime.now(UTC)) for item in resources
    }
    coordinator.last_update_success = True
    return entry, coordinator


def sensors_for(coordinator):
    return {
        (description.resource_path, description.value_key): BoschBuderusSensor(
            coordinator, description
        )
        for description in build_sensor_descriptions(coordinator.resources)
    }


def test_sources_coexist_with_independent_values(hass: HomeAssistant) -> None:
    _, coordinator = coordinator_for(hass, global_resource(), source_resource())
    sensors = sensors_for(coordinator)
    assert sensors[GLOBAL, None].native_value == 1234
    assert sensors[SOURCE, "total"].native_value == 42
    assert sensors[SOURCE, "pool"].native_value is None


def test_unexpected_source_scalar_does_not_hide_global(hass: HomeAssistant) -> None:
    _, coordinator = coordinator_for(
        hass, global_resource(), source_resource(type="floatValue", value=42, values=[])
    )
    sensors = sensors_for(coordinator)
    assert set(sensors) == {(GLOBAL, None)}
    assert sensors[GLOBAL, None].native_value == 1234


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (32767, 32767),
        (32767.0, 32767),
        (-1, None),
        (1.5, None),
        ("1234", None),
        (True, None),
        (None, None),
    ],
)
@pytest.mark.parametrize("unit", [None, " "])
def test_counter_numeric_validation(hass: HomeAssistant, value, expected, unit) -> None:
    _, coordinator = coordinator_for(hass, global_resource(value, unitOfMeasure=unit))
    assert sensors_for(coordinator)[GLOBAL, None].native_value == expected


@pytest.mark.parametrize("field", ["short", "open", "overflow", "underflow", "invalid"])
@pytest.mark.parametrize("unit", [None, " "])
def test_declared_error_value_is_not_a_count(
    hass: HomeAssistant, field: str, unit: str | None
) -> None:
    _, coordinator = coordinator_for(
        hass, global_resource(32767, unitOfMeasure=unit, state=[{field: 32767.0}])
    )
    assert sensors_for(coordinator)[GLOBAL, None].native_value is None


def test_unknown_emon_keys_never_create_entities(hass: HomeAssistant) -> None:
    _, coordinator = coordinator_for(
        hass, source_resource(values=[{"total": 42, "privateExtension": 123}])
    )
    sensors = sensors_for(coordinator)
    assert {key for _, key in sensors} == {"total", "ch", "dhw", "cooling", "pool"}


def test_conflicting_parts_are_unknown_without_hiding_other_parts(
    hass: HomeAssistant,
) -> None:
    _, coordinator = coordinator_for(
        hass, source_resource(values=[{"total": 42}, {"total": 12}, {"ch": 7}])
    )
    sensors = sensors_for(coordinator)
    assert sensors[SOURCE, "total"].native_value is None
    assert sensors[SOURCE, "ch"].native_value == 7


@pytest.mark.parametrize("present", [False, True])
def test_existing_counter_registry_entry_is_never_retired(
    hass: HomeAssistant, present: bool
) -> None:
    entry, coordinator = coordinator_for(
        hass, *([global_resource(), source_resource()] if present else [])
    )
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:numberOfStarts",
        config_entry=entry,
        suggested_object_id="my_start_count",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    registry.async_update_entity(entity.entity_id, name="My start count")
    _remove_retired_entities(hass, entry, (coordinator,))
    retained = registry.async_get(entity.entity_id)
    assert retained is not None
    assert retained.name == "My start count"
    assert retained.disabled_by is er.RegistryEntryDisabler.USER


async def test_outage_and_recovery_never_add_a_second_entity(
    hass: HomeAssistant,
) -> None:
    entry, coordinator = coordinator_for(hass, global_resource(), source_resource())
    added = []
    _track_resource_sensors(entry, coordinator, added.extend)
    original_ids = {sensor.unique_id for sensor in added}
    assert len(original_ids) == 6
    source = coordinator.data[SOURCE]
    for available in (False, True, False, True):
        coordinator.async_set_updated_data(
            {
                GLOBAL: coordinator.data[GLOBAL],
                SOURCE: ResourceSnapshot(
                    source.resource, available, source.last_success
                ),
            }
        )
    assert len(added) == 6
    assert {sensor.unique_id for sensor in added} == original_ids


@pytest.mark.parametrize(
    "state", [None, [], [{"off": 0}], [{"na": 0}], [{"invalid": None}]]
)
def test_optional_non_error_states_preserve_zero(state) -> None:
    assessment = assess_start_counter(global_resource(0, state=state))
    assert assessment.reading(None).value == 0


@pytest.mark.parametrize(
    "state",
    [
        {},
        "bad",
        [None],
        [{"invalid": True}],
        [{"invalid": "1234"}],
        [{"invalid": []}],
        [{"open": float("inf")}],
        [{}] * 33,
    ],
)
def test_malformed_error_metadata_is_local_and_never_published(state) -> None:
    resource = global_resource(state=state)
    assert resource.metadata.numeric_error_values == ()
    assert (
        assess_start_counter(resource).reading(None).reason == "invalid_error_metadata"
    )


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        -float("inf"),
        10**1000,
        MAX_START_COUNT + 1,
        float(MAX_START_COUNT + 1),
    ],
)
def test_unsafe_counts_do_not_overflow_or_round(value) -> None:
    resource = replace(global_resource(), value=value)
    assert assess_start_counter(resource).reading(None).value is None


def test_largest_exact_count_and_duplicate_error_markers() -> None:
    resource = global_resource(MAX_START_COUNT, state=[{"invalid": 12}, {"open": 12.0}])
    assert resource.metadata.numeric_error_values == (12,)
    assert assess_start_counter(resource).reading(None).value == MAX_START_COUNT


@pytest.mark.parametrize(
    ("unit", "supported"),
    [
        (None, True),
        ("", True),
        (" ", True),
        ("  ", True),
        ("\t", True),
        ("\r\n", True),
        ("\u00a0", True),
        ("\u2003", True),
        ("\u202f", True),
        (" \t\u00a0\r\n", True),
        ("other", True),
        (" other ", True),
        ("\tother\u00a0", True),
        ("C", False),
        (" C ", False),
        ("kWh", False),
        (" kWh ", False),
        ("OTHER", False),
        ("ot her", False),
        ("\u200b", False),
        ("\ufeff", False),
        ("\0", False),
    ],
)
@pytest.mark.parametrize("path", [GLOBAL, SOURCE])
def test_start_counter_normalizes_surrounding_unit_whitespace(
    hass: HomeAssistant, path: str, unit: str | None, supported: bool
) -> None:
    factory = global_resource if path == GLOBAL else source_resource
    resource = factory(unitOfMeasure=unit)
    key = None if path == GLOBAL else "total"
    expected = 1234 if path == GLOBAL else 42
    assessment = assess_start_counter(resource)
    assert assessment.supported is supported
    assert supports_entity(resource) is supported
    _, coordinator = coordinator_for(hass, resource)
    sensors = sensors_for(coordinator)
    diagnostic = _capability_diagnostics(resource, coordinator.data[path], {}, 0)
    assert diagnostic["entity_supported"] is supported
    assert diagnostic["start_counter"]["schema_supported"] is supported
    assert poll_group(resource) is PollGroup.SLOW
    if supported:
        assert assessment.reading(key).value == expected
        assert len(sensors) == (1 if path == GLOBAL else 5)
        sensor = sensors[path, key]
        assert sensor.native_value == expected
        assert sensor.native_unit_of_measurement is None
        assert sensor.state_class is SensorStateClass.TOTAL_INCREASING
        assert diagnostic["start_counter"]["readings"][key or "value"] == "valid"
        baseline = factory()
        assert {
            description.unique_key
            for description in build_sensor_descriptions({path: baseline})
        } == {item.entity_description.unique_key for item in sensors.values()}
    else:
        assert assessment.reason == "unsupported_unit"
        assert assessment.reading(key).value is None
        assert sensors == {}
        assert (
            diagnostic["start_counter"]["schema_rejection_reason"] == "unsupported_unit"
        )


@pytest.mark.parametrize("path", [GLOBAL, SOURCE])
@pytest.mark.parametrize("unit", [False, 0, 1, [], {}, [" "]])
def test_malformed_counter_unit_is_rejected_by_parser(path, unit) -> None:
    factory = global_resource if path == GLOBAL else source_resource
    with pytest.raises(InvalidPayload, match="Expected a string field"):
        factory(unitOfMeasure=unit)


@pytest.mark.parametrize("path", [GLOBAL, SOURCE])
@pytest.mark.parametrize("registered", [False, True])
async def test_unit_changes_recover_without_replacing_counter_entities(
    hass: HomeAssistant, path: str, registered: bool
) -> None:
    factory = global_resource if path == GLOBAL else source_resource
    expected = 1234 if path == GLOBAL else 42
    key = None if path == GLOBAL else "total"
    entry, coordinator = coordinator_for(hass, factory(unitOfMeasure="C"))
    if registered:
        coordinator.restore_start_counter_path(path)
    added = []
    _track_resource_sensors(entry, coordinator, added.extend)
    count = 1 if path == GLOBAL else 5
    assert len(added) == (count if registered else 0)
    assert all(sensor.native_value is None for sensor in added)
    original = tuple(added)
    for resource, status, value, reading_reason in (
        (factory(unitOfMeasure=" "), 200, expected, "valid"),
        (factory(unitOfMeasure=" kWh "), 200, None, "unsupported_unit"),
        (None, 404, None, "unavailable"),
        (factory(unitOfMeasure="\tother\u00a0"), 200, expected, "valid"),
        (
            factory(unitOfMeasure=" ", state=[{"invalid": expected}]),
            200,
            None,
            "device_error_value",
        ),
        (factory(unitOfMeasure=" ", state=[]), 200, expected, "valid"),
    ):
        result = BatchItemResult(
            "gateway-one",
            path,
            status,
            resource=resource,
            error=ResourceError(path, status) if status != 200 else None,
        )
        coordinator._apply_results(
            coordinator.data,
            (result,),
            attempted_at=datetime.now(UTC),
            now_monotonic=100,
            source=SnapshotSource.BATCH,
        )
        coordinator.async_set_updated_data(dict(coordinator.data))
        if not original:
            original = tuple(added)
        assert len(added) == count
        assert tuple(added) == original
        sensor = next(
            item for item in added if item.entity_description.value_key == key
        )
        assert sensor.available is (status == 200)
        assert sensor.native_value == value
        assert registered_start_counter_path(sensor.unique_id, "gateway-one") == path
        assert coordinator._paths_by_group[PollGroup.SLOW].count(path) == 1
        snapshot = coordinator.data[path]
        diagnostic = _capability_diagnostics(snapshot.resource, snapshot, {}, 0)
        counter = diagnostic["start_counter"]
        if reading_reason == "unsupported_unit":
            assert counter["schema_rejection_reason"] == reading_reason
        elif status == 200:
            assert counter["readings"][key or "value"] == reading_reason
        else:
            assert diagnostic["available"] is False
        serialized = json.dumps(diagnostic)
        assert str(expected) not in serialized
        assert "kWh" not in json.dumps(counter)
    coordinator.client.get_resource.assert_not_awaited()
    coordinator.client.get_resources_bulk.assert_not_awaited()


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "stringValue"},
        {"unitOfMeasure": "C"},
        {"references": ["/heatSources"]},
        {"values": []},
        {"values": [{"total": 1234}]},
    ],
)
def test_unexpected_global_schema_stays_recoverable(fields) -> None:
    resource = global_resource(**fields)
    assert not assess_start_counter(resource).supported
    assert poll_group(resource) is PollGroup.SLOW


def test_unrelated_resources_and_unknown_keys_have_no_counter_policy() -> None:
    assert not assess_start_counter(Resource("/system/test")).supported
    resource = source_resource(values={"total": 42, "privateUnknown": "secret"})
    assert assess_start_counter(resource).reading("total").value == 42
    assert assess_start_counter(resource).reading("privateUnknown").value is None
    assert assess_start_counter(resource).reading("privateUnknown").reason is not None


@pytest.mark.parametrize("values", [[True], ["bad"], [42]])
def test_malformed_emon_structure_remains_unknown(values) -> None:
    assessment = assess_start_counter(source_resource(values=values))
    assert assessment.supported
    assert {reading.reason for _, reading in assessment.readings} == {
        "invalid_structure"
    }


def test_partial_parts_metadata_and_duplicate_validation() -> None:
    resource = source_resource(
        value={"total": 42}, values=[{"total": 42.0, "pool": True, "ch": 7}]
    )
    assessment = assess_start_counter(resource)
    assert assessment.reading("total").value == 42
    assert assessment.reading("ch").value == 7
    assert assessment.reading("pool").reason == "not_numeric"
    resource = replace(
        resource, metadata=replace(resource.metadata, numeric_error_values=(7,))
    )
    assert assess_start_counter(resource).reading("ch").reason == "device_error_value"


def test_boolean_counter_payloads_never_create_binary_sensors() -> None:
    resources = [global_resource(True), source_resource(values=[{"ch": True}])]
    assert (
        build_binary_sensor_descriptions({item.path: item for item in resources}) == ()
    )


@pytest.mark.parametrize(
    "unique_id",
    [
        "other:heatSources:numberOfStarts",
        "gateway-one:heatSources:hs1:numberOfStarts:private",
        "gateway-one:heatSources:numberOfStarts:total",
        "gateway-one:heatSources:../hs1:numberOfStarts:total",
        "gateway-one:heatSources::numberOfStarts:total",
        "gateway-one:heatSources:.:numberOfStarts:total",
        "gateway-one:heatSources:..:numberOfStarts:total",
        "gateway-one:heatSources:hs?1:numberOfStarts:total",
        "gateway-one:heatSources:hs#1:numberOfStarts:total",
    ],
)
def test_registry_recovery_rejects_foreign_or_ambiguous_ids(unique_id) -> None:
    assert registered_start_counter_path(unique_id, "gateway-one") is None


@pytest.mark.parametrize("source_id", ["hs1", "hs-1", "hs_1", "hs.1", "source_ä"])
def test_created_counter_identities_can_be_restored(source_id) -> None:
    resource = source_resource(id=f"/heatSources/{source_id}/numberOfStarts")
    descriptions = build_sensor_descriptions({resource.path: resource})
    assert len(descriptions) == 5
    for description in descriptions:
        identity = f"gateway-one:{description.unique_key}"
        assert registered_start_counter_path(identity, "gateway-one") == resource.path


@pytest.mark.parametrize("status", [403, 404, 406])
async def test_dotted_source_recovers_through_real_discovery(hass, status) -> None:
    entry, coordinator = coordinator_for(hass)
    entry.runtime_data = SimpleNamespace(coordinators=(coordinator,))
    resource = source_resource(id="/heatSources/hs.1/numberOfStarts")
    description = next(
        item
        for item in build_sensor_descriptions({resource.path: resource})
        if item.value_key == "total"
    )
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"gateway-one:{description.unique_key}",
        config_entry=entry,
    )
    _restore_registered_startup_paths(hass, entry)
    recovering = False

    async def read(gateway, paths):
        results = []
        for path in paths:
            if path == "/heatSources":
                results.append(
                    BatchItemResult(gateway, path, 200, resource=Resource(path))
                )
            elif path == resource.path and recovering:
                results.append(BatchItemResult(gateway, path, 200, resource=resource))
            else:
                failure = status if path == resource.path else 404
                results.append(
                    BatchItemResult(
                        gateway, path, failure, error=ResourceError(path, failure)
                    )
                )
        return tuple(results)

    coordinator.client.get_resources_bulk.side_effect = read
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.monotonic",
        return_value=100,
    ):
        coordinator.data = await coordinator._async_discover_data()
    requested = {
        path
        for call in coordinator.client.get_resources_bulk.call_args_list
        for path in call.args[1]
    }
    assert resource.path in requested
    assert resource.path in coordinator._paths_by_group[PollGroup.SLOW]
    added = []
    _track_resource_sensors(entry, coordinator, added.extend)
    sensor = next(item for item in added if item.unique_id == existing.unique_id)
    assert not sensor.available
    assert sensor.native_value is None
    coordinator.client.get_resources_bulk.reset_mock()
    coordinator._next_update = {
        group: float("inf") for group in coordinator._poll_intervals
    }
    coordinator._next_update[PollGroup.SLOW] = 1000
    recovering = True
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.monotonic",
        return_value=1000,
    ):
        coordinator.data = await coordinator._async_update_data_locked()
    if status == 403:
        assert all(
            resource.path not in call.args[1]
            for call in coordinator.client.get_resources_bulk.call_args_list
        )
        assert not sensor.available
        with patch(
            "custom_components.bosch_buderus_heating.coordinator.monotonic",
            return_value=86501,
        ):
            coordinator.data = await coordinator._async_update_data_locked()
    poll_calls = coordinator.client.get_resources_bulk.call_args_list
    assert sum(resource.path in call.args[1] for call in poll_calls) == 1
    assert all(call.args[0] == "gateway-one" for call in poll_calls)
    assert all(len(call.args[1]) <= 30 for call in poll_calls)
    coordinator.client.get_resource.assert_not_awaited()
    assert sensor.available
    assert sensor.native_value == 42
    assert sensor.unique_id == existing.unique_id
    assert len(added) == 5


@pytest.mark.parametrize(
    "disabled_by",
    [None, er.RegistryEntryDisabler.USER, er.RegistryEntryDisabler.INTEGRATION],
)
def test_restore_and_migration_preserve_choices_per_gateway(hass, disabled_by) -> None:
    entry, first = coordinator_for(hass, global_resource(), source_resource())
    second = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-two"), entry
    )
    entry.runtime_data = SimpleNamespace(coordinators=(first, second))
    registry = er.async_get(hass)
    entities = []
    for gateway in ("gateway-one", "gateway-two"):
        for suffix in (
            "heatSources:numberOfStarts",
            "heatSources:hs1:numberOfStarts:pool",
        ):
            entities.append(
                registry.async_get_or_create(
                    "sensor",
                    DOMAIN,
                    f"{gateway}:{suffix}",
                    config_entry=entry,
                    disabled_by=disabled_by,
                )
            )
    _restore_registered_startup_paths(hass, entry)
    assert first.registered_start_counter_paths == {GLOBAL, SOURCE}
    assert second.registered_start_counter_paths == {GLOBAL, SOURCE}
    _enable_new_default_entities(hass, entry, (first, second))
    assert all(
        registry.async_get(item.entity_id).disabled_by is disabled_by
        for item in entities
    )


@pytest.mark.parametrize("available", [None, False, True])
def test_diagnostics_explains_validation_without_values(available) -> None:
    resource = global_resource(
        87654321, state=[{"invalid": 87654321, "secret": "private"}]
    )
    snapshot = (
        None
        if available is None
        else ResourceSnapshot(resource, available, datetime.now(UTC))
    )
    report = _capability_diagnostics(resource, snapshot, {}, 0)
    counter = report["start_counter"]
    assert counter["schema_supported"]
    assert counter["error_marker_count"] == 1
    assert counter["readings"]["value"] == (
        "not_read"
        if available is None
        else "device_error_value"
        if available
        else "unavailable"
    )
    serialized = json.dumps(report)
    assert "87654321" not in serialized
    assert "private" not in serialized
    assert "secret" not in serialized


@pytest.mark.parametrize(
    "profile,seconds",
    [(PollingProfile.STANDARD, 900), (PollingProfile.CLOUD_FRIENDLY, 1800)],
)
async def test_known_counter_recovery_uses_profile_and_no_extra_get(
    hass, profile, seconds
) -> None:
    entry, _ = coordinator_for(hass)
    client = AsyncMock()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway-one"), entry, profile
    )
    coordinator.restore_start_counter_path(GLOBAL)
    fast = parse_resource({"id": "/heatSources/actualModulation", "value": 20})
    with (
        patch(
            "custom_components.bosch_buderus_heating.coordinator.async_discover_resources",
            return_value={fast.path: fast},
        ) as discovery,
        patch(
            "custom_components.bosch_buderus_heating.coordinator.fault_resource_candidates",
            return_value=(),
        ),
        patch(
            "custom_components.bosch_buderus_heating.coordinator.monotonic",
            return_value=100,
        ),
    ):
        coordinator.data = await coordinator._async_discover_data()
    assert discovery.call_args.kwargs["roots"][0] == GLOBAL
    assert GLOBAL in coordinator._paths_by_group[PollGroup.SLOW]
    assert coordinator._next_update[PollGroup.SLOW] == 100 + seconds
    coordinator._next_update = {
        group: float("inf") for group in coordinator._poll_intervals
    }
    coordinator._next_update[PollGroup.SLOW] = 100 + seconds
    client.get_resources_bulk.return_value = (
        BatchItemResult("gateway-one", GLOBAL, 200, resource=global_resource()),
    )
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.monotonic",
        return_value=100 + seconds - 10,
    ):
        await coordinator._async_update_data_locked()
    client.get_resources_bulk.assert_not_awaited()
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.monotonic",
        return_value=100 + seconds,
    ):
        coordinator.data = await coordinator._async_update_data_locked()
    client.get_resources_bulk.assert_awaited_once_with("gateway-one", (GLOBAL,))
    client.get_resource.assert_not_awaited()
    assert coordinator.data[GLOBAL].resource.value == 1234
    assert coordinator._paths_by_group[PollGroup.SLOW].count(GLOBAL) == 1


@pytest.mark.parametrize(
    "registered,status,paused",
    [
        (True, 404, False),
        (False, 404, True),
        (True, 403, True),
        (False, 403, True),
        (False, 406, False),
    ],
)
def test_discovery_failures_respect_known_paths_and_pauses(
    hass, registered, status, paused
) -> None:
    _, coordinator = coordinator_for(hass)
    if registered:
        coordinator.restore_start_counter_path(GLOBAL)
    coordinator.discovery_diagnostics.paths[GLOBAL] = DiscoveryPathDiagnostic(
        source=DiscoveryPathSource.REFERENCE,
        bulk_result=f"http_{status}",
    )
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.monotonic",
        return_value=100,
    ):
        coordinator._merge_start_counter_candidates()
    assert GLOBAL in coordinator.resources
    assert coordinator._resource_is_paused(GLOBAL, 101) is paused
    assert GLOBAL not in (coordinator.data or {})


def test_optional_and_depth_limited_paths_are_not_promoted(hass) -> None:
    _, coordinator = coordinator_for(hass)
    coordinator.discovery_diagnostics.paths[GLOBAL] = DiscoveryPathDiagnostic(
        source=DiscoveryPathSource.OPTIONAL,
        bulk_result="http_404",
    )
    coordinator.discovery_diagnostics.paths[SOURCE] = DiscoveryPathDiagnostic(
        source=DiscoveryPathSource.REFERENCE
    )
    coordinator._merge_start_counter_candidates()
    assert coordinator.resources == {}


def test_recovery_shares_discovery_budget(hass) -> None:
    _, coordinator = coordinator_for(hass)
    coordinator.startup_data.reserve_discovery(
        tuple(f"/synthetic/{i}" for i in range(MAX_DISCOVERY_RESOURCES))
    )
    coordinator.restore_start_counter_path(GLOBAL)
    coordinator.restore_start_counter_path("/not/a/counter")
    coordinator._merge_start_counter_candidates()
    assert GLOBAL not in coordinator.resources
    assert coordinator.start_counter_recovery_diagnostics()["resource_limit_reached"]
    assert len(coordinator.startup_data.requested) == MAX_DISCOVERY_RESOURCES


@pytest.mark.parametrize("status", [403, 404, 406, 504])
def test_failed_reads_keep_identity_and_recover(hass, status) -> None:
    _, coordinator = coordinator_for(hass, global_resource())
    sensor = sensors_for(coordinator)[GLOBAL, None]
    identity = sensor.unique_id
    before = sensor.native_value
    now = datetime.now(UTC)
    for index in (1, 2):
        coordinator._apply_results(
            coordinator.data,
            (
                BatchItemResult(
                    "gateway-one", GLOBAL, status, error=ResourceError(GLOBAL, status)
                ),
            ),
            attempted_at=now,
            now_monotonic=index,
            source=SnapshotSource.BATCH,
        )
        if status == 504:
            # A gateway timeout is no answer for the reading: the last count
            # stays shown within its validity limit (reading validity).
            assert sensor.available and sensor.native_value == before
        else:
            assert not sensor.available
            assert sensor.native_value is None
    assert (GLOBAL in coordinator._negative_until) is (status in (403, 504))
    coordinator._apply_results(
        coordinator.data,
        (BatchItemResult("gateway-one", GLOBAL, 200, resource=global_resource(3)),),
        attempted_at=now,
        now_monotonic=100000,
        source=SnapshotSource.BATCH,
    )
    assert sensor.available
    assert sensor.native_value == 3
    assert sensor.unique_id == identity
    assert GLOBAL not in coordinator._negative_until


def test_schema_and_error_metadata_changes_do_not_reuse_old_readings(hass) -> None:
    _, coordinator = coordinator_for(hass, global_resource())
    sensor = sensors_for(coordinator)[GLOBAL, None]
    for resource, expected in (
        (global_resource(77, state=[{"invalid": 77}]), None),
        (global_resource(77, state=[]), 77),
        (global_resource(77, type="stringValue"), None),
        (global_resource(78), 78),
    ):
        coordinator._apply_results(
            coordinator.data,
            (BatchItemResult("gateway-one", GLOBAL, 200, resource=resource),),
            attempted_at=datetime.now(UTC),
            now_monotonic=100,
            source=SnapshotSource.BATCH,
        )
        assert sensor.native_value == expected
        assert coordinator._paths_by_group[PollGroup.SLOW].count(GLOBAL) == 1


@pytest.mark.parametrize(
    "error",
    [RateLimited(600), ServiceUnavailable(503), RequestTimeout(), TransportError()],
)
async def test_counter_recovery_stops_on_cloud_failures(hass, error) -> None:
    _, coordinator = coordinator_for(hass, global_resource())
    coordinator._paths_by_group = {PollGroup.SLOW: (GLOBAL,)}
    coordinator._next_update = {
        group: float("inf") for group in coordinator._poll_intervals
    }
    coordinator._next_update[PollGroup.SLOW] = 0
    coordinator.client.get_resources_bulk.side_effect = error
    with patch(
        "custom_components.bosch_buderus_heating.coordinator.monotonic",
        return_value=100,
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data_locked()
        if isinstance(error, RateLimited):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data_locked()
    coordinator.client.get_resources_bulk.assert_awaited_once()
    coordinator.client.get_resource.assert_not_awaited()
    assert GLOBAL in coordinator.resources


async def test_auth_failure_during_counter_recovery_requires_reauth(hass) -> None:
    _, coordinator = coordinator_for(hass, global_resource())
    coordinator._paths_by_group = {PollGroup.SLOW: (GLOBAL,)}
    coordinator.client.get_resources_bulk.side_effect = AuthenticationError()
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data_locked()
    coordinator.client.get_resource.assert_not_awaited()


async def test_counter_batches_are_bounded_and_disjoint(hass) -> None:
    resources = [
        source_resource(id=f"/heatSources/hs{i}/numberOfStarts") for i in range(65)
    ]
    _, coordinator = coordinator_for(hass, *resources)
    coordinator._paths_by_group = {
        PollGroup.SLOW: tuple(resource.path for resource in resources)
    }
    coordinator.client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
        BatchItemResult(gateway, path, 200, resource=coordinator.resources[path])
        for path in paths
    )
    await coordinator._async_update_data_locked()
    batches = [
        call.args[1] for call in coordinator.client.get_resources_bulk.call_args_list
    ]
    assert [len(batch) for batch in batches] == [30, 30, 5]
    assert len({path for batch in batches for path in batch}) == 65
    coordinator.client.get_resource.assert_not_awaited()


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("unit", [None, " ", " other "])
@pytest.mark.parametrize("source_scalar", [False, True])
async def test_real_ha_setup_reload_and_two_gateways_keep_counter_entities(
    hass,
    unit,
    source_scalar,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_BRAND: "buderus",
            CONF_GATEWAY_IDS: ["gateway-one", "gateway-two"],
            **tokens_to_data(AuthTokens("access", "refresh", expires_at=4e9)),
        },
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    existing = {}
    for gateway in ("gateway-one", "gateway-two"):
        entity = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{gateway}:heatSources:numberOfStarts",
            config_entry=entry,
        )
        registry.async_update_entity(entity.entity_id, name=f"My starts {gateway}")
        existing[gateway] = entity
    disabled = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:hs1:numberOfStarts:pool",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    client = Mock()
    scalar_entry = None
    if source_scalar:
        scalar_entry = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            "gateway-one:heatSources:hs1:numberOfStarts",
            config_entry=entry,
        )
        registry.async_update_entity(scalar_entry.entity_id, name="My WDDW2 starts")

    def current_source():
        if source_scalar:
            return global_resource(42, id=SOURCE, unitOfMeasure=unit)
        return source_resource(unitOfMeasure=unit)

    client.metrics = RequestMetrics()
    client.backoff = RateLimitBackoff()
    client.get_gateways = AsyncMock(
        return_value=(Gateway("gateway-one"), Gateway("gateway-two"))
    )
    fast = parse_resource({"id": "/heatSources/actualModulation", "value": 20})
    client.get_resources_bulk = AsyncMock(
        side_effect=lambda gateway, paths: tuple(
            BatchItemResult(
                gateway,
                path,
                200,
                resource=(
                    global_resource(
                        1234 if gateway == "gateway-one" else 9876, unitOfMeasure=unit
                    )
                    if path == GLOBAL
                    else current_source()
                    if path == SOURCE
                    else fast
                ),
            )
            for path in paths
        )
    )
    client.get_resource = AsyncMock()

    async def discover(*args, startup_data, roots, **kwargs):
        startup_data.begin_discovery()
        assert GLOBAL in roots
        return {fast.path: fast}

    with (
        pointt_client(client),
        patch(
            "custom_components.bosch_buderus_heating.coordinator.async_discover_resources",
            side_effect=discover,
        ),
        patch(
            "custom_components.bosch_buderus_heating.coordinator.fault_resource_candidates",
            return_value=(),
        ),
        patch(
            "custom_components.bosch_buderus_heating.async_setup_notifications",
            AsyncMock(return_value=()),
        ),
    ):
        for _ in range(2):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await wait_for_sources(hass, entry, 2)
            await hass.async_block_till_done()
            for entity in existing.values():
                assert hass.states.get(entity.entity_id).state == "unavailable"
            for coordinator in pointt_coordinators(entry):
                coordinator._next_update[PollGroup.SLOW] = 0
                await coordinator.async_refresh()
            await hass.async_block_till_done()
            assert hass.states.get(existing["gateway-one"].entity_id).state == "1234"
            assert hass.states.get(existing["gateway-two"].entity_id).state == "9876"
            if scalar_entry is not None:
                assert hass.states.get(scalar_entry.entity_id).state == "42"
                assert (
                    registry.async_get(scalar_entry.entity_id).name == "My WDDW2 starts"
                )
            for gateway, entity in existing.items():
                retained = registry.async_get(entity.entity_id)
                assert retained.unique_id == entity.unique_id
                assert retained.name == f"My starts {gateway}"
                assert (
                    hass.states.get(entity.entity_id).attributes["state_class"]
                    == "total_increasing"
                )
            assert (
                registry.async_get(disabled.entity_id).disabled_by
                is er.RegistryEntryDisabler.USER
            )
            assert hass.states.get(disabled.entity_id) is None
            first = next(
                coordinator
                for coordinator in pointt_coordinators(entry)
                if coordinator.gateway.gateway_id == "gateway-one"
            )
            first._apply_results(
                first.data,
                (
                    BatchItemResult(
                        "gateway-one",
                        GLOBAL,
                        200,
                        resource=global_resource(777, state=[{"invalid": 777}]),
                    ),
                ),
                attempted_at=datetime.now(UTC),
                now_monotonic=100,
                source=SnapshotSource.BATCH,
            )
            first.async_set_updated_data(dict(first.data))
            await hass.async_block_till_done()
            assert hass.states.get(existing["gateway-one"].entity_id).state == "unknown"
            assert hass.states.get(existing["gateway-two"].entity_id).state == "9876"
            counters_before_unload = {
                item.entity_id
                for item in er.async_entries_for_config_entry(registry, entry.entry_id)
                if "numberOfStarts" in item.unique_id
            }
            assert len(counters_before_unload) == (8 if source_scalar else 7)
            unloaded_coordinators = pointt_coordinators(entry)
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
            for coordinator in unloaded_coordinators:
                assert not coordinator._listeners
        client.get_resource.assert_not_awaited()


@pytest.fixture
def recorder_runtime_annotations(monkeypatch):
    """Supply a type-only HA name for the recorder fixture's Python 3.14 autospec."""
    from homeassistant.components.recorder import Recorder, migration
    from homeassistant.helpers import recorder as recorder_helper
    from sqlalchemy.orm import Session

    monkeypatch.setattr(migration, "Recorder", Recorder, raising=False)
    monkeypatch.setattr(recorder_helper, "Session", Session, raising=False)
    monkeypatch.setattr(recorder_helper, "Recorder", Recorder, raising=False)


@pytest.mark.parametrize("counter_path", [GLOBAL, SOURCE])
async def test_recorder_counts_only_valid_increments_and_device_reset(
    recorder_runtime_annotations, recorder_mock, hass, freezer, counter_path
) -> None:
    from homeassistant.components.recorder.statistics import (
        get_last_short_term_statistics,
    )
    from homeassistant.setup import async_setup_component

    from tests.ha_recorder_helpers import (
        async_wait_recording_done,
        do_adhoc_statistics,
        get_start_time,
    )

    def counter_resource(value, **fields):
        return global_resource(value, id=counter_path, **fields)

    start = get_start_time(datetime.now(UTC)) + timedelta(minutes=5)
    freezer.move_to(start)
    assert await async_setup_component(hass, "sensor", {})
    _, coordinator = coordinator_for(hass, counter_resource(100))
    sensor = sensors_for(coordinator)[counter_path, None]
    await add_without_device(hass, sensor)
    await async_wait_recording_done(hass)
    for minute, resource, available in (
        (1, counter_resource(105), True),
        (2, counter_resource(32767, state=[{"invalid": 32767}]), True),
        (3, counter_resource(105), False),
        (4, counter_resource(110), True),
    ):
        freezer.move_to(start + timedelta(minutes=minute))
        coordinator.async_set_updated_data(
            {counter_path: ResourceSnapshot(resource, available, datetime.now(UTC))}
        )
        await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(minutes=5))
    do_adhoc_statistics(hass, start=start)
    await async_wait_recording_done(hass)
    coordinator.async_set_updated_data(
        {counter_path: ResourceSnapshot(counter_resource(3), True, datetime.now(UTC))}
    )
    await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(minutes=10))
    do_adhoc_statistics(hass, start=start + timedelta(minutes=5))
    await async_wait_recording_done(hass)
    result = await recorder_mock.async_add_executor_job(
        get_last_short_term_statistics,
        hass,
        2,
        sensor.entity_id,
        False,
        {"state", "sum"},
    )
    rows = sorted(result[sensor.entity_id], key=lambda row: row["start"])
    assert [(row["state"], row["sum"]) for row in rows] == [(110, 10), (3, 13)]
