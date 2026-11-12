"""Kept local connections and their fallback against loopback HTTPS (G4, O56)."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.bosch_buderus_heating import local_keepalive as keepalive
from custom_components.bosch_buderus_heating import local_transport as transport
from custom_components.bosch_buderus_heating import managed_local
from custom_components.bosch_buderus_heating.local_keepalive import (
    FALLBACK_SECONDS,
    REUSE_FAILURE_WINDOW,
    ConnectionKind,
    ConnectionReuse,
    ReuseFailure,
)
from custom_components.bosch_buderus_heating.local_probe import (
    LocalEndpoint,
    TransportFailure,
)
from custom_components.bosch_buderus_heating.local_read import (
    LocalReadLimits,
    ReadProblem,
    ReadState,
)
from custom_components.bosch_buderus_heating.local_transport import (
    CertificateBinding,
    PortRole,
)
from tests.test_local_probe import CONTEXT
from tests.test_local_read import PATH, REQUEST, client, payload
from tests.test_local_read import clock as clock
from tests.test_local_transport import TOKEN, wire_reply
from tests.test_local_transport import certificates as certificates

pytestmark = pytest.mark.usefixtures("socket_enabled")
KEEP = {"Connection": "keep-alive"}


def answer(writer, status=200):
    body = payload() if status == 200 else b"{}"
    writer.write(wire_reply(body, status=status, headers=KEEP))


async def always(raw, writer, number):
    answer(writer)


@pytest.fixture
async def keepalive_gateway(certificates, monkeypatch):
    """A loopback gateway that answers several requests per connection.

    ``responder(raw, writer, number)`` answers request ``number`` of its
    connection; returning False closes the connection without an answer.
    """
    servers = []
    tasks = set()

    async def start(responder=always, *, certificate=0, port=0):
        state = SimpleNamespace(connections=0, requests=[], writers=[])

        async def handle(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            state.connections += 1
            state.writers.append(writer)
            number = 0
            try:
                while True:
                    raw = await reader.readuntil(b"\r\n\r\n")
                    state.requests.append(raw)
                    number += 1
                    if await responder(raw, writer, number) is False:
                        break
                    await writer.drain()
            except asyncio.IncompleteReadError, ConnectionError, ssl.SSLError:
                pass
            finally:
                writer.close()
                with suppress(OSError, ssl.SSLError):
                    await writer.wait_closed()
                tasks.discard(task)

        server = await asyncio.start_server(
            handle, "127.0.0.1", port, ssl=certificates[certificate][0]
        )
        servers.append(server)
        monkeypatch.setattr(
            LocalEndpoint, "resource_port", server.sockets[0].getsockname()[1]
        )
        monkeypatch.setattr(
            transport, "_resolve", AsyncMock(return_value=("127.0.0.1",))
        )
        binding = CertificateBinding(
            CONTEXT, PortRole.RESOURCES, certificates[certificate][1]
        )
        return server, state, binding

    yield start
    for server in servers:
        server.close()
        server.close_clients()
        await server.wait_closed()
    for task in tuple(tasks):
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def fingerprint_checks(monkeypatch):
    """Every fingerprint check aiohttp runs on a new connection."""
    checks = []
    check = aiohttp.Fingerprint.check

    def counting(self, transport_):
        checks.append(self)
        return check(self, transport_)

    monkeypatch.setattr(aiohttp.Fingerprint, "check", counting)
    return checks


def reuse_client(binding, clock, **kwargs):
    transport_client, budget = client(binding, clock, **kwargs)
    transport_client._reuse = ConnectionReuse(clock=clock)
    return transport_client, budget


async def read_ok(transport_client, clock):
    clock.advance()
    result = await transport_client.read(REQUEST)
    assert result.state is ReadState.SUCCESS, result
    return result


# ------------------------------------------------------- connection reuse


async def test_answered_connection_is_used_again_with_one_pin_check(
    keepalive_gateway, clock, fingerprint_checks
):
    _, state, binding = await keepalive_gateway()
    transport_client, _ = reuse_client(binding, clock)
    async with transport_client:
        for _ in range(4):
            await read_ok(transport_client, clock)
        # One certificate inspection, then one pinned connection for all reads.
        assert state.connections == 2 and len(state.requests) == 4
        assert transport_client.metrics.tls_inspections == 1
        assert fingerprint_checks == [transport_client._pin]
        reuse = transport_client.connection_reuse()
        assert reuse["mode"] == "keep_alive"
        assert reuse["new_connections"] == 1 and reuse["reused_connections"] == 3
        assert reuse["tls_handshakes"] == 2 and reuse["reuse_failures"] == {}
        times = reuse["response_times_by_connection"]
        assert times["new"]["samples"] == 1 and times["reused"]["samples"] == 3
        assert all(f"Bearer {TOKEN}".encode() in raw for raw in state.requests)


async def test_every_new_connection_checks_the_pin_before_sending(
    keepalive_gateway, clock, fingerprint_checks
):
    async def one_per_connection(raw, writer, number):
        writer.write(wire_reply(payload()))  # announces "Connection: close"

    _, state, binding = await keepalive_gateway(one_per_connection)
    transport_client, _ = reuse_client(binding, clock)
    async with transport_client:
        for _ in range(3):
            await read_ok(transport_client, clock)
        reuse = transport_client.connection_reuse()
        assert reuse["new_connections"] == 3 and reuse["reused_connections"] == 0
        assert reuse["closed_by_gateway"] == 3
        assert len(fingerprint_checks) == 3 == len(state.requests)
        assert set(map(id, fingerprint_checks)) == {id(transport_client._pin)}


async def test_error_answer_keeps_its_connection(keepalive_gateway, clock):
    async def not_found_first(raw, writer, number):
        answer(writer, 404 if number == 1 else 200)

    _, state, binding = await keepalive_gateway(not_found_first)
    transport_client, _ = reuse_client(binding, clock)
    async with transport_client:
        clock.advance()
        missing = await transport_client.read(REQUEST)
        assert missing.problem == ReadProblem.NOT_FOUND
        await read_ok(transport_client, clock)
        assert state.connections == 2 and len(state.requests) == 2
        assert transport_client.connection_reuse()["reused_connections"] == 1


async def test_idle_connection_is_not_used_after_the_idle_limit(
    keepalive_gateway, clock, monkeypatch
):
    monkeypatch.setattr(transport, "KEEPALIVE_IDLE_SECONDS", 0.05)
    _, state, binding = await keepalive_gateway()
    transport_client, _ = reuse_client(binding, clock)
    async with transport_client:
        await read_ok(transport_client, clock)
        await asyncio.sleep(0.15)
        await read_ok(transport_client, clock)
        reuse = transport_client.connection_reuse()
        assert reuse["new_connections"] == 2 and reuse["reused_connections"] == 0
        assert state.connections == 3


async def test_certificate_change_is_found_on_the_next_new_connection(
    keepalive_gateway, clock
):
    server, state, binding = await keepalive_gateway()
    port = server.sockets[0].getsockname()[1]
    transport_client, _ = reuse_client(binding, clock)
    async with transport_client:
        await read_ok(transport_client, clock)
        # The gateway restarts with another certificate: kept connections end.
        server.close()
        server.close_clients()
        await server.wait_closed()
        _, replaced, _ = await keepalive_gateway(certificate=1, port=port)
        await asyncio.sleep(0.05)
        clock.advance()
        result = await transport_client.read(REQUEST)
        assert result.state is ReadState.ACCESS_ERROR
        assert result.problem == TransportFailure.TLS_CHANGED
        assert result.http_requests == 0 and not replaced.requests
    assert len(state.requests) == 1


# -------------------------------------------- failures on kept connections


async def test_kept_connection_closed_before_answer_defers_without_pause(
    keepalive_gateway, clock
):
    async def close_second(raw, writer, number):
        if number == 2:
            return False
        answer(writer)

    _, state, binding = await keepalive_gateway(close_second)
    transport_client, budget = reuse_client(binding, clock)
    async with transport_client:
        await read_ok(transport_client, clock)
        clock.advance()
        result = await transport_client.read(REQUEST)
        assert result.state is ReadState.DEFERRED
        assert result.problem == ReadProblem.REUSED_CONNECTION_CLOSED
        assert result.http_requests == 1
        assert budget.pause_seconds == 0 and budget.remaining_seconds <= 0.25
        # The peer stays known: a new connection, no new inspection.
        await read_ok(transport_client, clock)
        assert transport_client.metrics.tls_inspections == 1
        reuse = transport_client.connection_reuse()
        assert reuse["reuse_failures"] == {"closed": 1}
        assert reuse["mode"] == "keep_alive" and reuse["fallbacks"] == 0
        assert transport_client.health()["deferred_reasons"] == {
            "reused_connection_closed": 1
        }
    assert len(state.requests) == 3


async def test_new_connection_closed_before_answer_still_pauses(
    keepalive_gateway, clock
):
    async def close(raw, writer, number):
        return False

    _, _, binding = await keepalive_gateway(close)
    transport_client, budget = reuse_client(binding, clock)
    async with transport_client:
        clock.advance()
        result = await transport_client.read(REQUEST)
        assert result.state is ReadState.ACCESS_ERROR
        assert result.problem == TransportFailure.CONNECT
        assert budget.pause_seconds == 60
        assert transport_client.connection_reuse()["reuse_failures"] == {}


async def test_timeout_on_kept_connection_counts_as_reuse_failure(
    keepalive_gateway, clock
):
    stall = asyncio.Event()

    async def stall_second(raw, writer, number):
        if number == 2:
            await stall.wait()
        answer(writer)

    _, _, binding = await keepalive_gateway(stall_second)
    transport_client, budget = reuse_client(
        binding, clock, limits=LocalReadLimits(timeout=1.0)
    )
    async with transport_client:
        await read_ok(transport_client, clock)
        clock.advance()
        result = await transport_client.read(REQUEST)
        stall.set()
        assert result.state is ReadState.PATH_ERROR
        assert result.problem == TransportFailure.TIMEOUT
        assert budget.pause_seconds == 0
        reuse = transport_client.connection_reuse()
        assert reuse["reuse_failures"] == {"timeout": 1}
        assert reuse["response_times_by_connection"]["reused"]["samples"] == 0


async def test_broken_answer_on_kept_connection_counts_as_reuse_failure(
    keepalive_gateway, clock
):
    async def truncate_second(raw, writer, number):
        if number == 2:
            body = payload()
            reply = wire_reply(body, headers=KEEP)
            writer.write(reply[:-5])
            await writer.drain()
            return False
        answer(writer)

    _, _, binding = await keepalive_gateway(truncate_second)
    transport_client, _ = reuse_client(binding, clock)
    async with transport_client:
        await read_ok(transport_client, clock)
        clock.advance()
        result = await transport_client.read(REQUEST)
        assert result.state is ReadState.PATH_ERROR
        assert result.problem == TransportFailure.INVALID_RESPONSE
        reuse = transport_client.connection_reuse()
        assert reuse["reuse_failures"] == {"invalid_response": 1}


async def test_repeated_failures_switch_to_a_new_connection_per_read(
    keepalive_gateway, clock, caplog
):
    async def close_second(raw, writer, number):
        if number == 2:
            return False
        answer(writer)

    _, state, binding = await keepalive_gateway(close_second)
    transport_client, budget = reuse_client(binding, clock)
    caplog.set_level(logging.DEBUG, logger=keepalive.__name__)
    async with transport_client:
        for _ in range(3):
            await read_ok(transport_client, clock)
            clock.advance()
            failed = await transport_client.read(REQUEST)
            assert failed.problem == ReadProblem.REUSED_CONNECTION_CLOSED
        reuse = transport_client.connection_reuse()
        assert reuse["mode"] == "new_connection_per_read"
        assert reuse["fallback_active"] and reuse["fallbacks"] == 1
        assert reuse["fallback_reason"] == "closed"
        assert reuse["fallback_remaining_seconds"] == FALLBACK_SECONDS[0]
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == keepalive.__name__
        ]
        assert len(warnings) == 1 and "closed" in warnings[0].getMessage()
        # Reads continue, each over its own connection: no more failures.
        connections = state.connections
        for _ in range(4):
            await read_ok(transport_client, clock)
        assert state.connections == connections + 4
        reuse = transport_client.connection_reuse()
        assert reuse["reuse_failures"] == {"closed": 3}
        assert transport_client._fallback_session is not None
        assert budget.pause_seconds == 0
        # After the fallback, kept connections are tried again.
        clock.advance(FALLBACK_SECONDS[0])
        assert transport_client.connection_reuse()["mode"] == "keep_alive"
        for _ in range(3):
            await read_ok(transport_client, clock)
            clock.advance()
            failed = await transport_client.read(REQUEST)
            assert failed.problem == ReadProblem.REUSED_CONNECTION_CLOSED
        reuse = transport_client.connection_reuse()
        assert reuse["fallbacks"] == 2
        assert reuse["fallback_remaining_seconds"] == FALLBACK_SECONDS[1]
        # Logged once as a warning; later fallbacks only at debug level.
        own = [r for r in caplog.records if r.name == keepalive.__name__]
        assert [r.levelno for r in own].count(logging.WARNING) == 1
        assert [r.levelno for r in own].count(logging.DEBUG) >= 2
    assert transport_client._fallback_session.closed
    assert transport_client._session.closed


# ------------------------------------------------------- parallel reads (O56)


async def test_overlapping_reads_are_counted(keepalive_gateway, clock):
    release = asyncio.Event()
    arrived = [asyncio.Event(), asyncio.Event()]

    async def wait_first(raw, writer, number):
        if not release.is_set():
            arrived[len(state.requests) - 1].set()
            await release.wait()
        answer(writer)

    _, state, binding = await keepalive_gateway(wait_first)
    transport_client, _ = reuse_client(binding, clock)
    async with transport_client:
        clock.advance()
        first = asyncio.create_task(transport_client.read(REQUEST))
        await asyncio.wait_for(arrived[0].wait(), 2)
        clock.advance()
        second = asyncio.create_task(transport_client.read(REQUEST))
        await asyncio.wait_for(arrived[1].wait(), 2)
        release.set()
        assert (await first).state is ReadState.SUCCESS
        assert (await second).state is ReadState.SUCCESS
        await read_ok(transport_client, clock)
        reuse = transport_client.connection_reuse()
        assert reuse["overlapping_reads"] == 1
        # Two connections for two parallel reads, then one of them is kept.
        assert reuse["new_connections"] == 2 and reuse["reused_connections"] == 1


# ----------------------------------------------------------- policy alone


def test_failures_spread_beyond_the_window_do_not_switch(clock):
    reuse = ConnectionReuse(clock=clock)
    for _ in range(5):
        reuse.failed(ReuseFailure.TIMEOUT)
        clock.advance(REUSE_FAILURE_WINDOW / 2 + 1)
    assert reuse.keep_alive and reuse.diagnostics(0)["fallbacks"] == 0
    clock.advance(REUSE_FAILURE_WINDOW)
    reuse.failed(ReuseFailure.CLOSED)
    reuse.failed(ReuseFailure.CLOSED)
    assert reuse.keep_alive
    reuse.failed(ReuseFailure.INVALID_RESPONSE)
    assert not reuse.keep_alive
    assert reuse.diagnostics(0)["fallback_reason"] == "invalid_response"


def test_fallback_durations_grow_to_a_day_and_end(clock):
    reuse = ConnectionReuse(clock=clock)
    expected = (*FALLBACK_SECONDS, FALLBACK_SECONDS[-1])
    for duration in expected:
        for _ in range(3):
            reuse.failed(ReuseFailure.CLOSED)
        assert reuse.diagnostics(0)["fallback_remaining_seconds"] == duration
        # Failures of reads that were already running do not extend it.
        clock.advance(1)
        for _ in range(3):
            reuse.failed(ReuseFailure.CLOSED)
        assert reuse.diagnostics(0)["fallback_remaining_seconds"] == duration - 1
        clock.advance(-1)
        clock.advance(duration - 1)
        assert not reuse.keep_alive
        clock.advance(1)
        assert reuse.keep_alive
    assert FALLBACK_SECONDS[-1] == 86400
    assert reuse.diagnostics(0)["fallbacks"] == len(expected)


def test_diagnostics_hold_only_counts_and_fixed_codes(clock):
    reuse = ConnectionReuse(clock=clock)
    assert reuse.diagnostics(0)["tls_handshakes_per_hour"] is None
    reuse.connected(ConnectionKind.NEW)
    reuse.connected(ConnectionKind.REUSED)
    reuse.timed(ConnectionKind.REUSED, 12.0)
    reuse.timed(ConnectionKind.REUSED, 30.0)
    reuse.answered(closes=False)
    reuse.overlapping()
    clock.advance(1800)
    report = reuse.diagnostics(1)
    assert report == {
        "mode": "keep_alive",
        "measured_seconds": 1800,
        "tls_handshakes": 2,
        "tls_handshakes_per_hour": 4.0,
        "new_connections": 1,
        "reused_connections": 1,
        "closed_by_gateway": 0,
        "reuse_failures": {},
        "fallback_active": False,
        "fallback_reason": None,
        "fallback_remaining_seconds": 0,
        "fallbacks": 0,
        "overlapping_reads": 1,
        "response_times_by_connection": {
            "new": {"samples": 0},
            "reused": {
                "samples": 2,
                "median_ms": 12.0,
                "p95_ms": 30.0,
                "maximum_ms": 30.0,
            },
        },
    }
    assert json.loads(json.dumps(report)) == report


async def test_local_diagnostics_report_connection_reuse(keepalive_gateway, clock):
    source = managed_local.LocalSource(SimpleNamespace(closed=False), "local", None)
    assert source.diagnostics()["connection_reuse"] is None
    _, _, binding = await keepalive_gateway()
    transport_client, _ = reuse_client(binding, clock)
    async with transport_client:
        await read_ok(transport_client, clock)
        source.client = transport_client
        report = source.diagnostics()["connection_reuse"]
        assert report["new_connections"] == 1 and report["mode"] == "keep_alive"
        serialized = json.dumps(report)
        assert "127.0.0.1" not in serialized and PATH not in serialized
        assert TOKEN not in serialized
