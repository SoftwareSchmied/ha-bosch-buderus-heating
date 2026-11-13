"""Local storage distinguishes reported scopes, read evidence and first trust."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from custom_components.bosch_buderus_heating.configuration import (
    ConfigurationError,
    load_configuration,
)
from custom_components.bosch_buderus_heating.configuration_migration import (
    convert_legacy_configuration,
)
from custom_components.bosch_buderus_heating.local_credentials import (
    LocalCredentialError,
    LocalCredentials,
    StoredLocalAccess,
    TokenOrigin,
    created_local_token,
    import_local_token,
    load_local_access,
)
from custom_components.bosch_buderus_heating.local_probe import LocalEndpoint
from tests.test_configuration import OWNER, add_local, legacy, proposed

PRIVATE_TOKEN = "synthetic-local-secret"
PRIVATE_ID = "000123"
HOST = "gateway.example.invalid"


def access_record(*, created=False):
    credentials = (
        created_local_token(
            {
                "access_token": PRIVATE_TOKEN,
                "token_type": "Bearer",
                "scope": "open_api.read unknown.scope",
            }
        )
        if created
        else import_local_token(PRIVATE_TOKEN)
    )
    return StoredLocalAccess(
        credentials,
        LocalEndpoint(HOST),
        "a" * 64,
        "b" * 64 if created else None,
        PRIVATE_ID,
        1234.5,
    )


def configured_access(*, created=False):
    data, options = proposed()
    target = add_local(data, options)
    data["connections"]["local"].update(access_record(created=created).to_data())
    return data, options, target


@pytest.mark.parametrize("created", [False, True])
def test_provenance_roundtrip_is_private_and_does_not_invent_scope_or_ownership(
    created,
):
    access = access_record(created=created)
    raw = access.to_data()
    restored = load_local_access(json.loads(json.dumps(raw)))
    assert restored == access
    credentials = raw["credentials"]
    assert credentials["origin"] == ("created" if created else "imported")
    assert credentials["reported_scope"] == (
        ["open_api.read", "unknown.scope"] if created else None
    )
    assert "scope" not in credentials and "token_id" not in credentials
    assert "expires_at" not in credentials
    for value in (access, access.credentials, restored):
        assert all(
            secret not in repr(value)
            for secret in (PRIVATE_TOKEN, PRIVATE_ID, HOST, "a" * 64, "unknown.scope")
        )
    raw["credentials"]["token"] = "changed"
    assert restored.credentials.token == PRIVATE_TOKEN
    with pytest.raises(FrozenInstanceError):
        restored.gateway_id = "other"


@pytest.mark.parametrize(
    "value", [None, "", " ", "x y", "x\r\nY:z", "ä", "x" * 8193, False, 123]
)
def test_invalid_tokens_have_no_sensitive_error(value):
    with pytest.raises(LocalCredentialError, match="invalid_local_token"):
        import_local_token(value)


@pytest.mark.parametrize(
    "scope",
    [
        None,
        [],
        "",
        "open_api.write",
        "open_api.read open_api.read",
        " open_api.read",
        "open_api.read ",
        "open_api.read\tother",
        'open_api.read "bad"',
        "open_api.read " + "a" * 2048,
    ],
)
def test_created_response_requires_documented_read_scope(scope):
    with pytest.raises(LocalCredentialError):
        created_local_token(
            {"access_token": PRIVATE_TOKEN, "token_type": "Bearer", "scope": scope}
        )


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"token_type": "Basic"},
        {"token_type": 1},
        {"token_type": "Bearer", "scope": "open_api.read"},
    ],
)
def test_invalid_creation_response_is_not_a_credential(value):
    with pytest.raises(LocalCredentialError):
        created_local_token(value)


def test_unknown_response_fields_cannot_claim_token_ownership_or_enable_writes():
    payload = {
        "access_token": PRIVATE_TOKEN,
        "token_type": "bearer",
        "scope": "open_api.read open_api.write",
        "token_id": "unproven",
        "password": "private",
        "expires_at": 999,
    }
    result = created_local_token(payload).to_data()
    assert result == {
        "token": PRIVATE_TOKEN,
        "token_type": "Bearer",
        "origin": "created",
        "reported_scope": ["open_api.read", "open_api.write"],
    }
    assert "unproven" not in json.dumps(result) and "password" not in result


@pytest.mark.parametrize(
    ("origin", "scope"),
    [
        ("imported", None),
        (TokenOrigin.IMPORTED, ("open_api.read",)),
        (TokenOrigin.CREATED, None),
        (TokenOrigin.CREATED, ["open_api.read"]),
        (TokenOrigin.CREATED, (1,)),
    ],
)
def test_direct_credentials_cannot_mix_imported_and_issuer_evidence(origin, scope):
    with pytest.raises(LocalCredentialError):
        LocalCredentials(PRIVATE_TOKEN, origin, scope)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resource_sha256", "A" * 64),
        ("resource_sha256", "a" * 63),
        ("resource_sha256", None),
        ("authentication_sha256", "b"),
        ("verified_at", True),
        ("verified_at", -1),
        ("verified_at", float("nan")),
        ("verified_at", float("inf")),
        ("verified_at", "soon"),
        ("verified_at", 10**400),
    ],
)
def test_invalid_persisted_evidence_is_rejected(field, value):
    with pytest.raises(ValueError):
        replace(access_record(), **{field: value})


def test_created_token_requires_authentication_certificate_evidence():
    with pytest.raises(LocalCredentialError, match="authentication_binding_required"):
        replace(access_record(created=True), authentication_sha256=None)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("credentials", "origin", "unknown"),
        ("credentials", "token_type", "Basic"),
        ("credentials", "reported_scope", "open_api.read"),
        ("credentials", "reported_scope", [1]),
        ("credentials", "reported_scope", ["open_api.read"]),
        ("credentials", "scope", ["open_api.read"]),
        ("credentials", "token_id", "not-proven"),
        ("endpoint", "host", "https://private.local"),
        ("endpoint", "host", "Gateway.Example.Invalid"),
        ("endpoint", "host", 7),
        ("endpoint", "port", 9442),
        ("endpoint", "port", True),
        ("endpoint", "transport", "http"),
        ("local_access", "version", 2),
        ("local_access", "version", True),
        ("certificate_binding", "algorithm", "spki"),
        ("certificate_binding", "resources", "b"),
        ("read_evidence", "method", "imported_claim"),
        ("read_evidence", "gateway_id", 123),
        ("read_evidence", "gateway_id", ""),
        ("read_evidence", "verified_at", None),
    ],
)
def test_new_storage_fails_closed_without_rewriting_input(section, field, value):
    data, options, _ = configured_access()
    record = data["connections"]["local"]
    nested = (
        record
        if section in ("credentials", "endpoint", "local_access")
        else record["local_access"]
    )
    nested[section][field] = value
    before = deepcopy((data, options))
    with pytest.raises(ConfigurationError):
        load_configuration(OWNER, data, options, minor_version=2)
    assert (data, options) == before


@pytest.mark.parametrize("field", ["credentials", "endpoint", "local_access"])
def test_missing_storage_sections_are_not_inferred(field):
    raw = access_record().to_data()
    raw.pop(field)
    with pytest.raises(ValueError):
        load_local_access(raw)


def test_new_storage_requires_explicit_minor_version_and_matches_binding_identity():
    data, options, _target = configured_access()
    with pytest.raises(ConfigurationError, match="unsupported_local_storage_version"):
        load_configuration(OWNER, data, options)
    snapshot = load_configuration(OWNER, data, options, minor_version=2)
    assert snapshot.to_data() == data and snapshot.to_options() == options
    data["connections"]["local"]["local_access"]["read_evidence"]["gateway_id"] = (
        "different"
    )
    with pytest.raises(ConfigurationError, match="local_identity_conflict"):
        load_configuration(OWNER, data, options, minor_version=2)


def test_old_preview_is_retained_but_cannot_be_used_as_trust_or_fresh_read_evidence():
    data, options = proposed()
    add_local(data, options)
    for minor in (1, 2):
        snapshot = load_configuration(OWNER, data, options, minor_version=minor)
        assert snapshot.to_data() == data
        with pytest.raises(LocalCredentialError, match="local_revalidation_required"):
            load_local_access(snapshot.connections["local"].to_data())
    with pytest.raises(ConfigurationError, match="unsupported_minor_version"):
        convert_legacy_configuration(OWNER, legacy(), {}, minor_version=2)


def test_cloud_only_storage_stays_unchanged_in_both_supported_minor_versions():
    data, options = proposed()
    for minor in (1, 2):
        result = load_configuration(OWNER, data, options, minor_version=minor)
        assert result.to_data() == data and result.to_options() == options
