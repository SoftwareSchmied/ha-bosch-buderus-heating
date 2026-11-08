"""Local setup evidence from synthetic responses, without gateway access."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from custom_components.bosch_buderus_heating.gateway_identity import (
    GatewayNamespace,
    GatewayReference,
    IdentityContext,
    IdentityMatchStatus,
    match_gateway_identity,
)
from custom_components.bosch_buderus_heating.local_probe import (
    MAX_BODY_BYTES,
    MAX_DISCOVERY_ADDRESSES,
    MAX_JSON_VALUES,
    SERVICE_TYPE,
    FirmwareVersion,
    GatewayBrand,
    LocalEndpoint,
    LocalProbeError,
    ProbeContext,
    ProbePath,
    ProbeProblem,
    ProbeResponse,
    ProbeStatus,
    TransportFailure,
    assess_local_probe,
    normalize_local_address,
    parse_discovery_candidate,
    parse_firmware_version,
)
from custom_components.bosch_buderus_heating.plant_model import PlantKey

IDENTIFIER = "0009007199254740993"
HOST = "gateway-example.local"
CONTEXT = ProbeContext(
    IdentityContext(GatewayNamespace.K40_LOCAL, "owner-example", "local-example", 2),
    LocalEndpoint(HOST),
)


def response(path, *, context=CONTEXT, status=200, **changes):
    if path == ProbePath.SYSTEM:
        payload = {
            "id": path.value,
            "type": "systeminfo",
            "writeable": 0,
            "status": "completed",
            "timeStamp": "",
            "gatewayId": IDENTIFIER,
            "values": [],
        }
    else:
        payload = {
            "id": path.value,
            "type": "stringValue",
            "writeable": 0,
            "value": "Buderus" if path == ProbePath.BRAND else "15.00.01",
        }
    payload.update(changes)
    return ProbeResponse(path, context, status, json.dumps(payload).encode())


def observations():
    return [response(path) for path in ProbePath]


def assess_changed(changed):
    return assess_local_probe(
        [changed if path == changed.path else response(path) for path in ProbePath],
        current_context=CONTEXT,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" 192.0.2.7 ", "192.0.2.7"),
        ("Gateway-Example.LOCAL.", HOST),
        ("gateway", "gateway"),
        ("a" * 63 + ".local", "a" * 63 + ".local"),
        ("2001:DB8:0:0::7", "2001:db8::7"),
        ("[2001:DB8::7]", "2001:db8::7"),
        ("[fe80::7%eth0]", "fe80::7%eth0"),
        ("fe80:0:0:0::7%2", "fe80::7%2"),
        ("169.254.2.7", "169.254.2.7"),
        ("::ffff:192.0.2.7", "::ffff:192.0.2.7"),
    ],
)
def test_manual_endpoints_normalize_without_changing_fixed_port_roles(raw, expected):
    endpoint = LocalEndpoint(raw)
    assert endpoint.host == expected
    assert endpoint.auth_port == 9442 and endpoint.resource_port == 9443
    assert expected not in repr(endpoint)
    with pytest.raises(FrozenInstanceError):
        endpoint.host = "different.local"


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        123,
        b"gateway",
        "",
        " ",
        "a" * 321,
        "https://gateway.local",
        "gateway.local:9443",
        "user:pass@gateway.local",
        "gateway/path",
        "gateway?token=private",
        "gateway#fragment",
        "gateway\\path",
        "gateway\r\n",
        "\tgateway",
        "gate way",
        "gateway\u200b.local",
        "gäteway.local",
        "gateway..",
        ".gateway",
        "-gateway.local",
        "gateway-.local",
        "a" * 64 + ".local",
        "[gateway.local]",
        "[2001:db8::7]:9443",
        "[2001:db8::7",
        "0.0.0.0",
        "127.0.0.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "ff02::1%eth0",
        "::ffff:127.0.0.1",
        "localhost",
        "service.localhost.",
        "2130706433",
        "0x7f000001",
        "127.1",
        "0177.0.0.1",
        "0x7f.0.0.1",
        "999.1.2.3",
        "192.0.2.7%eth0",
        "2001:db8::7%eth0",
        "fe80::7%",
        "fe80::7%0",
        "fe80::7%eth0%extra",
        "fe80::7%eth/0",
        "fe80::7%" + "a" * 65,
        "_hvac-open-api._tcp.local.",
    ],
)
def test_unsafe_or_ambiguous_host_fields_have_static_errors(value):
    with pytest.raises(LocalProbeError) as error:
        normalize_local_address(value)
    assert str(error.value) == "invalid_address"
    assert repr(value) not in str(error.value)


def test_link_local_ipv6_requires_an_explicit_ha_interface():
    with pytest.raises(LocalProbeError) as error:
        normalize_local_address("fe80::7")
    assert error.value.problem == ProbeProblem.SCOPE_REQUIRED


def test_native_discovery_and_manual_input_produce_the_same_endpoint():
    candidate = parse_discovery_candidate(
        service_type=SERVICE_TYPE.upper(),
        hostname="Gateway-Example.LOCAL.",
        addresses=["192.0.2.7", "192.0.2.7", "fe80::7", "2001:db8::7"],
        port=9443,
    )
    assert candidate.endpoints == (
        LocalEndpoint("192.0.2.7"),
        LocalEndpoint("2001:db8::7"),
        LocalEndpoint(HOST),
    )
    assert candidate.rejected_addresses == 1
    assert "192.0.2.7" not in repr(candidate)
    assert HOST not in repr(candidate)


def test_bad_discovery_hostname_does_not_hide_a_usable_ip_address():
    candidate = parse_discovery_candidate(
        service_type=SERVICE_TYPE,
        hostname="invalid service name",
        addresses=["192.0.2.7"],
        port=9442,
    )
    assert candidate.endpoints == (LocalEndpoint("192.0.2.7"),)
    assert candidate.endpoints[0].resource_port == 9443
    assert candidate.rejected_addresses == 1


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"service_type": "_unrelated._tcp.local."}, ProbeProblem.INVALID_DISCOVERY),
        ({"service_type": None}, ProbeProblem.INVALID_DISCOVERY),
        ({"addresses": "192.0.2.7"}, ProbeProblem.INVALID_DISCOVERY),
        (
            {"addresses": ["192.0.2.7"] * (MAX_DISCOVERY_ADDRESSES + 1)},
            ProbeProblem.INVALID_DISCOVERY,
        ),
        ({"port": True}, ProbeProblem.DISCOVERY_PORT),
        ({"port": "9443"}, ProbeProblem.DISCOVERY_PORT),
        ({"port": 443}, ProbeProblem.DISCOVERY_PORT),
        ({"port": 65536}, ProbeProblem.DISCOVERY_PORT),
        ({"addresses": [], "hostname": None}, ProbeProblem.NO_ADDRESS),
        ({"addresses": ["fe80::7"], "hostname": "bad name"}, ProbeProblem.NO_ADDRESS),
    ],
)
def test_discovery_hint_bounds_and_failures(changes, problem):
    values = dict(service_type=SERVICE_TYPE, hostname=HOST, addresses=[], port=9443)
    values.update(changes)
    with pytest.raises(LocalProbeError) as error:
        parse_discovery_candidate(**values)
    assert error.value.problem == problem


@pytest.mark.parametrize("value", ["15.00.01", "15.0.1", "015.000.001"])
def test_equivalent_numeric_firmware_versions(value):
    assert parse_firmware_version(value) == FirmwareVersion(15, 0, 1)


@pytest.mark.parametrize(
    "value",
    [
        None,
        15,
        "",
        "15.1",
        "v15.00.01",
        "15.00.01-beta",
        "15.00.01 ",
        "15.00.01.2",
        "-15.00.01",
        "\uff11\uff15.\uff10\uff10.\uff10\uff11",
        "9999999.1.1",
    ],
)
def test_unknown_firmware_syntax_is_not_guessed(value):
    result = assess_changed(response(ProbePath.FIRMWARE, value=value))
    assert result.status == ProbeStatus.FAILED
    assert result.identity is None
    # Non-string resource values fail the resource envelope first.
    expected = (
        ProbeProblem.UNKNOWN_FIRMWARE
        if isinstance(value, str)
        else ProbeProblem.INVALID_RESPONSE
    )
    assert result.issues[0].problem == expected


@pytest.mark.parametrize("value", ["01.00.00", "14.99.99", "15.00.00", "9.99.99"])
def test_old_firmware_is_not_confused_with_authentication_failure(value):
    result = assess_changed(response(ProbePath.FIRMWARE, value=value))
    assert result.issues[0].problem == ProbeProblem.OLD_FIRMWARE
    assert result.firmware == parse_firmware_version(value)
    assert result.identity is None


@pytest.mark.parametrize("value", ["15.0.2", "15.10.1", "16.0.0", "100.0.0"])
def test_newer_numeric_versions_pass_the_minimum_check(value):
    assert (
        assess_changed(response(ProbePath.FIRMWARE, value=value)).status
        == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    )


@pytest.mark.parametrize("brand", list(GatewayBrand))
def test_all_documented_brands_use_the_same_probe(brand):
    result = assess_changed(response(ProbePath.BRAND, value=brand.value.upper()))
    assert result.brand == brand
    assert result.status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON


def test_unknown_brand_is_not_retained_in_public_result():
    result = assess_changed(response(ProbePath.BRAND, value="private product name"))
    assert result.issues[0].problem == ProbeProblem.UNKNOWN_BRAND
    assert result.brand is None and result.identity is None
    assert "private product name" not in repr(result)


def test_successful_probe_still_needs_the_source_contract_and_target_comparison():
    result = assess_local_probe(list(reversed(observations())), current_context=CONTEXT)
    assert result.status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    assert result.reported_module_count == 0
    assert result.identity.value == IDENTIFIER
    assert result.identity.context == CONTEXT.identity
    assert IDENTIFIER not in repr(result)
    plant = PlantKey("owner-example", "existing-installation")
    reference = GatewayReference(plant, GatewayNamespace.POINTT, IDENTIFIER)
    args = dict(
        current_context=CONTEXT.identity, expected_plant=plant, references=[reference]
    )
    assert (
        match_gateway_identity(result.identity, **args).status
        == IdentityMatchStatus.UNVERIFIED
    )
    args["verified_namespace_pairs"] = [
        (GatewayNamespace.POINTT, GatewayNamespace.K40_LOCAL)
    ]
    assert match_gateway_identity(result.identity, **args).plant == plant
    args["references"] = [replace(reference, value="different-gateway")]
    assert (
        match_gateway_identity(result.identity, **args).status
        == IdentityMatchStatus.CONFLICT
    )


@pytest.mark.parametrize(
    "field", ["owner_id", "connection_id", "generation", "endpoint"]
)
def test_changed_request_provenance_rejects_every_old_response(field):
    if field == "endpoint":
        new = replace(CONTEXT, endpoint=LocalEndpoint("different.local"))
    else:
        new = replace(
            CONTEXT,
            identity=replace(
                CONTEXT.identity, **{field: 3 if field == "generation" else "different"}
            ),
        )
    result = assess_local_probe(observations(), current_context=new)
    assert result.status == ProbeStatus.FAILED
    assert result.issues[0].problem == ProbeProblem.STALE_OBSERVATION
    assert result.identity is None and result.brand is None


def test_a_single_response_from_a_different_generation_blocks_the_whole_probe():
    data = observations()
    data[-1] = replace(
        data[-1],
        context=replace(CONTEXT, identity=replace(CONTEXT.identity, generation=1)),
    )
    assert (
        assess_local_probe(data, current_context=CONTEXT).issues[0].problem
        == ProbeProblem.STALE_OBSERVATION
    )


def test_cloud_context_cannot_be_used_as_local_provenance():
    with pytest.raises(LocalProbeError) as error:
        replace(
            CONTEXT,
            identity=replace(CONTEXT.identity, namespace=GatewayNamespace.POINTT),
        )
    assert error.value.problem == ProbeProblem.WRONG_SOURCE


@pytest.mark.parametrize("count", [0, 1, 2])
def test_partial_response_sets_remain_incomplete(count):
    result = assess_local_probe(observations()[:count], current_context=CONTEXT)
    assert result.status == ProbeStatus.INCOMPLETE
    assert len(result.issues) == 3 - count
    assert result.identity is None


def test_explicit_failure_takes_precedence_over_missing_responses():
    result = assess_local_probe(
        [response(ProbePath.BRAND, status=401)], current_context=CONTEXT
    )
    assert result.status == ProbeStatus.FAILED
    assert result.issues[0].problem == ProbeProblem.AUTH_REJECTED


def test_duplicate_responses_do_not_select_the_last_gateway_identity():
    data = [*observations(), response(ProbePath.SYSTEM, gatewayId="different")]
    result = assess_local_probe(data, current_context=CONTEXT)
    assert result.issues[0].problem == ProbeProblem.DUPLICATE_RESPONSE
    assert result.identity is None


@pytest.mark.parametrize(
    ("status", "problem"),
    [
        (401, ProbeProblem.AUTH_REJECTED),
        (403, ProbeProblem.FORBIDDEN),
        (404, ProbeProblem.NOT_FOUND),
        (429, ProbeProblem.RATE_LIMITED),
        (500, ProbeProblem.SERVICE_UNAVAILABLE),
        (503, ProbeProblem.SERVICE_UNAVAILABLE),
        (301, ProbeProblem.REDIRECT),
        (307, ProbeProblem.REDIRECT),
        (400, ProbeProblem.HTTP_ERROR),
        (204, ProbeProblem.HTTP_ERROR),
        (True, ProbeProblem.INVALID_RESPONSE),
        (None, ProbeProblem.INVALID_RESPONSE),
        ("200", ProbeProblem.INVALID_RESPONSE),
        (600, ProbeProblem.INVALID_RESPONSE),
    ],
)
def test_http_failures_never_become_successful_identity_evidence(status, problem):
    result = assess_changed(response(ProbePath.SYSTEM, status=status))
    assert result.issues[0].problem == problem
    assert result.identity is None


@pytest.mark.parametrize("failure", list(TransportFailure))
def test_transport_failure_classification_uses_no_exception_text(failure):
    item = ProbeResponse(ProbePath.BRAND, CONTEXT, failure=failure)
    result = assess_changed(item)
    assert result.issues[0].problem.value == failure.value
    assert result.identity is None


@pytest.mark.parametrize(
    "changes",
    [{"status": 200}, {"body": b"private data"}, {"failure": "raw exception text"}],
)
def test_inconsistent_transport_outcomes_are_rejected(changes):
    item = replace(
        ProbeResponse(ProbePath.BRAND, CONTEXT, failure=TransportFailure.TIMEOUT),
        **changes,
    )
    result = assess_changed(item)
    assert result.issues[0].problem == ProbeProblem.INVALID_RESPONSE
    assert "private data" not in repr(result) and "raw exception text" not in repr(
        result
    )


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (b"", ProbeProblem.INVALID_JSON),
        (b"not json", ProbeProblem.INVALID_JSON),
        (b"\xff", ProbeProblem.INVALID_JSON),
        (b'{"id":"/gateway/brand","id":"other"}', ProbeProblem.INVALID_JSON),
        (b'{"unused":{"a":1,"a":2}}', ProbeProblem.INVALID_JSON),
        (b'{"value":NaN}', ProbeProblem.INVALID_JSON),
        (b'{"value":Infinity}', ProbeProblem.INVALID_JSON),
        (b'{"value":1e999}', ProbeProblem.INVALID_JSON),
        pytest.param(
            b"[" * 2000 + b"]" * 2000, ProbeProblem.INVALID_JSON, id="excessive-depth"
        ),
        (b"[]", ProbeProblem.INVALID_RESPONSE),
        (b"null", ProbeProblem.INVALID_RESPONSE),
        ("{}", ProbeProblem.INVALID_RESPONSE),
        pytest.param(
            b" " * (MAX_BODY_BYTES + 1),
            ProbeProblem.BODY_TOO_LARGE,
            id="excessive-size",
        ),
    ],
)
def test_bad_json_and_body_limits(body, problem):
    result = assess_changed(replace(response(ProbePath.BRAND), body=body))
    assert result.issues[0].problem == problem
    assert result.identity is None


def test_valid_json_at_the_body_limit_is_allowed():
    item = response(ProbePath.BRAND)
    item = replace(item, body=item.body + b" " * (MAX_BODY_BYTES - len(item.body)))
    assert assess_changed(item).status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "/private-path"},
        {"type": "floatValue"},
        {"writeable": True},
        {"writeable": 1},
        {"value": 15},
    ],
)
def test_string_resource_envelope_is_checked_before_values(changes):
    result = assess_changed(response(ProbePath.BRAND, **changes))
    assert result.status == ProbeStatus.FAILED
    assert result.issues[0].problem == (
        ProbeProblem.WRONG_RESOURCE
        if "id" in changes
        else ProbeProblem.INVALID_RESPONSE
    )
    assert "private-path" not in repr(result)


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"status": "pending"}, ProbeProblem.INCOMPLETE_SYSTEM),
        ({"gatewayId": 123}, ProbeProblem.INVALID_IDENTITY),
        ({"gatewayId": ""}, ProbeProblem.INVALID_IDENTITY),
        ({"values": {}}, ProbeProblem.INVALID_IDENTITY),
        ({"timeStamp": "yesterday"}, ProbeProblem.INVALID_IDENTITY),
    ],
)
def test_system_response_uses_existing_identity_rules(changes, problem):
    # status belongs to the JSON envelope, not the HTTP status argument.
    item = response(ProbePath.SYSTEM)
    payload = json.loads(item.body)
    payload.update(changes)
    result = assess_changed(replace(item, body=json.dumps(payload).encode()))
    assert result.issues[0].problem == problem
    assert result.identity is None


def test_reported_modules_are_not_device_or_function_claims():
    result = assess_changed(
        response(
            ProbePath.SYSTEM,
            values=[{"ProductName": "private product"}, {"unknown": "private module"}],
        )
    )
    assert result.reported_module_count == 2
    assert "private" not in repr(result)
    assert not hasattr(result, "devices") and not hasattr(result, "heating_circuits")


def test_unrelated_finite_metadata_is_accepted_but_not_retained():
    result = assess_changed(response(ProbePath.BRAND, extra={"value": 1.5}))
    assert result.status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    assert "extra" not in repr(result)


@pytest.mark.parametrize(
    "metadata", [[0] * MAX_JSON_VALUES, {"a": [0] * 5000, "b": [0] * 5000}]
)
def test_many_small_json_values_cannot_exceed_the_structure_budget(metadata):
    result = assess_changed(response(ProbePath.BRAND, extra=metadata))
    assert result.issues[0].problem == ProbeProblem.INVALID_JSON


@pytest.mark.parametrize("value", [None, 123, True])
def test_direct_firmware_parser_rejects_non_strings(value):
    with pytest.raises(LocalProbeError) as error:
        parse_firmware_version(value)
    assert error.value.problem == ProbeProblem.UNKNOWN_FIRMWARE


def test_unknown_response_path_is_rejected_without_retaining_it():
    result = assess_local_probe(
        [replace(response(ProbePath.BRAND), path="/private-path")],
        current_context=CONTEXT,
    )
    assert result.issues[0].problem == ProbeProblem.INVALID_RESPONSE
    assert "private-path" not in repr(result)
