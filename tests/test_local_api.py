"""Local contracts tested with pinned manufacturer examples and hostile inputs."""

from __future__ import annotations

import base64
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from decimal import Decimal
from itertools import product
from pathlib import Path
from urllib.parse import parse_qsl

import pytest

from custom_components.bosch_buderus_heating.gateway_identity import (
    GatewayNamespace,
    IdentityContext,
)
from custom_components.bosch_buderus_heating.local_api.catalog import (
    CATALOG,
    SOURCE_COMMIT,
    SOURCE_SHA256,
    make_request,
    request_for_path,
)
from custom_components.bosch_buderus_heating.local_api.decoder import decode_resource
from custom_components.bosch_buderus_heating.local_api.errors import (
    LocalResourceError,
    Problem,
)
from custom_components.bosch_buderus_heating.local_api.json import JsonLimits, load_json
from custom_components.bosch_buderus_heating.local_api.models import (
    DataIssue,
    NamedValues,
    Recording,
    ReferenceList,
    ScalarValue,
    SystemInfo,
    UpdateReport,
    UpdateStatus,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/local_api_examples.json").read_text()
)
EXAMPLES = FIXTURE["examples"]
CONTEXT = IdentityContext(
    GatewayNamespace.K40_LOCAL, "example-owner", "example-local", 2
)
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
TEMP = "/heatingCircuits/hc2/currentRoomSetpoint"


def read(raw, *, path=None, query=None, **kwargs):
    path = path or raw["id"]
    if "?" in path:
        path, encoded_query = path.split("?", 1)
        if query is None:
            query = dict(parse_qsl(encoded_query))
    if path.startswith("/recordings/") and query is None:
        query = {"sampleRate": "all"}
    request = request_for_path(
        path, query=query, signal_ids=(path.removeprefix("/signals/"),)
    )
    body = raw if isinstance(raw, bytes) else json.dumps(raw).encode()
    return decode_resource(
        request,
        body,
        context=kwargs.pop("context", CONTEXT),
        observed_at=kwargs.pop("observed_at", NOW),
        observed_monotonic=kwargs.pop("observed_monotonic", 4.25),
        **kwargs,
    )


def scalar(path=TEMP, **changes):
    raw = {
        "id": path,
        "type": "floatValue",
        "value": 21.25,
        "writeable": 0,
        "unitOfMeasure": "C",
    }
    raw.update(changes)
    return raw


def example(path):
    return json.loads(
        json.dumps(
            next(row["response"] for row in EXAMPLES if row["response"]["id"] == path)
        )
    )


def test_catalog_matches_pinned_independent_source_inventory():
    assert FIXTURE["source_commit"] == SOURCE_COMMIT
    assert FIXTURE["source_sha256"] == SOURCE_SHA256
    assert {d.template for d in CATALOG} == set(FIXTURE["templates"])
    assert len(CATALOG) == 261
    assert len({s for d in CATALOG for s in d.response_schemas}) == 21
    assert sum(len(row) for row in _address_domains()) == 1287


def _address_domains():
    for definition in CATALOG:
        params = [p for p in definition.parameters if p.location == "path"]
        if any(not p.options for p in params):
            continue
        yield list(product(*(p.options for p in params)))


@pytest.mark.parametrize("definition", CATALOG, ids=lambda d: d.template)
def test_every_documented_address_can_be_built_without_discovery_or_io(definition):
    params = [p for p in definition.parameters if p.location == "path"]
    choices = [p.options or ("SRC.RelativeTime",) for p in params]
    query = (
        {"sampleRate": "all"}
        if any(p.name == "sampleRate" for p in definition.parameters)
        else {}
    )
    for values in product(*choices):
        request = make_request(
            definition.template,
            parameters=dict(zip((p.name for p in params), values, strict=True)),
            query=query,
            signal_ids=("SRC.RelativeTime",),
        )
        assert (
            request_for_path(
                request.path, query=query, signal_ids=("SRC.RelativeTime",)
            )
            == request
        )
        assert request.method == "GET"
        assert "{" not in request.target and "}" not in request.target
        assert "http:" not in request.target


@pytest.mark.parametrize(
    "row", EXAMPLES, ids=lambda row: row["template"] + ":" + row["name"]
)
def test_official_response_variants_preserve_source_and_metadata(row):
    if "/hsX/" in row["response"]["id"] or "/hs${hs_id}/" in row["response"]["id"]:
        # The source contains two illustrative placeholders, not valid addresses.
        with pytest.raises(LocalResourceError):
            read(row["response"])
        return
    observation = read(row["response"])
    assert observation.request.definition.template == row["template"]
    assert observation.type == row["response"]["type"]
    assert observation.can_write is False
    assert observation.context == CONTEXT
    assert observation.observed_at == NOW and observation.observed_monotonic == 4.25
    assert observation.reported_writeable == bool(row["response"]["writeable"])
    assert row["response"]["id"] not in repr(observation)
    assert isinstance(observation.document, dict) is False


@pytest.mark.parametrize(
    "row", EXAMPLES, ids=lambda row: row["template"] + ":" + row["name"]
)
def test_response_from_another_resource_is_never_accepted(row):
    raw = row["response"].copy()
    raw["id"] = "/unrelated/resource"
    with pytest.raises(LocalResourceError, match="wrong_resource"):
        read(
            raw,
            path=row["response"]["id"]
            .replace("/hsX/", "/hs1/")
            .replace("/hs${hs_id}/", "/hs1/"),
        )


@pytest.mark.parametrize("raw", [True, False, "21", None, {}, []])
def test_numeric_values_do_not_coerce_other_types(raw):
    with pytest.raises(LocalResourceError, match="invalid_schema"):
        read(scalar(value=raw))


@pytest.mark.parametrize(
    "changes",
    [
        {"writeable": True},
        {"writeable": 2},
        {"recordable": False},
        {"minValue": "0"},
        {"maxValue": []},
        {"stepSize": None},
        {"type": "integerValue"},
        {"unitOfMeasure": 4},
    ],
)
def test_numeric_metadata_has_precise_types(changes):
    with pytest.raises(LocalResourceError, match="invalid_schema"):
        read(scalar(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"minValue": 30, "maxValue": 20},
        {"stepSize": 0},
        {"stepSize": -1},
        {"state": [{"OFF": 0}, {"OFF": 1}]},
    ],
)
def test_contradictory_numeric_metadata_is_rejected(changes):
    with pytest.raises(LocalResourceError, match="invalid_metadata"):
        read(scalar(**changes))


@pytest.mark.parametrize(
    "value, states", [(0, ("OFF_HEAT",)), (90, ("OFF_COOL",)), (21, ())]
)
def test_state_markers_are_not_temperature_measurements(value, states):
    raw = scalar(
        "/signals/SC.HC1.FlowTempSetp",
        type="integerValue",
        value=value,
        state=[{"OFF_COOL": 90}, {"OFF_HEAT": 0}],
    )
    result = read(raw).data
    assert isinstance(result, ScalarValue)
    assert result.matched_states == states
    assert result.measurement == (None if states else value)


def test_reported_writeable_never_creates_local_write_capability():
    result = read(scalar(writeable=1, recordable=1))
    assert result.reported_writeable and result.recordable
    assert result.can_write is False
    assert result.data.measurement == Decimal("21.25")


@pytest.mark.parametrize(
    "value, expected",
    [
        (b"900719925474099312345", 900719925474099312345),
        (b"9.00719925474099312345e20", 900719925474099312345),
        (b'"0009007199254740993"', "0009007199254740993"),
    ],
)
def test_identifiers_keep_exact_digits_and_leading_zeroes(value, expected):
    raw = example("/system/basicInfo")
    raw["values"][0]["ModuleSerialNumber"] = "REPLACE_SERIAL"
    body = json.dumps(raw).encode().replace(b'"REPLACE_SERIAL"', value)
    result = read(body, path="/system/basicInfo").data
    assert isinstance(result, SystemInfo)
    assert result.modules[0].serial_number == expected


def test_fractional_identifiers_are_rejected_not_rounded():
    raw = example("/system/basicInfo")
    raw["values"][0]["ModuleSerialNumber"] = 12.25
    with pytest.raises(LocalResourceError, match="invalid_metadata"):
        read(raw)


def test_named_values_are_keyed_preserved_and_not_implicitly_summed():
    raw = {
        "id": "/heatSources/emon/totalConsumption",
        "type": "emonValue",
        "writeable": 0,
        "unit": "kWh",
        "values": [{"total": 100}, {"compressor": 80}, {"eheater": 20}],
    }
    result = read(raw).data
    raw["values"].reverse()
    reverse = read(raw).data
    assert isinstance(result, NamedValues)
    assert (
        {v.name: v.value for v in result.values}
        == {v.name: v.value for v in reverse.values}
        == {"total": 100, "compressor": 80, "eheater": 20}
    )
    assert result.unit == "kWh"
    raw.pop("unit")
    assert read(raw).data.issues == (DataIssue.MISSING_UNIT,)


@pytest.mark.parametrize(
    "values",
    [
        [{"total": 1}, {"total": 2}],
        [{}],
        [{"future": "bad"}],
        [{"total": 1, "compressor": 2}],
    ],
)
def test_invalid_named_value_structures_do_not_leak_as_measurements(values):
    raw = {
        "id": "/heatSources/emon/totalConsumption",
        "type": "emonValue",
        "writeable": 0,
        "values": values,
    }
    with pytest.raises(LocalResourceError):
        read(raw)


@pytest.mark.parametrize("unit", ['rpm"', "future-unit"])
def test_unknown_or_malformed_units_are_kept_but_not_used_as_measurements(unit):
    result = read(scalar(unitOfMeasure=unit)).data
    assert result.unit == unit and result.measurement is None
    assert result.issues == (DataIssue.UNKNOWN_UNIT,)


@pytest.mark.parametrize(
    "charset, encoding",
    [
        ("UTF16", "utf-16-be"),
        ("UTF-16", "utf-16"),
        ("UTF8", "utf-8"),
        ("ASCII", "ascii"),
    ],
)
def test_explicit_name_encoding_is_decoded(charset, encoding):
    raw = {
        "id": "/zones/zone1/name",
        "type": "stringValue",
        "writeable": 1,
        "value": base64.b64encode("Room one".encode(encoding)).decode(),
        "valueConfig": {"charset": charset, "codingType": "base64", "maxLength": 50},
    }
    assert read(raw).data.decoded_text == "Room one"


def test_unknown_name_encoding_and_plain_strings_remain_distinct():
    raw = example("/zones/zone1/name")
    raw["valueConfig"]["charset"] = "future-charset"
    result = read(raw).data
    assert result.decoded_text is None and result.value == raw["value"]
    assert result.issues == (DataIssue.UNSUPPORTED_TEXT_ENCODING,)
    raw = {
        "id": "/gateway/brand",
        "type": "stringValue",
        "writeable": 0,
        "value": "QnVkZXJ1cw==",
    }
    assert read(raw).data.value == "QnVkZXJ1cw=="
    assert read(raw).data.decoded_text is None


@pytest.mark.parametrize(
    "change",
    [
        {"value": "not!base64"},
        {"valueConfig": None},
        {"valueConfig": {}},
        {"valueConfig": {"charset": "UTF16", "codingType": "base64", "maxLength": -1}},
    ],
)
def test_invalid_name_configuration_is_reported_with_static_error(change):
    raw = example("/zones/zone1/name")
    raw.update(change)
    with pytest.raises(LocalResourceError) as caught:
        read(raw)
    assert raw["value"] not in str(caught.value)


def test_references_keep_ids_without_following_uris_or_creating_devices():
    raw = example("/signals")
    raw["references"][0]["uri"] = "https://unrelated.example/signals/not-the-id"
    result = read(raw).data
    assert isinstance(result, ReferenceList)
    request = make_request(
        "/signals/{signalId}",
        parameters={"signalId": "SRC.RelativeTime"},
        signal_ids=result.signal_ids,
    )
    assert request.target == "/signals/SRC.RelativeTime"
    assert not hasattr(result, "devices")
    with pytest.raises(LocalResourceError, match="unconfirmed_signal"):
        make_request(
            "/signals/{signalId}",
            parameters={"signalId": "not-reported"},
            signal_ids=result.signal_ids,
        )
    raw["references"].append(raw["references"][0])
    with pytest.raises(LocalResourceError, match="invalid_metadata"):
        read(raw)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "//signals/X",
        "/signals/../X",
        "/signals/%2e%2e",
        "/signals/X?x=y",
        "/signals/X#fragment",
        "/signals/X\\Y",
        "http://example/signals/X",
        "/signals/X\n",
        "/signals/\ud800",
        "/signals/" + "x" * 1025,
        "/heatingCircuits/hc5/currentRoomSetpoint",
        "/heatingCircuits/hc01/currentRoomSetpoint",
    ],
)
def test_unsafe_unknown_and_out_of_domain_paths_are_rejected(path):
    with pytest.raises(LocalResourceError):
        request_for_path(path)


def test_dynamic_unicode_ids_are_encoded_once_after_explicit_evidence():
    result = make_request(
        "/signals/{signalId}", parameters={"signalId": "Raum.Ä"}, signal_ids=("Raum.Ä",)
    )
    assert result.path == "/signals/Raum.Ä" and result.target == "/signals/Raum.%C3%84"


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"sampleRate": "PT15M"},
        {"sampleRate": True},
        {"sampleRate": "all", "startDate": "2026-02-30"},
        {"sampleRate": "all", "startDate": "20261101"},
        {"sampleRate": "all", "startDate": "2026-11-02", "endDate": "2026-11-01"},
        {"sampleRate": "all", "limit": "5"},
    ],
)
def test_recording_queries_reject_missing_or_invalid_parameters(query):
    path = next(
        d.template for d in CATALOG if d.group == "recordings" and "{" not in d.template
    )
    with pytest.raises(LocalResourceError, match="invalid_parameter"):
        make_request(path, query=query)


@pytest.mark.parametrize("rate", ["PT1H", "P1D", "P1M", "all"])
def test_all_documented_history_query_forms_are_supported(rate):
    path = next(
        d.template for d in CATALOG if d.group == "recordings" and "{" not in d.template
    )
    result = make_request(
        path,
        query={"sampleRate": rate, "startDate": "2026-10-31", "endDate": "2026-10-31"},
    )
    assert dict(result.query)["sampleRate"] == rate


def test_plain_wifi_read_cannot_trigger_scan():
    path = "/gateway/wifi/apList"
    assert make_request(path).target == path
    assert (
        make_request(path, query={"refresh": False}).target == path + "?refresh=false"
    )
    for query in ({"refresh": True}, {"refresh": "true"}, {"refresh": 1}):
        with pytest.raises(LocalResourceError):
            make_request(path, query=query)


def test_recording_preserves_groups_offsets_missing_samples_and_counts():
    row = next(
        row for row in EXAMPLES if row["response"]["type"] == "yRecordingExtended"
    )
    raw = json.loads(json.dumps(row["response"]))
    raw["values"] = [
        {
            "sampleRate": "PT1H",
            "startTime": "2026-10-25T02:00:00+02:00",
            "endTime": "2026-10-25T02:00:00+01:00",
            "recording": [{"y": 1.25, "c": 4}, {"y": 0, "c": 0}],
        },
        {
            "sampleRate": "P1M",
            "startTime": "2026-10-01T00:00:00Z",
            "endTime": "2026-11-01T00:00:00Z",
            "recording": [],
        },
    ]
    result = read(raw).data
    assert isinstance(result, Recording)
    assert len(result.groups) == 2 and result.groups[1].samples == ()
    assert result.groups[0].samples[0].value == Decimal("1.25")
    assert result.groups[0].samples[1].count == 0
    assert result.groups[0].start.utcoffset() != result.groups[0].end.utcoffset()
    assert result.resource.id == raw["recordedResource"]["id"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("startTime", "2026-11-01T00:00:00"),
        ("endTime", "2020-01-01T00:00:00Z"),
        ("sampleRate", "all"),
        ("recording", [{"y": 1, "c": -1}]),
        ("recording", [{"y": True, "c": 1}]),
    ],
)
def test_invalid_history_groups_are_not_partly_published(field, value):
    raw = json.loads(
        json.dumps(
            next(
                row["response"]
                for row in EXAMPLES
                if row["response"]["type"] == "yRecordingExtended"
            )
        )
    )
    raw["values"][0][field] = value
    with pytest.raises(LocalResourceError):
        read(raw)


def test_update_reports_keep_legacy_fields_and_exact_module_identifiers():
    raw = example("/gateway/update/status")
    if raw["type"] == "updateStatus":
        raw["value"]["reciepeId"] = "legacy-id"
        result = read(raw).data
        assert (
            isinstance(result, UpdateStatus)
            and result.value["reciepeId"] == "legacy-id"
        )
    raw = example("/system/update/report")
    raw["values"] = [
        {"moduleId": "000000000000001", "status": "SUCCESS", "timeStamp": "1700000000"}
    ]
    result = read(raw).data
    assert isinstance(result, UpdateReport)
    assert result.modules[0].module_id == "000000000000001"


def test_unknown_fields_are_bounded_immutable_and_not_coerced_into_metadata():
    raw = scalar(
        extension={"nested": ["private value", 1.5]}, allowedValues={"untyped": 1}
    )
    result = read(raw)
    assert result.document["extension"]["nested"] == ("private value", Decimal("1.5"))
    assert result.data.allowed_values == ()
    assert "private value" not in repr(result.data) + repr(result)
    with pytest.raises(TypeError):
        result.document["extension"]["nested"] = ()
    with pytest.raises(FrozenInstanceError):
        result.data.value = 3


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"\xff",
        b'{"id":1,"id":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'{"x":1e-999}',
        b"[" * 1500 + b"0" + b"]" * 1500,
    ],
)
def test_invalid_json_is_rejected_without_echoing_payload(body):
    with pytest.raises(LocalResourceError, match="invalid_json"):
        load_json(body)


def test_parser_budgets_have_exact_boundaries_and_never_truncate():
    assert load_json(b"[1]", limits=JsonLimits(3, 1, 2)) == [1]
    assert load_json(b"{}", limits=JsonLimits(2, 1, 1)) == {}
    for body, limits in [
        (b"[1]", JsonLimits(2, 1, 2)),
        (b"[1,2]", JsonLimits(10, 1, 2)),
        (b"[[1]]", JsonLimits(10, 1, 3)),
    ]:
        with pytest.raises(LocalResourceError):
            load_json(body, limits=limits)
    for invalid in (0, -1, True):
        with pytest.raises(ValueError):
            JsonLimits(invalid, 1, 1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"observed_monotonic": -1},
        {"observed_monotonic": True},
        {"observed_monotonic": float("nan")},
        {"observed_at": datetime(2026, 1, 1)},
        {
            "context": IdentityContext(
                GatewayNamespace.POINTT, "example-owner", "example-cloud", 2
            )
        },
    ],
)
def test_observation_requires_local_source_and_valid_separate_timestamps(kwargs):
    with pytest.raises(LocalResourceError, match="invalid_context"):
        read(scalar(), **kwargs)


def test_mismatched_or_forged_request_contract_is_rejected():
    request = request_for_path(TEMP)
    forged = replace(request, definition=make_request("/gateway/brand").definition)
    with pytest.raises(LocalResourceError, match="invalid_parameter"):
        decode_resource(
            forged,
            json.dumps(scalar()).encode(),
            context=CONTEXT,
            observed_at=NOW,
            observed_monotonic=1,
        )


@pytest.mark.parametrize(
    "template,path,value",
    [
        ("/gateway/eth/mac", "/gateway/eth/mac", "00:00:00:00:00:00"),
        ("/gateway/wifi/mac", "/gateway/wifi/mac", "00:00:00:00:00:00"),
        (
            "/heatingCircuits/{heatingCircuitId}/openWindowDetection/status",
            "/heatingCircuits/hc4/openWindowDetection/status",
            "closed",
        ),
        (
            "/zones/{zoneId}/cooling/manualRoomSetpoint",
            "/zones/zone16/cooling/manualRoomSetpoint",
            24,
        ),
        (
            "/zones/{zoneId}/cooling/temporaryRoomSetpoint",
            "/zones/zone16/cooling/temporaryRoomSetpoint",
            24,
        ),
        (
            "/zones/{zoneId}/currentRoomSetpoint",
            "/zones/zone16/currentRoomSetpoint",
            20,
        ),
        (
            "/zones/{zoneId}/heating/manualRoomSetpoint",
            "/zones/zone16/heating/manualRoomSetpoint",
            20,
        ),
        (
            "/zones/{zoneId}/heating/temporaryRoomSetpoint",
            "/zones/zone16/heating/temporaryRoomSetpoint",
            20,
        ),
    ],
)
def test_operations_without_source_examples_have_explicit_synthetic_responses(
    template, path, value
):
    assert template not in {row["template"] for row in EXAMPLES}
    raw = {
        "id": path,
        "type": "stringValue" if isinstance(value, str) else "floatValue",
        "writeable": 0,
        "value": value,
    }
    observation = read(raw)
    assert observation.request.definition.template == template
    assert observation.data.value == value


@pytest.mark.parametrize(
    "template",
    [
        "/heatSources/{heatSourceId}/oduFanSpeed",
        "/heatSources/{heatSourceId}/refrigerant/heatCarrierPumpSpeed",
    ],
)
def test_source_placeholders_work_only_after_selecting_a_real_documented_address(
    template,
):
    raw = json.loads(
        json.dumps(
            next(row["response"] for row in EXAMPLES if row["template"] == template)
        )
    )
    raw["id"] = template.format(heatSourceId="hs6")
    assert read(raw).data.measurement == 60


@pytest.mark.parametrize(
    "suffix",
    [
        "?sampleRate=PT1H",
        "?sampleRate=all&sampleRate=all",
        "?sampleRate=all&unexpected=1",
        "?sampleRate=all#fragment",
        "?broken",
        "?sampleRate=all&startDate=2026-01-01&endDate=2026-01-02&extra=1",
    ],
)
def test_history_response_id_cannot_change_the_requested_query(suffix):
    raw = json.loads(
        json.dumps(
            next(
                row["response"]
                for row in EXAMPLES
                if row["response"]["type"] == "yRecordingExtended"
            )
        )
    )
    path = raw["id"].split("?", 1)[0]
    raw["id"] = path + suffix
    with pytest.raises(LocalResourceError, match="wrong_resource"):
        read(raw, path=path, query={"sampleRate": "all"})


def test_history_response_query_order_can_differ_but_not_its_values():
    raw = json.loads(
        json.dumps(
            next(
                row["response"]
                for row in EXAMPLES
                if row["response"]["type"] == "yRecordingExtended"
            )
        )
    )
    path, encoded_query = raw["id"].split("?", 1)
    query = dict(parse_qsl(encoded_query))
    raw["id"] = path + "?" + "&".join(reversed(encoded_query.split("&")))
    assert isinstance(read(raw, path=path, query=query).data, Recording)
    raw["id"] = path
    assert isinstance(read(raw, path=path, query=query).data, Recording)


def test_payload_errors_are_static_and_limits_apply_to_unknown_fields():
    for payload in (b"null", b"[]", b"42"):
        with pytest.raises(LocalResourceError) as caught:
            read(payload, path=TEMP)
        assert caught.value.problem is Problem.INVALID_SCHEMA
    with pytest.raises(LocalResourceError, match="body_too_large"):
        read(scalar(extra="x" * (256 * 1024)))
    with pytest.raises(LocalResourceError, match="invalid_json"):
        load_json("not bytes")


@pytest.mark.parametrize(
    "progress",
    [
        {"percent": 101},
        {"percent": -1},
        {"nsteps": -1},
        {"cur_step": True},
        {"cur_stepType": {"allowedValues": [1]}},
    ],
)
def test_nested_update_status_fields_are_validated(progress):
    raw = {
        "id": "/gateway/update/status",
        "type": "updateStatus",
        "writeable": 0,
        "value": {"progress": progress},
    }
    with pytest.raises(LocalResourceError, match="invalid_schema"):
        read(raw)


@pytest.mark.parametrize(
    "stamp",
    ["2026-10-25T02:00:00+01:90", "2026-10-25T02:00:00-00:60", "2026-13-25T02:00:00Z"],
)
def test_recording_offsets_and_calendar_dates_are_not_silently_normalized(stamp):
    raw = json.loads(
        json.dumps(
            next(
                row["response"]
                for row in EXAMPLES
                if row["response"]["type"] == "yRecordingExtended"
            )
        )
    )
    raw["values"][0]["startTime"] = stamp
    with pytest.raises(LocalResourceError, match="invalid_schema"):
        read(raw)


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"heatingCircuitId": "hc1", "extra": "x"},
        {"heatingCircuitId": 1},
        {"heatingCircuitId": "hc1/path"},
    ],
)
def test_path_parameters_must_match_the_selected_operation(parameters):
    with pytest.raises(LocalResourceError):
        make_request(
            "/heatingCircuits/{heatingCircuitId}/currentRoomSetpoint",
            parameters=parameters,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"timeStamp": "yesterday"},
        {"gatewayId": 1},
        {"values": [{"Tok": "8"}]},
        {"status": []},
    ],
)
def test_system_information_contract_is_not_just_a_type_label(change):
    raw = example("/system/basicInfo")
    raw.update(change)
    with pytest.raises(LocalResourceError, match="invalid_schema"):
        read(raw)
