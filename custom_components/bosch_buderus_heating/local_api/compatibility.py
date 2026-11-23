"""Narrow response variants observed beside the pinned manufacturer schema."""

import re
from decimal import Decimal
from functools import cache
from typing import Any

from ._data import SCHEMAS
from .errors import LocalResourceError, Problem

# Row lists whose decoder reads fixed keys only. A later firmware may add a
# field to such a row; the pinned schema forbids that and would reject the
# whole document (for /signals: every signal, for /system/basicInfo: the
# setup). The map names the response type, its row list and the schema part
# that owns the row properties.
_TOLERANT_ROWS: dict[str, tuple[str, str]] = {
    "refEnum": ("references", "Reference"),
    "systeminfo": ("values", "SystemBasicInfo"),
    "errorList": ("values", "ErrorList"),
    "deviceErrors": ("values", "DeviceErrors"),
}

# Descriptive row fields that the real firmware leaves out (a K40RF on
# 15.00.01 reports a burner module without ModuleSerialNumber) or reports as
# empty strings. A null or another type already counts as a missing key
# (received_document); the decoder reads these fields with get() and
# nothing is invented for them.
_OPTIONAL_ROW_FIELDS: dict[str, tuple[str, ...]] = {
    "systeminfo": ("ModuleSerialNumber", "ModuleHwIdentStr", "Ver"),
}


# The text fields of a module row of /system/basicInfo. The place and type
# (Tok, Id) identify a row; a row without them is left out.
_SYSTEM_KEYS = ("Tok", "Id")
_SYSTEM_TEXTS = ("Ver", "ProductSerialNumber", "ModuleHwIdentStr", "ProductName")
_ROW_FIELDS = frozenset((*_SYSTEM_KEYS, *_SYSTEM_TEXTS, "ModuleSerialNumber"))
_TIMESTAMP = re.compile(r"(?:[0-9]{10})?")


def _whole(value: object) -> int | None:
    """A whole number as the JSON text wrote it (also with an exponent)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return None


def _system_text(value: object) -> str | None:
    """A text field; a whole number stands for its digits, anything else
    (null, a fraction, a list) for a missing field."""
    if isinstance(value, str):
        return value
    number = _whole(value)
    return None if number is None else str(number)


def _system_serial(value: object) -> str | int | None:
    """A module serial number as text or whole number; a number with a
    fraction, null or another type counts as missing."""
    return value if isinstance(value, str) else _whole(value)


def _system_row(row: object) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    result: dict[str, Any] = {}
    for key in _SYSTEM_KEYS:
        if (text := _system_text(row.get(key))) is None:
            return None
        result[key] = text
    for key in _SYSTEM_TEXTS:
        if (text := _system_text(row.get(key))) is not None:
            result[key] = text
    if (serial := _system_serial(row.get("ModuleSerialNumber"))) is not None:
        result["ModuleSerialNumber"] = serial
    # Further fields stay for the validation copy, which leaves them out.
    return {**{k: v for k, v in row.items() if k not in _ROW_FIELDS}, **result}


def received_document(payload: dict[str, Any]) -> dict[str, Any]:
    """The document as decoded: a tolerant reading of /system/basicInfo.

    Only the gateway ID and the module list identify the gateway. The
    timestamp (not used) is read as empty when its form is another, the
    ``writeable`` and ``recordable`` flags as their meaning, the status
    without case and spaces. In the module list a row without place or type
    is left out, null or another type in a field counts as a missing field
    and a whole number in a text field as its digits. Every other response
    is returned unchanged.
    """
    if payload.get("type") != "systeminfo" or not isinstance(
        payload.get("values"), list
    ):
        return payload
    received = dict(payload)
    for flag in ("writeable", "recordable"):
        if flag in payload or flag == "writeable":
            value = payload.get(flag)
            received[flag] = 1 if value is True or (type(value) is int and value) else 0
    timestamp = payload.get("timeStamp")
    if not isinstance(timestamp, str) or _TIMESTAMP.fullmatch(timestamp) is None:
        received["timeStamp"] = ""
    status = payload.get("status")
    received["status"] = status.strip().lower() if isinstance(status, str) else ""
    received["values"] = [
        row for row in map(_system_row, payload["values"]) if row is not None
    ]
    return received


@cache
def _row_keys(schema_name: str) -> frozenset[str]:
    """Return the documented row properties of one pinned schema."""
    schema = SCHEMAS[schema_name]
    if "properties" in schema:
        return frozenset(schema["properties"])
    part = next(item for item in schema["allOf"] if "properties" in item)
    rows = next(
        value for value in part["properties"].values() if value.get("type") == "array"
    )
    return frozenset(rows["items"]["properties"])


def _without_extra_row_keys(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop undocumented row keys; required keys and types stay strict.

    The few named descriptive fields of _OPTIONAL_ROW_FIELDS may be absent;
    the validation copy then carries the empty form this firmware reports.
    """
    field, schema_name = _TOLERANT_ROWS[payload["type"]]
    rows = payload.get(field)
    if not isinstance(rows, list):
        return payload
    known = _row_keys(schema_name)
    optional = _OPTIONAL_ROW_FIELDS.get(payload["type"], ())
    trimmed = [
        {
            **{key: "" for key in optional if key not in row},
            **{key: value for key, value in row.items() if key in known},
        }
        if isinstance(row, dict)
        else row
        for row in rows
    ]
    return {**payload, field: trimmed}


_RECORDING_RATES = frozenset({"PT1H", "P1D", "P1M"})


def _recording_for_validation(payload: dict[str, Any]) -> dict[str, Any]:
    """A recording is judged slot by slot when it is read: the validation
    copy checks its envelope and group times only. A slot that is no number
    pair, a recording type other than ``actual`` and a sample rate of another
    name (read as an unknown rate) do not reject the whole recording."""
    copy = dict(payload)
    if isinstance(copy.get("recordingType"), str):
        copy["recordingType"] = "actual"
    groups = copy.get("values")
    if not isinstance(groups, list):
        return copy
    checked = []
    for group in groups:
        if isinstance(group, dict):
            group = dict(group)
            rate = group.get("sampleRate")
            if isinstance(rate, str) and rate not in _RECORDING_RATES:
                group["sampleRate"] = "PT1H"
            if isinstance(group.get("recording"), list):
                group["recording"] = [{"y": 0, "c": 0} for _ in group["recording"]]
        checked.append(group)
    copy["values"] = checked
    return copy


def response_for_validation(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a schema-shaped copy without changing the received document.

    Numeric state maps occur both directly and inside the documented list.
    Update reports may spell moduleId as module_id. Rows of reference lists,
    system information and fault lists may carry fields a later firmware adds;
    those fields are left out of the validation copy only. A module row may
    lack its serial number, hardware name or version; the validation copy then
    holds an empty string and the decoder reports the value as missing. No adaptation
    relaxes the remaining schema, invents an identifier or authorizes a write.
    """
    if isinstance(payload.get("type"), str) and payload["type"] in _TOLERANT_ROWS:
        return _without_extra_row_keys(payload)
    if payload.get("type") == "yRecordingExtended":
        return _recording_for_validation(payload)
    if payload.get("type") in ("floatValue", "integerValue") and isinstance(
        payload.get("state"), dict
    ):
        return {**payload, "state": [payload["state"]]}
    if payload.get("type") == "updateReport" and isinstance(
        payload.get("values"), list
    ):
        rows = []
        for row in payload["values"]:
            if (
                isinstance(row, dict)
                and row.get("moduleId", row.get("module_id")) == ""
            ):
                raise LocalResourceError(Problem.INVALID_SCHEMA)
            if isinstance(row, dict) and "module_id" in row:
                alias = row["module_id"]
                if "moduleId" in row and (
                    type(alias) is not type(row["moduleId"]) or alias != row["moduleId"]
                ):
                    raise LocalResourceError(Problem.INVALID_SCHEMA)
                row = {**row, "moduleId": alias}
            rows.append(row)
        return {**payload, "values": rows}
    return payload
