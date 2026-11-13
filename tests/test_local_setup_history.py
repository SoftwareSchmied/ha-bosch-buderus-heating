"""The last local setup attempts of an entry: codes and times, never data."""

import json
import logging
from datetime import datetime

import pytest

from custom_components.bosch_buderus_heating import local_flow as flow
from custom_components.bosch_buderus_heating import local_setup as setup
from custom_components.bosch_buderus_heating import local_setup_history as history
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.local_setup_history import (
    MAX_ATTEMPTS,
    LocalSetupHistory,
    async_forget_local_setup_history,
    local_setup_history,
)
from custom_components.bosch_buderus_heating.local_transport import (
    CertificateObservation,
)
from tests.ha_test_helpers import flush_store
from tests.test_local_auth import LABEL
from tests.test_local_auth import auth_gateway as auth_gateway
from tests.test_local_enrollment import journal as journal
from tests.test_local_flow import imported, start, submit
from tests.test_local_flow import supported as supported
from tests.test_local_setup import TOKEN, registered
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import wire_reply

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")

KEY = "bosch_buderus_heating.local_setup_history.{}"
SECRET_ADDRESS = "https://private:secret@gateway/path"


def _shape(attempts) -> list[tuple[str, str, str | None]]:
    return [(a["step"], a["result"], a["error"]) for a in attempts]


def _own(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("custom_components.bosch_buderus_heating")
        and r.levelno >= logging.INFO
        and not r.getMessage().startswith("Loaded ")
    ]


def _no_data(text: str, *values: str) -> None:
    for value in values:
        assert value not in text, value


async def test_attempts_keep_only_time_step_and_code(hass, caplog) -> None:
    item = LocalSetupHistory(hass, "entry")
    with caplog.at_level(logging.INFO, logger=history.__name__):
        item.add("local_auth", "local_invalid_credentials")
        item.add("local_result", None)
    attempts = await item.async_attempts()
    assert _shape(attempts) == [
        ("local_auth", "failed", "local_invalid_credentials"),
        ("local_result", "succeeded", None),
    ]
    for attempt in attempts:
        assert set(attempt) == {"at", "step", "result", "error"}
        at = datetime.fromisoformat(attempt["at"])
        assert at.utcoffset().total_seconds() == 0 and at.microsecond == 0
    # One line per failed attempt, with the code only.
    assert [r.getMessage() for r in caplog.records] == [
        "A local setup attempt failed: local_invalid_credentials"
    ]
    assert caplog.records[0].levelno == logging.INFO


async def test_only_fixed_codes_are_kept(hass) -> None:
    item = LocalSetupHistory(hass, "entry")
    item.add("local_auth", "a password: hunter2")
    item.add("local_method", "unexpected:RuntimeError")
    item.add("Not A Step!", "local_timeout")
    assert _shape(await item.async_attempts()) == [
        ("local_auth", "failed", "unknown"),
        ("local_method", "failed", "unexpected:RuntimeError"),
    ]


async def test_the_last_ten_attempts_survive_a_restart(hass, hass_storage) -> None:
    item = local_setup_history(hass, "entry")
    for number in range(MAX_ATTEMPTS + 3):
        item.add("local_address", f"code_{number}")
    attempts = await item.async_attempts()
    assert [a["error"] for a in attempts] == [
        f"code_{number}" for number in range(3, MAX_ATTEMPTS + 3)
    ]
    await flush_store(item._store)
    stored = hass_storage[KEY.format("entry")]["data"]["attempts"]
    assert stored == attempts
    restarted = LocalSetupHistory(hass, "entry")
    assert await restarted.async_attempts() == attempts
    restarted.add("local_result", None)
    after = await restarted.async_attempts()
    assert len(after) == MAX_ATTEMPTS and after[-1]["result"] == "succeeded"


async def test_stored_data_beyond_the_known_fields_is_dropped(hass, hass_storage):
    hass_storage[KEY.format("entry")] = {
        "version": 1,
        "key": KEY.format("entry"),
        "data": {
            "attempts": [
                {
                    "at": "2026-11-13T10:00:00+00:00",
                    "step": "local_auth",
                    "result": "failed",
                    "error": "local_timeout",
                    "token": "secret-token",
                },
                {"at": "yesterday", "step": "local_auth", "result": "failed"},
                {
                    "at": "2026-11-13T10:00:00",  # without a time zone
                    "step": "local_auth",
                    "result": "succeeded",
                    "error": None,
                },
                {
                    "at": "2026-11-13T10:01:00+00:00",
                    "step": "local_auth",
                    "result": "failed",
                    "error": "192.0.2.1",
                },
                "not an attempt",
            ]
        },
    }
    attempts = await LocalSetupHistory(hass, "entry").async_attempts()
    assert attempts == [
        {
            "at": "2026-11-13T10:00:00+00:00",
            "step": "local_auth",
            "result": "failed",
            "error": "local_timeout",
        }
    ]


async def test_a_damaged_file_starts_anew(hass, hass_storage) -> None:
    hass_storage[KEY.format("entry")] = {"version": 1, "data": ["broken"]}
    assert await LocalSetupHistory(hass, "entry").async_attempts() == []


async def test_a_rejected_sign_in_and_the_success_are_noted_without_data(
    hass, supported, journal, auth_gateway, monkeypatch, caplog
):
    entry, _, _, target = registered(hass)
    statuses = [401, 200]

    async def reply(head, body, writer):
        payload = {
            "access_token": TOKEN,
            "token_type": "Bearer",
            "scope": "open_api.read",
        }
        writer.write(wire_reply(json.dumps(payload).encode(), status=statuses.pop(0)))
        await writer.drain()

    _, _, pin = await auth_gateway(reply)

    async def inspect(context, role, *, is_current, budget):
        return CertificateObservation(context, role, pin.sha256, "127.0.0.1", "TLSv1.3")

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    manager = hass.config_entries.options
    with caplog.at_level(logging.INFO, logger=history.__name__):
        result = await start(hass, entry, target)
        result = await submit(hass, manager, result, {"address": SECRET_ADDRESS})
        result = await submit(
            hass, manager, result, {"address": "gateway.example.invalid"}
        )
        result = await submit(hass, manager, result, {"method": "label"})
        result = await submit(hass, manager, result, {"confirm": True})
        values = {"login": LABEL.login, "password": LABEL.password, "unlocked": True}
        result = await submit(hass, manager, result, {**values, "unlocked": False})
        result = await submit(hass, manager, result, values)
        assert result["errors"] == {"base": "local_invalid_credentials"}
        result = await submit(hass, manager, result, values)
        result = await submit(hass, manager, result, {"confirm": True})
        result = await submit(hass, manager, result, {"confirm": True})
        assert result["reason"] == "local_setup_complete"
        await hass.async_block_till_done()
    attempts = await local_setup_history(hass, entry.entry_id).async_attempts()
    assert _shape(attempts) == [
        ("local_address", "failed", "local_invalid_address"),
        ("local_auth", "failed", "local_unlock_required"),
        ("local_auth", "failed", "local_invalid_credentials"),
        ("local_result", "succeeded", None),
    ]
    assert _own(caplog) == [
        "A local setup attempt failed: local_invalid_address",
        "A local setup attempt failed: local_unlock_required",
        "A local setup attempt failed: local_invalid_credentials",
    ]
    report = await async_get_config_entry_diagnostics(hass, entry)
    assert report["local_setup_attempts"] == attempts
    _no_data(
        json.dumps(report, default=str) + json.dumps(attempts) + " ".join(_own(caplog)),
        LABEL.login,
        LABEL.password,
        TOKEN,
        "secret",
        "gateway.example.invalid",
        "127.0.0.1",
        pin.sha256.hex(),
        "000123",
    )


async def test_an_unexpected_error_names_only_its_type(
    hass, supported, journal, monkeypatch
):
    entry, _, _, target = registered(hass)

    async def inspect(context, role, *, is_current, budget):
        raise RuntimeError("gateway.example.invalid said secret things")

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "label"})
    assert result["step_id"] == "local_retry"
    assert result["errors"] == {"base": "local_unexpected"}
    # Retrying fails the same way: a second attempt, no third from the form.
    result = await submit(hass, manager, result, {})
    assert result["step_id"] == "local_retry"
    await hass.async_block_till_done()
    attempts = await local_setup_history(hass, entry.entry_id).async_attempts()
    assert _shape(attempts) == [
        ("local_method", "failed", "unexpected:RuntimeError"),
        ("local_retry", "failed", "unexpected:RuntimeError"),
    ]
    assert "secret" not in json.dumps(attempts)


async def test_a_removed_entry_forgets_its_attempts(hass, hass_storage) -> None:
    item = local_setup_history(hass, "entry")
    item.add("local_auth", "local_timeout")
    await item.async_attempts()
    await flush_store(item._store)
    assert KEY.format("entry") in hass_storage
    await async_forget_local_setup_history(hass, "entry")
    assert KEY.format("entry") not in hass_storage
    assert await local_setup_history(hass, "entry").async_attempts() == []


async def test_a_failed_stage_shown_in_the_pending_form_is_one_attempt(
    hass, supported, journal, monkeypatch
):
    entry, _, _, target = registered(hass)
    read = setup.read_local_probe
    failures = [ValueError("timeout")]

    async def flaky(*args, **kwargs):
        if failures:
            raise failures.pop()
        return await read(*args, **kwargs)

    monkeypatch.setattr(setup, "read_local_probe", flaky)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_pending"
    assert result["errors"]["base"] == "local_unexpected"
    await hass.async_block_till_done()
    attempts = await local_setup_history(hass, entry.entry_id).async_attempts()
    # Noted once when the stage failed, not again by the form showing it.
    assert _shape(attempts) == [
        ("local_certificate", "failed", "unexpected:ValueError")
    ]


async def test_a_broken_history_never_stops_the_dialog_or_the_report(
    hass, supported, journal, monkeypatch
):
    from custom_components.bosch_buderus_heating import diagnostics

    entry, _, _, target = registered(hass)

    def broken(*args):
        raise RuntimeError("no history")

    monkeypatch.setattr(flow, "local_setup_history", broken)
    monkeypatch.setattr(diagnostics, "local_setup_history", broken)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": SECRET_ADDRESS})
    assert result["errors"] == {"address": "local_invalid_address"}
    report = await async_get_config_entry_diagnostics(hass, entry)
    assert report["local_setup_attempts"] == []
