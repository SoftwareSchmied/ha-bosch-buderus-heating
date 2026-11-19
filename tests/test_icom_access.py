"""The stored iCom access, its identity rule and the circuit paths."""

import pytest

from custom_components.bosch_buderus_heating.icom_access import (
    ICOM_TRANSPORT,
    IcomAccessError,
    comparable_id,
    gateway_ids_match,
    icom_connection_data,
    is_icom_connection,
    load_icom_access,
)
from custom_components.bosch_buderus_heating.icom_crypto import derive_key
from custom_components.bosch_buderus_heating.icom_plan import (
    UUID_PATH,
    circuit_ids,
    readings,
)
from custom_components.bosch_buderus_heating.observations import (
    PAIR_CATALOG,
    pair_for_path,
)


def stored(**changes):
    data = icom_connection_data(
        host="192.168.1.20",
        gateway_password="abcd-efgh-ijkl-mnop",
        private_password="private",
        gateway_id="000123",
        verified_at=5.5,
    )
    for key, value in changes.items():
        section, _, name = key.partition("__")
        if name:
            data[section][name] = value
        else:
            data[section] = value
    return data


def test_a_stored_access_reads_back_and_derives_its_key():
    data = stored()
    assert is_icom_connection(data)
    assert data["endpoint"] == {
        "host": "192.168.1.20",
        "port": 80,
        "transport": ICOM_TRANSPORT,
    }
    access = load_icom_access(data)
    assert (access.host, access.port, access.gateway_id) == (
        "192.168.1.20",
        80,
        "000123",
    )
    assert access.verified_at == 5.5
    assert access.key == derive_key("abcdefghijklmnop", "private")
    # The secrets never appear in the representation.
    assert "private" not in repr(access) and "abcd" not in repr(access)


def test_the_sticker_password_is_stored_without_dashes():
    assert stored()["credentials"]["gateway_password"] == "abcdefghijklmnop"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"icom_access__version": 2}, "unsupported_icom_access_version"),
        ({"icom_access__version": True}, "unsupported_icom_access_version"),
        ({"endpoint__transport": "https_local_api"}, "invalid_icom_endpoint"),
        ({"endpoint__host": "a/b"}, "invalid_icom_endpoint"),
        ({"endpoint__host": "user@host"}, "invalid_icom_endpoint"),
        ({"endpoint__host": ""}, "invalid_icom_endpoint"),
        ({"endpoint__port": 0}, "invalid_icom_endpoint"),
        ({"endpoint__port": "80"}, "invalid_icom_endpoint"),
        ({"credentials__gateway_password": ""}, "invalid_gateway_password"),
        ({"credentials__private_password": None}, "invalid_private_password"),
        ({"icom_access__gateway_id": "a b"}, "invalid_icom_identity"),
        ({"icom_access__gateway_id": "--"}, "invalid_icom_identity"),
        ({"icom_access__verified_at": float("nan")}, "invalid_icom_verification_time"),
        ({"icom_access__verified_at": "yesterday"}, "invalid_icom_verification_time"),
    ],
)
def test_an_invalid_stored_access_is_named_by_a_fixed_code(changes, code):
    with pytest.raises(IcomAccessError, match=code):
        load_icom_access(stored(**changes))


def test_a_record_that_is_not_an_icom_access_is_not_loaded():
    with pytest.raises(IcomAccessError, match="icom_revalidation_required"):
        load_icom_access({"endpoint": {}})
    with pytest.raises(IcomAccessError, match="invalid_icom_access"):
        load_icom_access({"icom_access": 5})
    assert not is_icom_connection({"local_access": {}})


@pytest.mark.parametrize(
    ("local", "expected", "match"),
    [
        ("000123", "000123", True),
        ("0001-23", "000123", True),
        ("AB-12-cd", "ab12CD", True),
        ("000124", "000123", False),
        ("", "000123", False),
        (None, "000123", False),
        (123456, "123456", False),
        ("--", "--", False),
        ("12", "12", False),
        ("x" * 2000, "x" * 2000, False),
    ],
)
def test_the_local_gateway_id_must_equal_the_cloud_one(local, expected, match):
    assert gateway_ids_match(local, expected) is match


def test_comparison_ignores_separators_and_case_only():
    assert comparable_id("A-1_b 2") == "a1b2"
    assert comparable_id("a") is None


def document(*ids):
    return {"id": "/x", "references": [{"id": item} for item in ids]}


def test_circuits_come_from_the_directory_members():
    found = circuit_ids(
        document(
            "/heatingCircuits/hc10",
            "/heatingCircuits/hc2",
            "/heatingCircuits/hc1",
            "/heatingCircuits/hc1",
            "/heatingCircuits/hc1/sub",
            "/heatingCircuits/other",
            "/heatingCircuits/dhw1",
            "/elsewhere/hc3",
            "/heatingCircuits/hc",
            "/heatingCircuits/hc1234",
        ),
        "/heatingCircuits",
        "hc",
    )
    assert found == ("hc1", "hc2", "hc10")


@pytest.mark.parametrize(
    "value", [None, [], {}, {"references": 5}, {"references": [5, None, {"id": 3}]}]
)
def test_an_unusable_directory_lists_no_circuit(value):
    assert circuit_ids(value, "/heatingCircuits", "hc") == ()


def test_circuit_count_is_bounded():
    many = document(*(f"/heatSources/hs{n}" for n in range(1, 40)))
    assert len(circuit_ids(many, "/heatSources", "hs")) == 16


def test_readings_are_only_paired_paths_and_expand_per_circuit():
    requests = readings(["hc1", "hc2"], ["dhw1"], ["hs1"]).requests
    paths = [request.path for request in requests]
    assert len(paths) == len(set(paths))
    assert all(pair_for_path(path) is not None for path in paths)
    assert "/heatingCircuits/hc2/overallStatus" in paths
    assert "/dhwCircuits/dhw1/actualTemp" in paths
    assert "/heatSources/hs1/numberOfStarts" in paths
    assert "/system/sensors/temperatures/outdoor_t1" in paths
    # Never the identity path or any directory.
    assert UUID_PATH not in paths and "/heatingCircuits" not in paths


def test_without_circuits_the_placeholder_paths_are_left_out():
    paths = [request.path for request in readings().requests]
    assert not any("{" in path for path in paths)
    assert not any(path.startswith("/heatingCircuits/") for path in paths)
    assert "/heatSources/actualModulation" in paths
    assert len(PAIR_CATALOG) > len(paths)
