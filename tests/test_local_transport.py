"""Local TLS transport against synthetic loopback servers, never a gateway."""

from __future__ import annotations

import asyncio
import socket
import ssl
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from custom_components.bosch_buderus_heating import local_transport as transport
from custom_components.bosch_buderus_heating.local_probe import (
    MAX_BODY_BYTES,
    LocalEndpoint,
    ProbePath,
    ProbeProblem,
    ProbeStatus,
    TransportFailure,
    assess_local_probe,
)
from custom_components.bosch_buderus_heating.local_transport import (
    CertificateBinding,
    LocalTransportError,
    PortRole,
    StaleLocalRequest,
    inspect_local_certificate,
    read_local_probe,
)
from tests.test_local_probe import CONTEXT, IDENTIFIER, response

pytestmark = pytest.mark.usefixtures("socket_enabled")
TOKEN = "synthetic-token-for-loopback-tests"


@pytest.fixture
def certificates(tmp_path):
    """Same public key, different certificates exercise full-leaf pinning."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-gateway")])
    results = []
    for serial in (1, 2):
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(serial)
            .not_valid_before(datetime.now(UTC) - timedelta(days=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        pem = tmp_path / f"synthetic-{serial}.pem"
        private = tmp_path / f"synthetic-{serial}.key"
        pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(pem, private)
        results.append((context, cert.fingerprint(hashes.SHA256())))
    return results


def wire_reply(body=b"{}", *, status=200, headers=None):
    fields = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Connection": "close",
    }
    fields.update(headers or {})
    return (
        f"HTTP/1.1 {status} Test\r\n"
        + "".join(f"{key}: {value}\r\n" for key, value in fields.items())
        + "\r\n"
    ).encode() + body


@pytest.fixture
async def gateways(certificates, monkeypatch):
    servers = []
    tasks = set()

    async def start(responder=None, *, certificate=0, host="127.0.0.1", port=0):
        requests = []
        received = asyncio.Event()

        async def handle(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                raw = await reader.readuntil(b"\r\n\r\n")
                requests.append(raw)
                received.set()
                if responder is None:
                    path = ProbePath(raw.split(b" ", 2)[1].decode())
                    writer.write(wire_reply(response(path).body))
                    await writer.drain()
                else:
                    await responder(raw, writer)
            except asyncio.IncompleteReadError, ConnectionError:
                pass
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
                tasks.discard(task)

        server = await asyncio.start_server(
            handle, host, port, ssl=certificates[certificate][0]
        )
        servers.append(server)
        actual_port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(LocalEndpoint, "resource_port", actual_port)
        # The production resolver rejects loopback. Only this fixture redirects
        # a synthetic hostname; resolver behavior has separate tests below.
        monkeypatch.setattr(transport, "_resolve", AsyncMock(return_value=(host,)))
        return (
            server,
            requests,
            received,
            CertificateBinding(
                CONTEXT, PortRole.RESOURCES, certificates[certificate][1]
            ),
        )

    yield start
    for server in servers:
        server.close()
        await server.wait_closed()
    for task in tuple(tasks):
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def read(binding, *, is_current=lambda: True):
    return await read_local_probe(CONTEXT, TOKEN, binding, is_current=is_current)


def problem(exchange):
    assessment = assess_local_probe(exchange.responses, current_context=CONTEXT)
    assert assessment.identity is None
    return assessment.issues[0].problem


async def test_tls_inspection_has_no_http_and_successful_probe_preserves_evidence(
    gateways,
):
    _, requests, _, binding = await gateways()
    observed = await inspect_local_certificate(
        CONTEXT, PortRole.RESOURCES, is_current=lambda: True
    )
    assert observed.sha256 == binding.sha256 and observed.tls_version in (
        "TLSv1.2",
        "TLSv1.3",
    )
    assert not requests
    exchange = await read(binding)
    assessment = assess_local_probe(exchange.responses, current_context=CONTEXT)
    assert assessment.status == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
    assert assessment.identity is not None
    assert [r.split(b" ")[1].decode() for r in requests] == [p.value for p in ProbePath]
    assert all(
        r.startswith(b"GET ") and f"Authorization: Bearer {TOKEN}\r\n".encode() in r
        for r in requests
    )
    for evidence in (observed, binding, exchange):
        assert TOKEN not in repr(evidence) and IDENTIFIER not in repr(evidence)
        assert CONTEXT.endpoint.host not in repr(evidence)
        assert binding.sha256.hex() not in repr(evidence)


async def test_renewed_certificate_with_same_key_stops_before_http(
    gateways, certificates
):
    _, requests, _, binding = await gateways(certificate=1)
    old_binding = replace(binding, sha256=certificates[0][1])
    assert problem(await read(old_binding)) == ProbeProblem.TLS_CHANGED
    assert not requests


async def test_certificate_swap_after_inspection_blocks_bearer(
    gateways, certificates, monkeypatch
):
    server, requests, _, binding = await gateways()
    port = server.sockets[0].getsockname()[1]
    original = transport._inspect
    replacement_requests = []

    async def swap(*args, **kwargs):
        observation = await original(*args, **kwargs)
        server.close()
        await server.wait_closed()
        _, seen, _, _ = await gateways(certificate=1, port=port)
        replacement_requests.append(seen)
        return observation

    monkeypatch.setattr(transport, "_inspect", swap)
    assert problem(await read(binding)) == ProbeProblem.TLS_CHANGED
    assert not requests and replacement_requests == [[]]


async def test_changed_first_peer_cannot_be_hidden_by_alternate_peer(
    gateways, certificates, monkeypatch
):
    _, requests, _, binding = await gateways(certificate=1)
    resolver = AsyncMock(return_value=("127.0.0.1", "127.0.0.2"))
    connection = AsyncMock(wraps=asyncio.open_connection)
    monkeypatch.setattr(transport, "_resolve", resolver)
    monkeypatch.setattr(asyncio, "open_connection", connection)
    assert (
        problem(await read(replace(binding, sha256=certificates[0][1])))
        == ProbeProblem.TLS_CHANGED
    )
    assert connection.await_count == 1 and not requests


async def test_unreachable_peer_can_fall_back_before_any_http(gateways, monkeypatch):
    _, requests, _, binding = await gateways()
    original = asyncio.open_connection

    async def connection(host, *args, **kwargs):
        if host == "127.0.0.2":
            raise ConnectionRefusedError()
        return await original(host, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", connection)
    monkeypatch.setattr(
        transport, "_resolve", AsyncMock(return_value=("127.0.0.2", "127.0.0.1"))
    )
    assert len((await read(binding)).responses) == 3
    assert len(requests) == 3


async def test_authentication_port_is_independent_and_never_needed_to_import_token(
    gateways, certificates, monkeypatch
):
    server, requests, _, binding = await gateways()
    monkeypatch.setattr(LocalEndpoint, "auth_port", server.sockets[0].getsockname()[1])
    authentication = await inspect_local_certificate(
        CONTEXT, PortRole.AUTHENTICATION, is_current=lambda: True
    )
    assert authentication.role == PortRole.AUTHENTICATION
    assert authentication.sha256 == certificates[0][1] and not requests
    monkeypatch.setattr(LocalEndpoint, "auth_port", 1)
    assert len((await read(binding)).responses) == 3
    with pytest.raises(ValueError, match="resource_certificate_binding_required"):
        await read(replace(binding, role=PortRole.AUTHENTICATION))
    assert len(requests) == 3


async def test_two_ports_may_have_different_certificates(gateways, monkeypatch):
    auth, auth_requests, _, auth_binding = await gateways()
    monkeypatch.setattr(LocalEndpoint, "auth_port", auth.sockets[0].getsockname()[1])
    _, data_requests, _, binding = await gateways(certificate=1)
    observed = await inspect_local_certificate(
        CONTEXT, PortRole.AUTHENTICATION, is_current=lambda: True
    )
    assert observed.sha256 == auth_binding.sha256 != binding.sha256
    assert len((await read(binding)).responses) == 3
    assert len(data_requests) == 3 and not auth_requests


async def test_total_budget_covers_sum_of_requests(gateways, monkeypatch):
    async def reply(raw, writer):
        await asyncio.sleep(0.12)
        path = ProbePath(raw.split(b" ")[1].decode())
        writer.write(wire_reply(response(path).body))
        await writer.drain()

    _, requests, _, binding = await gateways(reply)
    monkeypatch.setattr(transport, "PROBE_TIMEOUT", 0.2)
    exchange = await read(binding)
    assert problem(exchange) == ProbeProblem.TIMEOUT
    assert exchange.responses[0].status == 200
    assert len(exchange.responses) == 2 and len(requests) == 2


async def test_huge_interface_index_is_a_structured_failure(monkeypatch):
    monkeypatch.setattr(socket, "if_indextoname", Mock(side_effect=OverflowError()))
    with pytest.raises(LocalTransportError, match="interface_required"):
        await transport._resolve(LocalEndpoint("fe80::7%" + "9" * 64))


@pytest.mark.parametrize(
    "token",
    [None, "", " ", " secret", "secret\n", "secret\r\nX: value", "ä", "x" * 8193],
)
async def test_invalid_credentials_do_not_start_transport(token, monkeypatch):
    resolve = AsyncMock()
    monkeypatch.setattr(transport, "_resolve", resolve)
    with pytest.raises(ValueError, match=r"^invalid_local_token$"):
        await read_local_probe(
            CONTEXT,
            token,
            CertificateBinding(CONTEXT, PortRole.RESOURCES, b"a" * 32),
            is_current=lambda: True,
        )
    resolve.assert_not_called()


@pytest.mark.parametrize("pin", [None, "a" * 32, b"a", b"a" * 31, b"a" * 33])
def test_binding_rejects_invalid_digest_without_echo(pin):
    with pytest.raises(ValueError, match=r"^invalid_certificate_binding$"):
        CertificateBinding(CONTEXT, PortRole.RESOURCES, pin)


async def test_invalid_roles_and_stale_binding_do_not_start_transport(monkeypatch):
    resolve = AsyncMock()
    monkeypatch.setattr(transport, "_resolve", resolve)
    with pytest.raises(ValueError, match="invalid_port_role"):
        await inspect_local_certificate(CONTEXT, "resources", is_current=lambda: True)
    with pytest.raises(ValueError, match="invalid_certificate_binding"):
        CertificateBinding(CONTEXT, "resources", b"a" * 32)
    binding = CertificateBinding(
        replace(CONTEXT, endpoint=LocalEndpoint("other.local")),
        PortRole.RESOURCES,
        b"a" * 32,
    )
    with pytest.raises(StaleLocalRequest):
        await read(binding)
    resolve.assert_not_called()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (301, ProbeProblem.REDIRECT),
        (307, ProbeProblem.REDIRECT),
        (401, ProbeProblem.AUTH_REJECTED),
        (403, ProbeProblem.FORBIDDEN),
        (404, ProbeProblem.NOT_FOUND),
        (429, ProbeProblem.RATE_LIMITED),
        (500, ProbeProblem.SERVICE_UNAVAILABLE),
        (503, ProbeProblem.SERVICE_UNAVAILABLE),
    ],
)
async def test_non_success_stops_probe_without_redirects_or_error_body(
    gateways, status, expected
):
    async def reply(raw, writer):
        writer.write(
            wire_reply(
                b"private server error",
                status=status,
                headers={"Location": "https://127.0.0.1:1/target", "Retry-After": "37"},
            )
        )
        await writer.drain()

    _, requests, _, binding = await gateways(reply)
    exchange = await read(binding)
    assert problem(exchange) == expected
    assert len(requests) == 1 and exchange.responses[0].body == b""
    assert exchange.retry_after_seconds == (37 if status in (429, 503) else None)


async def test_http_disconnect_does_not_send_hidden_retry(gateways):
    async def disconnect(raw, writer):
        pass

    _, requests, _, binding = await gateways(disconnect)
    assert problem(await read(binding)) == ProbeProblem.CONNECT
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Content-Type": "text/html"}, ProbeProblem.INVALID_RESPONSE),
        (
            {"Content-Type": "application/json; charset=latin1"},
            ProbeProblem.INVALID_RESPONSE,
        ),
        ({"Content-Encoding": "gzip"}, ProbeProblem.INVALID_RESPONSE),
        ({"Content-Length": str(MAX_BODY_BYTES + 1)}, ProbeProblem.BODY_TOO_LARGE),
    ],
)
async def test_bad_response_metadata_stops_before_accepting_body(
    gateways, headers, expected
):
    async def reply(raw, writer):
        writer.write(wire_reply(headers=headers))
        await writer.drain()

    _, requests, _, binding = await gateways(reply)
    assert problem(await read(binding)) == expected
    assert len(requests) == 1


@pytest.mark.parametrize("oversize", [False, True])
async def test_chunked_json_body_limit(gateways, oversize):
    async def reply(raw, writer):
        body = response(ProbePath(raw.split(b" ")[1].decode())).body
        body += b" " * (MAX_BODY_BYTES + int(oversize) - len(body))
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=UTF-8\r\n"
            b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        )
        for start in range(0, len(body), 16000):
            chunk = body[start : start + 16000]
            writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            await writer.drain()
        writer.write(b"0\r\n\r\n")
        await writer.drain()

    _, requests, _, binding = await gateways(reply)
    exchange = await read(binding)
    if oversize:
        assert problem(exchange) == ProbeProblem.BODY_TOO_LARGE and len(requests) == 1
    else:
        assert (
            assess_local_probe(exchange.responses, current_context=CONTEXT).status
            == ProbeStatus.READY_FOR_IDENTITY_COMPARISON
        )
        assert all(len(item.body) == MAX_BODY_BYTES for item in exchange.responses)


async def test_truncated_body_is_not_published(gateways):
    async def reply(raw, writer):
        writer.write(wire_reply(b"{", headers={"Content-Length": "1000"}))
        await writer.drain()

    _, requests, _, binding = await gateways(reply)
    assert problem(await read(binding)) == ProbeProblem.CONNECT and len(requests) == 1


@pytest.mark.parametrize("at", [ProbePath.BRAND, ProbePath.FIRMWARE, ProbePath.SYSTEM])
async def test_request_timeout_retains_only_completed_responses(
    gateways, monkeypatch, at
):
    async def reply(raw, writer):
        path = ProbePath(raw.split(b" ")[1].decode())
        if path == at:
            await asyncio.sleep(10)
        writer.write(wire_reply(response(path).body))
        await writer.drain()

    _, requests, _, binding = await gateways(reply)
    monkeypatch.setattr(transport, "REQUEST_TIMEOUT", 0.15)
    exchange = await read(binding)
    assert problem(exchange) == ProbeProblem.TIMEOUT
    expected = list(ProbePath)[: list(ProbePath).index(at) + 1]
    assert [r.path for r in exchange.responses] == expected
    assert len(requests) == len(expected)


async def test_whole_probe_budget_includes_dns_and_tls(monkeypatch):
    async def slow(endpoint):
        await asyncio.sleep(10)

    monkeypatch.setattr(transport, "_resolve", slow)
    monkeypatch.setattr(transport, "PROBE_TIMEOUT", 0.03)
    binding = CertificateBinding(CONTEXT, PortRole.RESOURCES, b"a" * 32)
    assert problem(await read(binding)) == ProbeProblem.TIMEOUT
    monkeypatch.setattr(transport, "REQUEST_TIMEOUT", 0.03)
    with pytest.raises(LocalTransportError) as error:
        await inspect_local_certificate(
            CONTEXT, PortRole.RESOURCES, is_current=lambda: True
        )
    assert error.value.failure == TransportFailure.TIMEOUT


async def test_stale_generation_during_body_read_and_cancellation_publish_nothing(
    gateways,
):
    blocked = asyncio.Event()
    release = asyncio.Event()

    async def reply(raw, writer):
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n{"
        )
        await writer.drain()
        blocked.set()
        await release.wait()
        writer.write(b"}")
        await writer.drain()

    _, requests, _, binding = await gateways(reply)
    current = True
    task = asyncio.create_task(read(binding, is_current=lambda: current))
    await asyncio.wait_for(blocked.wait(), 2)
    current = False
    release.set()
    with pytest.raises(StaleLocalRequest):
        await task
    assert len(requests) == 1
    release.clear()
    blocked.clear()
    task = asyncio.create_task(read(binding))
    await asyncio.wait_for(blocked.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(requests) == 2


async def test_stale_guard_before_and_after_dns_blocks_tls(monkeypatch):
    current = False
    connect = AsyncMock()
    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(StaleLocalRequest):
        await inspect_local_certificate(
            CONTEXT, PortRole.RESOURCES, is_current=lambda: current
        )
    current = True

    async def resolve(endpoint):
        nonlocal current
        current = False
        return ("127.0.0.1",)

    monkeypatch.setattr(transport, "_resolve", resolve)
    with pytest.raises(StaleLocalRequest):
        await inspect_local_certificate(
            CONTEXT, PortRole.RESOURCES, is_current=lambda: current
        )
    connect.assert_not_called()


async def test_proxy_environment_cookies_and_ambient_auth_are_not_used(
    gateways, monkeypatch, tmp_path
):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    netrc = tmp_path / "netrc"
    netrc.write_text("machine 127.0.0.1 login wrong password wrong\n")
    monkeypatch.setenv("NETRC", str(netrc))

    async def reply(raw, writer):
        path = ProbePath(raw.split(b" ")[1].decode())
        writer.write(
            wire_reply(response(path).body, headers={"Set-Cookie": "ambient=wrong"})
        )
        await writer.drain()

    _, requests, _, binding = await gateways(reply)
    assert len((await read(binding)).responses) == 3
    assert len(requests) == 3
    assert all(
        b"Cookie:" not in raw
        and b"Basic " not in raw
        and b"Accept-Encoding: identity" in raw
        for raw in requests
    )


@pytest.mark.parametrize(
    "raw",
    [None, "", "bad", "-1", "1.5", "\uff11", "9" * 129, "Sun, 06 Nov 1994 08:49:37"],
)
def test_unusable_retry_after_is_not_guessed(raw):
    assert transport._retry_after(raw) is None


def test_retry_after_seconds_dates_and_past_dates():
    assert transport._retry_after("123") == 123
    assert transport._retry_after("Sun, 06 Nov 1994 08:49:37 GMT") == 0
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=60))
    assert 58 <= transport._retry_after(future) <= 60


def dns_row(host, family=socket.AF_INET, scope=0):
    return (
        family,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        "",
        (host, 0, 0, scope) if family == socket.AF_INET6 else (host, 0),
    )


async def test_resolution_filters_unsafe_and_duplicate_addresses_and_is_bounded(
    monkeypatch,
):
    rows = [
        dns_row("127.0.0.1"),
        dns_row("0.0.0.0"),
        dns_row("192.0.2.5"),
        dns_row("192.0.2.5"),
        dns_row("fe80::7", socket.AF_INET6, 9),
        dns_row("ignored", socket.AF_UNIX),
    ]
    rows += [dns_row("224.0.0.1")] * 10 + [dns_row("192.0.2.99")]
    lookup = AsyncMock(return_value=rows)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    monkeypatch.setattr(socket, "if_indextoname", lambda value: "eth9")
    assert await transport._resolve(CONTEXT.endpoint) == ("192.0.2.5", "fe80::7%9")
    assert lookup.await_count == 1


@pytest.mark.parametrize(
    "result",
    [
        [],
        [dns_row("::1", socket.AF_INET6)],
        [dns_row("fe80::7", socket.AF_INET6)],
        OSError("private resolver error"),
    ],
)
async def test_dns_failure_has_only_static_error(result, monkeypatch):
    lookup = (
        AsyncMock(side_effect=result)
        if isinstance(result, Exception)
        else AsyncMock(return_value=result)
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    with pytest.raises(LocalTransportError, match=r"^name_resolution_failed$"):
        await transport._resolve(CONTEXT.endpoint)


async def test_numeric_addresses_skip_dns_and_scoped_ipv6_uses_local_interface(
    monkeypatch,
):
    lookup = AsyncMock()
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    monkeypatch.setattr(socket, "if_nametoindex", lambda name: 9)
    assert await transport._resolve(LocalEndpoint("192.0.2.5")) == ("192.0.2.5",)
    assert await transport._resolve(LocalEndpoint("fe80::7%eth9")) == ("fe80::7%9",)
    lookup.assert_not_called()
    monkeypatch.setattr(
        socket, "if_indextoname", Mock(side_effect=OSError("private interface"))
    )
    with pytest.raises(LocalTransportError, match=r"^interface_required$"):
        await transport._resolve(LocalEndpoint("fe80::7%999"))


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ssl.SSLError("private TLS details"), TransportFailure.TLS),
        (OSError("private connect details"), TransportFailure.CONNECT),
        (TimeoutError(), TransportFailure.TIMEOUT),
    ],
)
async def test_pre_http_connection_failures_are_sanitized(
    monkeypatch, failure, expected
):
    monkeypatch.setattr(transport, "_resolve", AsyncMock(return_value=("192.0.2.5",)))
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(side_effect=failure))
    with pytest.raises(LocalTransportError) as error:
        await inspect_local_certificate(
            CONTEXT, PortRole.RESOURCES, is_current=lambda: True
        )
    assert error.value.failure == expected and str(error.value) == expected.value


@pytest.mark.parametrize(
    "tls_object", [None, Mock(getpeercert=Mock(return_value=None))]
)
async def test_missing_tls_certificate_is_rejected_and_connection_closed(
    monkeypatch, tls_object
):
    writer = Mock(get_extra_info=Mock(return_value=tls_object), wait_closed=AsyncMock())
    monkeypatch.setattr(transport, "_resolve", AsyncMock(return_value=("192.0.2.5",)))
    monkeypatch.setattr(
        asyncio, "open_connection", AsyncMock(return_value=(Mock(), writer))
    )
    with pytest.raises(LocalTransportError, match="certificate_untrusted"):
        await inspect_local_certificate(
            CONTEXT, PortRole.RESOURCES, is_current=lambda: True
        )
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


@pytest.mark.parametrize(
    "failure", [OSError(), TimeoutError(), asyncio.CancelledError()]
)
async def test_failed_connection_cleanup_aborts_transport(failure):
    writer = Mock(wait_closed=AsyncMock(side_effect=failure))
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await transport._close(writer)
    else:
        await transport._close(writer)
    writer.close.assert_called_once()
    writer.transport.abort.assert_called_once()
