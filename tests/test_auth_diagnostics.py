"""Token refresh history with synthetic credentials and restarts."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, flush_store

from custom_components.bosch_buderus_heating.auth_diagnostics import (
    _RECORDERS,
    MAX_EVENTS,
    async_load_auth_diagnostics,
    async_remove_auth_diagnostics,
    auth_diagnostics_for,
)
from custom_components.bosch_buderus_heating.auth_lifecycle import (
    _TOKEN_MANAGERS,
    entry_auth,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.data import (
    tokens_from_data,
    tokens_to_data,
)
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    RefreshTokenRejected,
    RequestTimeout,
    TokenEndpointRejection,
)

SECRETS = ("access-0", "access-1", "refresh-0", "refresh-1")


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class ScriptedOAuth:
    """Rotate once, then reject every further refresh like a strict server."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.presented: list[str] = []
        self.accepted: set[str] = {"refresh-0"}

    async def refresh(self, refresh_token: str) -> AuthTokens:
        self.presented.append(refresh_token)
        if refresh_token not in self.accepted:
            raise RefreshTokenRejected(
                rejection=TokenEndpointRejection(
                    400, "invalid_grant", "Token is not active", "json"
                )
            )
        self.accepted.discard(refresh_token)
        number = len(self.presented)
        self.accepted.add(f"refresh-{number}")
        return AuthTokens(
            f"access-{number}",
            f"refresh-{number}",
            expires_at=self.clock() + 300,
            issued_at=self.clock(),
            refresh_expires_in=2_592_000,
            refresh_token_returned=True,
        )


def _storage_key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.auth_history.{entry.entry_id}"


def _restart(hass: Any) -> None:
    """Drop everything Home Assistant keeps only in memory."""
    hass.data.pop(_RECORDERS, None)
    hass.data.pop(_TOKEN_MANAGERS, None)


def _assert_no_secrets(value: object, hass_storage: dict[str, Any], entry: Any) -> None:
    rendered = repr(value)
    for secret in SECRETS:
        assert secret not in rendered
    stored = hass_storage[_storage_key(entry)]["data"]
    assert stored["key"] not in rendered
    for item in stored["issued"]:
        assert item["fingerprint"] not in rendered


async def test_history_detects_reuse_of_a_rotated_token_after_restart(
    hass, hass_storage, caplog
) -> None:
    clock = Clock()
    oauth = ScriptedOAuth(clock)
    # Expired on the real clock used by the token manager.
    original = AuthTokens("access-0", "refresh-0", 0)
    entry = MockConfigEntry(domain=DOMAIN, data=tokens_to_data(original))
    entry.add_to_hass(hass)

    first = await async_load_auth_diagnostics(hass, entry.entry_id, clock=clock)
    manager, _backoff = entry_auth(hass, entry, oauth)
    clock.now += 60
    assert await manager.get_access_token() == "access-1"
    assert tokens_from_data(entry.data).refresh_token == "refresh-1"
    await flush_store(first.store)
    assert _storage_key(entry) in hass_storage

    # Simulate a restart whose credential write was lost: the entry still holds
    # the rotated refresh-0, while the history already knows refresh-1.
    _restart(hass)
    hass.config_entries.async_update_entry(entry, data=tokens_to_data(original))
    clock.now += 3600
    caplog.set_level(logging.WARNING)
    recorder = await async_load_auth_diagnostics(hass, entry.entry_id, clock=clock)
    manager, _backoff = entry_auth(hass, entry, oauth)
    assert "older than the newest one" in caplog.text

    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()
    assert oauth.presented == ["refresh-0", "refresh-0"]

    report = recorder.diagnostics(manager)
    events = report["events"]
    assert [event["event"] for event in events] == [
        "credentials_loaded",
        "refresh",
        "persist",
        "credentials_loaded",
        "refresh",
    ]
    loaded = events[3]
    assert loaded["reason"] == "start"
    assert loaded["refresh_token"]["state"] == "older_than_latest"
    assert loaded["refresh_token"]["newer_tokens"] == 1
    success = events[1]
    assert success["outcome"] == "success"
    assert success["trigger"] == "expiry"
    assert success["refresh_token_rotated"] is True
    assert success["access_token_lifetime_s"] == 300
    assert success["refresh_token_lifetime_s"] == 2_592_000
    assert events[2]["outcome"] == "saved"
    rejected = events[4]
    assert rejected["outcome"] == "rejected"
    assert rejected["http_status"] == 400
    assert rejected["oauth_error"] == "invalid_grant"
    assert rejected["oauth_error_description"] == "Token is not active"
    assert rejected["presented_refresh_token"]["state"] == "older_than_latest"
    assert rejected["presented_refresh_token"]["presented_before"] == 1
    assert rejected["presented_refresh_token"]["accepted_before"] is True
    assert rejected["last_successful_refresh_age_s"] == 3600
    assert report["counters"] == {
        "credentials_loaded": 2,
        "persist_saved": 1,
        "refresh_rejected": 1,
        "refresh_success": 1,
        "stale_credentials_loaded": 1,
    }
    assert report["current"]["refresh_rejected"] is True
    assert [item["accepted"] for item in report["issued_refresh_tokens"]] == [
        None,
        False,
    ]
    assert "invalid_grant" in caplog.text
    assert "used 1 time(s) before" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text
    await flush_store(recorder.store)
    _assert_no_secrets(report, hass_storage, entry)


async def test_entry_diagnostics_include_history_without_credentials(
    hass, hass_storage
) -> None:
    clock = Clock()
    oauth = ScriptedOAuth(clock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=tokens_to_data(AuthTokens("access-0", "refresh-0", 0, scope=("openid",))),
    )
    entry.add_to_hass(hass)
    recorder = await async_load_auth_diagnostics(hass, entry.entry_id, clock=clock)
    manager, _backoff = entry_auth(hass, entry, oauth)
    await manager.get_access_token()
    await flush_store(recorder.store)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    authentication = diagnostics["authentication"]
    assert diagnostics["diagnostics_schema"] == 28
    assert diagnostics["privacy"]["contains_credentials"] is False
    assert authentication["history_available"] is True
    assert authentication["current"]["has_refresh_token"] is True
    assert authentication["current"]["refresh_token"]["state"] == "latest_known"
    assert authentication["current"]["scope"] == []
    assert authentication["events"][0]["at"].endswith("+00:00")
    _assert_no_secrets(diagnostics, hass_storage, entry)


async def test_entry_diagnostics_without_history(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=tokens_to_data(AuthTokens("access-0", "refresh-0", 0))
    )
    entry.add_to_hass(hass)
    assert (await async_get_config_entry_diagnostics(hass, entry))[
        "authentication"
    ] == {"history_available": False}

    entry_auth(hass, entry, ScriptedOAuth(Clock()))
    authentication = (await async_get_config_entry_diagnostics(hass, entry))[
        "authentication"
    ]
    assert authentication["history_available"] is False
    assert authentication["current"]["has_refresh_token"] is True
    assert authentication["current"]["refresh_rejected"] is False


async def test_skipped_credential_write_is_recorded(hass, caplog) -> None:
    clock = Clock()
    entry = MockConfigEntry(
        domain=DOMAIN, data=tokens_to_data(AuthTokens("access-0", "refresh-0", 0))
    )
    entry.add_to_hass(hass)
    recorder = await async_load_auth_diagnostics(hass, entry.entry_id, clock=clock)
    manager, _backoff = entry_auth(hass, entry, ScriptedOAuth(clock))
    # Another sign-in replaced the credentials while this refresh was running.
    hass.config_entries.async_update_entry(
        entry, data=tokens_to_data(AuthTokens("other", "other-refresh", 0))
    )
    caplog.set_level(logging.WARNING)
    await manager.get_access_token()

    assert tokens_from_data(entry.data).refresh_token == "other-refresh"
    events = recorder.diagnostics(manager)["events"]
    assert events[-1] == {
        **events[-1],
        "event": "persist",
        "outcome": "skipped_entry_credentials_changed",
    }
    assert "was not saved (skipped_entry_credentials_changed)" in caplog.text


async def test_reused_manager_receives_the_loaded_observer(hass) -> None:
    clock = Clock()
    entry = MockConfigEntry(
        domain=DOMAIN, data=tokens_to_data(AuthTokens("access-0", "refresh-0", 0))
    )
    entry.add_to_hass(hass)
    oauth = ScriptedOAuth(clock)
    manager, _backoff = entry_auth(hass, entry, oauth)
    recorder = await async_load_auth_diagnostics(hass, entry.entry_id, clock=clock)
    reused, _backoff = entry_auth(hass, entry, oauth)
    assert reused is manager
    await manager.get_access_token()
    assert [event["event"] for event in recorder.diagnostics(manager)["events"]] == [
        "refresh",
        "persist",
    ]


async def test_transient_failure_is_recorded_without_counting_a_rejection(
    hass,
) -> None:
    class TimingOut:
        async def refresh(self, refresh_token: str) -> AuthTokens:
            raise RequestTimeout("OAuth token request timed out")

    clock = Clock()
    entry = MockConfigEntry(
        domain=DOMAIN, data=tokens_to_data(AuthTokens("access-0", "refresh-0", 0))
    )
    entry.add_to_hass(hass)
    recorder = await async_load_auth_diagnostics(hass, entry.entry_id, clock=clock)
    manager, _backoff = entry_auth(hass, entry, TimingOut())
    with pytest.raises(RequestTimeout):
        await manager.get_access_token()

    report = recorder.diagnostics(manager)
    assert report["events"][-1]["outcome"] == "failed"
    assert report["events"][-1]["error_type"] == "RequestTimeout"
    assert report["counters"]["refresh_failed"] == 1
    assert "refresh_rejected" not in report["counters"]
    assert report["current"]["refresh_token"]["presented_before"] == 1


@pytest.mark.parametrize(
    "stored",
    [
        {"key": "not-hex", "created_at": 1, "events": [], "issued": [], "counters": {}},
        {"key": "0" * 64, "created_at": "now", "events": [], "issued": []},
        ["unexpected"],
    ],
)
async def test_damaged_history_starts_anew(hass, hass_storage, stored) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    hass_storage[_storage_key(entry)] = {
        "version": 1,
        "minor_version": 1,
        "key": _storage_key(entry),
        "data": stored,
    }
    recorder = await async_load_auth_diagnostics(hass, entry.entry_id)
    report = recorder.diagnostics(None)
    assert report["history_reset"] is True
    assert report["events"] == []
    assert "current" not in report


async def test_valid_history_ignores_damaged_items_and_stays_bounded(
    hass, hass_storage
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    hass_storage[_storage_key(entry)] = {
        "version": 1,
        "minor_version": 1,
        "key": _storage_key(entry),
        "data": {
            "key": "a" * 64,
            "created_at": 1_700_000_000,
            "counters": {"refresh_success": 3, "bogus": "x", "flag": True},
            "issued": [
                {"fingerprint": "f", "first_seen_at": 1, "presented": 0},
                "broken",
            ],
            "events": [{"at": 1_700_000_000 + i, "event": "refresh"} for i in range(80)]
            + [{"at": "never"}],
        },
    }
    recorder = await async_load_auth_diagnostics(hass, entry.entry_id)
    report = recorder.diagnostics(None)
    assert report["history_reset"] is False
    assert report["counters"] == {"refresh_success": 3}
    assert report["issued_refresh_tokens"] == []
    assert len(report["events"]) == MAX_EVENTS
    assert report["recorded_since"] == "2023-11-14T22:13:20+00:00"


async def test_removing_the_entry_deletes_its_history(hass, hass_storage) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=tokens_to_data(AuthTokens("access-0", "refresh-0", 0))
    )
    entry.add_to_hass(hass)
    recorder = await async_load_auth_diagnostics(hass, entry.entry_id)
    entry_auth(hass, entry, ScriptedOAuth(Clock()))
    await flush_store(recorder.store)
    assert _storage_key(entry) in hass_storage

    await async_remove_auth_diagnostics(hass, entry.entry_id)
    await hass.async_block_till_done()
    assert auth_diagnostics_for(hass, entry.entry_id) is None
    assert _storage_key(entry) not in hass_storage
    # Removing an unloaded history is also safe.
    await async_remove_auth_diagnostics(hass, entry.entry_id)


async def test_rate_limits_and_server_errors_keep_their_details(hass) -> None:
    from custom_components.bosch_buderus_heating.pointt import (
        RateLimited,
        ServiceUnavailable,
    )
    from custom_components.bosch_buderus_heating.pointt.transport import (
        RateLimitBackoff,
    )

    class Failing:
        def __init__(self) -> None:
            self.errors = [RateLimited(90), ServiceUnavailable(503)]

        async def refresh(self, refresh_token: str) -> AuthTokens:
            raise self.errors.pop(0)

    entry = MockConfigEntry(
        domain=DOMAIN, data=tokens_to_data(AuthTokens("access-0", "refresh-0", 0))
    )
    entry.add_to_hass(hass)
    recorder = await async_load_auth_diagnostics(hass, entry.entry_id, clock=Clock())
    manager, _backoff = entry_auth(hass, entry, Failing())
    with pytest.raises(RateLimited):
        await manager.get_access_token()
    # Leave the shared account pause; this test only checks the record.
    manager._backoff = RateLimitBackoff()
    with pytest.raises(ServiceUnavailable):
        await manager.get_access_token()

    events = recorder.diagnostics(manager)["events"]
    assert events[-2]["outcome"] == "rate_limited"
    assert events[-2]["retry_after_s"] == 90
    assert events[-1]["error_type"] == "ServiceUnavailable"
    assert events[-1]["http_status"] == 503


async def test_missing_refresh_token_rejection_is_recorded(hass, caplog) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=tokens_to_data(AuthTokens("access-0", None, 0))
    )
    entry.add_to_hass(hass)
    recorder = await async_load_auth_diagnostics(hass, entry.entry_id, clock=Clock())
    manager, _backoff = entry_auth(hass, entry, ScriptedOAuth(Clock()))
    caplog.set_level(logging.WARNING)
    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()

    report = recorder.diagnostics(manager)
    assert report["events"][0]["refresh_token"] == {"state": "missing"}
    assert report["events"][-1]["reason"] == "no_refresh_token"
    assert report["current"]["has_refresh_token"] is False
    assert "no refresh token available" in caplog.text


@pytest.mark.parametrize("change", ["removed", "invalid"])
async def test_other_skipped_credential_writes_are_recorded(hass, change) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=tokens_to_data(AuthTokens("access-0", "refresh-0", 0))
    )
    entry.add_to_hass(hass)
    recorder = await async_load_auth_diagnostics(hass, entry.entry_id, clock=Clock())
    manager, _backoff = entry_auth(hass, entry, ScriptedOAuth(Clock()))
    if change == "removed":
        expected = "skipped_entry_removed"
        with patch.object(hass.config_entries, "async_get_entry", return_value=None):
            await manager.get_access_token()
    else:
        hass.config_entries.async_update_entry(entry, data={"unexpected": True})
        expected = "skipped_invalid_entry_data"
        await manager.get_access_token()
    assert recorder.diagnostics(manager)["events"][-1]["outcome"] == expected


async def test_unreadable_history_does_not_block_setup(hass, caplog) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.bosch_buderus_heating.auth_diagnostics.Store.async_load",
        side_effect=OSError("disk"),
    ):
        recorder = await async_load_auth_diagnostics(hass, entry.entry_id)
    assert "could not be read" in caplog.text
    assert recorder.diagnostics(None)["history_reset"] is False
    assert await async_load_auth_diagnostics(hass, entry.entry_id) is recorder


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (None, "unknown"),
        (5, "5 s"),
        (600, "10 min"),
        (10800, "3.0 h"),
        (259200, "3.0 d"),
    ],
)
def test_log_durations_are_readable(seconds, text) -> None:
    from custom_components.bosch_buderus_heating.auth_diagnostics import _duration

    assert _duration(seconds) == text
