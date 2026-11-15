"""Counter shapes, answer comparison and bookkeeping of the local selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custom_components.bosch_buderus_heating.coordinator import (
    Freshness,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.local_selection import (
    LocalSelection,
    _like,
    _same_json,
    local_counter_snapshot,
    same_answer,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.test_local_api import read
from tests.test_local_selection import RETURN, TARGET, FakeRuntime

NOW = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
TOTAL = "/heatSources/emon/totalConsumption"
STARTS = "/heatSources/numberOfStarts"


def _cloud(resource: Resource) -> ResourceSnapshot:
    return ResourceSnapshot(resource, True, NOW - timedelta(minutes=2))


def _energy(**parts):
    return _cloud(
        Resource(
            TOTAL,
            values=(dict(parts),),
            has_value=True,
            metadata=ResourceMetadata(resource_type="emonValue"),
        )
    )


def _local_energy(values, *, unit="kWh"):
    return read(
        {
            "id": TOTAL,
            "type": "emonValue",
            "writeable": 0,
            "unit": unit,
            "values": values,
        },
        observed_at=NOW,
        observed_monotonic=10.0,
    )


def test_a_matching_local_counter_takes_the_shape_of_the_cloud_counter() -> None:
    shown = local_counter_snapshot(
        _energy(compressor=1.0, eheater=2.0),
        _local_energy([{"compressor": 3.0}, {"eheater": 4.0}]),
        TARGET,
    )
    assert shown is not None
    assert shown.source is SnapshotSource.LOCAL
    assert shown.freshness is Freshness.FRESH
    assert dict(shown.resource.values[0]) == {"compressor": 3.0, "eheater": 4.0}


def test_other_parts_than_the_cloud_counter_has_are_not_shown() -> None:
    cloud = _energy(compressor=1.0, eheater=2.0)
    for values in (
        [{"compressor": 3.0}],
        [{"compressor": 3.0}, {"eheater": 4.0}, {"extra": 1.0}],
    ):
        assert local_counter_snapshot(cloud, _local_energy(values), TARGET) is None


def test_a_cloud_counter_with_an_unusable_part_is_not_replaced() -> None:
    cloud = _cloud(
        Resource(
            TOTAL,
            values=("not-a-mapping",),
            has_value=True,
            metadata=ResourceMetadata(resource_type="emonValue"),
        )
    )
    local = _local_energy([{"compressor": 3.0}])
    assert local_counter_snapshot(cloud, local, TARGET) is None


def test_a_unit_other_than_the_cloud_unit_is_not_shown() -> None:
    cloud = _cloud(
        Resource(
            TOTAL,
            values=({"compressor": 1.0},),
            has_value=True,
            metadata=ResourceMetadata(resource_type="emonValue", unit="MWh"),
        )
    )
    local = _local_energy([{"compressor": 3.0}], unit="kWh")
    assert local_counter_snapshot(cloud, local, TARGET) is None


def _scalar_counter(value, *, unit=None):
    return read(
        {
            "id": STARTS,
            "type": "floatValue",
            "value": value,
            "writeable": 0,
            "unitOfMeasure": unit or "",
        },
        observed_at=NOW,
        observed_monotonic=10.0,
    )


def _scalar_cloud(value, **kwargs):
    return _cloud(
        Resource(
            STARTS,
            value=value,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue", **kwargs),
        )
    )


def test_a_scalar_counter_keeps_the_type_of_the_cloud_value() -> None:
    shown = local_counter_snapshot(_scalar_cloud(5), _scalar_counter(7.0), TARGET)
    assert shown is not None
    assert shown.resource.value == 7 and type(shown.resource.value) is int


def test_a_scalar_local_counter_does_not_replace_a_shaped_cloud_counter() -> None:
    cloud = _cloud(
        Resource(
            STARTS,
            values=({"a": 1.0},),
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue"),
        )
    )
    assert local_counter_snapshot(cloud, _scalar_counter(7.0), TARGET) is None


@pytest.mark.parametrize(
    "value,template,expected",
    [
        (True, 1, None),
        (None, 1, None),
        ("7", 1, None),
        (float("inf"), 1.0, None),
        (float("nan"), 1.0, None),
        (7.0, 1, 7),
        (7.5, 1, 7.5),
        (Decimal("7.25"), 1.0, 7.25),
        (3, 1.5, 3.0),
    ],
)
def test_a_local_number_follows_the_type_of_the_cloud_field(
    value, template, expected
) -> None:
    result = _like(value, template)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize(
    "before,after,same",
    [
        ({"a": 1}, {"a": 1.0}, False),
        ({"a": 1}, {"a": 1}, True),
        ({"a": 1}, {"b": 1}, False),
        ((1, 2), (1, 2), True),
        ((1, 2), (1,), False),
        ((1, (2, 3)), (1, (2, 4)), False),
        (Decimal("1.0"), Decimal("1.00"), False),
        (Decimal("1.0"), Decimal("1.0"), True),
        ([1], (1,), False),
    ],
)
def test_two_answers_are_the_same_only_with_the_same_type_and_spelling(
    before, after, same
) -> None:
    assert _same_json(before, after) is same


def test_the_same_reading_object_is_the_same_answer() -> None:
    reading = _scalar_counter(7.0)
    assert same_answer(reading, reading)
    assert same_answer(reading, _scalar_counter(7.0))
    assert not same_answer(reading, _scalar_counter(8.0))


def test_a_counter_the_cloud_can_write_is_not_replaced() -> None:
    selection = LocalSelection(FakeRuntime([_scalar_counter(7.0)]), TARGET)
    cloud = _scalar_cloud(5, writable=True)
    assert selection.counter_candidate(STARTS, cloud) is None


def test_a_reading_of_an_ineligible_path_is_not_shown_alone() -> None:
    selection = LocalSelection(FakeRuntime([_scalar_counter(7.0)]), TARGET)
    assert selection.local_only("/not/a/paired/path", _scalar_cloud(5)) is None
    assert selection.local_only(RETURN, _scalar_cloud(5)) is None


def test_tracking_of_a_local_only_reading_ends_with_the_last_user() -> None:
    selection = LocalSelection(FakeRuntime(), TARGET)
    selection.track_local_only(RETURN)
    selection.track_local_only(RETURN)
    selection.untrack_local_only(RETURN)
    assert selection._tracked_local_only[RETURN] == 1
    selection.untrack_local_only(RETURN)
    assert RETURN not in selection._tracked_local_only
