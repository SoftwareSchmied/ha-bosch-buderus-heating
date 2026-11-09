"""Common observation format and the verified cloud/local pair catalog."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custom_components.bosch_buderus_heating.coordinator import (
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.observations import (
    PAIR_CATALOG,
    PAIR_CATALOG_VERSION,
    Observation,
    PairResult,
    SourceKind,
    ValueStatus,
    compare,
    from_cloud_snapshot,
    from_local_observation,
    pair_for_path,
    pair_observations,
    pair_summary,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.test_local_api import read, scalar

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
TARGET = "target-a"


def cloud(
    path,
    value=None,
    *,
    values=(),
    has_value=True,
    resource_type="floatValue",
    unit="C",
    errors=(),
    available=True,
    freshness=Freshness.FRESH,
    at=NOW,
    writable=True,
):
    resource = Resource(
        path,
        value=value,
        has_value=has_value,
        values=tuple(values),
        has_values=bool(values),
        metadata=ResourceMetadata(
            resource_type=resource_type,
            unit=unit,
            numeric_error_values=tuple(errors),
            writable=writable,
        ),
    )
    snapshot = ResourceSnapshot(
        resource,
        available,
        at,
        last_attempt=at - timedelta(seconds=1),
        freshness=freshness,
    )
    return from_cloud_snapshot(snapshot, connection_id="cloud-1", target_id=TARGET)


def local(raw, *, current=True, at=NOW):
    return from_local_observation(
        read(raw, observed_at=at),
        connection_id="local-1",
        target_id=TARGET,
        current=current,
    )


# ----------------------------------------------------------------- catalog


def test_catalog_contains_only_documented_identical_paths() -> None:
    assert PAIR_CATALOG_VERSION == 1
    measurements = [pair.measurement for pair in PAIR_CATALOG]
    templates = [pair.template for pair in PAIR_CATALOG]
    assert len(set(measurements)) == len(measurements)
    assert len(set(templates)) == len(templates)
    for template in templates:
        path = (
            template.replace("{hc}", "hc1")
            .replace("{dhw}", "dhw1")
            .replace("{hs}", "hs1")
        )
        # Every pair is an exact path in the pinned official local API.
        assert request_for_path(path).path == path
        assert pair_for_path(path).template == template


@pytest.mark.parametrize(
    "path",
    [
        "/gateway/eth/ip/ipv4",
        "/gateway/eth/mac",
        "/gateway/wifi/ssid",
        "/devices/inclusionWhitelist",
        "/devices/uhc/assignedTo",
        "/notifications",
        "/zones/configuration",
        "/heatingCircuits/hc1/manualRoomSetpoint",
        "/heatSources/currentSupplySetpoint",
        "/heatSources/hs1/pumpVolumeFlow",
    ],
)
def test_private_configuration_and_single_source_paths_are_not_paired(path) -> None:
    assert pair_for_path(path) is None


def test_templates_follow_reported_circuits_without_renumbering() -> None:
    assert pair_for_path("/heatingCircuits/hc4/currentRoomSetpoint").measurement == (
        "room_setpoint_current"
    )
    assert pair_for_path("/heatingCircuits/hc1x/currentRoomSetpoint") is None
    assert pair_for_path("/heatingCircuits/hc1/currentRoomSetpoint/extra") is None
    counters = {pair.measurement for pair in PAIR_CATALOG if pair.counter}
    assert counters == {
        "energy_heating",
        "energy_cooling",
        "energy_hot_water",
        "energy_total",
        "starts_system",
        "starts_heat_source",
        "working_time_system",
        "working_time_heat_source",
    }


# ---------------------------------------------------------------- adapters


def test_observation_has_no_write_semantics() -> None:
    names = {item.name for item in dataclasses.fields(Observation)}
    assert not any("writ" in name or "control" in name for name in names)
    observation = cloud("/heatingCircuits/hc1/maxFlowTemp", 55.0, writable=True)
    assert observation.status is ValueStatus.VALID
    local_observation = local(scalar("/heatingCircuits/hc1/maxFlowTemp", writeable=1))
    assert not hasattr(local_observation, "writeable")
    assert "55" not in repr(observation)


@pytest.mark.parametrize(
    ("value", "expected"), [(21.5, Decimal("21.5")), (20.0, 20), (3, 3), ("on", "on")]
)
def test_cloud_values_are_normalized_exactly(value, expected) -> None:
    observation = cloud("/system/sensors/temperatures/outdoor_t1", value)
    assert observation.value == expected
    assert observation.source is SourceKind.CLOUD
    assert observation.group_kind is None
    assert observation.timing.requested_at == NOW - timedelta(seconds=1)


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"value": None, "has_value": False}, ValueStatus.MISSING),
        ({"value": ""}, ValueStatus.EMPTY),
        ({"value": None}, ValueStatus.EMPTY),
        ({"value": float("nan")}, ValueStatus.EMPTY),
        ({"value": -3276.8, "errors": (-3276.8,)}, ValueStatus.DEVICE_ERROR),
        ({"value": None, "values": ["not-a-mapping"]}, ValueStatus.UNSUPPORTED),
        (
            {"value": None, "values": [{"total": 1}, {"total": 2}]},
            ValueStatus.UNSUPPORTED,
        ),
    ],
)
def test_missing_empty_and_error_values_never_become_zero(kwargs, status) -> None:
    observation = cloud("/heatSources/actualSupplyTemperature", **kwargs)
    assert observation.status is status
    assert observation.value is None
    assert observation.parts == ()


def test_cloud_counters_keep_named_parts_and_group() -> None:
    observation = cloud(
        "/heatSources/hs1/numberOfStarts",
        values=[{"total": 42, "ch": 30.0}],
        resource_type="arrayData",
        unit=None,
    )
    assert observation.parts == (("total", 42), ("ch", 30))
    assert (observation.group_kind, observation.group_id) == ("heat_source", "hs1")


def test_stale_or_unavailable_cloud_values_are_not_current() -> None:
    assert cloud("/heatSources/returnTemperature", 30.0).current is True
    assert (
        cloud("/heatSources/returnTemperature", 30.0, freshness=Freshness.STALE).current
        is False
    )
    assert (
        cloud("/heatSources/returnTemperature", 30.0, available=False).current is False
    )


def test_local_scalars_named_values_and_markers() -> None:
    temperature = local(scalar("/heatingCircuits/hc2/currentRoomSetpoint", value=21.25))
    assert temperature.status is ValueStatus.VALID
    assert temperature.value == Decimal("21.25")
    assert (temperature.group_kind, temperature.group_id) == ("heating_circuit", "hc2")
    assert temperature.timing.received_monotonic == 4.25

    energy = local(
        {
            "id": "/heatSources/emon/totalConsumption",
            "type": "emonValue",
            "writeable": 0,
            "unit": "kWh",
            "values": [{"total": 100}, {"compressor": 80}],
        }
    )
    assert energy.parts == (("total", 100), ("compressor", 80))
    assert energy.unit == "kWh"

    marker = local(
        scalar(
            "/system/sensors/temperatures/outdoor_t1",
            value=-32768,
            state=[{"open": -32768, "short": 32767}],
        )
    )
    assert marker.status is ValueStatus.STATE_MARKER
    assert marker.value is None

    unknown = local(scalar("/heatSources/returnTemperature", unitOfMeasure="parsec"))
    assert unknown.status is ValueStatus.UNKNOWN_UNIT
    # C7: the number is kept for a generic entity without the unit.
    assert unknown.value == Decimal("21.25")


def test_unsupported_local_shapes_are_marked() -> None:
    from custom_components.bosch_buderus_heating.local_api.models import (
        DeviceFaultList,
    )

    raw = {
        "id": "/heatSources/actualHeatDemand",
        "type": "arrayData",
        "writeable": 0,
        "values": ["heating"],
    }
    # B2: a list of texts is a reading of its own shape.
    observation = local(raw)
    assert observation.status is ValueStatus.VALID
    assert observation.items == ("heating",) and observation.value is None
    other = from_local_observation(
        dataclasses.replace(read(raw, observed_at=NOW), data=DeviceFaultList(())),
        connection_id="local-1",
        target_id=TARGET,
        current=True,
    )
    assert other.status is ValueStatus.UNSUPPORTED


# -------------------------------------------------------------- comparison


def test_equal_scalars_agree_and_keep_both_observations() -> None:
    left = cloud("/heatSources/returnTemperature", 31.5)
    right = local(
        scalar("/heatSources/returnTemperature", value=31.5),
        at=NOW + timedelta(seconds=2),
    )
    comparison = compare(left, right)
    assert comparison.result is PairResult.AGREE
    assert comparison.cloud is left and comparison.local is right
    assert comparison.receive_gap_seconds == 2.0
    assert comparison.pair.measurement == "return_temperature"


def test_different_values_are_reported_not_averaged() -> None:
    comparison = compare(
        cloud("/heatSources/returnTemperature", 31.5),
        local(scalar("/heatSources/returnTemperature", value=29.0)),
    )
    assert comparison.result is PairResult.DIFFER
    assert comparison.cloud.value == Decimal("31.5")
    assert comparison.local.value == 29


@pytest.mark.parametrize(
    ("left", "right", "reason"),
    [
        ({"value": 31.5, "unit": "K"}, {"value": 31.5}, "unit_differs"),
        (
            {"value": 31.5, "resource_type": "stringValue"},
            {"value": 31.5},
            "type_differs",
        ),
        ({"value": None, "has_value": False}, {"value": 31.5}, "not_valid"),
    ],
)
def test_mismatched_type_unit_or_invalid_values_are_incomparable(
    left, right, reason
) -> None:
    comparison = compare(
        cloud("/heatSources/returnTemperature", **left),
        local(scalar("/heatSources/returnTemperature", **right)),
    )
    assert comparison.result is PairResult.INCOMPARABLE
    assert comparison.reason == reason


def test_counters_compare_named_parts_independent_of_order() -> None:
    raw = {
        "id": "/heatSources/emon/totalConsumption",
        "type": "emonValue",
        "writeable": 0,
        "unit": "kWh",
        "values": [{"compressor": 80}, {"total": 100}],
    }
    left = cloud(
        "/heatSources/emon/totalConsumption",
        values=[{"total": 100.0, "compressor": 80}],
        resource_type="emonValue",
        unit="kWh",
    )
    assert compare(left, local(raw)).result is PairResult.AGREE
    raw["values"][0] = {"compressor": 81}
    assert compare(left, local(raw)).result is PairResult.DIFFER
    scalar_left = cloud(
        "/heatSources/emon/totalConsumption",
        100.0,
        resource_type="emonValue",
        unit="kWh",
    )
    assert compare(scalar_left, local(raw)).reason == "shape_differs"


def test_unpaired_or_mismatched_observations_are_not_compared() -> None:
    left = cloud("/heatSources/returnTemperature", 31.5)
    other = local(scalar("/heatSources/actualSupplyTemperature", value=31.5))
    assert compare(left, other) is None
    assert (
        compare(
            cloud("/heatSources/currentSupplySetpoint", 40.0),
            local(scalar("/heatSources/currentSupplySetpoint", value=40.0)),
        )
        is None
    )
    foreign = dataclasses.replace(
        local(scalar("/heatSources/returnTemperature", value=31.5)),
        target_id="target-b",
    )
    assert compare(left, foreign) is None
    assert (
        compare(local(scalar("/heatSources/returnTemperature", value=31.5)), left)
        is None
    )


def test_pairing_and_summary_contain_no_values_or_paths() -> None:
    cloud_items = [
        cloud("/heatSources/returnTemperature", 31.5),
        cloud("/heatSources/actualSupplyTemperature", 40.0),
        cloud("/heatingCircuits/hc1/manualRoomSetpoint", 22.0),
        cloud(
            "/heatSources/hs1/numberOfStarts",
            values=[{"total": 4}],
            resource_type="arrayData",
            unit=None,
        ),
    ]
    local_items = [
        local(scalar("/heatSources/returnTemperature", value=31.5)),
        local(
            scalar("/heatSources/actualSupplyTemperature", value=41.0), current=False
        ),
        local(
            scalar(
                "/heatSources/hs1/pumpVolumeFlow",
                type="integerValue",
                value=900,
                unitOfMeasure="l/h",
            )
        ),
    ]
    comparisons = pair_observations(cloud_items, local_items)
    assert [item.pair.measurement for item in comparisons] == [
        "supply_temperature",
        "return_temperature",
    ]
    summary = pair_summary(comparisons)
    assert summary == {
        "catalog_version": 1,
        "catalog_pairs": len(PAIR_CATALOG),
        "observed_pairs": 2,
        "results": {"agree": 1, "differ": 1},
        "incomparable_reasons": {},
        "incomparable_units": {},
        "differing_measurements": ["supply_temperature"],
        "counter_pairs": 0,
        "counter_evidence": {},
        "both_current": 1,
        "maximum_receive_gap_seconds": 0.0,
        "maximum_receive_gap_seconds_by_class": {"live": 0.0},
    }
    rendered = repr(summary)
    assert "31.5" not in rendered and "/heatSources" not in rendered
    assert pair_summary(())["maximum_receive_gap_seconds"] is None


# ------------------------------------------- counters between reads (O90, O91)

WORKING_TIME = "/heatSources/hs1/workingTime"


def working_time(total, *, unit="s"):
    raw = {
        "id": WORKING_TIME,
        "type": "emonValue",
        "writeable": 0,
        "values": [{"ch": total - 100}, {"dhw": 100}, {"cooling": 0}, {"total": total}],
    }
    if unit is not None:
        raw["unit"] = unit
    return raw


def cloud_working_time(total, *, at=NOW):
    return cloud(
        WORKING_TIME,
        values=[{"ch": total - 100}, {"dhw": 100}, {"cooling": 0}, {"total": total}],
        resource_type="emonValue",
        unit=None,
        at=at,
        writable=False,
    )


def test_the_unitless_cloud_working_time_counts_seconds():
    comparison = compare(cloud_working_time(1434959), local(working_time(1434959)))
    assert comparison.result is PairResult.AGREE
    # Only seconds, and only this pair.
    other = compare(
        cloud_working_time(1434959), local(working_time(1434959, unit="min"))
    )
    assert other.result is PairResult.INCOMPARABLE and other.reason == "unit_differs"
    starts = compare(
        cloud(
            "/heatSources/hs1/numberOfStarts",
            values=[{"total": 5}],
            resource_type="emonValue",
            unit=None,
            writable=False,
        ),
        local(
            {
                "id": "/heatSources/hs1/numberOfStarts",
                "type": "emonValue",
                "writeable": 0,
                "unit": "s",
                "values": [{"total": 5}],
            }
        ),
    )
    assert starts.reason == "unit_differs"


@pytest.mark.parametrize(
    "cloud_total,local_total,gap,result",
    [
        (1434959, 1435379, 457, PairResult.CHANGED_BETWEEN_READS),  # live pair
        (1434959, 1435379, 60, PairResult.DIFFER),  # within one poll period
        (1435379, 1434959, 457, PairResult.DIFFER),  # the later one is lower
        (1434959, 1434959, 457, PairResult.AGREE),
    ],
)
def test_a_counter_read_later_and_higher_changed_between_reads(
    cloud_total, local_total, gap, result
):
    later = NOW + timedelta(seconds=gap)
    comparison = compare(
        cloud_working_time(cloud_total), local(working_time(local_total), at=later)
    )
    assert comparison.result is result
    # The cloud read later and higher counts the same way.
    swapped = compare(
        cloud_working_time(local_total, at=later), local(working_time(cloud_total))
    )
    assert swapped.result is result


@pytest.mark.parametrize(
    ("gap", "result"),
    [
        (600, PairResult.CHANGED_BETWEEN_READS),
        (61, PairResult.CHANGED_BETWEEN_READS),
        (60, PairResult.DIFFER),
        (0, PairResult.DIFFER),
    ],
)
def test_a_live_value_that_differs_after_a_poll_period_changed_between_reads(
    gap, result
):
    # A1: not only counters; a lower later value counts the same way.
    later = NOW + timedelta(seconds=gap)
    for first, second in ((30.0, 31.0), (31.0, 30.0)):
        comparison = compare(
            cloud("/heatSources/returnTemperature", first, writable=False),
            local(scalar("/heatSources/returnTemperature", value=second), at=later),
        )
        assert comparison.result is result
        assert comparison.receive_gap_seconds == gap


def test_the_summary_names_differing_pairs_and_gaps_per_class():
    later = NOW + timedelta(seconds=457)
    close = NOW + timedelta(seconds=30)
    comparisons = pair_observations(
        [
            cloud_working_time(1434959),
            cloud("/heatSources/returnTemperature", 30.0, writable=False),
            cloud("/gateway/brand", "B", resource_type="stringValue", unit=None),
        ],
        [
            local(working_time(1435379), at=later),
            # A1: read within one poll period, so a real difference.
            local(scalar("/heatSources/returnTemperature", value=31.0), at=close),
            local(
                {
                    "id": "/gateway/brand",
                    "type": "stringValue",
                    "writeable": 0,
                    "value": "B",
                },
                at=NOW + timedelta(hours=9),
            ),
        ],
    )
    summary = pair_summary(comparisons)
    assert summary["results"] == {
        "agree": 1,
        "changed_between_reads": 1,
        "differ": 1,
    }
    assert summary["differing_measurements"] == ["return_temperature"]
    assert summary["counter_evidence"]["working_time_heat_source"]["result"] == (
        "changed_between_reads"
    )
    assert summary["maximum_receive_gap_seconds"] == 32400.0
    assert summary["maximum_receive_gap_seconds_by_class"] == {
        "counters": 457.0,
        "live": 30.0,
        "static": 32400.0,
    }
    rendered = repr(summary)
    assert "1435379" not in rendered and "/heatSources" not in rendered
