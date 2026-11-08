"""Source choice for paired readings: local first, never polled less often (P13)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.bosch_buderus_heating.configuration import (
    CLOUD_KIND,
    load_configuration,
)
from custom_components.bosch_buderus_heating.coordinator import (
    SOURCE_STOPPED_CATEGORY,
    Freshness,
)
from custom_components.bosch_buderus_heating.local_api.catalog import (
    request_for_path,
)
from custom_components.bosch_buderus_heating.local_polling.inventory import (
    ResourceInventory,
)
from custom_components.bosch_buderus_heating.local_polling.policy import period
from custom_components.bosch_buderus_heating.local_selection import (
    LocalSelection,
    paired_cloud_interval,
)
from custom_components.bosch_buderus_heating.observations import PAIR_CATALOG
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
)
from tests.test_local_polling import CONTEXT
from tests.test_local_selection import TARGET as FAKE_TARGET
from tests.test_local_selection import FakeRuntime, cloud, local
from tests.test_local_setup import OWNER, starting

RETURN = "/heatSources/returnTemperature"
ISRC = "/system/iSRC/installationStatus"
OUTDOOR = "/system/sensors/temperatures/outdoor_t1"

# Cloud groups observed on my installation for the paired readings (P12 data).
OBSERVED_CLOUD_GROUPS = {
    "/system/sensors/temperatures/outdoor_t1": PollGroup.SLOW,
    "/system/type": PollGroup.STATIC,
    "/system/bus": PollGroup.SLOW,
    "/system/powerLimitation/active": PollGroup.SLOW,
    "/system/iSRC/installationStatus": PollGroup.FAST,
    "/system/iSRC/supportStatus": PollGroup.STATIC,
    "/system/variableTariff/supportStatus": PollGroup.FAST,
    "/gateway/brand": PollGroup.STATIC,
    "/gateway/versionFirmware": PollGroup.STATIC,
    "/gateway/versionHardware": PollGroup.STATIC,
    "/heatingCircuits/hc1/currentRoomSetpoint": PollGroup.FAST,
    "/heatingCircuits/hc1/currentSuWiMode": PollGroup.FAST,
    "/heatingCircuits/hc1/overallStatus": PollGroup.FAST,
    "/dhwCircuits/dhw1/actualTemp": PollGroup.FAST,
    "/dhwCircuits/dhw1/chargeRemainingTime": PollGroup.FAST,
    "/dhwCircuits/dhw1/currentSetpoint": PollGroup.FAST,
    "/dhwCircuits/dhw1/currentTemperatureLevel": PollGroup.FAST,
    "/dhwCircuits/dhw1/overallStatus": PollGroup.FAST,
    "/heatSources/actualHeatDemand": PollGroup.FAST,
    "/heatSources/actualModulation": PollGroup.FAST,
    "/heatSources/actualSupplyTemperature": PollGroup.FAST,
    "/heatSources/returnTemperature": PollGroup.FAST,
    "/heatSources/systemPressure": PollGroup.FAST,
    "/heatSources/Source/eHeater/status": PollGroup.FAST,
    "/heatSources/emStatus": PollGroup.FAST,
    "/heatSources/flameStatus": PollGroup.FAST,
    "/heatSources/hs1/heatPumpType": PollGroup.STATIC,
    "/heatSources/hs1/supplyFlowCondenserTemp": PollGroup.SLOW,
}


def _concrete(template: str) -> str:
    return (
        template.replace("{hc}", "hc1").replace("{dhw}", "dhw1").replace("{hs}", "hs1")
    )


def _snapshot(
    *, profile="standard", cloud_enabled=True, binding_enabled=True, local_values=True
):
    snapshot, _cloud, target = starting(local=True)
    data, options = snapshot.to_data(), snapshot.to_options()
    for key, connection in snapshot.connections.items():
        if connection.kind == CLOUD_KIND:
            options["connections"][key]["polling_profile"] = profile
            options["connections"][key]["enabled"] = cloud_enabled
    for key, binding in snapshot.bindings.items():
        if snapshot.connections[binding.connection_id].kind == CLOUD_KIND:
            options["bindings"][key]["enabled"] = binding_enabled
    options["targets"][target]["local_values"] = local_values
    return load_configuration(OWNER, data, options), target


async def test_coordinator_reports_the_observed_schedule(hass) -> None:
    from tests.test_validity import _coordinator

    resources = [
        Resource(path, value="x", has_value=True, metadata=ResourceMetadata())
        for path in OBSERVED_CLOUD_GROUPS
    ]
    coordinator = _coordinator(hass, *resources)
    coordinator._paths_by_group = {
        group: tuple(p for p, g in OBSERVED_CLOUD_GROUPS.items() if g is group)
        for group in set(OBSERVED_CLOUD_GROUPS.values())
    }
    for path, group in OBSERVED_CLOUD_GROUPS.items():
        known, interval = coordinator.scheduled_interval(path)
        expected = {
            PollGroup.FAST: timedelta(seconds=60),
            PollGroup.SLOW: timedelta(minutes=15),
            PollGroup.STATIC: None,
        }[group]
        assert known and interval == expected, path
    # Discovered without this path: final answer, no cap.
    assert coordinator.scheduled_interval("/unknown/path") == (True, None)


def _observed(binding: str, path: str) -> tuple[bool, timedelta | None]:
    group = OBSERVED_CLOUD_GROUPS.get(path)
    if group is None:
        return False, None
    return True, {
        PollGroup.FAST: timedelta(seconds=60),
        PollGroup.SLOW: timedelta(minutes=15),
        PollGroup.STATIC: None,
    }[group]


@pytest.mark.parametrize(
    ("profile", "path", "known", "expected"),
    [
        ("standard", RETURN, True, 60),
        ("standard", ISRC, True, 60),
        ("standard", OUTDOOR, True, 900),
        ("standard", "/gateway/brand", True, None),  # static: read once
        ("standard", OUTDOOR, False, 60),  # cloud not ready: shortest interval
        ("cloud_friendly", RETURN, False, 120),
        ("standard", "/heatSources/emon/totalConsumption", True, None),  # counter
        ("standard", "/heatSources/currentSupplySetpoint", True, None),  # unpaired
    ],
)
def test_paired_cloud_interval_follows_the_cloud_schedule(
    profile, path, known, expected
) -> None:
    snapshot, target = _snapshot(profile=profile)
    scheduled = _observed if known else None
    assert (
        paired_cloud_interval(snapshot, target, path, scheduled=scheduled) == expected
    )


@pytest.mark.parametrize(
    "changes",
    [{"cloud_enabled": False}, {"binding_enabled": False}, {"local_values": False}],
)
def test_disabled_cloud_sets_no_cap(changes) -> None:
    snapshot, target = _snapshot(**changes)
    assert paired_cloud_interval(snapshot, target, RETURN) is None
    assert paired_cloud_interval(snapshot, "another-target", RETURN) is None


@pytest.mark.parametrize(
    ("standard_path", "configured", "cap", "expected"),
    [
        (RETURN, 60, None, 60),
        (RETURN, 300, None, 300),
        (RETURN, 300, 60, 60),  # min(local 300, cloud 60)
        (RETURN, 300, 120, 120),
        (ISRC, 60, 60, 60),  # metadata class 900 → cloud 60
        ("/gateway/brand", 60, None, 86400),
        (RETURN, 60, 30, 60),  # never below 60 s
    ],
)
def test_effective_period_is_min_of_local_and_cloud(
    standard_path, configured, cap, expected
) -> None:
    assert period(request_for_path(standard_path), configured, cap) == expected


@pytest.mark.parametrize("configured", [60, 300, 900])
@pytest.mark.parametrize("profile", ["standard", "cloud_friendly"])
def test_local_is_never_polled_less_often_than_the_cloud(configured, profile) -> None:
    snapshot, target = _snapshot(profile=profile)
    context = CONTEXT.identity
    inventory = ResourceInventory(
        context,
        configured,
        lambda request: paired_cloud_interval(
            snapshot, target, request.path, scheduled=_observed
        ),
    )
    checked = 0
    for pair in PAIR_CATALOG:
        if pair.counter:
            continue
        path = _concrete(pair.template)
        cloud_interval = paired_cloud_interval(
            snapshot, target, path, scheduled=_observed
        )
        if cloud_interval is None:
            continue
        local_period = inventory.period(request_for_path(path))
        assert local_period is not None and local_period <= cloud_interval, path
        checked += 1
    assert checked >= 20


def test_capped_period_is_reported_and_drives_expiry() -> None:
    snapshot, target = _snapshot()
    context = CONTEXT.identity
    capped = ResourceInventory(
        context,
        60,
        lambda request: paired_cloud_interval(snapshot, target, request.path),
    )
    plain = ResourceInventory(context, 60)
    request = request_for_path(ISRC)
    assert plain.period(request) == 900
    assert capped.period(request) == 60
    record = capped.add(request, 0.0)
    view = capped.view(record, 0.0, source_ready=True)
    assert view.target_interval_seconds == 60


# ------------------------------------------------------------ diagnostics


def test_summary_names_why_eligible_readings_use_the_cloud() -> None:
    data = {
        RETURN: cloud(RETURN, 30.0),
        "/heatSources/actualSupplyTemperature": cloud(
            "/heatSources/actualSupplyTemperature", 40.0
        ),
        "/heatSources/systemPressure": cloud(
            "/heatSources/systemPressure", 2.0, unit="bar"
        ),
        "/heatSources/actualModulation": cloud(
            "/heatSources/actualModulation", 40.0, unit="%"
        ),
    }
    runtime = FakeRuntime(
        [
            local(RETURN, 31.5),
            local("/heatSources/systemPressure", 2.1, unitOfMeasure="kPa"),
            local("/heatSources/actualModulation", 40.0, unitOfMeasure="unknown"),
        ]
    )
    summary = LocalSelection(runtime, FAKE_TARGET).summary(data)
    assert summary["using_local"] == 1 and summary["using_cloud"] == 3
    assert sum(summary["cloud_reasons"].values()) == 3
    assert summary["cloud_reasons"]["local_not_current"] == 1
    assert "31.5" not in repr(summary)


# ------------------------------------------------------- decision matrix


def _cloud_state(state: str):
    base = replace(cloud(RETURN, 30.0), last_success=datetime.now(UTC))
    return {
        "current": base,
        "expired": replace(base, last_success=datetime.now(UTC) - timedelta(hours=1)),
        "no_answer": replace(
            base,
            available=False,
            freshness=Freshness.STALE,
            last_error_category="no_answer_requesttimeout",
        ),
        "ended": replace(
            base,
            available=False,
            freshness=Freshness.STALE,
            last_error_category="http_404",
            ended=True,
        ),
        "stopped": replace(
            base,
            available=False,
            freshness=Freshness.STALE,
            last_error_category=SOURCE_STOPPED_CATEGORY,
            ended=True,
        ),
        "withdrawn": None,
    }[state]


def _local_state(state: str) -> FakeRuntime:
    return {
        "current": FakeRuntime([local(RETURN, 31.5)]),
        "absent": FakeRuntime([]),
        "invalid": FakeRuntime([local(RETURN, 31.5, unitOfMeasure="kPa")]),
        "disabled": FakeRuntime([local(RETURN, 31.5)], binding_enabled=False),
        "contradicts": FakeRuntime([local(RETURN, 45.0)]),
    }[state]


# (cloud, local, switch) → shown value; None = unavailable.
MATRIX = [
    ("current", "current", True, 31.5),
    ("current", "contradicts", True, 45.0),  # both valid: local, no reaction
    ("current", "absent", True, 30.0),
    ("current", "invalid", True, 30.0),
    ("current", "disabled", True, 30.0),
    ("current", "current", False, 30.0),
    ("expired", "current", True, 31.5),
    ("expired", "absent", True, None),
    ("no_answer", "current", True, 31.5),
    ("no_answer", "absent", True, 30.0),  # last good value within its limit
    ("ended", "current", True, 31.5),
    ("ended", "absent", True, None),
    ("stopped", "current", True, None),  # a stopped source is never shown
    ("stopped", "absent", True, None),
    ("withdrawn", "current", True, None),  # no cloud resource, no entity value
    ("expired", "current", False, None),
]


@pytest.mark.parametrize(("cloud_state", "local_state", "switch", "shown"), MATRIX)
async def test_decision_matrix(hass, cloud_state, local_state, switch, shown) -> None:
    from tests.test_sensor import _sensor

    resource = cloud(RETURN, 30.0).resource
    sensor = _sensor(hass, resource)
    coordinator = sensor.coordinator
    snapshot = _cloud_state(cloud_state)
    coordinator.data = {} if snapshot is None else {RETURN: snapshot}
    coordinator.last_update_success = cloud_state in ("current", "expired")
    runtime = _local_state(local_state)
    runtime.snapshot.enabled = switch
    coordinator.local_selection = LocalSelection(runtime, FAKE_TARGET)
    if shown is None:
        assert not sensor.available
    else:
        assert sensor.available and sensor.native_value == shown


def test_local_source_cap_uses_the_cloud_plan_and_follows_changes() -> None:
    from types import SimpleNamespace

    from custom_components.bosch_buderus_heating.managed_local import (
        LocalSource,
    )

    snapshot, target = _snapshot()
    cloud_binding = next(
        key
        for key, binding in snapshot.bindings.items()
        if snapshot.connections[binding.connection_id].kind == CLOUD_KIND
    )
    plan = SimpleNamespace(scheduled_interval=lambda path: _observed("", path))
    runtime = SimpleNamespace(
        snapshot=snapshot,
        source_data=lambda key: plan if key == cloud_binding else None,
    )
    owner = SimpleNamespace(binding=SimpleNamespace(target_id=target))
    cap = LocalSource._paired_cap(owner, SimpleNamespace(runtime=runtime))
    assert cap(request_for_path(OUTDOOR)) == 900
    assert cap(request_for_path(ISRC)) == 60
    # A new snapshot with the cloud-friendly profile applies without restart.
    friendly = SimpleNamespace(scheduled_interval=lambda path: (False, None))
    runtime.snapshot, _ = _snapshot(profile="cloud_friendly")
    runtime.source_data = lambda key: friendly if key == cloud_binding else None
    assert cap(request_for_path(OUTDOOR)) == 120  # discovered nothing yet
    # Cloud source not running: no cloud pace to keep up with.
    runtime.source_data = lambda key: None
    assert cap(request_for_path(OUTDOOR)) is None


def test_shorter_cap_never_ages_a_reading_faster_than_scheduled() -> None:
    from types import SimpleNamespace

    from custom_components.bosch_buderus_heating.local_polling.inventory import (
        Freshness,
    )

    caps = {"value": None}
    inventory = ResourceInventory(CONTEXT.identity, 900, lambda request: caps["value"])
    request = request_for_path(OUTDOOR)
    record = inventory.add(request, 0.0)
    record.scheduled_period = 900  # as set by accept() for this read
    record.observation = SimpleNamespace(observed_monotonic=0.0)
    caps["value"] = 60  # e.g. the cloud reloads and briefly reports another plan
    view = inventory.view(record, 400.0, source_ready=True)
    assert view.freshness is Freshness.FRESH and view.available
    assert inventory.period(request) == 60  # the next read follows the cap
    late = inventory.view(record, 2800.0, source_ready=True)
    assert late.freshness is Freshness.EXPIRED


async def test_cloud_plan_answers_are_final_only_after_discovery(hass) -> None:
    from tests.test_validity import _coordinator

    adjustable = Resource(
        "/heatingCircuits/hc1/maxFlowTemp",
        value=55.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", writable=True),
    )
    empty = _coordinator(hass)
    empty.data = {}
    assert empty.scheduled_interval(RETURN) == (False, None)
    coordinator = _coordinator(hass, adjustable)
    assert coordinator.scheduled_interval(RETURN) == (True, None)  # not provided
    assert coordinator.scheduled_interval(adjustable.path) == (True, None)


def test_summary_reasons_add_up_when_off_or_stopped() -> None:
    data = {RETURN: cloud(RETURN, 30.0)}
    off = FakeRuntime([local(RETURN, 31.5)], enabled=False)
    summary = LocalSelection(off, FAKE_TARGET).summary(data)
    assert summary["cloud_reasons"] == {"option_off": 1}
    assert summary["using_local"] == 0 and summary["using_cloud"] == 1
    stopped = {
        RETURN: replace(
            data[RETURN],
            available=False,
            last_error_category=SOURCE_STOPPED_CATEGORY,
            ended=True,
        )
    }
    on = FakeRuntime([local(RETURN, 31.5)])
    summary = LocalSelection(on, FAKE_TARGET).summary(stopped)
    assert summary["cloud_reasons"] == {"source_stopped": 1}
    assert summary["using_local"] == 0
