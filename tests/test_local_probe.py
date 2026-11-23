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
    MINIMUM_FIRMWARE,
    SERVICE_TYPE,
    FirmwareVersion,
    GatewayBrand,
    LocalEndpoint,
    LocalProbeError,
    ProbeContext,
    ProbeIssue,
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
        "https://user:pass@gateway.local",
        "gateway.local:8080",
        "gateway.local:9443/path",
        "ftp://gateway.local",
        "user:pass@gateway.local",
        "gateway/path",
        "gateway?token=private",
        "gateway#fragment",
        "gateway\\path",
        "gate way",
        "gateway\u200b.local",
        "gäteway.local",
        "gateway..",
        ".gateway",
        "-gateway.local",
        "gateway-.local",
        "a" * 64 + ".local",
        "[gateway.local]",
        "[2001:db8::7]:8443",
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://gateway.local", "gateway.local"),
        ("HTTPS://Gateway.Local/", "gateway.local"),
        ("http://192.0.2.7/", "192.0.2.7"),
        ("gateway.local:9443", "gateway.local"),
        ("192.0.2.7:9442", "192.0.2.7"),
        ("192.0.2.7:80", "192.0.2.7"),
        ("https://192.0.2.7:443//", "192.0.2.7"),
        ("[2001:db8::7]:9443", "2001:db8::7"),
        ("https://[fe80::7%eth0]:9443/", "fe80::7%eth0"),
        ("gateway\r\n", "gateway"),
        ("\tgateway", "gateway"),
    ],
)
def test_a_copied_web_address_or_a_known_port_is_left_out(raw, expected):
    # Copied from a browser or typed with the port: the address alone is
    # kept; the two fixed ports stay as they are.
    endpoint = LocalEndpoint(raw)
    assert endpoint.host == expected
    assert endpoint.auth_port == 9442 and endpoint.resource_port == 9443


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


# The module list of a K40-class gateway: its row at the gateway's place.
K40_ROWS = [{"Tok": "72", "Id": "17", "Ver": "15.00.01", "ModuleHwIdentStr": "K40RF"}]


def assess_k40(*changed):
    """A probe whose system information names a K40-class gateway."""
    by_path = {item.path: item for item in changed}
    return assess_local_probe(
        [
            by_path.get(path)
            or (
                response(path, values=K40_ROWS)
                if path == ProbePath.SYSTEM
                else response(path)
            )
            for path in ProbePath
        ],
        current_context=CONTEXT,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("15.1", FirmwareVersion(15, 1, 0)),
        ("15", FirmwareVersion(15, 0, 0)),
        ("v15.00.01", FirmwareVersion(15, 0, 1)),
        ("15.00.01-beta", FirmwareVersion(15, 0, 1)),
        ("16.00.00-rc1", FirmwareVersion(16, 0, 0)),
        ("15.00.01+build.7", FirmwareVersion(15, 0, 1)),
        (" 15.00.01 ", FirmwareVersion(15, 0, 1)),
        ("15.00.01.2", FirmwareVersion(15, 0, 1)),
        ("05.06.00", FirmwareVersion(5, 6, 0)),
    ],
)
def test_other_version_forms_are_read_by_their_numbers(value, expected):
    assert parse_firmware_version(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        15,
        "",
        "-15.00.01",
        "15..1",
        "15.00.01.2.3",
        "version 15",
        "\uff11\uff15.\uff10\uff10.\uff10\uff11",
        "9999999.1.1",
    ],
)
def test_an_unknown_firmware_format_never_stops_the_setup(value):
    # Not a known version: the version stays unknown, no minimum is checked
    # and nothing is guessed, even on a K40-class gateway.
    result = assess_k40(response(ProbePath.FIRMWARE, value=value))
    assert result.status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    assert result.issues == ()
    assert result.firmware is None
    assert result.identity is not None


@pytest.mark.parametrize("value", ["01.00.00", "14.99.99", "15.00.00", "9.99.99"])
def test_old_firmware_is_not_confused_with_authentication_failure(value):
    result = assess_k40(response(ProbePath.FIRMWARE, value=value))
    assert result.issues == (ProbeIssue(ProbePath.FIRMWARE, ProbeProblem.OLD_FIRMWARE),)
    assert result.firmware == parse_firmware_version(value)
    assert result.identity is None


@pytest.mark.parametrize(
    "rows",
    [
        K40_ROWS,
        [{"Tok": "72", "Id": "17", "Ver": "15.00.01"}],
        [{"Tok": 75, "Id": 17, "ModuleHwIdentStr": "MX400"}],
        [{"Tok": "72", "Id": "99", "ModuleHwIdentStr": "mx300"}],
        [
            {"Tok": "8", "Id": "8"},
            {"Tok": "72", "Id": "4", "ModuleHwIdentStr": "K30RF"},
        ],
    ],
)
def test_the_minimum_firmware_applies_to_k30_and_k40_gateways(rows):
    result = assess_local_probe(
        [
            response(ProbePath.BRAND),
            response(ProbePath.FIRMWARE, value="14.00.01"),
            response(ProbePath.SYSTEM, values=rows),
        ],
        current_context=CONTEXT,
    )
    assert result.issues == (ProbeIssue(ProbePath.FIRMWARE, ProbeProblem.OLD_FIRMWARE),)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"Tok": "8", "Id": "17", "ModuleHwIdentStr": "K40RF"}],
        [{"Tok": "72", "Id": "17", "ModuleHwIdentStr": "K21"}],
        [{"Tok": "72", "Id": "5", "ModuleHwIdentStr": "CT200"}],
        [{"Tok": "72", "Id": "5"}],
        [{"Tok": True, "Id": "17", "ModuleHwIdentStr": "K40RF"}],
        ["not a row"],
    ],
)
def test_other_gateways_have_no_minimum_firmware(rows):
    # The same local service exists on other gateways with their own
    # version counting (for example 01.00.00 or 05.06.00).
    result = assess_local_probe(
        [
            response(ProbePath.BRAND),
            response(ProbePath.FIRMWARE, value="01.00.00"),
            response(ProbePath.SYSTEM, values=rows),
        ],
        current_context=CONTEXT,
    )
    assert result.status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    assert result.firmware == FirmwareVersion(1, 0, 0)
    assert result.firmware_minimum is False


def test_a_k40_with_the_minimum_firmware_notes_the_minimum():
    result = assess_k40()
    assert result.status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    assert result.firmware_minimum is True


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


@pytest.mark.parametrize(
    "value", ["unknown", "UNKNOWN", "", "   ", "private product name", "nefit"]
)
def test_a_brand_the_gateway_does_not_report_never_stops_the_setup(value):
    # A K40 of a heat pump of another brand answers "unknown".
    result = assess_changed(response(ProbePath.BRAND, value=value))
    assert result.status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    assert result.issues == ()
    assert result.brand is None
    assert result.identity is not None and result.firmware == MINIMUM_FIRMWARE
    assert value.strip() == "" or value not in repr(result)


def test_a_known_brand_with_spaces_is_still_recognized():
    result = assess_changed(response(ProbePath.BRAND, value=" Buderus "))
    assert result.brand == GatewayBrand.BUDERUS


@pytest.mark.parametrize(
    "changes",
    [
        {"value": None},
        {"value": 1},
        {"value": ["unknown"]},
        {"type": "floatValue"},
        {"id": "/private-path"},
    ],
)
def test_a_malformed_brand_answer_only_leaves_the_brand_unknown(changes):
    result = assess_changed(response(ProbePath.BRAND, **changes))
    assert result.status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    assert result.issues == ()
    assert result.brand is None and result.identity is not None
    assert "private-path" not in repr(result)


@pytest.mark.parametrize("status", [400, 403, 404, 405, 406, 410])
def test_an_error_answer_for_the_brand_only_leaves_the_brand_unknown(status):
    result = assess_changed(response(ProbePath.BRAND, status=status))
    assert result.status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    assert result.brand is None and result.firmware == MINIMUM_FIRMWARE


@pytest.mark.parametrize(
    ("status", "problem"),
    [
        (401, ProbeProblem.AUTH_REJECTED),
        (429, ProbeProblem.RATE_LIMITED),
        (500, ProbeProblem.SERVICE_UNAVAILABLE),
        (503, ProbeProblem.SERVICE_UNAVAILABLE),
        (301, ProbeProblem.REDIRECT),
        (204, ProbeProblem.HTTP_ERROR),
    ],
)
def test_a_refused_token_or_a_pause_on_the_brand_still_counts(status, problem):
    result = assess_changed(response(ProbePath.BRAND, status=status))
    assert result.status == ProbeStatus.FAILED
    assert result.issues == (ProbeIssue(ProbePath.BRAND, problem),)


@pytest.mark.parametrize("writeable", [1, True, None, "0"])
def test_the_brand_is_read_whatever_its_writeable_flag(writeable):
    result = assess_changed(response(ProbePath.BRAND, writeable=writeable))
    assert result.brand == GatewayBrand.BUDERUS


def test_an_unknown_brand_does_not_hide_the_firmware_check():
    result = assess_k40(
        response(ProbePath.BRAND, value="unknown"),
        response(ProbePath.FIRMWARE, value="14.00.01"),
    )
    assert result.issues == (ProbeIssue(ProbePath.FIRMWARE, ProbeProblem.OLD_FIRMWARE),)
    assert result.brand is None and result.identity is None


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
    result = assess_changed(replace(response(ProbePath.FIRMWARE), body=body))
    assert result.issues[0].problem == problem
    assert result.identity is None
    # The same answer for the brand only leaves the brand unknown.
    result = assess_changed(replace(response(ProbePath.BRAND), body=body))
    assert result.status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    assert result.brand is None


def test_valid_json_at_the_body_limit_is_allowed():
    item = response(ProbePath.BRAND)
    item = replace(item, body=item.body + b" " * (MAX_BODY_BYTES - len(item.body)))
    assert assess_changed(item).status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON


def test_the_firmware_answer_must_belong_to_its_resource():
    result = assess_changed(response(ProbePath.FIRMWARE, id="/private-path"))
    assert result.status == ProbeStatus.FAILED
    assert result.issues[0].problem == ProbeProblem.WRONG_RESOURCE
    assert "private-path" not in repr(result)


@pytest.mark.parametrize(
    ("changes", "firmware"),
    [
        ({"writeable": True}, MINIMUM_FIRMWARE),
        ({"writeable": 1}, MINIMUM_FIRMWARE),
        ({"writeable": None}, MINIMUM_FIRMWARE),
        ({"type": "floatValue"}, None),
        ({"value": 15}, None),
    ],
)
def test_a_read_only_answer_is_read_whatever_its_flags(changes, firmware):
    # The writeable flag of a value that is only read is not used; another
    # form of the value only leaves the version unknown.
    result = assess_k40(response(ProbePath.FIRMWARE, **changes))
    assert result.status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    assert result.firmware == firmware


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"status": "pending"}, ProbeProblem.INCOMPLETE_SYSTEM),
        ({"gatewayId": 123}, ProbeProblem.INVALID_IDENTITY),
        ({"gatewayId": ""}, ProbeProblem.INVALID_IDENTITY),
        ({"values": {}}, ProbeProblem.INVALID_IDENTITY),
        ({"status": "in_progress"}, ProbeProblem.INCOMPLETE_SYSTEM),
        ({"status": None}, ProbeProblem.INCOMPLETE_SYSTEM),
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
    result = assess_changed(response(ProbePath.FIRMWARE, extra=metadata))
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
