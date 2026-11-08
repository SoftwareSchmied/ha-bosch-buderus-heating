"""Durable enrollment boundaries with real files, TLS and cancellation."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace

import aiohttp
import pytest

from custom_components.bosch_buderus_heating import local_enrollment as enrollment
from custom_components.bosch_buderus_heating.configuration import (
    load_configuration,
)
from custom_components.bosch_buderus_heating.local_auth import (
    AuthState,
)
from custom_components.bosch_buderus_heating.local_budget import (
    BUDGET_KEY,
    local_budget_manager,
)
from custom_components.bosch_buderus_heating.local_enrollment import (
    JOURNAL_KEY,
    LocalEnrollmentError,
    LocalEnrollmentJournal,
    enrollment_journal,
)
from custom_components.bosch_buderus_heating.local_probe import ProbePath
from custom_components.bosch_buderus_heating.local_setup import verify_local_setup
from custom_components.bosch_buderus_heating.local_transport import PortRole
from tests.test_local_auth import LABEL, PAYLOAD
from tests.test_local_auth import auth_gateway as auth_gateway
from tests.test_local_probe import response
from tests.test_local_setup import begin, proposal, starting
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import gateways as gateways
from tests.test_local_transport import wire_reply

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")


@pytest.fixture
def journal(hass, tmp_path):
    # Actual atomic helper and actual temporary files, independently of HA's
    # mocked Store fixture. Production obtains the path from hass.config.path.
    result = enrollment_journal(hass)
    result._path = tmp_path / JOURNAL_KEY
    local_budget_manager(hass)._path = tmp_path / BUDGET_KEY
    return result


async def create(journal, snapshot, edit, pin, *, active=lambda: True):
    return await journal.create(
        edit,
        LABEL,
        replace(pin, context=edit.context),
        current=lambda: snapshot,
        is_active=active,
    )


def reopened(journal):
    result = LocalEnrollmentJournal(journal._hass)
    result._path = journal._path
    return result


async def test_legacy_pause_migration_ignores_wall_clock_and_runs_only_once(
    journal, monkeypatch
):
    from tests.test_local_budget import Clock

    manager = local_budget_manager(journal._hass)
    manager._clock = Clock()
    snapshot, _, target = starting()
    edit = begin(snapshot, target)
    record = await journal.import_token(
        edit, "synthetic-token", current=lambda: snapshot, is_active=lambda: True
    )
    saved = json.loads(journal._path.read_text())
    saved["version"] = 2
    saved["attempts"][0].pop("pause_migrated")
    saved["attempts"][0]["started_at"] = 1000
    saved["attempts"][0]["retry_not_before"] = 8200
    journal._path.write_text(json.dumps(saved))
    monkeypatch.setattr(enrollment, "time", lambda: 10**12)
    migrated = (await reopened(journal).records())[0]
    assert migrated.credentials == record.credentials
    assert await journal.remaining_pause(migrated) == 7200
    saved = json.loads(journal._path.read_text())
    assert saved["version"] == 3 and saved["attempts"][0]["pause_migrated"]
    manager._clock.advance(100)
    assert await journal.remaining_pause((await journal.records())[0]) == 7100
    await journal.discard(record.attempt_id, record.owner_id, confirmed=True)
    assert await journal.remaining_pause(record) == 7100
    with pytest.raises(LocalEnrollmentError, match="enrollment_retry_later"):
        await journal.import_token(
            edit,
            "new-synthetic-token",
            current=lambda: snapshot,
            is_active=lambda: True,
        )
    manager._clock.advance(7100)
    fresh = await journal.import_token(
        edit, "new-synthetic-token", current=lambda: snapshot, is_active=lambda: True
    )
    assert fresh.attempt_id != record.attempt_id


async def test_received_token_survives_restart_then_requires_fresh_tls_reads(
    journal, auth_gateway, gateways
):
    snapshot, cloud, target = starting()
    edit = begin(snapshot, target)
    calls, _, pin = await auth_gateway()
    record = await create(journal, snapshot, edit, pin)
    assert record.state == AuthState.RECEIVED and len(calls) == 1
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import sys; "
            "from custom_components.bosch_buderus_heating.local_enrollment "
            "import _load; "
            "r = _load(Path(sys.argv[1])); "
            "print(r[0].state.value, r[0].credentials is not None)"
        ),
        str(journal._path),
    ]
    restarted = await journal._hass.async_add_executor_job(
        lambda: subprocess.run(command, check=True, capture_output=True, text=True)
    )
    assert restarted.stdout.strip() == "received True"
    saved = journal._path.read_text()
    assert LABEL.password not in saved and LABEL.login not in saved
    assert PAYLOAD["access_token"] in saved and "token_id" not in saved
    assert os.stat(journal._path).st_mode & 0o777 == 0o600
    assert snapshot.to_data() == starting()[0].to_data()
    loaded = await reopened(journal).resume(record.attempt_id, snapshot)
    assert loaded.edit.context != edit.context and len(calls) == 1
    assert loaded.credentials == record.credentials
    assert not hasattr(loaded, "access")

    async def reply(raw, writer):
        path = ProbePath(raw.split(b" ")[1].decode())
        writer.write(
            wire_reply(
                response(path, context=loaded.edit.context, gatewayId="000123").body
            )
        )
        await writer.drain()

    _, reads, _, binding = await gateways(reply, certificate=1)
    proof = await verify_local_setup(
        loaded.edit,
        loaded.credentials,
        replace(binding, context=loaded.edit.context),
        authentication_binding=loaded.authentication_binding,
        current=lambda: snapshot,
        is_active=lambda: True,
    )
    after = proposal(loaded.edit, proof, snapshot)
    assert after.targets == snapshot.targets or set(after.targets) == set(
        snapshot.targets
    )
    assert after.connections[cloud] == snapshot.connections[cloud]
    assert len(reads) == 3 and all(r.startswith(b"GET ") for r in reads)
    assert (await journal.records())[0].credentials is not None
    for obj in (record, loaded):
        assert PAYLOAD["access_token"] not in repr(obj)
        assert edit.context.endpoint.host not in repr(obj)
        assert pin.sha256.hex() not in repr(obj)


@pytest.mark.parametrize("local", [False, True])
async def test_resume_preserves_cloud_rotation_and_checks_selected_configuration(
    journal, auth_gateway, local
):
    snapshot, cloud, target = starting(local=local)
    edit = begin(snapshot, target, local=local)
    calls, _, pin = await auth_gateway()
    record = await create(journal, snapshot, edit, pin)
    data, options = snapshot.to_data(), snapshot.to_options()
    data["connections"][cloud]["credentials"]["access_token"] = "rotated-cloud-token"
    current = load_configuration(snapshot.owner_id, data, options)
    recovered = await journal.resume(record.attempt_id, current)
    if local:
        assert recovered.edit.context.identity.connection_id == "local"
        assert recovered.edit.binding_id == edit.binding_id
    options["targets"][target]["enabled"] = False
    changed = load_configuration(snapshot.owner_id, data, options)
    with pytest.raises(LocalEnrollmentError, match="enrollment_context_changed"):
        await journal.resume(record.attempt_id, changed)
    assert len(calls) == 1


async def test_marker_exists_before_gateway_can_receive_form(journal, auth_gateway):
    seen = []

    async def reply(head, body, writer):
        # Read from disk while the network call is in progress.
        raw = json.loads(journal._path.read_text())
        seen.append(raw["attempts"][0])
        writer.write(wire_reply(json.dumps(PAYLOAD).encode()))
        await writer.drain()

    snapshot, _, target = starting()
    calls, _, pin = await auth_gateway(reply)
    await create(journal, snapshot, begin(snapshot, target), pin)
    assert len(calls) == len(seen) == 1
    assert seen[0]["state"] == "uncertain" and seen[0]["credentials"] is None


@pytest.mark.parametrize("cancel", [False, True])
async def test_lost_response_survives_restart_and_blocks_repeat(
    journal, auth_gateway, cancel
):
    async def reply(head, body, writer):
        if cancel:
            await asyncio.Future()
        else:
            writer.close()

    snapshot, _, target = starting()
    edit = begin(snapshot, target)
    calls, received, pin = await auth_gateway(reply)
    task = asyncio.create_task(create(journal, snapshot, edit, pin))
    await received.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).state == AuthState.UNCERTAIN
    loaded = reopened(journal)
    pending = (await loaded.records())[0]
    assert pending.state == AuthState.UNCERTAIN and pending.credentials is None
    with pytest.raises(LocalEnrollmentError, match="enrollment_already_pending"):
        await create(loaded, snapshot, edit, pin)
    with pytest.raises(
        LocalEnrollmentError, match="enrollment_has_no_recoverable_token"
    ):
        await loaded.resume(pending.attempt_id, snapshot)
    assert len(calls) == 1


async def test_explicit_acknowledgment_is_needed_and_never_revokes(
    journal, auth_gateway
):
    snapshot, _, target = starting()
    edit = begin(snapshot, target)
    calls, _, pin = await auth_gateway()
    record = await create(journal, snapshot, edit, pin)
    with pytest.raises(LocalEnrollmentError, match="enrollment_confirmation_required"):
        await journal.discard(record.attempt_id, snapshot.owner_id)
    with pytest.raises(LocalEnrollmentError, match="enrollment_not_found"):
        await journal.discard(record.attempt_id, "foreign-owner", confirmed=True)
    await journal.discard(record.attempt_id, snapshot.owner_id, confirmed=True)
    assert PAYLOAD["access_token"] not in journal._path.read_text()
    assert len(calls) == 1
    new = await create(journal, snapshot, edit, pin)
    assert new.attempt_id != record.attempt_id and len(calls) == 2
    assert all(head.startswith(b"POST /auth/token ") for head, _ in calls)


async def test_acknowledgment_cannot_bypass_retry_after(
    journal, auth_gateway, monkeypatch
):
    clock = 1000
    steady_clock = 1000
    local_budget_manager(journal._hass)._clock = lambda: steady_clock
    monkeypatch.setattr(enrollment, "time", lambda: clock)

    async def reply(head, body, writer):
        writer.write(wire_reply(status=429, headers={"Retry-After": "90"}))
        await writer.drain()

    snapshot, _, target = starting()
    edit = begin(snapshot, target)
    calls, _, pin = await auth_gateway(reply)
    record = await create(journal, snapshot, edit, pin)
    await journal.discard(record.attempt_id, snapshot.owner_id, confirmed=True)
    with pytest.raises(LocalEnrollmentError, match="enrollment_retry_later"):
        await create(reopened(journal), snapshot, edit, pin)
    clock += 90
    with pytest.raises(LocalEnrollmentError, match="enrollment_retry_later"):
        await create(journal, snapshot, edit, pin)
    steady_clock += 90
    assert (await create(journal, snapshot, edit, pin)).state == AuthState.REJECTED
    assert len(calls) == 2


async def test_concurrent_attempts_share_lock_and_only_one_post(
    journal, auth_gateway, hass
):
    assert enrollment_journal(hass) is journal
    snapshot, _, target = starting()
    calls, _, pin = await auth_gateway()
    attempts = [
        create(journal, snapshot, begin(snapshot, target), pin) for _ in range(2)
    ]
    results = await asyncio.gather(*attempts, return_exceptions=True)
    assert sum(isinstance(r, LocalEnrollmentError) for r in results) == 1
    assert len(calls) == 1 and len(await journal.records()) == 1


async def test_initial_save_failure_sends_nothing(journal, auth_gateway, monkeypatch):
    snapshot, _, target = starting()
    calls, _, pin = await auth_gateway()
    monkeypatch.setattr(
        enrollment,
        "save_json",
        lambda *a, **k: (_ for _ in ()).throw(OSError("private path")),
    )
    with pytest.raises(
        LocalEnrollmentError, match="local_enrollment_storage_failed"
    ) as error:
        await create(journal, snapshot, begin(snapshot, target), pin)
    assert "private path" not in str(error.value) and not calls


async def test_final_save_failure_keeps_uncertain_marker(
    journal, auth_gateway, monkeypatch
):
    snapshot, _, target = starting()
    edit = begin(snapshot, target)
    calls, _, pin = await auth_gateway()
    save, count = enrollment._save, 0

    def failing(path, entries):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("private disk error")
        save(path, entries)

    monkeypatch.setattr(enrollment, "_save", failing)
    with pytest.raises(LocalEnrollmentError, match="local_enrollment_storage_failed"):
        await create(journal, snapshot, edit, pin)
    assert (await reopened(journal).records())[0].state == AuthState.UNCERTAIN
    with pytest.raises(LocalEnrollmentError, match="enrollment_already_pending"):
        await create(journal, snapshot, edit, pin)
    assert len(calls) == 1


async def test_response_saved_even_when_ui_inactive(journal, auth_gateway):
    active = True

    async def reply(head, body, writer):
        nonlocal active
        active = False
        writer.write(wire_reply(json.dumps(PAYLOAD).encode()))
        await writer.drain()

    snapshot, _, target = starting()
    calls, _, pin = await auth_gateway(reply)
    assert (
        await create(
            journal, snapshot, begin(snapshot, target), pin, active=lambda: active
        )
    ).state == AuthState.RECEIVED
    assert len(calls) == 1


@pytest.mark.parametrize("final_write", [False, True])
async def test_cancel_during_atomic_write_waits_and_preserves_restart_state(
    journal, auth_gateway, monkeypatch, final_write
):
    snapshot, _, target = starting()
    edit = begin(snapshot, target)
    calls, _, pin = await auth_gateway()
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    save, count = enrollment._save, 0

    def slow(path, entries):
        nonlocal count
        count += 1
        if count == (2 if final_write else 1):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
        save(path, entries)

    monkeypatch.setattr(enrollment, "_save", slow)
    task = asyncio.create_task(create(journal, snapshot, edit, pin))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    blocked = asyncio.create_task(create(journal, snapshot, edit, pin))
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(LocalEnrollmentError, match="enrollment_already_pending"):
        await blocked
    record = (await reopened(journal).records())[0]
    assert record.state == (AuthState.RECEIVED if final_write else AuthState.UNCERTAIN)
    assert len(calls) == int(final_write)


@pytest.mark.parametrize(
    "corruption",
    [
        b"{",
        b"[]",
        b'{"version":1,"version":1,"attempts":[]}',
        b'{"version":4,"attempts":[]}',
        b'{"version":true,"attempts":[]}',
        b'{"version":1,"attempts":[{}]}',
        b"x" * (enrollment.MAX_JOURNAL_BYTES + 1),
    ],
)
async def test_corrupt_journal_never_looks_empty(journal, auth_gateway, corruption):
    journal._path.write_bytes(corruption)
    snapshot, _, target = starting()
    calls, _, pin = await auth_gateway()
    with pytest.raises(LocalEnrollmentError, match="local_enrollment_storage_failed"):
        await create(journal, snapshot, begin(snapshot, target), pin)
    assert not calls and journal._path.read_bytes() == corruption


async def test_wrong_owner_changed_target_and_role_cannot_use_pending_token(
    journal, auth_gateway
):
    snapshot, _, target = starting()
    edit = begin(snapshot, target)
    calls, _, pin = await auth_gateway()
    record = await create(journal, snapshot, edit, pin)
    foreign = load_configuration("foreign", snapshot.to_data(), snapshot.to_options())
    with pytest.raises(LocalEnrollmentError, match="enrollment_not_found"):
        await journal.resume(record.attempt_id, foreign)
    with pytest.raises(LocalEnrollmentError, match="authentication_binding_required"):
        await create(journal, snapshot, edit, replace(pin, role=PortRole.RESOURCES))
    with pytest.raises(LocalEnrollmentError, match="enrollment_context_changed"):
        await create(journal, snapshot, edit, pin, active=lambda: False)
    assert len(calls) == 1


async def test_no_silent_success_when_write_helper_does_not_persist(
    journal, auth_gateway, monkeypatch
):
    snapshot, _, target = starting()
    calls, _, pin = await auth_gateway()
    monkeypatch.setattr(enrollment, "save_json", lambda *a, **k: None)
    with pytest.raises(LocalEnrollmentError, match="local_enrollment_storage_failed"):
        await create(journal, snapshot, begin(snapshot, target), pin)
    assert not calls


async def test_received_token_persisted_before_cancelled_session_cleanup(
    journal, auth_gateway, monkeypatch
):
    snapshot, _, target = starting()
    edit = begin(snapshot, target)
    calls, _, pin = await auth_gateway()
    cleaning = asyncio.Event()
    original = aiohttp.ClientSession.__aexit__

    async def cleanup(session, *args):
        await original(session, *args)
        cleaning.set()
        await asyncio.Future()

    monkeypatch.setattr(aiohttp.ClientSession, "__aexit__", cleanup)
    task = asyncio.create_task(create(journal, snapshot, edit, pin))
    await cleaning.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pending = (await reopened(journal).records())[0]
    assert (
        pending.state == AuthState.RECEIVED
        and pending.credentials.token == PAYLOAD["access_token"]
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("authentication_sha256", "a"),
        ("basis_sha256", "Z" * 64),
        ("started_at", -1),
        ("retry_not_before", float("nan")),
        ("resolved", 1),
        ("host", "https://wrong.invalid"),
        (
            "credentials",
            {
                "token": "private",
                "token_type": "Bearer",
                "origin": "imported",
                "reported_scope": ["open_api.read"],
            },
        ),
        ("credentials", None),
        ("state", "invented"),
        ("owner_id", ""),
        ("problem", "private backend error"),
    ],
)
async def test_damaged_pending_record_blocks_new_creation(
    journal, auth_gateway, field, value
):
    snapshot, _, target = starting()
    edit = begin(snapshot, target)
    calls, _, pin = await auth_gateway()
    await create(journal, snapshot, edit, pin)
    raw = json.loads(journal._path.read_text())
    raw["attempts"][0][field] = value
    journal._path.write_text(json.dumps(raw))
    with pytest.raises(LocalEnrollmentError, match="local_enrollment_storage_failed"):
        await create(reopened(journal), snapshot, edit, pin)
    assert len(calls) == 1


async def test_conflicting_owner_and_full_journal_do_not_start_another_request(
    journal, auth_gateway, monkeypatch
):
    snapshot, _, target = starting()
    calls, _, pin = await auth_gateway()
    record = await create(journal, snapshot, begin(snapshot, target), pin)
    other = load_configuration("other-owner", snapshot.to_data(), snapshot.to_options())
    with pytest.raises(LocalEnrollmentError, match="enrollment_already_pending"):
        await create(journal, other, begin(other, target), pin)
    monkeypatch.setattr(enrollment, "MAX_PENDING_ENROLLMENTS", 1)
    with pytest.raises(LocalEnrollmentError, match="local_enrollment_storage_failed"):
        await create(journal, other, begin(other, target, host="second.invalid"), pin)
    assert (await journal.records())[0] == record and len(calls) == 1


async def test_config_change_while_initial_save_runs_prevents_post(
    journal, auth_gateway, monkeypatch
):
    snapshot, _, target = starting()
    current = snapshot
    edit = begin(snapshot, target)
    calls, _, pin = await auth_gateway()
    original = journal._io
    count = 0

    async def io(operation):
        nonlocal count, current
        result = await original(operation)
        count += 1
        if count == 2:
            options = snapshot.to_options()
            options["targets"][target]["enabled"] = False
            current = load_configuration(snapshot.owner_id, snapshot.to_data(), options)
        return result

    monkeypatch.setattr(journal, "_io", io)
    record = await journal.create(
        edit,
        LABEL,
        replace(pin, context=edit.context),
        current=lambda: current,
        is_active=lambda: True,
    )
    assert record.state == AuthState.NOT_SENT and not calls
