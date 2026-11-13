"""Decode complete local response forms without guessing device or cloud links."""

from __future__ import annotations

import base64
import binascii
import math
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, cast
from urllib.parse import parse_qsl

from ..gateway_identity import GatewayNamespace, IdentityContext
from .catalog import ReadRequest, response_schema, validate_path
from .compatibility import response_for_validation
from .errors import LocalResourceError, Problem
from .json import DEFAULT_LIMITS, JsonLimits, load_json
from .models import (
    AccessPoint,
    AccessPointList,
    ConfigurationValue,
    DataIssue,
    DeviceFault,
    DeviceFaultList,
    DevicePattern,
    Fault,
    FaultList,
    InclusionEntry,
    InclusionWhitelist,
    JsonObject,
    JsonValue,
    LocalObservation,
    NamedNumber,
    NamedValues,
    Recording,
    RecordingGroup,
    RecordingSample,
    ReferenceList,
    ResourceData,
    ResourceReference,
    ScalarValue,
    SystemInfo,
    SystemModule,
    UpdateModule,
    UpdateReport,
    UpdateStatus,
    ValueArray,
)
from .schema import is_number, iso_datetime, validate

# Preserve the reported unit; normalization/conversion belongs to later adapters.
_UNITS = frozenset(
    {
        "",
        "%",
        "1/min",
        "bar",
        "C",
        "°C",
        "K",
        "count",
        "db",
        "dB",
        "kW",
        "kWh",
        "l/h",
        "l/min",
        # Pressure spellings of generic signals (for example a pressure
        # difference); only recognized, never converted.
        "mbar",
        "hPa",
        "kPa",
        "Pa",
        "min",
        "mins",
        "h",
        "ppm",
        "rpm",
        "s",
        "W",
        "Wh",
        "wh",
    }
)


def _freeze(value: Any) -> JsonValue:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return cast(JsonValue, value)


def _object(value: dict[str, Any]) -> JsonObject:
    return cast(JsonObject, _freeze(value))


def _identifier(value: str | int | Decimal) -> str | int:
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise LocalResourceError(Problem.INVALID_METADATA)
        return int(value)
    return value


def _unit_issues(unit: str | None, *, required: bool = False) -> tuple[DataIssue, ...]:
    if unit is None or unit == "":
        return (DataIssue.MISSING_UNIT,) if required else ()
    return () if unit in _UNITS else (DataIssue.UNKNOWN_UNIT,)


def _reference(raw: dict[str, Any]) -> ResourceReference:
    validate_path(raw["id"])
    return ResourceReference(raw["id"], raw["uri"])


def _named(rows: list[dict[str, Any]], *, single: bool) -> tuple[NamedNumber, ...]:
    result: dict[str, NamedNumber] = {}
    for row in rows:
        if not row or (single and len(row) != 1):
            raise LocalResourceError(Problem.INVALID_METADATA)
        for name, value in row.items():
            if name in result or not is_number(value):
                raise LocalResourceError(Problem.INVALID_METADATA)
            result[name] = NamedNumber(name, value)
    return tuple(result.values())


def _decoded_text(raw: dict[str, Any]) -> tuple[str | None, tuple[DataIssue, ...]]:
    config = raw.get("valueConfig")
    if not isinstance(config, dict):
        return None, ()
    if "maxLength" in config and (
        type(config["maxLength"]) is not int or config["maxLength"] < 0
    ):
        raise LocalResourceError(Problem.INVALID_METADATA)
    charset, coding = config.get("charset"), config.get("codingType")
    if not isinstance(charset, str) or not isinstance(coding, str):
        return None, (DataIssue.UNSUPPORTED_TEXT_ENCODING,)
    charset = charset.replace("-", "").upper()
    if coding.lower() == "ascii" and charset in {"UTF8", "ASCII"}:
        return raw["value"], ()
    if coding.lower() != "base64" or charset not in {
        "UTF16",
        "UTF8",
        "ASCII",
    }:
        return None, (DataIssue.UNSUPPORTED_TEXT_ENCODING,)
    try:
        data = base64.b64decode(raw["value"], validate=True)
        encoding = {"UTF16": "utf-16-be", "UTF8": "utf-8", "ASCII": "ascii"}[charset]
        if charset == "UTF16" and data.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        return data.decode(encoding), ()
    except ValueError, UnicodeError, binascii.Error:
        raise LocalResourceError(Problem.INVALID_METADATA) from None


def _scalar(raw: dict[str, Any]) -> ScalarValue:
    numeric = raw["type"] in {"floatValue", "integerValue"}
    minimum, maximum, step = (
        raw.get(key) if numeric else None
        for key in ("minValue", "maxValue", "stepSize")
    )
    if (minimum is not None and maximum is not None and minimum > maximum) or (
        step is not None and step <= 0
    ):
        raise LocalResourceError(Problem.INVALID_METADATA)
    state = raw.get("state", []) if numeric else []
    states = _named([state] if isinstance(state, dict) else state, single=False)
    decoded, issues = _decoded_text(raw) if raw["type"] == "stringValue" else (None, ())
    value = raw["value"]
    unit = raw.get("unitOfMeasure") if numeric else None
    allowed = tuple(raw.get("allowedValues", ())) if not numeric else ()
    return ScalarValue(
        value,
        unit,
        allowed,
        minimum,
        maximum,
        step,
        states,
        tuple(
            state.name for state in states if is_number(value) and state.value == value
        ),
        decoded,
        issues + _unit_issues(unit),
    )


def _recording(raw: dict[str, Any]) -> Recording:
    groups = []
    for row in raw["values"]:
        start, end = iso_datetime(row["startTime"]), iso_datetime(row["endTime"])
        if end < start or any(sample["c"] < 0 for sample in row["recording"]):
            raise LocalResourceError(Problem.INVALID_METADATA)
        groups.append(
            RecordingGroup(
                row["sampleRate"],
                start,
                end,
                tuple(
                    RecordingSample(sample["y"], sample["c"])
                    for sample in row["recording"]
                ),
            )
        )
    return Recording(
        _reference(raw["recordedResource"]),
        raw["recordingType"],
        raw.get("unitOfMeasure"),
        tuple(groups),
        _unit_issues(raw.get("unitOfMeasure"), required=True),
    )


def _decode(raw: dict[str, Any]) -> ResourceData:
    match raw["type"]:
        case "floatValue" | "integerValue" | "stringValue":
            return _scalar(raw)
        case "arrayData":
            return ValueArray(tuple(raw["values"]), tuple(raw.get("allowedValues", ())))
        case "emonValue":
            return NamedValues(
                _named(raw["values"], single=True),
                raw.get("unit"),
                _unit_issues(raw.get("unit"), required=True),
            )
        case "refEnum":
            refs = tuple(_reference(row) for row in raw["references"])
            if len({ref.id for ref in refs}) != len(refs):
                raise LocalResourceError(Problem.INVALID_METADATA)
            return ReferenceList(refs)
        case "configData":
            return ConfigurationValue(_object(raw["values"]))
        case "systeminfo":
            modules = tuple(
                SystemModule(
                    row["Tok"],
                    row["Id"],
                    row["Ver"],
                    _identifier(row["ModuleSerialNumber"]),
                    row["ModuleHwIdentStr"],
                    row.get("ProductSerialNumber"),
                    row.get("ProductName"),
                )
                for row in raw["values"]
            )
            return SystemInfo(
                raw["status"], raw["timeStamp"], raw["gatewayId"], modules
            )
        case "errorList":
            return FaultList(
                tuple(
                    Fault(row["dcd"], row["ccd"], row["orig"], row["dlv"], row["fc"])
                    for row in raw["values"]
                )
            )
        case "deviceErrors":
            return DeviceFaultList(
                tuple(
                    DeviceFault(
                        row["errorType"],
                        row["occurrenceId"],
                        row["value"],
                        tuple(row["allowedValues"]),
                    )
                    for row in raw["values"]
                )
            )
        case "apList":
            return AccessPointList(
                tuple(
                    AccessPoint(row["ssid"], row["strength"], row["secType"])
                    for row in raw["values"]
                )
            )
        case "inclusionWhitelist":
            return InclusionWhitelist(
                tuple(
                    InclusionEntry(
                        row["assignedHC"],
                        row["actualDevices"],
                        row["maxSupportedDevices"],
                        tuple(
                            DevicePattern(pattern["type"], pattern["pattern"])
                            for pattern in row["sgtinPattern"]
                        ),
                    )
                    for row in raw["value"]
                )
            )
        case "updateStatus":
            return UpdateStatus(_object(raw["value"]))
        case "updateReport":
            return UpdateReport(
                raw["timeStamp"],
                raw["recipeId"],
                raw["status"],
                raw["gatewayId"],
                tuple(
                    UpdateModule(
                        _identifier(
                            row["moduleId"] if "moduleId" in row else row["module_id"]
                        ),
                        row["status"],
                        row["timeStamp"],
                        _object(row),
                    )
                    for row in raw["values"]
                ),
                tuple(_object(row) for row in raw.get("logs", ())),
            )
        case "yRecordingExtended":
            return _recording(raw)
    raise LocalResourceError(Problem.INVALID_SCHEMA)


def decode_resource(
    request: ReadRequest,
    body: bytes,
    *,
    context: IdentityContext,
    observed_at: datetime,
    observed_monotonic: float,
    limits: JsonLimits = DEFAULT_LIMITS,
) -> LocalObservation:
    """Decode one successful response. HTTP outcomes belong to the transport."""
    if (
        not isinstance(context, IdentityContext)
        or context.namespace != GatewayNamespace.K40_LOCAL
        or not isinstance(observed_at, datetime)
        or observed_at.utcoffset() is None
        or type(observed_monotonic) not in {int, float}
        or not math.isfinite(observed_monotonic)
        or observed_monotonic < 0
    ):
        raise LocalResourceError(Problem.INVALID_CONTEXT)
    schema = response_schema(request)
    payload = load_json(body, limits=limits)
    if not isinstance(payload, dict):
        raise LocalResourceError(Problem.INVALID_SCHEMA)
    if not _response_id_matches(payload.get("id"), request):
        raise LocalResourceError(Problem.WRONG_RESOURCE)
    validate(response_for_validation(payload), schema)
    data = _decode(payload)
    return LocalObservation(
        request,
        context,
        observed_at,
        observed_monotonic,
        payload["type"],
        bool(payload["writeable"]),
        bool(payload["recordable"]) if "recordable" in payload else None,
        data,
        _object(payload),
    )


def _response_id_matches(value: object, request: ReadRequest) -> bool:
    if value == request.path:
        return True
    if (
        not isinstance(value, str)
        or "yRecordingExtended" not in request.definition.response_schemas
    ):
        return False
    path, separator, query = value.partition("?")
    if not separator or path != request.path:
        return False
    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            errors="strict",
            max_num_fields=3,
        )
    except ValueError, UnicodeError:
        return False
    return (
        len({key for key, _ in pairs}) == len(pairs)
        and tuple(sorted(pairs)) == request.query
    )
