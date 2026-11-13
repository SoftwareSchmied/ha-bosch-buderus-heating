"""Synthetic compatibility regressions; no physical WDDW2 device was used."""

import json
import math
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import _restore_registered_startup_paths
from custom_components.bosch_buderus_heating.binary_sensor import (
    BoschBuderusBinarySensor,
    _track_binary_sensors,
    build_binary_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.dhw_safety import (
    assess_dhw_safety,
    is_dhw_safety_path,
)
from custom_components.bosch_buderus_heating.diagnostics import _capability_diagnostics
from custom_components.bosch_buderus_heating.discovery import MAX_DISCOVERY_RESOURCES
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    InvalidPayload,
    ResourceError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    poll_group,
    resource_name,
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
from custom_components.bosch_buderus_heating.writes import assess_control
from tests.ha_test_helpers import MockConfigEntry

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/wddw2/synthetic-readings.json").read_text()
)


def resource_for(name, **fields):
    return parse_resource({**FIXTURES[name], **fields})


def coordinator_for(hass, *resources):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one"), entry
    )
    coordinator.update_interval = None
    coordinator.resources = {r.path: r for r in resources}
    coordinator.data = {
        r.path: ResourceSnapshot(r, True, datetime.now(UTC)) for r in resources
    }
    coordinator.last_update_success = True
    return entry, coordinator


def test_wddw2_safety_is_an_active_state(hass: HomeAssistant) -> None:
    resource = resource_for("safety")
    _, coordinator = coordinator_for(hass, resource)
    descriptions = build_binary_sensor_descriptions(coordinator.resources)
    assert len(descriptions) == 1
    sensor = BoschBuderusBinarySensor(coordinator, descriptions[0])
    assert sensor.is_on is True
    assert sensor.unique_id == "gateway-one:dhwCircuits:dhw1:safetyTemperature"
    assert build_sensor_descriptions(coordinator.resources) == ()


def test_wddw2_source_scalar_has_its_own_identity(hass: HomeAssistant) -> None:
    resource = resource_for("source_counter")
    _, coordinator = coordinator_for(hass, resource)
    descriptions = build_sensor_descriptions(coordinator.resources)
    assert len(descriptions) == 1
    sensor = BoschBuderusSensor(coordinator, descriptions[0])
    assert sensor.native_value == 42
    assert sensor.unique_id == "gateway-one:heatSources:hs1:numberOfStarts"
    assert sensor.entity_description.value_key is None
    assert build_binary_sensor_descriptions(coordinator.resources) == ()


@pytest.mark.parametrize(
    ("value", "expected", "reason"),
    [
        ("on", True, None),
        ("off", False, None),
        (None, None, "missing_value"),
        (False, None, "not_string"),
        (1, None, "not_string"),
        ("private-unknown-state", None, "unknown_state"),
        ("", None, "unknown_state"),
    ],
)
def test_safety_states_do_not_guess_or_leak(hass, value, expected, reason) -> None:
    resource = resource_for("safety", value=value, writeable=1)
    _, coordinator = coordinator_for(hass, resource)
    description = build_binary_sensor_descriptions(coordinator.resources)[0]
    sensor = BoschBuderusBinarySensor(coordinator, description)
    assert sensor.is_on is expected
    assert not assess_control(resource).eligible
    report = _capability_diagnostics(resource, coordinator.data[resource.path], {}, 0)
    assert report["dhw_safety"]["value_valid"] is (expected is not None)
    assert report["dhw_safety"]["rejection_reason"] == reason
    assert "private-unknown-state" not in json.dumps(report)


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"type": "floatValue", "value": 60}, "unsupported_schema"),
        ({"type": "booleanValue", "value": True}, "unsupported_schema"),
        ({"references": ["/dhwCircuits/dhw1"]}, "unsupported_schema"),
        ({"values": []}, "unsupported_schema"),
        ({"value": {"private": True}}, "unsupported_schema"),
        ({"value": ["on"]}, "unsupported_schema"),
        ({"unitOfMeasure": "C"}, "unsupported_unit"),
    ],
)
def test_safety_rejects_other_shapes_and_keeps_polling(fields, reason) -> None:
    resource = resource_for("safety", **fields)
    assert not supports_entity(resource)
    assert assess_dhw_safety(resource).reason == reason
    assert not build_binary_sensor_descriptions({resource.path: resource})
    assert not build_sensor_descriptions({resource.path: resource})
    assert not assess_control(resource).eligible
    assert poll_group(resource) is PollGroup.FAST


@pytest.mark.parametrize(
    "path",
    [
        "/system/safetyTemperature",
        "/dhwCircuits/../safetyTemperature",
        "/dhwCircuits/dhw?1/safetyTemperature",
    ],
)
def test_safety_contract_does_not_match_other_or_noncanonical_paths(path) -> None:
    assert not is_dhw_safety_path(path)
    resource = replace(resource_for("safety"), path=path)
    assert assess_dhw_safety(resource).reason == "not_dhw_safety"


@pytest.mark.parametrize("unit", [None, "", " ", "other"])
def test_safety_accepts_only_dimensionless_metadata(unit) -> None:
    assert assess_dhw_safety(resource_for("safety", unitOfMeasure=unit)).value is True


def test_safety_names_describe_an_active_state() -> None:
    path = FIXTURES["safety"]["id"]
    assert resource_name(path, language="de") == "Sicherheitstemperatur aktiv"
    assert resource_name(path, language="en") == "Safety temperature active"


async def test_safety_recovers_after_malformed_start_and_failed_poll(hass) -> None:
    bad = resource_for("safety", type="floatValue", value=60, writeable=1)
    entry, coordinator = coordinator_for(hass, bad)
    added = []
    _track_binary_sensors(entry, coordinator, added.extend)
    assert not added
    assert poll_group(bad) is PollGroup.CONTROL
    original = None
    for fields, status, expected in (
        ({"value": "on"}, 200, True),
        ({"value": "off"}, 200, False),
        ({"type": "floatValue", "value": 60}, 200, None),
        ({"value": "on"}, 404, None),
        ({"value": "on"}, 200, True),
    ):
        resource = resource_for("safety", **fields)
        coordinator._apply_results(
            coordinator.data,
            (
                BatchItemResult(
                    "gateway-one",
                    resource.path,
                    status,
                    resource=resource if status == 200 else None,
                    error=ResourceError(resource.path, status)
                    if status != 200
                    else None,
                ),
            ),
            attempted_at=datetime.now(UTC),
            now_monotonic=100,
            source=SnapshotSource.BATCH,
        )
        coordinator.async_set_updated_data(dict(coordinator.data))
        assert len(added) == 1
        original = original or added[0]
        assert added[0] is original
        assert original.is_on is expected
        assert original.available is (status == 200)
    snapshot = coordinator.data[bad.path]
    coordinator.data[bad.path] = replace(snapshot, freshness=Freshness.STALE)
    assert not original.available
    assert original.is_on is None
    coordinator.client.get_resource.assert_not_awaited()
    coordinator.client.get_resources_bulk.assert_not_awaited()


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (0, None),
        (42.0, None),
        (MAX_START_COUNT, None),
        (-1, "out_of_range"),
        (MAX_START_COUNT + 1, "out_of_range"),
        (1.5, "not_integer"),
        (True, "not_numeric"),
        ("42", "not_numeric"),
        (None, "missing_value"),
        (float("nan"), "not_finite"),
        (float("inf"), "not_finite"),
    ],
)
def test_source_scalar_uses_the_same_bounded_count_contract(value, reason) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        with pytest.raises(InvalidPayload, match="finite JSON number"):
            resource_for("source_counter", value=value)
        resource = replace(resource_for("source_counter"), value=value)
    else:
        resource = resource_for("source_counter", value=value)
    assessment = assess_start_counter(resource)
    assert assessment.supported
    assert assessment.reading(None).reason == reason
    assert assessment.reading("total").value is None
    report = _capability_diagnostics(
        resource, ResourceSnapshot(resource, True, datetime.now(UTC)), {}, 0
    )
    assert report["start_counter"]["readings"] == {"value": reason or "valid"}


@pytest.mark.parametrize(
    ("fields", "schema_supported", "reason"),
    [
        ({"unitOfMeasure": "C"}, False, "unsupported_unit"),
        ({"unitOfMeasure": " other "}, True, None),
        ({"state": [{"invalid": 42}]}, True, "device_error_value"),
        ({"state": [{"invalid": "private"}]}, True, "invalid_error_metadata"),
        ({"values": []}, False, "unsupported_schema"),
        ({"references": ["/heatSources/hs1"]}, False, "unsupported_schema"),
        ({"type": "stringValue"}, False, "unsupported_schema"),
    ],
)
def test_source_scalar_metadata_validation(fields, schema_supported, reason) -> None:
    resource = resource_for("source_counter", **fields)
    assessment = assess_start_counter(resource)
    assert assessment.supported is schema_supported
    assert assessment.reading(None).reason == reason
    assert poll_group(resource) is PollGroup.SLOW
    assert not assess_control(resource).eligible


def test_global_two_sources_and_parts_are_independent(hass) -> None:
    resources = [
        resource_for("source_counter", id="/heatSources/numberOfStarts", value=1234),
        resource_for("source_counter"),
        parse_resource(
            {
                "id": "/heatSources/hs2/numberOfStarts",
                "type": "emonValue",
                "values": [{"total": 7, "ch": 3}],
            }
        ),
    ]
    _, coordinator = coordinator_for(hass, *resources)
    sensors = {
        d.unique_key: BoschBuderusSensor(coordinator, d)
        for d in build_sensor_descriptions(coordinator.resources)
    }
    assert len(sensors) == 7
    assert sensors["heatSources:numberOfStarts"].native_value == 1234
    assert sensors["heatSources:hs1:numberOfStarts"].native_value == 42
    assert sensors["heatSources:hs2:numberOfStarts:total"].native_value == 7
    assert sensors["heatSources:hs2:numberOfStarts:ch"].native_value == 3
    assert sensors["heatSources:hs2:numberOfStarts:pool"].native_value is None


@pytest.mark.parametrize("source_id", ["hs1", "hs.1", "source_ä"])
def test_registered_scalar_and_parts_survive_shape_changes(hass, source_id) -> None:
    path = f"/heatSources/{source_id}/numberOfStarts"
    scalar = resource_for("source_counter", id=path)
    parts = parse_resource(
        {
            "id": path,
            "type": "emonValue",
            "values": [{"total": 7}],
        }
    )
    entry, coordinator = coordinator_for(hass)
    entry.runtime_data = SimpleNamespace(coordinators=(coordinator,))
    registry = er.async_get(hass)
    identities = [
        f"gateway-one:heatSources:{source_id}:numberOfStarts",
        f"gateway-one:heatSources:{source_id}:numberOfStarts:total",
    ]
    for identity in identities:
        registered = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            identity,
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        registry.async_update_entity(registered.entity_id, name="My counter")
        assert registered_start_counter_path(identity, "gateway-one") == path
    _restore_registered_startup_paths(hass, entry)
    added = []
    _track_resource_sensors(entry, coordinator, added.extend)
    assert len(added) == 6
    assert set(identities) <= {sensor.unique_id for sensor in added}
    assert all(not sensor.available for sensor in added)
    original = tuple(added)
    for resource in (scalar, parts, scalar):
        coordinator.async_set_updated_data(
            {
                path: ResourceSnapshot(resource, True, datetime.now(UTC)),
            }
        )
        assert tuple(added) == original
        by_id = {sensor.unique_id: sensor for sensor in added}
        assert by_id[identities[0]].native_value == (42 if resource is scalar else None)
        assert by_id[identities[1]].native_value == (7 if resource is parts else None)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        assert entity.name == "My counter"
        assert entity.disabled_by is er.RegistryEntryDisabler.USER


def test_counter_limit_keeps_second_shape_of_an_existing_path(hass) -> None:
    _, coordinator = coordinator_for(hass)
    path = FIXTURES["source_counter"]["id"]
    coordinator.restore_start_counter_path(path)
    for index in range(2, MAX_DISCOVERY_RESOURCES + 1):
        coordinator.restore_start_counter_path(f"/heatSources/hs{index}/numberOfStarts")
    coordinator.restore_start_counter_path(path, scalar=True)
    coordinator.restore_start_counter_path(
        "/heatSources/extra/numberOfStarts", scalar=True
    )
    assert len(coordinator.registered_start_counter_paths) == MAX_DISCOVERY_RESOURCES
    assert coordinator.registered_start_counter_shapes[path] == {False, True}
    assert (
        "/heatSources/extra/numberOfStarts"
        not in coordinator.registered_start_counter_shapes
    )
