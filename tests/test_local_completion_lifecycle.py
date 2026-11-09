"""Pending enrollment lifecycle through native hooks, TLS and cancellation."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState

from custom_components.bosch_buderus_heating import async_remove_entry, async_setup
from custom_components.bosch_buderus_heating import local_enrollment as enrollment
from custom_components.bosch_buderus_heating.local_completion import (
    RecoveryStatus,
    ScopedIdentityContract,
)
from custom_components.bosch_buderus_heating.local_enrollment import (
    LocalEnrollmentError,
)
from custom_components.bosch_buderus_heating.local_handover import complete_local_setup
from custom_components.bosch_buderus_heating.local_probe import ProbePath
from custom_components.bosch_buderus_heating.local_setup import verify_local_setup
from tests.test_local_auth import PAYLOAD
from tests.test_local_auth import auth_gateway as auth_gateway
from tests.test_local_completion import commit, pending
from tests.test_local_completion import prepared as prepared
from tests.test_local_enrollment import create, reopened
from tests.test_local_enrollment import journal as journal
from tests.test_local_probe import response
from tests.test_local_setup import begin, registered
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import gateways as gateways
from tests.test_local_transport import wire_reply

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


async def test_v1_created_token_requires_fresh_actual_tls_before_native_completion(
    prepared,
    journal,
    auth_gateway,
    gateways,
):
    _, management, _, target = prepared
    before = management.current()
    edit = begin(before, target)
    calls, _, pin = await auth_gateway()
    record = await create(journal, before, edit, pin)
    legacy = json.loads(journal._path.read_text())
    legacy["version"] = 1
    del legacy["attempts"][0]["completion"]
    del legacy["attempts"][0]["pause_migrated"]
    journal._path.write_text(json.dumps(legacy))
    boot = reopened(journal)
    assert (await boot.records())[0].completion is None
    recovered = await boot.resume(record.attempt_id, before)

    async def reply(raw, writer):
        path = ProbePath(raw.split(b" ")[1].decode())
        writer.write(
            wire_reply(
                response(path, context=recovered.edit.context, gatewayId="000123").body
            )
        )
        await writer.drain()

    _, requests, _, resources = await gateways(reply)
    proof = await verify_local_setup(
        recovered.edit,
        recovered.credentials,
        replace(resources, context=recovered.edit.context),
        authentication_binding=recovered.authentication_binding,
        current=management.current,
        is_active=lambda: True,
    )
    after = await boot.commit_verified(
        record.attempt_id,
        proof,
        management,
        contract=ScopedIdentityContract(
            before.owner_id, target, "000123", "synthetic-device-contract"
        ),
    )
    assert after.minor_version == 2 and len(calls) == 1 and len(requests) == 3
    assert json.loads(journal._path.read_text())["version"] == 3
    assert (
        await boot.recovery_status(record.attempt_id, after) == RecoveryStatus.APPLIED
    )


@pytest.mark.parametrize("scope", ["owner", "connection"])
async def test_removal_during_auth_response_prevents_late_token_restoration(
    hass, journal, auth_gateway, scope
):
    entry, management, _, target = registered(hass, local=scope == "connection")
    before = management.current()
    release = asyncio.Event()

    async def reply(head, body, writer):
        await release.wait()
        writer.write(wire_reply(json.dumps(PAYLOAD).encode()))
        await writer.drain()

    calls, received, pin = await auth_gateway(reply)
    edit = begin(before, target, local=scope == "connection")
    auth = asyncio.create_task(create(journal, before, edit, pin))
    await received.wait()
    removal = asyncio.create_task(
        journal.remove_owner(entry.entry_id)
        if scope == "owner"
        else journal.remove_access(management, management.edit("local"))
    )
    # The removal marker must be visible before allowing the HTTP reply.
    for _ in range(100):
        if journal._removed_owners or journal._removed_connections:
            break
        await asyncio.sleep(0)
    assert journal._removed_owners or journal._removed_connections
    release.set()
    with pytest.raises(LocalEnrollmentError, match="owner_removed"):
        await auth
    await removal
    assert not await journal.records() and len(calls) == 1
    assert PAYLOAD["access_token"] not in journal._path.read_text()


async def test_removal_waits_for_atomic_write_despite_repeated_cancellation(
    prepared, journal, monkeypatch
):
    entry, management, _, target = prepared
    await pending(journal, management, target, monkeypatch)
    original = journal._io
    started, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def io(operation):
        nonlocal count
        count += 1
        if count == 2:
            started.set()
            await release.wait()
        return await original(operation)

    monkeypatch.setattr(journal, "_io", io)
    task = asyncio.create_task(journal.remove_owner(entry.entry_id))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await journal.records()


async def test_native_remove_hook_clears_only_selected_owner(
    hass, journal, monkeypatch
):
    entry, management, _, target = registered(hass)
    record, _, _ = await pending(journal, management, target, monkeypatch)
    other = replace(record, owner_id="other-owner", attempt_id="other-attempt")
    await journal._io(lambda: enrollment._save(journal._path, (record, other)))
    await async_remove_entry(hass, entry)
    assert await journal.records() == (other,)


async def test_cold_start_discards_absent_owner_but_preserves_other_pending(
    prepared, journal, monkeypatch
):
    _, management, _, target = prepared
    record, _, _ = await pending(journal, management, target, monkeypatch)
    orphan = replace(record, owner_id="deleted-owner", attempt_id="orphan-attempt")
    await journal._io(lambda: enrollment._save(journal._path, (record, orphan)))
    boot = reopened(journal)
    management.hass.set_state(CoreState.starting)
    await boot.initialize()
    assert await boot.records() == (record,)


async def test_corrupt_pending_storage_does_not_block_cloud_setup_or_removal(
    prepared, journal, caplog
):
    entry, management, _, _ = prepared
    journal._path.write_text("invalid synthetic-private-token")
    management.hass.set_state(CoreState.starting)
    with (
        patch(
            "custom_components.bosch_buderus_heating.async_setup_holiday_ui",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.bosch_buderus_heating.async_setup_controls_ui",
            new=AsyncMock(),
        ),
    ):
        assert await async_setup(management.hass, {})
    await async_remove_entry(management.hass, entry)
    assert "could not be reconciled" in caplog.text
    # FLOW-3: the removal sets the unreadable record aside instead of failing.
    assert "could not be cleared" not in caplog.text
    assert "set aside" in caplog.text
    assert not journal._path.exists()
    aside = journal._path.with_name(journal._path.name + ".corrupt")
    assert aside.read_text() == "invalid synthetic-private-token"
    assert await journal.records() == ()
    assert "synthetic-private-token" not in caplog.text


async def test_cancellation_after_accepted_update_waits_for_runtime_restart(
    prepared, journal, monkeypatch
):
    entry, management, _, target = prepared
    record, proof, contract = await pending(journal, management, target, monkeypatch)
    entry.mock_state(hass=management.hass, state=ConfigEntryState.LOADED)
    started, release = asyncio.Event(), asyncio.Event()

    async def unload(owner):
        entry.mock_state(hass=management.hass, state=ConfigEntryState.NOT_LOADED)
        return True

    async def restart(owner):
        started.set()
        await release.wait()
        return True

    with (
        patch.object(
            management.hass.config_entries, "async_unload", side_effect=unload
        ),
        patch.object(
            management.hass.config_entries, "async_setup", side_effect=restart
        ),
    ):
        task = asyncio.create_task(
            complete_local_setup(
                management, journal, record.attempt_id, proof, contract=contract
            )
        )
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (
        await journal.recovery_status(record.attempt_id, management.current())
        == RecoveryStatus.APPLIED
    )


async def test_storage_failure_before_acceptance_keeps_previous_configuration(
    prepared, journal, monkeypatch
):
    _, management, _, target = prepared
    bundle = await pending(journal, management, target, monkeypatch)
    before = management.current()

    def fail(*args):
        raise OSError("synthetic-private-storage-error")

    monkeypatch.setattr(enrollment, "_save", fail)
    with pytest.raises(LocalEnrollmentError, match="storage_failed"):
        await commit(journal, management, bundle)
    assert management.current() == before
    assert (await journal.records())[0].completion is None


async def test_cloud_refresh_failure_never_stops_runtime(
    prepared, journal, monkeypatch
):
    entry, management, _, target = prepared
    record, proof, contract = await pending(journal, management, target, monkeypatch)
    entry.mock_state(hass=management.hass, state=ConfigEntryState.LOADED)
    with (
        patch(
            "custom_components.bosch_buderus_heating.local_handover.async_wait_for_entry_refresh",
            side_effect=TimeoutError,
        ),
        patch.object(management.hass.config_entries, "async_unload") as unload,
        pytest.raises(TimeoutError),
    ):
        await complete_local_setup(
            management, journal, record.attempt_id, proof, contract=contract
        )
    unload.assert_not_called()
    assert (await journal.records())[0].completion is None


async def test_cancelled_unload_finishes_before_restoring_original_runtime(
    prepared,
    journal,
    monkeypatch,
):
    entry, management, _, target = prepared
    before = management.current()
    record, proof, contract = await pending(journal, management, target, monkeypatch)
    entry.mock_state(hass=management.hass, state=ConfigEntryState.LOADED)
    started, release = asyncio.Event(), asyncio.Event()

    async def unload(owner):
        entry.mock_state(
            hass=management.hass, state=ConfigEntryState.UNLOAD_IN_PROGRESS
        )
        started.set()
        await release.wait()
        entry.mock_state(hass=management.hass, state=ConfigEntryState.NOT_LOADED)
        return True

    with (
        patch.object(
            management.hass.config_entries, "async_unload", side_effect=unload
        ),
        patch.object(
            management.hass.config_entries, "async_setup", return_value=True
        ) as start,
    ):
        task = asyncio.create_task(
            complete_local_setup(
                management, journal, record.attempt_id, proof, contract=contract
            )
        )
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    start.assert_awaited_once_with(entry.entry_id)
    assert management.current() == before
    assert (await journal.records())[0].completion is None
