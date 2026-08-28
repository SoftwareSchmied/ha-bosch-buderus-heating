"""Tolerant parsers with strict validation of known PointT fields."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from .bulk import normalize_resource_path
from .exceptions import (
    InvalidBatchEnvelope,
    InvalidPayload,
    ResourceError,
    ResourceForbidden,
    ResourceNotAcceptable,
    ResourceNotFound,
)
from .models import (
    BatchItemResult,
    Gateway,
    JsonScalar,
    JsonValue,
    Resource,
    ResourceMetadata,
    ResourceReference,
)


def _mapping(value: object, *, message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidPayload(message)
    return value


def _sequence(value: object, *, message: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise InvalidPayload(message)
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidPayload("Expected a string field")
    return value


def _required_string(value: object, *, message: str) -> str:
    result = _optional_string(value)
    if not result:
        raise InvalidPayload(message)
    return result


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidPayload("Expected a numeric field")
    result = float(value)
    if not math.isfinite(result):
        raise InvalidPayload("Expected a finite numeric field")
    return result


def _status(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _json_value(value: object) -> JsonValue:
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidPayload("Expected a finite JSON number")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {str(key): _json_value(item) for key, item in value.items()}
    raise InvalidPayload("Unsupported JSON value")


def _scalar(value: object) -> JsonScalar:
    parsed = _json_value(value)
    if isinstance(parsed, (list, dict)):
        raise InvalidPayload("Expected a scalar value")
    return parsed


def parse_gateway(payload: object) -> Gateway:
    """Parse one gateway while tolerating unknown fields."""
    data = _mapping(payload, message="Gateway response must be an object")
    gateway_id = _required_string(
        data.get(
            "deviceId",
            data.get("id", data.get("gatewayId", data.get("gateway_id"))),
        ),
        message="Gateway response did not contain an ID",
    )
    return Gateway(
        gateway_id=gateway_id,
        device_type=_optional_string(data.get("deviceType", data.get("device_type"))),
        model=_optional_string(data.get("model")),
        firmware_version=_optional_string(
            data.get("firmwareVersion", data.get("firmware_version"))
        ),
    )


def parse_gateways(payload: object) -> tuple[Gateway, ...]:
    """Parse common gateway list response variants."""
    items: Sequence[object]
    if isinstance(payload, Mapping) and "gateways" in payload:
        items = _sequence(
            payload["gateways"], message="Gateway list field must be an array"
        )
    elif isinstance(payload, Mapping):
        items = (payload,)
    else:
        items = _sequence(payload, message="Gateway list response must be an array")
    return tuple(parse_gateway(item) for item in items)


def parse_part_number(payload: object) -> str:
    """Parse a gateway part-number response."""
    data = _mapping(payload, message="Part-number response must be an object")
    return _required_string(
        data.get("partNumber", data.get("partnumber", data.get("value"))),
        message="Part-number response did not contain a value",
    )


def _parse_references(value: object) -> tuple[ResourceReference, ...]:
    if value is None:
        return ()
    references = _sequence(value, message="Resource references must be an array")
    parsed: list[ResourceReference] = []
    for item in references:
        if isinstance(item, str):
            parsed.append(ResourceReference(path=normalize_resource_path(item)))
            continue
        data = _mapping(item, message="Resource reference must be an object")
        path = _required_string(
            data.get("id", data.get("path")),
            message="Resource reference did not contain a path",
        )
        parsed.append(
            ResourceReference(
                path=normalize_resource_path(path),
                name=_optional_string(data.get("name")),
            )
        )
    return tuple(parsed)


def parse_resource(payload: object, *, path: str | None = None) -> Resource:
    """Parse one resource without rejecting unknown vendor fields."""
    data = _mapping(payload, message="Resource response must be an object")
    resource_path = path or _required_string(
        data.get("id"), message="Resource response did not contain an ID"
    )
    resource_path = normalize_resource_path(resource_path)

    allowed_raw = data.get("allowedValues")
    allowed_values: tuple[JsonScalar, ...] = ()
    if allowed_raw is not None:
        allowed_values = tuple(
            _scalar(item)
            for item in _sequence(allowed_raw, message="allowedValues must be an array")
        )

    values_raw = data.get("values")
    values: tuple[JsonValue, ...] = ()
    if values_raw is not None:
        # PointT normally returns an array here, but structured resources such
        # as systemPressureRange use one object directly. Normalize both wire
        # formats so downstream entity extraction stays shape-independent.
        if isinstance(values_raw, Mapping):
            values = (_json_value(values_raw),)
        else:
            values = tuple(
                _json_value(item)
                for item in _sequence(
                    values_raw, message="values must be an array or object"
                )
            )

    writable_raw = data.get("writeable", data.get("writable", False))
    if not isinstance(writable_raw, (bool, int)) or writable_raw not in (0, 1):
        raise InvalidPayload("writeable must be a boolean or 0/1")

    return Resource(
        path=resource_path,
        value=_json_value(data.get("value")),
        has_value="value" in data,
        values=values,
        metadata=ResourceMetadata(
            resource_type=_optional_string(data.get("type")),
            unit=_optional_string(data.get("unitOfMeasure")),
            allowed_values=allowed_values,
            minimum=_optional_number(data.get("minValue")),
            maximum=_optional_number(data.get("maxValue")),
            writable=bool(writable_raw),
        ),
        references=_parse_references(data.get("references")),
    )


def resource_error(path: str, status: int) -> ResourceError:
    """Map a per-resource HTTP status to a stable exception."""
    if status == 403:
        return ResourceForbidden(path, status)
    if status == 404:
        return ResourceNotFound(path, status)
    if status == 406:
        return ResourceNotAcceptable(path, status)
    return ResourceError(path, status)


def parse_batch_response(
    payload: object, *, gateway_id: str, requested_paths: Sequence[str]
) -> tuple[BatchItemResult, ...]:
    """Parse a partial-success PointT bulk response in request order."""
    envelopes = _sequence(payload, message="Bulk response must be an array")
    if not envelopes:
        raise InvalidBatchEnvelope("Bulk response was empty")

    selected: Mapping[str, object] | None = None
    for envelope in envelopes:
        candidate = _mapping(envelope, message="Bulk gateway item must be an object")
        if candidate.get("gatewayId") in (None, gateway_id):
            selected = candidate
            if candidate.get("gatewayId") == gateway_id:
                break
    if selected is None:
        raise InvalidBatchEnvelope(
            "Bulk response did not contain the requested gateway"
        )

    raw_items = _sequence(
        selected.get("resourcePaths"),
        message="Bulk gateway item did not contain resource paths",
    )
    indexed: dict[str, Mapping[str, object]] = {}
    for raw_item in raw_items:
        item = _mapping(raw_item, message="Bulk resource item must be an object")
        raw_path = item.get("resourcePath")
        if isinstance(raw_path, str):
            indexed[normalize_resource_path(raw_path)] = item

    results: list[BatchItemResult] = []
    for requested in requested_paths:
        path = normalize_resource_path(requested)
        indexed_item = indexed.get(path)
        if indexed_item is None:
            results.append(
                BatchItemResult(
                    gateway_id=gateway_id,
                    path=path,
                    status=None,
                    error=InvalidPayload("Bulk response omitted a requested path"),
                )
            )
            continue

        server_status = _status(indexed_item.get("serverStatus"))
        if server_status != 200:
            status = server_status or 0
            results.append(
                BatchItemResult(
                    gateway_id=gateway_id,
                    path=path,
                    status=server_status,
                    error=resource_error(path, status),
                    server_status=server_status,
                )
            )
            continue

        gateway_response = indexed_item.get("gatewayResponse")
        if not isinstance(gateway_response, Mapping):
            results.append(
                BatchItemResult(
                    gateway_id=gateway_id,
                    path=path,
                    status=server_status,
                    error=InvalidPayload(
                        "Bulk item did not contain a gateway response"
                    ),
                    server_status=server_status,
                )
            )
            continue
        gateway_status = _status(gateway_response.get("status"))
        if gateway_status != 200:
            status = gateway_status or 0
            results.append(
                BatchItemResult(
                    gateway_id=gateway_id,
                    path=path,
                    status=gateway_status,
                    error=resource_error(path, status),
                    server_status=server_status,
                    gateway_status=gateway_status,
                )
            )
            continue

        try:
            resource = parse_resource(gateway_response.get("payload"), path=path)
        except InvalidPayload as err:
            results.append(
                BatchItemResult(
                    gateway_id=gateway_id,
                    path=path,
                    status=gateway_status,
                    error=err,
                    server_status=server_status,
                    gateway_status=gateway_status,
                )
            )
        else:
            results.append(
                BatchItemResult(
                    gateway_id=gateway_id,
                    path=path,
                    status=gateway_status,
                    resource=resource,
                    server_status=server_status,
                    gateway_status=gateway_status,
                )
            )
    return tuple(results)
