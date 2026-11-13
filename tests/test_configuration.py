"""Storage proposals preserve ownership and settings without activating a source."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

import pytest
from hypothesis import given
from hypothesis import strategies as st

from custom_components.bosch_buderus_heating import configuration as storage
from custom_components.bosch_buderus_heating.bacon.models import account_key, device_key
from custom_components.bosch_buderus_heating.configuration import (
    ConfigurationError,
    json_object,
    load_configuration,
)
from custom_components.bosch_buderus_heating.configuration_migration import (
    convert_legacy_configuration,
)
from custom_components.bosch_buderus_heating.data import tokens_from_data

OWNER = "private-owner"
SUBJECT = "synthetic-account"


def token(subject=SUBJECT):
    payload = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode()).decode()
    return f"synthetic.{payload.rstrip('=')}.unsigned"


def legacy(gateways=None, devices=None):
    return {
        "brand": "buderus",
        "gateway_ids": ["000123"] if gateways is None else gateways,
        "bacon_device_ids": [] if devices is None else devices,
        "bacon_account": account_key(SUBJECT),
        "access_token": token(),
        "refresh_token": "synthetic-refresh",
        "expires_at": 0.0,
        "scope": ["bacon", "unknown-scope"],
        "token_type": "Bearer",
    }


def proposed():
    snapshot = convert_legacy_configuration(OWNER, legacy(), {})
    return snapshot.to_data(), snapshot.to_options()


def add_local(data, options, *, kind="pointt_object"):
    target_id = next(iter(data["targets"]))
    target = data["targets"][target_id]
    identity = {"namespace": "local_system_gateway", "value": "000123"}
    target["identities"].append({**identity, "evidence": "synthetic_reference"})
    target["kind"] = kind
    data["connections"]["local"] = {
        "kind": "k40_local",
        "auth_epoch": 0,
        "config_revision": 1,
        "credentials": {"token": "synthetic-local", "scope": ["open_api.read"]},
        "endpoint": {
            "host": "mx400.example.invalid",
            "port": 443,
            "transport": "https_local_api",
        },
    }
    data["bindings"]["local"] = {
        "connection_id": "local",
        "target_id": target_id,
        "adapter": "k40_local",
        "locator": identity,
        "link_evidence": {"basis": "synthetic_reference", "contract": "draft"},
    }
    options["connections"]["local"] = {"enabled": True}
    options["bindings"]["local"] = {"enabled": True}
    options["targets"][target_id]["read_order"].insert(0, "local")
    return target_id


@pytest.mark.parametrize(
    "gateways,devices",
    [
        (["000123"], []),
        (["gateway-b", "gateway-a"], []),
        ([], ["softener-1"]),
        (["gateway-a", "wallbox-b"], ["softener-1", "air-7"]),
    ],
)
def test_conversion_keeps_one_cloud_login_and_original_identity_formats(
    gateways, devices
):
    original = legacy(gateways, devices)
    before = deepcopy(original)
    options = {
        "polling_profile": "cloud_friendly",
        "fault_notifications": {"000123": False},
    }
    previous_options = deepcopy(options)
    snapshot = convert_legacy_configuration(OWNER, original, options)
    data, settings = snapshot.to_data(), snapshot.to_options()

    assert original == before and options == previous_options
    assert snapshot.owner_id == OWNER
    assert snapshot.version == 4 and snapshot.minor_version == 1
    assert len(snapshot.connections) == 1
    assert len(snapshot.targets) == len(gateways) + len(devices)
    connection = next(iter(snapshot.connections.values())).to_data()
    assert tokens_from_data(connection["credentials"]) == tokens_from_data(original)
    assert connection["account_evidence"] is None
    assert connection["legacy_bacon_account"] == original["bacon_account"]
    assert {b.connection_id for b in snapshot.bindings.values()} == set(
        snapshot.connections
    )
    old_gateways, old_devices = set(), set()
    for target in snapshot.targets.values():
        record = target.to_data()["legacy_identity"]
        if target.kind == "pointt_object":
            old_gateways.add(record["gateway_id"])
        else:
            old_devices.add(record["serial"])
            assert record["entity_prefix"] == device_key(SUBJECT, record["serial"])
            assert "gateway" not in json.dumps(target.to_data())
    assert old_gateways == set(gateways) and old_devices == set(devices)
    assert settings["fault_notifications"] == options["fault_notifications"]
    assert (
        next(iter(settings["connections"].values()))["polling_profile"]
        == "cloud_friendly"
    )
    assert all(value["enabled"] for value in settings["bindings"].values())
    assert all(c.kind == "singlekey_cloud" for c in snapshot.connections.values())
    assert "entry_id" not in data and "unique_id" not in data and "title" not in data


def test_ids_are_deterministic_scoped_and_independent_of_token_order_and_brand():
    old = legacy(["01", "1", "gateway:two", "9007199254740993"], ["device-1"])
    first = convert_legacy_configuration(OWNER, old, {})
    old["gateway_ids"].reverse()
    old["refresh_token"] = "rotated-refresh"
    old["access_token"] = "new-expired-token-without-routing"
    old["brand"] = "bosch"
    second = convert_legacy_configuration(OWNER, old, {})
    assert first.targets.keys() == second.targets.keys()
    assert first.bindings.keys() == second.bindings.keys()
    assert first.connections.keys() == second.connections.keys()
    foreign = convert_legacy_configuration("other-owner", old, {})
    assert first.targets.keys().isdisjoint(foreign.targets)
    assert first.connections.keys().isdisjoint(foreign.connections)
    expanded = deepcopy(old)
    expanded["gateway_ids"].append("new-gateway")
    assert (
        first.targets.keys()
        < convert_legacy_configuration(OWNER, expanded, {}).targets.keys()
    )


@pytest.mark.parametrize(
    "access", ["unstructured", "x.@@@.x", token("other-account"), token(None)]
)
def test_unresolved_routing_retains_standalone_identity_without_guessing(access):
    old = legacy([], ["air-1"])
    old["access_token"] = access
    snapshot = convert_legacy_configuration(OWNER, old, {})
    target = next(iter(snapshot.targets.values())).to_data()
    assert target["legacy_identity"] == {
        "format": "bacon_v3",
        "account_key": account_key(SUBJECT),
        "serial": "air-1",
        "entity_prefix": None,
    }
    assert (
        next(iter(snapshot.connections.values())).to_data()["account_evidence"] is None
    )


def test_optional_old_token_fields_have_the_existing_effective_defaults():
    old = legacy()
    for key in (
        "refresh_token",
        "scope",
        "token_type",
        "bacon_account",
        "bacon_device_ids",
    ):
        old.pop(key)
    result = convert_legacy_configuration(OWNER, old, {})
    credentials = next(iter(result.connections.values())).to_data()["credentials"]
    assert tokens_from_data(credentials) == tokens_from_data(old)


@pytest.mark.parametrize("profile", [None, False, 7, [], {}, "unexpected"])
def test_invalid_legacy_polling_option_preserves_value_and_effective_default(profile):
    result = convert_legacy_configuration(OWNER, legacy(), {"polling_profile": profile})
    options = result.to_options()
    assert options["legacy_options"]["polling_profile"] == profile
    assert next(iter(options["connections"].values()))["polling_profile"] == "standard"


def test_reserved_old_fields_and_unknown_nested_options_are_not_overwritten():
    old = legacy()
    old.update({"connections": {"old": True}, "legacy_extra": ["keep"]})
    opts = {
        "connections": ["old"],
        "targets": 5,
        "bindings": False,
        "legacy_options": {"old": 1},
        "unknown": {"nested": [1, False, None]},
    }
    result = convert_legacy_configuration(OWNER, old, opts)
    assert result.to_data()["legacy_extra"] == {
        "connections": {"old": True},
        "legacy_extra": ["keep"],
    }
    assert result.to_options()["legacy_options"] == {
        k: v for k, v in opts.items() if k != "unknown"
    }
    assert result.to_options()["unknown"] == opts["unknown"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("brand", "other"),
        ("brand", None),
        ("gateway_ids", None),
        ("gateway_ids", ["a", "a"]),
        ("gateway_ids", [1]),
        ("gateway_ids", [" padded"]),
        ("gateway_ids", []),
        ("bacon_device_ids", ["x", "x"]),
        ("bacon_device_ids", ["bad/id"]),
        ("access_token", ""),
        ("access_token", True),
        ("refresh_token", 1),
        ("expires_at", True),
        ("expires_at", None),
        ("expires_at", float("nan")),
        ("expires_at", float("inf")),
        ("expires_at", 10**400),
        ("scope", "read"),
        ("scope", [1]),
        ("token_type", None),
        ("bacon_account", False),
    ],
)
def test_invalid_legacy_data_fails_without_partial_mutation(field, value):
    old = legacy()
    old[field] = value
    encoded_before = json.dumps(old)
    with pytest.raises(ConfigurationError):
        convert_legacy_configuration(OWNER, old, {})
    assert json.dumps(old) == encoded_before


@pytest.mark.parametrize("version", [1, 2, 4, 5, True, "3"])
def test_converter_cannot_replace_preview_migration_or_downgrade(version):
    with pytest.raises(ConfigurationError, match="unsupported_legacy_version"):
        convert_legacy_configuration(OWNER, legacy(), {}, version=version)


@pytest.mark.parametrize("minor", [0, 3, True, "1"])
def test_unknown_minor_versions_are_not_silently_rewritten(minor):
    data, options = proposed()
    with pytest.raises(ConfigurationError, match="unsupported_minor_version"):
        convert_legacy_configuration(OWNER, legacy(), {}, minor_version=minor)
    if minor == 3:
        return  # a newer minor version is read, see below
    with pytest.raises(ConfigurationError, match="unsupported_minor_version"):
        load_configuration(OWNER, data, options, minor_version=minor)


@pytest.mark.parametrize("minor", [3, 7])
def test_a_newer_minor_version_is_read_and_keeps_its_unknown_data(minor):
    data, options = proposed()
    data["later_optional_data"] = {"kept": True}
    options["later_optional_option"] = 1
    snapshot = load_configuration(OWNER, data, options, minor_version=minor)
    # Its own version stays, so saving never lowers it.
    assert snapshot.minor_version == minor
    assert snapshot.to_data() == data and snapshot.to_options() == options
    assert (
        snapshot.connections.keys()
        == load_configuration(OWNER, data, options, minor_version=2).connections.keys()
    )


def test_snapshot_and_every_export_are_detached_and_read_only():
    data, options = proposed()
    data["future"] = {"list": ["value"]}
    snapshot = load_configuration(
        OWNER, MappingProxyType(data), MappingProxyType(options)
    )
    original = snapshot.to_data()
    data["future"]["list"].append("outside")
    snapshot.to_data()["future"]["list"].append("export")
    snapshot.to_options()["connections"].clear()
    next(iter(snapshot.connections.values())).to_data()["credentials"].clear()
    next(iter(snapshot.targets.values())).to_data()["identities"].clear()
    next(iter(snapshot.bindings.values())).to_data()["locator"].clear()
    assert snapshot.to_data() == original
    assert snapshot.to_options() == options
    with pytest.raises(TypeError):
        snapshot.connections["new"] = None
    with pytest.raises(FrozenInstanceError):
        snapshot.version = 99
    assert (
        load_configuration(OWNER, snapshot.to_data(), snapshot.to_options()) == snapshot
    )


def test_hybrid_and_local_only_need_no_cloud_login_or_device_guess():
    data, options = proposed()
    target_id = add_local(data, options, kind="local_installation")
    hybrid = load_configuration(OWNER, data, options)
    assert len(hybrid.connections) == 2 and len(hybrid.targets) == 1
    for key in tuple(data["connections"]):
        if key != "local":
            data["connections"].pop(key)
            options["connections"].pop(key)
    for key in tuple(data["bindings"]):
        if key != "local":
            data["bindings"].pop(key)
            options["bindings"].pop(key)
    options["targets"][target_id]["read_order"] = ["local"]
    target = data["targets"][target_id]
    target["identities"] = [
        i for i in target["identities"] if i["namespace"] == "local_system_gateway"
    ]
    target["legacy_identity"] = None
    local = load_configuration(OWNER, data, options)
    assert len(local.connections) == 1
    assert "access_token" not in json.dumps(local.to_data())


def test_last_connection_can_be_removed_without_losing_targets_or_identity():
    data, options = proposed()
    target_state = deepcopy(data["targets"])
    for collection in ("connections", "bindings"):
        data[collection].clear()
        options[collection].clear()
    for target in options["targets"].values():
        target["read_order"] = []
    result = load_configuration(OWNER, data, options)
    assert result.to_data()["targets"] == target_state
    assert not result.connections and not result.bindings
    assert "synthetic-refresh" not in json.dumps(result.to_data())


def test_unknown_adapter_records_roundtrip_without_becoming_known_transports():
    data, options = proposed()
    connection_id = next(iter(data["connections"]))
    binding_id = next(iter(data["bindings"]))
    data["connections"][connection_id]["kind"] = "future_transport"
    data["connections"][connection_id]["future_setting"] = {"new": [1, 2]}
    data["bindings"][binding_id]["adapter"] = "future_adapter"
    snapshot = load_configuration(OWNER, data, options)
    assert snapshot.to_data() == data
    assert snapshot.connections[connection_id].kind == "future_transport"
    assert snapshot.bindings[binding_id].adapter == "future_adapter"


@pytest.mark.parametrize(
    "change,code",
    [
        ("dangling_connection", "dangling_binding"),
        ("dangling_target", "dangling_binding"),
        ("locator", "binding_identity_conflict"),
        ("adapter", "adapter_conflict"),
        ("duplicate_binding", "duplicate_binding"),
        ("duplicate_target", "competing_targets"),
        ("duplicate_identity", "duplicate_identity"),
        ("two_ids_one_namespace", "ambiguous_identity"),
        ("legacy_gateway", "legacy_identity_conflict"),
        ("missing_options", "option_reference_conflict"),
        ("foreign_option", "option_reference_conflict"),
        ("missing_order", "invalid_read_order"),
        ("foreign_order", "invalid_read_order"),
        ("duplicate_order", "invalid_read_order"),
        ("enabled_integer", "invalid_enabled"),
        ("profile", "invalid_polling_profile"),
        ("revision", "invalid_counter"),
        ("epoch", "invalid_counter"),
    ],
)
def test_invalid_graph_never_silently_redirects_or_discards_references(change, code):
    data, options = proposed()
    cid = next(iter(data["connections"]))
    tid = next(iter(data["targets"]))
    bid = next(iter(data["bindings"]))
    binding, target = data["bindings"][bid], data["targets"][tid]
    if change == "dangling_connection":
        binding["connection_id"] = "elsewhere"
    elif change == "dangling_target":
        binding["target_id"] = "elsewhere"
    elif change == "locator":
        binding["locator"]["value"] = "elsewhere"
    elif change == "adapter":
        binding["adapter"] = "k40_local"
    elif change == "duplicate_binding":
        data["bindings"]["copy"] = deepcopy(binding)
    elif change == "duplicate_target":
        data["targets"]["copy"] = deepcopy(target)
    elif change == "duplicate_identity":
        target["identities"].append(deepcopy(target["identities"][0]))
    elif change == "two_ids_one_namespace":
        target["identities"].append({**target["identities"][0], "value": "other"})
    elif change == "legacy_gateway":
        target["legacy_identity"]["gateway_id"] = "other"
    elif change == "missing_options":
        options["connections"].pop(cid)
    elif change == "foreign_option":
        options["bindings"]["elsewhere"] = {"enabled": True}
    elif change == "missing_order":
        options["targets"][tid]["read_order"] = []
    elif change == "foreign_order":
        options["targets"][tid]["read_order"] = ["elsewhere"]
    elif change == "duplicate_order":
        options["targets"][tid]["read_order"] *= 2
    elif change == "enabled_integer":
        options["bindings"][bid]["enabled"] = 1
    elif change == "profile":
        options["connections"][cid]["polling_profile"] = "fast"
    elif change == "revision":
        data["revision"] = True
    elif change == "epoch":
        data["connections"][cid]["auth_epoch"] = -1
    before = deepcopy((data, options))
    with pytest.raises(ConfigurationError, match=f"^{code}$"):
        load_configuration(OWNER, data, options)
    assert (data, options) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("port", 0),
        ("port", 65536),
        ("port", True),
        ("port", "443"),
        ("host", "user@host"),
        ("host", "host/path"),
        ("host", "host?secret"),
        ("transport", "http"),
    ],
)
def test_local_endpoint_is_structural_configuration_not_an_arbitrary_url(field, value):
    data, options = proposed()
    add_local(data, options)
    data["connections"]["local"]["endpoint"][field] = value
    with pytest.raises(ConfigurationError, match="invalid_endpoint"):
        load_configuration(OWNER, data, options)


@pytest.mark.parametrize("scopes", [[], ["open_api.write"], "open_api.read"])
def test_local_credentials_require_read_scope_without_inventing_write_support(scopes):
    data, options = proposed()
    add_local(data, options)
    data["connections"]["local"]["credentials"]["scope"] = scopes
    with pytest.raises(ConfigurationError):
        load_configuration(OWNER, data, options)


@pytest.mark.parametrize("version", [3, 5, True, "4"])
def test_loader_rejects_other_major_versions(version):
    data, options = proposed()
    with pytest.raises(ConfigurationError, match="unsupported_version"):
        load_configuration(OWNER, data, options, version=version)


@pytest.mark.parametrize("account", ["", "not-a-hash", "A" * 64, None])
def test_standalone_selection_requires_its_exact_old_account_scope(account):
    old = legacy([], ["air-1"])
    old["bacon_account"] = account
    with pytest.raises(ConfigurationError, match="invalid_account_key"):
        convert_legacy_configuration(OWNER, old, {})


@pytest.mark.parametrize(
    "case,code",
    [
        ("no_identity", "missing_identity"),
        ("wrong_identity", "missing_identity"),
        ("scoped_gateway", "invalid_identity_scope"),
        ("bad_prefix", "invalid_legacy_prefix"),
        ("wrong_serial", "legacy_identity_conflict"),
        ("local_as_standalone", "adapter_conflict"),
        ("bacon_as_plant", "adapter_conflict"),
        ("two_local_bindings", "ambiguous_local_target"),
        ("bad_brand", "invalid_brand"),
        ("bad_local_token", "invalid_credentials"),
        ("bad_local_expiry", "invalid_credentials"),
        ("bad_container", "invalid_object"),
    ],
)
def test_family_and_identity_conflicts_cannot_turn_into_new_device_bindings(case, code):
    snapshot = convert_legacy_configuration(OWNER, legacy(["000123"], ["air-1"]), {})
    data, options = snapshot.to_data(), snapshot.to_options()
    cid = next(iter(data["connections"]))
    plant = next(t for t in data["targets"].values() if t["kind"] == "pointt_object")
    standalone = next(
        t for t in data["targets"].values() if t["kind"] == "standalone_device"
    )
    if case == "no_identity":
        plant["identities"] = []
    elif case == "wrong_identity":
        plant["identities"][0]["namespace"] = "unknown"
    elif case == "scoped_gateway":
        plant["identities"][0]["account_key"] = "unrelated"
    elif case == "bad_prefix":
        standalone["legacy_identity"]["entity_prefix"] = "wrong"
    elif case == "wrong_serial":
        standalone["legacy_identity"]["serial"] = "other"
    elif case == "bad_brand":
        data["connections"][cid]["brand"] = "other"
    elif case == "bad_container":
        data["connections"][cid]["account_evidence"] = []
    elif case == "bacon_as_plant":
        standalone["kind"] = "pointt_object"
        standalone["identities"].append(
            {"namespace": "pointt_gateway", "value": "other", "evidence": "synthetic"}
        )
    else:
        # Ensure the helper adds the local source to the PointT target.
        tid = next(k for k, t in data["targets"].items() if t is plant)
        data["targets"] = {
            tid: plant,
            **{k: t for k, t in data["targets"].items() if k != tid},
        }
        add_local(data, options)
        local = data["connections"]["local"]
        if case == "local_as_standalone":
            plant["kind"] = "standalone_device"
            plant["identities"].append(
                {
                    "namespace": "bacon_device",
                    "value": "different",
                    "account_key": account_key(SUBJECT),
                    "evidence": "synthetic",
                }
            )
        elif case == "bad_local_token":
            local["credentials"]["token"] = ""
        elif case == "bad_local_expiry":
            local["credentials"]["expires_at"] = True
        elif case == "two_local_bindings":
            data["bindings"]["local_copy"] = {
                **data["bindings"]["local"],
                "adapter": "unknown",
            }
    with pytest.raises(ConfigurationError, match=f"^{code}$"):
        load_configuration(OWNER, data, options)


def test_future_target_metadata_and_local_expiry_remain_opaque():
    data, options = proposed()
    tid = add_local(data, options)
    data["connections"]["local"]["credentials"]["expires_at"] = -1
    cid = next(k for k in data["connections"] if k != "local")
    data["connections"][cid]["account_evidence"] = {"future": ["retained"]}
    target = data["targets"][tid]
    target["legacy_identity"] = {"format": "future_identity", "unknown": ["keep"]}
    target["identities"].append(
        {
            "namespace": "future",
            "account_key": "scope",
            "value": "new",
            "evidence": "unverified",
        }
    )
    assert load_configuration(OWNER, data, options).to_data() == data


def test_empty_owner_is_representable_after_explicit_target_removal():
    data = {
        "revision": 7,
        "connections": {},
        "targets": {},
        "bindings": {},
        "legacy_extra": {},
    }
    options = {"connections": {}, "targets": {}, "bindings": {}}
    snapshot = load_configuration(OWNER, data, options)
    assert snapshot.to_data() == data and not snapshot.targets


def test_deeply_nested_token_routing_is_unresolved_without_breaking_migration():
    old = legacy([], ["air-1"])
    nested = "[" * 1500 + "]" * 1500
    payload = base64.urlsafe_b64encode(nested.encode()).decode()
    old["access_token"] = f"x.{payload}.x"
    result = convert_legacy_configuration(OWNER, old, {})
    assert (
        next(iter(result.targets.values())).to_data()["legacy_identity"][
            "entity_prefix"
        ]
        is None
    )


@pytest.mark.parametrize(
    "value", [object(), {1: "value"}, {"x": (1, 2)}, [], {"x": float("-inf")}]
)
def test_non_json_values_are_rejected_with_static_errors(value):
    with pytest.raises(ConfigurationError):
        json_object(value)


def test_json_limits_reject_cycles_depth_nodes_and_size(monkeypatch):
    circular = {}
    circular["self"] = circular
    with pytest.raises(ConfigurationError, match="storage_limit"):
        json_object(circular)
    monkeypatch.setattr(storage, "MAX_JSON_NODES", 2)
    with pytest.raises(ConfigurationError, match="storage_limit"):
        json_object({"items": [1, 2]})
    monkeypatch.setattr(storage, "MAX_JSON_NODES", 100)
    monkeypatch.setattr(storage, "MAX_JSON_BYTES", 10)
    with pytest.raises(ConfigurationError, match="storage_limit"):
        json_object({"private": "private-value"})


def test_nonserializable_huge_integer_has_a_static_error():
    with pytest.raises(ConfigurationError, match="invalid_json"):
        json_object({"number": 10**5000})


def test_repr_and_errors_do_not_expose_configuration():
    old = legacy()
    snapshot = convert_legacy_configuration(OWNER, old, {})
    for view in [
        snapshot,
        *snapshot.connections.values(),
        *snapshot.targets.values(),
        *snapshot.bindings.values(),
    ]:
        for secret in (
            OWNER,
            "000123",
            old["access_token"],
            old["refresh_token"],
            account_key(SUBJECT),
        ):
            assert secret not in repr(view) and secret not in str(view)
    with pytest.raises(ConfigurationError) as error:
        load_configuration("private owner with space", {}, {})
    assert str(error.value) == "invalid_identifier"


JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=40),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(max_size=20), children, max_size=4)
    ),
    max_leaves=20,
)


@given(JSON)
def test_unknown_values_survive_conversion_and_repeated_loading(value):
    old = legacy()
    old["future"] = value
    snapshot = convert_legacy_configuration(OWNER, old, {"future_option": value})
    reloaded = load_configuration(OWNER, snapshot.to_data(), snapshot.to_options())
    assert reloaded == snapshot
    assert reloaded.to_data()["legacy_extra"]["future"] == value
    assert reloaded.to_options()["future_option"] == value


def test_storage_and_converter_work_without_home_assistant_or_network():
    package = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
    script = """
import importlib.abc
import pathlib
import socket
import sys
import types
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in ('homeassistant', 'aiohttp', 'aiomqtt'):
            raise AssertionError('Storage imported a runtime dependency')
sys.meta_path.insert(0, NoRuntime())
def no_network(*args, **kwargs):
    raise AssertionError('Storage attempted network access')
socket.socket = no_network
root = pathlib.Path(sys.argv[1])
for name, directory in [('custom_components', root.parent),
                        ('custom_components.bosch_buderus_heating', root)]:
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package
from custom_components.bosch_buderus_heating.configuration_migration import (
    convert_legacy_configuration,
)
from custom_components.bosch_buderus_heating.configuration import load_configuration
old = dict(brand='buderus', gateway_ids=['gateway'],
           access_token='expired', expires_at=0)
result = convert_legacy_configuration('owner', old, {})
assert load_configuration('owner', result.to_data(), result.to_options()) == result
from custom_components.bosch_buderus_heating.local_credentials import (
    StoredLocalAccess, import_local_token, load_local_access,
)
from custom_components.bosch_buderus_heating.local_probe import LocalEndpoint
access = StoredLocalAccess(import_local_token('synthetic'),
    LocalEndpoint('gateway.example.invalid'), 'a'*64, None, 'gateway', 1234)
assert load_local_access(access.to_data()) == access
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(package)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
