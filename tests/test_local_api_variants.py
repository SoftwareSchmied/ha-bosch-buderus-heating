"""Synthetic regressions for observed local response shapes, without plant data."""

import json
from decimal import Decimal

import pytest

from custom_components.bosch_buderus_heating.local_api.errors import LocalResourceError
from custom_components.bosch_buderus_heating.local_api.models import (
    ScalarValue,
    UpdateReport,
)
from tests.test_local_api import example, read, scalar


@pytest.mark.parametrize("kind", ["integerValue", "floatValue"])
@pytest.mark.parametrize("as_object", [True, False])
@pytest.mark.parametrize("value", [0, 21, 90])
def test_state_shapes_preserve_measurement_semantics_and_original_document(
    kind, as_object, value
):
    states = {"EXAMPLE_OFF": 0, "EXAMPLE_UNAVAILABLE": 90}
    raw = scalar(
        "/signals/Example.Temperature",
        type=kind,
        value=value,
        state=states if as_object else [states],
        writeable=1,
    )
    before = json.dumps(raw)
    observation = read(raw)
    data = observation.data
    assert isinstance(data, ScalarValue)
    assert {s.name: s.value for s in data.states} == states
    assert data.matched_states == tuple(k for k, v in states.items() if v == value)
    assert data.measurement == (21 if value == 21 else None)
    assert observation.can_write is False
    assert observation.document["state"] == (states if as_object else (states,))
    assert json.dumps(raw) == before
    with pytest.raises(TypeError):
        observation.document["state"] = {}


def test_float_state_map_keeps_decimal_precision():
    result = read(scalar(state={"EXAMPLE_LIMIT": 21.25})).data
    assert result.states[0].value == Decimal("21.25")
    assert result.measurement is None


@pytest.mark.parametrize(
    "state",
    [{}, {"EXAMPLE": True}, {"EXAMPLE": "0"}, {"EXAMPLE": None}, {"EXAMPLE": []}],
)
def test_invalid_state_objects_still_reject_complete_resource(state):
    with pytest.raises(LocalResourceError):
        read(scalar(state=state))


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "integerValue", "value": 1, "state": {"EXAMPLE": 0.5}},
        {"value": "21"},
        {"writeable": True},
        {"minValue": 30, "maxValue": 10},
    ],
)
def test_state_compatibility_preserves_other_validation(changes):
    raw = scalar(state={"EXAMPLE": 0})
    raw.update(changes)
    with pytest.raises(LocalResourceError):
        read(raw)


@pytest.mark.parametrize("path", ["/gateway/update/report", "/system/update/report"])
@pytest.mark.parametrize("field", ["moduleId", "module_id", "both"])
@pytest.mark.parametrize("identifier", ["000000000000001", 9007199254740993])
def test_update_report_identifier_variants_keep_exact_source(path, field, identifier):
    raw = example("/system/update/report")
    raw["id"] = path
    row = {"status": "SUCCESS", "timeStamp": "1700000000"}
    row.update(
        {
            name: identifier
            for name in (("moduleId", "module_id") if field == "both" else (field,))
        }
    )
    raw["values"] = [row]
    observation = read(raw)
    assert isinstance(observation.data, UpdateReport)
    module = observation.data.modules[0]
    assert module.module_id == identifier
    assert type(module.module_id) is type(identifier)
    assert module.fields == row
    assert observation.document["values"] == (row,)


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"module_id": None},
        {"module_id": True},
        {"module_id": []},
        {"module_id": ""},
        {"module_id": 1.5},
        {"moduleId": "001", "module_id": "002"},
        {"moduleId": "001", "module_id": 1},
        {"moduleId": 1, "module_id": True},
    ],
)
def test_missing_invalid_or_conflicting_module_identity_is_never_invented(identity):
    raw = example("/system/update/report")
    raw["values"] = [{"status": "SUCCESS", "timeStamp": "1700000000", **identity}]
    with pytest.raises(LocalResourceError):
        read(raw)


@pytest.mark.parametrize(
    "changes", [{"status": "UNKNOWN"}, {"timeStamp": 1}, {"statusCode": -1}]
)
def test_report_alias_keeps_other_schema_constraints(changes):
    raw = example("/system/update/report")
    raw["values"] = [
        {"module_id": "001", "status": "SUCCESS", "timeStamp": "1700000000", **changes}
    ]
    with pytest.raises(LocalResourceError):
        read(raw)
