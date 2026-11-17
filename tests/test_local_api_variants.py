"""Synthetic regressions for observed local response shapes, without plant data."""

import json
from decimal import Decimal

import pytest

from custom_components.bosch_buderus_heating.local_api.errors import LocalResourceError
from custom_components.bosch_buderus_heating.local_api.models import (
    DeviceFaultList,
    FaultList,
    ReferenceList,
    ScalarValue,
    SystemInfo,
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


_ROW_LISTS = [
    ("/signals", "references", ReferenceList),
    ("/system/basicInfo", "values", SystemInfo),
    ("/notifications", "values", FaultList),
    ("/devices/device1/errors", "values", DeviceFaultList),
]


@pytest.mark.parametrize(("path", "field", "kind"), _ROW_LISTS)
def test_added_row_fields_from_newer_firmware_do_not_reject_the_resource(
    path, field, kind
):
    raw = example(path)
    plain = read(example(path)).data
    for row in raw[field]:
        row["newFirmwareField"] = {"nested": [1, 2]}
    before = json.dumps(raw)
    observation = read(raw)
    assert isinstance(observation.data, kind)
    assert observation.data == plain
    assert json.dumps(raw) == before
    assert all("newFirmwareField" in row for row in observation.document[field])


@pytest.mark.parametrize(("path", "field", "kind"), _ROW_LISTS)
def test_tolerated_rows_keep_required_fields_and_types_strict(path, field, kind):
    raw = example(path)
    required = sorted(raw[field][0])[0]
    missing = example(path)
    del missing[field][0][required]
    with pytest.raises(LocalResourceError):
        read(missing)
    wrong = example(path)
    wrong[field][0][required] = (
        [] if not isinstance(raw[field][0][required], list) else 1
    )
    with pytest.raises(LocalResourceError):
        read(wrong)


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


# A module row as a K40RF on firmware 15.00.01 reports it: no serial number.
_BURNER_ROW = {
    "ModuleHwIdentStr": "Burner module 239",
    "Tok": "8",
    "Ver": "1.3.0+1",
    "Id": "65519",
}
_EMPTY_ROW = {
    "Tok": "",
    "Id": "",
    "Ver": "",
    "ModuleSerialNumber": "",
    "ModuleHwIdentStr": "",
}
_OPTIONAL_FIELDS = ["ModuleSerialNumber", "ModuleHwIdentStr", "Ver"]


def _basic_info(*extra):
    raw = example("/system/basicInfo")
    raw["values"].extend(json.loads(json.dumps(row)) for row in extra)
    return raw


def test_a_module_without_serial_number_is_decoded_as_missing():
    plain = read(example("/system/basicInfo")).data
    raw = _basic_info(_BURNER_ROW, _EMPTY_ROW)
    before = json.dumps(raw)
    observation = read(raw)
    modules = observation.data.modules
    assert modules[: len(plain.modules)] == plain.modules
    burner, empty = modules[len(plain.modules) :]
    assert burner.serial_number is None
    assert (burner.hardware, burner.version) == ("Burner module 239", "1.3.0+1")
    assert (empty.serial_number, empty.hardware, empty.version) == ("", "", "")
    assert json.dumps(raw) == before
    assert all(
        "ModuleSerialNumber" not in row
        for row in observation.document["values"]
        if row["Id"] == "65519"
    )


@pytest.mark.parametrize("field", _OPTIONAL_FIELDS)
def test_each_descriptive_module_field_may_be_missing(field):
    row = {**_BURNER_ROW, "ModuleSerialNumber": "0001"}
    del row[field]
    module = read(_basic_info(row)).data.modules[-1]
    attribute = {
        "ModuleSerialNumber": "serial_number",
        "ModuleHwIdentStr": "hardware",
        "Ver": "version",
    }[field]
    assert getattr(module, attribute) is None
    assert module.token == "8" and module.id == "65519"


@pytest.mark.parametrize("field", ["Tok", "Id"])
def test_a_module_without_its_key_is_still_rejected(field):
    row = {**_BURNER_ROW}
    del row[field]
    with pytest.raises(LocalResourceError):
        read(_basic_info(row))


@pytest.mark.parametrize("field", _OPTIONAL_FIELDS)
@pytest.mark.parametrize("value", [None, [], True])
def test_a_wrongly_typed_descriptive_field_is_still_rejected(field, value):
    with pytest.raises(LocalResourceError):
        read(_basic_info({**_BURNER_ROW, "ModuleSerialNumber": "0001", field: value}))


def test_a_fractional_serial_number_is_still_invalid_metadata():
    with pytest.raises(LocalResourceError):
        read(_basic_info({**_BURNER_ROW, "ModuleSerialNumber": 12.25}))


@pytest.mark.parametrize(
    ("path", "field"),
    [("/notifications", "dcd"), ("/devices/device1/errors", "errorType")],
)
def test_the_missing_field_allowance_is_limited_to_the_module_list(path, field):
    raw = example(path)
    del raw["values"][0][field]
    with pytest.raises(LocalResourceError):
        read(raw)


def test_the_basic_info_row_without_serial_number_is_read_by_the_resource_reader():
    from custom_components.bosch_buderus_heating.local_api.models import SystemModule

    observation = read(_basic_info(_BURNER_ROW))
    assert isinstance(observation.data, SystemInfo)
    assert all(isinstance(m, SystemModule) for m in observation.data.modules)
