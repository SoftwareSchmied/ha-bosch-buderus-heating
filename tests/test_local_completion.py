"""Native HA handover, restart recovery and deletion with actual journal files."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import CoreState

from custom_components.bosch_buderus_heating import local_enrollment as enrollment
from custom_components.bosch_buderus_heating import local_handover as handover
from custom_components.bosch_buderus_heating import local_setup as setup
from custom_components.bosch_buderus_heating.config_flow import BoschBuderusConfigFlow
from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.local_completion import (
    CompletionIntent,
    RecoveryStatus,
    ScopedIdentityContract,
    local_state_fingerprint,
)
from custom_components.bosch_buderus_heating.local_enrollment import (
    LocalEnrollmentError,
)
from custom_components.bosch_buderus_heating.local_handover import complete_local_setup
from tests.test_local_enrollment import journal as journal
from tests.test_local_enrollment import reopened
from tests.test_local_setup import TOKEN, begin, registered, verified

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
def prepared(hass, monkeypatch):
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
    return registered(hass)


async def pending(journal, management, target, monkeypatch, *, local=False):
    before = management.current()
    edit = begin(before, target, local=local)
    record = await journal.import_token(
        edit, TOKEN, current=management.current, is_active=lambda: True
    )
    proof = await verified(monkeypatch, before, edit)
    contract = ScopedIdentityContract(
        before.owner_id, target, "000123", "synthetic-device-contract"
    )
    return record, proof, contract


async def commit(journal, management, bundle):
    record, proof, contract = bundle
    return await journal.commit_verified(
        record.attempt_id, proof, management, contract=contract
    )


@pytest.mark.parametrize("phase", ["before", "after", "listener"])
async def test_native_acceptance_recovery_does_not_replay_or_overwrite_cloud(
    prepared,
    journal,
    monkeypatch,
    phase,
):
    entry, management, cloud, target = prepared
    bundle = await pending(journal, management, target, monkeypatch)
    record, proof, contract = bundle
    before = management.current()
    original = management.hass.config_entries.async_update_entry

    def update(*args, **kwargs):
        if phase != "before":
            original(*args, **kwargs)
        if phase != "listener":
            raise RuntimeError("synthetic private persistence detail")

    def broken_listener(snapshot):
        raise RuntimeError("synthetic private listener detail")

    if phase == "listener":
        management.subscribe(broken_listener)
    with (
        patch.object(
            management.hass.config_entries, "async_update_entry", side_effect=update
        ),
        pytest.raises(ConfigurationError) as failure,
    ):
        await commit(journal, management, bundle)
    assert "synthetic private" not in str(failure.value)
    saved = (await journal.records())[0]
    assert saved.completion is not None and saved.credentials == record.credentials
    assert saved.completion.process_id == enrollment.PROCESS_ID
    state = await reopened(journal).recovery_status(
        record.attempt_id, management.current()
    )
    if phase == "before":
        assert state == RecoveryStatus.READY and management.current() == before
        recovered = await reopened(journal).resume(record.attempt_id, before)
        assert (
            recovered.edit.context.identity.connection_id
            == proof.edit.context.identity.connection_id
        )
        assert recovered.edit.binding_id == proof.edit.binding_id
        fresh = await verified(monkeypatch, before, recovered.edit)
        after = await journal.commit_verified(
            record.attempt_id, fresh, management, contract=contract
        )
    else:
        assert state == RecoveryStatus.APPLIED
        with pytest.raises(LocalEnrollmentError, match="already_applied"):
            await journal.resume(record.attempt_id, management.current())
        after = management.current()
        with patch.object(
            management.hass.config_entries, "async_update_entry"
        ) as repeated:
            assert await commit(journal, management, bundle) == after
        repeated.assert_not_called()
    assert entry.minor_version == 2
    assert after.connections[cloud] == before.connections[cloud]
    assert set(after.targets) == set(before.targets)
    assert TOKEN not in repr(saved) and TOKEN not in repr(saved.completion)


@pytest.mark.parametrize(
    "startup", ["cold", "same_process", "running", "changed", "old", "invalid"]
)
async def test_only_matching_cold_loaded_configuration_clears_duplicate_token(
    prepared,
    journal,
    monkeypatch,
    startup,
):
    entry, management, cloud, target = prepared
    before = management.current()
    bundle = await pending(journal, management, target, monkeypatch)
    record, _, _ = bundle
    after = await commit(journal, management, bundle)
    data, options = after.to_data(), after.to_options()
    # Independent rotation is never a reason to replay or retain a stale token.
    data["connections"][cloud]["credentials"]["access_token"] = "rotated-cloud-token"
    if startup == "changed":
        data["connections"][bundle[1].edit.context.identity.connection_id][
            "credentials"
        ]["token"] = "newer-local-token"
    elif startup == "old":
        data, options = before.to_data(), before.to_options()
    elif startup == "invalid":
        data = {}
    management.hass.config_entries.async_update_entry(
        entry, data=data, options=options, minor_version=1 if startup == "old" else 2
    )
    boot = reopened(journal)
    management.hass.set_state(
        CoreState.running if startup == "running" else CoreState.starting
    )
    if startup != "same_process":
        monkeypatch.setattr(enrollment, "PROCESS_ID", "next-process")
    with patch.object(
        management.hass.config_entries, "async_update_entry"
    ) as no_replay:
        await boot.initialize()
        await boot.initialize()
    no_replay.assert_not_called()
    saved = (await boot.records())[0]
    assert saved.resolved == (startup == "cold")
    assert (saved.credentials is None) == (startup == "cold")
    if startup != "invalid":
        expected = {
            "cold": RecoveryStatus.RESOLVED,
            "changed": RecoveryStatus.CONFLICT,
            "old": RecoveryStatus.READY,
        }.get(startup, RecoveryStatus.APPLIED)
        assert (
            await boot.recovery_status(record.attempt_id, management.current())
            == expected
        )


async def test_mutated_journal_never_treats_initialization_as_cold_confirmation(
    prepared, journal, monkeypatch
):
    _, management, _, target = prepared
    bundle = await pending(journal, management, target, monkeypatch)
    await commit(journal, management, bundle)
    management.hass.set_state(CoreState.starting)
    monkeypatch.setattr(enrollment, "PROCESS_ID", "another-process")
    await journal.initialize()
    assert (await journal.records())[0].credentials is not None


@pytest.mark.parametrize("change", ["owner", "target", "gateway", "pointt"])
async def test_identity_evidence_cannot_be_reused_outside_selected_target(
    prepared, journal, monkeypatch, change
):
    _, management, _, target = prepared
    bundle = await pending(journal, management, target, monkeypatch)
    record, proof, contract = bundle
    if change == "pointt":
        data = management.current().to_data()
        data["targets"][target]["identities"][0]["value"] = "another-gateway"
        data["targets"][target]["legacy_identity"]["gateway_id"] = "another-gateway"
        for binding in data["bindings"].values():
            if binding["target_id"] == target:
                binding["locator"]["value"] = "another-gateway"
        management.hass.config_entries.async_update_entry(management.entry, data=data)
    else:
        contract = replace(contract, **{change + "_id": "another-identity"})
    with pytest.raises(ConfigurationError, match="identity_contract_scope_changed"):
        await journal.commit_verified(
            record.attempt_id, proof, management, contract=contract
        )
    assert (await journal.records())[0].completion is None


@pytest.mark.parametrize(
    "change", ["token", "endpoint", "basis", "expired", "inactive"]
)
async def test_unverified_changes_never_arm_completion(
    prepared, journal, monkeypatch, change
):
    _, management, _, target = prepared
    record, proof, contract = await pending(journal, management, target, monkeypatch)
    if change == "token":
        proof = replace(
            proof,
            access=replace(
                proof.access,
                credentials=replace(proof.access.credentials, token="other-token"),
            ),
        )
    elif change == "endpoint":
        proof = await verified(
            monkeypatch,
            management.current(),
            begin(management.current(), target, host="other.invalid"),
        )
    elif change == "basis":
        proof = replace(proof, edit=replace(proof.edit, _signature="changed"))
    elif change == "expired":
        monkeypatch.setattr(setup, "monotonic", lambda: proof._accepted_at + 61)
    else:
        proof = replace(proof, _is_active=lambda: False)
    with pytest.raises((ConfigurationError, LocalEnrollmentError)):
        await journal.commit_verified(
            record.attempt_id, proof, management, contract=contract
        )
    assert (await journal.records())[0].completion is None


async def test_import_stays_unverified_survives_reopen_and_blocks_duplicate(
    prepared, journal, monkeypatch
):
    _, management, _, target = prepared
    record, _, _ = await pending(journal, management, target, monkeypatch)
    recovered = await reopened(journal).resume(record.attempt_id, management.current())
    assert recovered.authentication_binding is None
    assert recovered.credentials.reported_scope is None
    assert not hasattr(recovered, "access")
    assert (
        await journal.recovery_status(record.attempt_id, management.current())
        == RecoveryStatus.READY
    )
    with pytest.raises(LocalEnrollmentError, match="already_pending"):
        await pending(journal, management, target, monkeypatch)


@pytest.mark.parametrize(
    "corruption", ["process", "fingerprint", "keys", "state", "imported_scope"]
)
async def test_malformed_completion_never_discards_recoverable_token(
    prepared, journal, monkeypatch, corruption
):
    _, management, _, target = prepared
    bundle = await pending(journal, management, target, monkeypatch)
    await commit(journal, management, bundle)
    raw = json.loads(journal._path.read_text())
    row = raw["attempts"][0]
    if corruption == "process":
        row["completion"]["process_id"] = ""
    elif corruption == "fingerprint":
        row["completion"]["expected_sha256"] = "bad"
    elif corruption == "keys":
        row["completion"]["token"] = TOKEN
    elif corruption == "state":
        row.update(state="uncertain", credentials=None, problem="response_uncertain")
    else:
        row["credentials"]["reported_scope"] = ["open_api.read"]
    body = json.dumps(raw)
    journal._path.write_text(body)
    with pytest.raises(LocalEnrollmentError, match="storage_failed"):
        await reopened(journal).records()
    assert journal._path.read_text() == body


@pytest.mark.parametrize("action", ["remove", "cancel", "change"])
async def test_local_removal_purges_pending_without_removing_target_or_cloud(
    hass, journal, monkeypatch, action
):
    _entry, management, cloud, target = registered(hass, local=True)
    record, _, _ = await pending(journal, management, target, monkeypatch, local=True)
    before = management.current()
    original = journal._purge

    async def purge(matches):
        await original(matches)
        if action == "cancel":
            raise asyncio.CancelledError
        if action == "change":
            management.set_enabled(management.edit("local"), False)

    monkeypatch.setattr(journal, "_purge", purge)
    if action == "remove":
        after = await journal.remove_access(management, management.edit("local"))
        assert "local" not in after.connections
    else:
        with pytest.raises(
            asyncio.CancelledError if action == "cancel" else ConfigurationError
        ):
            await journal.remove_access(management, management.edit("local"))
        after = management.current()
        assert "local" in after.connections
    assert not await journal.records()
    assert after.targets == before.targets
    assert after.connections[cloud] == before.connections[cloud]
    assert not journal._removed_connections
    with pytest.raises(LocalEnrollmentError, match="not_found"):
        await journal.resume(record.attempt_id, after)


async def test_removed_owner_cannot_resume_or_import_from_stale_snapshot(
    prepared, journal, monkeypatch
):
    _, management, _, target = prepared
    await pending(journal, management, target, monkeypatch)
    await journal.remove_owner(management.current().owner_id)
    assert not await journal.records()
    with pytest.raises(LocalEnrollmentError, match="owner_removed"):
        await pending(journal, management, target, monkeypatch)


async def test_hidden_flow_rejects_before_runtime_stop_or_completion_storage(
    hass, journal, monkeypatch
):
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 1)
    entry, management, _, target = registered(hass)
    bundle = await pending(journal, management, target, monkeypatch)
    record, proof, contract = bundle
    with (
        patch.object(hass.config_entries, "async_unload") as unload,
        pytest.raises(ConfigurationError, match="local_setup_not_enabled"),
    ):
        await complete_local_setup(
            management, journal, record.attempt_id, proof, contract=contract
        )
    unload.assert_not_called()
    assert (await journal.records())[0].completion is None
    assert entry.minor_version == 1


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "expired",
        "cancel",
        "unload_failed",
        "disabled",
        "removed",
        "restart_failed",
        "busy",
    ],
)
async def test_transition_restores_runtime_without_reenabling_disabled_entry(
    prepared, journal, monkeypatch, outcome
):
    entry, management, _, target = prepared
    before = management.current()
    record, proof, contract = await pending(journal, management, target, monkeypatch)
    entry.mock_state(
        hass=management.hass,
        state=ConfigEntryState.SETUP_IN_PROGRESS
        if outcome == "busy"
        else ConfigEntryState.LOADED,
    )
    events = []

    async def wait(hass, owner):
        events.append("refresh")

    async def unload(owner):
        events.append("unload")
        if outcome == "unload_failed":
            return False
        entry.mock_state(hass=management.hass, state=ConfigEntryState.NOT_LOADED)
        if outcome == "expired":
            monkeypatch.setattr(setup, "monotonic", lambda: proof._accepted_at + 61)
        elif outcome == "cancel":
            raise asyncio.CancelledError
        elif outcome == "disabled":
            await management.hass.config_entries.async_set_disabled_by(
                entry.entry_id, ConfigEntryDisabler.USER
            )
        elif outcome == "removed":
            # Native removal deletes the owner before the integration hook.
            management.hass.config_entries._entries.pop(entry.entry_id)
        return True

    async def start(owner):
        events.append("setup")
        return outcome != "restart_failed"

    monkeypatch.setattr(handover, "async_wait_for_entry_refresh", wait)
    with (
        patch.object(
            management.hass.config_entries, "async_unload", side_effect=unload
        ),
        patch.object(management.hass.config_entries, "async_setup", side_effect=start),
        patch.object(management.hass.config_entries, "async_reload", return_value=True),
    ):
        if outcome in ("success", "disabled"):
            await complete_local_setup(
                management, journal, record.attempt_id, proof, contract=contract
            )
        else:
            with pytest.raises(
                asyncio.CancelledError if outcome == "cancel" else ConfigurationError
            ):
                await complete_local_setup(
                    management, journal, record.attempt_id, proof, contract=contract
                )
    assert ("setup" in events) == (
        outcome in ("success", "expired", "cancel", "restart_failed")
    )
    if outcome not in ("success", "disabled", "restart_failed", "removed"):
        assert management.current() == before


async def test_existing_42_replacement_does_not_stop_cloud(
    prepared, journal, monkeypatch
):
    _, management, cloud, target = prepared
    first = await pending(journal, management, target, monkeypatch)
    await commit(journal, management, first)
    await journal.discard(
        first[0].attempt_id, management.current().owner_id, confirmed=True
    )
    before = management.current()
    connection_id = first[1].edit.context.identity.connection_id
    edit = setup.begin_local_setup(
        before, target, first[1].edit.context.endpoint, connection_id=connection_id
    )
    record = await journal.import_token(
        edit, TOKEN, current=management.current, is_active=lambda: True
    )
    proof = await verified(monkeypatch, before, edit)
    with (
        patch.object(management.hass.config_entries, "async_unload") as unload,
        patch.object(management.hass.config_entries, "async_setup") as start,
    ):
        after = await complete_local_setup(
            management, journal, record.attempt_id, proof, contract=first[2]
        )
    unload.assert_not_called()
    start.assert_not_called()
    assert (
        after.connections[connection_id].auth_epoch
        == before.connections[connection_id].auth_epoch + 1
    )
    assert after.connections[cloud] == before.connections[cloud]
    assert first[1].edit.binding_id in after.bindings


def test_fingerprint_needs_selected_local_relationships():
    from tests.test_local_setup import starting

    snapshot, cloud, target = starting()
    assert local_state_fingerprint(snapshot, target, cloud, "missing") is None
    intent = CompletionIntent("local", "binding", "a" * 64, "process")
    assert CompletionIntent.from_data(intent.to_data()) == intent
    assert not intent.matches(snapshot, target)
