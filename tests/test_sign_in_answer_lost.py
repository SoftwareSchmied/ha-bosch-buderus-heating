"""X39: a token renewal whose request was not sent or whose answer was lost.

Synthetic tokens and a simulated sign-in service only; no real account.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating.auth_diagnostics import (
    async_load_auth_histories,
    loaded_auth_history,
)
from custom_components.bosch_buderus_heating.auth_lifecycle import (
    ANSWER_LOST_ISSUE_KEY,
    entry_auth,
    forget_entry_auth,
)
from custom_components.bosch_buderus_heating.configuration_migration import (
    legacy_cloud_connection_id,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.pointt import (
    ANSWER_LOST,
    NOT_SENT,
    AuthTokens,
    OAuthClient,
    RefreshTokenRejected,
    RequestTimeout,
    ServiceUnavailable,
    SignInUnavailable,
    TokenEndpointRejection,
    TokenManager,
)
from custom_components.bosch_buderus_heating.pointt import (
    token_manager as token_module,
)
from custom_components.bosch_buderus_heating.pointt.const import (
    DEFAULT_CONNECT_TIMEOUT,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_auth_diagnostics import _restart

pytestmark = pytest.mark.usefixtures("socket_enabled")

INVALID_GRANT = TokenEndpointRejection(
    400, "invalid_grant", "Token is not active", "json"
)
NEW = AuthTokens("access-new", "refresh-new", expires_at=10_000.0)
SECRETS = ("access-old", "refresh-old", "access-new", "refresh-new")


def not_sent() -> Exception:
    return ServiceUnavailable(request_sent=False)


def answer_lost() -> Exception:
    return RequestTimeout("OAuth token request timed out", request_sent=True)


def rejected() -> Exception:
    return RefreshTokenRejected(rejection=INVALID_GRANT)


class ScriptedClient:
    """Answers each try from a script: an exception or new tokens."""

    def __init__(self, *script: Exception | AuthTokens) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, float | None]] = []

    async def refresh(
        self, refresh_token: str, *, answer_timeout: float | None = None
    ) -> AuthTokens:
        self.calls.append((refresh_token, answer_timeout))
        await asyncio.sleep(0)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class Sleeper:
    """Records the waits between tries; a gate holds a wait open."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self.gate is not None:
            await self.gate.wait()


def make_manager(
    client: ScriptedClient,
    *,
    expires_at: float = 0.0,
    sleeper: Sleeper | None = None,
    attempts: list[Any] | None = None,
    persisted: list[AuthTokens] | None = None,
) -> TokenManager:
    return TokenManager(
        client,
        AuthTokens("access-old", "refresh-old", expires_at=expires_at),
        (persisted if persisted is not None else []).append,
        clock=lambda: 1000.0,
        retry_clock=lambda: 0.0,
        sleep=sleeper or Sleeper(),
        observer=attempts.append if attempts is not None else None,
    )


def summary(attempts: list[Any]) -> list[tuple[Any, ...]]:
    return [
        (item.outcome, item.failure_case, item.retry, item.retry_in)
        for item in attempts
    ]


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


# --- case (a): the request never left -------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ServiceUnavailable(request_sent=False),
        RequestTimeout("connect", request_sent=False),
    ],
)
async def test_a_request_that_was_not_sent_is_repeated_soon(error) -> None:
    client = ScriptedClient(error, NEW)
    sleeper = Sleeper()
    attempts: list[Any] = []
    persisted: list[AuthTokens] = []
    manager = make_manager(
        client, sleeper=sleeper, attempts=attempts, persisted=persisted
    )

    assert await manager.get_access_token() == "access-new"
    assert client.calls == [("refresh-old", 90.0), ("refresh-old", 90.0)]
    assert sleeper.delays == [8.0]
    assert summary(attempts) == [
        ("failed", NOT_SENT, 0, 8.0),
        ("success", None, 1, None),
    ]
    # The new token is kept and saved once; no retry wait is left behind.
    assert manager.tokens is NEW and persisted == [NEW]
    assert await manager.get_access_token() == "access-new"
    assert len(client.calls) == 2


async def test_requests_that_were_not_sent_are_repeated_at_most_twice() -> None:
    client = ScriptedClient(not_sent(), not_sent(), not_sent(), NEW)
    sleeper = Sleeper()
    attempts: list[Any] = []
    manager = make_manager(client, sleeper=sleeper, attempts=attempts)

    with pytest.raises(SignInUnavailable) as caught:
        await manager.get_access_token()
    assert caught.value.retry_after == 30.0
    assert len(client.calls) == 3 and sleeper.delays == [8.0, 8.0]
    assert [item.retry_in for item in attempts] == [8.0, 8.0, None]
    # The usual retry wait follows: no further token request meanwhile.
    with pytest.raises(SignInUnavailable):
        await manager.get_access_token()
    assert len(client.calls) == 3


# --- case (b): the request may have arrived, the answer did not ------------


async def test_a_lost_answer_is_tried_once_more_with_the_same_token(caplog) -> None:
    caplog.set_level(logging.WARNING)
    client = ScriptedClient(answer_lost(), NEW)
    sleeper = Sleeper()
    attempts: list[Any] = []
    persisted: list[AuthTokens] = []
    manager = make_manager(
        client, sleeper=sleeper, attempts=attempts, persisted=persisted
    )

    assert await manager.get_access_token() == "access-new"
    assert [token for token, _ in client.calls] == ["refresh-old", "refresh-old"]
    assert sleeper.delays == [12.0]
    assert summary(attempts) == [
        ("failed", ANSWER_LOST, 0, 12.0),
        ("success", None, 1, None),
    ]
    assert persisted == [NEW] and manager.rejected_after_lost_answer is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "did not answer the token renewal started at 1970-01-01" in message
    assert "access token valid for 0 s more" in message
    assert "One more try with the same token follows in 12 s" in message
    for secret in SECRETS:
        assert secret not in caplog.text


async def test_a_lost_answer_is_tried_only_once(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=token_module.__name__)
    client = ScriptedClient(answer_lost(), answer_lost(), NEW)
    sleeper = Sleeper()
    manager = make_manager(client, sleeper=sleeper)

    with pytest.raises(SignInUnavailable):
        await manager.get_access_token()
    assert len(client.calls) == 2 and sleeper.delays == [12.0]
    lost = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if "did not answer" in record.getMessage()
    ]
    # One warning per refresh token; the repeat with the same token is debug.
    assert [level for level, _ in lost] == [logging.WARNING, logging.DEBUG]
    assert "follows in 12 s" in lost[0][1]
    assert "The next try follows after the usual wait" in lost[1][1]


async def test_invalid_grant_after_a_lost_answer_is_marked() -> None:
    client = ScriptedClient(answer_lost(), rejected())
    attempts: list[Any] = []
    manager = make_manager(client, attempts=attempts)

    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()
    assert [token for token, _ in client.calls] == ["refresh-old", "refresh-old"]
    assert manager.refresh_rejected and manager.rejected_after_lost_answer
    assert attempts[-1].outcome == "rejected" and attempts[-1].after_lost_answer
    # A rejected sign-in is never presented again by this manager.
    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()
    assert len(client.calls) == 2


@pytest.mark.parametrize("before", [[], [not_sent()]])
async def test_invalid_grant_without_a_lost_answer_is_not_marked(before) -> None:
    client = ScriptedClient(*before, rejected())
    attempts: list[Any] = []
    manager = make_manager(client, attempts=attempts)

    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()
    assert manager.refresh_rejected and not manager.rejected_after_lost_answer
    assert not attempts[-1].after_lost_answer


async def test_a_lost_answer_of_an_older_token_is_not_held_against_a_new_one() -> None:
    client = ScriptedClient(answer_lost(), NEW, rejected())
    manager = make_manager(client)

    assert await manager.get_access_token() == "access-new"
    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token(force_refresh=True, rejected_token="access-new")
    assert client.calls[-1][0] == "refresh-new"
    assert not manager.rejected_after_lost_answer


async def test_no_quick_retry_during_a_rate_limit_pause() -> None:
    class PausingClient(ScriptedClient):
        async def refresh(
            self, refresh_token: str, *, answer_timeout: float | None = None
        ) -> AuthTokens:
            manager._backoff.activate(60.0)
            return await super().refresh(refresh_token, answer_timeout=answer_timeout)

    client = PausingClient(answer_lost(), NEW)
    sleeper = Sleeper()
    manager = make_manager(client, sleeper=sleeper)
    with pytest.raises(SignInUnavailable):
        await manager.get_access_token()
    assert len(client.calls) == 1 and sleeper.delays == []


# --- the wait for the answer -------------------------------------------------


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [(599.0, 300.0), (541.0, 300.0), (400.0, 280.0), (250.0, 130.0), (150.0, 90.0)],
)
async def test_an_early_renewal_waits_longer_for_its_answer(
    remaining: float, expected: float
) -> None:
    client = ScriptedClient(NEW)
    manager = make_manager(client, expires_at=1000.0 + remaining)
    # The early renewal does not hold the request back.
    assert await manager.get_access_token() == "access-old"
    await manager.async_wait_for_refresh()
    assert client.calls == [("refresh-old", expected)]
    assert manager.tokens is NEW


@pytest.mark.parametrize(
    ("remaining", "force"), [(-5.0, False), (20.0, False), (3000.0, True)]
)
async def test_a_renewal_that_requests_wait_for_keeps_ninety_seconds(
    remaining: float, force: bool
) -> None:
    client = ScriptedClient(NEW)
    manager = make_manager(client, expires_at=1000.0 + remaining)
    if force:
        await manager.get_access_token(force_refresh=True, rejected_token="access-old")
    else:
        await manager.get_access_token()
    assert client.calls == [("refresh-old", 90.0)]


async def test_the_retry_after_a_lost_answer_waits_by_the_new_remaining_time() -> None:
    clock = [1000.0]
    client = ScriptedClient(answer_lost(), NEW)

    async def sleep(delay: float) -> None:
        clock[0] += 300.0 + delay

    manager = TokenManager(
        client,
        AuthTokens("access-old", "refresh-old", expires_at=1541.0),
        lambda tokens: None,
        clock=lambda: clock[0],
        sleep=sleep,
    )
    assert await manager.get_access_token() == "access-old"
    await manager.async_wait_for_refresh()
    # 541 s left: 5 min; after them and the pause 229 s left: 109 s.
    assert [timeout for _, timeout in client.calls] == [300.0, 109.0]


# --- one renewal at a time ---------------------------------------------------


async def test_retries_stay_inside_the_one_renewal_of_an_early_refresh() -> None:
    client = ScriptedClient(answer_lost(), NEW)
    sleeper = Sleeper()
    sleeper.gate = asyncio.Event()
    persisted: list[AuthTokens] = []
    manager = make_manager(
        client, expires_at=1500.0, sleeper=sleeper, persisted=persisted
    )

    assert await manager.get_access_token() == "access-old"
    await settle()
    assert sleeper.delays == [12.0] and manager.refresh_in_progress
    # While the renewal waits for its retry, requests go on with the current
    # token and no second token request starts.
    for _ in range(5):
        assert await manager.get_access_token() == "access-old"
    assert len(client.calls) == 1
    sleeper.gate.set()
    await manager.async_wait_for_refresh()
    assert len(client.calls) == 2 and persisted == [NEW]
    assert await manager.get_access_token() == "access-new"


async def test_waiting_requests_share_the_renewal_and_its_retry() -> None:
    client = ScriptedClient(not_sent(), answer_lost(), NEW)
    sleeper = Sleeper()
    sleeper.gate = asyncio.Event()
    persisted: list[AuthTokens] = []
    manager = make_manager(client, sleeper=sleeper, persisted=persisted)

    waiters = [asyncio.create_task(manager.get_access_token()) for _ in range(5)]
    await settle()
    assert len(client.calls) == 1 and not any(task.done() for task in waiters)
    sleeper.gate.set()
    assert await asyncio.gather(*waiters) == ["access-new"] * 5
    assert len(client.calls) == 3 and sleeper.delays == [8.0, 12.0]
    assert persisted == [NEW]


# --- the OAuth client --------------------------------------------------------


async def _serve(handler) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/token", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}/token"


async def test_the_answer_timeout_bounds_only_the_answer() -> None:
    posts = 0

    async def handler(_request: web.Request) -> web.Response:
        nonlocal posts
        posts += 1
        await asyncio.sleep(1.0)
        return web.json_response(
            {"access_token": "a", "refresh_token": "r2", "expires_in": 3600}
        )

    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            client = OAuthClient(session, token_url=url)
            seen: list[aiohttp.ClientTimeout] = []
            post = session.post

            def spy(*args: Any, **kwargs: Any) -> Any:
                seen.append(kwargs["timeout"])
                return post(*args, **kwargs)

            with patch.object(session, "post", spy):
                with pytest.raises(RequestTimeout) as caught:
                    await client.refresh("old", answer_timeout=0.3)
                assert caught.value.request_sent is True
                tokens = await client.refresh("old", answer_timeout=3.0)
                await client.refresh("old")
    finally:
        await runner.cleanup()
    assert tokens.refresh_token == "r2"
    assert posts == 3
    assert [(t.connect, t.total) for t in seen] == [
        (DEFAULT_CONNECT_TIMEOUT, 0.3),
        (DEFAULT_CONNECT_TIMEOUT, 3.0),
        (DEFAULT_CONNECT_TIMEOUT, 90.0),
    ]


async def test_a_refused_connection_was_not_sent() -> None:
    async with aiohttp.ClientSession() as session:
        client = OAuthClient(session, token_url="http://127.0.0.1:9/token")
        with pytest.raises(ServiceUnavailable) as caught:
            await asyncio.wait_for(client.refresh("old"), 10)
    assert caught.value.request_sent is False


async def test_a_connect_timeout_was_not_sent() -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise aiohttp.ConnectionTimeoutError("connect timed out")

    async with aiohttp.ClientSession() as session:
        client = OAuthClient(session, token_url="http://127.0.0.1:9/token")
        with (
            patch.object(session, "post", fail),
            pytest.raises(RequestTimeout) as caught,
        ):
            await client.refresh("old")
    assert caught.value.request_sent is False


async def test_a_connection_dropped_after_the_request_lost_the_answer() -> None:
    received = asyncio.Event()

    async def drop(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        received.set()
        writer.close()

    server = await asyncio.start_server(drop, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as session:
            client = OAuthClient(session, token_url=f"http://127.0.0.1:{port}/token")
            with pytest.raises(ServiceUnavailable) as caught:
                await client.refresh("old")
    finally:
        server.close()
        await server.wait_closed()
    assert received.is_set()
    assert caught.value.request_sent is True and caught.value.status is None


# --- Home Assistant: diagnostics, log and repair note -----------------------


@pytest.fixture
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(token_module, "ANSWER_LOST_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(token_module, "NOT_SENT_RETRY_DELAY_SECONDS", 0.0)


def _issue_id(entry: MockConfigEntry) -> str:
    return f"sign_in_answer_lost_{entry.entry_id}"


async def _history(hass, entry):
    await async_load_auth_histories(hass, entry)
    return loaded_auth_history(
        hass, entry.entry_id, legacy_cloud_connection_id(entry.entry_id)
    )


def _cloud_entry(hass, tokens: AuthTokens) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=tokens_to_data(tokens))
    entry.add_to_hass(hass)
    return entry


@pytest.mark.usefixtures("no_retry_pause")
async def test_lost_answer_then_rejection_raises_the_note_until_a_new_sign_in(
    hass, caplog
) -> None:
    caplog.set_level(logging.WARNING)
    registry = ir.async_get(hass)
    entry = _cloud_entry(hass, AuthTokens("access-old", "refresh-old", 0))
    recorder = await _history(hass, entry)
    manager, _ = entry_auth(hass, entry, ScriptedClient(answer_lost(), rejected()))

    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()

    issue = registry.async_get_issue(DOMAIN, _issue_id(entry))
    assert issue is not None
    assert issue.translation_key == ANSWER_LOST_ISSUE_KEY == "sign_in_answer_lost"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert not issue.is_fixable and not issue.is_persistent
    assert issue.data == {"entry_id": entry.entry_id}

    report = recorder.diagnostics(manager)
    failed, rejection = report["events"][-2:]
    assert failed["outcome"] == "failed" and failed["failure_case"] == "answer_lost"
    assert failed["answer_timeout_s"] == 90 and failed["retry_in_s"] == 0
    assert rejection["outcome"] == "rejected" and rejection["retry"] == 1
    assert rejection["after_lost_answer"] is True
    assert rejection["failed_before_rejection"][0]["failure_case"] == "answer_lost"
    assert report["counters"]["refresh_answer_lost"] == 1
    assert report["counters"]["refresh_rejected_after_lost_answer"] == 1
    assert report["current"]["rejected_after_lost_answer"] is True
    assert "did not answer the token renewal" in caplog.text
    assert "SingleKey ID most likely replaced the token" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text
        assert secret not in repr(report)

    # After a restart the note is gone (not persistent) and comes back when
    # the stored token is rejected again: the history remembers the loss.
    _restart(hass)
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
    await _history(hass, entry)
    client = ScriptedClient(rejected())
    manager, _ = entry_auth(hass, entry, client)
    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()
    assert registry.async_get_issue(DOMAIN, _issue_id(entry)) is not None

    # The new sign-in replaces the credentials and ends the note.
    hass.config_entries.async_update_entry(
        entry, data=tokens_to_data(AuthTokens("access-2", "refresh-2", 10**12))
    )
    entry_auth(hass, entry, ScriptedClient())
    assert registry.async_get_issue(DOMAIN, _issue_id(entry)) is None


@pytest.mark.usefixtures("no_retry_pause")
@pytest.mark.parametrize("script", [[rejected()], [not_sent(), rejected()]])
async def test_a_rejection_without_a_lost_answer_raises_no_note(hass, script) -> None:
    entry = _cloud_entry(hass, AuthTokens("access-old", "refresh-old", 0))
    await _history(hass, entry)
    manager, _ = entry_auth(hass, entry, ScriptedClient(*script))
    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()
    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry)) is None


@pytest.mark.usefixtures("no_retry_pause")
async def test_a_successful_renewal_ends_the_note(hass) -> None:
    entry = _cloud_entry(hass, AuthTokens("access-old", "refresh-old", 0))
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry),
        data={"entry_id": entry.entry_id},
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ANSWER_LOST_ISSUE_KEY,
    )
    manager, _ = entry_auth(hass, entry, ScriptedClient(answer_lost(), NEW))
    assert await manager.get_access_token() == "access-new"
    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry)) is None


@pytest.mark.usefixtures("no_retry_pause")
async def test_removing_the_entry_ends_the_note(hass) -> None:
    entry = _cloud_entry(hass, AuthTokens("access-old", "refresh-old", 0))
    manager, _ = entry_auth(hass, entry, ScriptedClient(answer_lost(), rejected()))
    with pytest.raises(RefreshTokenRejected):
        await manager.get_access_token()
    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry)) is not None
    forget_entry_auth(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry)) is None


@pytest.mark.usefixtures("no_retry_pause", "enable_custom_integrations")
async def test_a_running_entry_asks_for_sign_in_and_explains_the_lost_answer(
    hass,
) -> None:
    """The whole chain: setup, lost answer, rejection, reauth and note."""
    from unittest.mock import AsyncMock

    from custom_components.bosch_buderus_heating.pointt import Brand, Gateway
    from tests.test_auth_recovery import FLOW_MODULE
    from tests.test_config_flow import _redirect_from_result
    from tests.test_configuration import legacy
    from tests.test_entry_dispatch import loopback
    from tests.test_managed_pointt import Backend, eventually
    from tests.test_storage_migration import entry_for

    entry = entry_for(hass, data=legacy(["000123"], []))
    presented: list[str] = []

    async def refresh(
        _self: OAuthClient, refresh_token: str, *, answer_timeout: float | None = None
    ) -> AuthTokens:
        presented.append(refresh_token)
        if len(presented) == 1:
            raise answer_lost()
        raise rejected()

    registry = ir.async_get(hass)

    def notes() -> list[ir.IssueEntry]:
        return [
            issue
            for (domain, _), issue in registry.issues.items()
            if domain == DOMAIN and issue.translation_key == ANSWER_LOST_ISSUE_KEY
        ]

    with patch.object(OAuthClient, "refresh", refresh):
        async with loopback(Backend()):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await eventually(
                lambda: bool(list(entry.async_get_active_flows(hass, {SOURCE_REAUTH})))
            )
            await eventually(lambda: bool(notes()))
            flows = list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
            assert len(flows) == 1
            assert presented == ["synthetic-refresh", "synthetic-refresh"]
            # Home Assistant's own sign-in request and the explanation.
            assert registry.async_get_issue(
                "homeassistant", f"config_entry_reauth_{DOMAIN}_{entry.entry_id}"
            )
            (note,) = notes()
            assert note.data is not None
            assert note.data["entry_id"] == entry.entry_id
            assert note.data["connection_id"] == flows[0]["context"]["connection_id"]

            # The new sign-in in the reauthentication dialog ends the note.
            result = await hass.config_entries.flow.async_configure(
                flows[0]["flow_id"], {}
            )
            renewed = AuthTokens("access-2", "refresh-2", 10**12, scope=("bacon",))
            with (
                patch(
                    f"{FLOW_MODULE}.OAuthClient.exchange_code",
                    AsyncMock(return_value=renewed),
                ),
                patch(
                    "custom_components.bosch_buderus_heating.configuration_management"
                    ".PointTClient.get_gateways",
                    AsyncMock(return_value=(Gateway("000123"),)),
                ),
            ):
                result = await hass.config_entries.flow.async_configure(
                    result["flow_id"],
                    {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
                )
            assert result["reason"] == "reauth_successful"
            assert not notes()
            await hass.async_block_till_done()
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_finished_reauthentication_ends_the_note(
    hass, enable_custom_integrations
) -> None:
    from unittest.mock import AsyncMock, Mock

    from homeassistant import config_entries
    from homeassistant.data_entry_flow import FlowResultType

    from custom_components.bosch_buderus_heating.const import (
        CONF_BRAND,
        CONF_GATEWAY_IDS,
    )
    from custom_components.bosch_buderus_heating.pointt import Brand, Gateway
    from tests.test_auth_recovery import AUTH_MODULE, FLOW_MODULE, _redirect

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="buderus:existing",
        data={
            CONF_BRAND: Brand.BUDERUS.value,
            CONF_GATEWAY_IDS: ["gateway-one"],
            **tokens_to_data(AuthTokens("access-old", "refresh-old", 0)),
        },
    )
    entry.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry),
        data={"entry_id": entry.entry_id},
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ANSWER_LOST_ISSUE_KEY,
    )
    clock = Mock(return_value=1000.0)
    renewed = AuthTokens("access-new", "refresh-new", 9000.0)
    with (
        patch(f"{AUTH_MODULE}.monotonic", clock),
        patch(f"{FLOW_MODULE}.monotonic", clock, create=True),
        patch(
            f"{FLOW_MODULE}.OAuthClient.exchange_code", AsyncMock(return_value=renewed)
        ),
        patch(
            f"{FLOW_MODULE}.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("gateway-one"),)),
        ),
        patch.object(hass.config_entries, "async_reload", AsyncMock()),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=dict(entry.data),
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        # Not yet: the note stays while the sign-in is still open.
        assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry))
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _redirect(result)}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry)) is None
