"""Local credential handover preserves current HA configuration and identity."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState

from custom_components.bosch_buderus_heating import local_setup as setup
from custom_components.bosch_buderus_heating.auth_lifecycle import entry_auth
from custom_components.bosch_buderus_heating.cloud_configuration import (
    CloudDiscovery,
    new_cloud_configuration,
)
from custom_components.bosch_buderus_heating.config_flow import BoschBuderusConfigFlow
from custom_components.bosch_buderus_heating.configuration import (
    ConfigurationError,
    load_configuration,
)
from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.configuration_changes import (
    ConfigurationRuns,
)
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.data import tokens_from_data
from custom_components.bosch_buderus_heating.entry_sources import notification_gateways
from custom_components.bosch_buderus_heating.gateway_identity import GatewayNamespace
from custom_components.bosch_buderus_heating.local_credentials import (
    created_local_token,
    import_local_token,
    load_local_access,
)
from custom_components.bosch_buderus_heating.local_probe import LocalEndpoint, ProbePath
from custom_components.bosch_buderus_heating.local_setup import (
    begin_local_setup,
    commit_local_setup,
    prepare_local_configuration,
    verify_local_setup,
)
from custom_components.bosch_buderus_heating.local_transport import (
    CertificateBinding,
    LocalProbeExchange,
    PortRole,
    StaleLocalRequest,
)
from custom_components.bosch_buderus_heating.pointt import Brand, Gateway
from tests.ha_test_helpers import MockConfigEntry
from tests.test_configuration import OWNER, add_local, legacy, proposed
from tests.test_local_probe import response
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import gateways as gateways
from tests.test_local_transport import wire_reply

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
PAIRS = ((GatewayNamespace.K40_LOCAL, GatewayNamespace.POINTT),)
CONTRACT = "synthetic-device-contract"
TOKEN = "synthetic-new-local-token"


def starting(*, local=False):
    data, options = proposed()
    target = next(iter(data["targets"]))
    if local:
        add_local(data, options)
    snapshot = load_configuration(OWNER, data, options)
    return snapshot, next(iter(snapshot.connections)), target


def begin(snapshot, target, *, local=False, host="gateway.example.invalid"):
    return begin_local_setup(
        snapshot, target, LocalEndpoint(host), connection_id="local" if local else None
    )


def resource_pin(edit):
    return CertificateBinding(edit.context, PortRole.RESOURCES, b"a" * 32)


async def verified(monkeypatch, snapshot, edit, *, gateway="000123"):
    async def read(context, token, binding, *, is_current):
        assert context == edit.context and token == TOKEN and is_current()
        return LocalProbeExchange(
            tuple(
                response(path, context=context, gatewayId=gateway) for path in ProbePath
            )
        )

    monkeypatch.setattr(setup, "read_local_probe", read)
    return await verify_local_setup(
        edit,
        import_local_token(TOKEN),
        resource_pin(edit),
        current=lambda: snapshot,
        is_active=lambda: True,
    )


def proposal(edit, proof, *snapshots, pairs=PAIRS, contract=CONTRACT):
    return prepare_local_configuration(
        edit, proof, snapshots, verified_namespace_pairs=pairs, contract_id=contract
    )


@pytest.mark.parametrize("created", [False, True])
async def test_actual_tls_probe_prepares_access_for_selected_existing_target(
    gateways, created
):
    snapshot, cloud, target = starting()
    edit = begin(snapshot, target)

    async def reply(raw, writer):
        path = ProbePath(raw.split(b" ")[1].decode())
        writer.write(
            wire_reply(response(path, context=edit.context, gatewayId="000123").body)
        )
        await writer.drain()

    _, requests, _, binding = await gateways(reply)
    binding = replace(binding, context=edit.context)
    credentials = (
        created_local_token(
            {"access_token": TOKEN, "token_type": "Bearer", "scope": "open_api.read"}
        )
        if created
        else import_local_token(TOKEN)
    )
    proof = await verify_local_setup(
        edit,
        credentials,
        binding,
        current=lambda: snapshot,
        is_active=lambda: True,
        authentication_binding=replace(binding, role=PortRole.AUTHENTICATION)
        if created
        else None,
    )
    after = proposal(edit, proof, snapshot)
    assert len(requests) == 3 and all(raw.startswith(b"GET ") for raw in requests)
    assert after.minor_version == 2 and snapshot.minor_version == 1
    assert after.targets.keys() == snapshot.targets.keys()
    assert (
        after.targets[target].to_data()["legacy_identity"]
        == snapshot.targets[target].to_data()["legacy_identity"]
    )
    assert after.connections[cloud] == snapshot.connections[cloud]
    local = load_local_access(
        after.connections[edit.context.identity.connection_id].to_data()
    )
    assert local.credentials.reported_scope == (("open_api.read",) if created else None)
    assert local.gateway_id == "000123"
    assert all(
        secret not in repr(proof) + repr(edit) for secret in (TOKEN, "000123", OWNER)
    )


async def test_replacement_retains_ids_options_and_invalidates_only_local_work(
    monkeypatch,
):
    snapshot, cloud, target = starting(local=True)
    data, options = snapshot.to_data(), snapshot.to_options()
    options["connections"]["local"]["enabled"] = False
    options["connections"]["local"]["custom"] = "keep"
    options["bindings"]["local"]["enabled"] = False
    snapshot = load_configuration(OWNER, data, options)
    edit = begin(snapshot, target, local=True, host="replacement.example.invalid")
    proof = await verified(monkeypatch, snapshot, edit)
    before_data = snapshot.to_data()
    after = proposal(edit, proof, snapshot, pairs=(), contract=None)
    assert set(after.connections) == set(snapshot.connections)
    assert after.bindings.keys() == snapshot.bindings.keys()
    assert after.to_options() == options
    assert after.targets[target].to_data() == snapshot.targets[target].to_data()
    local = after.connections["local"].to_data()
    assert local["auth_epoch"] == data["connections"]["local"]["auth_epoch"] + 1
    assert (
        local["config_revision"] == data["connections"]["local"]["config_revision"] + 1
    )
    assert local["credentials"]["token"] == TOKEN
    assert snapshot.to_data() == before_data
    # Disabled access stays disabled through a replacement.
    runs = ConfigurationRuns(snapshot)
    cloud_generation = runs.capture(cloud)
    changes = runs.update(after)
    assert changes.connections == {"local"} and runs.accepts(cloud_generation)


async def test_reusing_same_token_still_invalidates_previous_generation(monkeypatch):
    before, cloud, target = starting(local=True)
    first_edit = begin(before, target, local=True)
    first_proof = await verified(monkeypatch, before, first_edit)
    first = proposal(first_edit, first_proof, before, pairs=(), contract=None)
    runs = ConfigurationRuns(first)
    old_read = runs.capture("local", "local")
    cloud_read = runs.capture(cloud)
    second_edit = begin(first, target, local=True)
    second_proof = await verified(monkeypatch, first, second_edit)
    second = proposal(second_edit, second_proof, first, pairs=(), contract=None)
    assert (
        first.connections["local"].to_data()["credentials"]
        == second.connections["local"].to_data()["credentials"]
    )
    runs.update(second)
    assert not runs.accepts(old_read) and runs.accepts(cloud_read)


async def test_local_token_only_changes_also_retire_reads_without_cloud_restart(
    monkeypatch,
):
    before, cloud, target = starting(local=True)
    edit = begin(before, target, local=True)
    before = proposal(
        edit, await verified(monkeypatch, before, edit), before, pairs=(), contract=None
    )
    runs = ConfigurationRuns(before)
    local_generation, cloud_generation = runs.capture("local"), runs.capture(cloud)
    data = before.to_data()
    data["connections"]["local"]["credentials"]["token"] = "different-token"
    after = load_configuration(OWNER, data, before.to_options(), minor_version=2)
    runs.update(after)
    assert not runs.accepts(local_generation) and runs.accepts(cloud_generation)


@pytest.mark.parametrize(
    "change",
    [
        "token",
        "epoch",
        "endpoint",
        "disabled",
        "binding",
        "target",
        "order",
        "minor",
        "removed",
    ],
)
async def test_changed_selected_access_cannot_be_overwritten(monkeypatch, change):
    before, _, target = starting(local=True)
    edit = begin(before, target, local=True)
    proof = await verified(monkeypatch, before, edit)
    data, options = before.to_data(), before.to_options()
    minor = 1
    if change == "token":
        data["connections"]["local"]["credentials"]["token"] = "changed"
    elif change == "epoch":
        data["connections"]["local"]["auth_epoch"] += 1
    elif change == "endpoint":
        data["connections"]["local"]["endpoint"]["host"] = "other.example.invalid"
    elif change == "disabled":
        options["connections"]["local"]["enabled"] = False
    elif change == "binding":
        options["bindings"]["local"]["enabled"] = False
    elif change == "target":
        data["targets"][target]["private_preference"] = "changed"
    elif change == "order":
        options["targets"][target]["read_order"].reverse()
    elif change == "minor":
        minor = 2
    else:
        del data["connections"]["local"], data["bindings"]["local"]
        del options["connections"]["local"], options["bindings"]["local"]
        options["targets"][target]["read_order"].remove("local")
    current = load_configuration(OWNER, data, options, minor_version=minor)
    with pytest.raises(ConfigurationError, match="configuration_changed"):
        proposal(edit, proof, current, pairs=(), contract=None)
    assert current.to_data() == data and current.to_options() == options


async def test_unrelated_cloud_rotation_and_display_changes_are_preserved(monkeypatch):
    before, cloud, target = starting()
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit)
    data, options = before.to_data(), before.to_options()
    data["connections"][cloud]["credentials"]["access_token"] = "new-cloud-token"
    data["revision"] += 7
    options["display_option"] = "keep-new-setting"
    current = load_configuration(OWNER, data, options)
    after = proposal(edit, proof, current)
    assert after.connections[cloud] == current.connections[cloud]
    assert after.to_options()["display_option"] == "keep-new-setting"
    assert after.revision == current.revision + 1
    old_order = options["targets"][target]["read_order"]
    assert after.to_options()["targets"][target]["read_order"] == [
        *old_order,
        edit.binding_id,
    ]


@pytest.mark.parametrize(
    ("gateway", "pairs", "contract", "error"),
    [
        ("wrong", PAIRS, CONTRACT, "local_identity_mismatch"),
        ("000123", (), None, "local_unverified_namespace"),
        ("000123", PAIRS, None, "identity_contract_required"),
    ],
)
async def test_identity_and_contract_are_separate_from_successful_tls(
    monkeypatch, gateway, pairs, contract, error
):
    before, _, target = starting()
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit, gateway=gateway)
    with pytest.raises(ConfigurationError, match=error):
        proposal(edit, proof, before, pairs=pairs, contract=contract)


@pytest.mark.parametrize("location", ["foreign", "same_owner"])
async def test_retained_disconnected_or_competing_identity_blocks_claim(
    monkeypatch, location
):
    before, _, target = starting()
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit)
    foreign_data = {
        "revision": 0,
        "connections": {},
        "bindings": {},
        "legacy_extra": {},
        "targets": {
            "retained": {
                "kind": "local_installation",
                "identities": [
                    {
                        "namespace": "local_system_gateway",
                        "value": "000123",
                        "evidence": "previous_verification",
                    }
                ],
            }
        },
    }
    foreign_options = {
        "connections": {},
        "bindings": {},
        "targets": {"retained": {"read_order": []}},
    }
    if location == "foreign":
        other = load_configuration("other-owner", foreign_data, foreign_options)
        snapshots = (before, other)
        error = "local_owner_conflict"
    else:
        data, options = before.to_data(), before.to_options()
        data["targets"].update(foreign_data["targets"])
        options["targets"].update(foreign_options["targets"])
        snapshots = (load_configuration(OWNER, data, options),)
        error = "local_ambiguous"
    with pytest.raises(ConfigurationError, match=error):
        proposal(edit, proof, *snapshots)


async def test_two_setup_attempts_cannot_exchange_proofs_or_add_duplicate_access(
    monkeypatch,
):
    before, _, target = starting()
    first, second = begin(before, target), begin(before, target)
    proof = await verified(monkeypatch, before, first)
    assert first.context != second.context
    with pytest.raises(ConfigurationError, match="local_verification_expired"):
        proposal(second, proof, before)
    second_proof = await verified(monkeypatch, before, second)
    accepted = proposal(first, proof, before)
    with pytest.raises(ConfigurationError, match="local_already_connected"):
        proposal(second, second_proof, accepted)


@pytest.mark.parametrize("age", [-1, 61, float("inf"), float("nan")])
async def test_expired_receipt_never_becomes_current_after_clock_change(
    monkeypatch, age
):
    before, _, target = starting()
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit)
    monkeypatch.setattr(setup, "monotonic", lambda: proof._accepted_at + age)
    with pytest.raises(ConfigurationError, match="local_verification_expired"):
        proposal(edit, proof, before)


@pytest.mark.parametrize(
    "change",
    ["resource_role", "resource_context", "auth_role", "auth_context", "missing_auth"],
)
async def test_certificate_context_must_match_before_sending_credentials(
    monkeypatch, change
):
    before, _, target = starting()
    edit, other = begin(before, target), begin(before, target)
    pin = resource_pin(edit)
    auth = replace(pin, role=PortRole.AUTHENTICATION)
    if change == "resource_role":
        pin = auth
    elif change == "resource_context":
        pin = replace(pin, context=other.context)
    elif change == "auth_role":
        auth = pin
    elif change == "auth_context":
        auth = replace(auth, context=other.context)
    else:
        auth = None
    read = AsyncMock()
    monkeypatch.setattr(setup, "read_local_probe", read)
    credentials = created_local_token(
        {"access_token": TOKEN, "token_type": "Bearer", "scope": "open_api.read"}
    )
    with pytest.raises(ConfigurationError):
        await verify_local_setup(
            edit,
            credentials,
            pin,
            current=lambda: before,
            is_active=lambda: True,
            authentication_binding=auth,
        )
    read.assert_not_called()


async def test_failed_real_probe_leaves_old_access_untouched(gateways):
    before, _, target = starting(local=True)
    edit = begin(before, target, local=True)

    async def forbidden(raw, writer):
        writer.write(wire_reply(status=403))
        await writer.drain()

    _, requests, _, pin = await gateways(forbidden)
    with pytest.raises(ConfigurationError, match="resource_forbidden"):
        await verify_local_setup(
            edit,
            import_local_token(TOKEN),
            replace(pin, context=edit.context),
            current=lambda: before,
            is_active=lambda: True,
        )
    assert len(requests) == 1
    assert (
        before.connections["local"].to_data()["credentials"]["token"]
        == "synthetic-local"
    )


@pytest.mark.parametrize("mode", ["cancel", "inactive", "changed"])
async def test_pending_verification_cannot_publish_after_abort_or_change(
    monkeypatch, mode
):
    before, _, target = starting(local=True)
    edit = begin(before, target, local=True)
    started, release = asyncio.Event(), asyncio.Event()
    active = True
    current = before

    async def slow(context, token, binding, *, is_current):
        started.set()
        await release.wait()
        return LocalProbeExchange(
            tuple(
                response(path, context=context, gatewayId="000123")
                for path in ProbePath
            )
        )

    monkeypatch.setattr(setup, "read_local_probe", slow)
    task = asyncio.create_task(
        verify_local_setup(
            edit,
            import_local_token(TOKEN),
            resource_pin(edit),
            current=lambda: current,
            is_active=lambda: active,
        )
    )
    await started.wait()
    if mode == "cancel":
        task.cancel()
    elif mode == "inactive":
        active = False
    else:
        data = before.to_data()
        data["connections"]["local"]["credentials"]["token"] = "concurrent"
        current = load_configuration(OWNER, data, before.to_options())
    release.set()
    with pytest.raises(
        asyncio.CancelledError if mode == "cancel" else StaleLocalRequest
    ):
        await task
    assert (
        before.connections["local"].to_data()["credentials"]["token"]
        == "synthetic-local"
    )


def registered(hass, *, local=False, owner=OWNER, disabled=False):
    before, cloud, target = starting(local=local)
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=owner,
        unique_id=owner,
        title="My unchanged name",
        version=4,
        data=before.to_data(),
        options=before.to_options(),
        disabled_by=ConfigEntryDisabler.USER if disabled else None,
    )
    entry.add_to_hass(hass)
    return entry, ConfigurationManagement(hass, entry), cloud, target


async def test_normal_flow_still_blocks_storage_activation(hass, monkeypatch):
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 1)
    entry, management, _, target = registered(hass)
    before = management.current()
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit)
    assert BoschBuderusConfigFlow.MINOR_VERSION == 1
    with pytest.raises(ConfigurationError, match="local_setup_not_enabled"):
        commit_local_setup(
            management,
            edit,
            proof,
            verified_namespace_pairs=PAIRS,
            contract_id=CONTRACT,
        )
    assert management.current() == before and entry.minor_version == 1


async def test_native_ha_commit_changes_data_and_version_together_and_preserves_cloud(
    hass, monkeypatch
):
    entry, management, cloud, target = registered(hass)
    before = management.current()
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit)
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
    with patch.object(
        hass.config_entries,
        "async_update_entry",
        wraps=hass.config_entries.async_update_entry,
    ) as update:
        after = commit_local_setup(
            management,
            edit,
            proof,
            verified_namespace_pairs=PAIRS,
            contract_id=CONTRACT,
        )
    update.assert_called_once()
    assert update.call_args.kwargs["minor_version"] == 2
    assert after == management.current() and entry.minor_version == 2
    assert entry.title == "My unchanged name" and entry.unique_id == OWNER
    assert (
        EntryConfiguration(entry, cloud).tokens().access_token
        == before.connections[cloud].to_data()["credentials"]["access_token"]
    )
    assert notification_gateways(entry) == ("000123",)
    # Existing controls preserve v4.2 when changing unrelated cloud settings.
    management.set_enabled(management.edit(cloud), False)
    assert entry.minor_version == 2
    assert (
        load_local_access(
            management.current()
            .connections[edit.context.identity.connection_id]
            .to_data()
        ).credentials.token
        == TOKEN
    )
    with pytest.raises(ConfigurationError):
        commit_local_setup(
            management,
            edit,
            proof,
            verified_namespace_pairs=PAIRS,
            contract_id=CONTRACT,
        )


async def test_error_before_ha_acceptance_retains_complete_existing_access(
    hass, monkeypatch
):
    entry, management, _, target = registered(hass, local=True)
    before = management.current()
    edit = begin(before, target, local=True)
    proof = await verified(monkeypatch, before, edit)
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
    with (
        patch.object(
            hass.config_entries,
            "async_update_entry",
            side_effect=OSError("synthetic disk/update failure"),
        ),
        pytest.raises(ConfigurationError, match="local_save_failed"),
    ):
        commit_local_setup(management, edit, proof)
    assert management.current() == before and entry.minor_version == 1


async def test_foreign_disabled_entry_is_rechecked_at_commit(hass, monkeypatch):
    _entry, management, _, target = registered(hass)
    before = management.current()
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit)
    registered(hass, owner="another-owner", disabled=True)
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
    with pytest.raises(ConfigurationError, match="local_owner_conflict"):
        commit_local_setup(
            management,
            edit,
            proof,
            verified_namespace_pairs=PAIRS,
            contract_id=CONTRACT,
        )
    assert management.current() == before


async def test_cloud_reconnect_preserves_new_local_storage(hass, monkeypatch):
    _entry, management, cloud, target = registered(hass)
    before = management.current()
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit)
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
    # The stored cloud comparison: without it, adding the cloud again needs a
    # fresh comparison first (P49b step 8, see test_local_first_merge).
    from custom_components.bosch_buderus_heating.local_identity import (
        CONTRACT as STORED_COMPARISON,
    )

    after = commit_local_setup(
        management,
        edit,
        proof,
        verified_namespace_pairs=PAIRS,
        contract_id=STORED_COMPARISON,
    )
    management.remove(management.edit(cloud))
    current = management.current()
    tokens = tokens_from_data(legacy())
    discovery = CloudDiscovery((Gateway("000123"),), (), "")
    proposed_cloud = new_cloud_configuration(
        hass,
        OWNER,
        "synthetic-flow",
        Brand.BUDERUS,
        tokens,
        discovery,
        {"000123"},
        set(),
        current,
    )
    restored = management.connect_cloud(current, proposed_cloud)
    assert restored.minor_version == 2
    assert (
        restored.connections[edit.context.identity.connection_id]
        == after.connections[edit.context.identity.connection_id]
    )


@pytest.mark.parametrize(
    "kind", ["missing", "standalone", "cloud", "removed", "other_local_target"]
)
def test_setup_selection_does_not_invent_or_reassign_targets(kind):
    snapshot, cloud, target = starting(local=True)
    if kind == "missing":
        target = "absent"
    if kind == "standalone":
        from custom_components.bosch_buderus_heating.configuration_migration import (
            convert_legacy_configuration,
        )

        snapshot = convert_legacy_configuration(OWNER, legacy([], ["air-1"]), {})
        target = next(iter(snapshot.targets))
    if kind == "other_local_target":
        data, options = snapshot.to_data(), snapshot.to_options()
        data["targets"]["another"] = {
            "kind": "local_installation",
            "identities": [
                {
                    "namespace": "local_system_gateway",
                    "value": "other",
                    "evidence": "retained",
                }
            ],
        }
        options["targets"]["another"] = {"read_order": []}
        snapshot = load_configuration(OWNER, data, options)
        target = "another"
    with pytest.raises(ConfigurationError):
        begin_local_setup(
            snapshot,
            target,
            LocalEndpoint("gateway.local"),
            connection_id=cloud
            if kind == "cloud"
            else "absent"
            if kind == "removed"
            else "local"
            if kind == "other_local_target"
            else None,
        )


async def test_abort_after_verification_invalidates_receipt(monkeypatch):
    before, _, target = starting()
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit)
    inactive = replace(proof, _is_active=lambda: False)
    with pytest.raises(ConfigurationError, match="local_verification_expired"):
        proposal(edit, inactive, before)


async def test_listener_failure_reports_uncertain_outcome_without_rollback(
    hass, monkeypatch
):
    entry, management, _, target = registered(hass, local=True)
    before = management.current()
    edit = begin(before, target, local=True)
    proof = await verified(monkeypatch, before, edit)
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
    remove = management.subscribe(
        Mock(side_effect=RuntimeError("private listener details"))
    )
    try:
        with pytest.raises(ConfigurationError, match="local_save_outcome_uncertain"):
            commit_local_setup(management, edit, proof)
    finally:
        remove()
    assert entry.minor_version == 2
    assert (
        management.current().connections["local"].to_data()["credentials"]["token"]
        == TOKEN
    )
    assert (
        before.connections["local"].to_data()["credentials"]["token"]
        == "synthetic-local"
    )


async def test_initial_storage_upgrade_cannot_change_a_running_entry(hass, monkeypatch):
    entry, management, _, target = registered(hass)
    before = management.current()
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit)
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    with pytest.raises(ConfigurationError, match="local_storage_upgrade_requires_stop"):
        commit_local_setup(
            management,
            edit,
            proof,
            verified_namespace_pairs=PAIRS,
            contract_id=CONTRACT,
        )
    assert management.current() == before
    entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)


async def test_storage_upgrade_replaces_old_cloud_view_and_retains_backoff(
    hass, monkeypatch
):
    entry, management, cloud, target = registered(hass)
    before = management.current()
    updated_tokens = tokens_from_data(
        {
            **legacy(),
            "access_token": "fresh-cloud",
            "refresh_token": "fresh-refresh",
            "expires_at": 10**12,
        }
    )
    oauth = Mock(refresh=AsyncMock(return_value=updated_tokens))
    old_manager, backoff = entry_auth(hass, entry, oauth, connection_id=cloud)
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit)
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
    commit_local_setup(
        management, edit, proof, verified_namespace_pairs=PAIRS, contract_id=CONTRACT
    )
    new_manager, retained_backoff = entry_auth(hass, entry, oauth, connection_id=cloud)
    assert new_manager is not old_manager and retained_backoff is backoff
    assert await new_manager.get_access_token(force_refresh=True) == "fresh-cloud"
    assert EntryConfiguration(entry, cloud).tokens() == updated_tokens
    assert (
        management.current()
        .connections[edit.context.identity.connection_id]
        .to_data()["credentials"]["token"]
        == TOKEN
    )


async def test_pending_cloud_refresh_blocks_minor_upgrade_without_losing_rotated_token(
    hass, monkeypatch
):
    entry, management, cloud, target = registered(hass)
    before = management.current()
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit)
    started, release = asyncio.Event(), asyncio.Event()
    updated = tokens_from_data(
        {
            **legacy(),
            "access_token": "rotated-cloud",
            "refresh_token": "rotated-refresh",
            "expires_at": 10**12,
        }
    )

    async def refresh(token):
        started.set()
        await release.wait()
        return updated

    manager, _ = entry_auth(hass, entry, Mock(refresh=refresh), connection_id=cloud)
    pending = asyncio.create_task(manager.get_access_token(force_refresh=True))
    await started.wait()
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
    try:
        with pytest.raises(ConfigurationError, match="authentication_in_progress"):
            commit_local_setup(
                management,
                edit,
                proof,
                verified_namespace_pairs=PAIRS,
                contract_id=CONTRACT,
            )
        assert management.current() == before
    finally:
        release.set()
        assert await pending == "rotated-cloud"
    assert EntryConfiguration(entry, cloud).tokens() == updated
    after = commit_local_setup(
        management, edit, proof, verified_namespace_pairs=PAIRS, contract_id=CONTRACT
    )
    assert (
        after.connections[cloud].to_data()["credentials"]["refresh_token"]
        == "rotated-refresh"
    )


async def test_later_local_replacement_runs_independently_of_cloud_refresh(
    hass, monkeypatch
):
    entry, management, cloud, target = registered(hass)
    before = management.current()
    edit = begin(before, target)
    proof = await verified(monkeypatch, before, edit)
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
    first = commit_local_setup(
        management, edit, proof, verified_namespace_pairs=PAIRS, contract_id=CONTRACT
    )
    local_id = edit.context.identity.connection_id
    next_edit = begin_local_setup(
        first, target, LocalEndpoint("gateway.example.invalid"), connection_id=local_id
    )
    next_proof = await verified(monkeypatch, first, next_edit)
    started, release = asyncio.Event(), asyncio.Event()
    refreshed = tokens_from_data(
        {
            **legacy(),
            "access_token": "later-cloud",
            "refresh_token": "later-refresh",
            "expires_at": 10**12,
        }
    )

    async def refresh(token):
        started.set()
        await release.wait()
        return refreshed

    manager, _ = entry_auth(hass, entry, Mock(refresh=refresh), connection_id=cloud)
    pending = asyncio.create_task(manager.get_access_token(force_refresh=True))
    await started.wait()
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    try:
        second = commit_local_setup(management, next_edit, next_proof)
    finally:
        release.set()
        await pending
        entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)
    assert (
        second.connections[local_id].auth_epoch
        == first.connections[local_id].auth_epoch + 1
    )
    assert management.current().connections[local_id] == second.connections[local_id]
    assert EntryConfiguration(entry, cloud).tokens() == refreshed
