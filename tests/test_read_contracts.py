"""Regression examples for collection shapes and duration interpretation."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.diagnostics import _capability_diagnostics
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    Resource,
    ResourceMetadata,
    ResourceReference,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.read_contracts import assess_runtime
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

FIXTURES = Path(__file__).parent / "fixtures/contracts"


def runtime(path, value, unit):
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit=unit),
    )


def sensor_for(hass, resource, key=None):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-fixture"), entry
    )
    coordinator.resources = {resource.path: resource}
    coordinator.data = {
        resource.path: ResourceSnapshot(resource, True, datetime.now(UTC))
    }
    coordinator.last_update_success = True
    description = next(
        item
        for item in build_sensor_descriptions(coordinator.resources)
        if item.value_key == key
    )
    return BoschBuderusSensor(coordinator, description)


@pytest.mark.parametrize(
    ("path", "value", "unit", "expected"),
    [
        ("/heatSources/workingTime/totalSystem", 120, "min", 2),
        ("/heatSources/workingTime/totalSystem", 2, "hour", 2),
        ("/heatSources/hs1/operationHours", 2, None, 2),
    ],
)
def test_duration_contract_converts_to_hours(hass, path, value, unit, expected):
    entity = sensor_for(hass, runtime(path, value, unit))
    assert entity.native_value == expected
    assert entity.native_unit_of_measurement == "h"


def test_unknown_duration_unit_cannot_create_a_reading():
    resource = runtime("/heatSources/workingTime/totalSystem", 120, "C")
    assert build_sensor_descriptions({resource.path: resource}) == ()


def test_monitor_collection_never_becomes_a_scalar():
    resource = runtime("/dhwCircuits/dhw1/monitorValues", 20, "C")
    assert build_sensor_descriptions({resource.path: resource}) == ()


def test_previously_created_monitor_sensor_does_not_publish_a_scalar(hass):
    entity = sensor_for(hass, runtime("/heatSources/hs1/operationHours", 2, "h"))
    collection = runtime("/dhwCircuits/dhw1/monitorValues", 20, "C")
    entity.entity_description = replace(
        entity.entity_description, resource_path=collection.path
    )
    entity.coordinator.data = {
        collection.path: ResourceSnapshot(collection, True, datetime.now(UTC))
    }
    assert entity.native_value is None


def test_duration_uses_current_unit_after_metadata_changes(hass):
    resource = runtime("/heatSources/workingTime/totalSystem", 3600, "s")
    entity = sensor_for(hass, resource)
    assert entity.native_value == 1
    identity = entity.unique_id
    changed = runtime(resource.path, 2, "h")
    entity.coordinator.data[resource.path] = ResourceSnapshot(
        changed, True, datetime.now(UTC)
    )
    assert entity.native_value == 2
    assert entity.unique_id == identity


def fixture_cases():
    cases = []
    for path in sorted(FIXTURES.glob("*/readings.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        for item in fixture["cases"]:
            cases.append(pytest.param(item, id=f"{fixture['family']}:{item['id']}"))
    return cases


@pytest.mark.parametrize("case", fixture_cases())
def test_family_reading_contracts_reach_ha(hass, case):
    resource = parse_resource(case["payload"])
    expected = case["expected"]
    entity = sensor_for(hass, resource, expected["part"])
    assert entity.native_value == expected["value"]
    assert entity.native_unit_of_measurement == expected["unit"]


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*/*.json")))
def test_fixtures_distinguish_synthetic_evidence_from_device_captures(path):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    provenance = fixture["provenance"]
    if provenance["kind"] == "anonymized_capture":
        assert provenance["basis"] == "observed_structure"
        assert provenance["capture_date"]
        assert provenance["redaction"]
        assert provenance["physical_device_verified"] is True
        return
    assert provenance["kind"] == "synthetic"
    assert provenance["basis"] == "compatibility_mapping"
    assert provenance["firmware"] is None
    assert provenance["capture_date"] is None
    assert provenance["physical_device_verified"] is False


@pytest.mark.parametrize(
    ("units", "value"),
    [
        (("s", "sec", "secs", "seconds"), 7200),
        (("m", "min", "mins", "minutes"), 120),
        (("h", "hour", "hours"), 2),
    ],
)
def test_duration_unit_aliases_have_exact_scaling(units, value):
    for unit in units:
        resource = runtime("/heatSources/workingTime/totalSystem", value, unit)
        assessment = assess_runtime(resource)
        assert assessment.supported
        assert assessment.reading(None).value == 2
        assert assessment.unit_source == "metadata"


@pytest.mark.parametrize("unit", [None, "", " ", "C", "unknown-private-unit", " h "])
def test_global_runtime_does_not_guess_unresolved_units(unit):
    resource = runtime("/heatSources/workingTime/totalSystem", 120, unit)
    assessment = assess_runtime(resource)
    assert not assessment.supported
    assert not supports_entity(resource)
    assert poll_group(resource) is PollGroup.SLOW
    report = _capability_diagnostics(resource, None, {}, 0)
    assert report["duration_contract"]["source_unit"] is None
    assert "unknown-private-unit" not in json.dumps(report)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "missing_value"),
        (True, "not_numeric"),
        ("private", "not_numeric"),
        (-1, "out_of_range"),
        (2**54, "out_of_range"),
        (float("nan"), "not_finite"),
        (float("inf"), "not_finite"),
    ],
)
def test_invalid_duration_values_have_no_numeric_state(value, reason):
    resource = runtime("/heatSources/hs2/operationHours", value, "h")
    assessment = assess_runtime(resource)
    assert assessment.supported
    assert assessment.reading(None).value is None
    assert assessment.reading(None).reason == reason


@pytest.mark.parametrize(
    "change",
    [
        lambda r: replace(r, references=(ResourceReference("/private-ref"),)),
        lambda r: replace(r, values=(), has_values=True),
        lambda r: replace(r, value={"total": 2}),
        lambda r: replace(r, value=[2]),
        lambda r: replace(r, metadata=replace(r.metadata, resource_type="stringValue")),
    ],
)
def test_scalar_runtime_rejects_other_shapes(change):
    resource = change(runtime("/heatSources/hs2/operationHours", 2, "h"))
    assert not assess_runtime(resource).supported
    assert not supports_entity(resource)
    assert poll_group(resource) is PollGroup.SLOW


@pytest.mark.parametrize(
    ("values", "expected", "reason"),
    [
        ([], None, "missing_value"),
        ([{"total": 120}], 2, None),
        ([{"total": 120}, {"total": 120}], 2, None),
        ([{"total": 120}, {"total": 60}], None, "conflicting_values"),
        ([{"total": 120}, {"total": "private"}], None, "not_numeric"),
        ([120], None, "invalid_structure"),
    ],
)
def test_multipart_duration_validation_is_per_part(values, expected, reason):
    resource = parse_resource(
        {
            "id": "/heatSources/hs2/workingTime",
            "type": "emonValue",
            "unitOfMeasure": "min",
            "values": values,
        }
    )
    assessment = assess_runtime(resource)
    reading = assessment.reading("total")
    assert reading.value == expected
    assert reading.reason == reason
    assert assessment.reading("ch").value is None
    assert assessment.reading("unknown").reason == "unsupported_part"


def test_duration_errors_and_shape_change_recover_same_entity(hass):
    resource = runtime("/heatSources/hs2/operationHours", 2, "h")
    entity = sensor_for(hass, resource)
    identity = entity.unique_id
    for changed, expected in [
        (
            replace(
                resource, metadata=replace(resource.metadata, numeric_error_values=(2,))
            ),
            None,
        ),
        (
            replace(
                resource,
                metadata=replace(resource.metadata, numeric_error_states_valid=False),
            ),
            None,
        ),
        (replace(resource, metadata=replace(resource.metadata, unit="C")), None),
        (replace(resource, has_value=False), None),
        (
            replace(
                resource, value=120, metadata=replace(resource.metadata, unit="min")
            ),
            2,
        ),
    ]:
        entity.coordinator.data[resource.path] = ResourceSnapshot(
            changed, True, datetime.now(UTC)
        )
        assert entity.native_value == expected
        assert entity.unique_id == identity
        assert poll_group(changed) is PollGroup.SLOW
    entity.coordinator.data[resource.path] = ResourceSnapshot(
        resource, False, datetime.now(UTC)
    )
    assert entity.native_value is None
    assert not entity.available


def test_duration_diagnostics_contain_decisions_without_measurements():
    resource = runtime("/heatSources/hs2/operationHours", 9876543, None)
    report = _capability_diagnostics(resource, None, {}, 0)
    assert report["duration_contract"]["unit_source"] == "operation_hours_contract"
    assert report["duration_contract"]["scale"] == 1
    assert report["duration_contract"]["parts"] == {
        "scalar": {"value_valid": True, "rejection_reason": None}
    }
    assert "9876543" not in json.dumps(report)
    assert assess_runtime(replace(resource, path="/unrelated")).reason == "not_runtime"


def test_monitor_collection_preserves_reference_paths_but_no_scalar_entity():
    fixture = json.loads((FIXTURES / "wddw2/monitor-collection.json").read_text())
    resource = parse_resource(fixture["payload"])
    assert [r.path for r in resource.references] == [
        r["id"] for r in fixture["payload"]["references"]
    ]
    assert not supports_entity(resource)
    assert build_sensor_descriptions({resource.path: resource}) == ()
    report = _capability_diagnostics(resource, None, {}, 0)
    assert (
        report["collection_contract"]["scalar_rejection_reason"]
        == "collection_not_scalar"
    )
    assert poll_group(resource) is PollGroup.STATIC


def test_duration_polling_creates_once_and_recovers_without_extra_requests(hass):
    initial = runtime("/heatSources/hs2/operationHours", 2, "h")
    coordinator = sensor_for(hass, initial).coordinator
    invalid = replace(initial, metadata=replace(initial.metadata, unit="C"))
    coordinator.resources = {initial.path: invalid}
    coordinator.data = {
        initial.path: ResourceSnapshot(invalid, True, datetime.now(UTC))
    }
    coordinator._paths_by_group = {PollGroup.SLOW: (initial.path,)}
    added = []
    _track_resource_sensors(coordinator._config_entry, coordinator, added.extend)
    assert not added
    for resource, expected in (
        (initial, 2),
        (invalid, None),
        (
            replace(initial, value=120, metadata=replace(initial.metadata, unit="min")),
            2,
        ),
    ):
        snapshots = dict(coordinator.data)
        coordinator._apply_results(
            snapshots,
            (
                BatchItemResult(
                    "gateway-fixture", resource.path, 200, resource=resource
                ),
            ),
            attempted_at=datetime.now(UTC),
            now_monotonic=1,
            source=SnapshotSource.BATCH,
        )
        coordinator.async_set_updated_data(snapshots)
        assert len(added) == 1
        assert added[0].native_value == expected
        assert resource.path in coordinator._paths_by_group[PollGroup.SLOW]
    coordinator.data[initial.path] = replace(
        coordinator.data[initial.path], freshness=Freshness.STALE
    )
    assert added[0].native_value is None
    assert not added[0].available
    coordinator.client.get_resource.assert_not_awaited()
    coordinator.client.put_resource_value.assert_not_awaited()


def test_multipart_errors_preserve_valid_siblings_and_reject_mixed_envelopes():
    resource = parse_resource(
        {
            "id": "/heatSources/hs2/workingTime",
            "type": "emonValue",
            "unitOfMeasure": "min",
            "values": [{"total": 120, "ch": -1}],
        }
    )
    assert assess_runtime(resource).reading("total").value == 2
    assert assess_runtime(resource).reading("ch").reason == "out_of_range"
    mixed = replace(resource, value={"total": 120}, has_value=True)
    assert assess_runtime(mixed).reading("total").reason == "mixed_structure"


@pytest.mark.parametrize("child_status", [200, 404])
async def test_monitor_discovery_uses_current_child_responses(hass, child_status):
    from custom_components.bosch_buderus_heating.discovery import (
        async_discover_resources,
    )

    fixture = json.loads((FIXTURES / "wddw2/monitor-collection.json").read_text())
    collection = parse_resource(fixture["payload"])
    child = replace(parse_resource(fixture["payload"]["references"][0]), value=25)
    root = Resource("/dhwCircuits", references=(ResourceReference(collection.path),))
    resources = {root.path: root, collection.path: collection}
    if child_status == 200:
        resources[child.path] = child
    client = AsyncMock()
    client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
        BatchItemResult(gateway, path, 200, resource=resources[path])
        if path in resources
        else BatchItemResult(gateway, path, 404)
        for path in paths
    )
    discovered = await async_discover_resources(
        client, "gateway-fixture", roots=(root.path,)
    )
    requested = [
        p for call in client.get_resources_bulk.await_args_list for p in call.args[1]
    ]
    assert all(ref.path in requested for ref in collection.references)
    assert discovered == resources
    if child_status == 200:
        assert sensor_for(hass, discovered[child.path]).native_value == 25
    else:
        assert build_sensor_descriptions(discovered) == ()
    assert collection.references[1].path not in discovered
    client.get_resource.assert_not_awaited()


def test_inventory_is_complete_and_matches_real_parser_and_catalog():
    inventory = json.loads(
        (FIXTURES.parents[2] / "docs/app-resource-inventory.json").read_text()
    )
    records = inventory["resources"]
    assert len(records) == 361
    assert len({(r["family"], r["model"]) for r in records}) == 361
    assert {r["model_id"] for r in records} == {f"R{i:03}" for i in range(1, 362)}
    assert sum(r["previous_path"] is not None for r in records) == 359
    for record in records:
        contract = record["contract"]
        assert contract["missing_evidence"]
        assert contract["provenance"]["physical_device_verified"] is False
        assert (FIXTURES.parents[2] / contract["family_fixture"]).is_file()
        if probe := record["schema_probe"]:
            assert (
                supports_entity(parse_resource(probe["payload"]))
                == probe["catalog_supported"]
            ), record["model_id"]
        if record["disposition"] == "missing_contract":
            assert record["followup_packages"]
    paths = {r["model_id"]: r["path"] for r in records}
    assert paths["R256"] == "/dhwCircuits/dhw{circuitId}/programs/pg{programId}/week"
    assert len(inventory["reconciliation"]["added_generic_execution_classes"]) == 2
