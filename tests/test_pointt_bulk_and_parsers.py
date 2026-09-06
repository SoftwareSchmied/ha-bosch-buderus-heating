from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from custom_components.bosch_buderus_heating.pointt import (
    InvalidBatchEnvelope,
    InvalidPayload,
    ResourceNotFound,
)
from custom_components.bosch_buderus_heating.pointt.bulk import (
    chunk_resource_paths,
    normalize_resource_path,
)
from custom_components.bosch_buderus_heating.pointt.parsers import (
    parse_batch_response,
    parse_gateway,
    parse_gateways,
    parse_part_number,
    parse_resource,
    resource_error,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("heating/status", "/heating/status"), (" /resource/heat/temp ", "/heat/temp")],
)
def test_normalize_resource_path(raw: str, expected: str) -> None:
    assert normalize_resource_path(raw) == expected


@pytest.mark.parametrize("raw", ["/", "../secret", "/a/../b", "/a?x=1", "https://x"])
def test_normalize_resource_path_rejects_unsafe_values(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_resource_path(raw)


def test_chunk_resource_paths_obeys_limit() -> None:
    chunks = tuple(chunk_resource_paths(f"/r/{index}" for index in range(31)))
    assert tuple(map(len, chunks)) == (30, 1)
    with pytest.raises(ValueError):
        tuple(chunk_resource_paths(["/r"], size=31))


@given(
    st.integers(min_value=1, max_value=30), st.lists(st.text(min_size=1), max_size=80)
)
def test_chunk_size_never_exceeds_requested_bound(size: int, parts: list[str]) -> None:
    safe = [
        f"/resource/{index}/v{part.replace('/', '_')}"
        for index, part in enumerate(parts)
    ]
    safe = [path.replace("?", "_").replace("#", "_").replace(":", "_") for path in safe]
    chunks = tuple(chunk_resource_paths(safe, size=size))
    assert all(1 <= len(chunk) <= size for chunk in chunks)


@pytest.mark.parametrize("reference", ["../broken", {"id": "../broken"}])
def test_invalid_cloud_reference_is_a_protocol_error(reference):
    with pytest.raises(InvalidPayload, match="resource path"):
        parse_resource({"references": [reference]}, path="/heatingCircuits")
    with pytest.raises(InvalidPayload, match="resource path"):
        parse_resource({"id": "../broken"})


def test_invalid_bulk_paths_and_references_do_not_discard_healthy_items():
    payload = [
        {
            "gatewayId": "gateway",
            "resourcePaths": [
                {"resourcePath": "../broken", "serverStatus": 200},
                {
                    "resourcePath": "/heatingCircuits",
                    "serverStatus": 200,
                    "gatewayResponse": {
                        "status": 200,
                        "payload": {"references": ["../broken"]},
                    },
                },
                {
                    "resourcePath": "/system/healthStatus",
                    "serverStatus": 200,
                    "gatewayResponse": {"status": 200, "payload": {"value": "ok"}},
                },
            ],
        }
    ]
    results = parse_batch_response(
        payload,
        gateway_id="gateway",
        requested_paths=("/heatingCircuits", "/system/healthStatus", "/notifications"),
    )
    assert results[0].fallback_reason == "malformed"
    assert isinstance(results[0].error, InvalidPayload)
    assert results[1].resource.value == "ok"
    assert results[2].fallback_reason == "malformed"


def test_gateway_variants_and_part_number() -> None:
    gateway = parse_gateway(
        {"gatewayId": "gw-1", "deviceType": "heatpump", "unknown": "ignored"}
    )
    assert gateway.gateway_id == "gw-1"
    assert (
        parse_gateways({"gateways": [{"id": "one"}, {"gateway_id": "two"}]})[
            1
        ].gateway_id
        == "two"
    )
    assert parse_gateways({"id": "one"}) == (parse_gateway({"id": "one"}),)
    assert parse_part_number({"partnumber": "873820"}) == "873820"


def test_gateway_accepts_observed_device_id() -> None:
    gateway = parse_gateway({"deviceId": "pointt-gateway", "deviceType": "MX300"})

    assert gateway.gateway_id == "pointt-gateway"
    assert gateway.device_type == "MX300"


def test_resource_parser_preserves_supported_json_values() -> None:
    resource = parse_resource(
        {
            "id": "/heat/temp",
            "value": {"current": 21.5},
            "values": [1, "auto", {"nested": True}],
            "type": "floatValue",
            "unitOfMeasure": "C",
            "allowedValues": ["auto", 1],
            "minValue": 5,
            "maxValue": 30.5,
            "writeable": 1,
            "references": ["/heat/mode", {"id": "/heat/target", "name": "Target"}],
            "futureField": {"ignored": True},
        }
    )
    assert resource.path == "/heat/temp"
    assert resource.value == {"current": 21.5}
    assert resource.has_value
    assert resource.metadata.unit == "C"
    assert resource.metadata.writable
    assert resource.references[1].name == "Target"


def test_resource_parser_normalizes_structured_values_object() -> None:
    resource = parse_resource(
        {
            "id": "/heatSources/systemPressureRange",
            "type": "systemPressureValues",
            "unitOfMeasure": "bar",
            "values": {
                "highSystemPressure": 2.5,
                "lowPressureThreshold": 0.6,
            },
        }
    )

    assert resource.values == (
        {"highSystemPressure": 2.5, "lowPressureThreshold": 0.6},
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"id": "/x", "writeable": "yes"},
        {"id": "/x", "allowedValues": {}},
        {"id": "/x", "minValue": True},
        {"id": "/x", "references": [3]},
        {"id": "/x", "values": 3},
        {"id": "/x", "value": object()},
    ],
)
def test_resource_parser_rejects_malformed_known_fields(payload: object) -> None:
    with pytest.raises(InvalidPayload):
        parse_resource(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"id": ""},
        {"id": "/x", "unitOfMeasure": 3},
        {"id": "/x", "minValue": float("inf")},
        {"id": "/x", "value": [object()]},
        {"id": "/x", "allowedValues": [[1]]},
        {"id": "/x", "references": [{}]},
    ],
)
def test_resource_parser_rejects_additional_invalid_shapes(payload: object) -> None:
    with pytest.raises(InvalidPayload):
        parse_resource(payload)


def test_bulk_response_keeps_partial_success_in_request_order() -> None:
    payload = [
        {
            "gatewayId": "gw",
            "resourcePaths": [
                {
                    "resourcePath": "/missing",
                    "serverStatus": 404,
                    "gatewayResponse": {"status": 404},
                },
                {
                    "resourcePath": "/ok",
                    "serverStatus": "200",
                    "gatewayResponse": {"status": 200, "payload": {"value": 42}},
                },
                {
                    "resourcePath": "/bad",
                    "serverStatus": 200,
                    "gatewayResponse": {"status": 200, "payload": []},
                },
            ],
        }
    ]
    results = parse_batch_response(
        payload,
        gateway_id="gw",
        requested_paths=["/ok", "/missing", "/omitted", "/bad"],
    )

    assert [item.path for item in results] == ["/ok", "/missing", "/omitted", "/bad"]
    assert results[0].ok and results[0].resource is not None
    assert results[0].resource.value == 42
    assert isinstance(results[1].error, ResourceNotFound)
    assert isinstance(results[2].error, InvalidPayload)
    assert isinstance(results[3].error, InvalidPayload)


def test_bulk_response_accepts_observed_k30_reference_envelope() -> None:
    payload = [
        {
            "gatewayId": "k30-gateway",
            "resourcePaths": [
                {
                    "resourcePath": "/system",
                    "serverStatus": 200,
                    "gatewayResponse": {
                        "status": 200,
                        "payload": {
                            "id": "/system",
                            "type": "refEnum",
                            "references": [
                                {
                                    "id": "/system/brand",
                                    "uri": "http://k30/system/brand",
                                }
                            ],
                        },
                    },
                }
            ],
        }
    ]

    result = parse_batch_response(
        payload, gateway_id="k30-gateway", requested_paths=["/system"]
    )[0]

    assert result.ok
    assert result.resource is not None
    assert result.resource.metadata.resource_type == "refEnum"
    assert tuple(reference.path for reference in result.resource.references) == (
        "/system/brand",
    )


@pytest.mark.parametrize(
    "payload", [[], {}, [{"gatewayId": "other", "resourcePaths": []}]]
)
def test_bulk_response_rejects_invalid_envelopes(payload: object) -> None:
    with pytest.raises((InvalidBatchEnvelope, InvalidPayload)):
        parse_batch_response(payload, gateway_id="gw", requested_paths=["/x"])


def test_bulk_response_maps_server_and_gateway_failures() -> None:
    payload = [
        {
            "resourcePaths": [
                {"resourcePath": "/server", "serverStatus": True},
                {"resourcePath": "/missing-response", "serverStatus": 200},
                {
                    "resourcePath": "/gateway",
                    "serverStatus": 200,
                    "gatewayResponse": {"status": 406},
                },
                {"ignored": "without path"},
            ]
        }
    ]
    results = parse_batch_response(
        payload,
        gateway_id="gw",
        requested_paths=["/server", "/missing-response", "/gateway"],
    )
    assert all(not result.ok for result in results)
    assert results[0].status is None
    assert results[0].server_status is None
    assert results[0].gateway_status is None
    assert isinstance(results[1].error, InvalidPayload)
    assert results[1].server_status == 200
    assert results[1].gateway_status is None
    assert results[2].server_status == 200
    assert results[2].gateway_status == 406


@pytest.mark.parametrize("status", [403, 406, 409])
def test_resource_error_maps_known_and_generic_statuses(status: int) -> None:
    error = resource_error("/path", status)
    assert error.status == status
    assert error.path == "/path"


@pytest.mark.parametrize(
    "response_id", ["/heatingCircuits/hc2/operationMode", "../broken", None, 7]
)
def test_resource_rejects_supplied_id_that_cannot_confirm_requested_path(response_id):
    with pytest.raises(InvalidPayload):
        parse_resource(
            {"id": response_id, "value": "auto"},
            path="/heatingCircuits/hc1/operationMode",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"value": "auto"},
        {"id": "/resource/heatingCircuits/hc1/operationMode", "value": "auto"},
    ],
)
def test_resource_accepts_missing_id_or_equivalent_normalized_id(payload):
    resource = parse_resource(payload, path="/heatingCircuits/hc1/operationMode")
    assert resource.path == "/heatingCircuits/hc1/operationMode"


def test_bulk_mismatched_inner_id_is_local_to_its_requested_path():
    from custom_components.bosch_buderus_heating.pointt.parsers import (
        parse_batch_response,
    )

    one = "/heatingCircuits/hc1/operationMode"
    two = "/heatingCircuits/hc2/operationMode"
    payload = [
        {
            "gatewayId": "synthetic",
            "resourcePaths": [
                {
                    "resourcePath": path,
                    "serverStatus": 200,
                    "gatewayResponse": {
                        "status": 200,
                        "payload": {"id": two, "value": "auto"},
                    },
                }
                for path in (one, two)
            ],
        }
    ]
    result = parse_batch_response(
        payload, gateway_id="synthetic", requested_paths=[one, two]
    )
    assert not result[0].ok and isinstance(result[0].error, InvalidPayload)
    assert result[1].ok and result[1].resource.path == two
