"""B2: paired lists of texts (``arrayData``) from the local access.

Heat demand and the variable tariff support status are lists on both
interfaces. A current local list replaces the cloud's list when it has the
same type and only entries the cloud declares; otherwise the cloud stays,
with a precise reason.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.coordinator import (
    Freshness,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.local_api.models import DeviceFaultList
from custom_components.bosch_buderus_heating.local_selection import (
    LocalSelection,
    _local_value,
    local_snapshot,
)
from custom_components.bosch_buderus_heating.observations import (
    PairResult,
    ValueStatus,
    compare,
    from_cloud_snapshot,
    from_local_observation,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.test_local_api import read
from tests.test_local_selection import TARGET, FakeRuntime
from tests.test_local_selection import local as scalar_local

NOW = datetime(2026, 11, 8, 8, 0, tzinfo=UTC)
DEMAND = "/heatSources/actualHeatDemand"
SUPPORT = "/system/variableTariff/supportStatus"
DEMAND_ALLOWED = ("", "ch", "dhw", "frost")
SUPPORT_ALLOWED = ("not_supported", "vta_gateway", "in_evaluation", "blocked")
LOCAL_DEMAND_ALLOWED = (
    "",
    "ch",
    "dhw",
    "frost",
    "ch_heat",
    "ch_cool",
    "dhw_exclusive",
    "dhw_nonexclusive",
    "pool",
)


def cloud(path, values, allowed, *, available=True):
    resource = Resource(
        path,
        values=tuple(values),
        has_values=True,
        metadata=ResourceMetadata(
            resource_type="arrayData", allowed_values=tuple(allowed)
        ),
    )
    return ResourceSnapshot(
        resource,
        available,
        NOW - timedelta(minutes=2),
        freshness=Freshness.FRESH if available else Freshness.STALE,
    )


def local(path, values, allowed, *, monotonic=10.0, kind="arrayData"):
    raw = {"id": path, "type": kind, "writeable": 0, "values": list(values)}
    if allowed is not None:
        raw["allowedValues"] = list(allowed)
    return read(raw, observed_at=NOW, observed_monotonic=monotonic)


def demand(values, allowed=LOCAL_DEMAND_ALLOWED, **kwargs):
    return local(DEMAND, values, allowed, **kwargs)


def test_lists_are_observed_and_compared_by_their_entries() -> None:
    cloud_item = from_cloud_snapshot(
        cloud(DEMAND, ("ch", "dhw"), DEMAND_ALLOWED),
        connection_id="cloud",
        target_id=TARGET,
    )
    local_item = from_local_observation(
        demand(("dhw", "ch")), connection_id="local", target_id=TARGET, current=True
    )
    assert cloud_item.status is local_item.status is ValueStatus.VALID
    assert cloud_item.items == ("ch", "dhw") and local_item.items == ("dhw", "ch")
    # The heat demand state does not depend on the order.
    assert compare(cloud_item, local_item).result is PairResult.AGREE
    other = from_local_observation(
        demand(("ch",)), connection_id="local", target_id=TARGET, current=True
    )
    assert compare(cloud_item, other).result is PairResult.DIFFER
    # The support status uses the first entry: there the order counts.
    support_cloud = from_cloud_snapshot(
        cloud(SUPPORT, ("vta_gateway", "blocked"), SUPPORT_ALLOWED),
        connection_id="cloud",
        target_id=TARGET,
    )
    support_local = from_local_observation(
        local(SUPPORT, ("blocked", "vta_gateway"), SUPPORT_ALLOWED),
        connection_id="local",
        target_id=TARGET,
        current=True,
    )
    assert compare(support_cloud, support_local).result is PairResult.DIFFER
    empty = from_local_observation(
        demand(()), connection_id="local", target_id=TARGET, current=True
    )
    assert empty.status is ValueStatus.VALID and empty.items == ()


def test_a_local_list_replaces_the_cloud_list() -> None:
    snapshot = cloud(DEMAND, ("ch",), DEMAND_ALLOWED, available=False)
    selection = LocalSelection(FakeRuntime([demand(("dhw", "ch"))]), TARGET)
    shown = selection.select(DEMAND, snapshot)
    assert shown.resource.values == ("dhw", "ch")
    assert shown.resource.has_values and not shown.resource.has_value
    assert shown.resource.metadata == snapshot.resource.metadata
    assert (shown.available, shown.freshness, shown.source) == (
        True,
        Freshness.FRESH,
        SnapshotSource.LOCAL,
    )
    # Empty like the cloud's empty list: nothing active.
    empty = local_snapshot(snapshot, demand(()), TARGET)
    assert empty is not None and empty.resource.values == ()


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        # Declared locally, but not by the cloud: the state would be unknown.
        (demand(("ch_heat",)), "unknown_entry"),
        # Neither declares its entries: nothing to check them against.
        (local(SUPPORT, ("vta_gateway",), None), "unknown_entry"),
        # A shape no list can take.
        (
            replace(demand(("ch",)), data=DeviceFaultList(())),
            "shape_differs",
        ),
    ],
)
def test_a_list_that_does_not_fit_keeps_the_cloud(observation, reason) -> None:
    path = observation.request.path
    allowed = DEMAND_ALLOWED if path == DEMAND else ()
    snapshot = cloud(path, ("ch",) if path == DEMAND else (), allowed)
    assert _local_value(snapshot, observation, TARGET) == (None, reason)
    assert local_snapshot(snapshot, observation, TARGET) is None
    selection = LocalSelection(FakeRuntime([observation]), TARGET)
    assert selection.select(path, snapshot) is snapshot
    summary = selection.summary({path: snapshot})
    assert summary["cloud_reasons"] == {reason: 1}
    assert "local_invalid" not in summary["cloud_reasons"]


def test_without_cloud_declarations_the_local_ones_decide() -> None:
    snapshot = cloud(SUPPORT, ("not_supported",), ())
    fits = local(SUPPORT, ("vta_gateway",), SUPPORT_ALLOWED)
    assert _local_value(snapshot, fits, TARGET) == (("vta_gateway",), None)
    unknown = local(SUPPORT, ("other",), SUPPORT_ALLOWED)
    assert _local_value(snapshot, unknown, TARGET) == (None, "unknown_entry")


def test_the_diagnostics_reason_names_the_measurement() -> None:
    data = {
        DEMAND: cloud(DEMAND, ("ch",), DEMAND_ALLOWED),
        SUPPORT: cloud(SUPPORT, ("not_supported",), SUPPORT_ALLOWED),
    }
    selection = LocalSelection(
        FakeRuntime(
            [
                demand(("ch", "dhw")),
                local(SUPPORT, ("vta_gateway", "unknown"), SUPPORT_ALLOWED),
            ]
        ),
        TARGET,
    )
    summary = selection.summary(data)
    assert summary["using_local"] == 1
    assert summary["cloud_reasons"] == {"unknown_entry": 1}
    assert summary["cloud_reason_measurements"] == {
        "unknown_entry": ["variable_tariff_support_status"]
    }


def test_scalar_readings_keep_their_reasons() -> None:
    path = "/heatSources/returnTemperature"
    marker_cloud = ResourceSnapshot(
        Resource(
            path,
            value=30.0,
            has_value=True,
            metadata=ResourceMetadata(
                resource_type="floatValue", unit="C", numeric_error_values=(-3276.8,)
            ),
        ),
        True,
        NOW,
    )
    marker = scalar_local(path, -3276.8)
    assert _local_value(marker_cloud, marker, TARGET) == (None, "local_invalid")
    assert _local_value(marker_cloud, scalar_local(path, 31.5), TARGET) == (
        31.5,
        None,
    )
    # A list where the cloud has a number is another shape.
    as_list = replace(demand(("ch",)), request=marker.request)
    assert _local_value(marker_cloud, as_list, TARGET)[1] == "type_differs"


# ------------------------------------------------------------- the entities


@pytest.mark.parametrize(
    ("path", "cloud_values", "local_values", "allowed", "expected"),
    [
        (DEMAND, ("ch",), ("dhw", "ch"), DEMAND_ALLOWED, "ch_dhw"),
        (DEMAND, ("ch",), (), DEMAND_ALLOWED, "none"),
        (SUPPORT, ("not_supported",), ("vta_gateway",), SUPPORT_ALLOWED, None),
    ],
)
async def test_the_entity_shows_the_same_state_from_either_source(
    hass: HomeAssistant, path, cloud_values, local_values, allowed, expected
) -> None:
    from tests.test_sensor import _sensor

    snapshot = cloud(path, cloud_values, allowed)
    sensor = _sensor(hass, snapshot.resource, value_key="values")
    coordinator = sensor.coordinator
    before = sensor.native_value
    runtime = FakeRuntime([local(path, local_values, allowed)])
    coordinator.local_selection = LocalSelection(runtime, TARGET)
    local_state = sensor.native_value
    local_attributes = sensor._base_attributes()
    # The cloud reports the same list: the same state and attributes.
    same = cloud(path, local_values, allowed)
    coordinator.local_selection = None
    coordinator.data = {path: replace(same, last_success=datetime.now(UTC))}
    assert sensor.native_value == local_state
    assert sensor._base_attributes() == local_attributes
    if expected is not None:
        assert local_state == expected and before != local_state
    else:
        assert local_state is not None and local_state != before
