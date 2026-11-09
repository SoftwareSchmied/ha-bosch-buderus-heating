"""Shared setup admission and durable failures over synthetic TLS only."""

import asyncio
from dataclasses import replace
from itertools import pairwise
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.bosch_buderus_heating import local_transport as transport
from custom_components.bosch_buderus_heating.local_auth import (
    AuthProblem,
    AuthState,
    request_local_token,
)
from custom_components.bosch_buderus_heating.local_budget import (
    MINIMUM_START_GAP,
    BudgetDeferred,
    LocalBudgetManager,
)
from custom_components.bosch_buderus_heating.local_probe import (
    LocalEndpoint,
    ProbePath,
    ProbeStatus,
    TransportFailure,
    assess_local_probe,
)
from custom_components.bosch_buderus_heating.local_transport import (
    LocalTransportError,
    PortRole,
    StaleLocalRequest,
    inspect_local_certificate,
    read_local_probe,
)
from tests.test_local_auth import LABEL
from tests.test_local_auth import auth_gateway as auth_gateway
from tests.test_local_probe import CONTEXT, response
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import gateways as gateways
from tests.test_local_transport import wire_reply

pytestmark = pytest.mark.usefixtures("socket_enabled")


@pytest.fixture
async def budget(hass, tmp_path):
    manager = LocalBudgetManager(hass)
    manager._path = tmp_path / "budget.json"
    result = await manager.budget(CONTEXT)
    yield result
    await manager.close()


async def probe(binding, budget, *, active=lambda: True):
    return await read_local_probe(
        CONTEXT, "synthetic-token", binding, is_current=active, budget=budget
    )


async def test_setup_probe_waits_for_reads_and_paces_each_actual_request(
    budget, gateways, monkeypatch
):
    starts = []

    async def reply(raw, writer):
        starts.append(asyncio.get_running_loop().time())
        path = ProbePath(raw.split(b" ")[1].decode())
        writer.write(wire_reply(response(path).body))
        await writer.drain()

    _, requests, _, binding = await gateways(reply)
    inspect = AsyncMock(wraps=transport._inspect)
    monkeypatch.setattr(transport, "_inspect", inspect)
    first = budget.claim()
    await asyncio.sleep(0.26)
    second = budget.claim()
    task = asyncio.create_task(probe(binding, budget))
    await asyncio.sleep(0.02)
    assert first and second and not inspect.called and not requests
    budget.release(first)
    await asyncio.sleep(0.02)
    assert not inspect.called
    budget.release(second)
    exchange = await task
    assert (
        assess_local_probe(exchange.responses, current_context=CONTEXT).status
        == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    )
    assert len(requests) == len(starts) == inspect.await_count == 3
    assert all(b - a >= MINIMUM_START_GAP - 0.02 for a, b in pairwise(starts))
    assert not budget.busy


@pytest.mark.parametrize("status", [429, 503, 500])
@pytest.mark.parametrize("stale", [False, True])
async def test_setup_response_pause_is_saved_before_generation_rejection(
    budget, gateways, status, stale
):
    current = True

    async def reply(raw, writer):
        nonlocal current
        current = not stale
        writer.write(wire_reply(status=status, headers={"Retry-After": "7200"}))
        await writer.drain()

    _, requests, _, binding = await gateways(reply)
    if stale:
        with pytest.raises(StaleLocalRequest):
            await probe(binding, budget, active=lambda: current)
    else:
        result = await probe(binding, budget)
        assert len(result.responses) == 1 and result.responses[0].status == status
    expected = 60 if status == 500 else 7200
    assert expected - 2 < budget.pause_seconds <= expected
    assert len(requests) == 1
    restored = LocalBudgetManager(budget._manager._hass)
    restored._path = budget._manager._path
    try:
        handle = await restored.budget(CONTEXT)
        assert expected - 2 < handle.pause_seconds <= expected
        with pytest.raises(BudgetDeferred):
            await probe(binding, handle)
        assert len(requests) == 1
    finally:
        await restored.close()


async def test_paused_certificate_check_and_auth_do_not_resolve_or_send(
    budget, auth_gateway, monkeypatch
):
    calls, _, binding = await auth_gateway()
    await budget.pause(7200, "rate_limited")
    resolver = AsyncMock(side_effect=AssertionError("No DNS during a pause"))
    monkeypatch.setattr(transport, "_resolve", resolver)
    with pytest.raises(BudgetDeferred):
        await inspect_local_certificate(
            CONTEXT, PortRole.AUTHENTICATION, is_current=lambda: True, budget=budget
        )
    persist = AsyncMock()
    outcome = await request_local_token(
        CONTEXT,
        LABEL,
        binding,
        is_current=lambda: True,
        on_response=persist,
        budget=budget,
    )
    assert outcome.state == AuthState.NOT_SENT
    assert outcome.problem == AuthProblem.BUDGET_BUSY
    persist.assert_awaited_once_with(outcome)
    assert not calls and not resolver.called


async def test_auth_and_certificate_inspection_use_shared_admission(
    budget, auth_gateway
):
    calls, _, binding = await auth_gateway()
    observed = await inspect_local_certificate(
        CONTEXT, PortRole.AUTHENTICATION, is_current=lambda: True, budget=budget
    )
    assert observed.sha256 == binding.sha256 and not calls
    outcome = await request_local_token(
        CONTEXT, LABEL, binding, is_current=lambda: True, budget=budget
    )
    assert outcome.state == AuthState.RECEIVED and len(calls) == 1
    assert not budget.busy


async def test_auth_rate_limit_blocks_resource_probe_on_other_port(
    budget, auth_gateway, gateways
):
    async def reply(head, body, writer):
        writer.write(wire_reply(status=429, headers={"Retry-After": "7200"}))
        await writer.drain()

    calls, _, authentication = await auth_gateway(reply)
    _, reads, _, resources = await gateways()
    outcome = await request_local_token(
        CONTEXT, LABEL, authentication, is_current=lambda: True, budget=budget
    )
    assert outcome.problem == AuthProblem.RATE_LIMITED
    assert 7198 < budget.pause_seconds <= 7200
    with pytest.raises(BudgetDeferred):
        await probe(resources, budget)
    assert len(calls) == 1 and not reads


async def test_resolved_alias_inherits_pause_before_tls_connection(
    budget, gateways, monkeypatch
):
    _, requests, _, binding = await gateways()
    alias_context = replace(
        CONTEXT,
        endpoint=LocalEndpoint("192.0.2.88"),
        identity=replace(CONTEXT.identity, connection_id="alias"),
    )
    alias = await budget._manager.budget(alias_context)
    await alias.bind_peers(("127.0.0.1",))
    await alias.pause(7200)
    connection = AsyncMock(side_effect=AssertionError("No TLS during a pause"))
    monkeypatch.setattr(asyncio, "open_connection", connection)
    with pytest.raises(BudgetDeferred):
        await probe(binding, budget)
    assert not connection.called and not requests
    assert budget.pause_seconds > 7198 and not budget.busy


@pytest.mark.parametrize(
    "error,failure,paused",
    [
        (LocalTransportError(TransportFailure.DNS), TransportFailure.DNS, True),
        (
            LocalTransportError(TransportFailure.TLS_CHANGED),
            TransportFailure.TLS_CHANGED,
            False,
        ),
        (
            aiohttp.ServerFingerprintMismatch(b"a", b"b", "test", 443),
            TransportFailure.TLS_CHANGED,
            False,
        ),
        (TimeoutError(), TransportFailure.TIMEOUT, True),
        (OSError(), TransportFailure.CONNECT, True),
    ],
)
async def test_managed_probe_failure_releases_slot_and_preserves_pause(
    budget, gateways, monkeypatch, error, failure, paused
):
    _, requests, _, binding = await gateways()
    monkeypatch.setattr(transport, "_inspect", AsyncMock(side_effect=error))
    result = await probe(binding, budget)
    assert result.responses[0].failure == failure
    assert not requests and not budget.busy
    assert (budget.pause_seconds > 0) == paused


async def test_cancelled_managed_probe_releases_slot_without_background_retries(
    budget, gateways
):
    async def reply(raw, writer):
        await asyncio.Event().wait()

    _, requests, received, binding = await gateways(reply)
    task = asyncio.create_task(probe(binding, budget))
    await received.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(requests) == 1 and not budget.busy and budget.pause_seconds == 0
