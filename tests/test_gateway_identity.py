"""Strict identity evidence and selected-target comparisons using synthetic data."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from custom_components.bosch_buderus_heating.gateway_identity import (
    MAX_IDENTIFIER_LENGTH,
    GatewayIdentity,
    GatewayNamespace,
    GatewayReference,
    IdentityContext,
    IdentityError,
    IdentityMatchStatus,
    IdentityProblem,
    match_gateway_identity,
    parse_local_gateway_identity,
    parse_pointt_gateway_identity,
)
from custom_components.bosch_buderus_heating.plant_model import PlantKey, PlantModel
from custom_components.bosch_buderus_heating.pointt import Gateway

POINTT = GatewayNamespace.POINTT
LOCAL = GatewayNamespace.K40_LOCAL
OWNER = "owner-private-example"
CONNECTION = "connection-private-example"
IDENTIFIER = "0009007199254740993"
PLANT = PlantKey(OWNER, "retained-installation-key")
VERIFIED = ((POINTT, LOCAL),)


def context(namespace: GatewayNamespace = LOCAL) -> IdentityContext:
    return IdentityContext(namespace, OWNER, CONNECTION, 1)


def local_payload(identifier: object = IDENTIFIER) -> dict[str, object]:
    return {
        "id": "/system/basicInfo",
        "type": "systeminfo",
        "writeable": 0,
        "status": "completed",
        "timeStamp": "1684201667",
        "gatewayId": identifier,
        "values": [],
    }


def parse(namespace: GatewayNamespace, identifier: object) -> GatewayIdentity:
    if namespace == POINTT:
        return parse_pointt_gateway_identity(
            {"deviceId": identifier}, context=context(namespace)
        )
    return parse_local_gateway_identity(
        local_payload(identifier), context=context(namespace)
    )


@pytest.mark.parametrize("namespace", [POINTT, LOCAL])
@pytest.mark.parametrize(
    "identifier",
    [
        "0",
        "000001",
        IDENTIFIER,
        "Gateway-AbC",
        "gateway_~.1",
        "opaque:value/with%encoding",
        "Équipement",
        "E\u0301quipement",
        "x" * MAX_IDENTIFIER_LENGTH,
    ],
)
def test_identifiers_remain_exact_opaque_strings(
    namespace: GatewayNamespace, identifier: str
) -> None:
    identity = parse(namespace, identifier)

    assert identity.value == identifier
    assert identity.context == context(namespace)


@pytest.mark.parametrize("namespace", [POINTT, LOCAL])
@pytest.mark.parametrize(
    "identifier",
    [
        None,
        True,
        False,
        123,
        9007199254740993,
        5.703731019068751e21,
        float("nan"),
        [],
        {},
        b"00123",
        "",
        " ",
        " identifier",
        "identifier ",
        "two identifiers",
        "line\nbreak",
        "tab\tvalue",
        "invisible\u200bvalue",
        "bidi\u202evalue",
        "zero\0value",
        "x" * (MAX_IDENTIFIER_LENGTH + 1),
    ],
)
def test_invalid_identifiers_are_never_coerced_or_trimmed(
    namespace: GatewayNamespace, identifier: object
) -> None:
    with pytest.raises(IdentityError) as error:
        parse(namespace, identifier)

    assert error.value.problem == IdentityProblem.INVALID_IDENTITY
    assert str(error.value) == "identity_invalid"


@pytest.mark.parametrize("field", ["deviceId", "id", "gatewayId", "gateway_id"])
def test_pointt_aliases_are_checked_before_the_tolerant_gateway_parser(
    field: str,
) -> None:
    identity = parse_pointt_gateway_identity(
        {field: IDENTIFIER, "model": "not-an-identity"}, context=context(POINTT)
    )

    assert identity.value == IDENTIFIER


def test_pointt_accepts_agreeing_aliases_and_ignores_unrelated_fields() -> None:
    payload = dict.fromkeys(("deviceId", "id", "gatewayId", "gateway_id"), IDENTIFIER)
    payload.update(serialId="unrelated-private-serial", model="example model")

    identity = parse_pointt_gateway_identity(payload, context=context(POINTT))

    assert identity.value == IDENTIFIER
    assert "unrelated-private-serial" not in repr(identity)


@pytest.mark.parametrize("field", ["id", "gatewayId", "gateway_id"])
def test_pointt_never_prefers_one_contradictory_alias(field: str) -> None:
    with pytest.raises(IdentityError) as error:
        parse_pointt_gateway_identity(
            {"deviceId": IDENTIFIER, field: "different-private-id"},
            context=context(POINTT),
        )

    assert error.value.problem == IdentityProblem.CONFLICTING_ALIASES
    assert IDENTIFIER not in str(error.value)
    assert "different-private-id" not in repr(error.value)


@pytest.mark.parametrize("invalid_alias", [None, "", 123, " invalid "])
def test_invalid_secondary_alias_does_not_fall_back_to_primary(
    invalid_alias: object,
) -> None:
    with pytest.raises(IdentityError) as error:
        parse_pointt_gateway_identity(
            {"deviceId": IDENTIFIER, "gateway_id": invalid_alias},
            context=context(POINTT),
        )

    assert error.value.problem == IdentityProblem.INVALID_IDENTITY


@pytest.mark.parametrize("namespace", [POINTT, LOCAL])
def test_missing_identity_is_distinct_from_invalid_identity(
    namespace: GatewayNamespace,
) -> None:
    payload = {} if namespace == POINTT else local_payload()
    payload.pop("gatewayId", None)
    parser = (
        parse_pointt_gateway_identity
        if namespace == POINTT
        else parse_local_gateway_identity
    )

    with pytest.raises(IdentityError) as error:
        parser(payload, context=context(namespace))

    assert error.value.problem == IdentityProblem.MISSING_IDENTITY


@pytest.mark.parametrize("namespace", [POINTT, LOCAL])
@pytest.mark.parametrize("payload", [None, [], "private-payload", 42])
def test_non_object_payload_has_a_safe_error(
    namespace: GatewayNamespace, payload: object
) -> None:
    parser = (
        parse_pointt_gateway_identity
        if namespace == POINTT
        else parse_local_gateway_identity
    )

    with pytest.raises(IdentityError) as error:
        parser(payload, context=context(namespace))

    assert error.value.problem == IdentityProblem.INVALID_PAYLOAD
    assert str(error.value) == "invalid_payload"


@pytest.mark.parametrize("namespace", [POINTT, LOCAL])
def test_parser_cannot_relabel_a_different_source(namespace: GatewayNamespace) -> None:
    parser = (
        parse_pointt_gateway_identity
        if namespace == POINTT
        else parse_local_gateway_identity
    )
    wrong = LOCAL if namespace == POINTT else POINTT

    with pytest.raises(IdentityError) as error:
        parser(local_payload(), context=context(wrong))

    assert error.value.problem == IdentityProblem.WRONG_SOURCE


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("id", "/system/info", IdentityProblem.WRONG_RESOURCE),
        ("id", "/system/basicInfo/", IdentityProblem.WRONG_RESOURCE),
        (
            "id",
            "https://example.invalid/system/basicInfo",
            IdentityProblem.WRONG_RESOURCE,
        ),
        ("id", None, IdentityProblem.WRONG_RESOURCE),
        ("type", "stringValue", IdentityProblem.WRONG_RESOURCE_TYPE),
        ("type", None, IdentityProblem.WRONG_RESOURCE_TYPE),
        ("status", "pending", IdentityProblem.INCOMPLETE_RESPONSE),
        ("status", "failed", IdentityProblem.INCOMPLETE_RESPONSE),
        ("status", None, IdentityProblem.INCOMPLETE_RESPONSE),
        ("timeStamp", 1684201667, IdentityProblem.INVALID_ENVELOPE),
        ("timeStamp", None, IdentityProblem.INVALID_ENVELOPE),
        ("timeStamp", "1684201667\n", IdentityProblem.INVALID_ENVELOPE),
        ("timeStamp", "123", IdentityProblem.INVALID_ENVELOPE),
        (
            "timeStamp",
            "\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19\uff10",
            IdentityProblem.INVALID_ENVELOPE,
        ),
        ("writeable", True, IdentityProblem.INVALID_ENVELOPE),
        ("writeable", False, IdentityProblem.INVALID_ENVELOPE),
        ("writeable", 0.0, IdentityProblem.INVALID_ENVELOPE),
        ("writeable", 1, IdentityProblem.INVALID_ENVELOPE),
        ("writeable", "0", IdentityProblem.INVALID_ENVELOPE),
        ("values", None, IdentityProblem.INVALID_ENVELOPE),
        ("values", {}, IdentityProblem.INVALID_ENVELOPE),
        ("values", (), IdentityProblem.INVALID_ENVELOPE),
    ],
)
def test_local_identity_requires_the_expected_completed_envelope(
    field: str, value: object, problem: IdentityProblem
) -> None:
    payload = local_payload()
    payload[field] = value

    with pytest.raises(IdentityError) as error:
        parse_local_gateway_identity(payload, context=context())

    assert error.value.problem == problem


@pytest.mark.parametrize(
    "field", ["id", "type", "status", "timeStamp", "writeable", "values"]
)
def test_missing_required_local_envelope_fields_fail(field: str) -> None:
    payload = local_payload()
    payload.pop(field)

    with pytest.raises(IdentityError):
        parse_local_gateway_identity(payload, context=context())


def test_local_parser_does_not_retain_or_interpret_modules_and_live_values() -> None:
    payload = local_payload()
    payload["timeStamp"] = ""  # Allowed by the vendor schema; not an age clock.
    payload["values"] = [
        {
            "Tok": "example-module",
            "ModuleSerialNumber": 5.703731019068751e21,
            "ProductSerialNumber": "private-product-serial",
            "ProductName": "private-device-name",
            "futureModuleField": {"value": "private-reading"},
        }
    ]
    payload["unknownGatewayField"] = "private-extra-data"
    identity = parse_local_gateway_identity(payload, context=context())
    payload["gatewayId"] = "changed-after-parsing"

    assert identity.value == IDENTIFIER
    assert not hasattr(identity, "values")
    for private in (
        "private-product-serial",
        "private-device-name",
        "private-reading",
        "private-extra-data",
    ):
        assert private not in repr(identity)
    with pytest.raises(FrozenInstanceError):
        identity.value = "replacement"  # type: ignore[misc]


@pytest.mark.parametrize("generation", [-1, True, 1.0, "1"])
def test_connection_generation_is_a_nonnegative_integer(generation: object) -> None:
    with pytest.raises(ValueError, match="Invalid connection generation"):
        IdentityContext(LOCAL, OWNER, CONNECTION, generation)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": ""},
        {"connection_id": "untrusted\nconnection"},
        {"namespace": "local_system_gateway"},
    ],
)
def test_invalid_context_cannot_supply_provenance(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(context(), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"namespace": "pointt_gateway"},
        {"value": ""},
        {"value": 9007199254740993},
        {"plant": PlantKey("", "installation")},
        {"plant": PlantKey(OWNER, " installation ")},
    ],
)
def test_invalid_retained_references_cannot_participate_in_comparison(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        replace(GatewayReference(PLANT, POINTT, IDENTIFIER), **changes)


@pytest.mark.parametrize("namespace", [POINTT, LOCAL])
def test_same_namespace_matches_without_a_cross_source_contract(
    namespace: GatewayNamespace,
) -> None:
    result = match_gateway_identity(
        parse(namespace, IDENTIFIER),
        current_context=context(namespace),
        expected_plant=PLANT,
        references=[GatewayReference(PLANT, namespace, IDENTIFIER)],
    )

    assert result.status == IdentityMatchStatus.MATCHED
    assert result.plant is PLANT


@pytest.mark.parametrize(("first", "incoming"), [(POINTT, LOCAL), (LOCAL, POINTT)])
def test_cloud_first_and_local_first_require_explicit_comparability(
    first: GatewayNamespace, incoming: GatewayNamespace
) -> None:
    identity = parse(incoming, IDENTIFIER)
    reference = GatewayReference(PLANT, first, IDENTIFIER)
    unverified = match_gateway_identity(
        identity,
        current_context=context(incoming),
        expected_plant=PLANT,
        references=[reference],
    )
    verified = match_gateway_identity(
        identity,
        current_context=context(incoming),
        expected_plant=PLANT,
        references=[reference],
        verified_namespace_pairs=VERIFIED,
    )

    assert unverified.status == IdentityMatchStatus.UNVERIFIED
    assert unverified.reason == "unverified_namespace"
    assert unverified.plant is None
    assert verified.status == IdentityMatchStatus.MATCHED
    assert verified.plant is PLANT
    assert verified.plant.target_id != identity.value


@pytest.mark.parametrize(
    ("known", "incoming"),
    [
        ("00123", "123"),
        ("Gateway-A", "gateway-a"),
        ("9007199254740993", "9007199254740992"),
        ("Équipement", "E\u0301quipement"),
    ],
)
def test_no_normalization_can_make_different_identifiers_match(
    known: str, incoming: str
) -> None:
    result = match_gateway_identity(
        parse(LOCAL, incoming),
        current_context=context(),
        expected_plant=PLANT,
        references=[GatewayReference(PLANT, POINTT, known)],
        verified_namespace_pairs=VERIFIED,
    )

    assert result.status == IdentityMatchStatus.CONFLICT
    assert result.reason == "identity_mismatch"
    assert result.plant is None


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": 2},
        {"connection_id": "reconfigured-connection"},
        {"owner_id": "another-owner"},
        {"namespace": POINTT},
    ],
)
def test_late_responses_cannot_confirm_a_changed_connection(
    changes: dict[str, object],
) -> None:
    identity = parse(LOCAL, IDENTIFIER)
    current = replace(context(), **changes)
    result = match_gateway_identity(
        identity,
        current_context=current,
        expected_plant=PLANT,
        references=[GatewayReference(PLANT, POINTT, IDENTIFIER)],
        verified_namespace_pairs=VERIFIED,
    )

    assert result.status == IdentityMatchStatus.UNVERIFIED
    assert result.reason == "stale_observation"
    assert result.plant is None


def test_selected_plant_must_belong_to_the_request_owner() -> None:
    foreign = PlantKey("other-owner", PLANT.target_id)
    result = match_gateway_identity(
        parse(LOCAL, IDENTIFIER),
        current_context=context(),
        expected_plant=foreign,
        references=[GatewayReference(foreign, LOCAL, IDENTIFIER)],
    )

    assert result.status == IdentityMatchStatus.OWNER_CONFLICT
    assert result.plant is None


@pytest.mark.parametrize("reverse", [False, True])
def test_existing_ownership_conflicts_are_not_hidden_by_a_matching_target(
    reverse: bool,
) -> None:
    references = [
        GatewayReference(PLANT, POINTT, IDENTIFIER),
        GatewayReference(PlantKey("other-owner", "other-plant"), POINTT, IDENTIFIER),
    ]
    if reverse:
        references.reverse()
    result = match_gateway_identity(
        parse(LOCAL, IDENTIFIER),
        current_context=context(),
        expected_plant=PLANT,
        references=references,
        verified_namespace_pairs=VERIFIED,
    )

    assert result.status == IdentityMatchStatus.OWNER_CONFLICT
    assert result.plant is None


@pytest.mark.parametrize("reverse", [False, True])
def test_multiple_installations_with_one_identifier_are_ambiguous(
    reverse: bool,
) -> None:
    references = [
        GatewayReference(PLANT, LOCAL, IDENTIFIER),
        GatewayReference(PlantKey(OWNER, "other-plant"), LOCAL, IDENTIFIER),
    ]
    if reverse:
        references.reverse()
    result = match_gateway_identity(
        parse(LOCAL, IDENTIFIER),
        current_context=context(),
        expected_plant=PLANT,
        references=references,
    )

    assert result.status == IdentityMatchStatus.AMBIGUOUS
    assert result.plant is None


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_references_of_selected_plant_cannot_be_ignored(
    reverse: bool,
) -> None:
    references = [
        GatewayReference(PLANT, POINTT, IDENTIFIER),
        GatewayReference(PLANT, LOCAL, "different-gateway"),
    ]
    if reverse:
        references.reverse()
    result = match_gateway_identity(
        parse(LOCAL, IDENTIFIER),
        current_context=context(),
        expected_plant=PLANT,
        references=references,
        verified_namespace_pairs=VERIFIED,
    )

    assert result.status == IdentityMatchStatus.CONFLICT
    assert result.plant is None


def test_multiple_agreeing_references_for_one_plant_are_not_multiple_plants() -> None:
    result = match_gateway_identity(
        parse(LOCAL, IDENTIFIER),
        current_context=context(),
        expected_plant=PLANT,
        references=[
            GatewayReference(PLANT, POINTT, IDENTIFIER),
            GatewayReference(PLANT, LOCAL, IDENTIFIER),
            GatewayReference(PLANT, POINTT, IDENTIFIER),
        ],
        verified_namespace_pairs=VERIFIED,
    )

    assert result.status == IdentityMatchStatus.MATCHED
    assert result.plant is PLANT


def test_unknown_namespace_does_not_turn_unrelated_references_into_aliases() -> None:
    result = match_gateway_identity(
        parse(LOCAL, IDENTIFIER),
        current_context=context(),
        expected_plant=PLANT,
        references=[
            GatewayReference(PLANT, POINTT, "unverified-other-namespace"),
            GatewayReference(PLANT, LOCAL, IDENTIFIER),
        ],
    )

    assert result.status == IdentityMatchStatus.MATCHED


@pytest.mark.parametrize("include_wrong_target", [False, True])
def test_matching_a_different_plant_never_changes_the_selected_target(
    include_wrong_target: bool,
) -> None:
    references = [GatewayReference(PlantKey(OWNER, "another-plant"), LOCAL, IDENTIFIER)]
    if include_wrong_target:
        references.append(GatewayReference(PLANT, LOCAL, "wrong-identifier"))
    result = match_gateway_identity(
        parse(LOCAL, IDENTIFIER),
        current_context=context(),
        expected_plant=PLANT,
        references=references,
    )

    assert result.status == (
        IdentityMatchStatus.CONFLICT
        if include_wrong_target
        else IdentityMatchStatus.UNVERIFIED
    )
    assert result.plant is None


def test_empty_inventory_is_unverified() -> None:
    result = match_gateway_identity(
        parse(LOCAL, IDENTIFIER),
        current_context=context(),
        expected_plant=PLANT,
        references=[],
    )

    assert result.status == IdentityMatchStatus.UNVERIFIED
    assert result.reason == "target_missing"


def test_decisions_and_evidence_representations_do_not_expose_private_identifiers() -> (
    None
):
    identity = parse(LOCAL, IDENTIFIER)
    reference = GatewayReference(PLANT, LOCAL, IDENTIFIER)
    result = match_gateway_identity(
        identity,
        current_context=context(),
        expected_plant=PLANT,
        references=[reference],
    )

    for item in (identity, identity.context, reference, result):
        for value in (IDENTIFIER, OWNER, CONNECTION, PLANT.target_id):
            assert value not in repr(item)
            assert value not in str(item)


def test_matching_keeps_existing_device_and_functional_group_keys() -> None:
    model = PlantModel.from_pointt_gateway(OWNER, Gateway(IDENTIFIER))
    before = model.bind_resource("/heatingCircuits/hc3/operationMode")
    reference = GatewayReference(model.key, POINTT, IDENTIFIER)
    result = match_gateway_identity(
        parse(LOCAL, IDENTIFIER),
        current_context=context(),
        expected_plant=model.key,
        references=[reference],
        verified_namespace_pairs=VERIFIED,
    )

    assert result.plant is model.key
    assert model.bind_resource("/heatingCircuits/hc3/operationMode") == before
    assert model.gateway_device.identifier == IDENTIFIER
    assert len(model.devices) == 1


def test_identity_module_can_parse_and_compare_without_home_assistant() -> None:
    package = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
    script = """
import importlib.abc
import pathlib
import sys
import types

class NoHomeAssistant(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "homeassistant" or fullname.startswith("homeassistant."):
            raise AssertionError("Identity parser imported Home Assistant")

sys.meta_path.insert(0, NoHomeAssistant())
root = pathlib.Path(sys.argv[1])
for name, directory in [
    ("custom_components", root.parent),
    ("custom_components.bosch_buderus_heating", root),
]:
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package

from custom_components.bosch_buderus_heating.gateway_identity import (
    GatewayNamespace, GatewayReference, IdentityContext, IdentityMatchStatus,
    match_gateway_identity, parse_pointt_gateway_identity,
)
from custom_components.bosch_buderus_heating.plant_model import PlantKey
context = IdentityContext(GatewayNamespace.POINTT, "owner", "connection", 1)
identity = parse_pointt_gateway_identity({"deviceId": "000001"}, context=context)
plant = PlantKey("owner", "stable-plant")
result = match_gateway_identity(
    identity, current_context=context, expected_plant=plant,
    references=[GatewayReference(plant, GatewayNamespace.POINTT, "000001")],
)
assert result.status == IdentityMatchStatus.MATCHED
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(package)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
