"""Tests for tolerant PointT notification handling."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.faults import (
    FaultEventType,
    FaultSeverity,
    FaultTracker,
    fault_resource_candidates,
    fault_severity_label,
    fault_summary,
    no_active_faults_label,
    parse_fault_resource,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Resource,
    ResourceReference,
)

NOW = datetime(2026, 8, 19, 0, 2, tzinfo=UTC)


def _notifications(*values: object) -> Resource:
    return Resource(path="/notifications", values=values, has_values=True)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [403, 404])
def test_lost_fault_source_cannot_resolve_active_faults(
    hass: HomeAssistant, status: int
) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    active = _notifications({"ccd": "6249", "fc": "12"})
    other = Resource(path="/heatSources/hs1/activefailure", has_values=True)
    tracker.process_resources({active.path: active, other.path: other})
    tracker.record_results((BatchItemResult("gateway", active.path, status),))
    for _ in range(3):
        assert tracker.process_resources({other.path: other}) == ()
    assert len(tracker.active_faults) == 1


@pytest.mark.parametrize(
    "payload", [{}, {"type": "notification"}, {"values": None}, {"value": None}]
)
def test_missing_fault_list_is_not_an_empty_confirmation(
    hass: HomeAssistant, payload: dict
) -> None:
    from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource

    tracker = FaultTracker(hass, "entry", "gateway")
    active = _notifications({"ccd": "6249", "fc": "12"})
    tracker.process_resources({active.path: active})
    malformed = parse_resource(payload, path=active.path)
    for _ in range(3):
        assert tracker.process_resources({active.path: malformed}) == ()
    assert len(tracker.active_faults) == 1
    empty = parse_resource({"values": []}, path=active.path)
    tracker.process_resources({active.path: empty})
    assert (
        tracker.process_resources({active.path: empty})[0].event_type
        is FaultEventType.RESOLVED
    )


@pytest.mark.parametrize("status", [403, 404, 503])
async def test_restart_retains_required_fault_sources(
    hass: HomeAssistant, status: int
) -> None:
    source = FaultTracker(hass, "source", "gateway")
    active = _notifications({"ccd": "6249", "fc": "12"})
    other = Resource(path="/devices/private-device/errors", has_values=True)
    source.process_resources({active.path: active, other.path: other})
    stored = source._serialize()
    assert "private-device" not in str(stored)
    restored = FaultTracker(hass, "restored", "gateway")
    restored._store.async_load = AsyncMock(return_value=stored)
    await restored.async_load()
    restored.record_results((BatchItemResult("gateway", active.path, status),))
    for _ in range(3):
        assert restored.process_resources({other.path: other}) == ()
    assert len(restored.active_faults) == 1
    empty = _notifications()
    restored.process_resources({empty.path: empty, other.path: other})
    assert (
        restored.process_resources({empty.path: empty, other.path: other})[0].event_type
        is FaultEventType.RESOLVED
    )


@pytest.mark.parametrize("failure", ["http", "malformed", "partial"])
def test_failed_notification_cycle_restarts_absence_confirmation(
    hass: HomeAssistant, failure: str
) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    active = _notifications({"ccd": "6249", "fc": "12"})
    empty = _notifications()
    other = Resource(path="/heatSources/hs1/activefailure", has_values=True)
    tracker.process_resources({active.path: active, other.path: other})
    tracker.process_resources({empty.path: empty, other.path: other})
    if failure == "http":
        tracker.record_results((BatchItemResult("gateway", active.path, 503),))
        tracker.process_resources({})
    elif failure == "malformed":
        tracker.process_resources(
            {empty.path: Resource(path=empty.path), other.path: other}
        )
    else:
        tracker.process_resources({other.path: other})
    assert tracker.process_resources({empty.path: empty, other.path: other}) == ()
    assert (
        tracker.process_resources({empty.path: empty, other.path: other})[0].event_type
        is FaultEventType.RESOLVED
    )


def test_unrelated_poll_does_not_break_notification_confirmation(
    hass: HomeAssistant,
) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    active = _notifications({"ccd": "6249", "fc": "12"})
    empty = _notifications()
    tracker.process_resources({active.path: active})
    tracker.process_resources({empty.path: empty})
    tracker.process_resources({})
    assert (
        tracker.process_resources({empty.path: empty})[0].event_type
        is FaultEventType.RESOLVED
    )


@pytest.mark.parametrize("source_keys", [None, [], ["invalid"]])
async def test_legacy_baseline_needs_reobservation_before_resolution(
    hass: HomeAssistant, source_keys
) -> None:
    source = FaultTracker(hass, "source", "gateway")
    active = _notifications({"ccd": "6249", "fc": "12"})
    source.process_resources({active.path: active})
    stored = {
        "active": source._serialize()["active"],
        "required_source_keys": source_keys,
    }
    restored = FaultTracker(hass, "restored", "gateway")
    restored._store.async_load = AsyncMock(return_value=stored)
    await restored.async_load()
    empty = _notifications()
    for _ in range(3):
        assert restored.process_resources({empty.path: empty}) == ()
    # A second restart must not turn the newly collected source evidence into
    # permission to clear a legacy fault that was never reobserved.
    pending = restored._serialize()
    restored._store.async_load = AsyncMock(return_value=pending)
    await restored.async_load()
    assert restored.process_resources({empty.path: empty}) == ()
    restored.process_resources({active.path: active})
    restored.process_resources({empty.path: empty})
    assert (
        restored.process_resources({empty.path: empty})[0].event_type
        is FaultEventType.RESOLVED
    )


def test_real_k40_fault_is_normalized_without_inventing_time() -> None:
    parsed = parse_fault_resource(
        _notifications({"ccd": 6249, "dcd": "", "fc": "12", "orig": "8", "dlv": "4"}),
        observed_at=NOW,
    )

    assert parsed.invalid_entries == 0
    assert len(parsed.faults) == 1
    fault = parsed.faults[0]
    assert fault.code == "6249"
    assert fault.subcode is None
    assert fault.pointt_class == "blocking"
    assert fault.severity is FaultSeverity.FAULT
    assert fault.occurred_at is None
    assert fault.first_seen_at == NOW
    assert fault_summary(fault, "de-DE") == (
        "Kommunikation zwischen Innen- und Außeneinheit gestört"
    )


def test_named_classes_and_unknown_fields_are_tolerated() -> None:
    parsed = parse_fault_resource(
        _notifications(
            {"ccd": "W1", "fc": "WARNING", "future": {"ignored": True}},
            {"ccd": "F1", "fc": "FATAL"},
            {"ccd": "M1", "fc": "MAINTENANCE"},
            {"ccd": "I1", "fc": "new-vendor-class"},
            {"ccd": "F2", "fc": "generic-error"},
            {"ccd": "F3", "fc": 12.0},
        ),
        observed_at=NOW,
    )

    assert [item.severity for item in parsed.faults] == [
        FaultSeverity.WARNING,
        FaultSeverity.CRITICAL,
        FaultSeverity.MAINTENANCE,
        FaultSeverity.UNKNOWN,
        FaultSeverity.FAULT,
        FaultSeverity.FAULT,
    ]


def test_explicitly_inactive_entries_are_not_reported_as_current_faults() -> None:
    parsed = parse_fault_resource(
        _notifications(
            {"ccd": "A", "fc": "FAULT", "active": False},
            {"ccd": "B", "fc": "FAULT", "isActive": "inactive"},
            {"ccd": "C", "fc": "FAULT", "resolved": True},
            {"ccd": "D", "fc": "FAULT", "status": "cleared"},
            {"ccd": "E", "fc": "FAULT", "active": True},
        ),
        observed_at=NOW,
    )

    assert parsed.invalid_entries == 0
    assert [item.code for item in parsed.faults] == ["E"]


def test_malformed_entries_are_isolated_and_timestamps_are_validated() -> None:
    parsed = parse_fault_resource(
        _notifications(
            "not-an-object",
            {"unrelated": True},
            {"ccd": 1000, "dcd": "A11", "t": "2060-08-01T10:52:00Z"},
            {"ccd": 1038, "t": "2026-08-18T23:59:00Z"},
        ),
        observed_at=NOW,
    )

    assert parsed.invalid_entries == 2
    assert parsed.faults[0].occurred_at is None
    assert parsed.faults[1].occurred_at == datetime(2026, 8, 18, 23, 59, tzinfo=UTC)


def test_fault_candidates_use_only_discovered_component_ids() -> None:
    resources = {
        "/heatSources": Resource(
            path="/heatSources",
            references=(
                ResourceReference("/heatSources/hs2"),
                ResourceReference("/heatSources/actualModulation"),
                ResourceReference("/heatSources/.."),
            ),
        ),
        "/heatSources/hs2": Resource(path="/heatSources/hs2"),
        "/devices/list": Resource(
            path="/devices/list",
            values=({"deviceId": "device7"}, {"deviceId": "../unsafe"}),
        ),
    }

    assert fault_resource_candidates(resources) == (
        "/devices/device7/errors",
        "/heatSources/hs2/activefailure",
        "/heatSources/hs2/failurelist",
        "/notifications",
    )
    assert not any(
        "actualModulation" in path for path in fault_resource_candidates(resources)
    )


def test_tracker_requires_two_complete_empty_reads_before_resolution(
    hass: HomeAssistant,
) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    active = _notifications({"ccd": 6249, "fc": "12"})
    empty = _notifications()

    assert tracker.process_resources({active.path: active}, observed_at=NOW) == ()
    assert len(tracker.active_faults) == 1

    events = []
    tracker.async_add_listener(events.append)
    assert (
        tracker.process_resources(
            {empty.path: empty}, observed_at=NOW.replace(minute=3)
        )
        == ()
    )
    assert len(tracker.active_faults) == 1

    resolved = tracker.process_resources(
        {empty.path: empty}, observed_at=NOW.replace(minute=4)
    )
    assert [item.event_type for item in resolved] == [FaultEventType.RESOLVED]
    assert events == list(resolved)
    assert tracker.active_faults == ()


def test_tracker_deduplicates_and_does_not_resolve_on_partial_parse(
    hass: HomeAssistant,
) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    duplicate = _notifications(
        {"ccd": 6249, "fc": "12"},
        {"ccd": 6249, "fc": "12"},
    )
    tracker.process_resources({duplicate.path: duplicate}, observed_at=NOW)
    assert len(tracker.active) == 1

    partial = _notifications("malformed")
    tracker.process_resources({partial.path: partial}, observed_at=NOW)
    tracker.process_resources({partial.path: partial}, observed_at=NOW)

    assert len(tracker.active) == 1
    assert tracker.diagnostics()["parser_errors"] == 2


def test_occurrence_id_deduplicates_the_same_fault_across_resources(
    hass: HomeAssistant,
) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    system = _notifications({"ccd": 6249, "fc": "12", "occurrenceId": "incident-1"})
    component = Resource(
        path="/heatSources/hs1/activefailure",
        value={"ccd": 6249, "fc": "12", "occurrenceId": "incident-1"},
        has_value=True,
    )

    tracker.process_resources(
        {system.path: system, component.path: component}, observed_at=NOW
    )

    assert len(tracker.active) == 1
    assert tracker.active[0].component_type == "heat_source"
    assert tracker.active[0].component_id == "hs1"
    assert tracker.active[0].source_resources == (
        "/heatSources/hs1/activefailure",
        "/notifications",
    )


async def test_restored_empty_baseline_emits_a_new_fault_after_restart(
    hass: HomeAssistant,
) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    tracker._store.async_load = AsyncMock(return_value={"active": []})
    await tracker.async_load()

    active = _notifications({"ccd": 6249, "fc": "12"})
    appeared = tracker.process_resources({active.path: active}, observed_at=NOW)
    assert [item.event_type for item in appeared] == [FaultEventType.APPEARED]

    received = []
    tracker.async_add_listener(received.append)
    assert received == list(appeared)


def test_capability_failures_do_not_turn_existing_faults_off(
    hass: HomeAssistant,
) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    active = _notifications({"ccd": 6249, "fc": "12"})
    tracker.process_resources({active.path: active}, observed_at=NOW)
    tracker.record_results(
        (
            BatchItemResult(
                gateway_id="gateway",
                path="/notifications",
                status=500,
            ),
        )
    )
    tracker.process_resources({}, successful_paths=set(), observed_at=NOW)

    assert len(tracker.active_faults) == 1
    assert tracker.diagnostics()["resource_results"] == {"/notifications": "500"}


def test_all_supported_wire_shapes_and_component_paths_are_bounded() -> None:
    active_failure = Resource(
        path="/heatSources/hs7/activefailure",
        value={"ccd": "HS", "fc": "LOCKING"},
        has_value=True,
    )
    device_errors = Resource(
        path="/devices/device2/errors",
        value=[{"value": "D1", "errorType": "GENERIC_ERROR"}],
        has_value=True,
    )

    heat_source_fault = parse_fault_resource(active_failure, observed_at=NOW).faults[0]
    device_fault = parse_fault_resource(device_errors, observed_at=NOW).faults[0]

    assert (heat_source_fault.component_type, heat_source_fault.component_id) == (
        "heat_source",
        "hs7",
    )
    assert (device_fault.component_type, device_fault.component_id) == (
        "device",
        "device2",
    )
    assert parse_fault_resource(Resource(path="/system/brand")).faults == ()
    assert (
        parse_fault_resource(Resource(path="/heatSources/hs7/failurelist")).faults == ()
    )
    assert parse_fault_resource(Resource(path="/notifications")).faults == ()
    scalar = Resource(path="/heatSources/hs7/activefailure", value=6249, has_value=True)
    assert parse_fault_resource(scalar, observed_at=NOW).faults[0].code == "6249"


def test_unknown_summaries_and_numeric_timestamps() -> None:
    seconds = int(datetime(2026, 8, 18, 23, 58, tzinfo=UTC).timestamp())
    parsed = parse_fault_resource(
        _notifications(
            {"ccd": "X1", "t": seconds},
            {"ccd": "X2", "t": seconds * 1000},
            {"ccd": {}, "fc": "INFO"},
        ),
        observed_at=NOW,
    )

    assert parsed.faults[0].occurred_at == parsed.faults[1].occurred_at
    assert fault_summary(parsed.faults[0], "de") == "Unbekannte Störung (Code X1)"
    assert fault_summary(parsed.faults[0], "en") == "Unknown fault (code X1)"
    no_code = parsed.faults[2]
    assert fault_summary(no_code, "de") == "Unbekannte Störung"
    assert fault_summary(no_code, None) == "Unknown fault"
    assert fault_severity_label(FaultSeverity.WARNING, "de") == "Warnung"
    assert fault_severity_label(FaultSeverity.CRITICAL, "en") == "Critical fault"
    assert no_active_faults_label("de") == "Keine aktiven Störungen"
    assert no_active_faults_label("en") == "No active faults"

    known = parse_fault_resource(
        _notifications({"ccd": 1000, "dcd": "a11", "fc": "12"}),
        observed_at=NOW,
    ).faults[0]
    assert fault_summary(known, "en") == "System configuration not confirmed"


def test_candidate_discovery_accepts_direct_device_paths_and_skips_noise() -> None:
    resources = {
        "/devices/device9": Resource(path="/devices/device9"),
        "/devices/..": Resource(path="/devices/.."),
        "/devices/list": Resource(
            path="/devices/list",
            values=(
                "ignored",
                {"id": "device8"},
                {"device": "device10"},
                {"deviceId": ".."},
            ),
        ),
        "/heatSources/emon": Resource(path="/heatSources/emon"),
    }

    candidates = fault_resource_candidates(resources)

    assert "/devices/device8/errors" in candidates
    assert "/devices/device9/errors" in candidates
    assert "/devices/device10/errors" in candidates
    assert not any("emon" in item for item in candidates)

    value_list = {
        "/devices/list": Resource(
            path="/devices/list",
            value=[{"deviceId": "device11"}],
            has_value=True,
        )
    }
    assert "/devices/device11/errors" in fault_resource_candidates(value_list)


async def test_tracker_restores_a_valid_fault_and_ignores_invalid_storage(
    hass: HomeAssistant,
) -> None:
    source = FaultTracker(hass, "source", "gateway")
    active = _notifications(
        {
            "ccd": 1038,
            "dcd": "A11",
            "fc": "BLOCKING",
            "t": "2026-08-18T23:59:00Z",
        }
    )
    source.process_resources({active.path: active}, observed_at=NOW)
    stored = source._serialize()
    serialized = stored["active"][0]

    assert "component_id" not in serialized
    assert "occurrence_id" not in serialized
    assert "origin_raw" not in serialized
    assert "source_resources" not in serialized

    restored = FaultTracker(hass, "restored", "gateway")
    restored._store.async_load = AsyncMock(
        return_value={"active": [None, {"severity": "bad"}, *stored["active"]]}
    )
    await restored.async_load()

    assert len(restored.active) == 1
    assert restored.active[0].subcode == "A11"
    assert restored.active[0].occurred_at == datetime(2026, 8, 18, 23, 59, tzinfo=UTC)
    assert restored.active[0].component_id is None
    assert restored.active[0].source_resources == ()
    invalid = FaultTracker(hass, "invalid", "gateway")
    invalid._store.async_load = AsyncMock(return_value={"active": "wrong"})
    await invalid.async_load()
    assert invalid.active == ()


def test_listener_removal_and_capability_result_categories(
    hass: HomeAssistant,
) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    received = []
    remove = tracker.async_add_listener(received.append)
    remove()
    success = _notifications()
    tracker.record_results(
        (
            BatchItemResult("gateway", "/system/brand", 200),
            BatchItemResult("gateway", "/notifications", 200, resource=success),
            BatchItemResult("gateway", "/notifications", 403),
            BatchItemResult("gateway", "/notifications", None),
        )
    )

    assert tracker.has_supported_source
    assert tracker.highest_severity is None
    assert tracker.diagnostics()["resource_results"] == {"/notifications": "error"}


def test_listener_failure_cannot_block_fault_tracking(hass: HomeAssistant) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    empty = _notifications()
    active = _notifications({"ccd": 6249, "fc": "12"})
    tracker.process_resources({empty.path: empty}, observed_at=NOW)
    received = []

    def broken_listener(event: object) -> None:
        raise RuntimeError("test listener failure")

    tracker.async_add_listener(broken_listener)
    tracker.async_add_listener(received.append)

    appeared = tracker.process_resources({active.path: active}, observed_at=NOW)

    assert received == list(appeared)
    assert len(tracker.active_faults) == 1


def test_existing_fault_is_refreshed_without_duplicate_event(
    hass: HomeAssistant,
) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    active = _notifications({"ccd": 6249, "fc": "12"})
    tracker.process_resources({active.path: active}, observed_at=NOW)

    assert (
        tracker.process_resources(
            {active.path: active}, observed_at=NOW.replace(minute=3)
        )
        == ()
    )
    assert tracker.active[0].first_seen_at == NOW
    assert tracker.active[0].last_seen_at == NOW.replace(minute=3)
