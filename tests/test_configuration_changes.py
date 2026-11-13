"""Changes and request generations keep independent sources and targets apart."""

from copy import deepcopy

import pytest

from custom_components.bosch_buderus_heating.configuration import (
    ConfigurationError,
    load_configuration,
)
from custom_components.bosch_buderus_heating.configuration_changes import (
    ConfigurationRuns,
    configuration_changes,
)
from custom_components.bosch_buderus_heating.configuration_migration import (
    convert_legacy_configuration,
)
from tests.test_configuration import OWNER, add_local, legacy


def topology():
    snapshot = convert_legacy_configuration(
        OWNER, legacy(["000123", "gateway-b"], ["softener-c"]), {}
    )
    data, options = snapshot.to_data(), snapshot.to_options()
    target = add_local(data, options)
    cloud = next(iter(snapshot.connections))
    binding = next(
        key
        for key, value in data["bindings"].items()
        if value["target_id"] == target and key != "local"
    )
    others = [key for key in snapshot.bindings if key != binding]
    return load_configuration(OWNER, data, options), cloud, target, binding, others


@pytest.mark.parametrize(
    "change",
    [
        "local_address",
        "cloud_off",
        "binding_off",
        "read_order",
        "login",
        "tokens",
        "display",
        "revision",
    ],
)
def test_only_affected_requests_are_invalidated(change):
    before, cloud, target, binding, others = topology()
    runs = ConfigurationRuns(before)
    stamps = {key: runs.capture(cloud, key) for key in [binding, *others]}
    local = runs.capture("local", "local")
    cloud_session = runs.capture(cloud)
    data, options = before.to_data(), before.to_options()
    if change == "local_address":
        data["connections"]["local"]["endpoint"]["host"] = "replacement.example.invalid"
        data["connections"]["local"]["config_revision"] += 1
    elif change == "cloud_off":
        options["connections"][cloud]["enabled"] = False
    elif change == "binding_off":
        options["bindings"][binding]["enabled"] = False
    elif change == "read_order":
        options["targets"][target]["read_order"].reverse()
    elif change == "login":
        data["connections"][cloud]["auth_epoch"] += 1
    elif change == "tokens":
        data["connections"][cloud]["credentials"]["access_token"] = "rotated"
    elif change == "display":
        options["fault_notifications"] = {"an-option": True}
    else:
        data["revision"] += 1
    after = load_configuration(OWNER, data, options)
    actions = runs.update(after)
    assert runs.accepts(local) is (change != "local_address")
    assert runs.accepts(stamps[binding]) is (
        change not in ("cloud_off", "binding_off", "login")
    )
    assert all(runs.accepts(stamps[key]) for key in others) is (
        change not in ("cloud_off", "login")
    )
    assert runs.accepts(cloud_session) is (change not in ("cloud_off", "login"))
    assert actions.tokens == ({cloud} if change == "tokens" else set())
    assert actions.display_options is (change == "display")
    if change == "binding_off":
        assert actions.bindings == {binding} and not actions.connections
    if change == "read_order":
        assert actions.targets == {target} and not actions.connections
        assert not actions.bindings
    if change in ("tokens", "display", "revision"):
        assert not actions.connections and not actions.bindings and not actions.targets
    assert not any(
        identifier in repr(actions) for identifier in (OWNER, cloud, target, binding)
    )


@pytest.mark.parametrize("scope", ["connection", "binding", "target"])
def test_remove_and_restore_never_accepts_old_work(scope):
    before, cloud, target, binding, _ = topology()
    runs = ConfigurationRuns(before)
    original = runs.capture(cloud, binding)
    data, options = before.to_data(), before.to_options()
    removed = {binding}
    if scope == "connection":
        removed = {
            key
            for key, value in data["bindings"].items()
            if value["connection_id"] == cloud
        }
        del data["connections"][cloud]
        del options["connections"][cloud]
    if scope == "target":
        removed.add("local")
        del data["targets"][target]
        del options["targets"][target]
    for key in removed:
        del data["bindings"][key]
        del options["bindings"][key]
    for value in options["targets"].values():
        value["read_order"] = [key for key in value["read_order"] if key not in removed]
    runs.update(load_configuration(OWNER, data, options))
    assert not runs.accepts(original)
    runs.update(before)
    assert not runs.accepts(original)
    assert runs.accepts(runs.capture(cloud, binding))


def test_owner_and_runtime_context_cannot_be_reused():
    before, cloud, _, binding, _ = topology()
    runs = ConfigurationRuns(before)
    original = runs.capture(cloud, binding)
    assert not ConfigurationRuns(before).accepts(original)
    other = load_configuration("someone-else", before.to_data(), before.to_options())
    with pytest.raises(ConfigurationError, match="owner_changed"):
        runs.update(other)
    assert runs.accepts(original)
    with pytest.raises(ConfigurationError, match="binding_unavailable"):
        runs.capture("local", binding)
    with pytest.raises(ConfigurationError, match="connection_unavailable"):
        runs.capture("unknown")


def test_unknown_sections_do_not_activate_a_transport():
    before, *_ = topology()
    data, options = before.to_data(), before.to_options()
    data["future"] = {"opaque": [1, 2]}
    unchanged = deepcopy(data)
    after = load_configuration(OWNER, data, options)
    result = configuration_changes(before, after)
    assert not result.connections and not result.bindings and not result.targets
    assert after.to_data() == unchanged


@pytest.mark.parametrize("scope", ["connection", "binding"])
@pytest.mark.parametrize("removed_source", ["local", "cloud"])
def test_removing_and_restoring_source_keeps_other_source_generation(
    scope, removed_source
):
    before, cloud, target, binding, _ = topology()
    runs = ConfigurationRuns(before)
    local_stamp = runs.capture("local", "local")
    cloud_stamp = runs.capture(cloud, binding)
    removed_stamp, retained_stamp = (
        (local_stamp, cloud_stamp)
        if removed_source == "local"
        else (cloud_stamp, local_stamp)
    )
    connection = "local" if removed_source == "local" else cloud
    data, options = before.to_data(), before.to_options()
    removed = {"local" if removed_source == "local" else binding}
    if scope == "connection":
        removed = {
            key
            for key, value in data["bindings"].items()
            if value["connection_id"] == connection
        }
        del data["connections"][connection]
        del options["connections"][connection]
    for key in removed:
        del data["bindings"][key]
        del options["bindings"][key]
    for settings in options["targets"].values():
        settings["read_order"] = [
            key for key in settings["read_order"] if key not in removed
        ]
    actions = runs.update(load_configuration(OWNER, data, options))
    assert target in actions.targets and actions.bindings == removed
    assert not runs.accepts(removed_stamp) and runs.accepts(retained_stamp)
    restored = runs.update(before)
    assert target in restored.targets and restored.bindings == removed
    assert not runs.accepts(removed_stamp) and runs.accepts(retained_stamp)


@pytest.mark.parametrize(
    "change", ["identity", "kind", "target_data", "unknown_option", "combined_order"]
)
def test_target_context_changes_still_invalidate_both_sources_only_for_that_target(
    change,
):
    before, cloud, target, binding, others = topology()
    runs = ConfigurationRuns(before)
    local_stamp = runs.capture("local", "local")
    cloud_stamp = runs.capture(cloud, binding)
    peers = [runs.capture(cloud, key) for key in others]
    data, options = before.to_data(), before.to_options()
    if change in ("identity", "combined_order"):
        data["targets"][target]["identities"][0]["evidence"] = "reverified_identity"
    elif change == "kind":
        data["targets"][target]["kind"] = "future_pointt_type"
    elif change == "target_data":
        data["targets"][target]["future_context"] = {"revision": 2}
    else:
        options["targets"][target]["future_policy"] = {"enabled": False}
    if change == "combined_order":
        options["targets"][target]["read_order"].reverse()
    actions = runs.update(load_configuration(OWNER, data, options))
    assert actions.targets == {target} and actions.bindings == {binding, "local"}
    assert not actions.connections
    assert not runs.accepts(local_stamp) and not runs.accepts(cloud_stamp)
    assert all(runs.accepts(stamp) for stamp in peers)


def test_binding_provenance_change_only_invalidates_its_own_source():
    before, cloud, target, binding, _ = topology()
    runs = ConfigurationRuns(before)
    local_stamp, cloud_stamp = (
        runs.capture("local", "local"),
        runs.capture(cloud, binding),
    )
    data, options = before.to_data(), before.to_options()
    data["bindings"]["local"]["link_evidence"]["contract"] = "replacement_contract"
    options["targets"][target]["read_order"].reverse()
    actions = runs.update(load_configuration(OWNER, data, options))
    assert actions.targets == {target} and actions.bindings == {"local"}
    assert not runs.accepts(local_stamp) and runs.accepts(cloud_stamp)


def test_capture_decodes_options_once_per_snapshot_and_sees_changes_at_once(
    monkeypatch,
):
    """O5: one JSON decode per stored version; a new version is read at once."""
    from custom_components.bosch_buderus_heating.configuration import (
        ConfigurationSnapshot,
    )

    before, cloud, _, binding, _ = topology()
    decoded = []
    original = ConfigurationSnapshot.to_options

    def counting(snapshot):
        decoded.append(snapshot)
        return original(snapshot)

    monkeypatch.setattr(ConfigurationSnapshot, "to_options", counting)
    runs = ConfigurationRuns(before)
    stamps = [runs.capture(cloud, binding) for _ in range(20)]
    assert all(runs.accepts(stamp) for stamp in stamps)
    assert len(decoded) == 1 and decoded[0] is before
    data, options = before.to_data(), before.to_options()
    options["bindings"][binding]["enabled"] = False
    after = load_configuration(OWNER, data, options)
    runs.update(after)
    decoded.clear()
    # The switched-off source is refused at once, not from the old decode.
    with pytest.raises(ConfigurationError, match="binding_unavailable"):
        runs.capture(cloud, binding)
    assert len(decoded) == 1 and decoded[0] is after
    # Back on: the next stored version is decoded again and accepted.
    options["bindings"][binding]["enabled"] = True
    restored = load_configuration(OWNER, data, options)
    runs.update(restored)
    decoded.clear()
    assert runs.capture(cloud, binding).binding_id == binding
    runs.capture(cloud)
    assert len(decoded) == 1 and decoded[0] is restored
