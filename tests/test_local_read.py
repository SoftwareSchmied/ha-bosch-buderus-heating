"""General local reads against real loopback HTTPS and synthetic credentials."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.bosch_buderus_heating import local_read as reader
from custom_components.bosch_buderus_heating import local_transport as transport
from custom_components.bosch_buderus_heating.local_api.catalog import (
    make_request,
    request_for_path,
)
from custom_components.bosch_buderus_heating.local_api.errors import LocalResourceError
from custom_components.bosch_buderus_heating.local_api.json import JsonLimits
from custom_components.bosch_buderus_heating.local_probe import (
    LocalEndpoint,
    TransportFailure,
)
from custom_components.bosch_buderus_heating.local_read import (
    DEFAULT_READ_LIMITS,
    LocalReadBudget,
    LocalReadClient,
    LocalReadLimits,
    ReadProblem,
    ReadState,
)
from custom_components.bosch_buderus_heating.local_transport import (
    CertificateBinding,
    LocalTransportError,
    PortRole,
    StaleLocalRequest,
)
from tests.test_local_probe import CONTEXT
from tests.test_local_transport import TOKEN, wire_reply
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import gateways as gateways

pytestmark = pytest.mark.usefixtures("socket_enabled")
PATH = "/heatingCircuits/hc2/currentRoomSetpoint"
REQUEST = request_for_path(PATH)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds=1):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


def payload(path=PATH, **changes):
    result = {
        "id": path,
        "type": "floatValue",
        "writeable": 1,
        "value": 21.25,
        "unitOfMeasure": "C",
    }
    result.update(changes)
    return json.dumps(result).encode()


async def success(raw, writer):
    writer.write(wire_reply(payload()))
    await writer.drain()


def client(
    binding,
    clock,
    *,
    budget=None,
    token=TOKEN,
    is_current=lambda: True,
    limits=DEFAULT_READ_LIMITS,
):
    budget = budget or LocalReadBudget(binding.context, clock=clock)
    return LocalReadClient(
        binding.context,
        token,
        binding,
        budget=budget,
        is_current=is_current,
        limits=limits,
    ), budget


async def test_one_correct_heating_circuit_get_has_separate_http_and_tls_counts(
    gateways, clock, monkeypatch
):
    _, seen, _, binding = await gateways(success)
    monkeypatch.setattr(LocalEndpoint, "auth_port", 1)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert result.state is ReadState.SUCCESS and not result.stop_access
        assert result.status == 200 and result.http_requests == 1
        assert result.response_bytes == len(payload())
        assert result.observation.context == CONTEXT.identity
        assert result.observation.request.path == PATH
        assert result.observation.data.measurement == Decimal("21.25")
        assert (
            result.observation.reported_writeable and not result.observation.can_write
        )
        assert result.observation.observed_at.tzinfo is UTC
        assert (
            transport_client.metrics.tls_inspections
            == transport_client.metrics.http_requests
            == transport_client.metrics.http_responses
            == transport_client.metrics.successes
            == 1
        )
        assert len(seen) == 1 and seen[0].startswith(
            b"GET " + PATH.encode() + b" HTTP/1.1"
        )
        assert f"Authorization: Bearer {TOKEN}".encode() in seen[0]
        assert "hc1" not in seen[0].decode()
        assert TOKEN not in repr(transport_client) + repr(result) + repr(budget)
    assert transport_client._session.closed and transport_client._token == ""


async def test_pacing_has_no_waiting_queue_and_counts_actual_header_time(
    gateways, clock, monkeypatch
):
    _, seen, _, binding = await gateways(success)
    resolve = transport._resolve

    async def slow_connection(*args, **kwargs):
        # Time spent before the request starts (address lookup, handshake).
        result = await resolve(*args, **kwargs)
        clock.advance(0.125)
        return result

    monkeypatch.setattr(transport, "_resolve", slow_connection)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        clock.advance(0.0625)
        result = await transport_client.read(REQUEST)
        assert result.state is ReadState.DEFERRED and result.http_requests == 0
        assert result.stop_access and result.problem == ReadProblem.PAUSED
        assert len(seen) == 1 and transport_client.metrics.tls_inspections == 1
        clock.advance(0.0625)
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        assert len(seen) == 2 and budget.remaining_seconds == 0.25


async def test_real_clock_enforces_quarter_second_between_http_starts(gateways):
    times = []

    async def reply(raw, writer):
        times.append(asyncio.get_running_loop().time())
        await success(raw, writer)

    _, seen, _, binding = await gateways(reply)
    async with LocalReadClient(
        CONTEXT,
        TOKEN,
        binding,
        budget=LocalReadBudget(CONTEXT),
        is_current=lambda: True,
    ) as transport_client:
        await transport_client.read(REQUEST)
        assert (await transport_client.read(REQUEST)).state is ReadState.DEFERRED
        await asyncio.sleep(0.26)
        await transport_client.read(REQUEST)
    assert len(seen) == 2 and times[1] - times[0] >= 0.25


async def test_three_clients_share_two_inflight_requests_without_queuing(
    gateways, clock
):
    release = asyncio.Event()
    received_two = asyncio.Event()
    count = 0

    async def reply(raw, writer):
        nonlocal count
        count += 1
        if count == 2:
            received_two.set()
        await release.wait()
        await success(raw, writer)

    _, seen, received, binding = await gateways(reply)
    first, budget = client(binding, clock)
    second, _ = client(binding, clock, budget=budget)
    third, _ = client(binding, clock, budget=budget)
    async with first, second, third:
        one = asyncio.create_task(first.read(REQUEST))
        await asyncio.wait_for(received.wait(), 2)
        clock.advance(0.25)
        two = asyncio.create_task(second.read(REQUEST))
        await asyncio.wait_for(received_two.wait(), 2)
        clock.advance(0.25)
        result = await third.read(REQUEST)
        assert result.problem == ReadProblem.BUSY and result.http_requests == 0
        assert len(seen) == 2 and third.metrics.tls_inspections == 0
        release.set()
        assert (await one).state is ReadState.SUCCESS
        assert (await two).state is ReadState.SUCCESS
        assert not budget.busy


@pytest.mark.parametrize(
    "status,state,problem",
    [
        (400, ReadState.PATH_ERROR, ReadProblem.HTTP_ERROR),
        (301, ReadState.PATH_ERROR, TransportFailure.REDIRECT),
        (307, ReadState.PATH_ERROR, TransportFailure.REDIRECT),
        (401, ReadState.ACCESS_ERROR, ReadProblem.AUTH_REQUIRED),
        (403, ReadState.PATH_ERROR, ReadProblem.FORBIDDEN),
        (404, ReadState.PATH_ERROR, ReadProblem.NOT_FOUND),
        (406, ReadState.PATH_ERROR, ReadProblem.NOT_ACCEPTABLE),
        (429, ReadState.ACCESS_ERROR, ReadProblem.RATE_LIMITED),
        (500, ReadState.ACCESS_ERROR, ReadProblem.SERVICE_UNAVAILABLE),
        (503, ReadState.ACCESS_ERROR, ReadProblem.SERVICE_UNAVAILABLE),
    ],
)
async def test_status_categories_do_not_return_error_bodies_or_follow_redirects(
    gateways, clock, status, state, problem
):
    async def reply(raw, writer):
        writer.write(
            wire_reply(
                b"private error body",
                status=status,
                headers={
                    "Content-Type": "text/html",
                    "Location": "https://127.0.0.1:1/other",
                    "Retry-After": "7200",
                },
            )
        )
        await writer.drain()

    _, seen, _, binding = await gateways(reply)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert result.state is state and result.problem == problem
        assert result.observation is None and result.http_requests == 1
        assert result.status == status and len(seen) == 1
        assert "private" not in repr(result)
        if status in {429, 503}:
            assert result.retry_after_seconds == budget.remaining_seconds == 7200
        elif status == 500:
            assert budget.remaining_seconds == 60
        elif state is ReadState.PATH_ERROR:
            assert budget.remaining_seconds == 0.25 and not result.stop_access
        assert (await transport_client.read(REQUEST)).http_requests == 0
        assert len(seen) == 1


async def test_long_pause_survives_client_and_token_replacement(gateways, clock):
    responses = [429, 200]

    async def reply(raw, writer):
        writer.write(
            wire_reply(
                payload(), status=responses.pop(0), headers={"Retry-After": "7200"}
            )
        )
        await writer.drain()

    _, seen, _, binding = await gateways(reply)
    old, budget = client(binding, clock)
    async with old:
        result = await old.read(REQUEST)
        assert result.retry_after_seconds == 7200
    changed = replace(
        CONTEXT,
        identity=replace(CONTEXT.identity, generation=CONTEXT.identity.generation + 1),
    )
    new_binding = replace(binding, context=changed)
    new, _ = client(
        new_binding, clock, budget=budget, token="replacement-synthetic-token"
    )
    async with new:
        clock.advance(3601)
        assert (await new.read(REQUEST)).state is ReadState.DEFERRED
        budget.defer(15)
        assert budget.remaining_seconds == 3599
        assert len(seen) == 1
        clock.advance(3599)
        assert (await new.read(REQUEST)).state is ReadState.SUCCESS
    assert len(seen) == 2 and b"replacement-synthetic-token" in seen[1]


@pytest.mark.parametrize("header", [None, "invalid", "-1", "1.5"])
async def test_missing_retry_after_uses_bounded_default_without_retry(
    gateways, clock, header
):
    async def reply(raw, writer):
        writer.write(
            wire_reply(
                status=503, headers={} if header is None else {"Retry-After": header}
            )
        )
        await writer.drain()

    _, seen, _, binding = await gateways(reply)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert result.retry_after_seconds == budget.remaining_seconds == 60
        assert len(seen) == 1


async def test_http_date_retry_after_is_kept_beyond_one_hour(gateways, clock):
    async def reply(raw, writer):
        writer.write(
            wire_reply(
                status=503,
                headers={
                    "Retry-After": format_datetime(
                        datetime.now(UTC) + timedelta(seconds=7200)
                    )
                },
            )
        )
        await writer.drain()

    _, seen, _, binding = await gateways(reply)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert 7198 <= result.retry_after_seconds <= 7200
        assert result.retry_after_seconds == budget.remaining_seconds
        assert len(seen) == 1


@pytest.mark.parametrize(
    "header",
    [
        "999999999",
        format_datetime(datetime.now(UTC) + timedelta(days=30)),
    ],
)
async def test_retry_after_longer_than_one_day_is_shortened(gateways, clock, header):
    async def reply(raw, writer):
        writer.write(wire_reply(status=429, headers={"Retry-After": header}))
        await writer.drain()

    _, _, _, binding = await gateways(reply)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert result.problem == ReadProblem.RATE_LIMITED
        assert 86398 <= budget.remaining_seconds <= 86400


async def test_successful_value_is_not_mutated_by_later_404_and_recovery(
    gateways, clock
):
    responses = [(200, 21.25), (404, 0), (200, 22)]

    async def reply(raw, writer):
        status, value = responses.pop(0)
        writer.write(wire_reply(payload(value=value), status=status))
        await writer.drain()

    _, seen, _, binding = await gateways(reply)
    transport_client, _ = client(binding, clock)
    async with transport_client:
        initial = await transport_client.read(REQUEST)
        initial_time = initial.observation.observed_monotonic
        clock.advance()
        missing = await transport_client.read(REQUEST)
        assert missing.state is ReadState.PATH_ERROR and missing.observation is None
        clock.advance()
        recovered = await transport_client.read(REQUEST)
    assert initial.observation.data.measurement == Decimal("21.25")
    assert initial.observation.observed_monotonic == initial_time
    assert recovered.observation.data.measurement == 22
    assert recovered.observation.observed_monotonic > initial_time
    assert len(seen) == 3


async def test_same_key_renewal_halts_before_any_http(gateways, certificates, clock):
    _, seen, _, binding = await gateways(success, certificate=1)
    old_binding = replace(binding, sha256=certificates[0][1])
    transport_client, _ = client(old_binding, clock)
    async with transport_client:
        first = await transport_client.read(REQUEST)
        assert (
            first.state is ReadState.ACCESS_ERROR
            and first.problem == TransportFailure.TLS_CHANGED
        )
        assert first.http_requests == 0 and not seen
        clock.advance(500)
        assert (
            await transport_client.read(REQUEST)
        ).problem == TransportFailure.TLS_CHANGED
        assert transport_client.metrics.tls_inspections == 1


async def test_certificate_swap_between_inspection_and_http_never_sends_token(
    gateways, clock, monkeypatch
):
    server, seen, _, binding = await gateways(success)
    port = server.sockets[0].getsockname()[1]
    inspect = transport._inspect
    replacement_seen = []

    async def swap(*args, **kwargs):
        result = await inspect(*args, **kwargs)
        server.close()
        await server.wait_closed()
        _, received, _, _ = await gateways(success, certificate=1, port=port)
        replacement_seen.append(received)
        return result

    monkeypatch.setattr(transport, "_inspect", swap)
    transport_client, _ = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert result.problem == TransportFailure.TLS_CHANGED
        assert result.http_requests == 0 and transport_client.metrics.http_requests == 0
    assert not seen and replacement_seen == [[]]


async def test_session_reuse_does_not_reuse_a_connection_after_certificate_change(
    gateways, clock
):
    server, seen, _, binding = await gateways(success)
    port = server.sockets[0].getsockname()[1]
    transport_client, _ = client(binding, clock)
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        session = transport_client._session
        server.close()
        await server.wait_closed()
        _, replaced_seen, _, _ = await gateways(success, certificate=1, port=port)
        clock.advance()
        result = await transport_client.read(REQUEST)
        assert result.problem == TransportFailure.TLS_CHANGED
        assert transport_client._session is session
        assert len(seen) == 1 and not replaced_seen


@pytest.mark.parametrize(
    "failure",
    [
        TransportFailure.DNS,
        TransportFailure.CONNECT,
        TransportFailure.INTERFACE,
        TransportFailure.TLS,
    ],
)
async def test_inspection_failures_are_static_and_pause_without_http(
    clock, monkeypatch, failure
):
    monkeypatch.setattr(
        transport, "_inspect", AsyncMock(side_effect=LocalTransportError(failure))
    )
    binding = CertificateBinding(CONTEXT, PortRole.RESOURCES, b"a" * 32)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert result.state is ReadState.ACCESS_ERROR and result.problem == failure
        assert result.http_requests == 0 and budget.remaining_seconds == 60


async def test_dns_is_inside_total_read_deadline(clock, monkeypatch):
    async def slow(endpoint):
        await asyncio.sleep(10)

    monkeypatch.setattr(transport, "_resolve", slow)
    binding = CertificateBinding(CONTEXT, PortRole.RESOURCES, b"a" * 32)
    transport_client, budget = client(
        binding, clock, limits=LocalReadLimits(timeout=0.03)
    )
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert result.problem == TransportFailure.TIMEOUT and result.stop_access
        assert result.http_requests == 0 and budget.remaining_seconds == 60


async def test_slow_resource_does_not_discard_previous_success_or_block_other_paths(
    gateways, clock
):
    async def reply(raw, writer):
        if b"/system/" in raw:
            await asyncio.sleep(10)
        await success(raw, writer)

    _, seen, _, binding = await gateways(reply)
    transport_client, budget = client(
        binding, clock, limits=LocalReadLimits(timeout=0.08)
    )
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        clock.advance()
        result = await transport_client.read(request_for_path("/system/bus"))
        assert (
            result.state is ReadState.PATH_ERROR
            and result.problem == TransportFailure.TIMEOUT
        )
        assert result.http_requests == 1 and budget.remaining_seconds == 0.25
        clock.advance()
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
    assert len(seen) == 3


async def test_disconnect_has_one_http_attempt_without_hidden_retry(gateways, clock):
    async def disconnect(raw, writer):
        pass

    _, seen, _, binding = await gateways(disconnect)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert result.problem == TransportFailure.CONNECT and result.stop_access
        assert (
            len(seen)
            == result.http_requests
            == transport_client.metrics.http_requests
            == 1
        )
        assert (
            transport_client.metrics.http_responses == 0
            and budget.remaining_seconds == 60
        )


@pytest.mark.parametrize(
    "headers,problem",
    [
        ({"Content-Type": "text/html"}, TransportFailure.INVALID_RESPONSE),
        (
            {"Content-Type": "application/json; charset=latin1"},
            TransportFailure.INVALID_RESPONSE,
        ),
        ({"Content-Encoding": "gzip"}, TransportFailure.INVALID_RESPONSE),
        ({"Content-Length": "262145"}, TransportFailure.BODY_TOO_LARGE),
    ],
)
async def test_body_metadata_errors_are_isolated_to_the_path(
    gateways, clock, headers, problem
):
    async def reply(raw, writer):
        writer.write(wire_reply(payload(), headers=headers))
        await writer.drain()

    _, seen, _, binding = await gateways(reply)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert result.problem == problem and result.state is ReadState.PATH_ERROR
        assert result.observation is None and budget.remaining_seconds == 0.25
        assert result.http_requests == len(seen) == 1


@pytest.mark.parametrize("oversize", [False, True])
async def test_chunked_body_uses_same_limit_as_decoder(gateways, clock, oversize):
    body = payload() + b" " * (1024 + int(oversize) - len(payload()))

    async def reply(raw, writer):
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        )
        writer.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
        await writer.drain()

    _, _, _, binding = await gateways(reply)
    transport_client, _ = client(
        binding, clock, limits=LocalReadLimits(json=JsonLimits(bytes=1024))
    )
    async with transport_client:
        result = await transport_client.read(REQUEST)
        if oversize:
            assert (
                result.problem == TransportFailure.BODY_TOO_LARGE
                and result.observation is None
            )
        else:
            assert result.state is ReadState.SUCCESS


@pytest.mark.parametrize(
    "body,problem",
    [
        (b'{"id":', "invalid_json"),
        (payload(value=True), "invalid_schema"),
        (payload(path="/heatingCircuits/hc1/currentRoomSetpoint"), "wrong_resource"),
    ],
)
async def test_decoder_failures_are_structured_without_losing_http_status(
    gateways, clock, body, problem
):
    async def reply(raw, writer):
        writer.write(wire_reply(body))
        await writer.drain()

    _, seen, _, binding = await gateways(reply)
    transport_client, _ = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert result.problem == problem and result.state is ReadState.PATH_ERROR
        assert result.status == 200 and result.http_requests == len(seen) == 1


async def test_truncated_body_is_not_a_valid_observation(gateways, clock):
    async def reply(raw, writer):
        writer.write(wire_reply(b"{", headers={"Content-Length": "1000"}))
        await writer.drain()

    _, _, _, binding = await gateways(reply)
    transport_client, _ = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert (
            result.problem == TransportFailure.INVALID_RESPONSE
            and result.observation is None
        )


async def test_stale_after_inspection_sends_no_http(gateways, clock, monkeypatch):
    _, seen, _, binding = await gateways(success)
    current = True
    inspect = transport._inspect

    async def changed(*args, **kwargs):
        nonlocal current
        result = await inspect(*args, **kwargs)
        current = False
        return result

    monkeypatch.setattr(transport, "_inspect", changed)
    transport_client, budget = client(binding, clock, is_current=lambda: current)
    async with transport_client:
        with pytest.raises(StaleLocalRequest):
            await transport_client.read(REQUEST)
        assert not seen and not budget.busy


async def test_stale_body_and_caller_cancellation_publish_no_value(gateways, clock):
    sent = asyncio.Event()
    release = asyncio.Event()

    async def reply(raw, writer):
        body = payload()
        writer.write(wire_reply(body)[:-1])
        await writer.drain()
        sent.set()
        await release.wait()
        writer.write(body[-1:])
        await writer.drain()

    _, seen, _, binding = await gateways(reply)
    current = True
    transport_client, budget = client(binding, clock, is_current=lambda: current)
    async with transport_client:
        task = asyncio.create_task(transport_client.read(REQUEST))
        await asyncio.wait_for(sent.wait(), 2)
        current = False
        release.set()
        with pytest.raises(StaleLocalRequest):
            await task
        current = True
        release.clear()
        sent.clear()
        clock.advance()
        task = asyncio.create_task(transport_client.read(REQUEST))
        await asyncio.wait_for(sent.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not budget.busy and transport_client.metrics.successes == 0
    assert len(seen) == 2


async def test_close_cancels_owned_read_without_waiting_for_its_caller_to_finish(
    gateways, clock
):
    wait = asyncio.Event()

    async def reply(raw, writer):
        await wait.wait()

    _, _, received, binding = await gateways(reply)
    transport_client, budget = client(binding, clock)
    continue_caller = asyncio.Event()

    async def caller():
        try:
            await transport_client.read(REQUEST)
        except asyncio.CancelledError:
            continue_caller.set()
            return "continued"

    task = asyncio.create_task(caller())
    await asyncio.wait_for(received.wait(), 2)
    await asyncio.wait_for(transport_client.close(), 2)
    assert await task == "continued" and continue_caller.is_set()
    assert transport_client._session.closed and not budget.busy
    await transport_client.close()
    with pytest.raises(StaleLocalRequest):
        await transport_client.read(REQUEST)


async def test_cancelled_close_waiter_does_not_abandon_session_cleanup(
    gateways, clock, monkeypatch
):
    _, _, _, binding = await gateways(success)
    transport_client, _ = client(binding, clock)
    await transport_client.read(REQUEST)
    entered = asyncio.Event()
    release = asyncio.Event()
    close = aiohttp.ClientSession.close

    async def slow_close(session):
        entered.set()
        await release.wait()
        await close(session)

    monkeypatch.setattr(aiohttp.ClientSession, "close", slow_close)
    task = asyncio.create_task(transport_client.close())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.wait_for(transport_client.close(), 2)
    assert transport_client._session.closed and transport_client._token == ""


async def test_new_resource_forms_and_queries_reach_wire_without_encoding_changes(
    gateways, clock
):
    fixtures = json.loads(
        (Path(__file__).parent / "fixtures/local_api_examples.json").read_text()
    )["examples"]
    recording = next(
        row["response"]
        for row in fixtures
        if row["response"]["type"] == "yRecordingExtended"
    )
    path = recording["id"].split("?")[0]
    query = {"sampleRate": "all", "startDate": "2025-09-01", "endDate": "2025-10-30"}
    request = make_request(path, query=query)

    async def reply(raw, writer):
        if b"/recordings/" in raw:
            body = json.dumps(recording).encode()
        else:
            body = payload(path="/signals/Room.Ä", type="floatValue")
        writer.write(wire_reply(body))
        await writer.drain()

    _, seen, _, binding = await gateways(reply)
    transport_client, _ = client(binding, clock)
    async with transport_client:
        assert (await transport_client.read(request)).state is ReadState.SUCCESS
        assert request.target.encode() in seen[0]
        clock.advance()
        signal = make_request(
            "/signals/{signalId}",
            parameters={"signalId": "Room.Ä"},
            signal_ids=("Room.Ä",),
        )
        assert (await transport_client.read(signal)).state is ReadState.SUCCESS
        assert b"/signals/Room.%C3%84" in seen[1] and b"%25" not in seen[1]


async def test_proxy_cookies_and_ambient_auth_are_not_used(
    gateways, clock, monkeypatch, tmp_path
):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    netrc = tmp_path / "netrc"
    netrc.write_text("machine 127.0.0.1 login wrong password wrong\n")
    monkeypatch.setenv("NETRC", str(netrc))

    async def reply(raw, writer):
        writer.write(wire_reply(payload(), headers={"Set-Cookie": "ambient=wrong"}))
        await writer.drain()

    _, seen, _, binding = await gateways(reply)
    transport_client, _ = client(binding, clock)
    async with transport_client:
        await transport_client.read(REQUEST)
        clock.advance()
        await transport_client.read(REQUEST)
    assert len(seen) == 2
    assert all(
        b"Cookie:" not in raw
        and b"Basic " not in raw
        and b"Accept-Encoding: identity" in raw
        for raw in seen
    )


@pytest.mark.parametrize("token", [None, "", "bad\nheader", "ä", "x" * 8193])
def test_invalid_credentials_never_create_a_session(clock, token):
    binding = CertificateBinding(CONTEXT, PortRole.RESOURCES, b"a" * 32)
    with pytest.raises(ValueError, match="invalid_local_token"):
        client(binding, clock, token=token)


def test_role_context_and_budget_scope_must_match(clock):
    binding = CertificateBinding(CONTEXT, PortRole.AUTHENTICATION, b"a" * 32)
    with pytest.raises(ValueError, match="resource_certificate_binding_required"):
        client(binding, clock)
    changed = replace(
        CONTEXT, identity=replace(CONTEXT.identity, connection_id="other-local")
    )
    binding = CertificateBinding(CONTEXT, PortRole.RESOURCES, b"a" * 32)
    with pytest.raises(StaleLocalRequest):
        LocalReadClient(
            changed,
            TOKEN,
            binding,
            budget=LocalReadBudget(changed),
            is_current=lambda: True,
        )
    with pytest.raises(ValueError, match="budget_scope_mismatch"):
        client(binding, clock, budget=LocalReadBudget(changed))


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan")])
def test_invalid_timeout_is_not_accepted(value):
    with pytest.raises(ValueError):
        LocalReadLimits(timeout=value)


@pytest.mark.parametrize("value", [-1, True, float("inf"), float("nan")])
def test_invalid_pause_is_not_accepted(clock, value):
    with pytest.raises(ValueError):
        LocalReadBudget(CONTEXT, clock=clock).defer(value)


async def test_forged_request_rejected_before_dns(clock, monkeypatch):
    inspect = AsyncMock()
    monkeypatch.setattr(transport, "_inspect", inspect)
    binding = CertificateBinding(CONTEXT, PortRole.RESOURCES, b"a" * 32)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        with pytest.raises(LocalResourceError):
            await transport_client.read(replace(REQUEST, query=(("refresh", True),)))
        assert not budget.busy
    inspect.assert_not_called()


async def test_received_pause_survives_generation_change(gateways, clock):
    current = True

    async def reply(raw, writer):
        nonlocal current
        writer.write(wire_reply(b"", status=429, headers={"Retry-After": "7200"}))
        await writer.drain()
        current = False

    _, seen, _, binding = await gateways(reply)
    transport_client, budget = client(binding, clock, is_current=lambda: current)
    async with transport_client:
        with pytest.raises(StaleLocalRequest):
            await transport_client.read(REQUEST)
        assert budget.remaining_seconds == 7200 and not budget.busy
        assert transport_client.metrics.successes == 0 and len(seen) == 1


async def test_cancel_before_owned_task_starts_releases_budget(clock, monkeypatch):
    inspect = AsyncMock()
    monkeypatch.setattr(transport, "_inspect", inspect)
    binding = CertificateBinding(CONTEXT, PortRole.RESOURCES, b"a" * 32)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        task = asyncio.create_task(transport_client.read(REQUEST))
        await asyncio.sleep(0)
        assert len(budget._state.root().leases) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not budget.busy and transport_client._session is None
    inspect.assert_not_called()


async def test_cancellation_during_dns_does_not_send_http(gateways, clock, monkeypatch):
    _, seen, _, binding = await gateways(success)
    started = asyncio.Event()

    async def resolve(endpoint):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(transport, "_resolve", resolve)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        task = asyncio.create_task(transport_client.read(REQUEST))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not seen and not budget.busy and transport_client._session is None


async def test_decoder_cannot_publish_after_request_deadline(
    gateways, clock, monkeypatch
):
    _, _, _, binding = await gateways(success)
    transport_client, _ = client(binding, clock)
    loop = asyncio.get_running_loop()
    original_time = loop.time
    decode = reader.decode_resource

    def overdue(*args, **kwargs):
        result = decode(*args, **kwargs)
        monkeypatch.setattr(loop, "time", lambda: original_time() + 10)
        return result

    monkeypatch.setattr(reader, "decode_resource", overdue)
    async with transport_client:
        try:
            result = await transport_client.read(REQUEST)
        finally:
            monkeypatch.setattr(loop, "time", original_time)
        assert result.problem == TransportFailure.TIMEOUT
        assert result.observation is None and transport_client.metrics.successes == 0


async def test_zero_retry_after_keeps_minimum_spacing(gateways, clock):
    async def reply(raw, writer):
        writer.write(wire_reply(b"", status=429, headers={"Retry-After": "0"}))
        await writer.drain()

    _, seen, _, binding = await gateways(reply)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        result = await transport_client.read(REQUEST)
        assert result.problem == ReadProblem.RATE_LIMITED
        assert budget.remaining_seconds == 0.25 and result.retry_after_seconds == 0.25
        assert (await transport_client.read(REQUEST)).state is ReadState.DEFERRED
        assert len(seen) == 1


# ------------------------------------------- one TLS connection per read (A3)


@pytest.fixture
def connections(monkeypatch):
    """Every TLS connection the loopback gateways accept (after the handshake)."""
    accepted = []
    start_server = asyncio.start_server

    async def counting(handle, *args, **kwargs):
        async def counted(reader, writer):
            accepted.append(writer)
            await handle(reader, writer)

        return await start_server(counted, *args, **kwargs)

    monkeypatch.setattr(asyncio, "start_server", counting)
    return accepted


async def test_reads_after_the_first_need_one_tls_connection_each(
    gateways, clock, connections
):
    _, seen, _, binding = await gateways(success)
    transport_client, _ = client(binding, clock)
    async with transport_client:
        for _ in range(3):
            assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
            clock.advance()
        # One certificate inspection, then only the pinned requests.
        assert transport_client.metrics.tls_inspections == 1
        assert len(seen) == 3 and len(connections) == 4


async def test_changed_certificate_without_inspection_sends_no_token(
    gateways, clock, connections
):
    server, seen, _, binding = await gateways(success)
    port = server.sockets[0].getsockname()[1]
    transport_client, _ = client(binding, clock)
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        server.close()
        await server.wait_closed()
        _, replaced_seen, _, _ = await gateways(success, certificate=1, port=port)
        clock.advance()
        result = await transport_client.read(REQUEST)
        assert result.state is ReadState.ACCESS_ERROR
        assert result.problem == TransportFailure.TLS_CHANGED
        assert result.http_requests == 0 and not replaced_seen
        assert transport_client.metrics.tls_inspections == 1
        clock.advance(500)
        # Stopped: no further connection to the changed gateway.
        count = len(connections)
        again = await transport_client.read(REQUEST)
        assert again.problem == TransportFailure.TLS_CHANGED
        assert len(connections) == count and not replaced_seen
    assert len(seen) == 1


async def test_connection_failure_inspects_the_certificate_again(gateways, clock):
    server, _, _, binding = await gateways(success)
    port = server.sockets[0].getsockname()[1]
    transport_client, budget = client(binding, clock)
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        server.close()
        await server.wait_closed()
        clock.advance()
        result = await transport_client.read(REQUEST)
        assert result.problem == TransportFailure.CONNECT
        await gateways(success, port=port)
        clock.advance(budget.remaining_seconds)
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        assert transport_client.metrics.tls_inspections == 2


async def test_address_change_inspects_the_certificate_again(
    gateways, clock, monkeypatch
):
    _, seen, _, binding = await gateways(success)
    transport_client, _ = client(binding, clock)
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        # The name now resolves to another address only.
        monkeypatch.setattr(
            transport, "_resolve", AsyncMock(return_value=("192.0.2.77",))
        )
        clock.advance()
        inspect = AsyncMock(side_effect=LocalTransportError(TransportFailure.CONNECT))
        monkeypatch.setattr(transport, "_inspect", inspect)
        result = await transport_client.read(REQUEST)
        assert result.problem == TransportFailure.CONNECT
        inspect.assert_awaited_once()
        assert len(seen) == 1


# ------------------------------------- address reuse and health (O48, O93)


async def test_a_resolved_address_is_reused_for_a_minute_only(
    gateways, clock, monkeypatch
):
    _, seen, _, binding = await gateways(success)
    resolve, calls = transport._resolve, []

    async def counted(endpoint):
        calls.append(endpoint)
        return await resolve(endpoint)

    monkeypatch.setattr(transport, "_resolve", counted)
    now = [1000.0]
    monkeypatch.setattr(reader, "monotonic", lambda: now[0])
    transport_client, _ = client(binding, clock)
    async with transport_client:
        for _ in range(3):  # the first read resolves inside the inspection
            assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
            clock.advance()
            now[0] += 10
        assert len(calls) == 2  # inspection, then one checked resolution
        now[0] += reader.RESOLVE_REUSE_SECONDS
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        assert len(calls) == 3 and len(seen) == 4
        assert transport_client.metrics.tls_inspections == 1


async def test_a_connection_failure_resolves_the_address_again(gateways, clock):
    server, _, _, binding = await gateways(success)
    port = server.sockets[0].getsockname()[1]
    transport_client, budget = client(binding, clock)
    async with transport_client:
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        clock.advance()
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS
        assert transport_client._resolved is not None
        server.close()
        await server.wait_closed()
        clock.advance()
        assert (await transport_client.read(REQUEST)).problem == (
            TransportFailure.CONNECT
        )
        assert transport_client._resolved is None and transport_client._peer is None
        await gateways(success, port=port)
        clock.advance(budget.remaining_seconds)
        assert (await transport_client.read(REQUEST)).state is ReadState.SUCCESS


async def test_health_counts_failure_classes_times_and_deferrals(gateways, clock):
    replies = iter((200, 404, 403, 500))

    async def reply(raw, writer):
        status = next(replies)
        writer.write(wire_reply(payload() if status == 200 else b"{}", status=status))
        await writer.drain()

    _, _, _, binding = await gateways(reply)
    transport_client, budget = client(binding, clock)
    async with transport_client:
        assert transport_client.health() == {
            "failures_by_class": {},
            "response_times": {"samples": 0},
            "deferred_reasons": {},
        }
        for _ in range(4):
            await transport_client.read(REQUEST)
            clock.advance(budget.remaining_seconds or 1)
        # The 500 paused the access: the next read is deferred.
        budget.defer(60)
        assert (await transport_client.read(REQUEST)).state is ReadState.DEFERRED
        health = transport_client.health()
        assert health["failures_by_class"] == {
            "http_4xx": 1,
            "http_5xx": 1,
            "not_found": 1,
        }
        assert health["deferred_reasons"] == {"paused": 1}
        times = health["response_times"]
        assert times["samples"] == 4
        assert 0 < times["average_ms"] <= times["p95_ms"] <= times["maximum_ms"]
        assert "127.0.0.1" not in json.dumps(health) and PATH not in json.dumps(health)


@pytest.mark.parametrize(
    "status,problem,expected",
    [
        (None, TransportFailure.TIMEOUT, "timeout"),
        (None, TransportFailure.TLS, "tls"),
        (None, TransportFailure.TLS_CHANGED, "tls"),
        (404, ReadProblem.NOT_FOUND, "not_found"),
        (403, ReadProblem.FORBIDDEN, "http_4xx"),
        (429, ReadProblem.RATE_LIMITED, "http_4xx"),
        (503, ReadProblem.SERVICE_UNAVAILABLE, "http_5xx"),
        (None, TransportFailure.CONNECT, "connection"),
        (None, TransportFailure.DNS, "connection"),
        (200, TransportFailure.INVALID_RESPONSE, "other"),
    ],
)
def test_failure_classes(status, problem, expected):
    result = reader.ReadResult(REQUEST, ReadState.PATH_ERROR, status, problem)
    assert reader._failure_class(result) == expected
    assert reader._failure_class(replace(result, state=ReadState.SUCCESS)) is None
