"""Equivalent unit spellings and named states of paired readings (P14)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.observations import (
    ERROR_MARKERS,
    PairResult,
    ValueStatus,
    canonical_unit,
    compare,
)
from tests.test_local_api import scalar
from tests.test_local_selection import TARGET, FakeRuntime, cloud, local
from tests.test_observations import cloud as cloud_observation
from tests.test_observations import local as local_observation

RETURN = "/heatSources/returnTemperature"
SETPOINT = "/heatingCircuits/hc1/currentRoomSetpoint"


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("C", "°C", True),
        ("degC", "C", True),
        ("ºC", "°C", True),
        ("min", "mins", True),
        ("wh", "Wh", True),
        ("", None, True),
        ("C", "K", False),
        ("W", "kW", False),  # a factor is a conversion, not a spelling
        ("l/h", "l/min", False),
    ],
)
def test_only_spellings_are_equivalent(left, right, same) -> None:
    assert (canonical_unit(left) == canonical_unit(right)) is same


@pytest.mark.parametrize("unit", ["C", "degC", "°C", "ºC"])
def test_contract_spellings_include_the_temperature_aliases(unit) -> None:
    """SEL-2: one normalization for every caller; a "°C" schema is a "C" one."""
    from custom_components.bosch_buderus_heating.pointt import (
        Resource,
        ResourceMetadata,
    )
    from custom_components.bosch_buderus_heating.resource_catalog import (
        _matches_optional_app_schema,
    )
    from custom_components.bosch_buderus_heating.rrc import scalar_value
    from custom_components.bosch_buderus_heating.unit_contracts import (
        canonical_unit as contract_unit,
    )

    assert contract_unit(unit) == contract_unit("C") == canonical_unit(unit)
    for path, writable in (
        ("/heatingCircuits/hc1/roomtemperature", False),
        ("/zones/zone1/heating/manualRoomSetpoint", True),
    ):
        resource = Resource(
            path,
            value=21.5,
            has_value=True,
            metadata=ResourceMetadata(
                resource_type="floatValue", unit=unit, writable=writable
            ),
        )
        assert _matches_optional_app_schema(resource), (path, unit)
    assert contract_unit("K") != contract_unit("C")
    assert contract_unit("") == ""
    assert contract_unit(None) is None
    rrc = Resource(
        "/zones/zn1/temperatureActual",
        value=19.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit=unit),
    )
    assert scalar_value(rrc) == 19.0


def test_equivalent_spelling_is_comparable_and_supplies_the_entity() -> None:
    result = compare(
        cloud_observation(RETURN, 30.0, unit="C"),
        local_observation(scalar(RETURN, value=30.0, unitOfMeasure="°C")),
    )
    assert result is not None and result.result is PairResult.AGREE
    shown = LocalSelection(
        FakeRuntime([local(RETURN, 30.5, unitOfMeasure="°C")]), TARGET
    ).select(RETURN, cloud(RETURN, 30.0, unit="C"))
    assert shown.resource.value == 30.5
    assert shown.resource.metadata.unit == "C"  # entity keeps its cloud unit


def test_named_state_is_an_ordinary_value_like_in_the_cloud() -> None:
    observation = local_observation(scalar(SETPOINT, value=0, state=[{"off": 0}]))
    assert observation.status is ValueStatus.VALID and observation.value == 0
    result = compare(cloud_observation(SETPOINT, 0.0), observation)
    assert result is not None and result.result is PairResult.AGREE
    shown = LocalSelection(
        FakeRuntime([local(SETPOINT, 0, state=[{"off": 0}])]), TARGET
    ).select(SETPOINT, cloud(SETPOINT, 21.0))
    assert shown is not None and shown.resource.value == 0.0


@pytest.mark.parametrize("marker", sorted(ERROR_MARKERS))
def test_error_markers_are_never_supplied_locally(marker) -> None:
    observation = local_observation(
        scalar(RETURN, value=-32768, state=[{marker: -32768}])
    )
    assert observation.status is ValueStatus.STATE_MARKER
    snapshot = cloud(RETURN, 30.0)
    shown = LocalSelection(
        FakeRuntime([local(RETURN, -32768, state=[{marker: -32768}])]), TARGET
    ).select(RETURN, snapshot)
    assert shown is snapshot


def test_a_number_the_cloud_declares_an_error_is_never_supplied() -> None:
    snapshot = cloud(RETURN, 30.0)
    snapshot = replace(
        snapshot,
        resource=replace(
            snapshot.resource,
            metadata=replace(
                snapshot.resource.metadata, numeric_error_values=(-3276.8,)
            ),
        ),
    )
    selection = LocalSelection(FakeRuntime([local(RETURN, -3276.8)]), TARGET)
    assert selection.select(RETURN, snapshot) is snapshot
    summary = selection.summary({RETURN: snapshot})
    assert summary["cloud_reasons"] == {"local_invalid": 1}
    assert summary["cloud_reason_measurements"] == {
        "local_invalid": ["return_temperature"]
    }


@pytest.mark.parametrize(
    ("unit", "reason"),
    [
        # An unknown unit makes the local value unusable.
        ("psi", "local_invalid"),
        # A known pressure unit on a temperature is a different unit.
        ("kPa", "unit_differs"),
    ],
)
def test_measurement_names_are_catalog_names_without_values(unit, reason) -> None:
    data = {RETURN: cloud(RETURN, 30.0), SETPOINT: cloud(SETPOINT, 21.0)}
    selection = LocalSelection(
        FakeRuntime([local(RETURN, 30.5, unitOfMeasure=unit)]), TARGET
    )
    summary = selection.summary(data)
    assert summary["cloud_reason_measurements"] == {
        reason: ["return_temperature"],
        "local_not_current": ["room_setpoint_current"],
    }
    rendered = repr(summary)
    assert "30.5" not in rendered and "/heat" not in rendered


@pytest.mark.parametrize("value", [-3276.8, 3276.7])
def test_one_decimal_form_of_an_error_marker_is_a_marker(value) -> None:
    states = [{"open": -32768}, {"short": 32767}]
    observation = local_observation(scalar(RETURN, value=value, state=states))
    assert observation.status is ValueStatus.STATE_MARKER
    snapshot = cloud(RETURN, 30.0)
    shown = LocalSelection(
        FakeRuntime([local(RETURN, value, state=states)]), TARGET
    ).select(RETURN, snapshot)
    assert shown is snapshot


def test_ordinary_values_near_markers_stay_valid() -> None:
    states = [{"open": -32768}, {"short": 32767}]
    for value in (-3276.7, 24.4, 0):
        observation = local_observation(scalar(RETURN, value=value, state=states))
        assert observation.status is ValueStatus.VALID, value


def test_option_off_names_no_measurements() -> None:
    data = {RETURN: cloud(RETURN, 30.0)}
    summary = LocalSelection(
        FakeRuntime([local(RETURN, 30.5)], enabled=False), TARGET
    ).summary(data)
    assert summary["cloud_reasons"] == {"option_off": 1}
    assert summary["cloud_reason_measurements"] == {}


def test_diagnostics_merge_names_across_installations_views() -> None:
    from custom_components.bosch_buderus_heating.diagnostics import _merged_names

    merged = _merged_names(
        [
            {"cloud_reason_measurements": {"local_invalid": ["return_temperature"]}},
            {
                "cloud_reason_measurements": {
                    "local_invalid": ["room_setpoint_current"],
                    "local_not_current": ["supply_temperature"],
                }
            },
            {},
        ]
    )
    assert merged == {
        "local_invalid": ["return_temperature", "room_setpoint_current"],
        "local_not_current": ["supply_temperature"],
    }


def test_new_pairs_with_unknown_named_states_need_a_review() -> None:
    """Named states other than error markers must be reviewed before pairing."""
    import json
    import re
    from pathlib import Path as _Path

    from custom_components.bosch_buderus_heating.observations import PAIR_CATALOG

    reviewed = {"room_setpoint_current": {"off"}}
    examples = json.loads(
        _Path("tests/fixtures/local_api_examples.json").read_text(encoding="utf-8")
    )["examples"]

    def key(template: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", template)

    states: dict[str, set[str]] = {}
    for example in examples:
        raw = example["response"].get("state") or []
        names = {name for item in raw for name in item}
        states.setdefault(key(example["template"]), set()).update(names)
    for pair in PAIR_CATALOG:
        named = states.get(key(pair.template), set()) - ERROR_MARKERS
        assert named <= reviewed.get(pair.measurement, set()), pair.measurement
