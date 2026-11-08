"""B1: a real local capture of a K40RF heat pump, read through the real code.

The fixture holds every path the gateway (firmware 15.00.01, heat pump with
hs1, hc1 and dhw1) answered with HTTP 200 on 2026-11-08, read once and
read-only and anonymized (no gateway identity, documentation address in the
signal references; ``/system/basicInfo`` and the EEBUS ID/SKI signals were
not captured). The counts below are pinned on purpose: a change of the
decoder, the pair catalog, the local-only rules or the local plant view shows
up here for review. No network, no Home Assistant instance.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

from custom_components.bosch_buderus_heating.coordinator import (
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.faults import (
    local_fault_resource,
    parse_fault_resource,
)
from custom_components.bosch_buderus_heating.gateway_identity import (
    GatewayNamespace,
    IdentityContext,
)
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_api.decoder import decode_resource
from custom_components.bosch_buderus_heating.local_api.models import (
    FaultList,
    LocalObservation,
    ScalarValue,
    ValueArray,
)
from custom_components.bosch_buderus_heating.local_plant_view import (
    build_local_plant_view,
    local_sensor,
)
from custom_components.bosch_buderus_heating.local_selection import _local_value
from custom_components.bosch_buderus_heating.observations import (
    ValueStatus,
    from_local_observation,
    pair_for_path,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata

CAPTURE = (
    Path(__file__).parent / "fixtures/local_capture/k40rf-heat-pump-2026-11-08.jsonl"
)
CONTEXT = IdentityContext(GatewayNamespace.K40_LOCAL, "capture-owner", "local", 2)
OBSERVED_AT = datetime(2026, 11, 8, 8, 0, tzinfo=UTC)
DEMAND = "/heatSources/actualHeatDemand"
SUPPORT = "/system/variableTariff/supportStatus"


@cache
def capture() -> tuple[tuple[str, dict], ...]:
    rows = []
    for line in CAPTURE.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        rows.append((row["path"], row["body"]))
    return tuple(rows)


def _decode(index: int, path: str, body: dict) -> LocalObservation:
    signal = path.removeprefix("/signals/")
    request = request_for_path(
        path, signal_ids=(signal,) if path.startswith("/signals/") else ()
    )
    return decode_resource(
        request,
        json.dumps(body).encode(),
        context=CONTEXT,
        observed_at=OBSERVED_AT,
        observed_monotonic=float(index),
    )


@cache
def observations() -> tuple[LocalObservation, ...]:
    return tuple(
        _decode(index, path, body) for index, (path, body) in enumerate(capture())
    )


def by_path(path: str) -> LocalObservation:
    return next(item for item in observations() if item.request.path == path)


def signals() -> tuple[LocalObservation, ...]:
    return tuple(
        item for item in observations() if item.request.path.startswith("/signals/")
    )


# ---------------------------------------------------------------- decoding


def test_every_answer_decodes_to_the_expected_models() -> None:
    assert len(capture()) == 177
    assert [item.request.path for item in observations()] == [
        path for path, _ in capture()
    ]
    assert Counter(type(item.data).__name__ for item in observations()) == {
        "ScalarValue": 166,
        "NamedValues": 6,
        "ValueArray": 2,
        "ReferenceList": 1,
        "ConfigurationValue": 1,
        "FaultList": 1,
    }
    # Every signal is a scalar reading; the signal list itself a refEnum.
    assert len(signals()) == 106
    assert all(isinstance(item.data, ScalarValue) for item in signals())
    assert by_path("/signals").type == "refEnum"
    references = by_path("/signals").data.references
    # The two EEBUS identity signals are listed but were not captured.
    assert len(references) == 108


def test_only_the_known_issues_are_reported() -> None:
    issues = {
        item.request.path: tuple(issue.value for issue in item.data.issues)
        for item in observations()
        if getattr(item.data, "issues", ())
    }
    assert issues == {
        # Installation date parts in "day", "month" and "year": not units the
        # decoder knows, so never shown as measurements.
        "/signals/SC.InstallationDate.Day": ("unknown_unit",),
        "/signals/SC.InstallationDate.Month": ("unknown_unit",),
        "/signals/SC.InstallationDate.Year": ("unknown_unit",),
        # Start counters have no unit; the reading stays valid.
        "/heatSources/hs1/numberOfStarts": ("missing_unit",),
    }


# ------------------------------------------------------------ observations


def test_no_paired_reading_is_unsupported() -> None:
    paired = [item for item in observations() if pair_for_path(item.request.path)]
    statuses = Counter(
        from_local_observation(
            item, connection_id="local", target_id="plant", current=True
        ).status
        for item in paired
    )
    assert statuses == {ValueStatus.VALID: 42}


def test_the_lists_are_valid_and_replace_the_cloud_list() -> None:
    demand = by_path(DEMAND)
    assert isinstance(demand.data, ValueArray)
    assert demand.data.values == ("frost",)
    assert demand.data.allowed_values == ("", "ch", "dhw", "frost")
    support = by_path(SUPPORT)
    assert isinstance(support.data, ValueArray)
    for item in (demand, support):
        reading = from_local_observation(
            item, connection_id="local", target_id="plant", current=True
        )
        assert reading.status is ValueStatus.VALID
        assert reading.items == item.data.values
        # The cloud of this plant declares the same entries (B2).
        cloud = ResourceSnapshot(
            Resource(
                item.request.path,
                values=("ch",) if item is demand else ("not_supported",),
                has_values=True,
                metadata=ResourceMetadata(
                    resource_type="arrayData",
                    allowed_values=item.data.allowed_values,
                ),
            ),
            True,
            OBSERVED_AT - timedelta(minutes=1),
            freshness=Freshness.FRESH,
        )
        assert _local_value(cloud, item, "plant") == (item.data.values, None)


def test_the_empty_error_list_is_a_complete_list() -> None:
    errors = by_path("/notifications")
    assert isinstance(errors.data, FaultList) and errors.data.faults == ()
    resource = local_fault_resource(errors)
    assert resource is not None and resource.has_values
    parsed = parse_fault_resource(resource, observed_at=OBSERVED_AT)
    assert parsed.faults == () and parsed.invalid_entries == 0


# ------------------------------------------------------- signals and view


def test_signals_follow_the_local_only_rules() -> None:
    """C7: every signal becomes an entity, with and without the cloud.

    Previously 51 text true/false signals, 21 numbers whose current value was
    not a number and the 3 installation date parts (unknown unit) gave none.
    """

    assert Counter(local_sensor(item)[1] for item in signals()) == {"ok": 106}
    sensors = [local_sensor(item)[0] for item in signals()]
    kinds = Counter(sensor.definition.kind for sensor in sensors if sensor)
    assert kinds == {
        # text true/false (C7-1) and the two TRUE/FALSE state maps (O107)
        "flag": 53,
        "generic": 27,
        "state": 17,  # state maps of real names, no unit (C7-2)
        "duration": 3,
        "temperature": 2,
        "power": 1,
        "percent": 1,
        "mode": 1,  # the smart grid mode
        "flow": 1,
    }
    enabled = sorted(
        item.request.path.removeprefix("/signals/")
        for item, sensor in zip(signals(), sensors, strict=True)
        if sensor is not None and sensor.enabled
    )
    assert len(enabled) == 24  # 13 on/off, 3 states, 8 measurements
    # #80: generic signals with a measurement unit get statistics; states,
    # on/off signals, times and numbers without a unit never do.
    classes = Counter(
        (sensor.definition.kind, sensor.unit, str(sensor.state_class))
        for sensor in sensors
        if sensor is not None and sensor.definition.kind in ("generic", "flag", "state")
    )
    assert all(
        (state_class == "measurement")
        == (kind == "generic" and unit not in (None, "s", "min", "h"))
        for (kind, unit, state_class) in classes
    )
    assert (
        sum(
            count
            for (kind, _, state_class), count in classes.items()
            if kind == "generic" and state_class == "measurement"
        )
        > 0
    )
    assert "SRC.CUHP.HMI.CompressorActive" in enabled
    assert "SRC.CUHP.HP1.CompressorStatus" not in enabled  # its duplicate


def test_the_local_plant_view_builds_from_the_capture() -> None:
    view = build_local_plant_view(observations())
    assert view.readings == 177
    assert len(view.resources) == 37  # paired readings in cloud form
    # 82 before C7, plus the 51 on/off signals, the 3 installation date parts
    # (C7-3) and the room controller assignment (C7-6).
    assert len(view.local_sensors) == 136
    assert view.skipped == {
        "private": 1,  # the time zone of the gateway
        "unsupported_shape": 3,  # signal list, zone configuration, error list
    }
    shapes = view.entity_shapes()
    # C4 (revised): the pool start counter again (disabled); C3: the
    # cylinder temperature disabled; C7: 24 signals enabled.
    assert Counter(platform for platform, _ in shapes) == {"sensor": 191}
    assert sum(bool(shape.enabled) for shape in shapes.values()) == 77


async def test_with_the_cloud_the_same_signals_become_entities(hass) -> None:
    """The with-cloud path (P45 selection) gives the same signal entities as
    the local plant view, plus the installation date."""
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        BoschBuderusInstallationDateSensor,
    )
    from tests.test_captures import load
    from tests.test_local_only import coordinator_with, track

    _, cloud = load(
        Path(__file__).parent / "fixtures/contracts/captures/k40-cloud-2026-11-05.json"
    )
    coordinator = coordinator_with(hass, list(observations()), *cloud.values())
    entities = track(coordinator)
    paths = {
        item.resource_path
        for item in entities
        if not isinstance(item, BoschBuderusInstallationDateSensor)
    }
    signal_paths = {path for path in paths if path.startswith("/signals/")}
    assert signal_paths == {item.request.path for item in signals()}
    assert len(signal_paths) + 1 == 107  # 106 signals and the installation date
    assert len(entities) == len(paths) + 1
    # The same local-only entities as the local plant view, except readings
    # the running cloud offers itself.
    view = build_local_plant_view(observations())
    assert paths == {sensor.path for sensor in view.local_sensors} - set(cloud)
    await coordinator.async_shutdown()
